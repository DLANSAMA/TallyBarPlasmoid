# TallyBar QML preview + screenshot harness

Small tools that run the widget's UI without a Plasma session:

| File                  | Purpose                                                        |
|-----------------------|----------------------------------------------------------------|
| `harness.qml`         | Interactive preview window (`make preview`)                     |
| `screenshot.qml`      | Offscreen PNG renderer (`make screenshots`, `make render-baselines`) |
| `demo_gif.py`         | Pointer cursors for a walk, and the GIF stitcher for `make screenshots` |
| `mock-telemetry.json` | The fixture the preview and the render baselines use            |

## Preview

```bash
make preview                           # loads FullRepresentation.qml (default)
make preview COMPONENT=CompactRepresentation
make preview COMPONENT=SettingsPopout
```

Requires a display (`$DISPLAY` or Wayland). `harness.qml` opens a plain `Window`
(no Plasmoid root) and uses a `Loader` to pull the target component out of
`io.github.dlansama.tallybar/contents/ui/`, then injects the fixture into its `telemetry`
property — the same property `main.qml` binds in the real applet.

## Screenshots

```bash
make screenshots       # README images, docs/screenshots/*.png + cost-demo.gif, from live usage
make render-baselines  # test baselines, tests/render-baselines/*.png, from the fixture
```

Both run fully offscreen, with no display and no Plasma.

`make screenshots` runs the backend once, the way the widget does
(`--once --background`), into a temp file and renders that snapshot as-is
(`snapshot=`). The README images therefore show the developer's REAL usage and
spend, and they are published: open every PNG, and step through the GIF, before
committing them. They differ on every run, so no test compares them.

`make render-baselines` renders `mock-telemetry.json` instead. `screenshot.qml`
slides the fixture's fixed reference week onto `SHOT_DAY`, so the baselines are
byte-stable and `tests/test_qml_render.py` can compare against them. One of them,
`cost-day-hover.png`, is the Day tab with a pointer resting on its busiest bar
(`walk=hover`, below): its tooltip must show.

Arguments (positional `key=value`, after `--`):

| Argument     | Default              | Meaning                              |
|--------------|----------------------|--------------------------------------|
| `out=`       | `shot.png`           | PNG destination                      |
| `component=` | `FullRepresentation` | also accepts `CompactRepresentation` |
| `provider=`  | `claude`             | which provider tab is selected       |
| `scale=`     | `2`                  | pixel ratio the grab renders at      |
| `today=`     | the real today       | day the fixture's week/month buckets are rebased onto (`make render-baselines` pins `SHOT_DAY`) |
| `snapshot=`  | none (the fixture)   | absolute path of a backend snapshot to render as-is instead; `today=` is ignored |
| `graphMode=` | `week`               | `CostPopout` only: `day`, `week` or `month` tab |
| `walk=`      | none                 | `CostPopout` only: drive a pointer through the flyout first — `hover` or `tour` (see *Pointer walks*) |
| `demo=`      | none                 | with a walk: absolute dir to write one frame per tick into, instead of one PNG |
| `cursors=`   | none                 | with a walk: absolute dir holding `cursors.json` + the pointer PNGs (`demo_gif.py cursors`) |

Four environment variables are required and set by the Makefile:

- `QML_XHR_ALLOW_FILE_READ=1` — QML blocks `XMLHttpRequest` on `file://` URLs by
  default, which is how the fixture is read.
- `QT_QUICK_BACKEND=software` — the offscreen platform plugin has no GL surface.
- `XDG_ICON_THEME=breeze-dark` — otherwise `Kirigami.Icon` resolves the light
  Breeze glyphs, which are invisible against the widget's dark card.
- `XDG_CONFIG_HOME=tools/preview/xdg-config` — its `kdeglobals` pins the icon
  theme to breeze-dark. With `QT_QPA_PLATFORMTHEME=kde` the theme is read from
  `kdeglobals`, so without this the renders follow your desktop's icon theme and
  stop matching the committed PNGs whenever you switch themes.

Renders are byte-stable across days and machines' desktop settings only with both
`today=` and `XDG_CONFIG_HOME` pinned — `tests/test_qml_render.py` compares
`tests/render-baselines/` byte for byte under exactly that environment.

`QT_FORCE_STDERR_LOGGING=1` is also set: without it Qt routes `console.log` to
the journal and the terminal stays silent, which makes a failed render look like
a silent success.

## Pointer walks and the demo GIF

`walk=` moves a pointer through the cost flyout before grabbing. The moves,
presses and releases go through QtTest's `TestEvent`, so the widget's own
`MouseArea` hover and click code runs, timers and all; a walk exercises the real
tooltip, not a picture of one.

- `hover` jumps from the bar beside the Day tab's busiest bar straight onto it,
  without crossing the gap between them, and rests until the tooltip shows. That
  jump is the move that used to leave the tooltip hidden, which is why it is a
  render baseline.
- `tour` rests on each tab's busiest bar until its tooltip shows, clicks it open
  to the per-model breakdown, then moves on to the next tab (Day, Week, Month,
  back to Day). It runs about 16 seconds.

Offscreen Qt has no cursor, so `screenshot.qml` draws one: Breeze's arrow, and its
pointing hand while the pointer is over anything with a pointing-hand
`cursorShape`, plus a ring for a moment after each click. `demo_gif.py cursors`
reads both from the installed Xcursor theme at `CURSOR_PX` (48) and writes them as
PNGs with their hotspots.

With `demo=`, every tick (about 50 ms) writes a frame named `f-<n>-<ms>.png`,
where `<ms>` is when it was grabbed. `demo_gif.py assemble` shows each frame until
the next one's time, so the GIF plays at real speed even though frames land
unevenly, and pads every frame to the tallest (the flyout changes height between
tabs). Frames are opaque on purpose: demo mode paints a solid canvas behind the
card, because ffmpeg's GIF encoder stores only the changed pixels of opaque
frames but every frame whole once there is transparency (19 MB instead of about
0.7 MB).

Walks need three things the plain renders don't: the `QtTest` QML module
(`qt6-declarative`), the Breeze cursor theme (`breeze`), and, for the GIF,
`ffmpeg`. The hover render test skips when the cursor theme is missing.

## Limitations

**`CostPopout.qml` CAN be screenshotted** — both targets write all three of
its tabs (`component=CostPopout graphMode=day|week|month`). It took two
non-obvious steps, both now handled in `screenshot.qml`:

- It inherits `PlasmaCore.PopupPlasmaWindow`, and `grabToImage` refuses an item
  whose *window is not visible*. Creating the object is not enough — the harness
  drives the widget's real `costDrawerOpen` state (gated by `drawerExpanded =
  costDrawerOpen && hasCostSection()`) so the window actually maps.
- Grab the `mainItem`, never its parent: the window's `QQuickRootItem` has no QML
  engine and `grabToImage` refuses it. The card paints no background of its own —
  Plasma's translucent surface normally supplies one and does not exist offscreen —
  so a backdrop `Rectangle` is injected *into* the `mainItem` at a low `z`.

**`SettingsPopout.qml` is not wired up for screenshots.** The same approach should
work (it is the same `PopupPlasmaWindow` shape), but nothing publishes it today.

**Window PLACEMENT still can't be verified offscreen.** KWin positions each flyout
beside the widget; that needs a live session and a real grab (`spectacle -a`). The
screenshots capture card *contents* only.

**`main.qml` is a `PlasmoidItem` root** — it inherits a Plasma C++ type that is
only registered inside Plasma, so it can't be loaded standalone either.

**`i18n()` is undefined outside a Plasma applet.** It's a global the applet host
injects; standalone, any code path that calls it logs a `ReferenceError` and
returns undefined. `FullRepresentation` is unaffected in practice, but
`CompactRepresentation` loses its text-mode label.

**Stubs that fail gracefully:** `org.kde.plasma.plasmoid` (all `Plasmoid.*`
reads are undefined) and `org.kde.plasma.plasma5support` (`DataSource` fails to
construct — the harness never triggers a refresh, so nothing depends on it).

## Extending the fixture

Edit `mock-telemetry.json`. It mirrors the JSON shape `backend.py` prints —
`providers[<key>].limits[]`, `.costSummary`, `.tier`, `.status` — so the quickest
way to get a new state is to run the backend, copy the shape, and replace the
numbers:

```bash
python3 io.github.dlansama.tallybar/contents/code/backend.py --once --no-network --pretty
```

Keep the numbers synthetic: the fixture is published with the repo.
