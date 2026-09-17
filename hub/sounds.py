"""The hub's one and only sound: a short alert when a match is found.

WHY IT IS SYNTHESISED RATHER THAN THE GAME'S OWN CUE (2026-09-14). Sam asked for the tick you
hear while an ally captures the hardpoint, three times over. Two things stand in the way of
literally using it, and both are worth writing down rather than quietly working around:

  * It is Reissad Studio's audio. The gamemode paks ship only our own content and reference the
    game's assets where they already sit on the player's disk; putting a game sound INSIDE a hub
    exe that we host and hand out is redistribution, which is a different thing entirely and not
    one an explicitly unofficial community tool should be doing.
  * Cooked UE5 sound on Windows is usually Bink Audio, which needs RAD's proprietary decoder.
    There is no pure-Python route to a playable WAV.

The honest version of what Sam wants - a short, dry, three-beat cue that reads as "your match is
up" - is a few lines of arithmetic, owes nobody anything, and adds no dependency. If he does want
the real cue later, the defensible route is reading it out of HIS OWN install at runtime
(`/Game/UI/Sfx/Cues/Simple_Click_Sound_HardPoint_Cue`, `/Game/Audio/UI/Bodycam_UI_Hardpoint_*`)
and never shipping it.

PLATFORM: `winsound` is Windows-only and is in the standard library, so this costs the exe
nothing. Everywhere else (the test machines) every function here is a no-op that returns False.

VOLUME: `winsound.PlaySound` has no volume control at all - it plays a file at whatever level the
file is at. So the slider cannot be applied at playback and has to be baked into the samples, which
means one cached WAV per level. They are 13 KB each and written in a blink, and `ensure_cue` sweeps
the old ones, so the state dir never collects more than one.
"""
import hashlib
import math
import struct
import sys
import wave

from . import paths

WINDOWS = sys.platform == "win32"

SAMPLE_RATE = 22050
BEEPS = 1                 # one ring (Sam, 2026-09-15: "reduce the match found sound to its
                          # base level instead of 3x"). It was 3 - "3 instances of the sound",
                          # 2026-09-14 - which at the accept window is three rings over 900 ms.

# The soft bell (Sam chose it from six candidates, 2026-09-14). The partial at 2.76x the
# fundamental is what makes it read as a BELL rather than a beep: a real bell's overtones are
# inharmonic, so a whole-number multiple would just sound like a louder sine. Long decay and a
# near-zero gap mean the three rings overlap into one phrase instead of three separate pings,
# which is what makes it bearable on the twentieth queue of an evening.
BEEP_GAP_MS = 300         # between the START of one ring and the next, so they run back to back
BEEP_MS = 300
FREQ_HZ = 880.0           # A5
HARMONIC = 0.45
HARMONIC_MULT = 2.76      # inharmonic on purpose
ATTACK_MS = 2.0
DECAY = 9.0               # exponential; low, so it rings rather than clicks

# LOUDNESS (Sam: "make it louder"). Two levers, because a bell is quieter than its peak suggests:
# most of its energy is in the first few milliseconds and the tail is nearly silent, so simply
# scaling it up runs out of headroom long before it sounds loud.
#   DRIVE  pushes the signal into a tanh saturator first, which lifts the BODY of the ring
#          towards the peak instead of just scaling everything. On a bell this reads as warmth
#          and presence rather than distortion, because the partials it adds are already there.
#   PEAK   how close to full scale the result is normalised. Left just under 1.0 so nothing
#          clips on playback.
# Past roughly DRIVE 3 the attack starts to buzz; 2.2 is loud without sounding broken.
DRIVE = 2.2
PEAK = 0.96

# Where the slider starts for somebody who has never touched it. Halved from 70 (Sam,
# 2026-09-16: "lower the default match found sound by 50%") - the slider scales the samples
# linearly, so 35 is literally half the amplitude of the old default. Anyone who has already
# moved the slider keeps their own level: state.comp_sound_volume is only absent for a fresh
# install, and volume_for_state() only falls through to this when it is None.
DEFAULT_VOLUME = 35


def clamp_volume(value) -> int:
    """The slider's value as a whole percent, 0 (silent) to 100."""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME


def volume_for_state(state) -> int:
    """The cue's level for a hub state dict, 0 (muted) to 100.

    THE ONE RULE, in one place: never set means the DEFAULT, not silence. `None` rather than a
    missing key is what "never set" looks like - state.load() gives every key it knows about, so
    the test has to be against the VALUE. It cannot be a truth test either: 0 is a real answer,
    and a player who muted the cue must stay muted across restarts.

    Three callers used to spell this out separately (the Tk panel, the Settings screen and - once
    the web UI grew the cue - the web panel), which is exactly how the web UI ended up with a
    working Test button and no sound on a real match."""
    state = state or {}
    if state.get("comp_sound_volume") is not None:
        return clamp_volume(state.get("comp_sound_volume"))
    # a hub that predates the slider carried a plain on/off flag; honour it once
    if state.get("comp_sound") is False:
        return 0
    return DEFAULT_VOLUME


def _samples(volume=100):
    """One ring: fast attack, exponential decay, one inharmonic partial, then driven and
    normalised so it is actually audible over a firefight."""
    n = int(SAMPLE_RATE * BEEP_MS / 1000.0)
    attack = max(1, int(SAMPLE_RATE * ATTACK_MS / 1000.0))
    raw = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = (i / attack) if i < attack else math.exp(-DECAY * (t - ATTACK_MS / 1000.0))
        wave_ = (math.sin(2 * math.pi * FREQ_HZ * t)
                 + HARMONIC * math.sin(2 * math.pi * FREQ_HZ * HARMONIC_MULT * t))
        raw.append(math.tanh(DRIVE * env * wave_ / (1 + HARMONIC)))
    loudest = max(1e-9, max(abs(v) for v in raw))
    scale = PEAK * clamp_volume(volume) / 100.0
    return [int(max(-1.0, min(1.0, v / loudest * scale)) * 32000) for v in raw]


def write_wav(path, volume=100):
    """Write the cue to `path`. Returns the path; raises only on a real IO failure."""
    frames = _samples(volume)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(struct.pack("<%dh" % len(frames), *frames))
    return path


def _cue_id(volume=100):
    """A short digest of everything that shapes the sound.

    It is in the FILENAME so that changing the cue actually reaches people: ensure_cue() only
    writes a file that is missing, so a fixed name would leave every existing install playing
    the old sound forever."""
    spec = "|".join(str(x) for x in (SAMPLE_RATE, BEEP_MS, FREQ_HZ, HARMONIC, HARMONIC_MULT,
                                     ATTACK_MS, DECAY, DRIVE, PEAK, clamp_volume(volume)))
    return hashlib.sha256(spec.encode()).hexdigest()[:8]


CUE_PREFIX = "match-found-"


def cue_path(volume=100):
    """Where the cue lives. Generated into the hub's own state dir rather than bundled, so
    PyInstaller has one less data file to get wrong."""
    return paths.state_dir() / ("%s%s.wav" % (CUE_PREFIX, _cue_id(volume)))


def ensure_cue(volume=100):
    path = cue_path(volume)
    try:
        if not path.exists() or path.stat().st_size < 1000:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(path, volume)
            # one level per file, so sweep the levels the player has moved off
            for old in path.parent.glob(CUE_PREFIX + "*.wav"):
                if old != path:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        return path
    except Exception:            # noqa: BLE001 - a read-only state dir must never break the tab
        return None


def play_once(volume=100):
    """One ring, asynchronous. False when there is no sound to play or no way to play it."""
    if not WINDOWS or clamp_volume(volume) <= 0:
        return False
    path = ensure_cue(volume)
    if path is None:
        return False
    try:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return True
    except Exception:            # noqa: BLE001 - no audio device, or a locked file
        return False


def play_match_found(schedule, volume=100):
    """The full cue: BEEPS rings, spaced, never blocking the UI thread.

    `schedule(ms, fn)` is the panel's own after(), so the repeats are cancelled with the panel
    and nothing here ever touches tkinter itself."""
    volume = clamp_volume(volume)
    if not play_once(volume):
        return False
    for i in range(1, BEEPS):
        schedule(BEEP_GAP_MS * i, lambda v=volume: play_once(v))
    return True
