# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /dist && \
    uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python "$(ls /dist/*.whl)[anthropic]"

FROM python:3.13-slim-bookworm
LABEL org.opencontainers.image.title="evidence-judge" \
      org.opencontainers.image.description="Grade browser-agent runs by checking every claim against recorded evidence" \
      org.opencontainers.image.source="https://github.com/hsayvaidya23/evidence-judge" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --create-home --uid 10001 judge
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/home/judge/.cache \
    EVJ_CACHE_DIR=/home/judge/.cache/evidence-judge
USER judge
WORKDIR /work
ENTRYPOINT ["evidence-judge"]
CMD ["--help"]
