# Admin analytics

Open **Admin → Analytics** (`/admin/analytics`). The existing Steam admin session and current allowlist protect every page, query, detail and export.

## Accounts across the console

Overview, Players and Analytics share current account population metrics: distinct player
profiles, verified Lights Out registrations (including linked accounts), linked accounts,
and Steam-only profiles. A linked Steam/Lights Out pair counts as one player profile.
Website registrations appear even before the first gameplay connection. Current profile
totals do not follow match filters; Analytics separately counts registrations in the
selected inclusive UTC dates. These are current registration records, not a historical
daily active-user series or a count of login attempts.

Players can be searched by name, canonical player ID, Lights Out account ID, Steam login
ID or last observed game Steam ID. The column picker includes account type, account ID,
creation date and both Steam identity fields. Creation date differs from first gameplay
connection. The former Sign-ins column is labeled Hub connections because reconnects
increment it. Existing saved column choices remain available.

Player links select the exact canonical profile. Free-text searches may intentionally
match more than one profile using a game Steam ID; exact links do not mix their histories.
Analytics match and event links return to the exact player row, and match details show
canonical player ID separately from the game's Steam ID. Disconnecting Steam retains
the original Lights Out profile while the new independent Steam owner stays separate.

`admin-accounts.cjs` reads verified account records and ownership ledgers through bounded
background SCAN/MGET batches (at most 100 records read per step, 250ms between steps,
one-minute refresh after completion). It publishes only a complete, internally consistent
allowlisted snapshot. Page requests do not scan account keys. Restart rebuilds this
snapshot from canonical storage; failed/partial reads show unavailable counts, and a
failed later refresh retains a clearly labeled previous snapshot. Profile/leaderboard
read failures also prevent complete population totals and are retried.

No credential, email, password hash, verification code or session is exposed or copied
into analytical events/exports. The read-only inventory creates no new durable account
index and does not change login, ownership, rank, moderation or settlement authority.
Restricted private recording services continue to reject account enumeration through
their existing store guard and report account inventory unavailable.

## What is captured

- Overview: completed/cancelled games, daily volume, scoreboard and damage coverage, formation-to-end duration, matchmaking quality, queue-unit waiting time, rating spread, starting-side outcomes and prediction calibration.
- Match explorer: map, release, population, result, players, before/after MMR, uncertainty and RR, saved performance and progress factors, rounds, retained formation and diagnostic evidence, available combat observations and resolved rules.
- Ratings: committed rating movement and starting-rank cohorts. Counts represent match appearances, not unique active accounts.
- Balance: map outcomes, starting sides, party outcomes, kills/deaths and complete-coverage damage. Team number does not identify the attacking side. Starting-side outcomes do not imply a side held throughout a game.
- Reliability: persisted daily event totals by type, source and release, errors/warnings, measured request duration averages and approximate percentile buckets, plus collection health.
- Event explorer: safe structured lifecycle/request/admin events and desktop sessions, screen/verb names, connection state, operation/update/launch outcomes and exception categories. No UI arguments or exception messages are captured.
- Rank audit: permanent, atomic records of manual MMR changes, rank changes and resets, including administrator, target and before/after values. Retries do not duplicate an operation; concurrent identical operations are labeled as no-ops.

The default population is ten-player matches. Select **All, including tests** to inspect smaller developer games. Date ranges are inclusive UTC days; row timestamps display in the browser timezone. Release labels on matches are the server-required hub release at settlement, not a claim that every participant ran that exact binary. Historical records lacking version/rules metadata are labeled unknown.

Missing observations are null and excluded from relevant averages. Existing collectors supply gameplay evidence; this release does not add native game hooks or establish complete shots, headshots, objectives or weapon telemetry. Observed differences between versions are descriptive, not causal proof. CTF installs/operations are observed, but only the existing competitive service produces authoritative ranked match dossiers.

## Persistence and recovery

Uses the existing `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, and `HUB_STORE_PREFIX`; no new provider is required. No credentials means bounded temporary development memory and an explicit dashboard warning.

- Operational event payloads: 30 days; stable IDs acknowledge retry duplicates.
- Analytical match detail: 365 days, lightweight indexed summaries for lists.
- Daily match/reliability totals: 1,095 days. Comparison is marked unavailable when its earlier period falls beyond retention.
- Canonical settlement receipts and manual rank audits: permanent, preserving the existing authoritative result lifetime. Match detail can fall back to a canonical receipt if its analytic copy has expired.
- Result commits atomically enqueue their canonical receipt key in `analytics:outbox`. A background worker projects once and removes the reference only after success. Receipt fingerprints are independent of presentation code. Failed items remain queued without blocking healthy items. Schema/reducer changes require an explicit aggregate migration; do not replay changed reducers into existing v1 totals.
- A bounded SCAN backfill imports existing canonical receipts with their original evidence. Old canceled archives without canonical receipts are not reconstructed. New terminal cancellations persist separately with retry; a hard crash before the asynchronous terminal save can lose that cancellation projection.
- New match configuration is resolved at settlement; current rules do not change during a running process. Deployment and effective rules are tracked separately. Gameplay overrides are snapshotted from the service.
- Server diagnostics buffer up to 1,000 events. They are best effort, may be lost on abrupt process death, and do not block gameplay. Collection counters expose write failures and drops. Terminal snapshots have a separate 100-item retry buffer.
- Desktop diagnostics buffer up to 1,000 events/1 MiB in the app state directory. Writes are coalesced off UI callbacks; uploads use <=40 events and a minimum 2.5-second interval with backoff. Only acknowledged IDs are removed. Identity changes discard another token's unsent rows with a gap event. Old anonymous contents are never adopted by a later user. Abrupt exits may lose the last ~250 ms before disk flush.

Event payloads allow only selected numbers, booleans and code tokens. New analytics excludes credentials, raw request bodies, typed chat/report text, IPs and paths. Existing authoritative systems retain their own records separately. Public disclosure: `/faq#faq-data`.

Queries scan at most 2,000 indexed candidates per page; “Next page” continues from that point. Exports contain up to 200 matching rows from the current offset, with continuation metadata in JSON or `X-Analytics-Next` for CSV. They are not an unlimited database dump. CSV cells neutralize spreadsheet formulas. Raw combat details page in groups of 100. Reliability daily groups are capped at 512 distinct combinations plus a visible mixed overflow group. Aggregate reads batch 14 UTC days per storage call.

## Validation

- `cd server && npm test`: full existing suite plus analytics and authenticated HTTP contracts.
- `python -m pytest tests -q`: desktop behavior, isolated outbox, retries, identity transitions and worker coalescing.
- `python server/scripts/test-analytics-lua.py` and `python server/scripts/test-settlement-lua.py`: actual production Lua using fakeredis/lupa; duplicate delivery, wrong types, corrupt aggregates, retention and atomic rank auditing.
- With Playwright installed, `node server/scripts/test-analytics-browser.cjs`: seven views at desktop/mobile widths, round detail, pagination, escaping and CSV export. Fixtures are local and never write production match data.

No installed game files, ranking formulas, match rules or enforcement settings are changed by analytics.
