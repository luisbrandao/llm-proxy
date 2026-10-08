"""`app/usage.py`: one `Usage` out of every vendor's spelling of the usage block.

The two priced samples are real responses (OpenRouter and NanoGPT, same prompt,
same model), kept verbatim so the parser is tested against what the wire
actually carries rather than against what the docs say it should.
"""
import pytest

from app.usage import Usage, format_cost, parse

OPENROUTER = {
    "id": "gen-1791466775-Tc7VPre0OME4UeAGUXaD",
    "object": "chat.completion.chunk",
    "model": "z-ai/glm-5.3-flash",
    "provider": "OpenInference",
    "choices": [{"index": 0, "delta": {"content": "", "role": "assistant"}, "finish_reason": "stop"}],
    "usage": {
        "prompt_tokens": 6903,
        "completion_tokens": 57,
        "total_tokens": 6960,
        "cost": 0.000368968443,
        "is_byok": False,
        "prompt_tokens_details": {
            "cached_tokens": 6834, "cache_write_tokens": 0, "audio_tokens": 0, "video_tokens": 0,
        },
        "cost_details": {
            "upstream_inference_cost": 0.000368968443,
            "upstream_inference_prompt_cost": 0.000138888,
            "upstream_inference_completions_cost": 0.000230080443,
        },
        "completion_tokens_details": {"reasoning_tokens": 21, "image_tokens": 0, "audio_tokens": 0},
    },
}

NANOGPT = {
    "id": "chatcmpl-eb4e5fde-8240-4ad2-adac-3f092fe703b3",
    "object": "chat.completion.chunk",
    "model": "z-ai/glm-5.3-flash",
    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    "x_nanogpt_pricing": {
        "amount": 0.0006954, "currency": "USD", "cost": 0.0006954,
        "inputTokens": 6903, "outputTokens": 139, "cacheReadInputTokens": 0, "cacheCost": 0,
        "requestId": "req_32940e34-526c-421c-8486-99276310c3ba",
        "costUsd": 0.0006954, "usdCost": 0.0006954, "paymentSource": "USD",
        "usageSource": "provider", "cacheUsageReported": True,
    },
    "usage": {
        "prompt_tokens": 6903,
        "completion_tokens": 139,
        "total_tokens": 7042,
        "reasoning_tokens": 90,
        "completion_tokens_details": {
            "reasoning_tokens": 90, "audio_tokens": 0,
            "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0,
        },
        "cache_read_input_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0, "created_cache_tokens": 0, "audio_tokens": 0},
        "cache_creation_input_tokens": 0,
        "citation_tokens": 0,
        "num_search_queries": 0,
        "input_tokens": 6903,
        "cost": 0.0006954,
        "currency": "USD",
    },
}


def test_openrouter_sample():
    u = parse(OPENROUTER)
    assert (u.prompt, u.completion) == (6903, 57)
    assert u.cached == 6834 and u.cache_write == 0
    assert u.reasoning == 21
    assert u.cost == pytest.approx(0.000368968443) and u.currency == "USD" and u.priced


def test_nanogpt_sample():
    u = parse(NANOGPT)
    assert (u.prompt, u.completion) == (6903, 139)
    assert u.cached == 0 and u.cache_write == 0
    assert u.reasoning == 90
    assert u.cost == pytest.approx(0.0006954) and u.currency == "USD"


def test_nanogpt_pricing_block_is_the_fallback_for_the_price():
    """`usage.cost` is what both vendors send today; the sibling pricing block
    covers a NanoGPT response that carries only that."""
    data = {"usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "x_nanogpt_pricing": {"cost": 0.25, "currency": "usd"}}
    u = parse(data)
    assert u.cost == 0.25 and u.currency == "USD"


def test_deepseek_cache_hits_are_cached_tokens():
    u = parse({"usage": {"prompt_tokens": 500, "completion_tokens": 9,
                         "prompt_cache_hit_tokens": 448, "prompt_cache_miss_tokens": 52}})
    assert u.cached == 448 and u.cache_write == 0


def test_anthropic_spelling_of_cache_reads_and_writes():
    u = parse({"usage": {"prompt_tokens": 60, "completion_tokens": 3,
                         "cache_read_input_tokens": 40, "cache_creation_input_tokens": 15}})
    assert u.cached == 40 and u.cache_write == 15


def test_two_spellings_of_one_quantity_take_the_larger():
    """A vendor that repeats the figure under two names agrees with itself; a
    stale zero in one of them must not hide the real number in the other."""
    u = parse({"usage": {"prompt_tokens": 100, "completion_tokens": 1,
                         "cache_read_input_tokens": 70,
                         "prompt_tokens_details": {"cached_tokens": 0}}})
    assert u.cached == 70


@pytest.mark.parametrize("data", [
    {}, {"usage": None}, {"usage": "lots"}, {"choices": []}, "not a dict", None, [1, 2],
])
def test_no_usage_block_is_none(data):
    assert parse(data) is None


def test_an_unpriced_response_has_no_cost_rather_than_a_zero_one():
    u = parse({"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    assert u.cost is None and u.currency is None and not u.priced


def test_a_free_model_reports_zero_and_zero_is_kept():
    u = parse({"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0}})
    assert u.cost == 0.0 and u.priced and u.currency == "USD"


def test_malformed_fields_zero_out_instead_of_raising():
    u = parse({"usage": {"prompt_tokens": "many", "completion_tokens": None, "cost": "1.2",
                         "prompt_tokens_details": "nope", "completion_tokens_details": None,
                         "cache_read_input_tokens": True}})
    assert u == Usage()


@pytest.mark.parametrize("cost,text", [
    (None, None), (0.0, "0"), (0.000368968443, "0.00036897"), (0.0006954, "0.0006954"),
    (1.5, "1.5"), (12.0, "12"), (1e-9, "0"),
])
def test_cost_formats_as_a_plain_decimal_for_the_log(cost, text):
    assert format_cost(cost) == text
