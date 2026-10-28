"""BLISS controller for the Dectris PILATUS3 300K detectors of the Xeuss 2.0 (xeuss20), via camserver.

No Lima: the Lima Pilatus plugin needs camserver to write into a tmpfs that Lima watches with
inotify, i.e. a change on the detector PCs (CentOS 6), which must stay as they are for SPEC.
This controller talks to camserver like SPEC's xenocs_macros/pilatus.mac does:

  - TCP port 41234 on the detector PC; camserver is started there by the Dectris0/Dectris1
    launchers (runtvx). Only the first client gets full control, so the connection is opened
    when a scan is prepared and CLOSED when the scan ends (SPEC also closes after each exposure).
  - "exptime <t>", then per point "exposure <file>". Replies end with "\\x18": "15 OK ..." is the
    acknowledgement, "7 OK <path>" means the image file is written. "k" aborts ("13 ERR kill").
  - camserver writes the EDF file on the detector PC (local_dir, /data/images). xeuss20 sees the
    same folder over NFS (host_dir, /data0/images or /data1/images) and reads the finished file.
    The EDF files are left untouched (SPEC adds header keys; here the NeXus file has that).
  - The image goes into the BLISS scan (NeXus/HDF5, SCAN_SAVING.base_path, /data_local/bliss).
  - With `delete_edf_after_read: true` the EDF is deleted right after reading: the BLISS scan data is
    then the only copy (a ct() without saving keeps no image at all).

Counters: <prefix>_image (2D, int32), <prefix>_sum (sum of the image), <prefix>_filenumber (number in
the EDF file name), and one counter <prefix>_<roi> per ROI (Lima-like ROI counters, see below);
prefix = YAML "counter_prefix" (saxs / waxs).
As SPEC (pilatus_imgtools.mac: pilct0 / pilroi0), negative pixels (gaps -1, bad pixels -2) count as 0
in sum and ROIs. The saved image itself is unchanged.

ROI counters (like Lima's roi_counters; drawn as rectangles in Flint's image tab by xeuss_bliss.flint_display):
    pilatus_saxs.roi_counters                         table of all ROIs
    pilatus_saxs.roi_counters["direct"] = (x, y, w, h)  move/add an ROI: x = first column, y = first row,
                                                        width, height in pixels (Lima convention)
    pilatus_saxs.roi_counters.set_spec("spec", 4, 578, 13, 583)   SPEC pilatus_def_roi numbers
                                                        (colbeg, rowbeg, colend, rowend, ends included)
    del pilatus_saxs.roi_counters["test"]             remove an ROI (its counter disappears)
    pilatus_saxs.roi_counters.reset("direct")         back to the YAML default (reset() = all)
Defaults in YAML ("rois", x y w h); changes are kept in the BLISS settings database.

Accumulated exposure (live sum in Flint):
    pilatus_saxs.accumulate(10)     every count of >= 2 x 10 s is taken as a series of equal frames of about
                                    10 s (camserver nimages/expperiod, 10 ms dead time per frame) that are
                                    summed; the running sum is shown in Flint ("pilatus_saxs accumulating")
                                    after every frame. The scan stores only the summed image (counts for the
                                    full count time; ROI counters and sum from it).
    pilatus_saxs.accumulate(0)      off (one exposure per point, the default)
    pilatus_saxs.accumulate()       show the setting
camserver keeps nimages/expperiod/setackint for every client, so they are put back (1 image, ack 0, previous
period) at the end of every scan and after an abort - SPEC expects single images.
Folders are created on the xeuss20 side with mode 777, as SPEC does (detector PC user "det"
writes into them; xeuss20 user saxs cannot create top-level folders in /data/images).

YAML example::

    - name: pilatus_saxs
      plugin: bliss
      class: PilatusCamserver
      package: xeuss_bliss.pilatus_camserver
      host: 192.168.0.100
      port: 41234
      local_dir: /data/images        # path on the detector PC (camserver)
      host_dir: /data0/images        # the same folder on xeuss20 (NFS)
      subdir: inhouse/bliss          # images go to <dir>/<subdir>/<YYYY-MM-DD>/
      rows: 619
      columns: 487
"""

import datetime
import os
import re
import socket
import time

import fabio
import gevent
import numpy

from bliss.common.counter import Counter
from bliss.config.settings import SimpleSetting
from bliss.controllers.counter import IntegratingCounterController
from bliss.scanning.acquisition.counter import IntegratingCounterAcquisitionSlave
from bliss.scanning.channel import AcquisitionChannel

from xeuss_bliss.acq_utils import emit_pending

EOS = b"\x18"
SERIES_GAP = 0.01  # s between frames of a series (expperiod - exptime); camserver refuses 3 ms
MAX_FRAMES = 65535  # camserver: "nimages n", n < 2^16
RUNNING_SUM_PERIOD = 10.0  # s, default of running_sum_period()
# Running sums of accumulated exposures for on-the-fly analysis: two extra scan channels
# <detector>:<prefix>_running_sum (image after every frame) and <prefix>_running_exptime (s), in scans that
# accumulate only. They are in the BLISS data store (Redis) like every channel, e.g. for ewoksdata
# iter_bliss_scan_data_from_memory(scan_key, counter_names=["saxs_running_sum", "saxs_running_exptime"]),
# but NOT written to the data file (_patch_writer), which keeps the layout of a normal scan.
_UNSAVED_CHANNELS = set()  # channel full names the BLISS writer must skip


def _patch_writer():
    """Make the BLISS (internal) writer skip the channels in _UNSAVED_CHANNELS."""
    try:
        from blisswriter.writer.main import ScanWriterWithState
    except ImportError:
        return
    original = ScanWriterWithState._save_channel
    if getattr(original, "_xeuss_patched", False):
        return

    def _save_channel(self, channel):
        if channel.name in _UNSAVED_CHANNELS:
            return False
        return original(self, channel)

    _save_channel._xeuss_patched = True
    ScanWriterWithState._save_channel = _save_channel


_patch_writer()
MIN_FREE_BYTES = 1e9  # keep at least 1 GB free on the detector PC (SPEC writes there too)


class ImageCounter(Counter):
    def __init__(self, name, shape, controller=None):
        super().__init__(name, controller=controller, dtype=numpy.int32)
        self._image_shape = tuple(shape)

    @property
    def shape(self):
        return self._image_shape


class PilatusAcquisitionSlave(IntegratingCounterAcquisitionSlave):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # accumulated exposure: running sum channels (Redis only, see _UNSAVED_CHANNELS)
        self._running = None
        device = self.device
        if device._series_planned(self.count_time):
            prefix = device._names["image"][: -len("_image")]
            shape = device._counters[device._names["image"]].shape
            image = AcquisitionChannel(f"{device.name}:{prefix}_running_sum", numpy.int32, shape)
            exposure = AcquisitionChannel(f"{device.name}:{prefix}_running_exptime", numpy.float64, (), unit="s")
            # BLISS adds the counter channels later; per-point data must not go into these (see _emit_new_data)
            self.channels.extend([image, exposure])
            _UNSAVED_CHANNELS.update({image.fullname, exposure.fullname})
            self._running = (image, exposure)

    def _emit_new_data(self, data):
        """Per-point counter values to the counter channels only (the running-sum channels get data per frame)."""
        channels = [c for c in self.channels if not self._running or c not in self._running]
        for channel, values in zip(channels, data):
            channel.emit(values)

    def prepare_device(self):
        self.device._running_channels = self._running
        if self._running:
            try:  # also keep them out of the h5 mapping of the scan
                channels = self.acq_chain_iter.acquisition_chain.scan.scan_info["channels"]
                for channel in self._running:
                    channels.setdefault(channel.fullname, {})["no_saving"] = True
            except Exception:
                pass
        self.device._prepare(self.count_time)

    def trigger(self):
        self.device._trigger()

    def wait_ready(self):
        self.device._wait_ready()

    def stop_device(self):
        self.device._stop()
        try:  # interrupted accumulation: its partial sum is stored as this point (same data as a complete one)
            emit_pending(self)
        except Exception as exc:
            print(f"WARNING: {self.device.name}: partial point not sent to the scan: {exc}")


class Camserver:
    """Minimal camserver client (blocking socket, gevent-cooperative through monkey patching)."""

    def __init__(self, host, port, timeout=5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buffer = b""

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def send(self, command):
        self._sock.sendall(command.encode() + b"\n")

    def read_message(self, timeout):
        """Next message (without the 0x18 terminator), as text."""
        deadline = time.monotonic() + timeout
        while EOS not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no answer from camserver within {timeout} s")
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("camserver closed the connection")
            self._buffer += chunk
        message, self._buffer = self._buffer.split(EOS, 1)
        return message.decode(errors="replace").strip()

    def command(self, command, timeout=5.0):
        """Send a command and return its "15 OK" answer; raise on ERR."""
        self.send(command)
        answer = self.read_message(timeout)
        if " ERR" in answer.split("\n")[0]:
            raise RuntimeError(f"camserver: {command!r} -> {answer!r}")
        return answer



class RoiCounters:
    """Lima-like ROI counters of a PilatusCamserver: one counter <prefix>_<name> per ROI."""

    _RESERVED = ("image", "sum", "filenumber", "exptime")

    def __init__(self, controller, prefix, shape, defaults):
        from bliss.config.settings import HashSetting

        self._ctrl = controller
        self._prefix = prefix
        self._rows, self._columns = shape
        self._defaults = {str(k): self._check(str(k), v) for k, v in dict(defaults).items()}
        self._store = HashSetting(f"{controller.name}:roi_counters")  # name -> "x,y,w,h" or "removed"
        self._counters = {}
        for name in self._names():
            self._make_counter(name)

    # --- stored state ----------------------------------------------------------------
    def _stored(self):
        return {str(k): str(v) for k, v in self._store.get_all().items()}

    def _names(self):
        stored = self._stored()
        names = [n for n in self._defaults if stored.get(n) != "removed"]
        names += [n for n, v in stored.items() if n not in self._defaults and v != "removed"]
        return names

    def _check(self, name, roi):
        if name in self._RESERVED or not name.isidentifier():
            raise ValueError(f"ROI name {name!r}: use a Python identifier, not one of {self._RESERVED}")
        x, y, w, h = (int(v) for v in roi)
        if not (w > 0 and h > 0 and 0 <= x and x + w <= self._columns and 0 <= y and y + h <= self._rows):
            raise ValueError(f"ROI {name} {(x, y, w, h)} outside the image "
                             f"({self._columns} columns x {self._rows} rows)")
        return (x, y, w, h)

    def _make_counter(self, name):
        from bliss.common.counter import Counter

        if name not in self._counters:
            self._counters[name] = self._ctrl.create_counter(Counter, f"{self._prefix}_{name}")

    # --- Lima-like interface --------------------------------------------------------
    def __getitem__(self, name):
        stored = self._stored().get(name)
        if stored and stored != "removed":
            return tuple(int(v) for v in stored.split(","))
        if name in self._defaults and stored != "removed":
            return self._defaults[name]
        raise KeyError(f"no ROI {name!r}; ROIs: {', '.join(self._names())}")

    def __setitem__(self, name, roi):
        x, y, w, h = self._check(name, roi)
        self._store[name] = f"{x},{y},{w},{h}"
        self._make_counter(name)
        print(f"{self._ctrl.name} ROI {name} (counter {self._prefix}_{name}): x {x}, y {y}, {w} x {h} px "
              f"= SPEC columns {x}..{x + w - 1}, rows {y}..{y + h - 1}")
        self._show_in_flint()

    def __delitem__(self, name):
        if name not in self._counters:
            raise KeyError(f"no ROI {name!r}")
        self._store[name] = "removed"
        self._ctrl.remove(self._counters.pop(name))
        print(f"{self._ctrl.name} ROI {name} removed")
        self._show_in_flint()

    def _show_in_flint(self):
        try:
            from xeuss_bliss.flint_display import roi_changed

            roi_changed(self._ctrl)
        except Exception:
            pass  # display only

    def __contains__(self, name):
        return name in self._counters

    def __iter__(self):
        return iter(list(self._counters))

    def set_spec(self, name, colbeg, rowbeg, colend, rowend):
        """Set an ROI with SPEC's pilatus_def_roi numbers (ends included)."""
        self[name] = (colbeg, rowbeg, colend - colbeg + 1, rowend - rowbeg + 1)

    def reset(self, name=None):
        """Back to the YAML defaults: one ROI, or all ROIs (added ones are removed)."""
        names = [name] if name is not None else list(set(self._names()) | set(self._stored()))
        for n in names:
            self._store.remove(n)
            if n in self._defaults:
                self._make_counter(n)
            elif n in self._counters:
                self._ctrl.remove(self._counters.pop(n))
        print(f"{self._ctrl.name} ROIs reset to the defaults: {', '.join(self._names())}")
        self._show_in_flint()

    def sums(self, positive_image):
        """{counter name: sum over the ROI} for one image (negative pixels already set to 0)."""
        result = {}
        for name, counter in self._counters.items():
            x, y, w, h = self[name]
            result[counter.name] = float(positive_image[y:y + h, x:x + w].sum())
        return result

    def __info__(self):
        lines = ["  ROI counters (x, y, width, height | SPEC columns, rows; ends included):"]
        for name in self._counters:
            x, y, w, h = self[name]
            lines.append(f"    {self._prefix}_{name:10} {x:4d} {y:4d} {w:4d} {h:4d} | "
                         f"columns {x}..{x + w - 1}, rows {y}..{y + h - 1}")
        if len(lines) == 1:
            lines.append("    none")
        return "\n".join(lines)

    def __repr__(self):
        return self.__info__()

class PilatusCamserver(IntegratingCounterController):
    def __init__(self, name, config):
        super().__init__(name)
        self._config = config
        self._host = config["host"]
        self._port = int(config.get("port", 41234))
        self._local_dir = config.get("local_dir", "/data/images")
        self._host_dir = config["host_dir"]
        self._subdir = config.get("subdir", "inhouse/bliss")
        shape = (int(config.get("rows", 619)), int(config.get("columns", 487)))
        # counter names carry the detector prefix, so SAXS and WAXS datasets do not collide in the NeXus file
        prefix = config.get("counter_prefix", name)
        self._names = {key: f"{prefix}_{key}" for key in ("image", "sum", "filenumber", "exptime")}
        self.create_counter(ImageCounter, self._names["image"], shape)
        self.create_counter(Counter, self._names["sum"])
        self.create_counter(Counter, self._names["filenumber"])
        # real exposure of the point: the count time, or less if an accumulated exposure was interrupted
        self.create_counter(Counter, self._names["exptime"], unit="s")
        self._shape = shape
        self.roi_counters = RoiCounters(self, prefix, shape, config.get("rois", {}))
        self._file_number = SimpleSetting(f"{name}:file_number", default_value=0)
        self._accumulate = SimpleSetting(f"{name}:accumulate_frame", default_value=0.0)
        # running sum to Redis (scan channels) at most every this many seconds; 0 = after every frame
        self._running_period = SimpleSetting(f"{name}:running_sum_period", default_value=RUNNING_SUM_PERIOD)
        self._last_running_emit = None
        self._series = None  # (number of frames, frame time) of the running scan, None: single exposures
        self._series_running = False  # True while a series point runs; stays True if it was interrupted
        self._running_channels = None  # (running sum, running exposure) scan channels, set by the slave
        self._partial = None  # running sum of the series point (saved if the series is interrupted)
        self._camserver_defaults = None
        # delete the EDF on the detector PC (through the NFS mount) once BLISS has read it;
        # the image then exists only in the BLISS scan data (NeXus file if the scan is saved)
        self._delete_edf = bool(config.get("delete_edf_after_read", False))
        self._camserver = None
        self._count_time = None
        self._data = []
        self._exposing = None

    def __info__(self):
        return (
            f"PILATUS via camserver '{self.name}' on {self._host}:{self._port}\n"
            f"  images: {self._local_dir}/{self._subdir}/<date>/ (detector PC)\n"
            f"        = {self._host_dir}/{self._subdir}/<date>/ (xeuss20)\n"
            f"  next file number: {self._file_number.get()}\n"
            f"  delete EDF after read: {self._delete_edf}\n"
            f"  accumulate: {self._accumulate_text()}\n"
            f"  running sum to Redis: {self._running_period_text()}\n"
            + self.roi_counters.__info__()
        )

    def __close__(self):
        self._stop()

    # --- BLISS counter controller interface ---------------------------------------

    def get_acquisition_object(self, acq_params, ctrl_params, parent_acq_params):
        return PilatusAcquisitionSlave(
            self, ctrl_params=ctrl_params, prepare_once=True, start_once=True, **acq_params
        )

    def get_values(self, from_index, *counters):
        rows = self._data[from_index:]
        return [[row[cnt.name] for row in rows] for cnt in counters]

    # --- acquisition ---------------------------------------------------------------

    def _folders(self):
        day = datetime.date.today().isoformat()
        rel = os.path.join(self._subdir, day)
        return os.path.join(self._local_dir, rel), os.path.join(self._host_dir, rel)

    def _prepare(self, count_time):
        self._stop()
        self._data = []
        self._series_running = False
        self._count_time = count_time
        local, host = self._folders()
        if not os.path.isdir(host):
            os.makedirs(host)
            os.chmod(host, 0o777)
        # checks BEFORE camserver is touched: an impossible series must fail now, not after hours
        frame = float(self._accumulate.get() or 0)
        series = None
        if frame > 0 and count_time >= 2 * frame:
            n = int(round(count_time / frame))
            if n > MAX_FRAMES:
                raise ValueError(
                    f"{self.name}: {count_time:g} s in frames of {frame:g} s = {n} frames; camserver allows at most "
                    f"{MAX_FRAMES} frames per series -> use pilatus_saxs.accumulate({count_time / MAX_FRAMES:.3g}) "
                    f"or longer")
            series = (n, count_time / n)
        self._check_disk(host, series)
        self._camserver = Camserver(self._host, self._port)
        cam = self._camserver
        period = cam.command("expperiod").rsplit(":", 1)[-1].split()[0]  # "15 OK Exposure period set to: 1.0015 sec."
        self._camserver_defaults = ("nimages 1", "setackint 0", f"expperiod {period}")
        if series is not None:
            n, dt = series
            self._series = series
            cam.command(f"exptime {dt}")
            cam.command(f"expperiod {dt + SERIES_GAP}")
            cam.command(f"nimages {n}")
            cam.command("setackint 1")
        else:
            self._series = None
            cam.command("nimages 1")  # in case a series was left set (e.g. crash)
            cam.command("setackint 0")
            cam.command(f"exptime {count_time}")

    def _trigger(self):
        if self._exposing is not None and not self._exposing.ready():
            raise RuntimeError(f"{self.name}: previous exposure still running")
        self._exposing = gevent.spawn(self._expose_one)

    def _wait_ready(self):
        if self._exposing is not None:
            self._exposing.get()

    def _expose_one(self):
        number = self._file_number.get()
        self._file_number.set(number + 1)
        local, host = self._folders()
        filename = f"{self.name}_{number:06d}.edf"
        cam = self._camserver
        if self._series is not None:
            self._expose_series(number, filename, local, host)
            return
        cam.command(f"exposure {os.path.join(local, filename)}")  # "15 OK Starting ..."
        done = cam.read_message(timeout=self._count_time + 30)  # "7 OK <path>"
        if not done.startswith("7 OK"):
            raise RuntimeError(f"{self.name}: unexpected camserver answer {done!r}")
        host_path = os.path.join(host, filename)
        image = self._read_image(host_path)
        if self._delete_edf:
            try:
                os.remove(host_path)
            except OSError as e:
                print(f"{self.name}: could not delete {host_path}: {e}")
        self._store(image, number, self._count_time)

    def _store(self, image, number, exposure):
        n = self._names
        positive = numpy.clip(image, 0, None)  # gaps (-1) and bad pixels (-2) count as 0, as SPEC
        row = {n["image"]: image, n["sum"]: float(positive.sum()), n["filenumber"]: float(number),
               n["exptime"]: float(exposure)}
        row.update(self.roi_counters.sums(positive))
        self._data.append(row)

    def _expose_series(self, number, filename, local, host):
        """One point as a series of n frames (camserver nimages): sum them, show the running sum in Flint."""
        n_frames, dt = self._series
        # camserver numbers a series by appending _00000, _00001, ... - but if the name ENDS in digits it
        # counts those up instead (..._000491 -> 000492, ...), colliding with the next points: end in "_acc"
        base = os.path.splitext(filename)[0] + "_acc"
        frames = [os.path.join(host, f"{base}_{i:05d}.edf") for i in range(n_frames)]
        cam = self._camserver
        self._series_running = True
        self._last_running_emit = None  # the first sum of each point is always sent
        self._partial = {"frames": frames, "log": os.path.join(host, base + ".log"), "n": n_frames, "dt": dt,
                         "k": 0, "number": number, "start": datetime.datetime.now().astimezone()}
        cam.command(f"exposure {os.path.join(local, base + '.edf')}")  # "15 OK Starting ..."
        rows, columns = self._counters[self._names["image"]].shape
        total = numpy.zeros((rows, columns), dtype=numpy.int64)
        mask = None
        done = False  # camserver sent "7 OK" (series finished)
        deadline = time.monotonic() + n_frames * (dt + SERIES_GAP) + 30
        next_frame = 0
        while next_frame < n_frames:
            if not done:
                try:
                    msg = cam.read_message(timeout=0.5)  # "15 OK img k" while running, "7 OK <last file>" at the end
                    if " ERR" in msg.split("\n")[0]:
                        raise RuntimeError(f"{self.name}: camserver: {msg!r}")
                    done = msg.startswith("7 OK")
                except TimeoutError:
                    pass
            # read every frame that is complete (all of them once camserver has finished)
            while next_frame < n_frames and (done or self._complete(frames[next_frame])):
                frame = self._read_image(frames[next_frame])
                if mask is None:
                    mask = frame < 0  # gaps (-1) / bad pixels (-2): same in every frame
                    marks = frame[mask]
                total += numpy.clip(frame, 0, None)
                if self._delete_edf:  # in the sum now: free the detector PC disk at once (long series)
                    try:
                        os.remove(frames[next_frame])
                    except OSError:
                        pass
                next_frame += 1
                self._partial.update(total=total, mask=mask, marks=marks, k=next_frame)
                self._show_accumulation(total, next_frame, n_frames, dt)
                self._emit_running(total, mask, marks, next_frame * dt, last=next_frame == n_frames)
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.name}: series not finished ({next_frame}/{n_frames} frames)")
        if not done:  # all frames read before the final message: take it, it must not answer the next command
            msg = cam.read_message(timeout=30)
            if " ERR" in msg.split("\n")[0]:
                raise RuntimeError(f"{self.name}: camserver: {msg!r}")
        image = total.astype(numpy.int32)
        image[mask] = marks  # keep the -1 / -2 marks of the single images
        if self._delete_edf:
            for path in frames + [os.path.join(host, base + ".log")]:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._store(image, number, n_frames * dt)
        self._series_running = False
        self._partial = None

    def _finish_partial(self):
        """Interrupted accumulated exposure (Ctrl-C, error): the sum of the complete frames becomes this point's
        image (stored like a complete point and sent to the scan by the acquisition slave at stop), with its real
        exposure in <prefix>_exptime. The frame files are deleted in any case."""
        part, self._partial = self._partial, None
        k, n, dt = part["k"], part["n"], part["dt"]
        try:
            if k == 0:
                print(f"{self.name}: accumulation interrupted before the first frame was complete - no image")
                return
            image = part["total"].astype(numpy.int32)
            image[part["mask"]] = part["marks"]
            self._store(image, part["number"], k * dt)
            # the sum of the complete frames is the last running sum of this point (even within the period)
            self._emit_running(part["total"], part["mask"], part["marks"], k * dt, last=True)
            print(f"{self.name}: accumulation interrupted after {k}/{n} frames - real exposure {k * dt:g} s "
                  f"of {n * dt:g} s (stored as the point's image, {self._names['exptime']} = {k * dt:g})")
        except Exception as exc:
            print(f"WARNING: {self.name}: could not store the interrupted accumulation: {exc}")
        finally:
            if self._delete_edf:
                for p in part["frames"] + [part["log"]]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            self._series_running = False

    def _emit_running(self, total, mask, marks, exposure, last=False):
        """Running sum after a frame to the running-sum scan channels (Redis only), at most every
        running_sum_period() seconds; the first and the last sum of a point are always sent. Never stops the
        acquisition."""
        if not self._running_channels:
            return
        now = time.monotonic()
        period = float(self._running_period.get() or 0)
        if not last and self._last_running_emit is not None and now - self._last_running_emit < period:
            return
        self._last_running_emit = now
        try:
            image = total.astype(numpy.int32)
            image[mask] = marks  # gap/bad pixels -1/-2 as in the saved image
            image_channel, exposure_channel = self._running_channels
            image_channel.emit(image)
            exposure_channel.emit(float(exposure))
        except Exception as exc:
            if not getattr(self, "_running_warned", False):
                print(f"WARNING: {self.name}: running sum not sent to the scan: {exc}")
                self._running_warned = True

    def _running_period_text(self):
        period = float(self._running_period.get() or 0)
        return f"every {period:g} s (and the last sum of each point)" if period > 0 else "after every frame"

    def running_sum_period(self, seconds=None):
        """How often the running sum of an accumulated exposure goes to Redis (channels saxs_running_sum /
        saxs_running_exptime, for on-the-fly analysis): running_sum_period(10) -> at most every 10 s,
        running_sum_period(0) -> after every frame, running_sum_period() -> show. Each sum is a full image
        (1.2 MB): with short frames, every frame would fill Redis quickly. Flint's live sum is not affected."""
        if seconds is not None:
            seconds = float(seconds)
            if seconds < 0:
                raise ValueError("period must be >= 0 s")
            self._running_period.set(seconds)
        print(f"{self.name} running sum to Redis: {self._running_period_text()}")

    def _series_planned(self, count_time):
        """Will a count of count_time be an accumulated exposure (series)?"""
        frame = float(self._accumulate.get() or 0)
        return bool(count_time) and frame > 0 and count_time >= 2 * frame

    def _check_disk(self, host, series):
        """Enough free space in the image folder of the detector PC (seen over NFS)? Frames of a series are
        deleted one by one once summed (delete_edf_after_read), so only a few are on disk at a time; without
        deletion all of them stay."""
        rows, columns = self._counters[self._names["image"]].shape
        frame_bytes = rows * columns * 4 + 4096  # int32 EDF + header
        if series is None or self._delete_edf:
            needed = 20 * frame_bytes
        else:
            needed = series[0] * frame_bytes
        needed += MIN_FREE_BYTES
        stat = os.statvfs(host)
        free = stat.f_bavail * stat.f_frsize
        if free < needed:
            raise RuntimeError(
                f"{self.name}: only {free / 1e9:.2f} GB free in {host} (detector PC); this count needs "
                f"{needed / 1e9:.2f} GB - free space on the detector PC first")

    def _complete(self, path):
        rows, columns = self._counters[self._names["image"]].shape
        try:
            return os.path.getsize(path) >= rows * columns * 4
        except OSError:
            return False

    def _show_accumulation(self, total, k, n_frames, dt):
        """Running sum to Flint, in the background (display only, never stops the acquisition)."""
        try:
            from xeuss_bliss.flint_display import show_accumulation

            text = f"{self.name}: frame {k}/{n_frames}, {k * dt:.1f} of {n_frames * dt:.1f} s"
            show_accumulation(self, total.astype(numpy.float32), text, first=(k == 1))
        except Exception:
            pass

    def _accumulate_text(self):
        frame = float(self._accumulate.get() or 0)
        return f"frames of ~{frame:g} s for counts >= {2 * frame:g} s" if frame > 0 else "off"

    def accumulate(self, frame_time=None):
        """Accumulated exposure: accumulate(10) -> counts >= 20 s as ~10 s frames summed, with a live sum in
        Flint; accumulate(0) -> off; accumulate() -> show."""
        if frame_time is not None:
            frame_time = float(frame_time or 0)
            if 0 < frame_time < 0.5:
                raise ValueError("frame time must be >= 0.5 s (or 0 for off)")
            self._accumulate.set(frame_time)
        print(f"{self.name} accumulate: {self._accumulate_text()}")

    def _read_image(self, path, timeout=15.0):
        """Wait until the EDF file is visible over NFS with its full size, then read it."""
        rows, columns = self._counters[self._names["image"]].shape
        expected = rows * columns * 4
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.path.getsize(path) >= expected:
                    data = fabio.open(path).data
                    if data.shape == (rows, columns):
                        return data.astype(numpy.int32, copy=False)
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.name}: image {path} not readable within {timeout} s")
            gevent.sleep(0.2)

    def _stop(self):
        if self._exposing is not None and not self._exposing.ready():
            self._exposing.kill()
            if self._camserver is not None and not self._series_running:
                try:
                    self._camserver.send("k")  # abort the running single exposure
                except OSError:
                    pass
        self._exposing = None
        if self._series_running and self._partial is not None:
            completed = self._abort_series()  # also after an error inside the series: camserver may still run it
            self._collect_frames(completed)
            self._finish_partial()
        series = self._series is not None
        if self._camserver is not None:
            self._camserver.close()  # releases control of camserver (as SPEC does)
            self._camserver = None
        if series:
            self._restore_camserver()
        self._series = None

    def _abort_series(self):
        """Stop a running camserver series ("k") and wait for its end. camserver ends the frame in progress
        early (written as a short frame) and answers "13 ERR kill", "7 ERR", "7 OK <path of that frame>".
        Returns the number of COMPLETE frames (the frames before the cut one)."""
        cam = self._camserver
        part = self._partial
        if cam is None:
            return part["k"]
        try:
            cam.send("k")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                msg = cam.read_message(timeout=max(0.1, deadline - time.monotonic()))
                if msg.startswith("7 OK"):
                    m = re.search(r"_(\d{5})\.edf", msg)
                    return int(m.group(1)) if m else part["k"]  # index of the cut frame = complete frames
        except Exception as exc:
            print(f"{self.name}: no clean end of the camserver series after abort ({exc})")
        return part["k"]

    def _collect_frames(self, completed):
        """Add the complete frames of an interrupted series that were not summed yet."""
        part = self._partial
        try:
            while part["k"] < min(completed, part["n"]):
                frame = self._read_image(part["frames"][part["k"]])
                if part.get("mask") is None:
                    part["mask"] = frame < 0
                    part["marks"] = frame[part["mask"]]
                    part["total"] = numpy.zeros(frame.shape, dtype=numpy.int64)
                part["total"] += numpy.clip(frame, 0, None)
                part["k"] += 1
        except Exception as exc:
            print(f"{self.name}: could not read all complete frames ({exc}); saving {part['k']} frames")

    def _restore_camserver(self):
        """Single images again for the next client (SPEC): nimages 1, setackint 0, previous expperiod.
        camserver puts back the nimages of a connection's last exposure when that connection closes (tested
        2026-10-21: "nimages 1" on the series connection is lost after close) -> set on a NEW connection,
        after the series connection is closed, and check on another one."""
        if self._camserver_defaults is None:
            return
        for attempt in range(2):
            try:
                gevent.sleep(0.3)
                cam = Camserver(self._host, self._port)
                try:
                    for command in self._camserver_defaults:
                        cam.command(command)
                finally:
                    cam.close()
                gevent.sleep(0.3)
                cam = Camserver(self._host, self._port)
                try:
                    answer = cam.command("nimages")
                finally:
                    cam.close()
                if answer.rstrip().endswith(": 1"):
                    return
            except Exception as exc:
                answer = f"error: {exc}"
        print(f"WARNING: {self.name}: camserver may still be in series mode ({answer!r}); SPEC needs single "
              f"images - run pilatus_saxs._restore_camserver() or restart camserver (Dectris0)")
