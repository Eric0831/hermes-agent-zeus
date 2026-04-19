"""Shared request resilience helpers for model API calls."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any


class DuplicateRequestError(RuntimeError):
    """Raised when the same logical request is already in flight."""


class RequestDedupeCache:
    """Tracks recent request fingerprints to suppress duplicate submits."""

    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 256):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._inflight: dict[str, str] = {}
        self._recent: "OrderedDict[str, tuple[str, float]]" = OrderedDict()

    def _prune_locked(self, now: float) -> None:
        expired = [
            fingerprint
            for fingerprint, (_, ts) in self._recent.items()
            if now - ts > self.ttl_seconds
        ]
        for fingerprint in expired:
            self._recent.pop(fingerprint, None)
        while len(self._recent) > self.max_entries:
            self._recent.popitem(last=False)

    def acquire(self, fingerprint: str, request_id: str) -> None:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            inflight = self._inflight.get(fingerprint)
            if inflight:
                raise DuplicateRequestError(
                    f"duplicate request suppressed (in_flight_request_id={inflight})"
                )
            self._inflight[fingerprint] = request_id

    def complete(self, fingerprint: str, request_id: str) -> None:
        now = time.time()
        with self._lock:
            self._inflight.pop(fingerprint, None)
            self._recent[fingerprint] = (request_id, now)
            self._prune_locked(now)

    def release(self, fingerprint: str) -> None:
        with self._lock:
            self._inflight.pop(fingerprint, None)


def classify_api_exception(error: Exception) -> dict[str, Any]:
    """Classify model request failures into stable error codes."""
    error_text = str(error).lower()
    status_code = getattr(error, "status_code", None)

    if isinstance(error, DuplicateRequestError):
        return {"code": "duplicate_request_suppressed", "retryable": False}

    timeout_markers = ("timeout", "timed out", "read timeout", "deadline exceeded")
    if isinstance(error, TimeoutError) or any(marker in error_text for marker in timeout_markers):
        return {"code": "timeout", "retryable": True}

    connection_markers = (
        "connection reset",
        "connection closed",
        "connection lost",
        "network connection",
        "network error",
        "remoteprotocolerror",
        "connecterror",
        "transport",
        "broken pipe",
    )
    if any(marker in error_text for marker in connection_markers):
        return {"code": "connection_reset", "retryable": True}

    if status_code == 413 or "payload too large" in error_text or "request entity too large" in error_text:
        return {"code": "payload_too_large", "retryable": True}

    if (
        status_code == 429
        or "rate limit" in error_text
        or "too many requests" in error_text
        or "rate_limit" in error_text
        or "usage limit" in error_text
        or "quota" in error_text
    ):
        return {"code": "rate_limited", "retryable": True}

    if "schema_validation_failed" in error_text:
        return {"code": "schema_validation_failed", "retryable": False}

    if isinstance(error, (ValueError, TypeError)) and not isinstance(error, UnicodeEncodeError):
        return {"code": "local_validation_error", "retryable": False}

    if isinstance(status_code, int) and 400 <= status_code < 500 and status_code not in {413, 429}:
        return {"code": "provider_4xx_non_retryable", "retryable": False}

    if status_code == 529 or (isinstance(status_code, int) and status_code >= 500):
        return {"code": "provider_transient_error", "retryable": True}

    return {"code": "provider_request_failed", "retryable": True}


def build_request_id(model: str, api_kwargs: dict[str, Any]) -> str:
    """Build a stable request ID for logs, retries, and dedupe."""
    payload = {
        "model": model,
        "messages": api_kwargs.get("messages"),
        "tools": api_kwargs.get("tools"),
        "tool_choice": api_kwargs.get("tool_choice"),
        "stream": api_kwargs.get("stream", False),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"req_{digest}"


def inject_request_id(api_kwargs: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Copy api kwargs and attach Hermes request metadata when possible."""
    cloned = dict(api_kwargs)
    extra_headers = dict(cloned.get("extra_headers") or {})
    extra_headers.setdefault("X-Hermes-Request-Id", request_id)
    cloned["extra_headers"] = extra_headers
    return cloned
