"""Slit commands for the Xeuss 2.0 (xeuss20), ported from the Xenocs SPEC macros (xenocs.mac).
Names (user, 2026-10-20): SPEC wss -> ws, SPEC xautoc -> alignslit.

Pseudo-motors (motors/slits.yml): s1vg s1vo s1hg s1ho, s2vg s2vo s2hg s2ho
(vertical/horizontal gap and offset). Blade positions are distances from the beam centre,
positive = open: gap = top + bot, offset = (top - bot) / 2 (horizontal: hr, hl).

    s1go(1.0)          1.0 mm x 1.0 mm, offsets kept (SPEC ns1go, used by SPECfe's slit page)
    s1go(0.8, 1.2)     vertical 0.8 mm, horizontal 1.2 mm
    ws()                 gaps and offsets of both slits (SPEC wss)
    alignslit("s1")      find the beam centre for every blade of s1 and make it the blade's 0
    alignslit("s2", 0.5) same for s2, then set its gap to 0.5 mm

alignslit starts with beamstop_in(): the det diode is IN the beamstop, so the beamstop must be in.
(beamstop_in() also takes the filter out; that does not change det, because the filter is behind the
beamstop/diode and only attenuates what reaches the Pilatus.)
Then it follows SPEC's xautoc (SPECfe Collimation -> "Current source"): open the slits to 8 mm,
open the shutter, and for each blade: blade to 0, coarse dscan -5..5 (10 steps, 0.1 s), move to the
edge, fine dscan -1..1 (20 steps, 0.1 s), move to the edge, redefine that position as 0, reopen to
8 mm. Shutter closed at the end. The edge is found with step_fit (xeuss_bliss.scan_commands) instead of SPEC's
pl_CFWHM. The detector is the `det` counter of the NI card (beam monitor diode).
Note: the new blade zeros are BLISS offsets only; SPEC keeps its own offsets.
"""

from bliss.common.scans import dscan
from bliss.common.standard import move
from bliss.config.static import get_config

OPEN_GAP = 8.0  # mm, as SPEC's xautoc (s1go 8 / s2go 8)
COARSE = (-5.0, 5.0, 10, 0.1)  # dscan start, stop, intervals, count time
FINE = (-1.0, 1.0, 20, 0.1)


def _get(name):
    return get_config().get(name)


def _go(slit, vgap, hgap=None):
    hgap = vgap if hgap is None else hgap
    move(_get(f"{slit}vg"), vgap, _get(f"{slit}hg"), hgap)


def s1go(vgap, hgap=None):
    """Set slit 1 to vgap x hgap mm (one value: square), keeping the offsets."""
    _go("s1", vgap, hgap)


def s2go(vgap, hgap=None):
    """Set slit 2 to vgap x hgap mm (one value: square), keeping the offsets."""
    _go("s2", vgap, hgap)


def ws():
    """Show gap and offset of both slits (SPEC wss)."""
    for slit in ("s1", "s2"):
        vg, vo, hg, ho = (_get(f"{slit}{k}").position for k in ("vg", "vo", "hg", "ho"))
        print(f"{slit}:  V gap {vg:8.4f}   V offset {vo:8.4f}   H gap {hg:8.4f}   H offset {ho:8.4f}")


def alignslit(slit, gap=None, counter=None):
    """Align the four blades of slit "s1" or "s2" on the beam (SPEC xautoc), see module doc.

    slit     "s1" or "s2"
    gap      gap in mm (square) set after the alignment; None: left 8 mm open, as SPEC
    counter  counter for the edge scans; default ni.counters.det
    """
    from xeuss_bliss.scan_commands import goto_step
    from xeuss_bliss.beamstop import beamstop_in

    if slit not in ("s1", "s2"):
        raise ValueError('slit must be "s1" or "s2"')
    counter = counter if counter is not None else _get("ni").counters.det
    shutter = _get("shutter")
    blades = [_get(f"{slit}{b}") for b in ("bot", "top", "hr", "hl")]

    def reopen():
        # as SPEC's "s1go 8" / "s2go 8": every blade to 4 mm, i.e. symmetric around the blade zeros
        names = (["s1"] if slit == "s1" else []) + ["s2"]
        args = []
        for s in names:
            for b in ("top", "bot", "hr", "hl"):
                args += [_get(f"{s}{b}"), OPEN_GAP / 2]
        move(*args)

    beamstop_in()  # det is the diode in the beamstop (filter is behind it: no effect on det)
    reopen()
    shutter.open()
    try:
        for blade in blades:
            move(blade, 0)
            dscan(blade, *COARSE, counter)
            goto_step(counter=counter, axis=blade)
            dscan(blade, *FINE, counter)
            goto_step(counter=counter, axis=blade)
            blade.position = 0  # this blade position = beam centre (SPEC: set <blade> 0)
            print(f"{blade.name}: beam centre is now 0")
            reopen()
    finally:
        shutter.close()
    if gap is not None:
        _go(slit, gap)
    ws()
