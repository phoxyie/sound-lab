"""DSP chain presets (ffmpeg -af syntax)."""

# Scenario A: after DeepFilterNet3 — body, boxiness cut, presence, air, de-esser, compressor
VOICE_A = ",".join([
    "highpass=f=85:poles=2",
    "equalizer=f=140:t=q:w=0.9:g=3.0",
    "equalizer=f=380:t=q:w=1.4:g=-3.0",
    "equalizer=f=3500:t=q:w=1.2:g=1.5",
    "treble=g=2.0:f=10000:t=q:w=0.7",
    "deesser=i=0.35:m=0.5:f=0.5:s=o",
    "acompressor=threshold=-20dB:ratio=2.8:attack=15:release=120:knee=4dB:makeup=3dB",
])

# Scenario B (after Resemble Enhance) — softer EQ, stronger de-esser
VOICE_B = ",".join([
    "highpass=f=80",
    "equalizer=f=150:t=q:w=1.0:g=2.5",
    "equalizer=f=420:t=q:w=1.5:g=-2.5",
    "deesser=i=0.4:m=0.5:f=0.5:s=o",
    "treble=g=1.5:f=10000:t=q:w=0.7",
    "acompressor=threshold=-18dB:ratio=2.5:attack=20:release=120:makeup=2dB",
])

# Light touch for quiet rooms — small EQ, gentle compressor
VOICE_LIGHT = ",".join([
    "highpass=f=80:poles=2",
    "equalizer=f=300:t=q:w=1.2:g=-2.0",
    "equalizer=f=4000:t=q:w=1.0:g=1.5",
    "deesser=i=0.2:m=0.5:f=0.5:s=o",
    "acompressor=threshold=-24dB:ratio=2:attack=20:release=200:knee=6dB",
])

# No DSP — denoise + loudness only (A/B comparison reference)
FLAT = "anull"

PRESETS = {"voice-a": VOICE_A, "voice-b": VOICE_B, "voice-light": VOICE_LIGHT, "flat": FLAT}

# ffmpeg-native denoiser used when deep-filter isn't found
AFFTDN_FALLBACK = "afftdn=nr=12:nf=-40:tn=1"
