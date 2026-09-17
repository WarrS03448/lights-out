# Contributing

Open an issue describing the problem, expected behavior and relevant version.
For a change, fork this repository, use a topic branch, and submit a pull request
with the reason for the change and the checks you ran.

- Run `python -m pytest -q` from the root and `npm test` from `server/`.
- Test against a local service with a separate `HUB_STATE_DIR`.
- Keep user-facing text in the existing seven languages: de, en, es, fr, pt, ru, zh.
- Include a focused regression test for behavior changes.
- Do not include credentials, player identifiers, chat logs, game dumps, stock
  game assets, or third-party content without its distribution license.
- Keep changes focused on consensual community gamemodes. Cheat functionality,
  unauthorized host abuse and misleading anti-cheat claims are outside the
  project's scope.

By submitting a contribution, you agree to license your original contribution
under the repository's MIT license. Clearly identify separately licensed material
and include its applicable notices. No copyright assignment is required.

For sensitive security reports, follow [SECURITY.md](SECURITY.md).
