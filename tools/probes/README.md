# Disposable recovery feasibility probes

These are local engineering experiments, not a Lights Out recovery implementation
or a Bodycam tester package. They write only into this isolated mirror project.
No Bodycam installation, Steam identity, online lobby or production service is used.

Requires the project's existing Python environment and Epic Unreal Engine 5.5 with
the `BodycamEditor` mirror target built. No additional Python dependencies are used.
Native Bodycam/HMS `.cpp` files in this mirror are declaration-compatible stubs;
their behavior cannot establish native game capture or restore.

## Reproduce

From this worktree, in PowerShell (all generated text is UTF-8):

```powershell
$probeProject = Join-Path (Get-Location) 'mirror/Bodycam/Bodycam.uproject'
$probeEditor = 'C:/Program Files/Epic Games/UE_5.5/Engine/Binaries/Win64/UnrealEditor-Cmd.exe'
$probePython = '.venv/Scripts/python.exe'
& 'C:/Program Files/Epic Games/UE_5.5/Engine/Build/BatchFiles/Build.bat' BodycamEditor Win64 Development "-Project=$probeProject" -WaitMutex
if ($LASTEXITCODE -ne 0) { throw 'Mirror build failed' }
& $probePython tools/probes/run_save_probe.py
if ($LASTEXITCODE -ne 0) { throw 'Checkpoint probe failed' }
& $probeEditor $probeProject -run=pythonscript "-script=$((Get-Location).Path)/tools/probes/build_connection_probe.py" -stdout -unattended -nopause -nosplash -nullrhi
if ($LASTEXITCODE -ne 0) { throw 'Connection Blueprint build failed' }
& $probePython tools/probes/run_connection_probe.py
```

Check the new `Saved/RecoveryProof/save-*/run.json`, its `capture.json`/`reload.json`,
`connection-build.txt`, and the new `network-*/run.json`/`readback.json` under the
mirror project. Both final `run.json` reports must say `status: verified`. A commandlet
exit code alone is insufficient: each report must pass and the graph build must
have no warnings/errors and end with `RESULT: OK`.

The runner starts a listen server and two clients on loopback, records changing
replicated values separately for each participant, stops one owned client, then
the owned host, checks a surviving client stays alive while its observations stop,
and connects a fresh client to a differently labeled session. A separate Unreal
process decodes the saved observations. Every run has unique live and evidence
slots. The replicated session value includes the owned host's unique run marker;
a client's own local marker cannot establish which server it joined. The network
report stays `ok: false` until native readback verifies the
complete set of eleven records and both copies of each snapshot's size/hash.
After a three-second grace, host loss is measured continuously for at least six
seconds while the surviving process remains alive. It terminates only processes
it created. Allow it to finish before rebuilding
the Blueprint assets.

The storage probe compares a nonempty synthetic `FHMS_GameSave` after a separate
Unreal process loads it from disk. Player and actor IDs, classes, transforms,
nested component maps, property buffers and Unicode names are compared; capture
values are asserted absent from Blueprint defaults. Each driver run uses a new
slot/manifest and random payload challenge; it waits for the exact capture process
to exit before starting reload. It also checks rejection of missing/corrupted
saves, an older manifest and an older valid save in separately owned slots.
These negative cases must fail for their intended reason; the original successful
save remains intact. Reload never recompiles the wrapper asset. No native capture, native
restore, gameplay continuation or second-machine transfer is tested.

Frozen connection records describe the last received update. They do not give
current controller/world state, identify the network failure cause or establish
that everyone disconnected. No backend reporting or recovery decision is tested.

Source and evidence remain local research. Do not ship generated mirror assets
or present these experiments as gameplay verification.

Evidence validator regressions (decoder replaced only at the Unreal boundary):
`python -m pytest tests/test_recovery_probes.py -q`. These test the checker;
the separate engine runs test native serialization and replication.
