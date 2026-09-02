import sys
from pathlib import Path

import pytest

pytest_plugins = ['nicegui.testing.user_plugin']

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: F401,E402  (registers the pages on the NiceGUI app)


@pytest.fixture
def panel():
    """The ChatPanel instance of the most recently opened simulated page."""
    import llm_panel
    return llm_panel.last_panel
