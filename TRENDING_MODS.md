# Trending Mods

The **2 · Trending** workspace is a live Nexus Mods discovery surface for Cyberpunk 2077.
It supports the Nexus `trending` and `latest_updated` feeds, displays live Endorse/Track
account state, and routes installation through the manager's existing Download Manager,
Archive Management, Installer and Smart Deployment layers.

## One-click installation

For Premium accounts, **Install** selects the Nexus primary MAIN file when one is marked;
otherwise it selects the newest MAIN file. It then downloads, retains the source archive,
uses authored FOMOD defaults, skips pre-existing conflicts by default, stages the payload,
and rebuilds smart-deployment links.

The manager intentionally refuses to guess when a mod exposes only OPTIONAL/MISC files.
Open the Nexus Files tab and choose a file explicitly in that case.

For free accounts, Nexus requires a fresh **Download with Manager** authorization token.
Pressing **Install** records a pending install intent and opens the selected Nexus Files URL.
When the matching `nxm://` link reaches Download Manager, the queue item is marked
`install_after`; once the archive finishes downloading it automatically continues through
the same safe installer pipeline. Download success remains preserved even if the later
installation step fails.

## Account actions

Endorse and Track buttons reuse the existing authenticated account backend and verify the
server state after every mutation. API keys are passed through the child environment and
are not written into command logs.
