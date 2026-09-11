# TallyBar QML preview + screenshot harness

Two small tools that run the widget's UI without a Plasma session:

| File                  | Purpose                                                        |
|-----------------------|----------------------------------------------------------------|
| `harness.qml`         | Interactive preview window (`make preview`)                     |
| `screenshot.qml`      | Offscreen PNG renderer (`make screenshots`)                     |
| `mock-telemetry.json` | The fixture both of them render                                 |

Both read the same fixture, so the preview and the screenshots published in the
README can't drift apart.

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
make screenshots       # re-renders docs/screenshots/*.png
```

Runs fully offscreen — no display, no Plasma, and no real usage data: the PNGs
are rendered from `mock-telemetry.json`, so nothing from the developer's own
`~/.tallybar` ledger can leak into a committed image. `screenshot.qml` slides the
fixture's fixed reference week onto the current one before rendering, so a
committed screenshot never shows "Updated 180d ago" or a stale month grid.

Arguments (positional `key=value`, after `--`):

| Argument     | Default              | Meaning                              |
|--------------|----------------------|--------------------------------------|
| `out=`       | `shot.png`           | PNG destination                      |
| `component=` | `FullRepresentation` | also accepts `CompactRepresentation` |
| `provider=`  | `claude`             | which provider tab is selected       |
| `scale=`     | `2`                  | pixel ratio the grab renders at      |

Three environment variables are required and set by the Makefile:

- `QML_XHR_ALLOW_FILE_READ=1` — QML blocks `XMLHttpRequest` on `file://` URLs by
  default, which is how the fixture is read.
- `QT_QUICK_BACKEND=software` — the offscreen platform plugin has no GL surface.
- `XDG_ICON_THEME=breeze-dark` — otherwise `Kirigami.Icon` resolves the light
  Breeze glyphs, which are invisible against the widget's dark card.

`QT_FORCE_STDERR_LOGGING=1` is also set: without it Qt routes `console.log` to
the journal and the terminal stays silent, which makes a failed render look like
a silent success.

## Limitations

**`CostPopout.qml` and `SettingsPopout.qml` cannot be screenshotted offscreen.**
Both inherit `PlasmaCore.PopupPlasmaWindow`, so each is a separate window with
its own scene. `grabToImage` on an item in another window's scene fails with
*"cannot call function with argument created in a different engine"*. Those two
need a live Plasma session and a real screen grab (`spectacle -a`).

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

Keep the numbers synthetic. These files are published.
