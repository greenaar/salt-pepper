"""Retcode validation for salt-api responses."""

from __future__ import annotations

from typing import Any


class PepperRetcode:
    """
    Validate ``retcode`` values in Salt command results.

    Each ``validate_*`` static method implements a different failure
    policy.
    """

    def validate(self, options: Any, result: Any) -> int:
        """
        Dispatch to the correct validation method based on *options*.

        Args:
            options: The parsed CLI arguments (must have ``fail_any``,
                ``fail_any_none``, etc. attributes).
            result: The raw result dict/list from salt-api.

        Returns:
            An exit code (0 = success, >0 = failure, -1 = no data).
        """
        if getattr(options, "fail_any", False):
            return self.validate_fail_any(result)
        if getattr(options, "fail_any_none", False):
            return self.validate_fail_any_none(result)
        if getattr(options, "fail_all", False):
            return self.validate_fail_all(result)
        if getattr(options, "fail_all_none", False):
            return self.validate_fail_all_none(result)
        return 0

    @staticmethod
    def _retcodes(result: Any) -> list[int]:
        """Extract all non-None retcode values from the first minion dict."""
        if not isinstance(result, list) or not result:
            return []
        first = result[0]
        if not isinstance(first, dict):
            return []
        return [
            first[name].get("retcode")
            for name in first
            if isinstance(first[name], dict)
            and first[name].get("retcode") is not None
        ]

    @staticmethod
    def validate_fail_any(result: Any) -> int:
        """Fail if **any** retcode is non-zero. Return 0 if no retcodes."""
        codes = PepperRetcode._retcodes(result)
        return next((c for c in codes if c != 0), 0)

    @staticmethod
    def validate_fail_any_none(result: Any) -> int:
        """Fail if **any** retcode is non-zero **or** no retcodes exist."""
        codes = PepperRetcode._retcodes(result)
        if not codes:
            return -1
        return next((c for c in codes if c != 0), 0)

    @staticmethod
    def validate_fail_all(result: Any) -> int:
        """Fail only if **all** retcodes are non-zero."""
        codes = PepperRetcode._retcodes(result)
        if codes and all(c != 0 for c in codes):
            return next(c for c in codes if c != 0)
        return 0

    @staticmethod
    def validate_fail_all_none(result: Any) -> int:
        """Fail if **all** retcodes are non-zero **or** no retcodes exist."""
        codes = PepperRetcode._retcodes(result)
        if not codes:
            return -1
        if all(c != 0 for c in codes):
            return next(c for c in codes if c != 0)
        return 0
