# Lights Out service

Node.js 20 or newer; no npm runtime dependencies.

```sh
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
