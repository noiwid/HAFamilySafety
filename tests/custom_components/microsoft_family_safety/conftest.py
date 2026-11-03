"""Fixtures for the Microsoft Family Safety tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading integrations from custom_components."""
    yield
