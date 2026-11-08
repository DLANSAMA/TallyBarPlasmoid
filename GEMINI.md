# GEMINI.md

Guidance for Gemini / Google Antigravity working in this repository.

**Read [`CLAUDE.md`](CLAUDE.md).** It is the single source of truth for this
project's architecture, invariants and contracts, and it applies to every agent
working here — nothing in it is Claude-specific.

Antigravity's *global* rules live at `~/.gemini/GEMINI.md`; this file is
project-scoped.

Area contracts (Antigravity ledger, cost and pricing, providers, Grok, QML) live in `.claude/rules/*.md`. Claude Code loads them by file path; you won't, so read the one matching the area you're editing (index at the end of `CLAUDE.md`).
