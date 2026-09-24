# Stock multiplayer enum compatibility probe

This standalone Unreal Engine 5.5 commandlet uses authored synthetic enums and
the engine's real `FByteProperty::NetSerializeItem`. It does not read, modify or
launch Bodycam. No extracted game assets or generated engine files belong here.

Copy this directory to a short local path, such as `C:\w\enum-compat-probe`, then
run the commands below with an installed Unreal Engine 5.5 and C++ toolchain:

```powershell
& 'C:\Program Files\Epic Games\UE_5.5\Engine\Build\BatchFiles\Build.bat' EnumCompatProbeEditor Win64 Development '-Project=C:\w\enum-compat-probe\EnumCompatProbe.uproject' -WaitMutex -NoHotReloadFromIDE
& 'C:\Program Files\Epic Games\UE_5.5\Engine\Binaries\Win64\UnrealEditor-Cmd.exe' 'C:\w\enum-compat-probe\EnumCompatProbe.uproject' -run=EnumCompatibility -unattended -nullrhi -nop4 -nosplash -stdout -FullStdOutLogOutput
```

Expected result: `ENUM_PROBE RESULT: 72 checks, 0 failures` and exit code 0.
Both legacy and current engine network versions are tested. The old layout is a
negative control that reproduces a following-field mismatch. The corrected
layout round-trips all original mode values between stock and custom peers and
all three custom values between custom peers. Name lookup, friendly lobby names,
safe byte conversion and named-property saving/loading of BB1 are also checked.

The terminal MAX entry aliases BB1's value 15 and remains last, so the first
matching name is BB1. The probe intentionally bypasses the editor-only enum
renumbering helper to model the raw cooked runtime Names array. Generic enum
text export treats MAX as invalid; the inspected Bodycam Blueprint paths use
the name/friendly-name operations tested here. This probe does not establish
compatibility with uninspected native game code or replace actual multiplayer
acceptance testing. A fourth distinct custom mode cannot be added to this
layout without revisiting its network contract.
