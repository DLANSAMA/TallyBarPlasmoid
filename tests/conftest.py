"""Suite-wide isolation from the developer's real ~/.tallybar state.

Most modules import their state paths at import time (``Path.home() / ".tallybar" / …``)
and tests patch the ones they exercise. The Claude Code statusLine capture is read by
EVERY build_snapshot call as a fallback, so an unpatched run would pick up whatever the
developer's live hook last wrote — and a test asserting a failing Claude status would
pass or fail depending on whether Claude Code ran recently on that machine.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))


@pytest.fixture(autouse=True)
def _isolate_claude_statusline(tmp_path, monkeypatch):
    import providers.claude as claude_mod
    monkeypatch.setattr(claude_mod, "CLAUDE_STATUSLINE_PATH", tmp_path / "claude_statusline.json")
