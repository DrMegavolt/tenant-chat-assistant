import os
import time
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sentence_transformers import SentenceTransformer

from internal_auth import authenticate_internal_bearer, load_internal_credentials


MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
MODEL_REVISION = os.environ.get(
    "EMBEDDING_MODEL_REVISION", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
)
DEVICE = os.environ.get("EMBEDDING_DEVICE", "cpu")
BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", "16"))
INTERNAL_CREDENTIALS = load_internal_credentials(
    {
        "INGESTION_TO_EMBEDDING_TOKEN": "ingestion-service",
        "FINANCING_TO_EMBEDDING_TOKEN": "financing-agent",
    }
)

app = FastAPI(title="Qwen3 Embedding Service")
MODEL = None

REQUESTS = Counter("embedding_requests_total", "Embedding requests", ["endpoint"])
TEXTS = Counter("embedding_texts_total", "Embedded text count")
LATENCY = Histogram("embedding_request_seconds", "Embedding request latency")


class EmbedRequest(BaseModel):
    texts: List[str] = Field(min_length=1, max_length=128)


class EmbedResponse(BaseModel):
    model: str
    dimensions: int
    embeddings: List[List[float]]


def require_embedding_caller(authorization: Optional[str] = Header(default=None)) -> None:
    if authenticate_internal_bearer(authorization, INTERNAL_CREDENTIALS) is None:
        raise HTTPException(
            status_code=401,
            detail="Internal service authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_model() -> SentenceTransformer:
    global MODEL
    if MODEL is None:
        MODEL = SentenceTransformer(
            MODEL_NAME,
            device=DEVICE,
            revision=MODEL_REVISION,
            trust_remote_code=False,
        )
    return MODEL


@app.get("/health")
def health():
    return {
        "status": "ok",
        "modelLoaded": MODEL is not None,
        "model": MODEL_NAME,
        "modelRevision": MODEL_REVISION,
    }


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/embed", response_model=EmbedResponse, dependencies=[Depends(require_embedding_caller)])
def embed(request: EmbedRequest):
    started = time.time()
    REQUESTS.labels(endpoint="/embed").inc()
    TEXTS.inc(len(request.texts))
    model = get_model()
    vectors = model.encode(
        request.texts,
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    embeddings = vectors.tolist()
    LATENCY.observe(time.time() - started)
    return {
        "model": MODEL_NAME,
        "dimensions": len(embeddings[0]) if embeddings else 0,
        "embeddings": embeddings,
    }
