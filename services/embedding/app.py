import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import torch  # type: ignore[import-not-found]
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

from internal_auth import authenticate_internal_bearer, load_internal_credentials

logger = logging.getLogger("embedding")

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
MODEL_REVISION = os.environ.get(
    "EMBEDDING_MODEL_REVISION", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
)
DEVICE = os.environ.get("EMBEDDING_DEVICE", "cpu")
BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", "16"))
INTERNAL_CREDENTIALS = load_internal_credentials(
    {
        "INGESTION_TO_EMBEDDING_TOKEN": "job-worker",
        "CHAT_TO_EMBEDDING_TOKEN": "chat-backend",
    }
)


@asynccontextmanager
async def _lifespan(running: FastAPI) -> AsyncIterator[None]:
    # The load must start here, not on the first /ready probe: an orchestrator
    # gates traffic on readiness, so a readiness-triggered load would never be
    # reached and the pod would report "loading" forever. The thread is a
    # daemon because a half-downloaded model is worthless at shutdown.
    loader = threading.Thread(
        target=_load_model_in_thread, name="embedding-model-load", daemon=True
    )
    loader.start()
    yield


app = FastAPI(title="Qwen3 Embedding Service", lifespan=_lifespan)
MODEL: SentenceTransformer | None = None
# The load takes minutes and the sync endpoints run in a threadpool, so two
# concurrent first callers would each start a download without a lock.
_MODEL_LOCK = threading.Lock()

REQUESTS = Counter("embedding_requests_total", "Embedding requests", ["endpoint"])
TEXTS = Counter("embedding_texts_total", "Embedded text count")
LATENCY = Histogram("embedding_request_seconds", "Embedding request latency")


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=128)


class EmbedResponse(BaseModel):
    model: str
    dimensions: int
    embeddings: list[list[float]]


def require_embedding_caller(authorization: str | None = Header(default=None)) -> None:
    if authenticate_internal_bearer(authorization, INTERNAL_CREDENTIALS) is None:
        raise HTTPException(
            status_code=401,
            detail="Internal service authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _load_model() -> SentenceTransformer:
    # Qwen3-Embedding's weights are published in bfloat16; on a CPU
    # deployment the matmul then fails ("expected m1 and m2 to have the
    # same dtype, but got: float != c10::BFloat16") because the inputs
    # stay fp32. Load fp32 so weights and inputs agree.
    return SentenceTransformer(
        MODEL_NAME,
        device=DEVICE,
        revision=MODEL_REVISION,
        trust_remote_code=False,
        model_kwargs={"torch_dtype": torch.float32},
    )


def get_model() -> SentenceTransformer:
    """Load the model once, serializing concurrent first callers."""
    global MODEL
    if MODEL is None:
        with _MODEL_LOCK:
            if MODEL is None:
                MODEL = _load_model()
    return MODEL


def _load_model_in_thread() -> None:
    # A failed load (a refused download, OOM) must kill the process: the
    # daemon thread's default excepthook only writes stderr, so the pod would
    # otherwise sit unready forever with no probe to restart it. k8s restarts
    # a crashed process, which is the honest state for a service whose one
    # job cannot start.
    try:
        get_model()
    except Exception:
        logger.exception("model load failed; exiting so the orchestrator restarts the pod")
        os._exit(1)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "modelLoaded": MODEL is not None,
        "model": MODEL_NAME,
        "modelRevision": MODEL_REVISION,
    }


@app.get("/ready")
def ready() -> Any:
    """Report readiness without triggering the load itself.

    A multi-minute download started by a readiness probe would keep the pod
    out of rotation *and* burn the probe's timeout; the orchestrator needs the
    honest "still loading" answer until the model is resident.
    """
    if MODEL is None:
        return JSONResponse(
            status_code=503,
            content={"status": "loading", "modelLoaded": False, "model": MODEL_NAME},
        )
    return {"status": "ready", "modelLoaded": True, "model": MODEL_NAME}


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/embed", response_model=EmbedResponse, dependencies=[Depends(require_embedding_caller)])
def embed(request: EmbedRequest) -> dict[str, Any]:
    started = time.time()
    REQUESTS.labels(endpoint="/embed").inc()
    TEXTS.inc(len(request.texts))
    model = get_model()
    vectors: Any = model.encode(
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
