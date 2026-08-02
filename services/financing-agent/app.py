import json
import os
import time
from typing import Any, Dict, List, Optional

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from internal_auth import (
    authenticate_internal_bearer,
    internal_bearer_headers,
    load_internal_credentials,
    reject_external_credential_reuse,
)
from runtime_security import (
    load_openai_compatible_settings,
    openai_request_headers,
    require_production_environment,
)


ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
ES_USERNAME = os.environ.get("ES_USERNAME", "")
ES_PASSWORD = os.environ.get("ES_PASSWORD", "")
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://embedding-service:8001")
INDEX_NAME = os.environ.get("KNOWLEDGE_INDEX", "tenant-knowledge-chunks")
LLM_SETTINGS = load_openai_compatible_settings(local_base_url="")
LLM_BASE_URL = LLM_SETTINGS.base_url
LLM_MODEL = LLM_SETTINGS.model
LLM_API_KEY = LLM_SETTINGS.api_key
LLM_TIMEOUT_SECONDS = LLM_SETTINGS.timeout_seconds
TOP_K = int(os.environ.get("TOP_K", "6"))
INTERNAL_CREDENTIALS = load_internal_credentials(
    {
        "CHAT_TO_FINANCING_TOKEN": "chat-backend",
        "FINANCING_TO_EMBEDDING_TOKEN": "financing-agent",
    }
)
EMBEDDING_TOKEN = INTERNAL_CREDENTIALS.get("financing-agent")
reject_external_credential_reuse(INTERNAL_CREDENTIALS, ("LLM_API_KEY",))

require_production_environment(("ES_USERNAME", "ES_PASSWORD"))

app = FastAPI(title="Financing RAG Agent")

REQUESTS = Counter("financing_agent_requests_total", "Financing agent requests", ["status"])
LATENCY = Histogram("financing_agent_request_seconds", "Financing agent request latency")
RETRIEVED = Counter("financing_agent_chunks_retrieved_total", "Retrieved financing chunks")


class Message(BaseModel):
    role: str
    content: str


class AnswerRequest(BaseModel):
    tenantId: str
    sessionId: Optional[str] = None
    query: str
    messages: List[Dict[str, Any]] = []


def require_chat_backend(authorization: Optional[str] = Header(default=None)) -> None:
    if (
        authenticate_internal_bearer(
            authorization,
            {
                "chat-backend": token
                for token in (INTERNAL_CREDENTIALS.get("chat-backend"),)
                if token
            },
        )
        is None
    ):
        raise HTTPException(
            status_code=401,
            detail="Internal service authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def embed_query(query: str) -> List[float]:
    response = requests.post(
        f"{EMBEDDING_URL.rstrip('/')}/embed",
        json={"texts": [query]},
        headers=internal_bearer_headers(EMBEDDING_TOKEN),
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["embeddings"][0]


def es_auth():
    if ES_USERNAME and ES_PASSWORD:
        return (ES_USERNAME, ES_PASSWORD)
    return None


def search_chunks(tenant_id: str, query: str) -> List[Dict[str, Any]]:
    vector = embed_query(query)
    body = {
        "size": TOP_K,
        "knn": {
            "field": "embedding",
            "query_vector": vector,
            "k": TOP_K,
            "num_candidates": 50,
            "filter": [
                {"term": {"tenant_id": tenant_id}},
                {"term": {"domain": "financing"}},
                {"term": {"active": True}},
            ],
        },
        "_source": ["title", "section", "text", "source_path", "doc_id", "chunk_id"],
    }
    response = requests.post(
        f"{ES_URL.rstrip('/')}/{INDEX_NAME}/_search",
        json=body,
        timeout=30,
        auth=es_auth(),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    hits = response.json().get("hits", {}).get("hits", [])
    chunks = []
    for hit in hits:
        source = hit.get("_source", {})
        source["score"] = hit.get("_score")
        chunks.append(source)
    RETRIEVED.inc(len(chunks))
    return chunks


def build_prompt(query: str, chunks: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    context = "\n\n".join(
        f"[{index + 1}] {chunk.get('title')} / {chunk.get('section')}\n{chunk.get('text')}"
        for index, chunk in enumerate(chunks)
    )
    system = """
You are a financing specialist for a home-services chat assistant.

Guardrails:
- Answer only from the retrieved financing context.
- Do not invent APRs, lender names, minimum purchase amounts, promotional periods, credit requirements, fees, or approval odds.
- Never say the customer qualifies. Say eligibility is subject to lender approval when relevant.
- If the context is insufficient, say you do not have that financing detail and offer to create a follow-up lead.
- Keep the answer concise and practical.
""".strip()
    user = f"Retrieved financing context:\n{context or '(no financing context retrieved)'}\n\nCustomer question: {query}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def call_llm(query: str, chunks: List[Dict[str, Any]]) -> Optional[str]:
    if not LLM_BASE_URL:
        return None
    payload = {
        "model": LLM_MODEL,
        "messages": build_prompt(query, chunks),
        "temperature": 0.1,
    }
    response = requests.post(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        json=payload,
        headers=openai_request_headers(LLM_API_KEY),
        timeout=LLM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"].get("content", "").strip()


def fallback_answer(chunks: List[Dict[str, Any]]) -> str:
    if not chunks:
        return (
            "I do not have enough financing information in the company documents to answer that. "
            "I can create a follow-up lead so the team can confirm current financing options."
        )
    first = chunks[0]
    return (
        "Based on the financing document I found, customers may have financing options, "
        "but eligibility and final terms are subject to lender approval. "
        f"Relevant source: {first.get('title')} / {first.get('section')}. "
        "I can create a follow-up lead if the customer wants current options confirmed."
    )


@app.get("/health")
def health():
    return {"status": "ok", "index": INDEX_NAME}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/answer", dependencies=[Depends(require_chat_backend)])
def answer(request: AnswerRequest):
    started = time.time()
    try:
        chunks = search_chunks(request.tenantId, request.query)
        llm_answer = call_llm(request.query, chunks)
        answer_text = llm_answer or fallback_answer(chunks)
        REQUESTS.labels(status="ok").inc()
        LATENCY.observe(time.time() - started)
        return {"answer": answer_text, "chunks": chunks}
    except Exception as error:
        REQUESTS.labels(status="error").inc()
        raise error
