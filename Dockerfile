FROM python:3.11-slim

# Default to São Paulo; override with the TZ env var at runtime. tzdata is
# required for the log formatter to emit a correct local-time offset — the
# slim image ships without the zoneinfo database.
ENV TZ=America/Sao_Paulo

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The official Claude Code CLI, for `kind: claude-cli` providers (app/claude_cli.py).
# The same artifact Anthropic's install.sh fetches — pinned, sha256-checked against
# the release manifest — but installed as the bare binary, without install.sh's
# home-directory launcher and self-updater: in a container the CLI's version
# changes with the image, never underneath it. Bump to a version listed at
# https://downloads.claude.ai/claude-code-releases/stable. Above the pip layer
# so a requirements change doesn't re-download ~240 MB.
ARG CLAUDE_CODE_VERSION=2.1.285
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) platform=linux-x64 ;; \
      aarch64) platform=linux-arm64 ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    base="https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}"; \
    sum="$(curl -fsSL "$base/manifest.json" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["platforms"][sys.argv[1]]["checksum"])' "$platform")"; \
    curl -fsSL -o /usr/local/bin/claude "$base/$platform/claude"; \
    echo "$sum  /usr/local/bin/claude" | sha256sum -c -; \
    chmod 755 /usr/local/bin/claude; \
    claude --version

# All of the CLI's state — its login above all — lives in one directory, which
# the deploy mounts as a volume so a recreate does not log it out. Log in once:
#   docker exec -it llm-proxy claude auth login --claudeai
ENV CLAUDE_CONFIG_DIR=/claude \
    DISABLE_AUTOUPDATER=1

# The tokenizer the context guardrail counts with (app/trim.py, `trim.tokenizer`;
# config.DEFAULT_TOKENIZER is this path). Qwen's: every local model here is
# Qwen3.5 or later, one shared vocabulary, and on other families it still lands
# far closer than a characters-per-token guess. Pinned to a revision and
# sha256-checked, like the CLI above; 13 MB on disk, loaded (~120 MB) only once a
# request comes near its num_ctx.
ARG QWEN_TOKENIZER_REV=1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0
ARG QWEN_TOKENIZER_SHA256=0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3
RUN mkdir -p /app/tokenizers \
    && curl -fsSL -o /app/tokenizers/qwen3.8.json \
       "https://huggingface.co/Qwen/Qwen3.8-27B/resolve/${QWEN_TOKENIZER_REV}/tokenizer.json" \
    && echo "${QWEN_TOKENIZER_SHA256}  /app/tokenizers/qwen3.8.json" | sha256sum -c -

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Build identity, injected by CI (see .github/workflows/docker-build.yml) and
# read by app/version.py. Declared HERE, after the dependency install, on
# purpose: APP_VERSION changes on every single build, and an ARG placed above
# `pip install` would invalidate that layer every time and turn a 20-second
# build into a full reinstall.
#
# An undefined ARG expands to the empty string rather than being absent, which is
# why version.py treats "" as "not provided" and reports "dev".
ARG APP_VERSION=""
ARG APP_REVISION=""
ENV APP_VERSION=${APP_VERSION} \
    APP_REVISION=${APP_REVISION}

ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
