"""Small fixes applied inside the Flint process (xeuss20); started by xeuss_bliss.flint_main.

Side profiles of image plots (silx ImageView) in log scale: silx sets the value axis of a side profile to
the data range after every new image and every zoom; a profile with zeros (module gaps, empty rows) then
starts the log axis at 1e-38 and the curve is squeezed to the edge (seen in the "pilatus_saxs accumulating"
tab, 2026-10-21). install() adds a correction to that limit update: on a log axis the range is the smallest
positive .. largest value of the drawn profile (x 1/2 .. x 2). Linear axes are unchanged.
"""

import numpy


def _log_limits(side):
    from silx.gui import qt
    from silx.gui.utils import blockSignals

    horizontal = side._direction == qt.Qt.Horizontal
    axis = side.getYAxis() if horizontal else side.getXAxis()
    if axis.getScale() != "log":
        return
    curve = side.getCurve("profile") or side.getCurve("profilesum")
    if curve is None:
        return
    data = numpy.asarray(curve.getYData(copy=False) if horizontal else curve.getXData(copy=False), dtype=float)
    positive = data[numpy.isfinite(data) & (data > 0)]
    if positive.size == 0:
        return
    with blockSignals(axis):
        axis.setLimits(float(positive.min()) / 2, float(positive.max()) * 2)
    side.replot()


def install():
    from silx.gui.plot.ImageView import _SideHistogram

    original = _SideHistogram._SideHistogram__updateLimits
    if getattr(original, "_xeuss_patched", False):
        return

    def update_limits(self):
        original(self)
        _log_limits(self)

    update_limits._xeuss_patched = True
    _SideHistogram._SideHistogram__updateLimits = update_limits
