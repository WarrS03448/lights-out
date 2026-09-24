# Ranked match recovery

Lights Out can offer recovery after host migration fails when a compatible
completed-round checkpoint exists and recent participant observations no longer
show progress in the authorized match session. Missing reports alone do not
prove that Steam deleted the lobby. The client may ask players to close the
stranded game before a participant can become the replacement host.

Recovery preserves the saved map, teams, score and completed-round statistics.
The interrupted round is replayed. One participant claims the replacement host
role; other players use the recovered-match rejoin action after its lobby is ready.

Players have five minutes from replacement-lobby readiness to return. The client
shows the countdown and who has returned. Play can resume earlier when everyone
returns. Missing players at the cutoff are excluded from that match and receive
the existing abandonment penalty. With players on both sides, the match continues
with the remaining roster. An empty side produces a forfeit using the recorded
score. A failed replacement host does not automatically penalize everyone else.

The release has automated server, client, browser and cooked-asset checks.
Actual cross-host Bodycam recovery, physical spawn/team restoration and removal
of a late player joining through Steam have not yet been verified in multiplayer.
The game integration requests removal after login rather than rejecting native
login before it completes. These limits remain subject to live-game validation.

Update Lights Out and the installed Bodybomb pack before joining a new queue.
Recovery is unavailable for older matches that do not have compatible checkpoints.
