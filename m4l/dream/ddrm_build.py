#!/usr/bin/env python3
"""Build a Deckard's Dream CC controller as an Ableton .amxd device.

VERSION 1.0.0  (2026-08-01)

    python3 ddrm_build.py                  -> DeckardsDream-CC.amxd
    python3 ddrm_build.py --maxpat         -> also write the raw .maxpat JSON
    python3 ddrm_build.py --extract F.amxd -> dump the patcher JSON out of any amxd
    python3 ddrm_build.py --version        -> print the version and exit

EXEMPT FROM SEND ALL
Three controls are deliberately left out of the SEND ALL replay and out of the
values transmitted when the device loads. They stay fully live on the panel --
moving one still sends immediately. Only the unprompted burst skips them. The
set is NO_AUTO_SEND below; each entry is there for a specific reason:

  CC 0    BANK SELECT. Not a sound parameter at all. The manual (p.16) maps
          0 = User Bank 1, 1 = User Bank 2, 2 = Factory, so transmitting it
          changes which bank the synth is on.

  CC 93   COARSE PITCH
  CC 94   FINE PITCH
          Both are centre-detent sliders, and the detent centre is calibrated
          per unit (CALIBRATION -> SLIDERS, p.51). The nominal centre is 64,
          but the value that is actually in tune varies from synth to synth,
          so replaying a nominal number can leave the instrument detuned.
          Set these by ear, and once you know your unit's values, put them in
          INIT_PATCH and delete 93 and 94 from NO_AUTO_SEND -- SEND ALL will
          then restore tuning along with everything else.

Worth knowing if you add to that set: FEET (CC 102 / 103) is a six-position
octave/fifths selector, so a wrong value transposes the synth rather than just
sounding off. It is in the burst, with FEET_VALUE set outright rather than
derived from an assumption about which end of the slider CC 0 corresponds to.

CC chart transcribed from the DECKARD'S DREAM MK2 operation manual v2.3.0
(MIDI CC CHART, pp. 54-56): 80 controllers, of which 26 per layer are the
duplicated layer I / layer II strip and the rest are master or MIDI-only.

The .amxd container format is ported from ktamas77/js2max (src/amxd/writer.ts).
"""
import json
import struct
import sys

# ---------------------------------------------------------------- config
VERSION = "1.0.0"

DEFAULT_VALUE = 64

# Six-zone selectors. The chart gives the band boundaries outright:
#   0-21 / 22-42 / 43-63 / 64-84 / 85-105 / 106-127
# so a position maps to the middle of its band instead of a guessed step.
ZONE_BOUNDS = [(0, 21), (22, 42), (43, 63), (64, 84), (85, 105), (106, 127)]


def zone(i):
    """Value squarely inside band i (0-5) of a six-position selector."""
    lo, hi = ZONE_BOUNDS[i]
    return (lo + hi) // 2


# Value sent for FEET on both layers. FEET is a six-position octave/fifths
# selector (16', 8', 5 1/3', 4', 2 2/3', 2'), and while the chart publishes the
# value bands, it does not say which end of the slider CC 0 corresponds to.
# Computing it from a polarity assumption risks landing on 5 1/3' or 2 2/3',
# which transposes by a fifth, so the value is set outright from what the
# hardware actually does.
FEET_VALUE = 127


# Two-state switches. The chart marks these "0-64 / 65-127".
SWITCH_CCS = {43, 44, 70, 71, 9}
SWITCH_ON = 0            # value sent when a switch is CHECKED
SWITCH_OFF = 127         # value sent when a switch is CLEAR
SWITCH_DEFAULT_ON = 1    # 1 = checked on a fresh device

# Controllers excluded from the load-time output and from the value store that
# SEND ALL replays. They stay fully usable on the panel -- moving one still
# transmits immediately. This only stops them being sent unprompted.
#
#   0   Bank Select. The manual (p.16) documents 0 = User Bank 1,
#       1 = User Bank 2, 2 = Factory. Sending it changes the patch bank.
#   93  Coarse Pitch and 94 Fine Pitch. Both are centre-detent sliders whose
#       centre is calibrated per unit (CALIBRATION -> SLIDERS, p.51), so a
#       nominal 64 is not guaranteed to be in tune on any given synth.
#       Replaying them is what was leaving the DDRM detuned after SEND ALL.
NO_AUTO_SEND = {0, 93, 94}

# 0 keeps the output shut until live.thisdevice reports ready, so the
# parameter-restore burst never reaches the synth. SEND ALL still replays it.
SEND_ON_LOAD = 0

# Milliseconds between messages when SEND ALL replays the panel. Sending the
# whole panel in one scheduler tick hands the synth ~250 bytes with no spacing;
# if its input buffer overruns and drops one, the stream misaligns and value
# bytes start being read as controller numbers.
SEND_INTERVAL_MS = 8

# ---------------------------------------------------------------- parameters
# Paired: (label, full name, CC layer A, CC layer B). Order within each
# section follows the front panel left to right.
VCO = [
    ("SPEED", "PWM Speed",   40, 67),
    ("PWM",   "PWM Amount",  41, 68),
    ("PW",    "Pulse Width", 42, 69),
    ("SQR",   "Square",      43, 71),   # manual: 43 SQUARE A, 71 SQUARE B
    ("SAW",   "Saw",         44, 70),   # manual: 44 SAW A,    70 SAW B
    ("NOISE", "Noise",       45, 72),
]
# The filter envelope is part of the VCF section on the panel, not separate.
VCF = [
    ("HPF",  "VCF HPF",           46, 73),
    ("RESH", "VCF ResH",          47, 119),
    ("LPF",  "VCF LPF",           48, 75),
    ("RESL", "VCF ResL",          49, 76),
    ("IL",   "VCF Initial Level", 50, 77),
    ("AL",   "VCF Attack Level",  51, 78),
    ("A",    "VCF Attack",        52, 79),
    ("D",    "VCF Decay",         53, 80),
    ("R",    "VCF Release",       54, 81),
]
# VCF LEVEL and SINE are the first two controls of the VCA strip: they set
# what the amplifier receives. SINE bypasses the filter (manual p.24).
VCA = [
    ("VCF LVL", "VCF Level",   55, 82),
    ("SINE",    "Sine",        56, 83),
    ("A",       "VCA Attack",  57, 84),
    ("D",       "VCA Decay",   58, 85),
    ("S",       "VCA Sustain", 59, 86),
    ("R",       "VCA Release", 60, 87),
    ("LEVEL",   "VCA Level",   61, 88),
]
TOUCH = [
    ("INI BRIL", "TR Initial Brilliance", 62, 89),
    ("INI LVL",  "TR Initial Level",      63, 90),
    ("AFT BRIL", "TR After Brilliance",   65, 91),
    ("AFT LVL",  "TR After Level",        66, 92),
]

# Global: (label, full name, CC), in panel order
PITCH = [
    ("COARSE",  "Coarse Pitch", 93),
    ("FINE",    "Fine Pitch",   94),
    ("DETUNE",  "Detune CH II", 32),
    ("FEET I",  "Feet I",       102),
    ("FEET II", "Feet II",      103),
]
SUBOSC = [
    ("FUNC",  "Sub Osc Function", 104),
    ("SPEED", "Sub Osc Speed",    105),
    ("VCO",   "Sub Osc to VCO",   106),
    ("VCF",   "Sub Osc to VCF",   107),
    ("VCA",   "Sub Osc to VCA",   108),
]
TONE = [
    ("MIX I/II", "Mix I II",          8),
    ("BRIL",     "Master Brilliance", 109),
    ("RESO",     "Master Resonance",  110),
]
TOUCH_G = [
    ("P BEND", "TR Pitchbend", 111),
    ("SPEED",  "TR Speed",     112),
    ("VCO",    "TR VCO",       113),
    ("VCF",    "TR VCF",       114),
]
KBD = [
    ("BRIL LOW", "Keyboard Brilliance Low",  115),
    ("BRIL HI",  "Keyboard Brilliance High", 116),
    ("LVL LOW",  "Keyboard Level Low",       117),
    ("LVL HI",   "Keyboard Level High",      118),
]
PORT = [
    ("P/G TIME", "Portamento Glissando Time", 5),
    ("SUSTAIN",  "Sustain Slider",            10),
    ("SUS I/II", "Sustain Switch",            9),
]
# Not on the front panel -- CC-only controls from the chart.
PERF = [
    ("MOD WHL", "Mod Wheel",     1),
    ("SUS PED", "Sustain Pedal", 64),
    ("BRILL",   "Brilliance",    74),
    ("BANK",    "Bank Select",   0),
]

# ---------------------------------------------------------------- init patch
# Keyed by parameter name, so one entry covers both layers. A plain, audible,
# responsive patch: filters open, resonance down, fast attacks, sustaining,
# every modulation depth at zero. Musical judgement, except the centre-detent
# and zone values, which come from the manual.
#
# Slider polarity varies by parameter (manual p.18), so "open" is not always
# 127: HPF is open at the bottom, LPF is open at the top, and the sub
# oscillator depths are inactive at the top.
INIT_PATCH = {
    # --- safety ---
    "Mod Wheel": 0,
    "Sustain Pedal": 0,                # >= 64 latches the pedal DOWN
    "Bank Select": 0,                  # also in NO_AUTO_SEND
    "Portamento Glissando Time": 0,    # bottom = barely any glide

    # --- centre detents (CALIBRATION -> SLIDERS, p.51; chart "0-64-127") ---
    "Coarse Pitch": 64,
    "Fine Pitch": 64,
    "Detune CH II": 64,
    "Mix I II": 64,                    # equal blend of layers I and II
    "Master Brilliance": 64,           # centre = no effect on the patch
    "Keyboard Brilliance Low": 64,
    "Keyboard Brilliance High": 64,
    "Keyboard Level Low": 64,
    "Keyboard Level High": 64,

    # --- oscillator ---
    "PWM Speed": 64,
    "PWM Amount": 0,                   # no pulse width modulation
    "Pulse Width": 0,                  # 50%, a true square
    "Noise": 0,

    # --- feet: see FEET_VALUE above ---
    "Feet I": FEET_VALUE,
    "Feet II": FEET_VALUE,

    # --- filter: fully open, no resonance ---
    "VCF HPF": 0,                      # bottom = completely open
    "VCF ResH": 0,
    "VCF LPF": 127,                    # top = completely open
    "VCF ResL": 0,
    "Master Resonance": 0,             # top = no effect
    "Brilliance": 64,

    # --- filter envelope: inactive, IL and AL at 0 have no effect ---
    "VCF Initial Level": 0,
    "VCF Attack Level": 0,
    "VCF Attack": 0,
    "VCF Decay": 64,
    "VCF Release": 64,

    # --- VCA: audible, responsive, sustaining ---
    "VCF Level": 100,
    "Sine": 0,
    "VCA Attack": 0,
    "VCA Decay": 64,
    "VCA Sustain": 127,
    "VCA Release": 40,
    "VCA Level": 100,
    "Sustain Slider": 40,

    # --- touch response and sub oscillator: all depths at zero ---
    "TR Initial Brilliance": 0,
    "TR Initial Level": 0,
    "TR After Brilliance": 0,
    "TR After Level": 0,
    "TR Pitchbend": 0,
    "TR Speed": 0,
    "TR VCO": 0,
    "TR VCF": 0,
    "Sub Osc Function": zone(0),       # sine
    "Sub Osc Speed": 64,
    "Sub Osc to VCO": 0,
    "Sub Osc to VCF": 0,
    "Sub Osc to VCA": 0,
}
# Square, Saw and Sustain Switch are toggles and take SWITCH_DEFAULT_ON.

DEFAULT_OVERRIDES = {}   # filled in from INIT_PATCH once the tables exist

# ---------------------------------------------------------------- geometry
MARGIN, GUTTER = 8, 16
COL_W, GCOL_W = 44, 54
NB_W, GNB_W, NB_H = 36, 44, 16
GAP, GROUP_GAP = 11, 20
HDR_Y, HDR_H = 0, 11
LBL_Y, LBL_H = 16, 11
ROW1_Y, ROW2_Y = 30, 50
G_L1_Y, G_R1_Y, G_L2_Y, G_R2_Y = 14, 25, 43, 54
G_LH = 10
F_HDR, F_LBL, F_GLBL, F_CHAN = 9.0, 8.0, 7.5, 9.0
BLOCK_Y = [4, 80]
DEV_H = 156

boxes, lines = [], []
_n, _pi = [0], [0]


def nid():
    _n[0] += 1
    return "obj-%d" % _n[0]


def add(b):
    boxes.append({"box": b})
    return b["id"]


def link(s, d, so=0, di=0):
    lines.append({"patchline": {"destination": [d, di], "source": [s, so]}})


# Hairline rules. Kept in their own list and spliced in front of the other
# boxes at assembly time, so they are the first thing drawn and cannot end up
# painted over a control. Only bgcolor is set: if a given Max version ignores
# it the panel still draws in its default grey, which is a usable hairline
# either way -- the layout never depends on the colour landing.
rules = []
RULE_COLOR = [0.42, 0.44, 0.47, 0.55]


BLOCK_RULE_H = 70   # header top down through the second row of controls


def vrule(x, y, h=BLOCK_RULE_H, w=1.0):
    """A hairline divider sitting in the gap between two sections."""
    rules.append({"box": {
        "id": nid(), "maxclass": "panel", "mode": 0,
        "numinlets": 1, "numoutlets": 0,
        "patching_rect": [float(x), float(y), float(w), float(h)],
        "presentation": 1,
        "presentation_rect": [float(x), float(y), float(w), float(h)],
        "rounded": 0, "border": 0,
        "bgcolor": RULE_COLOR,
    }})


def comment(x, y, w, h, text, size, just=1):
    return add({
        "id": nid(), "maxclass": "comment", "numinlets": 1, "numoutlets": 0,
        "patching_rect": [float(x), float(y), float(w), float(h)],
        "presentation": 1,
        "presentation_rect": [float(x), float(y), float(w), float(h)],
        "text": text, "fontsize": size, "textjustification": just,
    })


# ------------------------------------------------- expand the init patch
_PAIRED = [VCO, VCF, VCA, TOUCH]
_GLOBAL = [PITCH, SUBOSC, TONE, TOUCH_G, KBD, PORT, PERF]
_by_name = {}
for _t in _PAIRED:
    for _lab, _full, _a, _b in _t:
        _by_name.setdefault(_full, []).extend([_a, _b])
for _t in _GLOBAL:
    for _lab, _full, _cc in _t:
        _by_name.setdefault(_full, []).append(_cc)

_switch_names = {f for t in _PAIRED for _, f, a, b in t
                 if a in SWITCH_CCS or b in SWITCH_CCS}
_switch_names |= {f for t in _GLOBAL for _, f, c in t if c in SWITCH_CCS}
INIT_UNMATCHED = sorted(set(INIT_PATCH) - set(_by_name))
INIT_UNUSED = sorted(n for n in _by_name
                     if n not in INIT_PATCH and n not in _switch_names)
for _name, _val in INIT_PATCH.items():
    for _cc in _by_name.get(_name, []):
        DEFAULT_OVERRIDES[_cc] = _val


def patch_slot():
    i = _pi[0]
    _pi[0] += 1
    return 30 + (i % 6) * 175, 320 + (i // 6) * 84


def cc_tail(srcs, cc, px, py, dy=24.0):
    prep = add({
        "id": nid(), "maxclass": "newobj",
        "numinlets": 2, "numoutlets": 1, "outlettype": [""],
        "patching_rect": [float(px), float(py) + dy, 62.0, 22.0],
        "text": "prepend %d" % cc,
    })
    snd = add({
        "id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 0,
        "patching_rect": [float(px) + 68.0, float(py) + dy, 70.0, 22.0],
        "text": "s ---ddrm.cc",
    })
    for src in srcs:
        link(src, prep)
    link(prep, snd)


def numbox(x, y, w, longname, shortname, cc):
    px, py = patch_slot()
    nb = add({
        "id": nid(), "maxclass": "live.numbox",
        "numinlets": 1, "numoutlets": 2, "outlettype": ["", "float"],
        "parameter_enable": 1,
        "patching_rect": [float(px), float(py), 46.0, 15.0],
        "presentation": 1,
        "presentation_rect": [float(x), float(y), float(w), float(NB_H)],
        "varname": "cc_%d" % cc,
        "saved_attribute_attributes": {
            "valueof": {
                "parameter_longname": longname,
                "parameter_shortname": shortname[:15],
                "parameter_mmin": 0.0,
                "parameter_mmax": 127.0,
                "parameter_type": 1,
                "parameter_unitstyle": 0,
                "parameter_initial_enable": 0 if cc in NO_AUTO_SEND else 1,
                "parameter_initial": [DEFAULT_OVERRIDES.get(cc, DEFAULT_VALUE)],
            }
        },
    })
    cc_tail([nb], cc, px, py)
    return nb


def switch(x, y, w, longname, shortname, cc):
    px, py = patch_slot()
    tg = add({
        "id": nid(), "maxclass": "live.toggle",
        "numinlets": 1, "numoutlets": 1, "outlettype": [""],
        "parameter_enable": 1,
        "patching_rect": [float(px), float(py), 20.0, 20.0],
        "presentation": 1,
        "presentation_rect": [float(x) + (w - 16) / 2, float(y), 16.0, 16.0],
        "varname": "cc_%d" % cc,
        "saved_attribute_attributes": {
            "valueof": {
                "parameter_longname": longname,
                "parameter_shortname": shortname[:15],
                "parameter_initial_enable": 1,
                "parameter_initial": [SWITCH_DEFAULT_ON],
            }
        },
    })
    pick = add({
        "id": nid(), "maxclass": "newobj", "numinlets": 2, "numoutlets": 3,
        "outlettype": ["bang", "bang", ""],
        "patching_rect": [float(px) + 52.0, float(py), 58.0, 22.0],
        "text": "sel 0 1",
    })
    m_off = add({
        "id": nid(), "maxclass": "message", "numinlets": 2, "numoutlets": 1,
        "outlettype": [""],
        "patching_rect": [float(px) + 52.0, float(py) + 26.0, 36.0, 22.0],
        "text": str(SWITCH_OFF),
    })
    m_on = add({
        "id": nid(), "maxclass": "message", "numinlets": 2, "numoutlets": 1,
        "outlettype": [""],
        "patching_rect": [float(px) + 96.0, float(py) + 26.0, 36.0, 22.0],
        "text": str(SWITCH_ON),
    })
    link(tg, pick)
    link(pick, m_off, 0, 0)
    link(pick, m_on, 1, 0)
    cc_tail([m_off, m_on], cc, px, py, dy=52.0)
    return tg


def control(x, y, w, longname, shortname, cc):
    if cc in SWITCH_CCS:
        return switch(x, y, w, longname, shortname, cc)
    return numbox(x, y, w, longname, shortname, cc)


def section(x, y, title, params, paired, glob=False):
    cw = GCOL_W if glob else COL_W
    cols = len(params) if paired else (len(params) + 1) // 2
    w = cols * cw
    comment(x, y + HDR_Y, w, HDR_H, title, F_HDR, just=0)
    if paired:
        for i, (lab, full, ccA, ccB) in enumerate(params):
            cx = x + i * cw
            comment(cx, y + LBL_Y, cw, LBL_H, lab, F_LBL)
            control(cx + (cw - NB_W) / 2, y + ROW1_Y, NB_W,
                    "I " + full, lab, ccA)
            control(cx + (cw - NB_W) / 2, y + ROW2_Y, NB_W,
                    "II " + full, lab, ccB)
    else:
        for i, (lab, full, cc) in enumerate(params):
            col, row = i % cols, i // cols
            cx = x + col * cw
            comment(cx, y + (G_L1_Y if row == 0 else G_L2_Y), cw, G_LH,
                    lab, F_GLBL)
            control(cx + (cw - GNB_W) / 2,
                    y + (G_R1_Y if row == 0 else G_R2_Y), GNB_W, full, lab, cc)
    return w


# ---------------------------------------------------------------- panel
# Two blocks, layer I above layer II within each, mirroring how the hardware
# stacks the two identical layer strips. Each entry is
# (title, params, paired, global, gap after) and a divider is dropped into the
# middle of every gap.
# Top: the per-layer strip. On the hardware, layer I and layer II are two
# identical rows of 26 controls, so the whole strip lives in one block with
# the layers stacked.
LAYER_BLOCK = [
    ("VCO",            VCO,   True, False, GAP),
    ("VCF",            VCF,   True, False, GAP),
    ("VCA",            VCA,   True, False, GAP),
    ("TOUCH RESPONSE", TOUCH, True, False, 0),
]
# Bottom: the master section, in panel order left to right.
MASTER_BLOCK = [
    ("PITCH",          PITCH,   False, True, GAP),
    ("SUB OSCILLATOR", SUBOSC,  False, True, GAP),
    ("TONE",           TONE,    False, True, GAP),
    ("TOUCH RESPONSE", TOUCH_G, False, True, GAP),
    ("KEYBOARD CTRL",  KBD,     False, True, GAP),
    ("PORT / GLISS",   PORT,    False, True, GAP),
    ("MIDI",           PERF,    False, True, 0),
]


def build_block(by, spec, layered):
    if layered:
        comment(MARGIN, by + ROW1_Y + 2, GUTTER - 2, 12, "I", F_CHAN)
        comment(MARGIN, by + ROW2_Y + 2, GUTTER - 2, 12, "II", F_CHAN)
    x = MARGIN + GUTTER
    for title, params, paired, glob, gap in spec:
        x += section(x, by, title, params, paired, glob)
        if gap:
            vrule(x + gap / 2.0, by + HDR_Y)
            x += gap
    return x


right1 = build_block(BLOCK_Y[0], LAYER_BLOCK, True)
right2 = build_block(BLOCK_Y[1], MASTER_BLOCK, False)

# SEND ALL sits beside MIDI in the master row, on the same grid as the
# sections around it, so the width is set by the layer strip alone.
CTRL_W = 76
vrule(right2 + GROUP_GAP / 2.0, BLOCK_Y[1] + HDR_Y)
CTRL_X = right2 + GROUP_GAP
DEV_W = max(right1, CTRL_X + CTRL_W) + MARGIN

# ---------------------------------------------------------------- MIDI engine
EY = 60
recv = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 1,
            "outlettype": [""], "patching_rect": [30.0, float(EY), 90.0, 22.0],
            "text": "r ---ddrm.cc"})
fan = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 2,
           "outlettype": ["", ""],
           "patching_rect": [30.0, float(EY + 16), 60.0, 22.0],
           "text": "t l l"})
unp = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 2,
           "outlettype": ["int", "int"],
           "patching_rect": [30.0, float(EY + 76), 70.0, 22.0],
           "text": "unpack 0 0"})
trig = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 3,
            "outlettype": ["bang", "int", "bang"],
            "patching_rect": [30.0, float(EY + 108), 78.0, 22.0],
            "text": "t b i b"})
val = add({"id": nid(), "maxclass": "newobj", "numinlets": 2, "numoutlets": 1,
           "outlettype": ["int"],
           "patching_rect": [30.0, float(EY + 140), 36.0, 22.0], "text": "int"})
stat = add({"id": nid(), "maxclass": "message", "numinlets": 2, "numoutlets": 1,
            "outlettype": [""],
            "patching_rect": [160.0, float(EY + 140), 36.0, 22.0],
            "text": "176"})
mout = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 0,
            "patching_rect": [30.0, float(EY + 180), 58.0, 22.0],
            "text": "midiout"})
min_ = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 1,
            "outlettype": ["int"],
            "patching_rect": [300.0, float(EY + 76), 52.0, 22.0],
            "text": "midiin"})

# ---- SEND ALL, paced ----
ALL_CCS = {int(b["box"]["varname"].split("_")[1]) for b in boxes
           if b["box"]["maxclass"] in ("live.numbox", "live.toggle")}
LAST_STORED_CC = max(ALL_CCS - NO_AUTO_SEND)

guard = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 2,
             "outlettype": ["", ""],
             "patching_rect": [430.0, float(EY + 140), 150.0, 22.0],
             "text": "route " + " ".join(str(c) for c in sorted(NO_AUTO_SEND))})
keep = add({"id": nid(), "maxclass": "message", "numinlets": 2, "numoutlets": 1,
            "outlettype": [""],
            "patching_rect": [430.0, float(EY + 172), 116.0, 22.0],
            "text": "store $1 $1 $2"})
store = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 4,
             "outlettype": ["", "", "", "bang"],
             "patching_rect": [430.0, float(EY + 204), 60.0, 22.0],
             "text": "coll"})
btn = add({"id": nid(), "maxclass": "button",
           "numinlets": 1, "numoutlets": 1, "outlettype": ["bang"],
           "patching_rect": [430.0, float(EY), 24.0, 24.0],
           "presentation": 1,
           "presentation_rect": [float(CTRL_X + (CTRL_W - 20) / 2),
                                 float(BLOCK_Y[1] + G_R1_Y), 20.0, 20.0]})
comment(CTRL_X, BLOCK_Y[1] + HDR_Y, CTRL_W, HDR_H, "SEND ALL", F_HDR, just=0)
comment(CTRL_X, BLOCK_Y[1] + G_R2_Y, CTRL_W, G_LH, "v" + VERSION, 7.5)

start = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 2,
             "outlettype": ["bang", "bang"],
             "patching_rect": [430.0, float(EY + 28), 60.0, 22.0],
             "text": "t b b"})
rewind = add({"id": nid(), "maxclass": "message", "numinlets": 2,
              "numoutlets": 1, "outlettype": [""],
              "patching_rect": [530.0, float(EY + 56), 56.0, 22.0],
              "text": "goto 0"})
run = add({"id": nid(), "maxclass": "message", "numinlets": 2, "numoutlets": 1,
           "outlettype": [""],
           "patching_rect": [430.0, float(EY + 56), 28.0, 22.0], "text": "1"})
tick = add({"id": nid(), "maxclass": "newobj", "numinlets": 2, "numoutlets": 1,
            "outlettype": ["bang"],
            "patching_rect": [430.0, float(EY + 84), 74.0, 22.0],
            "text": "metro %d" % SEND_INTERVAL_MS})
step = add({"id": nid(), "maxclass": "message", "numinlets": 2, "numoutlets": 1,
            "outlettype": [""],
            "patching_rect": [430.0, float(EY + 112), 48.0, 22.0],
            "text": "next"})
done = add({"id": nid(), "maxclass": "newobj", "numinlets": 2, "numoutlets": 2,
            "outlettype": ["bang", ""],
            "patching_rect": [620.0, float(EY + 232), 70.0, 22.0],
            "text": "sel %d" % LAST_STORED_CC})
halt = add({"id": nid(), "maxclass": "message", "numinlets": 2, "numoutlets": 1,
            "outlettype": [""],
            "patching_rect": [620.0, float(EY + 260), 28.0, 22.0], "text": "0"})

link(btn, start)
link(start, rewind, 1, 0)      # right outlet first: rewind before starting
link(rewind, store)
link(start, run, 0, 0)
link(run, tick, 0, 0)
link(tick, step)
link(step, store)
link(store, unp, 0, 0)         # outlet 0: the stored (cc, value) pair
link(store, done, 1, 0)        # outlet 1: the address just walked
link(done, halt, 0, 0)
link(halt, tick, 0, 0)
link(fan, guard, 1, 0)
link(guard, keep, len(NO_AUTO_SEND), 0)
link(keep, store)

# ---- passthrough, control change deliberately not forwarded ----
# The DDRM replicates messages it receives back out, so forwarding CC would
# turn that echo into a permanent MIDI feedback loop.
parse = add({"id": nid(), "maxclass": "newobj", "numinlets": 1, "numoutlets": 7,
             "outlettype": ["list", "list", "list", "int", "int", "int", "int"],
             "patching_rect": [300.0, float(EY + 108), 84.0, 22.0],
             "text": "midiparse"})
fmt = add({"id": nid(), "maxclass": "newobj", "numinlets": 7, "numoutlets": 1,
           "outlettype": ["int"],
           "patching_rect": [300.0, float(EY + 140), 180.0, 22.0],
           "text": "midiformat"})
link(min_, parse)
for ch in (0, 1, 3, 4, 5, 6):
    link(parse, fmt, ch, ch)
link(fmt, mout)

link(recv, fan)
if SEND_ON_LOAD:
    link(fan, unp, 0, 0)
else:
    ready = add({"id": nid(), "maxclass": "newobj",
                 "numinlets": 1, "numoutlets": 3,
                 "outlettype": ["bang", "", ""],
                 "patching_rect": [700.0, float(EY), 108.0, 22.0],
                 "text": "live.thisdevice"})
    settle = add({"id": nid(), "maxclass": "newobj",
                  "numinlets": 2, "numoutlets": 1, "outlettype": ["bang"],
                  "patching_rect": [700.0, float(EY + 28), 54.0, 22.0],
                  "text": "del 150"})
    openm = add({"id": nid(), "maxclass": "message",
                 "numinlets": 2, "numoutlets": 1, "outlettype": [""],
                 "patching_rect": [700.0, float(EY + 56), 28.0, 22.0],
                 "text": "1"})
    hold = add({"id": nid(), "maxclass": "newobj",
                "numinlets": 2, "numoutlets": 1, "outlettype": [""],
                "patching_rect": [30.0, float(EY + 44), 54.0, 22.0],
                "text": "gate"})
    link(ready, settle, 0, 0)
    link(settle, openm)
    link(openm, hold, 0, 0)
    link(fan, hold, 0, 1)
    link(hold, unp)

link(unp, trig, 0, 0)
link(unp, val, 1, 1)
link(trig, stat, 2, 0)
link(trig, mout, 1, 0)
link(trig, val, 0, 0)
link(stat, mout)
link(val, mout)

# ---------------------------------------------------------------- amxd writer
DEVICE_TYPE = {"audio-effect": b"aaaa", "midi-effect": b"mmmm",
               "instrument": b"iiii"}


def be32(v):
    return struct.pack(">I", v)


def le32(v):
    return struct.pack("<I", v)


def tlv(tag, data):
    return tag.encode("ascii") + be32(8 + len(data)) + data


def tlv_u32(tag, v):
    return tlv(tag, be32(v))


def tlv_str(tag, s):
    e = s.encode("ascii")
    return tlv(tag, e + b"\x00" * ((4 - len(e) % 4) % 4))


def make_dlst(filename, n):
    dire = (tlv_str("type", "JSON") + tlv_str("fnam", filename)
            + tlv_u32("sz32", n + 2) + tlv_u32("of32", 16)
            + tlv_u32("vers", 0) + tlv_u32("flag", 0x11)
            + tlv_u32("mdat", 0))
    return tlv("dlst", tlv("dire", dire))


def build_amxd(patch, device_type, filename):
    body = json.dumps(patch, indent="\t").encode("utf-8")
    n = len(body)
    ptch = (b"mx@c" + be32(16) + be32(0) + be32(n + 2 + 16)
            + body + b"\x0a\x00" + make_dlst(filename, n))
    return (b"ampf" + le32(4) + DEVICE_TYPE[device_type]
            + b"meta" + le32(4) + le32(7)
            + b"ptch" + le32(len(ptch)) + ptch)


def extract(path):
    buf = open(path, "rb").read()
    if buf[0:4] != b"ampf":
        raise SystemExit("not an amxd file (missing ampf magic)")
    ptch = buf[32:32 + struct.unpack("<I", buf[28:32])[0]]
    return buf[8:12].decode(), json.loads(
        ptch[16:ptch.rfind(b"\x0a\x00")].decode("utf-8"))


patcher = {"patcher": {
    "fileversion": 1,
    "appversion": {"major": 8, "minor": 6, "revision": 2,
                   "architecture": "x64", "modernui": 1},
    "classnamespace": "box",
    "rect": [60.0, 90.0, float(DEV_W), float(DEV_H)],
    "bglocked": 0,
    "openinpresentation": 1,
    "default_fontsize": 12.0,
    "default_fontface": 0,
    "default_fontname": "Arial",
    "gridonopen": 1,
    "gridsize": [5.0, 5.0],
    "gridsnaponopen": 1,
    "objectsnaponopen": 1,
    "statusbarvisible": 2,
    "toolbarvisible": 1,
    "lefttoolbarpinned": 0,
    "toptoolbarpinned": 0,
    "righttoolbarpinned": 0,
    "bottomtoolbarpinned": 0,
    "toolbars_unpinned_last_save": 0,
    "tallnewobj": 0,
    "boxanimatetime": 200,
    "enablehscroll": 1,
    "enablevscroll": 1,
    "devicewidth": float(DEV_W),
    "description": "Deckard's Dream CC controller v%s" % VERSION,
    "digest": "",
    "tags": "",
    "style": "",
    "subpatcher_template": "",
    "assistshowspatchername": 0,
    "boxes": rules + boxes,
    "lines": lines,
    "dependency_cache": [],
    "autosave": 0,
}}

if __name__ == "__main__":
    if "--version" in sys.argv:
        print("ddrm_build.py %s" % VERSION)
        sys.exit()

    if "--extract" in sys.argv:
        dtype, p = extract(sys.argv[sys.argv.index("--extract") + 1])
        print("device type:", dtype, file=sys.stderr)
        print(json.dumps(p, indent="\t"))
        sys.exit()

    if INIT_UNMATCHED:
        print("WARNING: INIT_PATCH names matching no parameter:",
              ", ".join(INIT_UNMATCHED), file=sys.stderr)
    if INIT_UNUSED:
        print("NOTE: still on the DEFAULT_VALUE fallback:",
              ", ".join(INIT_UNUSED), file=sys.stderr)
    name = "DeckardsDream-CC.amxd"
    open(name, "wb").write(build_amxd(patcher, "midi-effect", name))
    print("wrote %s  v%s  (%d x %d, %d parameters)"
          % (name, VERSION, DEV_W, DEV_H, len(ALL_CCS)))
    if "--maxpat" in sys.argv:
        json.dump(patcher, open("DeckardsDream-CC.maxpat", "w"), indent="\t")
        print("wrote DeckardsDream-CC.maxpat")
