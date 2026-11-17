"""The host side never imports the guest agent's module.

pynixd resolves the runner from its pyproject in a Python set of its own,
with no dinit-client, and `vivarium_runner.machine` failed to import there
once `agent` imported dinit_client. `channel` holds what both sides share.
"""

from __future__ import annotations

import subprocess
import sys


def test_machine_imports_no_agent() -> None:
    probe = "import sys, vivarium_runner.machine; print(sorted({'vivarium_runner.agent', 'dinit_client'} & set(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True).stdout
    assert out.strip() == "[]"
