"""Beamstop in/out for the Xeuss 2.0 (xeuss20): move the beamstop sideways out of the beam.

    beamstop_out()    filter IN, then remember the current bsx position as "in" and move bsx by
                      OUT_SHIFT (-10 mm): the direct beam reaches the detector attenuated
    beamstop_in()     bsx back to the remembered "in" position, then filter OUT
    beamstop_state()  "in" / "out", the remembered position and the filter state
    take_direct_beam(t)  beamstop out (filter in), sct(t), beamstop back as before

Beamstop and filter are paired (user, 2026-10-20). Order for the detector's safety: the filter goes in
BEFORE the beamstop leaves, and comes out only AFTER the beamstop is back. Both functions also set the
filter when the beamstop is already where it should be.

The "in" position is wherever bsx is when beamstop_out() is called, so after re-aligning the
beamstop the next beamstop_out() remembers the new position. beamstop_out() while already out
does nothing (it must not overwrite the remembered position). bsz is not moved.
The state is kept in the BLISS settings database (Redis), so it survives a session restart.
Replaces the users' SPEC macros beamstop_in/beamstop_out (~/SPEC-macros, "umv bsz 0 / -10");
the user preferred moving out in x (2026-10-20).
"""

from bliss.common.standard import move
from bliss.config.settings import HashSetting
from bliss.config.static import get_config

from xeuss_bliss.filter import filter_in, filter_out, filter_state

AXIS = "bsx"
OUT_SHIFT = -10.0  # mm, same side as Xenocs' take_direct_beam (umvr bsx -7)
_state_setting = None


def _state():
    """Settings in Redis, created on first use: keys "state" ("in"/"out"), "in_position" (bsx)."""
    global _state_setting
    if _state_setting is None:
        _state_setting = HashSetting("xeuss:beamstop")
    return _state_setting


def _axis():
    return get_config().get(AXIS)


def beamstop_state():
    """Return ("in" or "out", remembered in-position of bsx or None, filter state)."""
    state = _state().get("state") or "in"  # nothing stored yet: assume in
    pos = _state().get("in_position")
    return state, (None if pos in (None, "", "None") else float(pos)), filter_state()


def beamstop_out():
    """Filter in, then the beamstop out of the beam (bsx by OUT_SHIFT, in-position remembered)."""
    state, in_pos, _ = beamstop_state()
    bsx = _axis()
    filter_in()  # FIRST: the detector must never see the unattenuated direct beam
    if state == "out":
        print(f"beamstop already out (bsx {bsx.position:.4f}, in-position {in_pos:.4f}) - not moved")
        return
    in_pos = bsx.position
    _state()["in_position"] = in_pos
    # "out" BEFORE moving: if the move is interrupted, a second beamstop_out() must not take the
    # half-way position as the new in-position, and beamstop_in() still knows where to go back
    _state()["state"] = "out"
    move(bsx, in_pos + OUT_SHIFT)
    print(f"beamstop OUT: bsx {in_pos:.4f} -> {bsx.position:.4f} (in-position remembered)")


def beamstop_in():
    """Beamstop back into the beam (bsx to the remembered in-position), then filter out."""
    state, in_pos, _ = beamstop_state()
    bsx = _axis()
    if state == "in":
        print(f"beamstop already in (bsx {bsx.position:.4f}) - not moved")
    else:
        if in_pos is None:
            raise RuntimeError("no remembered in-position for the beamstop - move bsx by hand")
        move(bsx, in_pos)
        _state()["state"] = "in"
        print(f"beamstop IN: bsx {bsx.position:.4f}")
    filter_out()  # only AFTER the beamstop covers the direct beam again


def take_direct_beam(count_time=0.1):
    """Image of the attenuated direct beam (e.g. for transmission): beamstop out (filter in), one saved
    count (sct, default measurement group: NI counters + Pilatus image), then the beamstop back as it
    was (in -> filter out). The shutter is opened/closed by the automatic shutter of the scan.
    Replaces Xenocs' take_direct_beam (SPEC: bsx -7, 0.1 s Pilatus exposure). Returns the scan."""
    from bliss.common.scans import sct

    state_before = beamstop_state()[0]
    beamstop_out()
    try:
        # own title: BLISS names every sct "ct <time> 1" in the data file, which looks like an unsaved ct
        return sct(count_time, title=f"take_direct_beam {count_time:g}")
    finally:
        if state_before == "in":
            beamstop_in()
