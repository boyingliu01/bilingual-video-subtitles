import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import agent_transcribe  # noqa: E402,F401


def test_bridge_module_loads():
    assert hasattr(agent_transcribe, "main") or callable(agent_transcribe)
