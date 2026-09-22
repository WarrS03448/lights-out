# Lights Out 2.6.4

Lights Out now connects through `https://play.lightsoutranked.com`, with Bunny forwarding requests to the existing Railway service. The website and browser sign-in use `https://lightsoutranked.com`. Existing accounts and data stay on the same backend.

This improves access on the Russian networks reached by the proxy: the initial trial reached eight of ten tested Russian locations. After release, valid HTTPS checks reached the app from nine of ten and the account page from eight of ten; the failing locations differed between hostnames and test runs. This is a small network sample, not an estimate of all Russian players or proof of complete multiplayer sessions. Players unable to reach the old update service will need to download the new installer from the website.

Bodybomb 5v5 is now 1.0.29. Its authored callbacks and the authored lobby/join seeds use the new connection hostname. The replacement preserves asset lengths and all other bytecode; the release proof accounts for 32 URL constants across seven files. Cached lobby seeds receive new names so an older seed cannot retain the previous route. Capture the Flag remains 1.0.4 with identical archive bytes.

Browser authentication uses a fixed, validated public origin. Account-page routing recognizes the original hostname supplied by the proxy. Customer CDN request logging, permanent log storage and log forwarding remain disabled. Following Bunny support guidance, all-request rules set `X-Forwarded-For` and `X-Real-IP` to empty values to suppress forwarding those client-IP headers to the origin. The application does not collect client IP addresses; network providers necessarily process them to deliver traffic.

Validation: 792 Python tests and 26 subtests passed; the full server suite passed. The signed installer and bundled application passed the publisher checks. The released installer and both packs were downloaded in full and matched across the app, root, www and legacy routes; the downloaded installer signature is valid. The frozen build contains the reviewed connection settings, authored seeds and retarget tool. Temporary lobby/join assembly and all map variants passed using read-only game inputs. No game launch, real-player sign-in, ten-player match, voice relay or in-game migration test was performed for this release.

Lights Out provides community game modes and matchmaking. Its purpose is not to cheat, and the files it installs are not cheats.
