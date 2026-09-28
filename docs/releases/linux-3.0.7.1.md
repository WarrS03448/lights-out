# Lights Out for Linux 3.0.7.1 (beta)

This update fixes three problems players hit with the first Linux beta.

- Lights Out now opens on Wayland desktops. On newer distributions (for
  example Arch, Fedora 44 and openSUSE Leap 16) the app bundled an older
  Wayland library that kept the system's graphics driver from loading, so the
  window never appeared. It now uses your system's Wayland library. If the
  window still cannot open, Lights Out tries again through X11 and then with
  software drawing by itself, and if nothing works it tells you and writes
  `~/.local/state/lights-out-linux-beta/logs/last-start.log`. It no longer
  closes without a word.
- The Steam sign-in page opens in your browser again on KDE Plasma. If no
  browser can be opened, Lights Out shows the sign-in link so you can open it
  yourself.
- Settings shows your Bodycam folder from Steam right away, before your first
  gamemode install, and finds it in more Steam setups: libraries on other
  drives, linked or moved Steam folders, and a game with an update waiting.
  When it cannot find the game, Settings says why.

Installed Linux betas offer this update in their update strip. The window
start and the telemetry kept across restarts were also made more reliable.

Downloads:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| LightsOut-Linux-Native-3.0.7.1-x86_64.zip | 36481183 | `7b48b1ac3f585d888bcde7880c3de1ea1c936f55e6ebe0adf5760e9d27231031` |
| LightsOut-Linux-Native-3.0.7.1-source.zip | 56122997 | `88b5265db69ef03033a755f3ce3707c63b0b13449eb0f884b11f2799217ed480` |

The source ZIP is the corresponding source of this release, including the
source of its GPL and LGPL components. See docs/release-provenance.md.

Lights Out is an unofficial community project, not affiliated with or endorsed
by Reissad Studio. Its purpose is community gamemodes, not cheating, and its
downloads are not designed to provide cheats.
