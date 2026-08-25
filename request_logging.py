"""Opt-in structured request logging for A2A HTTP routes."""

import json
import logging
import uuid
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from starlette.datastructures import Headers

logger = logging.getLogger(__name__)

LOGGED_PATH_PREFIXES = ("/v1/", "/a2a/")
MAX_LOGGED_BODY_BYTES = 1_048_576
REDACTED = "[REDACTED]"
SENSITIVE_NAME_MARKERS = (
    "password",
    "secret",
    "token",
    "cookie",
    "authorization",
    "apikey",
    "clientcredential",
)


def is_request_logging_enabled(value: str | None) -> bool:
    """Return whether detailed request logging is explicitly enabled."""
    return value is not None and value.casefold() == "true"


def _is_sensitive_name(name: str) -> bool:
    normalized = "".join(
        character for character in name.casefold() if character.isalnum()
    )
    return any(marker in normalized for marker in SENSITIVE_NAME_MARKERS)


def redact_headers(headers: Headers) -> dict[str, str]:
    """Return request headers with credential-bearing values removed."""
    return {
        name: REDACTED if _is_sensitive_name(name) else value
        for name, value in headers.items()
    }


def redact_json(value: Any) -> Any:
    """Recursively redact values whose JSON keys indicate credentials."""
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive_name(str(key)) else redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


def _decode_body(request: Request, body: bytes) -> Any:
    if not body:
        return None

    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            return redact_json(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {
                "redacted": True,
                "reason": "malformed JSON body",
                "size": len(body),
            }

    return {
        "content_type": media_type or None,
        "redacted": True,
        "reason": "non-JSON body",
        "size": len(body),
    }


def _redacted_query_string(request: Request) -> str:
    query_items = [
        (name, REDACTED if _is_sensitive_name(name) else value)
        for name, value in request.query_params.multi_items()
    ]
    return urlencode(query_items)


async def _body_for_logging(request: Request) -> Any:
    content_length = request.headers.get("content-length")
    if content_length is None:
        if request.method in {"POST", "PUT", "PATCH"}:
            return {
                "redacted": True,
                "reason": "body size is unknown",
            }
        return None

    try:
        body_size = int(content_length)
    except ValueError:
        return {
            "redacted": True,
            "reason": "invalid content length",
        }

    if body_size < 0:
        return {
            "redacted": True,
            "reason": "invalid content length",
        }
    if body_size > MAX_LOGGED_BODY_BYTES:
        return {
            "limit": MAX_LOGGED_BODY_BYTES,
            "reason": "body exceeds logging limit",
            "redacted": True,
            "size": body_size,
        }

    return _decode_body(request, await request.body())


def build_request_record(request: Request, body: Any) -> dict[str, Any]:
    """Build the structured, redacted record written to application logs."""
    raw_path = request.scope.get("raw_path", b"")
    client = request.client
    return {
        "correlation_id": str(uuid.uuid4()),
        "method": request.method,
        "path": request.url.path,
        "raw_path": raw_path.decode("ascii", errors="replace"),
        "query": _redacted_query_string(request),
        "client": f"{client.host}:{client.port}" if client else None,
        "headers": redact_headers(request.headers),
        "body": body,
    }


def add_a2a_request_logging(app: FastAPI, *, enabled: bool) -> None:
    """Register request logging for A2A paths when explicitly enabled."""
    if not enabled:
        return

    @app.middleware("http")
    async def log_a2a_request(request: Request, call_next):
        if not request.url.path.startswith(LOGGED_PATH_PREFIXES):
            return await call_next(request)

        body = await _body_for_logging(request)
        record = build_request_record(request, body)
        logger.info("A2A request: %s", json.dumps(record, sort_keys=True))
        return await call_next(request)
