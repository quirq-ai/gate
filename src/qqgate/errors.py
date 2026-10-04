"""Typed gate errors. A gate that cannot compute its checks refuses; it never guesses."""


class GateError(Exception):
    """The required checks cannot be computed, or the inputs are inconsistent."""
