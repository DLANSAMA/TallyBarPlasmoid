# Bolt's Journal - Critical Learnings

## 2026-07-02 - Memoizing model name normalization during cost ledger calculation
**Learning:** `antigravity_ledger_cost_summary` evaluates pricing per entry across large ledgers (tens of thousands of items). Normalizing display names (`_normalize_model_name`) using regex sub operations per entry creates significant regex execution overhead despite a downstream price cache dictionary.
**Action:** Use `@functools.lru_cache(maxsize=128)` on string normalization functions like `_normalize_model_name` so string parsing and regex compilation overhead are bypassed for repeated model names across thousands of entries.
