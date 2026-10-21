# syntax=docker/dockerfile:1.7
# Base images are pinned by tag and manifest-list digest; Dependabot (docker ecosystem) bumps both.
FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-default-groups --extra anthropic --no-emit-project \
        --format requirements-txt --output-file /tmp/requirements.txt && \
    uv build --wheel --out-dir /dist && \
    uv venv --python /usr/local/bin/python3 /opt/venv && \
    uv pip install --python /opt/venv/bin/python --require-hashes -r /tmp/requirements.txt && \
    uv pip install --python /opt/venv/bin/python --no-deps /dist/*.whl

FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56
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
