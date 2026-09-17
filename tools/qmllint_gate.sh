#!/bin/sh
# QML lint gate. Usage: qmllint_gate.sh <qt6-qmllint> <full|syntax> <file.qml>...
#
#   full    Trust the linter's own verdict. Needs the QtQuick + Plasma QML modules
#           installed, i.e. a real Plasma 6 dev machine.
#   syntax  Fail only on syntax errors. For environments where the modules are absent
#           (CI: Ubuntu ships neither Plasma 6 nor, by default, the QtQuick modules), where
#           an older qmllint emits thousands of "not resolved" warnings about valid code
#           and exits non-zero on them.
#
# Every run first SELF-TESTS the linter against a known-bad file. A misconfigured linter
# fails open (binary off PATH -> step skipped; a Qt5 binary -> accepts anything), so
# "the linter ran and said nothing" is not accepted as evidence without the self-test.
set -u
QMLLINT=$1; MODE=$2; shift 2

SYNTAX_RE='\[syntax\]|Expected token|Unexpected token|Syntax error'

tmp=$(mktemp -d) || exit 3
trap 'rm -rf "$tmp"' EXIT INT TERM
printf 'import QtQuick\nItem {\n    width: (\n}\n' > "$tmp/Broken.qml"
if ! "$QMLLINT" "$tmp/Broken.qml" 2>&1 | grep -Eq "$SYNTAX_RE"; then
    echo "qmllint gate: SELF-TEST FAILED — $QMLLINT did not report a deliberate syntax error." >&2
    echo "It is probably not a Qt6 qmllint; refusing to treat its silence as a pass." >&2
    exit 3
fi

case $MODE in
full)
    "$QMLLINT" --unqualified disable "$@"
    ;;
syntax)
    "$QMLLINT" "$@" > "$tmp/out.txt" 2>&1
    if grep -Eq "$SYNTAX_RE" "$tmp/out.txt"; then
        grep -E -A3 "$SYNTAX_RE" "$tmp/out.txt"
        echo "qmllint gate: syntax errors found." >&2
        exit 1
    fi
    echo "qmllint gate: no syntax errors in $# files (syntax-only mode: $(grep -c '^Warning' "$tmp/out.txt") unresolved-module warnings ignored)."
    ;;
*)
    echo "qmllint gate: unknown mode '$MODE' (want full|syntax)" >&2
    exit 2
    ;;
esac
