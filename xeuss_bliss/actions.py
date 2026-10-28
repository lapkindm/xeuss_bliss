"""Actions at given points of a scan: switch a lamp on after point 10, go to
100 deg after point 20, ...

    s = loopscan(100, 1, run=False)
    with_actions(s, {10: lamp_on, 20: "umv(temp, 100)"})

``n: action`` runs after n points have been measured, before the motors
move to the next point; the scan waits until the action has finished (e.g.
until a temperature has settled). An action is a function, a function with
its arguments as a tuple ``(func, arg, ...)``, or the text of a command,
run in the session like a typed command. Everything is checked before the
scan starts. Each action is printed when it runs and recorded as a comment
of the scan in the data file. An action that fails stops the scan.

From xrd3003_bliss (github.com/lapkindm/xrd3003_bliss, commit 23e6e6d), included here 2026-10-21.
"""

from __future__ import annotations

import ast
import builtins

from bliss.scanning.chain import ChainIterationPreset, ChainPreset
from bliss.scanning.scan_state import ScanState


class _Action:
    def __init__(self, action, namespace: dict):
        self.namespace = namespace
        if isinstance(action, str):
            try:
                self.code = compile(action, "<scan action>", "exec")
            except SyntaxError as exc:
                raise ValueError(f"action {action!r} is not a valid command: {exc.msg}") from None
            unknown = sorted({n.id for n in ast.walk(ast.parse(action))
                              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                              and n.id not in namespace and not hasattr(builtins, n.id)})
            if unknown:
                raise NameError(f"action {action!r}: unknown name(s) {', '.join(unknown)}")
            self.func, self.args, self.text = None, (), action
            return
        func, args = (action[0], action[1:]) if isinstance(action, tuple) else (action, ())
        if not callable(func):
            raise TypeError(f"action {action!r} is not a function or a command text")
        self.code = None
        self.func, self.args = func, args
        name = getattr(func, "__name__", repr(func))
        self.text = f"{name}({', '.join(repr(a) for a in args)})" if name != "<lambda>" else "lambda"

    def __call__(self):
        if self.code is not None:
            exec(self.code, self.namespace)
        else:
            self.func(*self.args)


class ActionsPreset(ChainPreset):
    """Chain preset running ``actions[n]`` after n points of the scan."""

    def __init__(self, scan, actions: dict[int, _Action]):
        self.scan = scan
        self.actions = actions
        self.done: list[int] = []

    def get_iterator(self, chain):
        n = 0
        while True:
            yield _Point(self, n)
            n += 1


class _Point(ChainIterationPreset):
    def __init__(self, parent: ActionsPreset, n: int):
        self.parent, self.n = parent, n

    def prepare(self):  # per point Bliss runs: presets' prepare, the motor move, presets' start, the count
        action = self.parent.actions.get(self.n)
        if action is None:
            return
        print(f"\n--- after point {self.n}: {action.text}")
        try:
            self.parent.scan.add_comment(f"after point {self.n}: {action.text}")
        except Exception:  # noqa: BLE001  (the action matters more than the note)
            pass
        action()
        self.parent.done.append(self.n)


def _namespace() -> dict:
    try:
        from bliss import current_session

        return current_session.env_dict
    except Exception:  # noqa: BLE001
        return {}


def with_actions(scan, actions: dict, run: bool = True, namespace: dict | None = None):
    """Attach ``actions`` ({points measured: action}) to a scan prepared with
    ``run=False`` and run it (``run=False`` here only attaches them).
    Returns the scan."""
    if scan.state != ScanState.IDLE:
        raise RuntimeError("the scan has already run: prepare it with run=False")
    namespace = _namespace() if namespace is None else namespace
    npoints = scan.scan_info.get("npoints")
    checked = {}
    for n, action in actions.items():
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise ValueError(f"point {n!r}: use the number of points measured before the action (0, 1, 2, ...)")
        if npoints and n >= npoints:  # 0 / None: endless (timescan), no limit
            raise ValueError(f"point {n}: the scan has only {npoints} points, the action would never run")
        checked[n] = _Action(action, namespace)
    preset = ActionsPreset(scan, checked)
    scan.acq_chain.add_preset(preset)
    for n in sorted(checked):
        print(f"after point {n}: {checked[n].text}")
    if run:
        scan.run()
    return scan
