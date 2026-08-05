import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from internal_auth import (
    authenticate_internal_bearer,
    internal_bearer_headers,
    load_internal_credentials,
)
from runtime_security import require_production_environment


ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
ES_USERNAME = os.environ.get("ES_USERNAME", "")
ES_PASSWORD = os.environ.get("ES_PASSWORD", "")
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://embedding-service:8001")
INDEX_NAME = os.environ.get("KNOWLEDGE_INDEX", "tenant-knowledge-chunks")
DOCS_PATH = Path(os.environ.get("DOCS_PATH", "/data/docs"))
CHUNK_TOKENS = int(os.environ.get("CHUNK_TOKENS", "650"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "120"))
INTERNAL_CREDENTIALS = load_internal_credentials(
    {
        "SEED_TO_INGESTION_TOKEN": "seed-financing-docs",
        "INGESTION_TO_EMBEDDING_TOKEN": "ingestion-service",
    }
)
EMBEDDING_TOKEN = INTERNAL_CREDENTIALS.get("ingestion-service")

require_production_environment(("ES_USERNAME", "ES_PASSWORD"))

app = FastAPI(title="Knowledge Ingestion Service")

INGESTIONS = Counter("ingestion_runs_total", "Ingestion runs", ["domain", "status"])
CHUNKS = Counter("ingestion_chunks_total", "Chunks indexed", ["domain"])
LATENCY = Histogram("ingestion_run_seconds", "Ingestion run latency")


class IngestRequest(BaseModel):
    tenantId: str = "apex"
    domain: str = "financing"
    path: Optional[str] = None


def require_ingestion_caller(authorization: Optional[str] = Header(default=None)) -> None:
    if (
        authenticate_internal_bearer(
            authorization,
            {
                "seed-financing-docs": token
                for token in (INTERNAL_CREDENTIALS.get("seed-financing-docs"),)
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


def tokenize(text: str) -> List[str]:
    return re.findall(r"\S+", text)


def chunk_text(text: str) -> Iterable[str]:
    tokens = tokenize(text)
    if not tokens:
        return
    step = max(1, CHUNK_TOKENS - CHUNK_OVERLAP)
    for start in range(0, len(tokens), step):
        chunk = tokens[start : start + CHUNK_TOKENS]
        if chunk:
            yield " ".join(chunk)


def parse_doc(path: Path) -> Dict[str, str]:
    text = path.read_text(encoding="utf-8")
    title = path.stem.replace("-", " ").replace("_", " ").title()
    section = "General"
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip() or section
            lines.append(stripped)
        elif stripped:
            lines.append(stripped)
    return {"title": title, "section": section, "text": "\n".join(lines)}


def embed(texts: List[str], request_id: Optional[str] = None, trace_id: Optional[str] = None) -> Dict:
    headers = internal_bearer_headers(EMBEDDING_TOKEN)
    if request_id:
        headers["X-Request-Id"] = request_id
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    response = requests.post(
        f"{EMBEDDING_URL.rstrip('/')}/embed",
        json={"texts": texts},
        headers=headers,
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def es_auth():
    if ES_USERNAME and ES_PASSWORD:
        return (ES_USERNAME, ES_PASSWORD)
    return None


def create_index(dimensions: int) -> None:
    mapping = {
        "mappings": {
            "properties": {
                "tenant_id": {"type": "keyword"},
                "domain": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "section": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "text": {"type": "text"},
                "source_path": {"type": "keyword"},
                "embedding_model": {"type": "keyword"},
                "active": {"type": "boolean"},
                "created_at": {"type": "date"},
                "embedding": {
                    "type": "dense_vector",
                    "dims": dimensions,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        }
    }
    response = requests.head(f"{ES_URL.rstrip('/')}/{INDEX_NAME}", timeout=30, auth=es_auth())
    if response.status_code == 404:
        create = requests.put(
            f"{ES_URL.rstrip('/')}/{INDEX_NAME}",
            json=mapping,
            timeout=30,
            auth=es_auth(),
        )
        create.raise_for_status()


def bulk_index(docs: List[Dict]) -> None:
    lines = []
    for doc in docs:
        lines.append(json.dumps({"index": {"_index": INDEX_NAME, "_id": doc["chunk_id"]}}))
        lines.append(json.dumps(doc))
    response = requests.post(
        f"{ES_URL.rstrip('/')}/_bulk",
        data="\n".join(lines) + "\n",
        headers={"Content-Type": "application/x-ndjson"},
        timeout=120,
        auth=es_auth(),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload)[:2000])


@app.get("/health")
def health():
    return {"status": "ok", "index": INDEX_NAME, "docsPath": str(DOCS_PATH)}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/ingest", dependencies=[Depends(require_ingestion_caller)])
def ingest(
    request: IngestRequest,
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_trace_id: Optional[str] = Header(default=None, alias="X-Trace-Id"),
):
    started = time.time()
    base = Path(request.path) if request.path else DOCS_PATH / request.tenantId / request.domain
    files = sorted(path for path in base.glob("*.md") if path.is_file())
    chunks_to_embed = []
    metadata = []

    for path in files:
        parsed = parse_doc(path)
        doc_hash = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        for index, chunk in enumerate(chunk_text(parsed["text"])):
            chunks_to_embed.append(
                f"Title: {parsed['title']}\nSection: {parsed['section']}\n\n{chunk}"
            )
            metadata.append((path, parsed, doc_hash, index, chunk))

    if not chunks_to_embed:
        INGESTIONS.labels(domain=request.domain, status="empty").inc()
        return {"indexed": 0, "files": 0, "message": f"No markdown docs found in {base}"}

    embedding_payload = embed(chunks_to_embed, x_request_id, x_trace_id)
    vectors = embedding_payload["embeddings"]
    create_index(embedding_payload["dimensions"])

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    docs = []
    for (path, parsed, doc_hash, index, chunk), vector in zip(metadata, vectors):
        chunk_id = f"{request.tenantId}:{request.domain}:{path.stem}:{doc_hash}:{index:04d}"
        docs.append(
            {
                "tenant_id": request.tenantId,
                "domain": request.domain,
                "doc_id": f"{path.stem}:{doc_hash}",
                "chunk_id": chunk_id,
                "title": parsed["title"],
                "section": parsed["section"],
                "text": chunk,
                "source_path": str(path),
                "embedding_model": embedding_payload["model"],
                "embedding": vector,
                "active": True,
                "created_at": now,
            }
        )

    bulk_index(docs)
    CHUNKS.labels(domain=request.domain).inc(len(docs))
    INGESTIONS.labels(domain=request.domain, status="ok").inc()
    LATENCY.observe(time.time() - started)
    return {"indexed": len(docs), "files": len(files), "index": INDEX_NAME}
