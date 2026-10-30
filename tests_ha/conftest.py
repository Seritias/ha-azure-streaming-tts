"""Load the custom integration into the Home Assistant test harness."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant find custom_components in this repository."""
    return
