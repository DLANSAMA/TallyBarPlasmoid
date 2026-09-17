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
        menuMoneySpacing: menuMoneySpacing,
        normalizedCostLine: normalizedCostLine,
        costLineValue: costLineValue,
        compactUsd: compactUsd,
        prettyModelName: prettyModelName
    };
}
