# Bundled fonts (offline, no CDN at runtime)

The web UI renders the 1c design's type system: **Oswald** (display) and **IBM Plex Sans**
(body). These are bundled here as `.woff2` and wired up in `../tokens.css` with `@font-face` so the
hub renders with no network and no CDN — the same "bundle it, do not fetch it" discipline the rest
of the app uses (`docs/ui-redesign-plan.md`, "Assets and fonts").

## What is here

Both families are **variable fonts**, so a single file per subset covers every weight the design
uses (Oswald 500/600/700, IBM Plex Sans 400/600/700):

- `oswald-latin.woff2`, `oswald-latin-ext.woff2`
- `ibmplexsans-latin.woff2`, `ibmplexsans-latin-ext.woff2`

Source: Google Fonts (`fonts.googleapis.com` / `fonts.gstatic.com`), the `latin` and `latin-ext`
unicode-range subsets. SIL Open Font License 1.1.

## Coverage / fallbacks

Bundled subsets cover en/de/es/fr/pt. For **ru** (Cyrillic) and **zh** (CJK) — which these
families do not include anyway — `tokens.css` falls back to the system stack (`Segoe UI` /
`Microsoft YaHei UI` / `system-ui`), so text stays readable in every language.

## TODO (Phase 2)

- Add the **cyrillic** subsets of Oswald + IBM Plex Sans for a nicer `ru`.
- Add **Chakra Petch** and **Space Grotesk** (used by the other design directions) if any later
  screen needs them.

## Re-fetching

To refresh, request the `css2` URLs with a modern-Chrome `User-Agent` (so Google serves woff2),
pull the `latin`/`latin-ext` `src url(...)` from each, and save here:

    https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&display=swap
    https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&display=swap
