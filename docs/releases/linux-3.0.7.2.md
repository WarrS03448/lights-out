# Lights Out for Linux 3.0.7.2 (beta)

This update fixes three problems Linux players reported.

- In-app updates and gamemode installs work on btrfs, the default file system
  on Fedora and used by openSUSE and others. Before, the update downloaded
  completely and then said "The update was damaged". If a future update ever
  fails, Lights Out now writes the reason to
  `~/.local/state/lights-out-linux-beta/logs/update.log`.
- Lights Out keeps working after you restart your PC. Before, some systems
  needed "Install and Open Lights Out" again after every restart.
- The rank icons in Profile's rank ladder no longer flicker.

If you are on 3.0.7 or 3.0.7.1 and your home folder is on btrfs (most Fedora
installs), the in-app update to this version still fails, because the check
runs in your current version. Download the ZIP from lightsoutranked.com and
open "Install and Open Lights Out" once; later updates work in the app.

Downloads:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| LightsOut-Linux-Native-3.0.7.2-x86_64.zip | 36483841 | `a057ce95275d6da857079043964cb87b549028a3d1538b3811f6ceb8fb4250b6` |
| LightsOut-Linux-Native-3.0.7.2-source.zip | 56127731 | `f5d83b47ca02bf1aa7a7bb67ef0b8ca43c5309834380d6704bf1b844e4c703dd` |

The source ZIP is the corresponding source of this release, including the
source of its GPL and LGPL components. See docs/release-provenance.md.

Lights Out is an unofficial community project, not affiliated with or endorsed
by Reissad Studio. Its purpose is community gamemodes, not cheating, and its
downloads are not designed to provide cheats.
