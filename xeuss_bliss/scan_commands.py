"""Shell commands working on the last scan, like Bliss's cen() / fwhm().

    from xeuss_bliss.scan_commands import deriv, deriv_fit, deriv_fwhm, shad_fit, step_fit, goto_step

From xrd3003_bliss (github.com/lapkindm/xrd3003_bliss, commit 23e6e6d), included here 2026-10-21.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy

from bliss.common.logtools import log_warning

PLOT_NAME = "deriv"


class Derivative(NamedTuple):
    x: numpy.ndarray  # the scanned positions
    y: numpy.ndarray  # d(counter)/d(axis) at those positions
    counter: str
    axis: str


def derivative(x, y) -> tuple[numpy.ndarray, numpy.ndarray]:
    """Three-point derivative dy/dx at every measured point (numpy.gradient):
    second-order central differences inside, valid for uneven spacing (the
    measured positions are used), one-sided three-point formulas at both
    ends. No smoothing."""
    x = numpy.asarray(x, dtype=float)
    y = numpy.asarray(y, dtype=float)
    if x.ndim != 1 or y.ndim != 1:
        raise ValueError("x and y must be 1-D arrays")
    n = min(len(x), len(y))  # an aborted scan can have one more position
    if n < 3:
        raise ValueError("at least 3 points are needed for a three-point derivative")
    x, y = x[:n], y[:n]
    if numpy.any(numpy.diff(x) == 0):
        raise ValueError("repeated positions in the scan: the derivative is undefined there")
    return x, numpy.gradient(y, x, edge_order=2)


class DerivativeFit(NamedTuple):
    """Gaussian fitted to a derivative (a falling edge gives a negative height)."""

    center: float
    fwhm: float  # corrected for the widening by the three-point derivative
    height: float
    background: float
    fwhm_fit: float  # as fitted to the derivative (wider by about h**2 / 3 in variance)
    counter: str
    axis: str
    x: numpy.ndarray  # the derivative that was fitted
    y: numpy.ndarray

    def curve(self, x) -> numpy.ndarray:
        """The fitted Gaussian (as fitted, i.e. with fwhm_fit)."""
        sigma = self.fwhm_fit / FWHM_PER_SIGMA
        x = numpy.asarray(x, dtype=float)
        return self.background + self.height * numpy.exp(-0.5 * ((x - self.center) / sigma) ** 2)


FWHM_PER_SIGMA = 2 * numpy.sqrt(2 * numpy.log(2))


def fit_derivative(x, y) -> tuple[float, float, float, float, float]:
    """Gaussian fit of the three-point derivative of y(x), with Flint's own
    fit (flint.utils.mathutils.fit_gaussian). It fits upward peaks, so the
    derivative is flipped when its largest extreme is negative (a falling
    edge, e.g. a z or y scan into the sample).

    A central difference over x-h..x+h is the true derivative averaged over
    a window 2h wide, which widens the peak: sigma_fit**2 ~ sigma**2 + h**2/3.
    The returned FWHM is corrected for it (h = median step); the fitted one
    is returned too. Error of the corrected FWHM (smooth erf edge): < 0.5 %
    for a step up to 0.75 sigma, 1.5 % at 1 sigma, 3 % at 1.2 sigma, 8 % at
    1.5 sigma (uncorrected: 9, 17, 24, 39 %). A warning is printed when the
    step is larger than sigma; an edge sharper than that is refused. Returns (center, fwhm, height, background, fwhm_fit),
    height and background with the derivative's sign."""
    from flint.utils.mathutils import fit_gaussian

    x, d = derivative(x, y)
    sign = -1.0 if abs(d.min()) > abs(d.max()) else 1.0
    try:
        fit = fit_gaussian(x, sign * d)
    except Exception as exc:
        raise RuntimeError(f"Gaussian fit of the derivative failed: {exc}") from exc
    if not (numpy.isfinite(fit.fwhm) and fit.fwhm > 0 and x.min() <= fit.pos_x <= x.max()):
        raise RuntimeError(f"Gaussian fit of the derivative failed (centre {fit.pos_x:g}, FWHM {fit.fwhm:g})")
    h = float(numpy.median(numpy.abs(numpy.diff(x))))
    var = (fit.fwhm / FWHM_PER_SIGMA) ** 2 - h ** 2 / 3
    if var <= 0:
        raise RuntimeError(f"the edge is sharper than the scan step ({h:g}): "
                           f"FWHM below {FWHM_PER_SIGMA * h / math.sqrt(3):g}, use a finer scan")
    fwhm = FWHM_PER_SIGMA * math.sqrt(var)
    if h > math.sqrt(var):
        log_warning("deriv", "scan step %g is larger than the fitted sigma %g: the FWHM is only "
                    "accurate to a few %%, use a finer scan", h, math.sqrt(var))
    return float(fit.pos_x), float(fwhm), sign * float(fit.height), sign * float(fit.background), float(fit.fwhm)


class ShadowFit(NamedTuple):
    """Two straight lines fitted to the flanks of a triangular (shadow) scan.
    "left" / "right" are below / above the maximum in motor position."""

    peak: float  # motor position of the highest point
    apex: float  # where the two fitted lines cross
    height: float  # line value at the apex
    slope_left: float  # counts per motor unit, signed
    slope_right: float
    intercept_left: float  # line: y = slope * x + intercept
    intercept_right: float
    n_left: int  # points used for each line
    n_right: int
    counter: str
    axis: str


def fit_shadow(x, y, levels: tuple[float, float] | None = None):
    """Fit straight lines to both sides of the maximum of y(x).

    The points are taken in order of x. The highest point lies on only one of
    the two lines when the true apex falls between scan points, and would
    bias the other one (spec's shad_scan_1 put it on both sides): the lines
    are first fitted without it, then every point is assigned to the side of
    the crossing point (apex) it lies on, and both lines are fitted again.
    ``levels=(low, high)`` keeps only points with low*max <= y <= high*max
    (e.g. (0.2, 0.9) leaves out a flat baseline and a rounded tip).
    Returns (peak, apex, height, slope_left, slope_right, intercept_left,
    intercept_right, n_left, n_right).
    """
    x = numpy.asarray(x, dtype=float)
    y = numpy.asarray(y, dtype=float)
    n = min(len(x), len(y))  # an aborted scan can have one more position
    order = numpy.argsort(x[:n], kind="stable")
    x, y = x[:n][order], y[:n][order]
    i = int(numpy.argmax(y))
    ymax = y[i]
    if levels is not None:
        low, high = levels
        if not 0 <= low < high <= 1:
            raise ValueError("levels must be (low, high) with 0 <= low < high <= 1")
        usable = (y >= low * ymax) & (y <= high * ymax)
    else:
        usable = numpy.ones(n, dtype=bool)

    def lines(left, right):
        out = []
        for name, side in (("left", left), ("right", right)):
            side = side & usable
            if side.sum() < 2:
                raise ValueError(f"not enough points on the {name} side of the maximum "
                                 f"(at {x[i]:g}) for a straight line")
            out.append((numpy.polyfit(x[side], y[side], 1), int(side.sum())))
        return out

    index = numpy.arange(n)
    (lf, _), (rf, _) = lines(index < i, index > i)  # without the highest point
    apex0 = (rf[1] - lf[1]) / (lf[0] - rf[0]) if lf[0] != rf[0] else x[i]
    (lf, n_l), (rf, n_r) = lines(x <= apex0, x > apex0)  # every point on its own side
    (m_l, b_l), (m_r, b_r) = lf, rf
    apex = (b_r - b_l) / (m_l - m_r) if m_l != m_r else float("nan")
    return (float(x[i]), float(apex), float(m_l * apex + b_l), float(m_l), float(m_r),
            float(b_l), float(b_r), n_l, n_r)


class StepFit(NamedTuple):
    """Error-function step fitted to a scan: a Gaussian beam cut by an edge,
    y = low + (high - low) * erfc((x - position) / (sqrt(2) sigma)) / 2 for a
    falling step (high before the edge), mirrored for a rising one."""

    position: float  # x of the half-height point between the two levels
    fwhm: float  # beam width, 2.355 sigma
    high: float  # fitted level on the bright side
    low: float  # fitted level on the dark side
    position_err: float  # one standard deviation, from the fit
    fwhm_err: float
    falling: bool  # high before the edge (e.g. z scan into the sample)
    x_min: float  # range of the points that were fitted
    x_max: float
    counter: str
    axis: str

    def curve(self, x) -> numpy.ndarray:
        from scipy.special import erfc

        sigma = self.fwhm / FWHM_PER_SIGMA
        u = (numpy.asarray(x, dtype=float) - self.position) / (math.sqrt(2) * sigma)
        return self.low + (self.high - self.low) * 0.5 * erfc(u if self.falling else -u)


def _step_window(x, y, falling: bool) -> tuple[int, int]:
    """Points to fit: everything on the bright side, and the dark side only
    while the signal stays at the bottom: it ends where the signal climbs
    more than 2 % of the step height above the lowest dark value so far (a z
    scan has the reflection peak after the edge, which a step model cannot
    follow)."""
    d = numpy.gradient(y, x)
    i_edge = int(numpy.argmin(d) if falling else numpy.argmax(d))
    span = y.max() - y.min()
    dark = y.min() + 0.1 * span  # reached the dark side
    rise = 0.02 * span  # climbing again

    def follow(start, step):
        j = start
        while 0 <= j + step < len(y) and y[j] > dark:
            j += step
        lowest = y[j]
        while 0 <= j + step < len(y) and y[j + step] <= min(dark, lowest + rise):
            j += step
            lowest = min(lowest, y[j])
        return j

    return (0, follow(i_edge, +1)) if falling else (follow(i_edge, -1), len(y) - 1)


def fit_step(x, y, xrange: tuple[float, float] | None = None):
    """Fit an error-function step to y(x). The direction (falling or rising)
    is taken from the steepest change. Without ``xrange`` the dark side is
    fitted only until the signal rises again (see _step_window). Returns
    (position, fwhm, high, low, position_err, fwhm_err, falling, x_min, x_max)."""
    from scipy.optimize import curve_fit
    from scipy.special import erfc

    x = numpy.asarray(x, dtype=float)
    y = numpy.asarray(y, dtype=float)
    n = min(len(x), len(y))  # an aborted scan can have one more position
    order = numpy.argsort(x[:n], kind="stable")
    x, y = x[:n][order], y[:n][order]
    if n < 5:
        raise ValueError("at least 5 points are needed for a step fit")
    d = numpy.gradient(y, x)
    falling = abs(d.min()) > abs(d.max())
    if xrange is not None:
        keep = (x >= min(xrange)) & (x <= max(xrange))
    else:
        i0, i1 = _step_window(x, y, falling)
        keep = numpy.zeros(n, dtype=bool)
        keep[i0:i1 + 1] = True
    xs, ys = x[keep], y[keep]
    if len(xs) < 5:
        if xrange is not None:
            raise ValueError(f"fewer than 5 points in xrange {xrange}")
        raise RuntimeError("no clear step: the automatic fit range has fewer than 5 points")
    if numpy.ptp(ys) == 0:
        raise RuntimeError("no step: the counter is constant in the fit range")

    sign = 1.0 if falling else -1.0

    def model(x, position, sigma, high, low):
        return low + (high - low) * 0.5 * erfc(sign * (x - position) / (math.sqrt(2) * sigma))

    dd = numpy.gradient(ys, xs)
    i_edge = int(numpy.argmin(dd) if falling else numpy.argmax(dd))
    step = float(numpy.median(numpy.abs(numpy.diff(xs))))
    p0 = [xs[i_edge], max(step, 1e-12), ys.max(), ys.min()]
    bounds = ([xs.min(), 1e-6 * step, -numpy.inf, -numpy.inf], [xs.max(), numpy.ptp(xs), numpy.inf, numpy.inf])
    try:
        popt, pcov = curve_fit(model, xs, ys, p0=p0, bounds=bounds, maxfev=20000)
    except Exception as exc:
        raise RuntimeError(f"step fit failed: {exc}") from exc
    position, sigma, high, low = popt
    perr = numpy.sqrt(numpy.diag(pcov)) if numpy.all(numpy.isfinite(pcov)) else [numpy.nan] * 4
    if high <= low:
        raise RuntimeError("step fit failed: no step in the expected direction")
    # A fit to data without an edge (e.g. an aborted scan on the plateau)
    # turns noise into a "step": refuse it.
    noise = float(numpy.std(ys - model(xs, *popt)))
    mid = (high + low) / 2
    if (high - low) < 10 * noise or (ys > mid).sum() < 2 or (ys < mid).sum() < 2:
        raise RuntimeError(f"no clear step in {xs.min():g}..{xs.max():g}: step {high - low:.3g} "
                           f"against a scatter of {noise:.3g}, {(ys > mid).sum()} points above and "
                           f"{(ys < mid).sum()} below half height")
    return (float(position), float(FWHM_PER_SIGMA * sigma), float(high), float(low), float(perr[0]),
            float(FWHM_PER_SIGMA * perr[1]), bool(falling), float(xs.min()), float(xs.max()))


def _name(obj) -> str:
    return obj if isinstance(obj, str) else getattr(obj, "name", str(obj))


def deriv(counter=None, axis=None, scan=None, plot: bool = True):
    """Derivative d(counter)/d(axis) of the last scan, three-point scheme
    (central differences, one-sided at the ends; see derivative()).

    The scan, counter and axes are chosen by Bliss as for cen() / fwhm(): the
    last scan, the plotselect counter, every scanned motor (time for a
    loopscan). ``deriv()``, ``deriv(mythen.counters.spec)``, ``deriv(axis=th)``.

    Returns a ``Derivative(x, y, counter, axis)``, or for a scan with several
    motors (a2scan) a mapping motor -> Derivative, like cen(). Shows the first
    motor's derivative in the Flint tab "deriv" (``plot=False`` to skip).
    """
    from bliss.scanning.scan_tools import ScanFits, _get_default_scan_and_counter

    scan, counter = _get_default_scan_and_counter(scan, counter)
    found = ScanFits(scan).find_position(derivative, counter, axis=axis)
    cname = _name(counter)
    for ax, (mx, dy) in found.items():
        found[ax] = Derivative(mx, dy, cname, _name(ax))
        imax, imin = int(numpy.argmax(dy)), int(numpy.argmin(dy))
        print(f"d({cname})/d({_name(ax)}), {len(dy)} points: max {dy[imax]:g} at {mx[imax]:g}, "
              f"min {dy[imin]:g} at {mx[imin]:g}")
    if plot and found:
        _show(next(iter(found.values())))
    return scan._simplify_fit_return(found, return_axes=False)


def deriv_fit(counter=None, axis=None, scan=None, plot: bool = True):
    """Gaussian fit of the derivative of the last scan (see deriv() for the
    choice of scan, counter and axes). Prints and returns a
    ``DerivativeFit(center, fwhm, height, background, ...)`` (a mapping per
    motor for an a2scan) and shows the derivative with the fitted curve in
    the Flint tab "deriv"."""
    from bliss.scanning.scan_tools import ScanFits, _get_default_scan_and_counter

    scan, counter = _get_default_scan_and_counter(scan, counter)
    cname = _name(counter)

    def fit(x, y):
        dx, dy = derivative(x, y)
        return (*fit_derivative(x, y), dx, dy)

    found = ScanFits(scan).find_position(fit, counter, axis=axis)
    for ax, (center, fwhm, height, background, fwhm_fit, dx, dy) in found.items():
        found[ax] = DerivativeFit(center, fwhm, height, background, fwhm_fit, cname, _name(ax), dx, dy)
        print(f"d({cname})/d({_name(ax)}) Gaussian fit: centre {center:g}, FWHM {fwhm:g} "
              f"(fitted {fwhm_fit:g}, corrected for the step), height {height:g}, background {background:g}")
    if plot and found:
        f = next(iter(found.values()))
        xf = numpy.linspace(f.x.min(), f.x.max(), 400)
        _show(Derivative(f.x, f.y, f.counter, f.axis), fit=(xf, f.curve(xf)))
    return scan._simplify_fit_return(found, return_axes=False)


def deriv_fwhm(counter=None, axis=None, scan=None, plot: bool = True):
    """FWHM of a Gaussian fitted to the derivative of the last scan (beam
    width from an edge scan). Returns a number, or a mapping per motor for an
    a2scan, like fwhm()."""
    res = deriv_fit(counter, axis, scan, plot)
    if isinstance(res, DerivativeFit):
        return res.fwhm
    for ax in res:
        res[ax] = res[ax].fwhm
    return res


def shad_fit(counter=None, axis=None, scan=None, levels=None, plot: bool = True):
    """Maximum and the two flank slopes of a shadow scan (triangle).

    Scan, counter and axes are chosen as for cen(): the last scan, the
    plotselect counter, every scanned motor (an a2scan th/tth gives one fit
    per motor). Straight lines are fitted to the points on each side of the
    maximum; ``levels=(0.2, 0.9)`` uses only points between 20 and 90 % of
    the maximum. Returns a ``ShadowFit`` (peak, apex, height, slope_left,
    slope_right, ...), a mapping per motor for several motors, and shows the
    scan with both lines in the Flint tab "shadow".
    """
    from bliss.scanning.scan_tools import ScanFits, _get_default_scan_and_counter

    scan, counter = _get_default_scan_and_counter(scan, counter)
    cname = _name(counter)

    def fit(x, y):
        return (*fit_shadow(x, y, levels), numpy.asarray(x, dtype=float), numpy.asarray(y, dtype=float))

    found = ScanFits(scan).find_position(fit, counter, axis=axis)
    for ax, values in found.items():
        *numbers, x, y = values
        f = ShadowFit(*numbers, cname, _name(ax))
        found[ax] = f
        print(f"{cname} vs {f.axis} shadow fit: max at {f.peak:g}, lines cross at {f.apex:g} "
              f"(height {f.height:g}); slopes {f.slope_left:g} (left, {f.n_left} points), "
              f"{f.slope_right:g} (right, {f.n_right} points)")
        if plot and ax == next(iter(found)):
            _show_shadow(x, y, f)
    return scan._simplify_fit_return(found, return_axes=False)


def _show_shadow(x, y, f: ShadowFit):
    try:
        from bliss.common.plot import get_flint

        flint = get_flint()
        p = flint.get_plot("curve", name="shadow", unique_name="xeuss:shadow", selected=True)
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        tip = f.apex if numpy.isfinite(f.apex) else f.peak
        xl = numpy.array([x.min(), tip])
        xr = numpy.array([tip, x.max()])
        with p.transaction():
            p.clear_items()
            p.set_data(x=x, y=y, xl=xl, yl=f.slope_left * xl + f.intercept_left,
                       xr=xr, yr=f.slope_right * xr + f.intercept_right)
            p.add_curve_item("x", "y", legend=f.counter)
            p.add_curve_item("xl", "yl", legend="left line")
            p.add_curve_item("xr", "yr", legend="right line")
        p.xlabel = f.axis
        p.ylabel = f.counter
        flint.set_plot_focus(p.plot_id)  # get_plot only selects a plot it creates
    except Exception as exc:  # the numbers are already printed
        log_warning("shad_fit", "could not show the fit in Flint: %s", exc)


def _step_fits(counter, axis, scan, xrange, plot):
    """step_fit() for every scanned motor: (scan, {axis: StepFit})."""
    from bliss.scanning.scan_tools import ScanFits, _get_default_scan_and_counter

    scan, counter = _get_default_scan_and_counter(scan, counter)
    cname = _name(counter)

    def fit(x, y):
        return (*fit_step(x, y, xrange), numpy.asarray(x, dtype=float), numpy.asarray(y, dtype=float))

    found = ScanFits(scan).find_position(fit, counter, axis=axis)
    first = next(iter(found), None)
    for ax, values in found.items():
        *numbers, x, y = values
        f = StepFit(*numbers, cname, _name(ax))
        found[ax] = f
        print(f"{cname} vs {f.axis} step fit ({'falling' if f.falling else 'rising'}, "
              f"{f.x_min:g}..{f.x_max:g}): edge at {f.position:g} +- {f.position_err:.2g}, "
              f"FWHM {f.fwhm:g} +- {f.fwhm_err:.2g}, levels {f.high:g} / {f.low:g}")
        if plot and ax == first:
            _show_step(x, y, f)
    return scan, found


def step_fit(counter=None, axis=None, scan=None, xrange=None, plot: bool = True):
    """Edge position and beam width from an error-function step fitted to
    the last scan (e.g. a z scan into the sample, or a y scan over its edge).

    Scan, counter and axes are chosen as for cen(). The fit uses the bright
    side and the dark side up to where the signal rises again (the reflection
    peak of a z scan is left out); ``xrange=(start, stop)`` sets the range
    instead. Returns a ``StepFit(position, fwhm, high, low, position_err,
    fwhm_err, ...)`` (a mapping per motor for several motors) and shows the
    data, the fitted points and the step in the Flint tab "step".
    """
    scan, found = _step_fits(counter, axis, scan, xrange, plot)
    return scan._simplify_fit_return(found, return_axes=False)


def goto_step(counter=None, axis=None, scan=None, xrange=None, plot: bool = True):
    """Fit the step of the last scan (as step_fit) and move the scanned
    motors to the edge position, like goto_cen(). For an a2scan every motor
    goes to its own edge position, all in one move. Nothing moves if the fit
    is refused; the position is always inside the fitted range of the scan.
    Returns the step fit(s)."""
    from bliss.common.axis.axis import Axis
    from bliss.shell.standard import umv

    scan, found = _step_fits(counter, axis, scan, xrange, plot)
    moves = []
    for ax, f in found.items():
        if not isinstance(ax, Axis):
            raise RuntimeError(f"cannot move to the step: {f.axis} is not a motor (e.g. a loopscan)")
        moves += [ax, f.position]
    umv(*moves)
    return scan._simplify_fit_return(found, return_axes=False)


def _show_step(x, y, f: StepFit):
    try:
        from bliss.common.plot import get_flint

        flint = get_flint()
        p = flint.get_plot("curve", name="step", unique_name="xeuss:step", selected=True)
        n = min(len(x), len(y))
        x, y = numpy.asarray(x[:n], dtype=float), numpy.asarray(y[:n], dtype=float)
        inside = (x >= f.x_min) & (x <= f.x_max)
        xf = numpy.linspace(f.x_min, f.x_max, 400)
        with p.transaction():
            p.clear_items()
            p.set_data(x=x, y=y, xi=x[inside], yi=y[inside], xf=xf, yf=f.curve(xf))
            p.add_curve_item("x", "y", legend=f.counter)
            p.add_curve_item("xi", "yi", legend="fitted points")
            p.add_curve_item("xf", "yf", legend="step fit")
        p.xlabel = f.axis
        p.ylabel = f.counter
        flint.set_plot_focus(p.plot_id)  # get_plot only selects a plot it creates
    except Exception as exc:  # the numbers are already printed
        log_warning("step_fit", "could not show the fit in Flint: %s", exc)


def _show(d: Derivative, fit=None):
    try:
        from bliss.common.plot import get_flint

        flint = get_flint()
        p = flint.get_plot("curve", name=PLOT_NAME, unique_name="xeuss:deriv", selected=True)
        legend = f"d({d.counter})"
        with p.transaction():
            p.clear_items()
            p.set_data(x=d.x, y=d.y)
            p.add_curve_item("x", "y", legend=legend)
            if fit is not None:
                p.set_data(xf=fit[0], yf=fit[1])
                p.add_curve_item("xf", "yf", legend="Gaussian fit")
        p.xlabel = d.axis
        p.ylabel = legend
        # get_plot(selected=True) only selects a plot it creates: show the
        # existing "deriv" tab explicitly (scans switch Flint back to the live tab)
        flint.set_plot_focus(p.plot_id)
    except Exception as exc:  # the numbers are already printed
        log_warning("deriv", "could not show the derivative in Flint: %s", exc)
