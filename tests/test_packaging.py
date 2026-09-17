"""Packaging + process-boundary smoke tests.

Two concerns the rest of the suite (which drives ``main()`` in-process) can't cover:

1. The shipped ``.plasmoid`` must carry its own license/attribution. ``make build``
   tars ``io.github.dlansama.tallybar/`` (``tar -C <applet> .``), so the repo-root
   ``LICENSE`` never lands in the package on its own — a copy has to live under the
   applet dir. These tests pin that copy byte-identical to the root so the two can't
   silently drift, and assert the third-party brand-mark ``NOTICE`` ships too.

2. QML never imports the backend — it runs ``python3 backend.py --once`` as a real
   subprocess and ``JSON.parse``s stdout. The final test exercises that exact boundary
   under an isolated ``HOME`` so it can't read or write the real ``~/.tallybar`` or any
   browser profile.
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
APPLET_DIR = REPO_ROOT / "io.github.dlansama.tallybar"
BACKEND_PY = APPLET_DIR / "contents" / "code" / "backend.py"


def test_packaged_license_is_byte_identical_to_repo_root():
    # `make build` tars only the applet dir, so the root LICENSE never ships; the copy
    # beside contents/ is what lands in the .plasmoid. Guard against drift byte-for-byte.
    root = REPO_ROOT / "LICENSE"
    packaged = APPLET_DIR / "LICENSE"
    assert root.is_file(), "repo-root LICENSE is missing"
    assert packaged.is_file(), "io.github.dlansama.tallybar/LICENSE is missing — it won't ship"
    assert packaged.read_bytes() == root.read_bytes(), (
        "packaged LICENSE drifted from the repo-root LICENSE — re-copy it"
    )


def test_notice_ships_and_points_at_logo_attribution():
    # The bundled provider logos are third-party marks; the NOTICE must ship inside the
    # package and route readers to the per-file attribution.
    notice = APPLET_DIR / "NOTICE"
    assert notice.is_file(), "io.github.dlansama.tallybar/NOTICE is missing — it won't ship"
    text = notice.read_text()
    assert "contents/images/logos/SOURCES.md" in text
    assert (APPLET_DIR / "contents" / "images" / "logos" / "SOURCES.md").is_file()


def test_backend_once_subprocess_prints_parseable_json(tmp_path):
    # Drive the REAL process boundary the widget uses: python3 backend.py --once, capture
    # stdout, json.loads it. HOME and the XDG_* dirs are redirected into tmp_path so the run
    # can't touch the real ~/.tallybar state or browser cookie stores. --no-network keeps it
    # offline (no LiteLLM/dashboard fetches); --once always exits 0, even on a fatal, because
    # backend.main() degrades to the cached/minimal snapshot rather than crashing to stderr.
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_RUNTIME_DIR": str(home / "run"),
    }
    proc = subprocess.run(
        [sys.executable, str(BACKEND_PY), "--once", "--no-network", "--timeout", "5"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    # The one-shot contract: always exit 0, always emit parseable JSON on stdout.
    assert proc.returncode == 0, f"non-zero exit; stderr:\n{proc.stderr}"
    snapshot = json.loads(proc.stdout)
    assert isinstance(snapshot, dict)
    for key in ("ok", "providers", "diagnostics"):
        assert key in snapshot, f"snapshot missing top-level key {key!r}"
    assert isinstance(snapshot["providers"], dict)


def test_built_plasmoid_ships_no_developer_caches():
    # A developer-local cache directory inside the applet tree gets tarred straight into
    # the shipped package: `make typecheck` used to drop a 5 MB .mypy_cache/ into
    # contents/code/, which tripled the .plasmoid and shipped absolute paths from the
    # build machine to every user. mypy.ini now parks its cache at the repo root AND the
    # tar excludes it — this pins both.
    make = shutil.which("make")
    if make is None or shutil.which("tar") is None:
        pytest.skip("make/tar not available")

    build = subprocess.run(
        [make, "build"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    assert build.returncode == 0, f"make build failed:\n{build.stdout}\n{build.stderr}"

    package = REPO_ROOT / "io.github.dlansama.tallybar.plasmoid"
    assert package.is_file(), "make build produced no package"

    with tarfile.open(package, "r:gz") as archive:
        names = archive.getnames()

    # The package root is "." — every other member must be a real applet file, not a
    # dot-directory (.mypy_cache, .ruff_cache, .pytest_cache, .git, ...) or a bytecode
    # artifact.
    offenders = [
        name for name in names
        if name != "."
        and (any(part.startswith(".") for part in name.lstrip("./").split("/"))
             or name.endswith((".pyc", ".pyo")))
    ]
    assert not offenders, f"developer artifacts in the shipped package: {offenders[:10]}"

    # And the things that MUST ship still do.
    for required in ("./metadata.json", "./LICENSE", "./NOTICE", "./contents/code/backend.py"):
        assert required in names, f"{required} missing from the package"


def test_packaged_metadata_exposes_no_personal_mailbox():
    # metadata.json ships inside every .plasmoid and is displayed by the widget
    # chooser, so whatever address sits in KPlugin.Authors is published. The author
    # contact is deliberately a GitHub noreply alias; bug reports route through
    # KPlugin.BugReportUrl instead. KAboutPerson::fromJSON reads "Email" with
    # QJsonObject::value().toString(), so an absent key is also valid — only a
    # personal mailbox is not.
    #
    # If the project ever adopts a real project mailbox, widen this deliberately.
    metadata = json.loads((APPLET_DIR / "metadata.json").read_text())
    authors = metadata["KPlugin"]["Authors"]
    assert authors, "KPlugin.Authors is empty"
    for author in authors:
        assert author.get("Name"), f"author entry with no Name: {author}"
        email = author.get("Email")
        if email:
            assert email.endswith("@users.noreply.github.com"), (
                f"author email {email!r} is not a noreply alias — metadata.json is published "
                "in every package; use a GitHub noreply address or drop the Email key"
            )


# --- CI / release gate parity ----------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# The four gates that must run before ANY artifact is produced. ci.yml gates merges;
# release-plasmoid.yml gates the v* tag that becomes the store.kde.org download. They
# drifted once — release ran only flake8 + pytest, so a tag could ship QML that CI had
# already rejected. Keep both lists in lockstep; this test is what stops the re-drift.
REQUIRED_GATES = {
    "flake8": "flake8 --select=F",
    "qmllint": "make qmllint",
    "mypy": "make typecheck",
    "pytest": "pytest tests/",
}


@pytest.mark.parametrize("workflow", ["ci.yml", "release-plasmoid.yml"])
@pytest.mark.parametrize("gate", sorted(REQUIRED_GATES))
def test_workflow_runs_every_gate(workflow, gate):
    text = (WORKFLOWS / workflow).read_text()
    needle = REQUIRED_GATES[gate]
    assert needle in text, (
        f"{workflow} does not run the {gate} gate ({needle!r}). A gate that runs in one "
        f"workflow but not the other lets a release ship what the other would reject."
    )


def test_release_gates_precede_the_build():
    """Order matters: every gate must run BEFORE `make build`, or a failing gate would
    still have produced the .plasmoid artifact it was supposed to block."""
    text = (WORKFLOWS / "release-plasmoid.yml").read_text()
    build_at = text.index("make build")
    for gate, needle in sorted(REQUIRED_GATES.items()):
        assert text.index(needle) < build_at, f"{gate} gate runs after `make build`"
