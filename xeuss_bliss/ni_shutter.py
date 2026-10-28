"""X-ray shutter of the Xeuss 2.0 (xeuss20) on a digital output line of the NI PCIe-6320.

Same wiring as SPEC's xenocs_macros/shutter_NI63.mac: line 7 of port 0, high = open, low = closed.
Like SPEC (clr_bit 7 at startup), the shutter is CLOSED when the object is created,
and also when the BLISS session ends (SPEC leaves it as it is).

There is no position feedback: `state` is the level the line is driven to, read back
from the output task. The line is never read through a digital *input* task, because that
would turn it back into an input. (As an input after boot it reads low = closed.)

Only works in BLISS mode (NI-DAQmx driver loaded). Tested with the user on 2026-10-20:
line 7 high -> shutter opened, det counted; low -> closed.

YAML example::

    - name: shutter
      plugin: bliss
      class: NIShutter
      package: xeuss_bliss.ni_shutter
      line: Dev1/port0/line7
"""

import nidaqmx

from bliss.common.base_shutter import BaseShutter, BaseShutterState


class NIShutter(BaseShutter):
    def __init__(self, name, config):
        self._name = name
        self._config = config
        self._line = config.get("line", "Dev1/port0/line7")
        self._task = nidaqmx.Task()
        self._task.do_channels.add_do_chan(self._line)
        self._task.write(False)  # closed at startup, as SPEC does

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        try:
            return BaseShutterState.OPEN if self._task.read() else BaseShutterState.CLOSED
        except Exception:
            return BaseShutterState.UNKNOWN

    def open(self, timeout=None):
        self._task.write(True)
        print("X-RAY SHUTTER OPEN")

    def close(self, timeout=None):
        self._task.write(False)
        print("X-RAY SHUTTER CLOSED")

    def __info__(self):
        return f"{self.name}: {self.state_string} (NI {self._line}, no position feedback)"

    def __close__(self):
        """Session end: close the shutter (stricter than SPEC), then release the NI task."""
        try:
            self.close()
        finally:
            self._task.close()
