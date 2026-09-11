"""Provider-specific API callers and data acquisition logic.

Each provider (Gemini, Claude, Codex/OpenAI, Antigravity) has its own fetch
path that returns a normalised provider dict suitable for the QML UI.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from accounting import (
    antigravity_credit_state,
    antigravity_tier,
)
from http_helpers import scrub_credentials
from io_helpers import atomic_write_text
from parsers import (
    parse_antigravity_limits,
    parse_antigravity_quota_summary,
)


# Hosts that receive an Authorization: Bearer header or the OAuth refresh-token body. A 3xx
# from any of these is treated as an attack/misconfig, never followed.
# NOTE: kept only for the test monkeypatch seam and documentation; the actual gate lives in
# _is_credentialed_google_host, which also matches channel-prefixed Cloud Code hosts (see below).
_CREDENTIALED_GOOGLE_HOSTS = ("oauth2.googleapis.com", "cloudcode-pa.googleapis.com")


def _is_credentialed_google_host(host: str) -> bool:
    """True for any host that receives Google OAuth/Cloud-Code credentials and must never
    have a 3xx followed (the redirect would copy the Bearer header / refresh-token body
    cross-origin). Matches ``oauth2.googleapis.com`` (exact or dot-suffix) and ANY host
    ending ``cloudcode-pa.googleapis.com`` — a plain endswith (no dot boundary) so the
    channel-prefixed live hosts ``daily-cloudcode-pa`` / ``autopush-cloudcode-pa`` /
    ``staging-cloudcode-pa`` are covered (antigravity_cloudcode_host() returns e.g.
    ``daily-cloudcode-pa.googleapis.com`` on this machine — the char before ``cloudcode``
    is ``-``, not ``.``, so the old dot-suffix gate missed it). Over-blocking is fail-safe:
    a wrongly refused redirect just raises HTTPError into the existing scrubbed handling.
    Any hosts monkeypatched into _CREDENTIALED_GOOGLE_HOSTS (tests) are also honoured."""
    if not host:
        return False
    if host == "oauth2.googleapis.com" or host.endswith(".oauth2.googleapis.com"):
        return True
    if host.endswith("cloudcode-pa.googleapis.com"):
        return True
    return any(host == h or host.endswith("." + h) for h in _CREDENTIALED_GOOGLE_HOSTS)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow 3xx on credentialed Google calls.

    CPython's default HTTPRedirectHandler copies ALL request headers — including the
    Authorization: Bearer / refresh-token body — onto a cross-origin redirect target with no
    same-origin check. The credentialed Google endpoints never legitimately redirect, so for a
    request whose host is one of them we return None: urllib then raises the 3xx as an HTTPError
    (which flows into the existing scrubbed error handling) rather than leaking the credential to
    the redirect Location. Non-credentialed hosts (pricing catalog on raw.githubusercontent, the
    local language server) keep normal redirect following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
        if _is_credentialed_google_host(host):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Installed as the process-wide default opener so plain urllib.request.urlopen (used by the
# credentialed Google calls below) refuses redirects for those hosts only. build_opener with just
# the redirect handler keeps CPython's default HTTPSHandler / verifying TLS context — verification
# is NOT dropped. Installing globally (rather than calling opener.open directly) preserves the
# urllib.request.urlopen mocking seam the tests rely on, and the per-host gate keeps every other
# provider's redirect behaviour unchanged.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())
urllib.request.install_opener(_NO_REDIRECT_OPENER)


ANTIGRAVITY_MODEL_LABELS = ("Gemini", "Others")
ANTIGRAVITY_REMOTE_BASE_URL = "https://cloudcode-pa.googleapis.com"
# agy's Cloud Code host varies by build channel — this machine's agy hits the DAILY host, and
# prod vs daily return DIFFERENT quota numbers, so we must call whichever host agy uses or the
# percentages won't match /usage. The host isn't in any config (it's baked into the agy binary),
# so antigravity_cloudcode_host() detects it from agy's own request logs, defaulting to prod.
ANTIGRAVITY_CLOUDCODE_LOG_DIR = Path.home() / ".gemini" / "antigravity-cli" / "log"
_ANTIGRAVITY_CLOUDCODE_HOST_RE = re.compile(r"https://([A-Za-z0-9.-]*cloudcode-pa\.googleapis\.com)")
ANTIGRAVITY_BINARY_SCAN_CHUNK_BYTES = 10 * 1024 * 1024  # 10 MiB window when scanning the language-server binary for OAuth client pairs
ANTIGRAVITY_BINARY_SCAN_OVERLAP_BYTES = 1024  # carry-over so an OAuth id/secret split across two read chunks is still matched



# ---------------------------------------------------------------------------
# Antigravity local language server
# ---------------------------------------------------------------------------

def _parse_language_server_cmdline(cmd_str: str, args: list[str]) -> tuple[str, str] | None:
    """If a process cmdline is an Antigravity language server, return its (csrf_token, scheme)."""
    if "language_server" not in cmd_str or "--app_data_dir" not in cmd_str or "antigravity" not in cmd_str:
        return None
    token = None
    for i, arg in enumerate(args):
        if arg == "--csrf_token" and i + 1 < len(args):
            token = args[i + 1]
            break
        elif arg.startswith("--csrf_token="):
            token = arg.split("=", 1)[1]
            break
    if not token:
        return None
    scheme = "https" if "--https_server_port" in cmd_str else "http"
    return token, scheme


def _parse_agy_cmdline(args: list[str]) -> tuple[str, str] | None:
    """If a process cmdline is the Antigravity CLI (``agy``), return ``("", "http")``.

    The CLI runs an *in-process* server on loopback that speaks the same Connect-RPC
    API as the Desktop/IDE language servers but requires NO CSRF token (verified live
    2026-06-09: GetAllCascadeTrajectories / GetCascadeTrajectoryGeneratorMetadata /
    GetUserStatus / GetAvailableModels all answer token-less on the CLI's listening
    ports, plain http). The empty token tells the POST helpers to omit the CSRF
    header. Without this, CLI sessions are invisible to the RPC harvest — agy's
    cmdline is just ``agy``, which fails the language-server gate above.
    """
    if not args or os.path.basename(args[0] or "") != "agy":
        return None
    return "", "http"


# One-shot-process scan memo. The backend runs as `python3 backend.py --once` and exits,
# so the set of running Antigravity language servers cannot change within a single run —
# scanning /proc twice per refresh (once for GetUserStatus / account status via
# find_antigravity_process, once for the RPC token harvest in collect_antigravity_rpc_usage)
# is pure duplicated work. Cache the first scan for the life of THIS process only. SAFE ONLY
# because the backend is one-shot: do NOT reuse this memo in a long-lived/daemon context,
# where processes come and go and a cached list would go stale.
_process_scan_cache: "list[tuple[int, str, str]] | None" = None


def find_antigravity_processes() -> list[tuple[int, str, str]]:
    """Every running Antigravity language server as ``(pid, csrf_token, scheme)``, scanned at
    most once per backend process (memoized in ``_process_scan_cache`` — see its note)."""
    global _process_scan_cache
    if _process_scan_cache is None:
        _process_scan_cache = _scan_antigravity_processes()
    return _process_scan_cache


def _reset_process_scan_cache() -> None:
    """Drop the one-shot scan memo so the next call re-scans. Test hook only — the real
    one-shot backend never needs it (the process exits after a single refresh)."""
    global _process_scan_cache
    _process_scan_cache = None


def _scan_antigravity_processes() -> list[tuple[int, str, str]]:
    """Every running Antigravity language server as ``(pid, csrf_token, scheme)``.

    There can be more than one — the standalone Desktop app and the IDE each spawn their own
    language server with its own loaded conversations and its own CSRF token, and every running
    Antigravity CLI (``agy``) serves the same API in-process (token-less). Querying all of them
    is what lets token capture cover every surface, not just whichever process is found first.
    Real language servers sort before agy processes so account-level calls that take the first
    process keep preferring the Desktop/IDE server when one is up.
    """
    found: dict[int, tuple[int, str, str]] = {}
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline') or []
                if not cmdline:
                    continue
                parsed = (_parse_language_server_cmdline(" ".join(cmdline), cmdline)
                          or _parse_agy_cmdline(cmdline))
                if parsed:
                    found[proc.info['pid']] = (proc.info['pid'], parsed[0], parsed[1])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except ImportError:
        pass

    # /proc iteration (the stdlib-only runtime path; also catches anything psutil missed).
    try:
        proc_entries = os.listdir("/proc")
    except FileNotFoundError:
        proc_entries = []
    for pid_str in proc_entries:
        if not pid_str.isdigit() or int(pid_str) in found:
            continue
        try:
            with open(f"/proc/{pid_str}/cmdline", "r", encoding="utf-8", errors="replace") as f:
                cmdline = f.read()
        except OSError:
            continue
        args = cmdline.split("\0")
        parsed = _parse_language_server_cmdline(cmdline, args) or _parse_agy_cmdline(args)
        if parsed:
            found[int(pid_str)] = (int(pid_str), parsed[0], parsed[1])
    return sorted(found.values(), key=lambda t: t[1] == "")


def find_antigravity_process() -> tuple[int, str, str] | None:
    """The first running Antigravity language server (for account-level calls like GetUserStatus,
    where any of the user's language servers reports the same shared-account status)."""
    procs = find_antigravity_processes()
    return procs[0] if procs else None


def antigravity_ports(pid: int) -> tuple[list[int], str]:
    ports: list[int] = []
    try:
        import psutil
        try:
            proc = psutil.Process(pid)
            # psutil deprecated Process.connections() in favour of net_connections();
            # prefer the new name and fall back, and catch AttributeError so a future
            # psutil that removes connections() degrades to lsof instead of erroring.
            get_conns = getattr(proc, "net_connections", None) or proc.connections
            for conn in get_conns(kind='tcp'):
                if conn.status == psutil.CONN_LISTEN:
                    ports.append(conn.laddr.port)
            if ports:
                return sorted(list(set(ports))), ""
        except (psutil.Error, AttributeError):
            pass
    except ImportError:
        pass

    lsof_error = ""
    # Fallback to lsof
    lsof = shutil.which("lsof")
    if lsof:
        try:
            output = subprocess.check_output(
                [lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            )
            for match in re.finditer(r":(\d+)\s+\(LISTEN\)", output):
                port = int(match.group(1))
                if port not in ports:
                    ports.append(port)
        except subprocess.TimeoutExpired as e:
            lsof_error = f"lsof timeout: {e}"
        except Exception as e:
            lsof_error = f"lsof failed: {e}"
            
    if not ports:
        ports = antigravity_ports_from_proc(pid)
    return sorted(list(set(ports))), lsof_error


def antigravity_ports_from_proc(pid: int) -> list[int]:
    fd_dir = Path(f"/proc/{pid}/fd")
    if not fd_dir.is_dir():
        return []
    socket_inodes: set[str] = set()
    try:
        for entry in fd_dir.iterdir():
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if target.startswith("socket:["):
                socket_inodes.add(target[len("socket:[") : -1])
    except OSError:
        return []
    if not socket_inodes:
        return []
    ports: list[int] = []
    for proc_file in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
        try:
            with open(proc_file, "r", encoding="ascii") as handle:
                next(handle, None)  # header
                for line in handle:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    if parts[3] != "0A":  # TCP_LISTEN
                        continue
                    if parts[9] not in socket_inodes:
                        continue
                    port_hex = parts[1].rsplit(":", 1)[-1]
                    try:
                        port = int(port_hex, 16)
                    except ValueError:
                        continue
                    if port not in ports:
                        ports.append(port)
        except OSError:
            continue
    return ports


def post_local_json(url: str, token: str, timeout: float) -> tuple[int, Any]:
    body = json.dumps(
        {
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "ideVersion": "unknown",
                "locale": "en",
            }
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if token:
        headers["X-Codeium-Csrf-Token"] = token  # agy's in-process server is token-less
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    context = ssl.create_default_context()
    if url.startswith("https://127.0.0.1") or url.startswith("https://localhost"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        text = response.read(2_000_000).decode("utf-8", errors="replace")
        return int(response.status), json.loads(text)


# Connect-RPC surface on the local language server used for token telemetry.
_LS_RPC_BASE = "/exa.language_server_pb.LanguageServerService"
_LS_RPC_METADATA = {
    "metadata": {
        "ideName": "antigravity",
        "extensionName": "antigravity",
        "ideVersion": "unknown",
        "locale": "en",
    }
}


def _as_int(value: Any) -> int:
    # The RPC reports token counts as strings ("24078"); coerce safely.
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rpc_local_date(iso: str) -> str:
    """Convert an RPC ISO-8601 UTC timestamp to a *local* ``YYYY-MM-DD``.

    The language server emits ``createdAt`` as a UTC instant with 9-digit (nanosecond)
    fractional seconds and a trailing ``Z`` (e.g. ``2026-05-30T00:30:48.137443809Z``).
    ``datetime.fromisoformat`` only accepts <=6 fractional digits, so trim first; then
    convert to the local day — naive ``[:10]`` slicing would bucket a generation made at
    17:30 local on the 29th onto the 30th (UTC), the exact off-by-one that misdates
    near-midnight usage. Returns ``""`` if unparseable so the caller falls back to today.
    """
    s = (iso or "").strip()
    if not s:
        return ""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # trim ns -> us so fromisoformat accepts it
    try:
        return dt.datetime.fromisoformat(s).astimezone().date().isoformat()
    except ValueError:
        return ""


def _rpc_local_hour(iso: str) -> int | None:
    """Local hour-of-day (0..23) for an RPC ``createdAt`` UTC instant, else ``None``.

    Mirrors :func:`_rpc_local_date`'s parsing (ns->us trim, UTC->local) but yields the
    hour, so the cost popout's Day view can bucket *today's* generations by when they
    occurred. ``None`` for an unparseable/empty stamp (the entry then carries no hour).
    """
    s = (iso or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        return dt.datetime.fromisoformat(s).astimezone().hour
    except ValueError:
        return None


def _post_local_rpc(url: str, token: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
    """POST a Connect-RPC request to the local language server and parse the JSON reply.

    Same loopback + CSRF contract as ``post_local_json`` but with a caller-supplied body
    (the trajectory endpoints need a ``cascadeId``, not just the metadata envelope).
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if token:
        headers["X-Codeium-Csrf-Token"] = token  # agy's in-process server is token-less
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    context = ssl.create_default_context()
    if url.startswith("https://127.0.0.1") or url.startswith("https://localhost"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        text = response.read(16_000_000).decode("utf-8", errors="replace")
        return int(response.status), json.loads(text)


def collect_antigravity_rpc_usage(
    timeout: float,
    known: "dict[str, str] | None" = None,
    deadline: "float | None" = None,
    learned_names: "dict[str, str] | None" = None,
) -> "tuple[dict[str, list[dict[str, Any]]], dict[str, str]]":
    """Harvest per-generation token usage from the local Antigravity language server.

    This is the authoritative, format-stable source for Antigravity token telemetry: it reads
    the live language-server memory over Connect-RPC rather than reverse-engineering the
    trajectory-DB protobufs, whose layout shifts between Antigravity releases (2.0 moved usage out
    of ``steps.metadata`` into ``gen_metadata`` and changed its marker, silently breaking the disk
    scan). ``GetAllCascadeTrajectories`` lists the conversations currently loaded in LS memory; then
    ``GetCascadeTrajectoryGeneratorMetadata`` returns each generation's ``usage`` with clean fields
    (``inputTokens`` / ``cacheReadTokens`` / ``outputTokens``) plus ``apiProvider`` and a
    ``MODEL_PLACEHOLDER_M<N>`` model id, so every generation is attributed to its real model.

    Returns ``(usage, marks)`` where ``usage`` is ``{cascadeId: [{stepKey, u, c, o,
    model_placeholder, api_provider, date}, ...]}`` (merged across every running language server —
    Desktop + IDE each own a disjoint set) and ``marks`` is ``{cascadeId: lastModifiedTime}`` for
    every cascade that was successfully queried this run (the caller persists these as watermarks so
    unchanged cascades are skipped on the next refresh — see ``rpcWatermarks`` in the ledger).

    **Incremental harvest contract:**
    - ``known``: cascadeId → previously persisted ``lastModifiedTime``.  When the listed
      ``lastModifiedTime`` matches the known value (exact equality, not ordering — equality makes a
      clock regression on LS restart a harmless re-query rather than a silent permanent skip), the
      metadata call is skipped and the mark is re-confirmed in ``marks``.  A missing or empty
      ``lastModifiedTime`` always triggers a query (can't tell if it changed).
    - Watermarks are set ONLY on a successful metadata response (status 200, dict body) — including
      zero-usage conversations (so they stop being re-queried).  An exception, non-200, or deadline
      skip leaves the cascade unmarked, so it will be retried on the next refresh rather than
      having its generations silently lost.
    - ``deadline``: wall-clock ``time.time()`` deadline for the whole harvest.  Checked before each
      LS, each port probe, and each per-cascade metadata call; on expiry the function returns what
      was harvested so far (partial result is fine — the persistent ledger + watermarks resume on
      the next refresh, providing a self-healing ratchet: already-queried cascades leave the work
      set regardless of iteration order).

    Best-effort: any failure (LS not running, no port, RPC error) is skipped, so an empty result
    simply means the caller falls back to the on-disk scan.  Only conversations currently loaded in
    LS memory are returned (not the full on-disk history) — the persistent ledger keeps them after
    they unload.

    ``learned_names``: optional output param (an existing dict this call MERGES into, not
    returned — this keeps the function's return shape 2-tuple-compatible with the many existing
    callers/mocks that unpack it as ``usage, marks``). When provided, populated with every
    ``MODEL_PLACEHOLDER_M<N>`` -> displayName pair this run's ``GetAvailableModels`` call(s)
    returned, across every language server queried — i.e. the exact picker names for every model
    CURRENTLY on offer, not just ones with usage this run. The caller persists these so a model's
    real name, once seen from a live language server, is remembered even after Antigravity closes
    and heals any disk-scan entry whose enum isn't in the static ``_MODEL_ENUM_NAMES`` table yet.
    """
    known = known or {}
    out: dict[str, list[dict[str, Any]]] = {}
    marks: dict[str, str] = {}
    for pid, token, scheme in find_antigravity_processes():
        if deadline is not None and time.time() >= deadline:
            break
        ports, _ = antigravity_ports(pid)
        if not ports:
            continue
        schemes = (scheme, "http" if scheme == "https" else "https")
        base = None
        summaries: Any = {}
        for port in ports:
            if deadline is not None and time.time() >= deadline:
                break
            for s in schemes:
                per_call = min(timeout, 3.0)
                if deadline is not None:
                    per_call = max(0.1, min(per_call, deadline - time.time()))
                try:
                    status, data = _post_local_rpc(
                        f"{s}://127.0.0.1:{port}{_LS_RPC_BASE}/GetAllCascadeTrajectories",
                        token, _LS_RPC_METADATA, per_call,
                    )
                except Exception:
                    continue
                if status == 200 and isinstance(data, dict):
                    base = f"{s}://127.0.0.1:{port}"
                    summaries = data.get("trajectorySummaries") or {}
                    break
            if base:
                break
        if not base or not isinstance(summaries, dict):
            continue
        # Exact picker display names, live from THIS language server: one cheap call per
        # LS per refresh mapping model id ("MODEL_PLACEHOLDER_M132" / "MODEL_OPENAI_…")
        # -> displayName ("Gemini 3.5 Flash (High)"). Records carry it as model_display
        # so the ledger stores the picker's own wording even for ids the static enum map
        # has never seen. Best-effort: on failure records fall back to the enum map.
        display_names: dict[str, str] = {}
        try:
            per_call = min(timeout, 3.0)
            if deadline is not None:
                per_call = max(0.1, min(per_call, deadline - time.time()))
            status, data = _post_local_rpc(
                f"{base}{_LS_RPC_BASE}/GetAvailableModels", token, _LS_RPC_METADATA, per_call,
            )
            if status == 200 and isinstance(data, dict):
                for m in ((data.get("response") or {}).get("models") or {}).values():
                    mid = str((m or {}).get("model") or "")
                    dn = str((m or {}).get("displayName") or "")
                    if mid and dn:
                        display_names[mid] = dn
        except Exception:
            display_names = {}
        if learned_names is not None:
            learned_names.update(display_names)
        for cascade_id, summary in summaries.items():
            if cascade_id in out:
                continue  # already harvested from another language server
            if deadline is not None and time.time() >= deadline:
                break
            lmt = str((summary.get("lastModifiedTime") if isinstance(summary, dict) else None) or "")
            # Incremental skip: if we know this cascade and its lastModifiedTime hasn't changed,
            # re-confirm the watermark without issuing a metadata call.
            if lmt and known.get(cascade_id) == lmt:
                marks[cascade_id] = lmt
                continue
            per_call = min(timeout, 3.0)
            if deadline is not None:
                per_call = max(0.1, min(per_call, deadline - time.time()))
            try:
                status, data = _post_local_rpc(
                    f"{base}{_LS_RPC_BASE}/GetCascadeTrajectoryGeneratorMetadata",
                    token, {**_LS_RPC_METADATA, "cascadeId": cascade_id}, per_call,
                )
            except Exception:
                continue  # no mark — will be retried next refresh
            if status != 200 or not isinstance(data, dict):
                continue  # no mark — will be retried next refresh
            records: list[dict[str, Any]] = []
            for gen_index, gen in enumerate(data.get("generatorMetadata") or []):
                if not isinstance(gen, dict):
                    continue  # skip a malformed RPC item instead of aborting the whole harvest
                try:
                    chat = gen.get("chatModel") or {}
                    usage = chat.get("usage") or {}
                    if not usage:
                        continue
                    step_indices = gen.get("stepIndices") or []
                    # Per-generation ledger key (`<cascadeId>#<stepKey>`) must be STABLE across
                    # polls and unique within the conversation. stepIndices[0] is both when present.
                    # When it's absent, fall back to the generation's position in the list —
                    # NOT len(records): that counted only usage-bearing siblings, so it shifted
                    # across polls (double-count) and could equal a real stepIndices[0] (collision
                    # → the gen silently dropped as "already seen"). Prefix "g" so the positional
                    # key can never collide with a real integer stepIndices[0] key.
                    step_key = str(int(step_indices[0])) if step_indices else f"g{gen_index}"
                    # createdAt lives at chatModel.chatStartMetadata.createdAt (NOT on `gen`
                    # directly — an earlier path read `gen.chatStartMetadata` and always got
                    # "", so every generation fell back to today). It's a UTC instant; localise.
                    created_raw = str((chat.get("chatStartMetadata") or {}).get("createdAt") or "")
                    created = _rpc_local_date(created_raw)
                    placeholder = usage.get("model") or ""
                    records.append({
                        "stepKey": step_key,
                        "u": _as_int(usage.get("inputTokens")),       # uncached input
                        "c": _as_int(usage.get("cacheReadTokens")),   # cached input (read)
                        "o": _as_int(usage.get("outputTokens")),      # total output (thinking + response)
                        "model_placeholder": placeholder,
                        "model_display": display_names.get(placeholder, ""),  # exact picker name (may be "")
                        "api_provider": usage.get("apiProvider") or "",
                        "date": created,
                        "hour": _rpc_local_hour(created_raw),         # local hour-of-day for the Day view
                    })
                except Exception:
                    # A malformed item (non-dict chatModel, non-numeric stepIndices[0], etc.)
                    # must not abort the whole harvest — skip just this generation and keep going.
                    continue
            if records:
                out[cascade_id] = records
            # Watermark only on success (including zero-usage conversations: marks them so they
            # stop being re-queried each refresh until they actually change).
            marks[cascade_id] = lmt
    return out, marks


def normalized_antigravity_lanes(limits: list[dict[str, Any]], include_missing: bool = False) -> list[dict[str, Any]]:
    by_label: dict[str, dict[str, Any]] = {}
    for limit in limits:
        label = str(limit.get("label") or "").strip().lower()
        for expected in ANTIGRAVITY_MODEL_LABELS:
            if label == expected.lower() and expected not in by_label:
                row = dict(limit)
                row["label"] = expected
                by_label[expected] = row
    rows: list[dict[str, Any]] = []
    for label in ANTIGRAVITY_MODEL_LABELS:
        if label in by_label:
            rows.append(by_label[label])
        elif include_missing:
            rows.append({"label": label, "percent": 0.0, "reset": "", "unit": "%"})
    return rows


def choose_antigravity_result(
    local_result: dict[str, Any],
    remote_result: dict[str, Any] | None,
) -> dict[str, Any]:
    # The cloud retrieveUserQuotaSummary (5h + weekly per group) is the ONLY source that matches
    # agy's /usage, so prefer it even when the local LS is up — local GetUserStatus only exposes a
    # single-window per-model view. Keep the LS-only enrichments (tier, creditBalance) by merging.
    if (isinstance(remote_result, dict) and remote_result.get("status") == "ok"
            and remote_result.get("limitsSource") == "quota-summary"):
        merged = dict(remote_result)
        if isinstance(local_result, dict) and local_result.get("status") == "ok":
            # GetUserStatus is the ONLY authoritative tier source (CLAUDE.md: "only Antigravity's
            # GetUserStatus has it") — remote loadCodeAssist returns the product name ("Antigravity"),
            # not the real "Google AI Ultra/Pro" tier — so the local tier wins whenever present.
            if local_result.get("tier"):
                merged["tier"] = local_result["tier"]
            if "creditBalance" not in merged and "creditBalance" in local_result:
                merged["creditBalance"] = local_result["creditBalance"]
        return merged
    if local_result.get("status") == "ok":
        return local_result
    if remote_result is None:
        return local_result
    if remote_result.get("status") == "ok":
        return remote_result

    messages: list[str] = []
    for result in (local_result, remote_result):
        message = str(result.get("message") or "").strip()
        if message and message not in messages:
            messages.append(message)

    preferred = remote_result
    if remote_result.get("status") == "missing-oauth" and local_result.get("status") != "not-running":
        preferred = local_result

    result = dict(preferred)
    result["label"] = "Antigravity"
    result["source"] = "antigravity-only"
    result["limits"] = []
    if messages:
        result["message"] = "; ".join(messages)
    return result



# ---------------------------------------------------------------------------
# Antigravity OAuth
# ---------------------------------------------------------------------------

def antigravity_oauth_credentials_path() -> Path:
    return Path.home() / ".tallybar" / "antigravity" / "oauth_creds.json"


def antigravity_cli_oauth_credentials_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def normalize_antigravity_credentials(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if isinstance(token, dict):
        credentials = dict(token)
        if data.get("auth_method"):
            credentials.setdefault("auth_method", data.get("auth_method"))
        return credentials
    return dict(data)


def load_antigravity_oauth_credentials() -> list[tuple[dict[str, Any], Path, dict[str, Any], str]]:
    credentials: list[tuple[dict[str, Any], Path, dict[str, Any], str]] = []
    for source, path in (
        ("antigravity-cli-oauth", antigravity_cli_oauth_credentials_path()),
        ("tallybar-oauth", antigravity_oauth_credentials_path()),
    ):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        normalized = normalize_antigravity_credentials(data)
        if normalized is not None:
            credentials.append((normalized, path, data if isinstance(data, dict) else {}, source))
    return credentials


def antigravity_token_expiry_seconds(credentials: dict[str, Any]) -> float | None:
    value = credentials.get("expiry_date") or credentials.get("expiryDate") or credentials.get("expiry")
    if value is None:
        return None
    try:
        seconds = float(value)
    except Exception:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        match = re.match(r"^(.*\.\d{6})\d+([+-]\d\d:\d\d)$", text)
        if match:
            text = match.group(1) + match.group(2)
        try:
            parsed = dt.datetime.fromisoformat(text)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    if seconds > 10_000_000_000:
        seconds /= 1000.0
    return seconds


def save_antigravity_credentials(path: Path, raw_data: dict[str, Any], credentials: dict[str, Any]) -> None:
    if isinstance(raw_data.get("token"), dict):
        token = dict(raw_data["token"])
        token["access_token"] = credentials.get("access_token") or credentials.get("accessToken") or token.get("access_token", "")
        token["token_type"] = credentials.get("token_type") or credentials.get("tokenType") or token.get("token_type", "Bearer")
        if credentials.get("refresh_token") or credentials.get("refreshToken"):
            token["refresh_token"] = credentials.get("refresh_token") or credentials.get("refreshToken")
        if credentials.get("expiry"):
            token["expiry"] = credentials["expiry"]
        raw_data["token"] = token
        data = raw_data
    else:
        data = credentials
    # Atomic 0600 write via the shared io_helpers recipe: unique 0600 mkstemp (no
    # world/group-readable window for the OAuth credentials — SEC-6), fsync + dir-fsync so a
    # torn write can't 0-byte the token file (SEC-2), parent chmod'd 0700 (dir listing would
    # leak "this user authed Antigravity, last refreshed at T"), temp unlinked on failure.
    payload = json.dumps(data, indent=2, sort_keys=True)
    # Resolve through any symlink before the atomic os.replace: ~/.tallybar/antigravity/
    # oauth_creds.json is a symlink -> ~/.gemini/oauth_creds.json on live machines, and
    # os.replace onto the link path would swap in a regular file, breaking the link and
    # forking the credential (Gemini CLI would keep reading the stale token). realpath is
    # non-strict, so a nonexistent path is returned unchanged (normal first-write case).
    atomic_write_text(Path(os.path.realpath(path)), payload)


def antigravity_oauth_pairs_from_data(data: bytes) -> list[tuple[str, str]]:
    ids: list[str] = []
    secrets: list[str] = []
    for match in re.finditer(rb"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", data):
        value = match.group(0).decode("ascii")
        if value not in ids:
            ids.append(value)
    for match in re.finditer(rb"GOCSPX-[A-Za-z0-9_-]{28}", data):
        value = match.group(0).decode("ascii")
        if value not in secrets:
            secrets.append(value)
    if not ids or not secrets:
        return []

    preferred: list[tuple[str, str]] = []
    if len(secrets) == 1 and len(ids) > 1:
        preferred.append((ids[-1], secrets[0]))
    elif len(secrets) == len(ids) and len(secrets) > 1:
        preferred.append((ids[0], secrets[-1]))
    else:
        preferred.append((ids[0], secrets[0]))

    for client_id in ids:
        for client_secret in secrets:
            pair = (client_id, client_secret)
            if pair not in preferred:
                preferred.append(pair)
    return preferred


def antigravity_oauth_clients(credentials: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []

    def add_pair(client_id: Any, client_secret: Any) -> None:
        client_id = str(client_id or "").strip()
        client_secret = str(client_secret or "").strip()
        if client_id and client_secret and (client_id, client_secret) not in pairs:
            pairs.append((client_id, client_secret))

    add_pair(os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID"), os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET"))
    add_pair(
        credentials.get("client_id") or credentials.get("clientID"),
        credentials.get("client_secret") or credentials.get("clientSecret"),
    )

    home = Path.home()
    candidates = [
        home / ".local/share/antigravity-ide/resources/app/extensions/antigravity/bin/language_server_linux_x64",
        home / ".local/share/antigravity-ide/resources/app/out/main.js",
    ]
    for pattern in (
        str(home / ".local/opt/Antigravity*/resources/app/extensions/antigravity/bin/language_server_linux_x64"),
        str(home / ".local/opt/Antigravity*/resources/app/out/main.js"),
        "/opt/Antigravity*/resources/app/extensions/antigravity/bin/language_server_linux_x64",
        "/opt/Antigravity*/resources/app/out/main.js",
    ):
        candidates.extend(Path(path) for path in glob.glob(pattern))

    for path in candidates:
        try:
            with open(path, "rb") as f:
                chunk_size = ANTIGRAVITY_BINARY_SCAN_CHUNK_BYTES
                overlap = ANTIGRAVITY_BINARY_SCAN_OVERLAP_BYTES
                data = f.read(chunk_size)
                while data:
                    for pair in antigravity_oauth_pairs_from_data(data):
                        if pair not in pairs:
                            pairs.append(pair)
                    next_chunk = f.read(chunk_size - overlap)
                    if not next_chunk:
                        break
                    data = data[-overlap:] + next_chunk
        except Exception:
            continue
    return pairs


def refresh_antigravity_credentials(
    credentials: dict[str, Any],
    path: Path,
    raw_data: dict[str, Any],
    timeout: float,
) -> str | None:
    refresh_token = str(credentials.get("refresh_token") or credentials.get("refreshToken") or "").strip()
    if not refresh_token:
        return None

    for client_id, client_secret in antigravity_oauth_clients(credentials):
        body = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=min(timeout, 4.0)) as response:
                data = json.loads(response.read(100_000).decode("utf-8", errors="replace"))
        except Exception:
            continue
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            continue
        credentials["access_token"] = access_token
        credentials["token_type"] = data.get("token_type") or credentials.get("token_type") or "Bearer"
        if data.get("id_token"):
            credentials["id_token"] = data["id_token"]
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=float(expires_in))
            if isinstance(raw_data.get("token"), dict):
                credentials["expiry"] = expiry.isoformat()
            else:
                credentials["expiry_date"] = int(expiry.timestamp() * 1000)
        credentials.setdefault("client_id", client_id)
        credentials.setdefault("client_secret", client_secret)
        # Persist the refreshed token, but a save failure must not lose the freshly
        # obtained access_token (still valid in-memory for this run; it re-refreshes
        # next run). Surface the failure to stderr (scrubbed) instead of swallowing it
        # silently, so a persistent persistence problem is at least visible in the logs.
        try:
            save_antigravity_credentials(path, raw_data, credentials)
        except Exception as exc:
            print(f"antigravity: failed to persist refreshed credentials: {scrub_credentials(str(exc))[:160]}",
                  file=sys.stderr)
        return access_token
    return None


def antigravity_cloudcode_host() -> str:
    """Return the Cloud Code host agy actually uses (prod vs daily/dev channel).

    The host is baked into the agy build and lives in no config file, so we detect it from agy's
    own request logs — prod and daily return DIFFERENT quota numbers, so hitting the wrong one
    shows the wrong %used (e.g. daily reports the Gemini group as 9%/19% used while prod reports
    0%). Reads the TAIL of the newest log (most recent host wins) and falls back to prod when no
    log is present. Cheap: one bounded tail read, no full-file scan."""
    default = "cloudcode-pa.googleapis.com"
    try:
        logs = sorted(ANTIGRAVITY_CLOUDCODE_LOG_DIR.glob("*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return default
    for log in logs[:3]:
        try:
            with open(log, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 262_144))  # last 256 KiB is plenty; logs record every request
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        hosts = _ANTIGRAVITY_CLOUDCODE_HOST_RE.findall(text)
        if hosts:
            return hosts[-1]  # most recent occurrence
    return default


def antigravity_bearer_json(path: str, access_token: str, body: dict[str, Any], timeout: float,
                            base_url: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        (base_url or ANTIGRAVITY_REMOTE_BASE_URL) + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity",
            "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as err:
        try:
            data = json.loads(err.read(2_000_000).decode("utf-8", errors="replace"))
        except Exception:
            data = {"error": {"message": f"HTTP {err.code}"}}
        return int(err.code), data


def antigravity_project_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    project = data.get("cloudaicompanionProject")
    if isinstance(project, str) and project.strip():
        return project.strip()
    if isinstance(project, dict):
        value = project.get("id") or project.get("projectId")
        if value:
            return str(value).strip()
    return None


def run_antigravity_remote(timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": "Antigravity",
        "status": "missing-oauth",
        "source": "antigravity-oauth",
        "message": "Antigravity OAuth credentials not found",
        "limits": [],
    }
    credential_sources = load_antigravity_oauth_credentials()
    if not credential_sources:
        return result

    failures: list[str] = []
    partial_result: dict[str, Any] | None = None  # degraded-but-authenticated; returned only if no source is full-ok
    for credentials, path, raw_data, source_name in credential_sources:
        # Guard each source: antigravity_bearer_json only catches HTTPError, so a
        # network timeout/URLError would otherwise abort the whole loop and skip
        # the remaining credential sources. Record it and try the next source.
        try:
            attempt = run_antigravity_remote_with_credentials(credentials, path, raw_data, source_name, timeout)
        except Exception as exc:
            msg = f"{source_name}: {scrub_credentials(str(exc))[:120]}"
            if msg not in failures:
                failures.append(msg)
            continue
        if attempt.get("status") == "ok":
            return attempt
        if attempt.get("status") == "partial" and partial_result is None:
            # Remember it, but keep trying the other credential sources for a full reading first.
            partial_result = attempt
        message = str(attempt.get("message") or attempt.get("status") or "").strip()
        if message and message not in failures:
            failures.append(message)

    if partial_result is not None:
        return partial_result
    result.update(status="oauth-unavailable", message="; ".join(failures) if failures else "Antigravity OAuth unavailable")
    return result


def run_antigravity_remote_with_credentials(
    credentials: dict[str, Any],
    path: Path,
    raw_data: dict[str, Any],
    source_name: str,
    timeout: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": "Antigravity",
        "status": "missing-oauth",
        "source": source_name,
        "message": "Antigravity OAuth credentials not found",
        "limits": [],
    }
    access_token = str(credentials.get("access_token") or credentials.get("accessToken") or "").strip()
    if not access_token:
        result.update(status="missing-oauth", message="Antigravity OAuth access token missing")
        return result

    expiry = antigravity_token_expiry_seconds(credentials)
    if expiry is not None and expiry <= time.time() + 60:
        refreshed = refresh_antigravity_credentials(credentials, path, raw_data, timeout)
        if not refreshed:
            result.update(status="oauth-expired", message=f"{source_name} token expired and could not refresh")
            return result
        access_token = refreshed

    metadata = {
        "metadata": {
            "ideType": "ANTIGRAVITY",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    }
    # Match agy's Cloud Code host (prod vs daily) for EVERY call — the token is the agy CLI token and
    # the quota numbers are host-specific, so a mismatched host yields wrong percentages.
    base_url = f"https://{antigravity_cloudcode_host()}"
    status, code_assist = antigravity_bearer_json("/v1internal:loadCodeAssist", access_token, metadata, timeout, base_url=base_url)
    if status == 401:
        refreshed = refresh_antigravity_credentials(credentials, path, raw_data, timeout)
        if refreshed:
            access_token = refreshed
            status, code_assist = antigravity_bearer_json("/v1internal:loadCodeAssist", access_token, metadata, timeout, base_url=base_url)
    if status == 401:
        result.update(status="unauthorized", message=f"{source_name} session was rejected")
        return result
    if status not in (200, 403):
        result.update(status="api-error", message=f"Antigravity remote usage returned HTTP {status}")
        return result

    tier = ""
    if isinstance(code_assist, dict):
        plan_info = code_assist.get("planInfo") or {}
        tier_info = code_assist.get("currentTier") or {}
        if isinstance(plan_info, dict):
            tier = str(plan_info.get("planType") or "").strip()
        if not tier and isinstance(tier_info, dict):
            tier = str(tier_info.get("name") or "").strip()

    # PRIMARY: retrieveUserQuotaSummary (empty body) — the exact per-group, 5h + weekly view agy's
    # /usage shows. Four lanes (Gemini + Claude·GPT, each 5-hour and weekly). Tagged with
    # ``limitsSource: "quota-summary"`` so choose_antigravity_result prefers it over the local LS's
    # single-window GetUserStatus lanes (the cloud summary is the only source of the windowed view).
    summary_status, summary = antigravity_bearer_json(
        "/v1internal:retrieveUserQuotaSummary", access_token, {}, timeout, base_url=base_url)
    summary_lanes = parse_antigravity_quota_summary(summary) if summary_status == 200 else []
    if summary_lanes:
        result.update(
            status="ok",
            source=source_name,
            message="Read Antigravity quota summary (5h + weekly per group)",
            limits=summary_lanes,
            limitsSource="quota-summary",
        )
        if tier:
            result["tier"] = tier
        return result

    # FALLBACK (summary unavailable): the legacy fetchAvailableModels / retrieveUserQuota path.
    project_id = antigravity_project_id(code_assist)
    model_body = {"project": project_id} if project_id else {}
    status, models = antigravity_bearer_json("/v1internal:fetchAvailableModels", access_token, model_body, timeout, base_url=base_url)
    limits = parse_antigravity_limits(models) if status == 200 else []
    # fetchAvailableModels is the ONLY legacy source that returns the full model set (Gemini +
    # Claude/GPT) on the real session reset window. When it fails (this account commonly gets 403
    # PERMISSION_DENIED), retrieveUserQuota answers 200 but with a PARTIAL, Gemini-only view on a
    # different (daily) window. That's enough to prove we're authenticated, but NOT a faithful
    # session-lane reading — presenting it would fabricate "0% used" for the Claude/GPT lane it
    # omits and show the wrong reset window. So we flag it ``partial``: the caller surfaces
    # last-known lanes instead of a confident wrong number.
    partial = False
    if not limits:
        quota_status, quota = antigravity_bearer_json("/v1internal:retrieveUserQuota", access_token, model_body, timeout, base_url=base_url)
        if quota_status == 200:
            limits = parse_antigravity_limits(quota)
            if limits:
                partial = True
    if not limits:
        result.update(status="api-empty", message="Antigravity remote usage returned no model quotas")
        return result

    if partial:
        # Degraded-but-authenticated read: DON'T emit lanes here (no fabricated 0% for the models
        # the partial daily-quota response omits). build_snapshot detects this ``partial`` status
        # and grafts the last-known lanes from the cached snapshot (tagged ``stale``) so the widget
        # freezes the previous reading instead of a confident wrong number. (The snapshot cache
        # alone does NOT freeze them — the other live providers cause it to be overwritten — which
        # is exactly why the carry-forward lives in build_snapshot.)
        result.update(
            status="partial",
            source=source_name,
            message="Antigravity full model quota unavailable (partial daily-quota view) — showing last-known session limits",
            limits=[],
        )
    else:
        result.update(
            status="ok",
            source=source_name,
            message="Read Antigravity Cloud Code model usage",
            limits=normalized_antigravity_lanes(limits, include_missing=True),
        )
    if tier:
        result["tier"] = tier
    return result






def run_antigravity_local(timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": "Antigravity",
        "status": "not-running",
        "source": "local-antigravity",
        "message": "Antigravity language server not found",
        "limits": [],
    }
    proc = find_antigravity_process()
    if not proc:
        return result
    pid, token, scheme = proc
    ports, lsof_error = antigravity_ports(pid)
    if not ports:
        msg = "Antigravity process found, no listening API port"
        if lsof_error:
            msg += f" ({lsof_error})"
        result.update(status="no-port", message=msg)
        return result
    path = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
    schemes = (scheme, "http" if scheme == "https" else "https")
    for port in ports:
        for s in schemes:
            try:
                status, data = post_local_json(f"{s}://127.0.0.1:{port}{path}", token, min(timeout, 3.0))
                if status != 200:
                    continue
                limits = parse_antigravity_limits(data)
                lanes = normalized_antigravity_lanes(limits, include_missing=True)

                credit_balance, credit_limit = antigravity_credit_state(data)
                if credit_limit is not None:
                    lanes.append(credit_limit)
                provider: dict[str, Any] = {
                    "label": "Antigravity",
                    "status": "ok" if limits else "api-empty",
                    "source": f"local-antigravity:{port}",
                    "message": "Read Antigravity local language-server status",
                    "limits": lanes,
                }
                tier = antigravity_tier(data)
                if tier:
                    provider["tier"] = tier
                if credit_balance is not None:
                    provider["creditBalance"] = credit_balance
                return provider
            except Exception:
                continue
    result.update(status="api-error", message="Antigravity local API did not return status")
    return result


def apply_google_one_credits(antigravity: dict[str, Any], google_one: dict[str, Any] | None) -> None:
    """Surface the real Google One AI credit balance in the Antigravity section.

    The Antigravity language server reports Code Assist "Pro" prompt/flow credits
    (e.g. 500/100 against a 50k/150k monthly pool). For a Google AI Ultra/Pro
    subscriber those are the wrong metric — they render as a misleading ~100%-used
    "Credits" bar — so the real AI credit pool from one.google.com/ai/activity
    replaces them: the plan-status credit bar is dropped, and the credit balance is
    set to the Google One figure (or cleared if it could not be fetched).
    """
    if not isinstance(antigravity, dict):
        return
    # Drop the misleading plan-status monthly credit-pool bar regardless of the
    # Google One result — the numbers behind it are the wrong metric.
    lanes = antigravity.get("limits")
    if isinstance(lanes, list):
        antigravity["limits"] = [
            lane
            for lane in lanes
            if not (isinstance(lane, dict) and lane.get("reset") == "Monthly credit pool")
        ]
    balance = google_one.get("creditBalance") if isinstance(google_one, dict) else None
    if isinstance(balance, dict):
        antigravity["creditBalance"] = balance
        return
    # No Google One balance available: don't keep showing the wrong prompt/flow figure.
    existing = antigravity.get("creditBalance")
    if isinstance(existing, dict) and existing.get("source") == "antigravity-plan-status":
        antigravity.pop("creditBalance", None)



