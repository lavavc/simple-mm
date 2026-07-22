"""Small helpers for normalizing Web3 hex/bytes boundary values."""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping, Sequence
from string import hexdigits
from typing import Any

from eth_typing import HexStr

_RPC_PATH_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>(?:https|wss)://[^/\s'\"?]+/(?:v2|v3)/)[^/?#\s'\";,]+",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>(?:https|wss)://)[^/@\s'\"]+@",
    re.IGNORECASE,
)
_QUERY_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>[?&](?:api[_-]?key|access[_-]?token|auth|authorization|key|token)=)"
    r"[^&#\s'\"]+",
    re.IGNORECASE,
)
_HEADER_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>\b(?:authorization|x-api-key|x_api_key)['\"]?\s*[:=]\s*['\"]?)"
    r"(?:(?:bearer|basic)\s+)?[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>\bbearer\s+)[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)


def _looks_like_hex(value: str) -> bool:
    return bool(value) and all(char in hexdigits for char in value)


def coerce_hex_str(raw: Any) -> str:
    """Normalize bytes-like and Web3 hash values into a plain hex string."""
    value: str
    if isinstance(raw, str):
        value = raw
    elif isinstance(raw, (bytes, bytearray, memoryview)):
        value = bytes(raw).hex()
    else:
        hex_method = getattr(raw, "hex", None)
        if callable(hex_method):
            value = str(hex_method())
        else:
            value = str(raw)

    if value.startswith(("0x", "0X")):
        return "0x" + value[2:].lower()
    if _looks_like_hex(value):
        return "0x" + value.lower()
    return value


def coerce_hex_bytes(raw: Any) -> bytes:
    """Normalize Web3 log/data payloads into raw bytes."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw)

    raw_str = coerce_hex_str(raw)
    hex_body = raw_str[2:] if raw_str.startswith("0x") else raw_str
    return bytes.fromhex(hex_body)


def as_hexstr(raw: str) -> HexStr:
    """Normalize a hex string for Web3 TypedDict request payloads."""
    return HexStr(coerce_hex_str(raw))


def redact_rpc_credentials(
    raw: object,
    *,
    sensitive_values: Sequence[str] = (),
) -> str:
    """Redact common RPC credential shapes from a diagnostic value."""
    value = str(raw)
    for sensitive in sorted(
        (item for item in sensitive_values if item),
        key=len,
        reverse=True,
    ):
        value = value.replace(sensitive, "[REDACTED]")
    value = _RPC_PATH_CREDENTIAL_RE.sub(r"\g<prefix>[REDACTED]", value)
    value = _URL_USERINFO_RE.sub(r"\g<scheme>[REDACTED]@", value)
    value = _QUERY_CREDENTIAL_RE.sub(r"\g<prefix>[REDACTED]", value)
    value = _HEADER_CREDENTIAL_RE.sub(r"\g<prefix>[REDACTED]", value)
    return _BEARER_CREDENTIAL_RE.sub(r"\g<prefix>[REDACTED]", value)


def redact_rpc_log_event(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
    *,
    sensitive_values: Sequence[str] = (),
) -> MutableMapping[str, Any]:
    """Sanitize credential-bearing values before structured log rendering."""
    seen = {id(event_dict)}
    for key, value in tuple(event_dict.items()):
        event_dict[key] = _redact_rpc_log_value(value, seen, sensitive_values)
    return event_dict


def _redact_rpc_log_value(
    value: object,
    seen: set[int],
    sensitive_values: Sequence[str],
) -> object:
    if isinstance(value, str):
        return redact_rpc_credentials(value, sensitive_values=sensitive_values)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return redact_rpc_credentials(
            bytes(value).decode("utf-8", errors="backslashreplace"),
            sensitive_values=sensitive_values,
        )
    if isinstance(value, BaseException):
        return redact_rpc_credentials(value, sensitive_values=sensitive_values)
    if isinstance(value, Mapping):
        if id(value) in seen:
            return "[REDACTED_CYCLE]"
        seen.add(id(value))
        try:
            return {
                key: (
                    "[REDACTED]"
                    if _is_credential_key(key)
                    else _redact_rpc_log_value(item, seen, sensitive_values)
                )
                for key, item in value.items()
            }
        finally:
            seen.remove(id(value))
    if isinstance(value, list):
        if id(value) in seen:
            return "[REDACTED_CYCLE]"
        seen.add(id(value))
        try:
            return [_redact_rpc_log_value(item, seen, sensitive_values) for item in value]
        finally:
            seen.remove(id(value))
    if isinstance(value, tuple):
        if id(value) in seen:
            return "[REDACTED_CYCLE]"
        seen.add(id(value))
        try:
            return tuple(_redact_rpc_log_value(item, seen, sensitive_values) for item in value)
        finally:
            seen.remove(id(value))
    if isinstance(value, Sequence):
        if id(value) in seen:
            return "[REDACTED_CYCLE]"
        seen.add(id(value))
        try:
            return [_redact_rpc_log_value(item, seen, sensitive_values) for item in value]
        finally:
            seen.remove(id(value))
    return value


def _is_credential_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.lower().replace("_", "-")
    return normalized in {
        "access-token",
        "api-key",
        "apikey",
        "authorization",
        "x-api-key",
    }
