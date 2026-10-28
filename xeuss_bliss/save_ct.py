"""save_ct(): save a finished ct afterwards, as a normal scan of the data file.

ct() is not saved (BLISS), so a count can be looked at first and kept only if it is good:

    ct(60)          # look at the image / counters
    save_ct()       # keep it: next scan number in the current data file (SCAN_SAVING.filename)

The data come from the BLISS data store (Redis), where a finished ct stays available for a while (until
Redis memory management removes old scans). The entry has the same layout as one written by the BLISS
writer for sct (instrument/<counter>/data, measurement/ links, plotselect, positioners, source, slits).
Difference: ct does not collect metadata, so motor positions, slits and source are recorded when
save_ct() runs (noted in writer/note). save_ct(scan) saves a given ct, e.g. save_ct(Out[12]).
"""

import datetime
import os

import numpy

_saved = {}  # data store key of a saved ct -> "file::entry" (no double saving)
_POSITIONER_CHANNELS = ("elapsed_time", "epoch")


_last = {"ct": None}  # the last unsaved ct of the session (BLISS does not keep unsaved cts in SCANS)


def _is_unsaved_ct(scan):
    return scan.scan_info.get("type") == "ct" and not getattr(scan, "_save", True)


def install():
    """Remember every unsaved ct (session scan hook); called by the session setup. BLISS 2.4 does not add an unsaved
    ct to current_session.scans (Scan._add_to_scans_queue = not nonsaved_ct), but calls the scan_queue hooks for it."""
    from bliss import current_session
    from bliss.scanning.hooks import ScanQueueHooks

    class _LastCtHook(ScanQueueHooks):
        def scan_finalized(self, scan):
            if _is_unsaved_ct(scan):
                _last["ct"] = scan

    registry = current_session.hooks.scan_queue
    if not any(type(h).__name__ == "_LastCtHook" for h in registry._hooks):
        registry.register(_LastCtHook())


def _last_ct():
    if _last["ct"] is not None:
        return _last["ct"]
    raise RuntimeError("no unsaved ct since the session start (only ct() counts can be saved afterwards; "
                       "an older one: save_ct(Out[n]))")


def _write_dict(group, values):
    """Write a BLISS metadata dict: "@attr" -> attribute, "name@units" -> dataset with units, dict -> group."""
    for key, value in values.items():
        if key.startswith("@"):
            group.attrs[key[1:]] = value
            continue
        name, _, units = key.partition("@")
        if units and name in group:
            group[name].attrs["units"] = value
            continue
        if isinstance(value, dict):
            _write_dict(group.require_group(name), value)
        elif units:
            continue  # unit given before its value: written with the value below
        else:
            group[name] = value
            unit = values.get(f"{name}@units")
            if unit:
                group[name].attrs["units"] = unit


def _metadata(scan):
    """Instrument metadata (source, slits, ...) and motor positions, evaluated now."""
    from bliss import global_map
    from bliss.scanning.scan_meta import META_TIMING, get_controllers_scan_meta, get_user_scan_meta

    instrument = {}
    for meta in (get_user_scan_meta(), get_controllers_scan_meta()):
        try:
            values = meta.to_dict(scan, timing=META_TIMING.START)
        except Exception as exc:
            print(f"WARNING: save_ct: metadata not collected ({exc})")
            continue
        instrument.update(values.get("instrument", {}))
    positions, dial, units = {}, {}, {}
    for axis, disabled, error, pos, dial_pos, unit in global_map.get_axes_positions_iter(on_error="ERR"):
        if disabled or error:
            continue
        positions[axis.name], dial[axis.name], units[axis.name] = pos, dial_pos, unit
    return instrument, positions, dial, units


def save_ct(scan=None):
    """Save the last ct (or the given one) into the current data file with the next scan number.
    Returns "file::entry"."""
    import h5py
    from bliss import current_session

    scan = scan if scan is not None else _last_ct()
    info = scan.scan_info
    if info.get("type") != "ct" or getattr(scan, "_save", True):
        raise ValueError("save_ct() saves ct() counts only (sct and scans are saved already)")
    data_scan = scan._scan_data
    if data_scan.key in _saved:
        print(f"this ct is already saved: {_saved[data_scan.key]}")
        return _saved[data_scan.key]
    end_reason = data_scan.info.get("end_reason", "SUCCESS")
    if end_reason != "SUCCESS":
        print(f"WARNING: this ct ended with {end_reason}; saving what was measured")

    # data of every channel (fails if the data store has already dropped this scan)
    channels = {}
    try:
        try:
            from xeuss_bliss.pilatus_camserver import _UNSAVED_CHANNELS
        except Exception:
            _UNSAVED_CHANNELS = set()
        for full_name, stream in data_scan.streams.items():
            short = full_name.rsplit(":", 1)[-1]
            # Redis-only channels (running sum of an accumulated exposure): not in the file, as for sct
            if full_name in _UNSAVED_CHANNELS or short.endswith(("_running_sum", "_running_exptime")):
                continue
            channels[short] = numpy.asarray(stream[:])
    except Exception as exc:
        raise RuntimeError(f"the data of this ct are no longer available ({exc})") from None
    instrument_meta, positions, dial, units = _metadata(scan)

    scan_saving = current_session.scan_saving
    filename = scan_saving.filename
    # the BLISS writer creates the folder only with the first saved scan; open the file BEFORE taking a scan
    # number, so that a failure does not leave a gap in the numbering
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    now = datetime.datetime.now().astimezone()
    with h5py.File(filename, "a") as f:
        number = scan_saving._incr_scan_number()  # same counter as the scans: no number used twice
        entry_name = f"{number}.1"
        if entry_name in f:
            raise RuntimeError(f"{filename} already has an entry {entry_name}")
        entry = f.create_group(entry_name)
        entry.attrs["NX_class"] = "NXentry"
        entry.attrs["default"] = "plotselect"
        entry["title"] = info.get("title", "ct")
        entry["start_time"] = str(data_scan.info.get("start_time", info.get("start_time", "")))
        entry["end_time"] = str(data_scan.info.get("end_time", info.get("end_time", "")))
        entry["end_reason"] = end_reason
        params = entry.create_group("scan_parameters")
        params.attrs["NX_class"] = "NXparameters"
        params["count_time"] = info.get("count_time", 0)
        params["npoints"] = info.get("npoints", 1)
        writer = entry.create_group("writer")
        writer.attrs["NX_class"] = "NXnote"
        writer["date"] = now.isoformat()
        writer["status"] = "SUCCEEDED"
        writer["note"] = ("ct saved afterwards with save_ct(); positioners, source and slits recorded at "
                          f"{now:%Y-%m-%d %H:%M:%S}, not during the count")

        inst = entry.create_group("instrument")
        inst.attrs["NX_class"] = "NXinstrument"
        meas = entry.create_group("measurement")
        meas.attrs["NX_class"] = "NXcollection"
        paths = {}
        for name, data in channels.items():
            group = inst.create_group(name)
            if name in _POSITIONER_CHANNELS:
                group.attrs["NX_class"] = "NXpositioner"
                path = f"{name}/value"
                group.create_dataset("value", data=data)
            else:
                group.attrs["NX_class"] = "NXdetector"
                path = f"{name}/data"
                if data.ndim == 3:
                    ds = group.create_dataset("data", data=data, compression="gzip")
                    ds.attrs["interpretation"] = "image"
                else:
                    group.create_dataset("data", data=data)
            paths[name] = f"/{entry_name}/instrument/{path}"
            meas[name] = h5py.SoftLink(paths[name])

        for group_name, values in (("positioners", positions), ("positioners_start", positions),
                                   ("positioners_dial_start", dial)):
            group = inst.create_group(group_name)
            group.attrs["NX_class"] = "NXcollection"
            for axis_name, value in values.items():
                group[axis_name] = value
                if units.get(axis_name):
                    group[axis_name].attrs["units"] = units[axis_name]
        _write_dict(inst, instrument_meta)

        # default plot as the BLISS writer makes it for sct (xeuss_bliss.nexus_default): the image if there is one,
        # else the selected counter (plotselect), else the Pilatus sum - against elapsed time
        plot = entry.create_group("plotselect")
        plot.attrs["NX_class"] = "NXdata"
        images = [c for c, d in channels.items() if d.ndim == 3]
        if images:
            signal = "saxs_image" if "saxs_image" in images else images[0]
            plot.attrs["signal"] = signal
            plot[signal] = h5py.SoftLink(paths[signal])
            plot.attrs["axes"] = ["elapsed_time", "datadim0", "datadim1"]
            plot["datadim0"] = numpy.arange(channels[signal].shape[1])
            plot["datadim1"] = numpy.arange(channels[signal].shape[2])
        else:
            try:
                from bliss.common.plot import get_plotted_counters

                selected = [c.rsplit(":", 1)[-1] for c in get_plotted_counters()]
            except Exception:
                selected = []
            signal = next((c for c in selected if c in channels and channels[c].ndim == 1),
                          "saxs_sum" if "saxs_sum" in channels else next(iter(channels)))
            plot.attrs["signal"] = signal
            plot[signal] = h5py.SoftLink(paths[signal])
            plot.attrs["axes"] = ["elapsed_time"]
        if "elapsed_time" in channels:
            plot["elapsed_time"] = h5py.SoftLink(paths["elapsed_time"])

    where = f"{filename}::/{entry_name}"
    _saved[data_scan.key] = where
    print(f"saved: {info.get('title', 'ct')} ({data_scan.info.get('start_time', '')[:19]}) as scan {number} "
          f"in {filename}")
    return where
