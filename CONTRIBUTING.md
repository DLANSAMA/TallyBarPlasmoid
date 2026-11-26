# Contributing to TallyBar

## Before you start

Read [`CLAUDE.md`](CLAUDE.md) and the relevant `.claude/rules/*.md` file for the area you are changing. They document the architecture contracts and the tests that enforce them.

The backend (`io.github.dlansama.tallybar/contents/code/`) is **Python 3.11+ standard-library only** — do not add third-party runtime dependencies. `.venv` is for development tools and tests only.

## Local setup & checks

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
make check     # flake8 (--select=F) + Qt6 qmllint + mypy
make test      # pytest tests/
make build     # verify .plasmoid archive builds cleanly
```

## UI changes

- Run `make preview` (for example, `make preview COMPONENT=CostPopout`) to inspect QML changes in the standalone Qt6 runner without restarting Plasma.
- Run `make render-baselines` and visually compare the generated PNGs in `tests/render-baselines/` before updating them.
- Run `make pot` if you added or changed user-visible `i18n()` strings.

## Adding a provider

TallyBar needs a real response payload shape before a provider parser can be written. If you have an account (free tiers are enough), generate a local capture with:

```bash
python3 tools/capture_provider.py <provider>
```

The script writes a scrubbed `0600` JSON file locally and uploads nothing. Inspect the capture file yourself to verify no tokens, cookies, or personal details remain, then attach it using the **New provider request / capture** issue template.

## Pull requests

- Keep PRs focused on one change or fix.
- Include tests for behavior changes.
- Add an entry under `## [Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md) for user-visible changes.
- Ensure `make check` and `make test` pass before opening the PR.
