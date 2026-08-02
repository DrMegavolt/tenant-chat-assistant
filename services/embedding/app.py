import os
import time
from typing import List

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sentence_transformers import SentenceTransformer


MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
DEVICE = os.environ.get("EMBEDDING_DEVICE", "cpu")
BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", "16"))

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


def get_model() -> SentenceTransformer:
    global MODEL
    if MODEL is None:
        MODEL = SentenceTransformer(MODEL_NAME, device=DEVICE, trust_remote_code=True)
    return MODEL


@app.get("/health")
def health():
    return {"status": "ok", "modelLoaded": MODEL is not None, "model": MODEL_NAME}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/embed", response_model=EmbedResponse)
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
