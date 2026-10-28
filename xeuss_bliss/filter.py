"""Attenuating filter of the Xeuss 2.0 (xeuss20), motor `fil`, ported from SPEC's filter.mac.

    filter_in()      fil to FILTER_IN  (20 mm): beam attenuated
    filter_out()     fil to FILTER_OUT (0 mm): full beam
    filter_state()   "in", "out" or the position if it is neither

Positions from SPEC's xenocs-settings.mac (XNSET_POS["fil"]["IN"] = 20, ["OUT"] = 0, HAS_FILTER = 1).
SPEC's multiexp took the filter out for exposures and put it back in afterwards, so "in" is the
resting state that protects the detector.
"""

from bliss.common.standard import move
from bliss.config.static import get_config

AXIS = "fil"
FILTER_IN = 20.0   # mm
FILTER_OUT = 0.0   # mm
TOLERANCE = 0.01   # mm


def _fil():
    return get_config().get(AXIS)


def filter_state():
    """Return "in", "out", or the fil position if it is at neither."""
    pos = _fil().position
    if abs(pos - FILTER_IN) < TOLERANCE:
        return "in"
    if abs(pos - FILTER_OUT) < TOLERANCE:
        return "out"
    return pos


def filter_in():
    """Put the attenuating filter into the beam (fil to 20 mm)."""
    move(_fil(), FILTER_IN)
    print("filter in")


def filter_out():
    """Take the attenuating filter out of the beam (fil to 0 mm)."""
    move(_fil(), FILTER_OUT)
    print("filter out")
