"""Database package exports."""

__all__ = [
    "DatabaseRepository",
    "SQLiteConnectionManager",
    "open_repository",
]


def __getattr__(name: str) -> object:
    if name == "SQLiteConnectionManager":
        from .connection import SQLiteConnectionManager

        return SQLiteConnectionManager
    if name in {"DatabaseRepository", "open_repository"}:
        from .repository import DatabaseRepository, open_repository

        exports = {
            "DatabaseRepository": DatabaseRepository,
            "open_repository": open_repository,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
