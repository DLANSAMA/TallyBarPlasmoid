.PHONY: install upgrade remove build test lint qmllint typecheck check preview screenshots pot clean

APPLET := io.github.dlansama.tallybar
VENV := .venv/bin

install:
	@find $(APPLET) -type d -name "__pycache__" -prune -exec rm -rf {} +
	kpackagetool6 -t Plasma/Applet -i $(APPLET)

upgrade:
	@find $(APPLET) -type d -name "__pycache__" -prune -exec rm -rf {} +
	kpackagetool6 -t Plasma/Applet -u $(APPLET)

remove:
	kpackagetool6 -t Plasma/Applet -r $(APPLET)

build:
	@echo "Building $(APPLET).plasmoid..."
	@rm -f $(APPLET).plasmoid
	@tar --exclude="__pycache__" --exclude="*.pyc" --exclude="*.pyo" --exclude="*.bak-*" \
	     --exclude=".mypy_cache" --exclude=".ruff_cache" --exclude=".pytest_cache" \
	     --exclude=".DS_Store" --exclude="requirements.txt" \
	     -czf $(APPLET).plasmoid -C $(APPLET) .
	@tar tzf $(APPLET).plasmoid | grep -q "metadata.json" || { echo "ERROR: $(APPLET).plasmoid is missing metadata.json" >&2; exit 1; }
	@echo "Build complete."

# Run the test suite exactly as CI does (uses the .venv pytest).
test:
	$(VENV)/pytest tests/

# pyflakes-only lint (Python), matching CI — keep the tree F-clean.
lint:
	$(VENV)/python -m flake8 --select=F $(APPLET)/contents/code/ tests/ integrations/

# QML static analysis via the Qt6 qmllint — logic and rationale in tools/qmllint_gate.sh.
# The binary is resolved explicitly: on many distros a bare `qmllint` on PATH is the Qt5
# tool (which silently accepts this Qt6 tree, syntax errors included), while the Qt6 one
# lives off-PATH under /usr/lib/qt6/bin. A bare `qmllint` is only trusted if it reports 6.x.
# QMLLINT_MODE=full (default) trusts the linter's verdict; CI uses `syntax` because the
# QtQuick/Plasma QML modules aren't installable there. Missing linter: skipped locally, a
# hard failure under CI (so the gate can't rot silently).
QMLLINT ?= $(firstword $(wildcard /usr/lib/qt6/bin/qmllint /usr/lib64/qt6/bin/qmllint /usr/bin/qmllint6 /usr/bin/qmllint-qt6) \
	$(shell qmllint --version 2>/dev/null | grep -q '^qmllint 6\.' && command -v qmllint))
QMLLINT_MODE ?= full
qmllint:
	@if [ -n "$(QMLLINT)" ] && [ -x "$(QMLLINT)" ]; then \
	    echo "Running $(QMLLINT) ($$($(QMLLINT) --version), mode=$(QMLLINT_MODE)) on $(APPLET)/contents/ui/*.qml and components/*.qml ..."; \
	    sh tools/qmllint_gate.sh "$(QMLLINT)" "$(QMLLINT_MODE)" $(APPLET)/contents/ui/*.qml $(APPLET)/contents/ui/components/*.qml; \
	elif [ -n "$$CI" ]; then \
	    echo "Qt6 qmllint not found — failing because CI is set (install qt6-declarative-dev-tools)"; exit 1; \
	else \
	    echo "Qt6 qmllint not found — skipping QML lint (install qt6-declarative / qt6-declarative-dev-tools)"; \
	fi

# Type-check the Python backend with mypy (lenient; config in mypy.ini).
# Prefer uvx mypy when available (no install step needed), fall back to plain mypy.
typecheck:
	@if command -v uvx > /dev/null 2>&1; then \
	    echo "Running mypy (via uvx) on $(APPLET)/contents/code/ ..."; \
	    cd $(APPLET)/contents/code && uvx mypy . --config-file ../../../mypy.ini; \
	elif command -v mypy > /dev/null 2>&1; then \
	    echo "Running mypy on $(APPLET)/contents/code/ ..."; \
	    cd $(APPLET)/contents/code && mypy . --config-file ../../../mypy.ini; \
	else \
	    echo "mypy not found — skipping type-check (pip install mypy or: uvx mypy)"; \
	fi

# Aggregate static-analysis gate: lint + qmllint + typecheck, all STRICT.
#
# The tree is type-clean, so typecheck blocks: a gate that is allowed to stay red
# stops being read, and the first real error goes unnoticed. If mypy is not
# installed the step is skipped, not failed — it must not break a contributor who
# has neither uvx nor mypy.
check: lint qmllint typecheck

# QML preview — loads a plasmoid UI component in the standalone qml runner.
# Usage:
#   make preview                       # default: FullRepresentation
#   make preview COMPONENT=CostPopout
#   make preview COMPONENT=CompactRepresentation
COMPONENT ?= FullRepresentation
# QML_XHR_ALLOW_FILE_READ: the harness reads its telemetry fixture via
# XMLHttpRequest, which QML blocks for file:// URLs unless this is set.
# QT_FORCE_STDERR_LOGGING: without it Qt routes console.log/console.error to the
# journal and the terminal stays silent.
QML_ENV := QML_XHR_ALLOW_FILE_READ=1 QT_FORCE_STDERR_LOGGING=1
preview:
	@if command -v qml6 > /dev/null 2>&1; then \
	    echo "Launching preview for $(COMPONENT) (qml6/Qt6) ..."; \
	    $(QML_ENV) qml6 tools/preview/harness.qml -- componentName=$(COMPONENT); \
	elif command -v qml > /dev/null 2>&1; then \
	    echo "Launching preview for $(COMPONENT) (qml/Qt) ..."; \
	    $(QML_ENV) qml tools/preview/harness.qml -- componentName=$(COMPONENT); \
	else \
	    echo "Neither qml6 nor qml found — install qt6-tools or qtdeclarative6-dev-tools"; \
	    exit 1; \
	fi

# Re-render docs/screenshots/*.png from the same mock fixture the preview uses.
# Runs fully offscreen (no Plasma session, no display) and never touches real
# usage data. QT_QUICK_BACKEND=software: the offscreen platform has no GL
# surface. XDG_ICON_THEME=breeze-dark: without it Kirigami.Icon resolves the
# light Breeze glyphs, which vanish against the widget's dark card.
SHOT_ENV := $(QML_ENV) QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software \
            XDG_ICON_THEME=breeze-dark QT_QPA_PLATFORMTHEME=kde
SHOT := tools/preview/screenshot.qml
screenshots:
	@command -v qml6 > /dev/null 2>&1 || { \
	    echo "qml6 not found — install qt6-declarative (qt6-tools)"; exit 1; }
	@mkdir -p docs/screenshots
	@$(SHOT_ENV) qml6 $(SHOT) -- provider=claude      out=docs/screenshots/widget-claude.png
	@$(SHOT_ENV) qml6 $(SHOT) -- provider=antigravity out=docs/screenshots/widget-antigravity.png
	@echo "Wrote docs/screenshots/*.png"

# Regenerate the translation template from the i18n() calls in the QML.
# CLAUDE.md tells contributors to run this after adding user-visible prose; the
# target it referred to never existed, so the .pot drifted ~5 strings behind.
POT := po/plasma_applet_io.github.dlansama.tallybar.pot
pot:
	@command -v xgettext > /dev/null 2>&1 || { \
	    echo "xgettext not found — install gettext"; exit 1; }
	@xgettext --from-code=UTF-8 --language=JavaScript \
	    --keyword=i18n:1 --keyword=i18nc:1c,2 --keyword=i18np:1,2 --keyword=i18ncp:1c,2,3 \
	    --package-name=plasma_applet_$(APPLET) \
	    --copyright-holder="Dylan Reed" \
	    --msgid-bugs-address="https://github.com/DLANSAMA/TallyBarPlasmoid/issues" \
	    -o $(POT) $(APPLET)/contents/ui/*.qml $(APPLET)/contents/ui/components/*.qml
	@sed -i \
	    -e 's/^# SOME DESCRIPTIVE TITLE\./# Translation template for TallyBar./' \
	    -e 's/^# FIRST AUTHOR <EMAIL@ADDRESS>, YEAR\./# Generated by `make pot` — do not edit by hand./' \
	    $(POT)
	@echo "Wrote $(POT) ($$(grep -c '^msgid' $(POT)) entries)"

clean:
	@rm -f $(APPLET).plasmoid
	@find $(APPLET) -type d -name "__pycache__" -prune -exec rm -rf {} +
	@rm -rf $(APPLET)/contents/code/.mypy_cache .mypy_cache
