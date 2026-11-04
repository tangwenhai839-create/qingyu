"""Shared fixtures for the stdio adapter."""

from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
