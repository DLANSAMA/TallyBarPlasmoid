// Pure UI formatting and URL helper functions for TallyBar.
//
// Imported by FullRepresentation.qml (`import "lib/ui_helpers.js" as UIHelpers`).
// All functions are pure deterministic helpers that do not require QML root state.

function tabLabel(provider) {
    if (provider === "gemini") return "Gemini";
    if (provider === "antigravity") return "Antigravity";
    if (provider === "codex") return "Codex";
    if (provider === "claude") return "Claude";
    if (provider === "grok") return "Grok";
    return "Codex";
}

function providerShortLabel(key) {
    if (key === "codex") return "Codex";
    if (key === "claude") return "Claude";
    if (key === "gemini") return "Gemini";
    if (key === "antigravity") return "Antigr.";
    if (key === "grok") return "Grok";
    return key;
}

function providerLoginSite(providerKey) {
    switch (providerKey) {
    case "claude": return "claude.ai";
    case "codex": return "chatgpt.com";
    case "gemini": return "gemini.google.com";
    case "antigravity": return "antigravity";
    default: return providerKey;
    }
}

function providerLoginUrl(providerKey) {
    switch (providerKey) {
    case "claude": return "https://claude.ai/login";
    case "codex": return "https://chatgpt.com/";
    case "gemini": return "https://gemini.google.com/";
    default: return "";
    }
}

function dashboardUrl(providerKey) {
    if (providerKey === "gemini")
        return "https://gemini.google.com/usage";
    if (providerKey === "antigravity")
        return "https://gemini.google.com/usage";
    if (providerKey === "codex")
        return "https://chatgpt.com/codex/settings/usage";
    if (providerKey === "claude")
        return "https://claude.ai/settings/usage";
    if (providerKey === "grok")
        return "https://grok.com/?_s=usage";
    return "https://chatgpt.com/codex/settings/usage";
}

function statusUrl(providerKey) {
    if (providerKey === "gemini" || providerKey === "antigravity")
        return "https://www.google.com/appsstatus/dashboard/products/npdyhgECDJ6tB66MxXyo/history";
    if (providerKey === "codex")
        return "https://status.openai.com/";
    if (providerKey === "claude")
        return "https://status.claude.com/";
    if (providerKey === "grok")
        return "https://status.x.ai/";
    return "https://status.openai.com/";
}

function statusIsBad(status) {
    switch (status) {
    case "missing-cookies":
    case "missing-cli":
    case "unauthorized":
    case "api-error":
    case "error":
    case "timeout":
    case "wallet-locked":
    case "wallet-state-unknown":
    case "not-running":
    case "no-port":
    case "missing-oauth":
    case "oauth-expired":
    case "oauth-unavailable":
        return true;
    default:
        return false;
    }
}

// The limit rows that are real capacity windows — everything except the extra-usage /
// credit / spend rows the backend flags isExtraUsage (Claude overage, Codex/Antigravity
// credit pools). Badges, pulses and panel bars key off these only, matching the backend's
// notifications, which skip isExtraUsage rows too. Loops on .length (not Array.isArray)
// so a QVariantList that crossed a Repeater boundary still works.
function usageLimits(limits) {
    const out = [];
    const src = limits || [];
    for (let i = 0; i < src.length; ++i) {
        if (src[i] && !src[i].isExtraUsage)
            out.push(src[i]);
    }
    return out;
}

// The statuses that need the user to act (sign in / unlock the wallet). Deliberately NOT
// transient timeout/api-error, which would flap the tray badge.
const ATTENTION_STATUSES = ["missing-cookies", "unauthorized", "wallet-locked", "wallet-state-unknown"];

// Whether the panel should raise NeedsAttention for this provider: an actionable status,
// or a capacity window at >= 90%. Muted providers never do; extra-usage rows never count.
function needsAttention(provider, muted) {
    if (muted || !provider)
        return false;
    if (ATTENTION_STATUSES.indexOf(String(provider.status || "")) >= 0)
        return true;
    const rows = usageLimits(provider.limits);
    for (let i = 0; i < rows.length; ++i) {
        if (Number(rows[i].percent || 0) >= 90)
            return true;
    }
    return false;
}

function menuMoneySpacing(text) {
    return String(text || "").replace(/\$(?=\d)/g, "$ ");
}

function normalizedCostLine(value, label) {
    const text = String(value || "").trim();
    if (text.length === 0)
        return "";

    const spacedText = menuMoneySpacing(text);
    const lower = text.toLowerCase();
    const lowerLabel = String(label || "").toLowerCase();
    if (lower.indexOf(lowerLabel + ":") === 0)
        return spacedText;

    if (label === "Today" && lower.endsWith(" today")) {
        const amount = text.slice(0, text.length - " today".length).trim();
        return amount.length > 0 ? "Today: " + menuMoneySpacing(amount) : spacedText;
    }
    if (label === "Last 30 days" && lower.endsWith(" last 30 days")) {
        const amount = text.slice(0, text.length - " last 30 days".length).trim();
        return amount.length > 0 ? "Last 30 days: " + menuMoneySpacing(amount) : spacedText;
    }
    return spacedText;
}

function costLineValue(text, label) {
    const value = String(text || "").trim();
    const prefix = String(label || "") + ":";
    if (value.toLowerCase().indexOf(prefix.toLowerCase()) === 0)
        return value.slice(prefix.length).trim();

    return value;
}

function compactUsd(value) {
    const v = Number(value || 0);
    if (v >= 1000)
        return "$" + (v / 1000).toFixed(1).replace(/\.0$/, "") + "K";
    if (v >= 100)
        return "$" + Math.round(v);
    if (v > 0 && v < 0.01)
        return "<$0.01";
    return "$" + v.toFixed(2);
}

// Parse a user-typed amount under the given locale's separators (Qt.locale().decimalPoint /
// .groupSeparator), returning NaN when it isn't a plain non-negative number.
//
// The budget field used `parseFloat(text.replace(/,/g, ""))`: in a decimal-comma locale
// (de_DE, fr_FR, …) the DoubleValidator accepts "12,50", and stripping every comma turned
// it into 1250 — a 100x budget. Group separators are removed, the locale's decimal point
// becomes ".", and a C-style "12.50" typed in a comma locale is still read as 12.5 (a lone
// group separator followed by 1-2 digits can only be a decimal point).
function parseLocaleAmount(text, decimalPoint, groupSeparator) {
    let t = String(text || "").replace(/[\s\u00a0\u202f]/g, "");
    const dp = String(decimalPoint || ".");
    const gs = String(groupSeparator || ",");
    if (t.length === 0)
        return NaN;
    if (t.indexOf(dp) < 0 && gs !== dp) {
        const parts = t.split(gs);
        if (parts.length === 2 && /^\d{1,2}$/.test(parts[1]))
            t = parts[0] + dp + parts[1];
    }
    if (gs !== dp)
        t = t.split(gs).join("");
    t = t.split(dp).join(".");
    if (!/^\d+(\.\d+)?$/.test(t))
        return NaN;
    return Number(t);
}

function prettyModelName(name) {
    let s = String(name || "").trim();
    if (s.length === 0 || s === "Unknown")
        return "Other";
    if (s.indexOf(" ") >= 0)
        return s;
    s = s.replace(/-\d{8}$/, "");
    s = s.replace(/-(\d)/g, " $1").replace(/-/g, " ");
    s = s.replace(/(\d) (\d)/g, "$1.$2");
    return s.replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

if (typeof module !== 'undefined') {
    module.exports = {
        tabLabel: tabLabel,
        providerShortLabel: providerShortLabel,
        providerLoginSite: providerLoginSite,
        providerLoginUrl: providerLoginUrl,
        dashboardUrl: dashboardUrl,
        statusUrl: statusUrl,
        statusIsBad: statusIsBad,
        usageLimits: usageLimits,
        needsAttention: needsAttention,
        menuMoneySpacing: menuMoneySpacing,
        normalizedCostLine: normalizedCostLine,
        costLineValue: costLineValue,
        compactUsd: compactUsd,
        parseLocaleAmount: parseLocaleAmount,
        prettyModelName: prettyModelName
    };
}
