# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE="python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
ARG UV_IMAGE="ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b"

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock .python-version ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --only-group prototype-runtime --no-install-workspace --compile-bytecode

FROM ${PYTHON_IMAGE} AS runtime
ARG SOURCE_DATE_EPOCH="0"
ARG VCS_REF="unknown"
LABEL org.opencontainers.image.created="${SOURCE_DATE_EPOCH}" \
      org.opencontainers.image.revision="${VCS_REF}"

RUN groupadd --gid 10001 tenantchat \
    && useradd --uid 10001 --gid tenantchat --create-home --shell /usr/sbin/nologin tenantchat \
    && install -d -o 10001 -g 10001 /var/lib/tenantchat/chats
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHAT_HOST=0.0.0.0 \
    CHAT_PORT=8000 \
    CHATS_DIR=/var/lib/tenantchat/chats
COPY --from=builder /app/.venv /app/.venv
# No frontend assets: the web image serves them and proxies the API here, so a
# second copy in this image could only ever be a stale one.
COPY --chown=10001:10001 server.py runtime_security.py internal_auth.py README.md ./

USER 10001:10001
EXPOSE 8000 8004
CMD ["python", "server.py"]
