"""BASS's DirectX 8 effects as genericMud puts them on a cue: their numbers, their settings as
BASS lays them out, and a stand-in for gargle where BASS has none.

Chatpack puts these on its sounds when ChatMUD asks for echo, reverb, flanger or gargle. BASS
has all nine on Windows; on macOS and Linux it lacks the compressor, gargle and the I3DL2
reverb. Gargle, the one a MUD asks for, is made there from BASS_FX's volume envelope instead:
the sound's volume rises and falls in straight lines (or steps, for a square wave) at the rate
asked, as DirectX's gargle does. The other two have no caller in any pack and stay missing.
"""

from __future__ import annotations

import ctypes
import math
from dataclasses import dataclass

DX8_CHORUS = 0
DX8_COMPRESSOR = 1
DX8_DISTORTION = 2
DX8_ECHO = 3
DX8_FLANGER = 4
DX8_GARGLE = 5
DX8_I3DL2REVERB = 6
DX8_PARAMEQ = 7
DX8_REVERB = 8
BFX_VOLUME_ENV = 0x10012  # BASS_FX's volume envelope
_BFX_ALL_CHANNELS = -1

_FLOAT = ctypes.c_float
_INT = ctypes.c_int
_DWORD = ctypes.c_uint32

# Each effect's settings in BASS's order, by BASS's names (bass.h).
SETTINGS: dict[int, tuple[tuple[str, type], ...]] = {
    DX8_CHORUS: (
        ("fWetDryMix", _FLOAT), ("fDepth", _FLOAT), ("fFeedback", _FLOAT),
        ("fFrequency", _FLOAT), ("lWaveform", _DWORD), ("fDelay", _FLOAT), ("lPhase", _DWORD),
    ),
    DX8_COMPRESSOR: (
        ("fGain", _FLOAT), ("fAttack", _FLOAT), ("fRelease", _FLOAT),
        ("fThreshold", _FLOAT), ("fRatio", _FLOAT), ("fPredelay", _FLOAT),
    ),
    DX8_DISTORTION: (
        ("fGain", _FLOAT), ("fEdge", _FLOAT), ("fPostEQCenterFrequency", _FLOAT),
        ("fPostEQBandwidth", _FLOAT), ("fPreLowpassCutoff", _FLOAT),
    ),
    DX8_ECHO: (
        ("fWetDryMix", _FLOAT), ("fFeedback", _FLOAT), ("fLeftDelay", _FLOAT),
        ("fRightDelay", _FLOAT), ("lPanDelay", _INT),
    ),
    DX8_FLANGER: (
        ("fWetDryMix", _FLOAT), ("fDepth", _FLOAT), ("fFeedback", _FLOAT),
        ("fFrequency", _FLOAT), ("lWaveform", _DWORD), ("fDelay", _FLOAT), ("lPhase", _DWORD),
    ),
    DX8_GARGLE: (("dwRateHz", _DWORD), ("dwWaveShape", _DWORD)),
    DX8_I3DL2REVERB: (
        ("lRoom", _INT), ("lRoomHF", _INT), ("flRoomRolloffFactor", _FLOAT),
        ("flDecayTime", _FLOAT), ("flDecayHFRatio", _FLOAT), ("lReflections", _INT),
        ("flReflectionsDelay", _FLOAT), ("lReverb", _INT), ("flReverbDelay", _FLOAT),
        ("flDiffusion", _FLOAT), ("flDensity", _FLOAT), ("flHFReference", _FLOAT),
    ),
    DX8_PARAMEQ: (("fCenter", _FLOAT), ("fBandwidth", _FLOAT), ("fGain", _FLOAT)),
    DX8_REVERB: (
        ("fInGain", _FLOAT), ("fReverbMix", _FLOAT), ("fReverbTime", _FLOAT),
        ("fHighFreqRTRatio", _FLOAT),
    ),
}

# The C structure BASS reads each effect's settings from.
STRUCTURES: dict[int, type[ctypes.Structure]] = {
    kind: type(f"Dx8Settings{kind}", (ctypes.Structure,), {"_fields_": list(fields)})
    for kind, fields in SETTINGS.items()
}

# DirectX's gargle: 20 times a second, a triangle wave, when a pack doesn't say.
GARGLE_DEFAULTS = {"dwRateHz": 20, "dwWaveShape": 0}
_GARGLE_SLOWEST = 1  # DirectX takes 1 to 1000 Hz
_GARGLE_FASTEST = 1000
_GARGLE_SQUARE = 1
_STEP_SECONDS = 0.0005  # how long a square wave's step from full to silent takes
_MOST_NODES = 100_000  # past this, the rest of a long sound plays without gargling


def read_settings(structure: ctypes.Structure, kind: int) -> dict[str, float]:
    """An effect's settings as a dictionary of BASS's names."""
    return {name: getattr(structure, name) for name, _type in SETTINGS[kind]}


def write_settings(structure: ctypes.Structure, kind: int, settings: dict[str, float]) -> None:
    """Put the settings named into ``structure``, leaving the rest as they are. A value that
    isn't a number is left out; whole-number settings take its whole part."""
    for name, kind_type in SETTINGS[kind]:
        if name not in settings:
            continue
        value = settings[name]
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        setattr(structure, name, int(value) if kind_type is not _FLOAT else float(value))


class _EnvelopeNode(ctypes.Structure):
    """BASS_BFX_ENV_NODE: a point the volume passes through, packed as BASS_FX packs it."""

    _pack_ = 4
    _fields_ = [("pos", ctypes.c_double), ("val", ctypes.c_float)]


class _VolumeEnvelope(ctypes.Structure):
    """BASS_BFX_VOLUME_ENV."""

    _fields_ = [
        ("lChannel", ctypes.c_int),
        ("lNodeCount", ctypes.c_int),
        ("pNodes", ctypes.POINTER(_EnvelopeNode)),
        ("bFollow", ctypes.c_int),
    ]


@dataclass
class GargleEnvelope:
    """BASS_FX's settings for a gargle, and the points they refer to, which have to stay alive
    while BASS reads them."""

    settings: _VolumeEnvelope
    nodes: object


def gargle_envelope(rate_hz: object, wave_shape: object, seconds: float) -> GargleEnvelope:
    """A volume envelope that gargles a sound ``seconds`` long as DirectX does: full to silent
    and back ``rate_hz`` times a second, in straight lines for a triangle wave (shape 0) or in
    steps for a square one (shape 1). It follows the sound's position, so it starts over each
    time a looping sound goes round."""
    rate = int(rate_hz) if isinstance(rate_hz, (int, float)) and math.isfinite(rate_hz) else 0
    rate = min(max(rate, _GARGLE_SLOWEST), _GARGLE_FASTEST)
    square = wave_shape == _GARGLE_SQUARE
    period = 1 / rate
    points_each = 4 if square else 2
    periods = min(math.ceil(max(seconds, period) / period), _MOST_NODES // points_each)
    points: list[tuple[float, float]] = []
    for index in range(periods):
        start = index * period
        middle = start + period / 2
        if square:
            points += [(start, 1.0), (middle - _STEP_SECONDS, 1.0)]
            points += [(middle, 0.0), (start + period - _STEP_SECONDS, 0.0)]
        else:
            points += [(start, 1.0), (middle, 0.0)]
    points.append((periods * period, 1.0))  # and full volume after the last one
    nodes = (_EnvelopeNode * len(points))(*[_EnvelopeNode(at, level) for at, level in points])
    settings = _VolumeEnvelope(
        _BFX_ALL_CHANNELS, len(points), ctypes.cast(nodes, ctypes.POINTER(_EnvelopeNode)), 1
    )
    return GargleEnvelope(settings=settings, nodes=nodes)
