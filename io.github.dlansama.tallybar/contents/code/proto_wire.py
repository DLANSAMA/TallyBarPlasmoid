"""Protobuf wire-format parser for Antigravity trajectory and generation blobs.

Decodes raw LEB128 varints and length-delimited records directly from bytes
without external dependencies (pure stdlib). Hardened against truncated buffers,
infinite varints, and schema drift.
"""
from __future__ import annotations

from typing import Any

# Field numbers of an Antigravity usage record.
USAGE_MODEL = 1       # model enum
USAGE_INPUT = 2       # uncached input tokens
USAGE_OUTPUT = 3      # output tokens (thoughts + response)
USAGE_CACHED = 5      # cached input tokens
USAGE_MARKER = 6      # 24 = steps.metadata layout, 26 = gen_metadata layout
USAGE_THOUGHTS = 9    # thinking tokens
USAGE_RESPONSE = 10   # response tokens (output = thoughts + response)


def _pb_read_varint(buf: bytes, i: int) -> tuple[int, int]:
    """Read an unsigned LEB128 varint from buf[i:].

    Raises IndexError on truncated buffer, ValueError on varint exceeding 64 bits.
    """
    shift = result = 0
    n = len(buf)
    while True:
        if i >= n:
            raise IndexError("Unexpected EOF while reading varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, i
        shift += 7
        if shift > 64:
            raise ValueError("Malformed varint: exceeds 64 bits")


def _dominant_enum(found: list[dict[int, int]]) -> int:
    """The model enum (field 1) of the record carrying the most tokens in ``found``.

    A blob is one model in practice; a mixed blob's whole summed entry is attributed to
    its dominant model. ONE entry per steps idx (one per generation for gen_metadata)
    preserves every key invariant (the ``:``/``@``/``#`` namespaces, RPC-purge prefixes,
    memo/prune).
    """
    if not found:
        return 0
    best = max(found, key=lambda vd: sum(vd.get(f, 0) for f in (USAGE_INPUT, USAGE_CACHED, USAGE_OUTPUT)))
    return best.get(USAGE_MODEL, 0)


def _pb_fields(buf: bytes) -> tuple[dict[int, int], list[tuple[int, bytes]]]:
    """Decode ONE protobuf message level -> (varints, subs).

    ``varints`` maps field number -> value (wire types 0/1/5 collapsed to a number) and ``subs`` is
    the ordered list of ``(field_number, bytes)`` length-delimited submessages. Fails soft: a
    malformed tail just stops the walk (matching ``_pb_find_usage``'s tolerance).
    """
    varints: dict[int, int] = {}
    subs: list[tuple[int, bytes]] = []
    i, n = 0, len(buf)
    while i < n:
        try:
            tag, i = _pb_read_varint(buf, i)
            fn, wt = tag >> 3, tag & 7
            if wt == 0:
                v, i = _pb_read_varint(buf, i)
                varints[fn] = v
            elif wt == 2:
                ln, i = _pb_read_varint(buf, i)
                if ln < 0 or i + ln > n:
                    break
                subs.append((fn, buf[i:i + ln]))
                i += ln
            elif wt == 5:
                if i + 4 > n:
                    break
                i += 4
            elif wt == 1:
                if i + 8 > n:
                    break
                i += 8
            else:
                break
        except (IndexError, ValueError):
            break
    return varints, subs


def _pb_find_usage(
    buf: bytes,
    depth: int = 0,
    out: list[dict[int, int]] | None = None,
    marker: int | None = None,
) -> list[dict[int, int]]:
    """Collect protobuf sub-messages identifiable as Antigravity usage records.

    A usage record is gated by ``field6 in {24, 26}`` (24 = pre-2.0 ``steps.metadata``
    layout; 26 = Antigravity-2.0 ``gen_metadata`` layout) AND field 2 present (input tokens).
    Field 1 is the *model* enum (e.g. 1016/1036=Gemini Pro, 1020/1133=Gemini Flash), so we
    must NOT gate on a single value or non-Pro generations (Flash/Claude/etc.) are dropped.

    ``marker``: when provided, gate on that exact value (e.g. ``marker=26`` for the fallback
    gen_metadata path). When None (the default), accept both 24 and 26.
    """
    if out is None:
        out = []
    if depth > 8:
        return out
    _valid_markers = frozenset((24, 26)) if marker is None else frozenset((marker,))
    varints: dict[int, int] = {}
    subs: list[bytes] = []
    i, n = 0, len(buf)
    while i < n:
        try:
            tag, i = _pb_read_varint(buf, i)
            fn, wt = tag >> 3, tag & 7
            if wt == 0:
                v, i = _pb_read_varint(buf, i)
                varints[fn] = v
            elif wt == 2:
                ln, i = _pb_read_varint(buf, i)
                if ln < 0 or i + ln > n:
                    break
                chunk = buf[i:i + ln]
                i += ln
                if len(chunk) >= 2:
                    subs.append(chunk)
            elif wt == 5:
                if i + 4 > n:
                    break
                i += 4
            elif wt == 1:
                if i + 8 > n:
                    break
                i += 8
            else:
                break
        except (IndexError, ValueError):
            break
    if (varints.get(USAGE_MARKER) in _valid_markers
            and USAGE_MODEL in varints and USAGE_INPUT in varints):
        out.append(varints)
    for chunk in subs:
        _pb_find_usage(chunk, depth + 1, out, marker)
    return out


def _dedupe_usage_records(found: list[dict[int, int]]) -> list[dict[int, int]]:
    """Drop exact duplicate usage records, keeping first-seen order.

    A gen_metadata generation stores its usage record TWICE — at ``field4`` and again at
    ``field17.2`` — so the recursive ``_pb_find_usage`` returns each one twice (measured on
    real undated CLI blobs: 28 records, 14 unique). Distinct generations with identical
    token counts are vanishingly unlikely within one blob; double-counting every one is not."""
    seen: set[tuple[tuple[int, int], ...]] = set()
    out: list[dict[int, int]] = []
    for rec in found:
        key = tuple(sorted(rec.items()))
        if key not in seen:
            seen.add(key)
            out.append(rec)
    return out


def _pb_generations(buf: bytes, require_timestamp: bool = True) -> list[dict[str, Any]]:
    """Extract per-generation usage from a ``gen_metadata.data`` blob.

    Each row is a root wrapping its generation(s) under field 1; each generation carries:
      - a usage record at ``generation.field4`` — gated by ``field6 in {24, 26}`` (24 = pre-2.0,
        26 = the Antigravity-2.0 marker flip; accept both or 2.0 generations are dropped), with a
        model enum (field 1) and uncached-input (field 2); tokens are 2=uncached-input, 3=output,
        5=cached-input. The SAME record is duplicated at ``generation.field17.2`` — read field 4
        ONLY, never both, or the totals double.
      - a ``google.protobuf.Timestamp`` at ``generation.field9.field4``, seconds in its field 1.

    Returns ``[{u, c, o, me, mk, secs}]`` — one entry per usage-bearing generation (``mk`` is the
    record's field-6 marker, 24 or 26). Dating by this EMBEDDED timestamp (not the DB file mtime)
    is what makes the per-day buckets correct: re-scanning a file never re-stamps old generations
    as "today", and a late-flushed generation lands on its real day. A generation missing the
    usage record is skipped; one missing the timestamp is skipped too unless
    ``require_timestamp=False``, which returns it with ``secs: None`` (the caller dates it) —
    still reading field 4 only, so it never picks up the field-17.2 duplicate.
    """
    out: list[dict[str, Any]] = []
    _, root_subs = _pb_fields(buf)
    gen_frames = [sub for fn, sub in root_subs if fn == 1]
    if not gen_frames:
        gen_frames = [buf]
    for gen in gen_frames:
        _, gsubs = _pb_fields(gen)
        usage: dict[int, int] | None = None
        secs: int | None = None
        for fn, sub in gsubs:
            if fn == 4 and usage is None:
                uva, _ = _pb_fields(sub)
                if uva.get(USAGE_MARKER) in (24, 26) and USAGE_MODEL in uva and USAGE_INPUT in uva:
                    usage = uva
            elif fn == 9 and secs is None:
                _, s9 = _pb_fields(sub)
                for tfn, tsub in s9:
                    if tfn == 4:
                        tva, _ = _pb_fields(tsub)
                        if 1 in tva:
                            secs = tva[1]
                            break
        if usage is not None and (secs is not None or not require_timestamp):
            out.append({
                "u": usage.get(USAGE_INPUT, 0),
                "c": usage.get(USAGE_CACHED, 0),
                "o": usage.get(USAGE_OUTPUT, 0),
                "me": usage.get(USAGE_MODEL, 0),
                "mk": usage.get(USAGE_MARKER, 0),
                "secs": secs,
            })
    return out
