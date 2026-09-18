# Lights Out service

Node.js 20 or newer. Install the pinned dependencies first.

```sh
npm ci
npm start
# In a separate terminal:
npm test
```

The local website listens at <http://localhost:8081>. `/api/health` is the health
endpoint. Automated tests create their own temporary servers and storage.

## Configuration

`server/.env.example` documents common settings. The app does not automatically
load `.env`; set variables in your shell/host or use Node's `--env-file` option on
a compatible Node version. After creating your local `.env`, for example:

```sh
node --env-file=.env server.cjs
```

The website can boot without credentials. Persistent accounts, matchmaking and
analytics require your own Upstash Redis REST URL/token; Steam persona lookups
use `STEAM_WEB_API_KEY`; relay ping measurements require your own Cloudflare TURN
credentials. Configure authentication for your own host before testing Steam
sign-in. Never commit a filled-in `.env` or use the official service for automated tests.

The bundled catalogue contains official versions and download URLs. Installers
are not stored in this source repository, so a direct local installer URL will
not serve a binary until you publish your own. Authored gamemode packs are
included. For a deployment, use your own URLs, hashes, operator identity, privacy
notices, credentials and storage prefix.

Set `NODE_ENV=production` for a real service. Test identities are accepted only
in explicit test mode. Review matchmaking, moderation, storage and admin settings
before accepting users. The public source repository does not deploy the
official Lights Out service.

## Email accounts

The website account-creation page is `/account`. Email/password sign-in, emailed
login codes and remembered device sessions are supported by the desktop app.
Configure `HUB_ACCOUNTS_ENABLED=1`, a durable `HUB_ACCOUNT_SECRET`,
`HUB_ACCOUNT_ORIGIN` for your own HTTPS origin and persistent Upstash storage.
`HUB_ACCOUNT_STORE_PREFIX` controls the account namespace. Preserve both this
namespace and the account secret when redeploying; changing either can break access.

For mail, set `HUB_MAIL_TRANSPORT=resend-https` and supply your own Resend sending
key as `HUB_SMTP_PASSWORD`, plus an approved `HUB_SMTP_FROM` sender. SMTP is also
supported; the example environment documents its settings. Never use live mail
credentials in tests. Account tests use fixtures and require the pinned Python
dependencies in `scripts/account-test-requirements.txt` (fakeredis with Lua).
`ACCOUNT_TEST_PYTHON` may select that interpreter explicitly.

`HUB_ACCOUNT_GAMEPLAY_ENABLED=1` enables verified Bodycam Steam authentication
for Lights Out account gameplay and requires your own `STEAM_WEB_API_KEY`.
Verifying the game identity does not link Steam as a sign-in provider.
`HUB_ACCOUNT_OWNERSHIP_ENABLED=1` enables verified account linking, available in
Settings > Account in Lights Out 2.3.89. User-facing disconnection and password
recovery controls remain unfinished. Never
merge profiles merely because submitted names, emails or SteamIDs match.

The supplied privacy and terms pages describe the official service; adapt them
to your own operator, processors, settings and actual retention before deployment.
