"""Typed failures shared by weighted-portfolio execution paths."""


class ExecutionAccountingError(ValueError):
    """Raised when an execution path does not conserve marked value."""


class JointActionAffordabilityError(ExecutionAccountingError):
    """Raised when joint execution costs exceed a sleeve wallet."""


class NoValidationSwapError(ValueError):
    """Raised when a window has no valid swap at which to value a method."""
