"""Small helpers for normalizing Web3 hex/bytes boundary values."""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from string import hexdigits
from typing import Any

from eth_typing import HexStr

_RPC_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>(?:https|wss)://[^/\s'\"?]+/v2/)[A-Za-z0-9_-]+"
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


def redact_rpc_credentials(raw: object) -> str:
    """Redact credential path segments from HTTPS and WSS RPC endpoints."""
    return _RPC_CREDENTIAL_RE.sub(r"\g<prefix>[REDACTED]", str(raw))


def redact_rpc_log_event(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Sanitize credential-bearing values before structured log rendering."""
    for key, value in tuple(event_dict.items()):
        event_dict[key] = _redact_rpc_log_value(value)
    return event_dict


def _redact_rpc_log_value(value: object) -> object:
    if isinstance(value, str):
        return redact_rpc_credentials(value)
    if isinstance(value, BaseException):
        return redact_rpc_credentials(value)
    if isinstance(value, dict):
        return {key: _redact_rpc_log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_rpc_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_rpc_log_value(item) for item in value)
    return value
