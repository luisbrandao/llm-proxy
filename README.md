# DeepSeek Proxy

OpenAI-compatible reverse proxy for the DeepSeek API, with built-in **Prometheus metrics**, **token counting**, and **structured logging**.

Drop it between your LLM clients (Open WebUI, LangChain, any OpenAI SDK, etc.) and DeepSeek, and start getting observability for free.

## Architecture

```
Your App (OpenAI client)
       │
       │  POST /v1/chat/completions
       ▼
┌─────────────────┐
│  deepseek-proxy  │  ← adds Authorization header, logs tokens, exports metrics
│  :8000           │
└────────┬────────┘
         │
         │  + Bearer <DEEPSEEK_API_KEY>
         ▼
   DeepSeek API
   api.deepseek.com
```

## Quick Start

```bash
cp .env.example .env
# Edit .env — set DEEPSEEK_API_KEY
docker compose up -d
```

That's it. Point any OpenAI client at `http://<host>:8000/v1/chat/completions`.

### Using a pre-built image

Push to `master` triggers an automatic build. Pull the image from GHCR:

```yaml
services:
  deepseek-proxy:
    image: ghcr.io/<your-org>/deepseek-proxy:latest
    ports:
      - "8000:8000"
    environment:
      - DEEPSEEK_API_KEY=sk-...
    restart: unless-stopped
```

## Endpoints

| Path | Method | Description |
|---|---|---|
| `/health` | `GET` | `{"status": "ok"}` — health check for Docker / load balancer |
| `/metrics` | `GET` | Prometheus-format metrics — scrape this URL |
| `/*` | any | Catch-all proxy — any request is forwarded to DeepSeek |

## Prometheus Metrics

Scrape `http://<host>:8000/metrics` to get:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `deepseek_proxy_requests_total` | Counter | `model` | Total completed requests |
| `deepseek_proxy_tokens_input_total` | Counter | `model` | Cumulative input tokens |
| `deepseek_proxy_tokens_output_total` | Counter | `model` | Cumulative output tokens |
| `deepseek_proxy_request_duration_seconds` | Histogram | `model` | Request latency (buckets: 0.1–600s) |
| `deepseek_proxy_errors_total` | Counter | `model`, `status_code` | Failed requests (upstream errors + connection errors) |

### Example Grafana queries

```promql
# Active requests per minute, per model
rate(deepseek_proxy_requests_total[5m]) * 60

# Input tokens per second, per model
rate(deepseek_proxy_tokens_input_total[5m])

# Output tokens per second, per model
rate(deepseek_proxy_tokens_output_total[5m])

# p95 request duration
histogram_quantile(0.95, rate(deepseek_proxy_request_duration_seconds_bucket[5m]))

# Error rate (last 5 minutes)
sum(rate(deepseek_proxy_errors_total[5m])) / sum(rate(deepseek_proxy_requests_total[5m]))
```

## Token Logging

Every completed request emits a single log line:

```
2026-05-29 14:19:03 - INFO - Tokens - Model: deepseek-chat, In: 29323, Out: 142, Time: 0:00:10, Speed: 13.76 t/s
```

- **In** — input tokens (`usage.prompt_tokens` from DeepSeek)
- **Out** — output tokens (`usage.completion_tokens`; parsed from the last SSE chunk for streaming)
- **Time** — wall-clock duration of the request
- **Speed** — output tokens per second

This line is always emitted, regardless of debug flags.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | yes | — | Your DeepSeek API key |
| `DEEPSEEK_BASE_URL` | no | `https://api.deepseek.com` | Override the upstream API URL (useful for testing or alternative endpoints) |
| `PORT` | no | `8000` | Port the proxy binds to |
| `LOG_INPUT` | no | `false` | When `true`, log the full proxied request (method, URL, headers, body) in curl-style format. Authorization token is masked. |
| `LOG_OUTPUT` | no | `false` | When `true`, log the full upstream response (pretty-printed JSON). For streaming, emits a single assembled JSON with `_assembled_content` and `_reasoning_content` fields. |

### Debug example

```bash
LOG_INPUT=true LOG_OUTPUT=true docker compose up
```

```
2026-05-29 14:20:00 - INFO - Request:
POST https://api.deepseek.com/v1/chat/completions
  -H 'content-type: application/json'
  -H 'authorization: Bearer ***'
  -d '{
  "model": "deepseek-chat",
  "messages": [...],
  "stream": false
}'
...
2026-05-29 14:20:01 - INFO - Response (200):
{
  "id": "...",
  "choices": [
    {
      "message": {
        "content": "Hello!"
      }
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 1,
    "total_tokens": 11
  }
}
```

## Supported Features

- ✅ Any OpenAI-compatible endpoint (`/v1/chat/completions`, `/v1/models`, etc.)
- ✅ Non-streaming responses
- ✅ SSE streaming (`stream: true`) — forwarded in real time with no buffering
- ✅ Model parameter propagated in all metrics and log lines
- ✅ Upstream errors (4xx, 5xx) forwarded faithfully to the client
- ✅ Query string forwarding
- ✅ Connection error handling with graceful metrics recording

## Building

```bash
# Via docker compose
docker compose build

# Standalone Docker build
docker build -t deepseek-proxy .

# Run with env vars inline
docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-... deepseek-proxy
```

## CI / CD

Pushes to `master` trigger a GitHub Actions workflow that:

1. Builds the Docker image
2. Pushes to GitHub Container Registry (`ghcr.io`)
3. Tags with `latest` and `master-{run_number}`

No additional secrets are required — the workflow uses `GITHUB_TOKEN`.

## Tech Stack

- **FastAPI** — async HTTP framework
- **httpx** — async HTTP client with streaming support
- **prometheus-client** — native Prometheus metric types
- **uvicorn** — ASGI server
- **Python 3.11**

## License

MIT
