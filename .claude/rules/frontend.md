---
paths:
  - "io.github.dlansama.tallybar/contents/ui/**/*"
  - "tests/test_qml_render.py"
  - "tests/test_qml_text_format.py"
  - "tests/test_ui_helpers.py"
  - "tests/test_compact_count_parity.py"
  - "tools/preview/**/*"
---

# QML frontend

Area contracts moved out of the root CLAUDE.md; they load only when a matching file is touched.

**`main.qml` owns all state.** Three DataSources: refresh, config-write, cold-start `cacheLoader`. `liveLoaded` flag prevents slow cache read from clobbering a faster fresh fetch. `configSaving` flag preserves the in-flight config during a concurrent refresh — don't drop either branch.

**Plasma 6 API traps — both fail SILENTLY at runtime.** Enum constants (status / `backgroundHints` / `formFactor`) live on `PlasmaCore.Types`, not `Plasmoid.X` (which resolves to `undefined` and breaks the attention badge + vertical layout). `PlasmaCore.Theme` / `Kirigami.Theme` colours throw TypeErrors — the widget paints its own dark background, so use the fixed light-on-dark palette (`primaryTextColor`, `mutedTextColor`, etc.). → `tests/test_enforced_rules.py::test_no_plasma5_theme_or_plasmoid_enum_constants_in_ui`

**Every QML `Text` that renders a DYNAMIC string pins `textFormat: Text.PlainText`.** Provider messages, model names and error strings originate outside the app (API responses, local logs, the `GetAvailableModels` RPC); Qt's default AutoText would render markup in them, and an `<img src=…>` becomes an outbound network fetch from the widget. Static bindings (a string literal or `i18n()` of literals) are exempt. → `tests/test_qml_text_format.py`.

**Shared QML helpers live in `lib/format.js` (imported `as Fmt`) — don't re-inline them.** `Fmt.compactCount`/`trimDecimals` (the backend-parity formatter), `Fmt.providerAccent` (the per-provider fallback colour map), and `Fmt.pickFont(availableFonts, prefs)` (Apple-first font resolution) are each the SINGLE source shared by CompactRepresentation and FullRepresentation. `providerAccent` mirrors the backend accents; keep them aligned (→ `tests/test_enforced_rules.py::test_qml_provider_accents_match_backend`). The CommonJS `module.exports` at the file end is only for the `node` parity test — QML ignores it. → `tests/test_compact_count_parity.py`.

**Interactive controls carry `Accessible.role`/`Accessible.name` (and `.checked` for toggles).** All SettingsPopout chips/toggles and the close buttons are annotated for screen readers — match the pattern when adding a control. New user-visible prose gets `i18n()` (then `make pot`); brand names, match-keys, `$`/`×`/`$50` glyphs stay bare (see `po/`).

**Bars stay provider-accent colour at every percentage.** No warning/critical recolour to amber/red (`compactFillColor`, `tabUsageColor`, expanded metric bars). The warning *pulse* (opacity throb) is kept. Don't re-add recolour. → `tests/test_enforced_rules.py::test_usage_bars_never_recolour_by_percentage`

**`metricsBodyHeight()` mirrors each section's real height deterministically** (the popup height can't come from `implicitHeight`, which reads 0 until Repeater delegates instantiate). Section heights are single-sourced — `rowMetricHeight()`, `costSectionHeight()`, `extraUsageBodyHeight` feed BOTH the section's `Layout.preferredHeight` and the mirror; add a new section the same way. A mismatch makes `metricsScroller` interactive → unwanted scrollbar. The cost popout's `drawerHeightFor()` follows the same rule with `costBurnRowHeight`/`costModelRowHeight`.

**Cost popout is a SEPARATE `PopupPlasmaWindow` (`CostPopout.qml`) — never inline it into the widget body.** Details:
- `visualParent: flyoutAnchor` (invisible 1×1 in `FullRepresentation.qml`, `anchors.left: parent.left`), `popupDirection: Qt.LeftEdge` — the widget's outer left edge, not an interior item. This is what makes KWin place it beside (not over) the body on Wayland.
- `flyoutAnchor.anchors.verticalCenterOffset: 160` — aligns card center with the Cost row.
- `floating: true`, `animated: true`. `onActiveChanged → costDrawerOpen = false` (focus-out closes).
- Month grid width is tuned through the single `fillFraction: 0.96` value — adjust only there and re-verify live.
- Tooltip: hover shows two compact lines (date + total) ONLY — a deliberate design choice; don't add the breakdown to hover. **Click** a bar/month-day expands that bucket's tip with its per-model rows (backend per-bucket `models`, top-4 by cost — `bucket_add` + serializers in `accounting/buckets.py`); not a pin — exit or click-again collapses (`chartArea.expandedKey`). A bucket's exit tears the tip down only while `chartArea.hoverKey` is still its own: when one move carries the pointer straight from a bar into the next (past the gap), Qt delivers the new bar's enter BEFORE the old bar's exit, and an unguarded exit stopped the timer the new bar had just started, so the tip never showed. → `tests/test_qml_render.py::test_cost_popout_tooltip_shows_after_a_jump_between_bars`. QVariantList caveat: bucket `models` crossing a Repeater fails `Array.isArray` — duck-type on `.length` (`withModelLines`). `chartTip` x AND y backstop-clamped to popout window bounds; >2-line tips left-align.
