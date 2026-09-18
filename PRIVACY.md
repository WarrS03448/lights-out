# Connection IP privacy

This document describes connection privacy in Lights Out 2.3.87.
The application does not derive player identity from a connection IP
or record connection IPs in its diagnostic logs, analytics, or player records.

## Audited paths

| Path | Protection |
| --- | --- |
| Host travel | The cooked launcher seed sends the host's private match credential. The server authorizes that credential and checks the host's permit without reading socket or forwarding-header addresses. |
| Server diagnostics | Request headers are limited to normalized content type, size and a redacted authorization marker. Query parameters, user-agent strings, IP fields, unknown body fields and unstructured bodies are omitted before memory/storage writes. |
| Match reports | Authenticated reports use the same diagnostic filtering. Gameplay processing still receives the separate combat-completion value carried in the native API's legacy `ip` field. This value is not a network address. |
| Server analytics | Ingestion only accepts selected diagnostic fields and code values. Raw headers and IP fields are discarded. |
| Desktop telemetry | Only selected fields enter the local outbox and upload batch. IP fields and literal IPv4/IPv6 values in diagnostic reason/error fields are discarded. |
| Connection-quality checks | WebRTC uses relay connections. Client origin, connection and related addresses are masked; the server rejects unmasked signaling. Signaling is forwarded transiently rather than written to storage. Relay infrastructure addresses are needed to connect to the relay. |
| Local web bridge | HTTP request logging is disabled. Its loopback listening address is not a player-identity or analytics record. |
| Native attribution inputs | Generated gameplay/lobby calls send empty IP inputs, except the combat-completion value described above. |

## Run the checks

From the repository root, with Node and the Python test dependencies installed:

```sh
npm --prefix server run test:privacy
node tests/test_relay_browser.cjs
python -m pytest tests/test_telemetry.py tests/test_host_travel_safety.py tests/test_attribution_privacy.py -q
```

GitHub Actions runs these checks on pushes and pull requests, followed by the
full Python and server suites. The HTTP tests inspect actual diagnostic storage
writes using local fixture storage, exercise authenticated reporting, and throw
if the host-travel handler tries to read a socket or forwarding-header address.
Tests use documentation-range IPv4/IPv6 addresses and synthetic account IDs.
Desktop tests inspect both the saved outbox and the emitted upload batch.
Generated-graph tests inspect attribution inputs in lobby, gameplay and combat
requests, including the one deliberate combat-completion field reuse.

These checks protect the reviewed paths; they are not a proof against every
possible future change. Review new logging, analytics, middleware, dependencies
and infrastructure settings against this requirement before merging them.

## Scope and limits

Hosting and relay providers still see network addresses needed to deliver the
service. [Railway HTTP logs](https://docs.railway.com/observability/logs) include
source IPs, and [Cloudflare TURN](https://developers.cloudflare.com/realtime/turn/faq/)
processes client addresses to establish and maintain relay connections. This
repository cannot disable those independent provider logs.

User-entered chat, reports or support messages can contain information a person
chooses to type; the application does not promise to remove every address from
that content. Bodycam and Steam have their own networking and privacy practices.
Their native implementations and hosting infrastructure are outside this source
audit. Fork operators must review their own middleware, access logs and providers.

See the [published Privacy Policy](https://lightsoutranked.com/privacy) for the
official service's complete disclosure.
