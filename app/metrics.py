from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST


REQUESTS_TOTAL = Counter(
    "deepseek_proxy_requests_total",
    "Total proxied requests",
    ["provider", "model"],
)

TOKENS_INPUT_TOTAL = Counter(
    "deepseek_proxy_tokens_input_total",
    "Total input tokens",
    ["provider", "model"],
)

TOKENS_OUTPUT_TOTAL = Counter(
    "deepseek_proxy_tokens_output_total",
    "Total output tokens",
    ["provider", "model"],
)

REQUEST_DURATION = Histogram(
    "deepseek_proxy_request_duration_seconds",
    "Request duration in seconds",
    ["provider", "model"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
)

ERRORS_TOTAL = Counter(
    "deepseek_proxy_errors_total",
    "Total errors",
    ["provider", "model", "status_code"],
)


def metrics_response():
    data = generate_latest()
    return data, 200, {"Content-Type": CONTENT_TYPE_LATEST}
