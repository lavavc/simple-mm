"""Typed failures shared by weighted-portfolio execution paths."""


class NoValidationSwapError(ValueError):
    """Raised when a window has no valid swap at which to value a method."""
