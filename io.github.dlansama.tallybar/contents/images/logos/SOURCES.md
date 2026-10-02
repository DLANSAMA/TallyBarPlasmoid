# Logo Sources

These assets are provider marks, not TallyBar-owned artwork. Keep them unmodified except for format conversion, cropping to the provided standalone icon, or transparent-background raster fallbacks required by Plasma/QML.

- `antigravity.png`: Google Antigravity desktop icon, taken from the installed application's own pixmap (`~/.local/share/pixmaps/antigravity.png`).
- `claude.svg`: Claude product symbol from Anthropic's Claude asset proxy, `https://assets-proxy.anthropic.com/claude-ai/v2/assets/v1/cd02a42d9-Vq_H3mgS.svg`.
- `gemini.svg` and `gemini.png`: Google-hosted Gemini sparkle assets referenced by `https://gemini.google.com/app`.
- `openai.svg`: OpenAI Blossom path extracted from OpenAI's official brand-page SVG at `https://openai.com/brand/`.
- `../tallybar-provider-icons/*`: Monochrome provider switcher marks copied from
  [CodexBar](https://github.com/steipete/CodexBar) by Peter Steinberger
  (`Sources/CodexBar/Resources/ProviderIcon-*.svg`), which is MIT-licensed — see the
  copyright notice reproduced in [`../../../NOTICE`](../../../NOTICE). The inactive
  variants change only the SVG fill colour.
- `../tallybar-provider-icons/grok-*.svg`: The Grok mark, a trademark of xAI. Taken from
  the author's own `grok-build-mobile` project assets and reused here unmodified except
  for the fill colour (`#6d6978` inactive / `white` selected, matching the other switcher
  marks) and a tightened `viewBox` so it carries the same visual weight as its siblings —
  the path data is untouched. Replaces an earlier placeholder X mark drawn for this repo.
- `../tallybar-provider-icons/grokbot-*.svg`: The Grok Bot mark, a trademark of xAI /
  SpaceXAI. Geometry taken from Grok Bot 0.63.0 (`/opt/Grok Bot/resources/app.asar` →
  `dist/renderer/assets/index.eager-common-CN0nwxVh.js`): the outline path `h7` plus the
  resting eye rings `Nn[0]`, the same three paths as
  `https://asvg.app/assets/svg/grok-bot/grok-bot-logomark-mono.svg`. Only the fill
  (`#6d6978` inactive / `#ffffff` selected) and the combination into one even-odd path, so
  the eyes are real holes under `isMask`, are changed.
