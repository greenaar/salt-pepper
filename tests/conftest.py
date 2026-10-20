"""Pytest configuration for salt-pepper."""

from __future__ import annotations

from typing import Any

import pytest

from salt_pepper import Pepper


@pytest.fixture
def api() -> Pepper:
    """A Pepper instance pointing at a fake API URL."""
    return Pepper("https://salt-api.example.com:8000")


def pytest_configure(config: Any) -> None:
    """Configure pytest for salt-pepper unit tests."""
