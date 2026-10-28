from bliss.setup_globals import *

# BLISS scan files (NeXus/HDF5, including the Pilatus images) on the local data disk:
# /data_local/bliss/<experimenter>/<sample>.h5, changed with newfile("alice", "sample1").
# Defaults xeuss/data = the file used before newfile existed (/data_local/bliss/xeuss/data.h5).
SCAN_SAVING.base_path = "/data_local/bliss"
for _field, _default in (("experimenter", "xeuss"), ("sample", "data")):
    if not hasattr(SCAN_SAVING, _field):
        SCAN_SAVING.add(_field, _default)
SCAN_SAVING.template = "{experimenter}/"
SCAN_SAVING.data_filename = "{sample}"

# Scan analysis, actions during scans, newfile and e-mail notifications: taken over from the XRD-3003TT
# package (github.com/lapkindm/xrd3003_bliss, commit 23e6e6d) into xeuss_bliss on 2026-10-21
from xeuss_bliss.scan_commands import deriv, deriv_fit, deriv_fwhm, step_fit, goto_step, shad_fit
from xeuss_bliss.actions import with_actions
from xeuss_bliss.saving import newfile
from xeuss_bliss.save_ct import save_ct, install as _install_save_ct

_install_save_ct()  # remember unsaved cts for save_ct() (BLISS does not keep them in SCANS)
from xeuss_bliss.notify import notifying
from xeuss_bliss.beamstop import beamstop_in, beamstop_out, beamstop_state, take_direct_beam
from xeuss_bliss.shutter_preset import shutter_auto, beam_guard
from xeuss_bliss.slits import s1go, s2go, ws, alignslit
from xeuss_bliss.filter import filter_in, filter_out, filter_state
import xeuss_bliss.notify

# every error e-mail of notifying(...) also goes to the person responsible for the instrument
# (mail goes by SMTP to the local Postfix, see xeuss_bliss/notify.py)
xeuss_bliss.notify.ERRORS_ALWAYS_TO = "responsible@example.org"  # set on xeuss20


# X-ray source, written into every scan (NeXus: instrument/source). Cu tube WITHOUT monochromator:
# both Cu K-alpha lines (Kalpha2/Kalpha1 intensity 1:2). No sample-detector distance or beam centre
# is stored on purpose (the distance is not measured on this instrument).
from bliss.scanning.scan_meta import get_user_scan_meta

# The dict is merged into the scan's "instrument" group, so it needs its own "source" level.
get_user_scan_meta().instrument.set(
    "source",
    {"source": {
        "@NX_class": "NXsource",
        "name": "Cu X-ray tube, Xenocs Xeuss 2.0, no monochromator",
        "type": "Fixed Tube X-ray",
        "probe": "x-ray",
        "target_material": "Cu",
        "wavelength_Ka1": 1.540593,
        "wavelength_Ka1@units": "angstrom",
        "wavelength_Ka2": 1.544414,
        "wavelength_Ka2@units": "angstrom",
        "intensity_ratio_Ka2_Ka1": 0.5,
        "wavelength_mean": 1.5418,
        "wavelength_mean@units": "angstrom",
    }},
)


# Flint (the plot window) opens with the first scan and shows every scan live (BLISS default: off).
# BLISS starts Flint with fork(), which crashes (SIGSEGV) while an NI-DAQmx task is open in this
# process (the shutter task always is) -> start it with posix_spawn instead (xeuss_bliss/flint_spawn.py).
from xeuss_bliss.flint_spawn import install as _install_flint_spawn

_install_flint_spawn()
SCAN_DISPLAY.flint_python_bin = ""  # undo the debug wrapper used while investigating (2026-10-21)
SCAN_DISPLAY.auto = True
# Counts (ct, sct, loopscan) show the Pilatus image tab in Flint instead of the Count tab
from xeuss_bliss.flint_display import flint_image_tab, open_flint, show_rois

flint_image_tab(True)

# Default counter for the curve plot and cen()/peak()/goto_com()/step_fit(): det (the beamstop diode),
# only if nothing is selected yet (plotselect(...) of the user is kept across sessions)
from bliss.common.plot import plotselect

if not SCAN_DISPLAY.displayed_channels:
    plotselect(ni.counters.det)


# Short commands to choose the selected counter (curve plot, cen/peak/goto_com/step_fit, ...)
def select_diode():
    """Select the diode in the beamstop (ni:det)."""
    plotselect(ni.counters.det)
    print("selected counter: diode (ni:det)")


def select_direct():
    """Select the direct-beam ROI of the SAXS Pilatus (pilatus_saxs:saxs_direct)."""
    plotselect(pilatus_saxs.counters.saxs_direct)
    print("selected counter: direct (pilatus_saxs:saxs_direct)")


def select_specular():
    """Select the specular-reflection ROI of the SAXS Pilatus (pilatus_saxs:saxs_spec)."""
    plotselect(pilatus_saxs.counters.saxs_spec)
    print("selected counter: specular (pilatus_saxs:saxs_spec)")

# Every scan (ct, sct, loopscan, ascan, ...) opens the shutter if needed and restores it at the end;
# shutter_auto(False) switches this off (xeuss_bliss/shutter_preset.py).
shutter_auto(True)

# data file: default plot of ct / sct entries = the SAXS image (not a counter against time)
from xeuss_bliss.nexus_default import nexus_image_default

nexus_image_default(True)


# Personal macros: ~/Macros/Users/<name>/<file>.py, loaded with
# user_script_load("<name>/<file>", export_global=True); user_script_list() lists them.
user_script_homedir("/home/saxs/Macros/Users")


# Initialise all motors now: BLISS does it lazily at first use, which made the first scan of a session
# 3-6 s slower (24 axes, several queries each on the 9600-baud MDrive line). A motor that fails to
# initialise gives a warning instead of stopping the session start.
def _init_motors():
    import time
    from bliss import global_map

    t0 = time.time()
    failed = []
    axes = list(global_map.get_axes_iter())
    print(f"Initialising {len(axes)} motors (about 6 s) ...", flush=True)
    for axis in axes:
        try:
            axis.position
        except Exception as e:
            failed.append(f"{axis.name} ({e})")
    print(f"Motors initialised: {len(axes) - len(failed)}/{len(axes)} in {time.time() - t0:.1f} s")
    for f in failed:
        print(f"WARNING: motor not initialised: {f}")


_init_motors()


def so():
    """Open the X-ray shutter (as SPEC 'so'); the generator must report the beam within 2 s, else the shutter is
    closed again and BeamLost tells why (interlock, X-rays off, fault) - see beam_guard()."""
    from xeuss_bliss.shutter_preset import check_before_open, verify_open

    check_before_open()  # generator not ready (standby, door, ...): not opened
    shutter.open()
    verify_open(shutter)


def sc():
    """Close the X-ray shutter (as SPEC 'sc')."""
    shutter.close()


print("Xeuss BLISS session: counters ni (sec det), shutter (so/sc), 16 MDrive motors (detz: known hardware problem), pilatus_saxs/pilatus_waxs (camserver must run).")
print("Data file:", SCAN_SAVING.filename, "| newfile(experimenter, sample) to change it")

open_flint()  # Flint opens with the session (xeuss_bliss/flint_display.py)


# Dima's alignment macros (z_scan, z_scan_spec, om_scan_spec, spatial_scan) at every start, at the END of the
# setup: user_script_load gives the macro file the names of bliss.setup_globals, which does not contain the
# names of this setup file yet (BLISS adds them when the setup has finished) -> put them there first
# (found 2026-10-22: z_scan_spec() -> NameError beamstop_out). An error in the macro file must not stop the
# session (load it again by hand after fixing it).
# Only the functions DEFINED in the macro file are added to the session: export_global=True would also try to
# re-export inherited names that differ from the session's (e.g. plotselect, which is protected -> whole load
# refused, 2026-10-22).
import bliss.setup_globals as _setup_globals

_setup_globals.__dict__.update({_k: _v for _k, _v in globals().items() if not _k.startswith("_")})
_MACRO_FILE = "/home/saxs/Macros/Users/Dima/alignment.py"
try:
    _macros = user_script_load("Dima/alignment", export_global=False)
    for _name, _obj in _macros._asdict().items():
        if getattr(getattr(_obj, "__code__", None), "co_filename", None) == _MACRO_FILE:
            globals()[_name] = _obj
except Exception as _exc:
    print(f"WARNING: Dima/alignment macros not loaded: {_exc}")
