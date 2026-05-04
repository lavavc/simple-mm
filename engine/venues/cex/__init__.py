"""CEX venue adapters."""

__all__ = ["QuidaxAdapter"]


def __getattr__(name: str) -> object:
    if name == "QuidaxAdapter":
        from .quidax import QuidaxAdapter

        return QuidaxAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
