# Mod Detail Modal

Nexus Collection Manager 0.12 adds one shared in-app detail dialog for Nexus mods. It is used by Trending, Recently Updated, Endorsed, Tracked, and the installed Nexus-account view.

## Sections

- **Description** — summary plus the Nexus mod description.
- **Files** — file category, filename, version, size, upload date and Nexus file ID.
- **Changelog** — Nexus changelog entries grouped by version.
- **Requirements** — Nexus mod requirements, notes and links.
- **DLC** — game/DLC requirements reported for the mod.

Secondary sections are fail-soft: if Nexus cannot return files, changelogs, requirements, DLC data, or account state, the available sections still open and the dialog shows a warning describing the unavailable data.

## Actions

The header provides **Install**, **Endorse/Unendorse**, **Track/Untrack**, and **Open Nexus**. In the Files section a selected Nexus file can be installed directly. Install actions reuse the existing Download Manager -> Archive Management -> Smart Installer -> Smart Deployment pipeline; they do not create a separate download/install path.

Free Nexus accounts still use the required Download-with-Manager/NXM authorization flow. Premium accounts can continue through the direct manager-download path.

## Data sources

The dialog aggregates the Nexus v1 mod detail, file and changelog endpoints with the project's Nexus GraphQL requirement/DLC lookup. Account state is synchronized through the existing endorsement/tracking backend. The primary mod metadata request is required; all secondary requests are independent and fail-soft.

Nexus-supplied description HTML is rendered with active/embed-only elements removed before display. External links are opened by the desktop rather than inside the application.
