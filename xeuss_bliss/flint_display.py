"""Flint display helpers for the Xeuss 2.0 (xeuss20).

open_flint()          start Flint at session start (called from the setup; silent if it cannot)
FlintImagePreset      chooses Flint's tab for every scan: scans WITHOUT a moving motor (ct, sct,
                      loopscan, timescan) that record a Pilatus image -> the image tab
                      ("pilatus_saxs:saxs_image", instead of the Count tab); motor scans (ascan,
                      dscan, ...) -> the curve tab (Flint itself does not switch back to it).
flint_image_tab(False / True)   switch the preset off / on
show_rois()           draw the Pilatus ROI counters as rectangles in Flint's image tab; done by the
                      preset for every scan with a Pilatus image and after every ROI change
                      (pilatus_saxs.roi_counters[...] = ...), so it is rarely needed by hand
The preset also brings the Flint window to the front at every scan (raise only, no keyboard focus).

The preset never starts Flint and never disturbs a scan: errors are only logged.
ROI drawing: Flint draws ROIs only for Lima devices, so the rectangles are plain silx plot items
(addShape / addMarker on the image plot through Flint's run_method RPC). Flint keeps them across scans;
they are redrawn anyway (Flint restart, ROI changes). The RPC cannot send the created silx item back,
so the "Unknown type" TypeError of these calls is expected and ignored.
"""

import os

import gevent
import numpy

from bliss.common.logtools import log_warning
from bliss.scanning.chain import ChainPreset

PRESET_NAME = "xeuss_flint_image"
ROI_COLORS = {"direct": "#ff3030", "spec": "#30c0ff"}
OTHER_COLORS = ["#ffb000", "#40e040", "#ff40ff", "#ffffff"]
_drawn = {}  # Flint image device name -> ROI names drawn there (to remove deleted ROIs)


def _flint():
    from flint.client.proxy import get_flint

    return get_flint(creation_allowed=False, mandatory=False)


def open_flint():
    """Start Flint now (session start); prints a warning instead of failing."""
    try:
        from flint.client.proxy import get_flint

        get_flint()
        print("Flint started")
    except Exception as exc:
        print(f"WARNING: Flint not started: {exc}")


def _image_detectors(chain):
    """Flint device names of the Pilatus images in this scan, whether a motor moves, and the Pilatus
    controllers of the scan."""
    from xeuss_bliss.pilatus_camserver import PilatusCamserver

    motor = False
    images = []
    controllers = []
    for node in chain.nodes_list:
        # every master of bliss.scanning.acquisition.motor moves motors (LinearStepTriggerMaster,
        # VariableStepTriggerMaster used by dscan/ascan in 2.4, MotorMaster, mesh, ...); class
        # checks missed VariableStepTriggerMaster, which is not a _StepTriggerMaster subclass
        if type(node).__module__ == "bliss.scanning.acquisition.motor":
            motor = True
        device = getattr(node, "device", None)
        if isinstance(device, PilatusCamserver):
            images.append(f"{device.name}:{device._names['image']}")
            controllers.append(device)
    return motor, images, controllers


def _run(flint, plot_id, method, args, kwargs):
    try:
        flint.run_method(plot_id, method, args, kwargs)
    except TypeError as exc:
        if "Unknown type" not in str(exc):  # the returned silx item cannot be sent back: expected
            raise


def _draw_rois(flint, controller, retries=10, plot_id=None):
    """Draw the ROIs of one PilatusCamserver on its Flint image plot (once the plot exists), or on plot_id."""
    image = f"{controller.name}:{controller._names['image']}"
    for _ in range(retries if plot_id is None else 0):
        plot_id = flint.get_live_plot_detector(image, plot_type="image", create=False)
        if plot_id is not None:
            break
        gevent.sleep(0.3)
    if plot_id is None:
        return False
    rois = {name: controller.roi_counters[name] for name in controller.roi_counters}
    for name in _drawn.get(plot_id, set()) - set(rois):
        _run(flint, plot_id, "remove", [f"xeuss_roi:{name}"], {"kind": "item"})
        _run(flint, plot_id, "remove", [f"xeuss_roi_label:{name}"], {"kind": "marker"})
    others = iter(OTHER_COLORS * 10)
    for name, (x, y, w, h) in rois.items():
        color = ROI_COLORS.get(name) or next(others)
        # pixel (column x, row y) covers x..x+1, y..y+1 in Flint's image coordinates
        _run(flint, plot_id, "addShape", [[x, x + w], [y, y + h]],
             {"legend": f"xeuss_roi:{name}", "shape": "rectangle", "color": color, "fill": False,
              "overlay": False, "linewidth": 1.5})
        _run(flint, plot_id, "addMarker", [x + w + 1, y + h / 2],
             {"legend": f"xeuss_roi_label:{name}", "text": name, "color": color, "symbol": ""})
    _drawn[plot_id] = set(rois)
    return True


VIEW_DIR = os.path.expanduser("~/bliss/local/flint")  # saved view (colormap, mask) of the accumulating tabs
_COLORMAP_GETTERS = ("getName", "getNormalization", "getAutoscaleMode", "getVRange")
_view_masks = {}  # detector name -> mask (bool array) drawn in its accumulating tab, or None
_view_colormaps = {}  # detector name -> last saved colormap dict


def _view_file(controller):
    return os.path.join(VIEW_DIR, f"{controller.name}_accumulation_view.npz")


def _load_view(controller):
    """Saved colormap (dict) and mask (bool array or None) of the accumulating tab, or (None, None)."""
    try:
        with numpy.load(_view_file(controller), allow_pickle=False) as f:
            colormap = {k: f[k].tolist() for k in ("name", "normalization", "autoscale") if k in f}
            if "vrange" in f:
                vrange = f["vrange"].tolist()
                colormap["vrange"] = [None if v != v else v for v in vrange]  # nan = automatic
            mask = f["mask"].astype(bool) if "mask" in f and f["mask"].size else None
        return colormap, mask
    except Exception:
        return None, None


def _save_view(controller, colormap, mask):
    try:
        os.makedirs(VIEW_DIR, exist_ok=True)
        data = {"mask": numpy.zeros(0, bool) if mask is None else mask.astype(bool)}
        if colormap:
            data.update(name=colormap["name"], normalization=colormap["normalization"],
                        autoscale=colormap["autoscale"],
                        vrange=numpy.array([numpy.nan if v is None else v for v in colormap["vrange"]], float))
        numpy.savez(_view_file(controller), **data)
    except Exception as exc:
        log_warning(controller, "accumulating tab view not saved: %s", exc)


def _read_colormap(flint, plot_id, path="getColormap()"):
    values = [flint.run_method(plot_id, f"{path}.{getter}", [], {}) for getter in _COLORMAP_GETTERS]
    return dict(zip(("name", "normalization", "autoscale", "vrange"), values))


def _apply_colormap(flint, plot_id, colormap):
    flint.run_method(plot_id, "getColormap().setName", [colormap["name"]], {})
    flint.run_method(plot_id, "getColormap().setNormalization", [colormap["normalization"]], {})
    flint.run_method(plot_id, "getColormap().setAutoscaleMode", [colormap["autoscale"]], {})
    vmin, vmax = colormap.get("vrange") or (None, None)
    flint.run_method(plot_id, "getColormap().setVRange", [vmin, vmax], {})


def show_accumulation(controller, image, text, first=False):
    """Running sum of an accumulated exposure (PilatusCamserver.accumulate) in its own Flint tab
    "<detector> accumulating" (side profiles in log scale); in the background, errors only logged.
    first: select the tab, set it up, draw the ROIs.

    The tab remembers its view: the mask drawn with the tab's mask tool (silx keeps it on the image from frame
    to frame and leaves it out of the profiles) and the colormap are saved to ~/bliss/local/flint/ when they
    change, and given back to a newly created tab (first use, tab closed, Flint restarted; without a saved view
    the colormap of the live image tab is used instead of grey/linear). Display only: data are not masked."""
    controller._accum_text = text

    def update():
        try:
            from flint.client import plots

            flint = _flint()
            if flint is None:
                return
            unique = f"{controller.name}_accumulation"
            existed = flint.plot_exists(unique)
            plot = flint.get_plot(plots.ImageView, name=f"{controller.name} accumulating",
                                  unique_name=unique, selected=first)
            pid = plot.plot_id
            name = controller.name
            restore_mask = None
            if not existed:  # new tab: saved view, else the colormap of the live image tab
                colormap, restore_mask = _load_view(controller)
                if colormap is None:
                    try:  # Flint raises if the live image tab does not exist (yet)
                        live = flint.get_live_plot_detector(f"{name}:{controller._names['image']}",
                                                            plot_type="image", create=False)
                        if live is not None:
                            colormap = _read_colormap(flint, live, "defaultColormap()")
                    except Exception:
                        colormap = None
                if colormap:
                    _apply_colormap(flint, pid, colormap)
                _view_masks[name] = restore_mask
                _view_colormaps[name] = colormap
            if first:
                plot.yaxis_direction = "down"  # as Flint's live image
                plot.side_histogram_displayed = True
                # side profiles (row / column sums) in log scale: silx ImageView side plots, values on y (below
                # the image) and on x (right of it); their log range is fixed in Flint by xeuss_bliss.flint_patches
                flint.run_method(pid, "_histoHPlot.getYAxis().setScale", ["log"], {})
                flint.run_method(pid, "_histoVPlot.getXAxis().setScale", ["log"], {})
            plot.set_data(image)
            if restore_mask is not None and restore_mask.shape == image.shape:
                flint.run_method(pid, "getImage().setMaskData", [restore_mask.astype(numpy.uint8)], {})
            elif existed:  # mask drawn / changed / cleared since the last frame, colormap changed: save the view
                drawn = flint.run_method(pid, "getImage().getMaskData", [], {})
                drawn = None if drawn is None or not numpy.any(drawn) else numpy.asarray(drawn) != 0
                previous = _view_masks.get(name)
                colormap = _read_colormap(flint, pid)
                changed = (drawn is None) != (previous is None) or (
                    drawn is not None and not numpy.array_equal(drawn, previous))
                if changed or colormap != _view_colormaps.get(name):
                    _view_masks[name] = drawn
                    _view_colormaps[name] = colormap
                    _save_view(controller, colormap, drawn)
            plot.title = text
            if first:
                plot.focus()
                _draw_rois(flint, controller, plot_id=pid)
        except Exception as exc:
            log_warning(controller, "Flint accumulation display: %s", exc)

    gevent.spawn(update)


def show_rois(controller=None):
    """Draw the Pilatus ROI counters in Flint's image tab (all Pilatus detectors, or one)."""
    from bliss.config.static import get_config

    flint = _flint()
    if flint is None:
        print("Flint is not running")
        return
    controllers = [controller] if controller is not None else [get_config().get("pilatus_saxs")]
    for ctrl in controllers:
        if not _draw_rois(flint, ctrl, retries=1):
            print(f"{ctrl.name}: no image plot in Flint yet (it appears with the first scan with an image)")


def roi_changed(controller):
    """Called by RoiCounters after a change: redraw in the background, never fails."""

    def redraw():
        try:
            flint = _flint()
            if flint is not None:
                _draw_rois(flint, controller, retries=1)
        except Exception as exc:
            log_warning(controller, "Flint ROI display: %s", exc)

    gevent.spawn(redraw)


class FlintImagePreset(ChainPreset):
    def __init__(self):
        super().__init__()
        self._images = []
        self._curve = False
        self._pilatus = []
        self._task = None

    def prepare(self, chain):
        try:
            motor, images, controllers = _image_detectors(chain)
        except Exception as exc:
            log_warning(self, "Flint image tab: %s", exc)
            motor, images, controllers = True, [], []
        self._images = [] if motor else images
        self._curve = motor
        self._pilatus = controllers

    def start(self, chain):
        if any(c._series for c in self._pilatus):
            return  # accumulated exposure: its own tab is shown from the first frame; live image at the end
        if self._images or self._curve:
            # Flint chooses its own tab when the scan starts; switch a moment later
            self._task = gevent.spawn(self._focus, delay=1.0)

    def stop(self, chain):
        if self._images or self._curve or self._pilatus:
            if self._task is not None:
                self._task.kill()
            gevent.spawn(self._focus, delay=0.2)  # once more at the end (short scans)

    def _focus(self, delay):
        try:
            gevent.sleep(delay)
            flint = _flint()
            if flint is None:
                return
            for ctrl in self._pilatus:  # ROI rectangles, also for motor scans with images
                try:
                    _draw_rois(flint, ctrl)
                except Exception as exc:
                    log_warning(self, "Flint ROI display: %s", exc)
            for _ in range(10):
                if self._curve:
                    plot_id = flint.get_default_live_scan_plot("curve")
                else:
                    plot_id = flint.get_live_plot_detector(self._images[0], plot_type="image", create=False)
                if plot_id is not None:
                    flint.set_plot_focus(plot_id)
                    # Flint window above the other windows, WITHOUT the keyboard focus (typing stays in the BLISS
                    # terminal); needs the KWin rule "focus stealing prevention: none" for flint (kwinrulesrc [1])
                    flint.run_method(plot_id, "window().raise_", [], {})
                    return
                gevent.sleep(0.3)
        except Exception as exc:  # never disturb the scan
            log_warning(self, "Flint image tab: %s", exc)


def flint_image_tab(on=None):
    """Switch Flint to the image tab for scans without motor: flint_image_tab(True/False)."""
    from bliss.common.scans import DEFAULT_CHAIN

    active = PRESET_NAME in DEFAULT_CHAIN._presets
    if on is None:
        print(f"Flint tab choice (image for counts, curve for motor scans): {'ON' if active else 'OFF'}")
        return active
    if on and not active:
        DEFAULT_CHAIN.add_preset(FlintImagePreset(), name=PRESET_NAME)
    elif not on and active:
        DEFAULT_CHAIN.remove_preset(name=PRESET_NAME)
    print(f"Flint tab choice (image for counts, curve for motor scans): {'ON' if on else 'OFF'}")
    return bool(on)
