# Security policy

Report vulnerabilities privately to **contact@theneeb.com** with the affected
version, a description and reproduction steps using a local test environment.
Do not send passwords, session tokens, player records or exploit demonstrations
against the live service. Do not post an unpatched vulnerability in a public issue.

Security fixes target the latest release; older versions are not separately
maintained. No response-time guarantee or bug-bounty program is offered.

The public repository contains no deployment secrets. Keep Steam, Upstash and
TURN credentials server-side, and never commit `.env` files or local state.
