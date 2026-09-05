"""Pytest configuration for salt-pepper."""

from __future__ import annotations

import json
import os
from typing import Any, Generator

import pytest


def pytest_configure(config: Any) -> None:
    """Disable tests that require a live salt-api."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require a running salt-api "
        "(deselect with '-m \"not integration\"')",
    )
