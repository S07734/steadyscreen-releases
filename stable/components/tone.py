#!/usr/bin/env python3
"""Write the SteadyScreen test tone to stdout as a WAV.

Pure standard library on purpose: a client has python3 and nothing else
guaranteed, and a test tone that needs a package installed is a test that
fails for the wrong reason.

The sound: a fast rising sweep, then a two-note chime an octave apart with a
little detune so it shimmers, over a soft sub-octave body. Stereo, with the
voices offset slightly left and right so it reads as wide rather than flat.
"""
import math, struct, sys

RATE = 44100
DUR = 1.45

def env_attack_decay(t, attack, decay, hold=0.0):
    if t < attack:
        return t / attack
    t -= attack
    if t < hold:
        return 1.0
    t -= hold
    return math.exp(-t / decay)

def sample(t):
    left = right = 0.0

    # 1. the sweep: 180 Hz up to 1400 Hz in a third of a second, quiet and
    #    quick, so it reads as a run-up rather than a siren
    if t < 0.34:
        k = t / 0.34
        f = 180.0 * math.exp(k * math.log(1400.0 / 180.0))
        a = 0.30 * math.sin(math.pi * k) ** 1.5          # fades in and out
        ph = 2 * math.pi * f * t
        left += a * math.sin(ph)
        right += a * math.sin(ph + 0.35)                  # slight width

    # 2. the chime, once the sweep has cleared
    ct = t - 0.30
    if ct > 0:
        e = env_attack_decay(ct, 0.012, 0.42)
        for f, amp, pan in ((880.0, 0.30, -1.0),          # A5
                            (1318.5, 0.22, 1.0),          # E6
                            (1760.0, 0.10, 0.0)):         # A6 sparkle
            det = 1.0 + 0.0016 * pan                      # detune for shimmer
            v = amp * e * math.sin(2 * math.pi * f * det * ct)
            left += v * (0.5 - 0.32 * pan)
            right += v * (0.5 + 0.32 * pan)

        # body underneath, so it is not all treble on a small panel speaker
        b = 0.16 * env_attack_decay(ct, 0.02, 0.30) * math.sin(2 * math.pi * 220.0 * ct)
        left += b
        right += b

    return left, right

REPEATS = 3
GAP = 0.22          # silence between repeats, in seconds


def main():
    frames = int(RATE * DUR)
    hit = bytearray()
    for i in range(frames):
        t = i / RATE
        l, r = sample(t)
        # a short fade at the very end kills the click a hard stop makes
        if i > frames - 400:
            f = (frames - i) / 400.0
            l *= f; r *= f
        hit += struct.pack("<hh",
                           max(-32767, min(32767, int(l * 26000))),
                           max(-32767, min(32767, int(r * 26000))))

    # Three times, with the gap baked in. Playing the file three times from the
    # shell would reopen the device between each, and the pause would be
    # whatever the driver felt like rather than a chosen rhythm.
    gap = bytes(int(RATE * GAP) * 4)
    data = bytearray()
    for n in range(REPEATS):
        data += hit
        if n != REPEATS - 1:
            data += gap

    out = sys.stdout.buffer
    out.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt ")
    out.write(struct.pack("<IHHIIHH", 16, 1, 2, RATE, RATE * 4, 4, 16))
    out.write(b"data" + struct.pack("<I", len(data)))
    out.write(bytes(data))

main()
