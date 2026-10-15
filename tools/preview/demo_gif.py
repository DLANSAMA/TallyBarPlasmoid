#!/usr/bin/env python3
"""Builds docs/screenshots/cost-demo.gif for `make screenshots` (see tools/preview/README.md).

Stdlib + ffmpeg only.

  demo_gif.py cursors <dir> <nominal-size>   Breeze arrow + hand cursors -> PNGs + cursors.json
  demo_gif.py assemble <frames-dir> <out.gif> <width>

screenshot.qml's `demo=` mode writes one PNG per frame named f-<n>-<ms>.png, where <ms> is
when the frame was grabbed. The pointer walk and the widget's own timers run on the real
clock, so frames land at uneven intervals: each one is shown for exactly the time until the
next, so the GIF plays at real speed. The cost popout changes height between tabs, so every
frame is padded (top-left anchored) to the tallest, in the canvas colour screenshot.qml paints
behind the card. Frames are opaque on purpose: ffmpeg's GIF encoder stores only the changed
pixels of opaque frames, but every frame whole once there is transparency (19 MB, not 0.7 MB).
"""

import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

CURSOR_THEME = Path("/usr/share/icons/breeze_cursors/cursors")
CURSORS = {"arrow": "default", "hand": "pointing_hand"}
XCURSOR_IMAGE = 0xFFFD0002
FRAME_NAME = re.compile(r"^f-(\d+)-(\d+)\.png$")
END_HOLD_SECONDS = 0.8
CANVAS = "0x141416"  # screenshot.qml demoCanvas
# Read each frame in a millisecond time base. The image demuxer's default 25 fps time base
# rounds every duration to 40 ms steps, so ~50 ms frames played as 40, 40, 40, 40, 80: a
# stutter in every move. In 1 ms steps the GIF's 10 ms delays keep the real cadence.
MS_TIME_BASE = "option framerate 1000"


def _xcursor_image(path: Path, nominal: int):
    """(width, height, xhot, yhot, premultiplied ARGB words) of the image closest to `nominal`."""
    data = path.read_bytes()
    magic, _header, _version, ntoc = struct.unpack_from("<4sIII", data, 0)
    if magic != b"Xcur":
        raise SystemExit(f"{path} is not an Xcursor file")
    images = []
    for i in range(ntoc):
        kind, size, pos = struct.unpack_from("<III", data, 16 + 12 * i)
        if kind == XCURSOR_IMAGE:
            images.append((abs(size - nominal), pos))
    if not images:
        raise SystemExit(f"{path} holds no cursor image")
    pos = min(images)[1]
    _hdr, _kind, _size, _ver, w, h, xhot, yhot, _delay = struct.unpack_from("<9I", data, pos)
    return w, h, xhot, yhot, struct.unpack_from(f"<{w * h}I", data, pos + 36)


def _png(width: int, height: int, argb) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for px in argb[y * width:(y + 1) * width]:
            a = px >> 24
            r, g, b = (px >> 16) & 255, (px >> 8) & 255, px & 255
            if 0 < a < 255:  # Xcursor pixels are premultiplied; PNG is straight alpha
                r, g, b = (min(255, c * 255 // a) for c in (r, g, b))
            rows += bytes((r, g, b, a))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def cursors(out_dir: str, nominal: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec = {}
    for role, name in CURSORS.items():
        path = CURSOR_THEME / name
        if not path.exists():
            raise SystemExit(f"{path} not found: install the Breeze cursor theme (breeze)")
        w, h, xhot, yhot, argb = _xcursor_image(path, int(nominal))
        (out / f"{role}.png").write_bytes(_png(w, h, argb))
        spec[role] = {"file": f"{role}.png", "width": w, "height": h, "hotX": xhot, "hotY": yhot}
    (out / "cursors.json").write_text(json.dumps(spec))


def _png_size(path: Path) -> tuple[int, int]:
    width, height = struct.unpack(">II", path.read_bytes()[16:24])
    return width, height


def assemble(frames_dir: str, out_gif: str, width: str) -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found: install ffmpeg")
    frames = sorted((int(m.group(1)), int(m.group(2)), p)
                    for p in Path(frames_dir).iterdir() if (m := FRAME_NAME.match(p.name)))
    if len(frames) < 2:
        raise SystemExit(f"only {len(frames)} frame(s) in {frames_dir}")
    sizes = [_png_size(p) for _, _, p in frames]
    pad_w, pad_h = max(w for w, _ in sizes), max(h for _, h in sizes)
    pad_w += pad_w % 2
    pad_h += pad_h % 2

    with tempfile.TemporaryDirectory() as tmp:
        listing = ["ffconcat version 1.0"]
        for i, (_, ms, path) in enumerate(frames):
            seconds = (frames[i + 1][1] - ms) / 1000 if i + 1 < len(frames) else END_HOLD_SECONDS / 2
            listing += [f"file '{path.resolve()}'", MS_TIME_BASE, f"duration {max(seconds, 0.02):.3f}"]
        # The concat demuxer drops the last frame's duration unless the file is listed once
        # more, and the GIF writer gives that repeat the same delay: the hold is split in two.
        listing += [f"file '{frames[-1][2].resolve()}'", MS_TIME_BASE]
        concat = Path(tmp) / "frames.ffconcat"
        concat.write_text("\n".join(listing) + "\n")
        padded = Path(tmp) / "padded.mkv"
        quiet = ["-hide_banner", "-loglevel", "error", "-y"]
        # Pass 1: one size for every frame. Padding is stateless, so ffmpeg rebuilding the
        # filter when the input size changes is harmless here — it would reset palettegen.
        subprocess.run(["ffmpeg", *quiet, "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-vf", f"pad={pad_w}:{pad_h}:0:0:color={CANVAS},format=bgr0",
                        "-fps_mode", "passthrough", "-c:v", "ffv1", str(padded)], check=True)
        # Pass 2: one palette for the whole clip; only changed rectangles are re-encoded.
        graph = (f"scale={width}:-2:flags=lanczos,split[a][b];"
                 "[a]palettegen=reserve_transparent=1:stats_mode=full[p];"
                 "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
        subprocess.run(["ffmpeg", *quiet, "-i", str(padded), "-filter_complex", graph,
                        "-fps_mode", "passthrough", "-loop", "0", out_gif], check=True)
    print(f"wrote {out_gif} ({len(frames)} frames, {frames[-1][1] / 1000:.1f}s)")


if __name__ == "__main__":
    commands = {"cursors": cursors, "assemble": assemble}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]](*sys.argv[2:])
