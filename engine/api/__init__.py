"""API package exports."""

from __future__ import annotations

from typing import Any

__all__ = ["api_router"]


def __getattr__(name: str) -> Any:
    if name == "api_router":
        from engine.api.router import api_router

        return api_router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
