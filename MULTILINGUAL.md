# Multilingual UI

Nexus Collection Manager v0.17.5 ships with two built-in interface languages:

- English (`en`)
- Українська (`uk`, `uk-UA`)

## Language selection

The language is stored in `QSettings` as `ui/language`.

- A new installation follows the operating-system locale: Ukrainian locales start in Ukrainian; other locales start in English.
- The first Guided Onboarding page includes a language selector.
- At any time use **Setup → Language → English / Українська**. The change is applied immediately without restarting the manager.

The language choice itself is saved immediately so the onboarding wizard can be reopened in the selected language. Game paths, Nexus credentials, and scan configuration are still committed only when Guided Onboarding finishes.

## What is translated

Application-owned UI text is localized: tabs, buttons, labels, menus, table headers, tooltips, placeholders, dialogs, wizard pages, common manager statuses, and dynamically created context-menu actions/cards.

Nexus-authored content is deliberately left unchanged. Mod names, descriptions, changelogs, file names, and other author text are displayed exactly as Nexus supplies them. Backend CLI/log protocol text also remains English so scripts and parsers stay stable.

## Developer notes

`gui/i18n.py` contains the English→Ukrainian catalogue and the Qt runtime localization manager. English is the canonical/fallback source language.

`tests/test_i18n.py` scans static GUI strings and fails if a new application-owned string is introduced without a Ukrainian catalogue entry. Technical tokens that intentionally look identical in both languages (for example `FOMOD` and `SHA-256`) are still explicitly listed in the catalogue.
