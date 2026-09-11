// Shared compact token-count formatter — the SINGLE source for the "13.4M tok" style strings.
//
// Imported by FullRepresentation.qml (`import "lib/format.js" as Fmt`) for the graph labels,
// tooltips, the cost subtitle and the All-AI row. It MUST stay behaviourally identical to
// accounting.compact_token_count (Python), which prints the same strings on the backend cost
// lines. The two are cross-checked across a shared golden table in
// tests/test_compact_count_parity.py (Python asserts its own formatter; a `node` step runs
// THIS exact file). Carry at the 1000-of-a-unit boundary (999.5K -> 1M), trim trailing zeros
// (13.0M -> 13M), half-up rounding (Math.round, matching Python int(x + 0.5)).
//
// The trailing CommonJS export exists only so the node parity test can require() this file;
// QML ignores it (`module` is undefined there, so `typeof module` evaluates to "undefined").

function trimDecimals(str) {
    if (str.indexOf('.') === -1)
        return str;
    return str.replace(/0+$/, "").replace(/\.$/, "");
}

function compactCount(value) {
    let amount = Math.max(0, Math.round(Number(value || 0)));
    if (!isFinite(amount)) return "0";
    const units = [
        { div: 1e9, suffix: "B" },
        { div: 1e6, suffix: "M" },
        { div: 1e3, suffix: "K" }
    ];
    for (let i = 0; i < units.length; ++i) {
        const u = units[i];
        if (amount >= u.div) {
            let n = amount / u.div;

            if (u.suffix === "K") {
                let rounded = Math.round(n);
                if (rounded >= 1000) {
                    amount = Math.round(amount / 1000) * 1000;
                    return compactCount(amount);
                }
                return rounded.toString() + "K";
            }

            if (n >= 100) {
                let rounded = Math.round(n);
                if (rounded >= 1000) {
                    amount = Math.round(amount / u.div) * u.div;
                    return compactCount(amount);
                }
                return rounded.toString() + u.suffix;
            }

            if (n >= 10) {
                let formatted = trimDecimals(n.toFixed(1));
                if (Number(formatted) >= 100) {
                    amount = Math.round(amount / u.div) * u.div;
                    return compactCount(amount);
                }
                return formatted + u.suffix;
            }

            let formatted = trimDecimals(n.toFixed(2));
            if (Number(formatted) >= 10) {
                amount = Math.round(amount / u.div) * u.div;
                return compactCount(amount);
            }
            return formatted + u.suffix;
        }
    }
    return amount.toString();
}

// Per-provider accent fallback — the SINGLE source shared by CompactRepresentation and
// FullRepresentation (they previously each hard-coded an identical map that could drift).
// Used only before telemetry arrives / when the backend omits accentColor; PlasmaCore.Theme
// does not exist in Plasma 6, so these fixed values stand in. Keep in sync with the backend
// accents (backend.py provider_accent).
function providerAccent(providerKey) {
    if (providerKey === "claude") return "#c07f63";
    if (providerKey === "gemini") return "#2f74f5";
    if (providerKey === "antigravity") return "#30b795";
    if (providerKey === "grok") return "#1d9bf0";
    return "#218df4"; // codex / default
}

// Resolve the first installed family from an Apple-first preference list (both
// representations share the same list so the panel and popup can't pick different fonts).
// Returns "" — Qt's default application font — when none are installed.
function pickFont(availableFonts, prefs) {
    for (var i = 0; i < prefs.length; ++i) {
        if (availableFonts.indexOf(prefs[i]) >= 0)
            return prefs[i];
    }
    return "";
}

if (typeof module !== 'undefined') {
    module.exports = { compactCount: compactCount, trimDecimals: trimDecimals,
                       providerAccent: providerAccent, pickFont: pickFont };
}
