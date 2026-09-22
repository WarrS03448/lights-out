# Lights Out 2.6.5

Saved sign-ins now recover automatically after a temporary connection failure.
The app retries in the background, gradually waiting up to 30 seconds between
attempts. Signing out, starting another sign-in, changing accounts, or closing
the app cancels the old restoration. Rejected or revoked credentials are not
retried as if they were an outage.

If the secure connection to the normal app address cannot be established,
Lights Out tries the main website address. Authentication, live updates,
matchmaking actions, catalogue requests, and installer/pack downloads share
this fallback and remember the reachable address for the current app process.
Both addresses use the same existing service and accounts. Custom development
endpoints never fall back to the public service.

Switching occurs only before an HTTP request could have been sent. An action
whose response is lost is not automatically replayed by this transport. HTTPS
certificate checks and download checksums remain enforced, and the fallback
does not forward credentials to unrelated hosts or plaintext URLs.

This adds no new hosting service or IP tracking. Both addresses still use Bunny,
so simultaneous failures on both routes remain possible. This release improves
recovery and route choice; it does not establish universal Russian access or
change the in-game callback addresses. The existing game-mode packs are unchanged.

Lights Out provides community game modes and matchmaking. Its purpose is not to
cheat, and the files it installs are not cheats. It is a community project, not
affiliated with or endorsed by Reissad Studio.
