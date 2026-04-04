"""Small helpers for normalizing Web3 hex/bytes boundary values."""

from __future__ import annotations

from string import hexdigits
from typing import Any

from eth_typing import HexStr


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
