# Themes

Nexus Collection Manager v0.17.5 includes four runtime-selectable Qt themes.

## Available themes

| Theme | Purpose |
| --- | --- |
| **Qt Default** | Restores the platform Qt style, palette, and application stylesheet captured before NCM applies any custom theme. |
| **Qt Dark** | The existing NCM dark Fusion theme. This remains the default for upgrades and new installs. |
| **Qt White** | A light Fusion theme using the same spacing/component treatment as Qt Dark. |
| **Cyberpunk 2077** | A Linux-native black/yellow/cyan/red theme inspired by the game's UI, with sharp corners and high-contrast focus/selection states. |

Themes can be changed while the application is running from **Setup → Theme**. The setting is saved as `ui/theme` in the existing Qt `QSettings` store and is restored on next launch.

The first page of Guided Onboarding also exposes the theme selector so language and appearance can be chosen before Nexus/game configuration.

## Theme identifiers

The persisted identifiers are stable and may be used by integrations or tests:

- `qt-default`
- `qt-dark`
- `qt-white`
- `cyberpunk-2077`

Unknown values fall back to `qt-dark`.

## Qt Default restoration

The theme manager captures the untouched Qt application style name, palette, and stylesheet immediately after `QApplication` is created. Selecting **Qt Default** restores that state rather than approximating a native theme with another palette.

## Semantic widget styling

Application-owned labels/buttons that need a semantic color use Qt dynamic properties rather than literal color styles:

- `muted=true`
- `warning=true`
- `danger=true`
- `accent=true`

That lets the same controls render correctly in Dark, White, and Cyberpunk themes. Pure Qt Default intentionally leaves those controls to the native platform style except for behavior/layout.

## Cyberpunk 2077 theme

The Cyberpunk theme does not ship or depend on game assets, fonts, Windows tools, or proprietary REDmod components. It is only a QPalette/QSS color and geometry preset. Its primary visual tokens are:

- black/charcoal surfaces
- Cyberpunk yellow primary actions and active tabs
- cyan focus/selection states
- red destructive actions
- square/sharp control geometry

It therefore remains fully Linux-native and has no external theme dependency.
