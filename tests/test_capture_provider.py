"""tools/capture_provider.py — the probe that makes a new provider cheap to add.

Only the offline halves are exercised: credential discovery, the no-credential
exit, and scrubbing. The network probes themselves need a real account, which is
the entire reason this tool exists.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
TOOL = REPO_ROOT / "tools" / "capture_provider.py"


def _load(monkeypatch, home):
    """Import the tool with HOME redirected, so credential discovery is sandboxed."""
    monkeypatch.setenv("HOME", str(home))
    spec = importlib.util.spec_from_file_location("capture_provider", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_copilot_token_found_in_nested_apps_json(monkeypatch, tmp_path):
    cfg = tmp_path / ".config" / "github-copilot"
    cfg.mkdir(parents=True)
    (cfg / "apps.json").write_text(json.dumps({
        "github.com:Iv1.abc": {"user": "octocat", "oauth_token": "gho_SECRETVALUE123"}
    }))
    mod = _load(monkeypatch, tmp_path)
    token, where = mod._copilot_token()
    assert token == "gho_SECRETVALUE123"
    assert "apps.json" in where


def test_copilot_token_absent_reports_every_location(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    token, where = mod._copilot_token()
    assert token is None
    # The message has to tell the user where to look, not just "not found".
    assert "apps.json" in where and "hosts.json" in where


def test_cursor_token_read_from_a_real_sqlite_db(monkeypatch, tmp_path):
    """Cursor stores a JWT in its VS Code-style globalStorage DB."""
    gs = tmp_path / ".config" / "Cursor" / "User" / "globalStorage"
    gs.mkdir(parents=True)
    con = sqlite3.connect(gs / "state.vscdb")
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", "eyJhbGciOi.PAYLOAD.SIG"))
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("unrelated/key", "noise"))
    con.commit(); con.close()

    mod = _load(monkeypatch, tmp_path)
    token, where = mod._cursor_token()
    assert token == "eyJhbGciOi.PAYLOAD.SIG"
    assert "state.vscdb" in where


def test_cursor_signed_out_db_is_reported_not_crashed(monkeypatch, tmp_path):
    gs = tmp_path / ".config" / "Cursor" / "User" / "globalStorage"
    gs.mkdir(parents=True)
    con = sqlite3.connect(gs / "state.vscdb")
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.commit(); con.close()

    mod = _load(monkeypatch, tmp_path)
    token, where = mod._cursor_token()
    assert token is None
    assert "signed out" in where


def test_cursor_db_is_copied_before_reading(monkeypatch, tmp_path):
    """Cursor's DB is live; we must not hold a lock on the original."""
    gs = tmp_path / ".config" / "Cursor" / "User" / "globalStorage"
    gs.mkdir(parents=True)
    db = gs / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", "tok"))
    con.commit(); con.close()
    before = db.stat().st_mtime_ns

    mod = _load(monkeypatch, tmp_path)
    mod._cursor_token()
    assert db.stat().st_mtime_ns == before      # original untouched


@pytest.mark.parametrize("provider", ["copilot", "cursor"])
def test_missing_credential_exits_2_with_guidance(tmp_path, provider):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, str(TOOL), provider],
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "No " + provider in proc.stderr
    assert "free" in proc.stderr.lower()        # tells them it costs nothing to fix


def test_capture_output_is_scrubbed(monkeypatch, tmp_path):
    """The response document is scrubbed wholesale — these payloads carry ids and
    tokens we have not catalogued, so field-by-field redaction would miss some."""
    mod = _load(monkeypatch, tmp_path)
    doc = json.dumps({"token": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                      "id_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"})
    cleaned = mod.scrub_credentials(doc)
    assert "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig" not in cleaned
    assert "REDACTED" in cleaned
