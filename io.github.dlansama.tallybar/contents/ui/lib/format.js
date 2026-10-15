// Shared compact token-count formatter — the SINGLE source for the "13.4M tok" style strings.
//
// Must match accounting.compact_token_count (Python), which prints the same strings on the
// backend cost lines: carry at the 1000-of-a-unit boundary (999.5K -> 1M), trim trailing
// zeros, half-up rounding (Math.round, matching Python int(x + 0.5)).
// → tests/test_compact_count_parity.py
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

// Per-provider accent fallback for before telemetry arrives / a missing accentColor.
// Must match the backend accents. → tests/test_enforced_rules.py::test_qml_provider_accents_match_backend
function providerAccent(providerKey) {
    if (providerKey === "claude") return "#c07f63";
    if (providerKey === "gemini") return "#2f74f5";
    if (providerKey === "antigravity") return "#30b795";
    if (providerKey === "grok") return "#1d9bf0";
    if (providerKey === "grokbot") return "#e6e6e6";
    return "#218df4";
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
