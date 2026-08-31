from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SELF_TEST_PATH = REPO_ROOT / "Plugins" / "extensions" / "godot-ai" / "tests" / "test_plugin.py"
SPEC = importlib.util.spec_from_file_location("folderbridge_external_godot_ai_selftests", SELF_TEST_PATH)
assert SPEC is not None and SPEC.loader is not None
selftests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selftests)

# Re-export the plugin-local test case so the repository's standard
# `python -m unittest discover -s tests -p test_*.py -v` suite executes the
# exact same behavior tests without duplicating them here.
PluginTests = selftests.PluginTests
