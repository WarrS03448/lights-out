# Lights Out 2.3.87

The auto-host now finishes native lobby startup before opening the selected
match map once. Its original startup callback cannot schedule a second map
load. Joiners get one **Reconnect to game** button after launching, translated
into all seven supported languages. Initial launch still waits for host readiness.

The release process now signs and verifies the application before packaging,
then signs and verifies the final installer. Both have timestamped signatures
from **Samuel Warren**. Signing failures stop publication. The working setup and
repeatable procedure are documented in [code-signing.md](code-signing.md).

Normal matchmaking, gameplay packs, joiner assets and server behavior are
unchanged. Private solo-test settings and permissions are excluded.

## Verification

- Production source: 681 Python tests and 26 subtests passed.
- Public source with its additional privacy checks: 691 Python tests and
  26 subtests passed.
- Full server suites passed; production includes the existing 247 integration
  checks. Browser checks cover 112 role/phase/readiness/connection/language states
  and verify exactly one correct game action per click.
- Real local HTTP/SSE match recovery passed with simulated game processes.
- Host/joiner package generation, all installed map targets, token retargeting,
  replacement and cleanup passed using scratch installations, without skips.
- Source graph checks and shipped host bytecode checks passed. The joiner assets
  match the previous public release exactly.
- Packaged client self-check passed: 9 screens, 71 actions, 7 languages.
- Independent read-only review found no blocking issue in the production
  extraction, single-load flow, reconnect UI or signing pipeline.

The signed installer is 21,337,752 bytes. SHA256:
`a42bb3b6ed56535f692b5247220630bd1fef771145adfdbb9ea079104821ea66`.

Solo tests observed one map load per launch. A multi-client Steam test was not
performed for this release. The previously observed native memory crash has not
been identified or claimed fixed. Existing public host recovery after the host
process closes is not changed; the reconnect label applies to joiners.

This is an unofficial community project, not affiliated with or endorsed by
Reissad Studio. Its purpose is community gamemodes, not cheating; it does not
download cheats.
