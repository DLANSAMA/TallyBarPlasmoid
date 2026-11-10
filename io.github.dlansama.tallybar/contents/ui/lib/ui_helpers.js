// Pure UI formatting and URL helper functions for TallyBar.
//
// Imported by FullRepresentation.qml (`import "lib/ui_helpers.js" as UIHelpers`).

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

// The ONE bad-status set: the tab warning glyph, the panel badge and the empty-state link all
// read it. ok / loading / idle / cookies-ready are healthy or still in progress.
function statusIsBad(status) {
    switch (status) {
    case "missing-cookies":
    case "missing-cli":
    case "unauthorized":
    case "api-error":
    case "api-empty":
    case "partial":
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
// notifications, which skip isExtraUsage rows too. An idle window (a Claude session not
// yet started) is left out as well, so the panel shows the next window that has a reading.
// Loops on .length (not Array.isArray) so a QVariantList that crossed a Repeater boundary
// still works.
function usageLimits(limits) {
    const out = [];
    const src = limits || [];
    for (let i = 0; i < src.length; ++i) {
        if (src[i] && !src[i].isExtraUsage && !src[i].idle)
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

// Escape text for a desktop-notification BODY. The freedesktop Notifications spec treats
// the body as markup (<b>, <a href>, <img>) and Plasma renders it, while the summary is
// plain text — so only the body is escaped. Bodies carry provider/API error messages
// (e.g. a Codex app-server error string), which must render literally: a "<" or "&" would
// otherwise garble the text, and an <a href>/<img> from a response would become a live
// link or a remote image fetch in the notification.
function escapeNotificationMarkup(text) {
    return String(text === undefined || text === null ? "" : text)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
    // Thresholds apply after rounding, so $999.50 carries to "$1K" the way $1000 does.
    if (Math.round(v) >= 1000)
        return "$" + (v / 1000).toFixed(1).replace(/\.0$/, "") + "K";
    if (Math.round(v) >= 100)
        return "$" + Math.round(v);
    if (v > 0 && v < 0.01)
        return "<$0.01";
    return "$" + v.toFixed(2);
}

// A percent the UI rounds itself: never "100" for a value below 100 (99.6 reads 99, as in the
// backend's text), so a bar that is not full never claims it is. No upper clamp, so a budget
// percent over 100 stays as it is.
function displayPercent(value) {
    const n = Number(value);
    if (!isFinite(n))
        return 0;
    const rounded = Math.round(n);
    return (n < 100 && rounded >= 100) ? 99 : rounded;
}

// Epoch ms of an ISO-8601 timestamp, NaN when unparseable. A string without an offset is UTC,
// as in the backend's parsers.relative_reset (Date.parse would read it as local time), and
// fraction digits beyond milliseconds are dropped (the QML JS engine rejects them).
function parseIsoMs(value) {
    const text = String(value === undefined || value === null ? "" : value);
    const m = /^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?)?\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?\s*$/.exec(text);
    if (!m)
        return NaN;
    const year = Number(m[1]), month = Number(m[2]), day = Number(m[3]);
    const hour = m[4] ? Number(m[4]) : 0, minute = m[5] ? Number(m[5]) : 0;
    const second = m[6] ? Number(m[6]) : 0;
    const millis = m[7] ? Number((m[7] + "00").slice(0, 3)) : 0;
    if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59)
        return NaN;
    const utc = Date.UTC(year, month - 1, day, hour, minute, second, millis);
    if (new Date(utc).getUTCDate() !== day)
        return NaN;
    let offsetMinutes = 0;
    if (m[8] && m[8].toUpperCase() !== "Z") {
        const digits = m[8].replace(":", "");
        offsetMinutes = (digits.charAt(0) === "-" ? -1 : 1)
            * (Number(digits.slice(1, 3)) * 60 + (digits.length > 3 ? Number(digits.slice(3, 5)) : 0));
    }
    return utc - offsetMinutes * 60000;
}

// "Resets in 2h 5m" counted from an ISO reset time against nowMs, word for word what the
// backend's parsers.relative_reset bakes at fetch time; `fallback` (the baked string) when the
// timestamp is missing or unparseable. → tests/test_frontend_display_state.py::test_relative_reset_js_matches_python_word_for_word
function relativeReset(resetAt, nowMs, fallback) {
    const at = parseIsoMs(resetAt);
    if (isNaN(at))
        return String(fallback || "");
    const diff = (at - nowMs) / 1000;
    const seconds = diff < 0 ? Math.ceil(diff) : Math.floor(diff);
    if (seconds <= 0)
        return "Reset due";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days)
        return "Resets in " + days + "d " + hours + "h";
    if (hours)
        return "Resets in " + hours + "h " + minutes + "m";
    return "Resets in " + minutes + "m";
}

// A limit row's reset text, counted down live from `resetAt` when the row's baked `reset` is a
// countdown ("Resets in ..." / "Reset due") or empty; any other text ("Monthly credit pool")
// is the backend's own and stays.
function resetText(limit, nowMs) {
    const baked = String(limit && limit.reset ? limit.reset : "");
    if (!limit || !limit.resetAt)
        return baked;
    if (baked.length > 0 && !/^(Resets in |Reset due$)/.test(baked))
        return baked;
    return relativeReset(limit.resetAt, nowMs, baked);
}

// Epoch ms a STALE provider reading was actually fetched: staleAsOf (a carried reading), else
// fetchedAt (the Grok log, the Claude statusLine). NaN for a fresh reading or when neither
// parses, so the caller falls back to the snapshot's own timestamp.
function staleAsOfMs(provider) {
    if (!provider || provider.stale !== true)
        return NaN;
    const carried = parseIsoMs(provider.staleAsOf);
    return isNaN(carried) ? parseIsoMs(provider.fetchedAt) : carried;
}

// Age in seconds of the reading a provider shows: a stale provider from its own fetch time,
// everything else from the snapshot timestamp. -1 when no usable timestamp exists.
function readingAgeSeconds(provider, snapshotTimestamp, nowMs) {
    let at = staleAsOfMs(provider);
    if (isNaN(at))
        at = parseIsoMs(snapshotTimestamp);
    if (isNaN(at))
        return -1;
    return Math.max(0, (nowMs - at) / 1000);
}

// An age as {unit, n} in the units the "Updated ..." subtitle and the tooltip word: "now"
// under 45 s, then minutes, hours, days.
function ageParts(seconds) {
    if (seconds < 45)
        return { "unit": "now", "n": 0 };
    const mins = Math.round(seconds / 60);
    if (mins < 60)
        return { "unit": "m", "n": mins };
    const hrs = Math.round(mins / 60);
    if (hrs < 24)
        return { "unit": "h", "n": hrs };
    return { "unit": "d", "n": Math.round(hrs / 24) };
}

// Model names the backend priced at an inherited or default rate for one provider
// (diagnostics.unpriced_models[provider]), as a clean list of strings.
function unpricedModels(diagnostics, providerKey) {
    const map = diagnostics && diagnostics.unpriced_models;
    const list = map ? map[providerKey] : null;
    const out = [];
    for (let i = 0; list && i < list.length; ++i) {
        const name = String(list[i] || "").trim();
        if (name.length > 0)
            out.push(name);
    }
    return out;
}

// Parse a user-typed amount under the given locale's separators (Qt.locale().decimalPoint /
// .groupSeparator), returning NaN when it isn't a plain non-negative number.
//
// Group separators are removed, the locale's decimal point becomes ".", and a C-style "12.50" typed in a comma locale is still read as 12.5 (a lone
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
        escapeNotificationMarkup: escapeNotificationMarkup,
        menuMoneySpacing: menuMoneySpacing,
        normalizedCostLine: normalizedCostLine,
        costLineValue: costLineValue,
        compactUsd: compactUsd,
        displayPercent: displayPercent,
        parseIsoMs: parseIsoMs,
        relativeReset: relativeReset,
        resetText: resetText,
        staleAsOfMs: staleAsOfMs,
        readingAgeSeconds: readingAgeSeconds,
        ageParts: ageParts,
        unpricedModels: unpricedModels,
        parseLocaleAmount: parseLocaleAmount,
        prettyModelName: prettyModelName
    };
}
