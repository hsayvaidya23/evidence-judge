---
description: Build and run the Evidence Judge Docker image - a non-root, multi-stage image with the CLI as its entrypoint.
---

# Docker

The repository's `Dockerfile` builds a small image with the CLI as its entrypoint:

- **Multi-stage**: a `uv` build stage builds the wheel and installs it with the `anthropic`
  extra into a virtualenv; the runtime stage is `python:3.13-slim-bookworm` with only that
  virtualenv copied in.
- **Non-root**: runs as user `judge` (uid 10001) in `/work`.
- **Entrypoint** `evidence-judge`, default command `--help`.
- The LLM cache and redaction key file live in `/home/judge/.cache/evidence-judge`
  (`EVJ_CACHE_DIR`).

CI builds the image and judges a recorded forged run inside it, which must exit `1`.

## Build

```bash
git clone https://github.com/hsayvaidya23/evidence-judge
cd evidence-judge
docker build -t evidence-judge .
```

## Judge a run

Mount the directory holding your recording at `/work` (read-only is enough when you only print
the verdict):

```bash
docker run --rm -v "$PWD:/work:ro" evidence-judge \
  judge --trace trace.zip --answer-file answer.txt --spec spec.yaml --offline
echo $?   # 0 pass, 1 fail, 2 needs review, 3 bad input, 4 internal error
```

To write reports, mount a writable output directory:

```bash
docker run --rm -v "$PWD/runs/checkout:/work:ro" -v "$PWD/out:/out" evidence-judge \
  judge --trace trace.zip --har run.har --answer-file answer.txt --spec spec.yaml \
  --offline --out /out/verdict.json --html /out/report.html
```

The container runs as uid 10001, so the output directory must be writable by that user.

## With an LLM

Pass the key and any settings as environment variables. Mount a volume for the cache so LLM
responses and the redaction key persist between runs:

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY \
  -e EVJ_MAX_COST_USD=0.10 \
  -v evidence-judge-cache:/home/judge/.cache \
  -v "$PWD:/work:ro" \
  evidence-judge judge --trace trace.zip --answer-file answer.txt --spec spec.yaml
```

## Other commands

```bash
docker run --rm -v "$PWD:/work:ro" evidence-judge inspect --trace trace.zip
docker run --rm evidence-judge schema > run-verdict.schema.json
docker run --rm evidence-judge version
```
