import pytest

from agent.request_resilience import (
    classify_api_exception,
    DuplicateRequestError,
    RequestDedupeCache,
    build_request_id,
    inject_request_id,
)


def test_request_dedupe_rejects_inflight_duplicate():
    cache = RequestDedupeCache(ttl_seconds=60)
    cache.acquire("fp-1", "req-1")
    with pytest.raises(DuplicateRequestError):
        cache.acquire("fp-1", "req-2")


def test_request_headers_include_request_id():
    request_id = build_request_id("gpt-test", {"messages": [{"role": "user", "content": "hi"}]})
    payload = inject_request_id({"messages": []}, request_id)
    assert payload["extra_headers"]["X-Hermes-Request-Id"] == request_id


def test_classify_rate_limit_error():
    error = RuntimeError("Rate limit exceeded")
    classified = classify_api_exception(error)
    assert classified["code"] == "rate_limited"
    assert classified["retryable"] is True


def test_classify_non_retryable_provider_4xx():
    error = RuntimeError("bad request")
    error.status_code = 401
    classified = classify_api_exception(error)
    assert classified["code"] == "provider_4xx_non_retryable"
    assert classified["retryable"] is False


def test_classify_duplicate_request_error():
    classified = classify_api_exception(DuplicateRequestError("dup"))
    assert classified["code"] == "duplicate_request_suppressed"
    assert classified["retryable"] is False
