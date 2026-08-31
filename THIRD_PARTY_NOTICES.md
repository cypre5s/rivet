# Third-Party Notices

Rivet uses the following third-party components. Versions are pinned by
`tui/bun.lock`; the installed packages retain their complete license files.
This notice does not replace or alter any component's license terms.

## Direct TUI components

| Component | Version | License | Copyright notice |
|---|---:|---|---|
| `@opentui/core` | 0.5.8 | MIT | Copyright (c) 2025 opentui |
| `@opentui/keymap` | 0.5.8 | MIT | Copyright (c) 2025 opentui |
| `@opentui/react` | 0.5.8 | MIT | Copyright (c) 2025 opentui |
| `react` | 19.2.8 | MIT | Copyright (c) Meta Platforms, Inc. and affiliates |

The complete MIT texts are distributed as `LICENSE` in each corresponding
package directory.

## OpenTUI native bundle

Platform-specific `@opentui/core-*` packages include the OpenTUI license plus
the upstream license files listed below. Rivet's offline license verifier
requires all of them to remain present in every installed native bundle.

| Bundled component | License file |
|---|---|
| OpenTUI native core | `LICENSE` |
| Ghostty terminal components | `LICENSE-GHOSTTY` |
| Little CMS | `LICENSE-LCMS2` |
| libwebp | `LICENSE-LIBWEBP` |
| stb libraries | `LICENSE-STB` |
| Wuffs | `LICENSE-WUFFS` |

Complete, unmodified terms and attributions are available beside each native
binary in its installed package. Source distributions can reproduce the exact
set with `bun install --frozen-lockfile` from `tui/`.
