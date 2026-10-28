"""Open the X-ray shutter automatically for every scan of the Xeuss 2.0 (xeuss20), and guard the beam.

ShutterPreset sits on BLISS's default acquisition chain, so it acts on ct, sct, loopscan, ascan,
dscan, ... (as SPEC's multiexp opened and closed the shutter around exposures):

    start of the scan   open the shutter if it is closed, wait SETTLE s (shutter fully open)
    end of the scan     put the shutter back to what it was: closed if it was closed,
                        open if the user had opened it (so()). Also after Ctrl-C and errors.

    shutter_auto(False) / shutter_auto(True)   switch the automatic opening off / on
    shutter_auto()                             show the current setting

Beam guard (2026-10-23): the GeniX3D generator (object "genix", xeuss_bliss.genix, read over Modbus/TCP) reports
whether its safety shutter - the same shutter that the NI card opens - is really open, the X-rays are on and the
interlock is OK. While the shutter should be open:

    before every opening (scan start, so())  the generator must be ready - X-rays on AT THE SETPOINTS (kV within 2 %,
                                              mA within 5 %; standby runs below them), doors closed, interlock OK, no
                                              fault - else the shutter is NOT opened (BeamLost with the reason)
    after every opening                       wait up to OPEN_TIMEOUT s for "open"; if not: close the shutter and
                                              stop with the reason
    after every point of every scan           stop the scan if the beam is gone
    during the scan (also long ct/sct counts) check every GUARD_POLL s, stop the scan if the beam is gone

A stopped scan ends with BeamLost (end reason FAILURE); an accumulated exposure keeps its complete frames; notifying()
sends its error e-mail. Dark measurements (shutter_auto(False), shutter closed) are not checked. If the generator does
not answer, a warning is printed and the measurement goes on (the guard is for the data, not for safety).

    beam_guard(False) / beam_guard(True) / beam_guard()   switch the guard off / on / show
"""

import time

import gevent

from bliss.common.greenlet_utils import KillMask
from bliss.config.static import get_config
from bliss.scanning.chain import ChainIterationPreset, ChainPreset
from bliss.scanning.scan import ScanPreset

PRESET_NAME = "xeuss_shutter"
SETTLE = 0.1  # s after opening, before counting
OPEN_TIMEOUT = 2.0  # s for the generator to report the shutter open
KV_TOLERANCE = 0.02  # actual kV within 2 % of the setpoint
MA_TOLERANCE = 0.05  # actual mA within 5 % of the setpoint
GUARD_POLL = 1.0  # s between checks during a scan
_guard = {"on": True}


class BeamLost(RuntimeError):
    """The X-ray beam is not there although the shutter should be open."""


def _genix():
    try:
        return get_config().get("genix")
    except Exception:
        return None


def _setpoint_problem(status):
    """Text if the actual kV / mA are not at the generator's setpoints (standby, ramping, power drop), else None.
    Standby on this GeniX3D (tested 2026-10-23) keeps "X-rays on" and the setpoints (50 kV / 0.6 mA) but runs at
    30 kV / 0.3 mA - only this comparison shows it."""
    kv, ma, kv_set, ma_set = status["kv"], status["ma"], status["kv_setpoint"], status["ma_setpoint"]
    if kv_set <= 0 or ma_set <= 0:
        return None  # no setpoint: X-rays off is reported separately
    if abs(kv - kv_set) > KV_TOLERANCE * kv_set or abs(ma - ma_set) > MA_TOLERANCE * ma_set:
        return (f"generator not at setpoint: {kv:.1f} kV / {ma:.2f} mA instead of {kv_set:.1f} kV / "
                f"{ma_set:.2f} mA (standby?)")
    return None


def _beam_problem(status, shutter=True):
    """None if the beam is there (shutter open, X-rays on at the setpoints, doors closed, interlock OK, no fault),
    else the reason as text. shutter=False: everything except the shutter position (check before opening)."""
    reasons = []
    if not status["xray_on"]:
        reasons.append("X-rays off" + (" (standby)" if status["standby"] else ""))
    else:
        setpoint = _setpoint_problem(status)
        if setpoint:
            reasons.append(setpoint)
    if not status.get("door_closed", True):
        reasons.append("enclosure door open")
    if not status["interlock_ok"]:
        reasons.append("generator interlock not OK")
    if status["faults"] or status["fault"]:
        reasons.append("generator fault: " + (", ".join(status["faults"]) or "unspecified"))
    if shutter and not status["shutter_open"] and status.get("door_closed", True):
        reasons.append("safety shutter closed")  # door open is reported above (it closes the shutter in hardware)
    return "; ".join(reasons) or None


def _read_status(warn=True):
    """Generator status, or None if the guard is off or the generator does not answer (warning if warn)."""
    if not _guard["on"]:
        return None
    genix = _genix()
    if genix is None:
        if warn:
            print("WARNING: beam guard: no 'genix' object - beam not checked")
        return None
    try:
        return genix.status()
    except Exception as exc:
        if warn:
            print(f"WARNING: beam guard: generator not readable ({exc}) - beam not checked")
        return None


def check_before_open():
    """Before opening the shutter: raise BeamLost (shutter NOT opened) if the generator is not ready - X-rays off or
    not at the setpoints (standby), enclosure door open, interlock, fault."""
    status = _read_status()
    if status is None:
        return
    problem = _beam_problem(status, shutter=False)
    if problem:
        raise BeamLost(f"X-ray shutter not opened: {problem}")


def verify_open(shutter, timeout=OPEN_TIMEOUT):
    """After shutter.open(): wait until the generator reports the beam; else close the shutter and raise BeamLost."""
    deadline = time.monotonic() + timeout
    while True:
        status = _read_status()
        if status is None:
            return
        problem = _beam_problem(status)
        if problem is None:
            return
        if time.monotonic() > deadline:
            shutter.close()
            raise BeamLost(f"X-ray shutter did not open: {problem}")
        gevent.sleep(0.1)


class _PointCheck(ChainIterationPreset):
    """After every point of a scan: stop it if the beam is gone."""

    def __init__(self, guard):
        self._guard = guard

    def stop(self):
        if self._guard.active:
            self._guard.check("after a scan point")


class _Guard:
    def __init__(self):
        self.active = False
        self._warned = False

    def check(self, when):
        status = _read_status(warn=not self._warned)  # one warning per scan if the generator does not answer
        if status is None:
            self._warned = True
            return
        problem = _beam_problem(status)
        if problem:
            self.active = False  # report once
            raise BeamLost(f"X-ray beam lost {when}: {problem}")


class _CloseAtScanEnd(ScanPreset):
    """Safety net: BLISS calls the chain presets' stop only if the scan got past its prepare phase. A Ctrl-C while
    the scan is prepared (motors moving to the start position, detector set up) - the shutter is already open then -
    skipped it and left the shutter open. Scan presets' stop always runs at the end of the scan."""

    def __init__(self, shutter_preset):
        super().__init__()
        self._shutter_preset = shutter_preset

    def stop(self, scan):
        self._shutter_preset._end()


class ShutterPreset(ChainPreset):
    def __init__(self, shutter_name="shutter"):
        super().__init__()
        self._shutter_name = shutter_name
        self._was_open = None
        self._guard = _Guard()
        self._monitor = None

    def _shutter(self):
        return get_config().get(self._shutter_name)

    def prepare(self, chain):
        shutter = self._shutter()
        self._was_open = shutter.is_open
        self._guard = _Guard()
        try:
            chain.scan.add_preset(_CloseAtScanEnd(self))
        except Exception as exc:
            print(f"WARNING: automatic shutter: no safety net for this scan ({exc})")
        if not self._was_open:
            check_before_open()  # generator not ready (standby, door, ...): the shutter is not opened at all
            shutter.open()
            gevent.sleep(SETTLE)
        verify_open(shutter)  # opened now or by so(): the beam must be there before counting
        self._guard.active = _guard["on"]

    def get_iterator(self, chain):
        while True:
            yield _PointCheck(self._guard)

    def start(self, chain):
        if self._guard.active:
            self._monitor = gevent.spawn(self._watch, chain)

    def _watch(self, chain):
        """During the scan (long counts): check every GUARD_POLL s, stop the scan when the beam is gone."""
        while self._guard.active:
            gevent.sleep(GUARD_POLL)
            try:
                self._guard.check("during the scan")
            except BeamLost as exc:
                chain.scan.stop_scan_on_error(exc)
                return

    def stop(self, chain):
        self._end()

    def _end(self):
        """End of the scan (chain stop, or the scan-end safety net; the second call does nothing): stop the guard,
        close the shutter if it was closed before the scan."""
        self._guard.active = False
        if self._monitor is not None:
            self._monitor.kill()
            self._monitor = None
        if self._was_open is False:
            with KillMask(masked_kill_nb=1):  # one more Ctrl-C must not leave the shutter open
                self._shutter().close()
        self._was_open = None


def _default_chain():
    from bliss.common.scans import DEFAULT_CHAIN

    return DEFAULT_CHAIN


def shutter_auto(on=None):
    """Automatic shutter for scans: shutter_auto(True) / (False); no argument: show the setting."""
    chain = _default_chain()
    active = PRESET_NAME in chain._presets
    if on is None:
        print(f"automatic shutter for scans: {'ON' if active else 'OFF'}")
        return active
    if on and not active:
        chain.add_preset(ShutterPreset(), name=PRESET_NAME)
    elif not on and active:
        chain.remove_preset(name=PRESET_NAME)
    print(f"automatic shutter for scans: {'ON' if on else 'OFF'}")
    return bool(on)


def beam_guard(on=None):
    """Beam guard (generator check of shutter / X-rays / interlock): beam_guard(True) / (False); no argument: show."""
    if on is not None:
        _guard["on"] = bool(on)
    print(f"beam guard (GeniX3D: shutter open, X-rays on, interlock OK): {'ON' if _guard['on'] else 'OFF'}")
    return _guard["on"]
