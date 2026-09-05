"""Custom exceptions for salt-pepper."""


class PepperAuthException(Exception):  # noqa: N818
    """Authentication-related errors."""


class PepperArgumentsException(Exception):  # noqa: N818
    """Invalid CLI argument errors."""


class PepperException(Exception):  # noqa: N818
    """General salt-api client errors."""
