"""Cross-language parity for the compact token-count formatter.

The panel renders token counts with QML's lib/format.js (`Fmt.compactCount`); the backend cost
lines render them with Python's `accounting.compact_token_count`. The two MUST produce identical
strings or the popup and the cost summary disagree. They used to be two hand-maintained copies
(the documented "keep these in sync" foot-gun); now QML imports lib/format.js, and this test pins
BOTH implementations to one shared golden table — and, where `node` is available (CI's ubuntu
has it), runs the ACTUAL lib/format.js so a QML-side change that drifts is caught, not just a
Python one.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import pytest  # noqa: E402
import accounting  # noqa: E402

FORMAT_JS = (Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents"
             / "ui" / "lib" / "format.js")

# The shared contract. Covers the edge cases CLAUDE.md calls out: carry at the 1000-of-a-unit
# boundary (999.5K -> 1M), trailing-zero trim (13.0M -> 13M), half-up rounding.
GOLDEN = [
    (0, "0"), (1, "1"), (150, "150"), (999, "999"),
    (1000, "1K"), (1001, "1K"), (1500, "2K"), (9950, "10K"), (9999, "10K"),
    (15400, "15K"), (99999, "100K"), (100000, "100K"), (250000, "250K"),
    (999499, "999K"), (999500, "1M"), (999999, "1M"), (1000000, "1M"),
    (1200000, "1.2M"), (1500000, "1.5M"), (9999999, "10M"), (13000000, "13M"),
    (15400000, "15.4M"), (999999999, "1B"), (1000000000, "1B"), (1500000000, "1.5B"),
]


def test_python_compact_token_count_matches_golden():
    for value, expected in GOLDEN:
        assert accounting.compact_token_count(value) == expected, f"compact_token_count({value})"


def test_qml_format_js_matches_python_across_golden():
    """Run the real lib/format.js under node and assert it returns the same strings as the
    Python formatter for every golden vector — true cross-language parity. Skipped where node
    is unavailable (the Python contract above still runs)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; QML-side parity not checked")
    assert FORMAT_JS.is_file(), FORMAT_JS
    inputs = [v for v, _ in GOLDEN]
    script = (
        "const f=require(process.argv[1]);"
        "const xs=JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(xs.map(x=>f.compactCount(x))));"
    )
    proc = subprocess.run(
        [node, "-e", script, str(FORMAT_JS), json.dumps(inputs)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    qml_results = json.loads(proc.stdout)
    for (value, expected), qml in zip(GOLDEN, qml_results):
        assert qml == expected, f"format.js({value})={qml!r} != golden {expected!r}"
        assert accounting.compact_token_count(value) == qml  # python == qml for every value
