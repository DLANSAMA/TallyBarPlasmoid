"""Shared low-level filesystem durability helpers.

Imported flat (``from io_helpers import fsync_dir``) by backend.py, pricing_data.py
and providers/cost.py — they all live under contents/code/ which is on sys.path both
at runtime (Plasma runs the script from that dir) and in tests. Stdlib only.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path

# Orphaned atomic-write temps take two shapes: a bare ``mkstemp`` leftover (``tmp`` + 8
# random chars, from a crashed default-prefix mkstemp) and the current atomic_write_text
# recipe's ``<name>.<random>.tmp`` (prefix=name+'.', suffix='.tmp'). Match ONLY these — never
# .bak/.corrupt/.json or any other file.
_MKSTEMP_ORPHAN_RE = re.compile(r"tmp\w{8}")


def sweep_stale_temp_files(dirs, max_age_seconds: int = 86400, only_prefix: str | None = None) -> int:
    """Best-effort deletion of orphaned atomic-write temp files across ``dirs``.

    Removes only REGULAR files (via os.lstat — symlinks and dirs are skipped) whose name is
    either a bare ``mkstemp`` orphan (``tmp`` + 8 word chars, full match) OR ends with
    ``.tmp`` (the atomic_write_text recipe), and whose mtime is strictly older than
    ``max_age_seconds``. Never recurses, never raises (per-file and per-dir guards), and
    touches nothing else (.bak/.corrupt/.json etc. are left alone). Returns the count removed.

    ``only_prefix`` NARROWS eligibility for third-party dirs the widget only lightly touches
    (e.g. ~/.gemini): when set, a file is eligible ONLY if its name starts with ``only_prefix``
    AND ends with ``.tmp`` — the generic ``tmp`` + 8-word-char rule does NOT apply, so unrelated
    tmpXXXXXXXX / *.tmp files another tool left there are spared. All other gates (lstat
    regular-file, strict 24h age, non-recursive, never-raise) are unchanged.

    A crashed atomic write (interpreter kill between mkstemp and os.replace) leaves these temps
    behind — over months they accumulate (observed: six July tmp* files plus a 101 MB
    ``claude_logs.json.<rand>.tmp``). Called early in the --once refresh to keep ~/.tallybar tidy."""
    removed = 0
    cutoff = time.time() - max_age_seconds
    for d in dirs:
        try:
            entries = os.scandir(str(d))
        except OSError:
            continue  # nonexistent / unreadable dir is harmless
        try:
            for entry in entries:
                try:
                    name = entry.name
                    if only_prefix is not None:
                        if not (name.startswith(only_prefix) and name.endswith(".tmp")):
                            continue
                    elif not (_MKSTEMP_ORPHAN_RE.fullmatch(name) or name.endswith(".tmp")):
                        continue
                    st = os.lstat(entry.path)  # do NOT follow symlinks
                    if not stat.S_ISREG(st.st_mode):
                        continue  # skip symlinks, dirs, sockets, …
                    if st.st_mtime >= cutoff:
                        continue  # too fresh — may be an in-flight write
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    continue
        finally:
            entries.close()
    return removed


def flock_with_timeout(lock_fd: int, timeout: float, poll: float = 0.05) -> bool:
    """Acquire an exclusive flock on ``lock_fd`` NON-blocking, retrying until ``timeout``
    seconds elapse. Returns True if the lock was taken, False on contention timeout.

    A plain blocking ``fcntl.flock(fd, LOCK_EX)`` is unsafe when the caller may be
    cancelled (e.g. inside an asyncio.to_thread worker under an outer wait_for): the
    cancellation can't unblock a thread parked in the kernel on the lock, hanging the
    process. Callers bound the wait and degrade gracefully on False instead. Always
    makes at least one attempt even when ``timeout <= 0``."""
    end = time.time() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= end:
                return False
            time.sleep(poll)


def fsync_dir(path: Path | str) -> None:
    """Best-effort fsync of a directory so a just-completed os.replace() rename is
    durable across a crash (on POSIX the dir entry isn't durable until the dir is
    fsynced). Silently ignores platforms/filesystems that don't support it."""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def atomic_write_text(path: Path | str, text: str, mode: int = 0o600) -> None:
    """Atomically write ``text`` to ``path`` at ``mode`` perms — the project's standard
    ~/.tallybar write recipe: a unique 0600 temp file (``mkstemp``, so no shared-.tmp
    corruption race) in the same dir, fsync'd, then ``os.replace``'d over the target, with
    the parent dir fsync'd so the rename is crash-durable on POSIX. The parent dir is created
    0700. Raises on an unrecoverable error (after cleaning up the temp file); callers that
    treat the write as best-effort should catch OSError."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock down the immediate parent — and the ~/.tallybar root if we write into a subdir of it
    # (e.g. ~/.tallybar/cache/…): mkdir(parents=True) creates intermediates at umask, so the root
    # could be left group/world-traversable if this helper is the first writer to create it.
    # chmod(0700) matches every other ~/.tallybar writer.
    dirs_to_lock = {path.parent}
    tallybar_root = Path.home() / ".tallybar"
    if tallybar_root == path.parent or tallybar_root in path.parents:
        dirs_to_lock.add(tallybar_root)
    for d in dirs_to_lock:
        try:
            d.chmod(0o700)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def to_daemon_thread(func, *args):
    """Run blocking ``func(*args)`` in a DAEMON thread; return an awaitable Future.

    A drop-in for ``asyncio.to_thread`` with one load-bearing difference: the worker thread
    is a DAEMON, so if the awaiter is cancelled — e.g. an outer ``asyncio.wait_for`` times
    out — the orphaned thread can NOT gate interpreter exit. ``asyncio.to_thread`` runs on the
    loop's default ThreadPoolExecutor, whose threads are non-daemon and are JOINED at exit; a
    provider stuck on slow I/O (notably DNS resolution, which urllib's timeout does NOT bound)
    therefore stalls the one-shot backend's exit — and pytest's — until the OS resolver gives
    up. With a daemon worker the process exits promptly and the stuck thread is abandoned.

    Result and exception propagate to the awaiter exactly like ``asyncio.to_thread``. If the
    awaiter has already gone (cancelled, or the loop closed), the late result is dropped.
    Same family as the non-blocking-flock guard in ``flock_with_timeout`` (CLAUDE.md): never
    let a thread blocked in the kernel gate the interpreter.
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _deliver(result, exc):
        # Runs on the loop thread. The future may already be cancelled/resolved (the awaiter
        # timed out) — in which case the late result/exception is simply discarded.
        if fut.cancelled() or fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def _runner():
        try:
            result, exc = func(*args), None
        except BaseException as err:  # propagate ANY failure back, like asyncio.to_thread
            result, exc = None, err
        try:
            loop.call_soon_threadsafe(_deliver, result, exc)
        except RuntimeError:
            pass  # loop already closed — the awaiter is gone; drop the late result

    threading.Thread(target=_runner, name="tallybar-worker", daemon=True).start()
    return fut
