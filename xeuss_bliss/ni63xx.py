"""BLISS counter controller for the NI PCIe-6320 of the Xeuss 2.0 (xeuss20), using NI-DAQmx.

It reproduces SPEC's NI63xx setup (SPEC config CNT000-CNT003, see spec_help ni63xx):

    sec  ctr0  gate generator, value = gate time in seconds
    mon  ctr1  counts rising edges on PFI3 (connector pin 42)
    det  ctr2  counts rising edges on PFI0 (pin 11)
    shu  ctr3  counts rising edges on PFI5 (pin 6)

For every point the counting channels are re-armed (count reset to 0) with a pause
trigger on the gate counter's internal output, so they only count while the gate is
high. The gate counter then emits a single high pulse of `count_time`. Like SPEC, the
gate counter also drives its physical output (PFI12, pin 2, jumpered to Gate(0) pin 3).

Only works in BLISS mode (NI-DAQmx driver loaded). The DIO lines are NOT touched here:
line 7 is the X-ray shutter, lines 5/6 heating/cooling power.

YAML example::

    - name: ni
      plugin: bliss
      class: NI63xx
      package: xeuss_bliss.ni63xx
      device: Dev1
      gate_counter: ctr0
      counters:
        - counter_name: sec
          channel: timer
          unit: s
        - counter_name: mon
          channel: ctr1
"""

import time

import gevent
import nidaqmx
from nidaqmx.constants import (
    AcquisitionType,
    CountDirection,
    Edge,
    Level,
    TriggerType,
)

from bliss.common.counter import IntegratingCounter
from bliss.controllers.counter import IntegratingCounterController
from bliss.scanning.acquisition.counter import IntegratingCounterAcquisitionSlave

from xeuss_bliss.acq_utils import emit_pending

TIMER = "timer"
# Short idle times around the gate pulse. 1e-4 s is a whole number of ticks for both
# timebases DAQmx may pick (100 MHz for short gates, 100 kHz for long ones).
GATE_IDLE = 1e-4
POLL = 0.005
# Longest gate: 32-bit counter on the slowest timebase (100 kHz) of the PCIe-6320 -> (2^32 - 1) / 1e5 s,
# minus the idle times around the pulse
MAX_COUNT_TIME = (2**32 - 1) / 100e3 - 2 * GATE_IDLE  # 42949.67 s = 11.9 h


class NI63xxAcquisitionSlave(IntegratingCounterAcquisitionSlave):
    def prepare_device(self):
        self.device._prepare(self.count_time)

    def trigger(self):
        self.device._trigger()

    def wait_ready(self):
        self.device._wait_ready()

    def stop_device(self):
        self.device._stop()
        try:  # aborted point: counts up to the abort, sec = time really counted (same data as a full point)
            emit_pending(self)
        except Exception as exc:
            print(f"WARNING: {self.device.name}: partial point not sent to the scan: {exc}")


class NI63xx(IntegratingCounterController):
    def __init__(self, name, config):
        super().__init__(name)
        self._config = config
        self._device = config.get("device", "Dev1")
        self._gate_counter = config.get("gate_counter", "ctr0")
        self._channels = {}
        for cnt_cfg in config.get("counters", []):
            cnt_name = cnt_cfg["counter_name"]
            channel = cnt_cfg["channel"]
            if channel == self._gate_counter:
                raise ValueError(f"{name}: {channel} is the gate counter, use 'channel: timer'")
            self.create_counter(IntegratingCounter, cnt_name, unit=cnt_cfg.get("unit"))
            self._channels[cnt_name] = channel
        self._gate_task = None
        self._count_tasks = {}
        self._count_time = None
        self._data = []
        self._counting = None
        self._gate_started = None

    def __info__(self):
        lines = [f"NI63xx counter controller '{self.name}' on {self._device}"]
        lines.append(f"  gate counter: {self._gate_counter}")
        for cnt_name, channel in self._channels.items():
            lines.append(f"  {cnt_name:6s} {channel}")
        return "\n".join(lines)

    def __close__(self):
        """Session end: stop counting and release the NI tasks."""
        self._stop()

    # --- BLISS counter controller interface -------------------------------------

    def get_acquisition_object(self, acq_params, ctrl_params, parent_acq_params):
        # prepared and started once per scan, triggered once per point
        return NI63xxAcquisitionSlave(
            self, ctrl_params=ctrl_params, prepare_once=True, start_once=True, **acq_params
        )

    def get_values(self, from_index, *counters):
        rows = self._data[from_index:]
        return [[row[cnt.name] for row in rows] for cnt in counters]

    # --- hardware ----------------------------------------------------------------

    def _gate_terminal(self):
        number = self._gate_counter.replace("ctr", "")
        return f"/{self._device}/Ctr{number}InternalOutput"

    def _close_tasks(self):
        for task in [self._gate_task, *self._count_tasks.values()]:
            if task is not None:
                try:
                    task.close()
                except Exception:
                    pass
        self._gate_task = None
        self._count_tasks = {}

    def _prepare(self, count_time):
        if count_time > MAX_COUNT_TIME:
            raise ValueError(
                f"{self.name}: count time {count_time:g} s is longer than the NI PCIe-6320 can gate in one count "
                f"({MAX_COUNT_TIME:.0f} s = {MAX_COUNT_TIME / 3600:.1f} h, 32-bit counter at 100 kHz) -> split it, "
                f"e.g. loopscan(n, {MAX_COUNT_TIME // 2:.0f})")
        self._stop()
        self._data = []
        self._count_time = count_time

        gate = nidaqmx.Task()
        gate.co_channels.add_co_pulse_chan_time(
            f"{self._device}/{self._gate_counter}",
            idle_state=Level.LOW,
            initial_delay=GATE_IDLE,
            low_time=GATE_IDLE,
            high_time=count_time,
        )
        gate.timing.cfg_implicit_timing(sample_mode=AcquisitionType.FINITE, samps_per_chan=1)
        self._gate_task = gate

        for cnt_name, channel in self._channels.items():
            if channel == TIMER:
                continue
            task = nidaqmx.Task()
            task.ci_channels.add_ci_count_edges_chan(
                f"{self._device}/{channel}",
                edge=Edge.RISING,
                initial_count=0,
                count_direction=CountDirection.COUNT_UP,
            )
            pause = task.triggers.pause_trigger
            pause.trig_type = TriggerType.DIGITAL_LEVEL
            pause.dig_lvl_src = self._gate_terminal()
            pause.dig_lvl_when = Level.LOW
            self._count_tasks[cnt_name] = task

    def _trigger(self):
        if self._counting is not None and not self._counting.ready():
            raise RuntimeError(f"{self.name}: previous count is still running")
        self._counting = gevent.spawn(self._count_one_point)

    def _wait_ready(self):
        if self._counting is not None:
            self._counting.get()

    def _count_one_point(self):
        for task in self._count_tasks.values():
            task.stop()  # stop + start re-arms the counter at 0
            task.start()
        gate = self._gate_task
        gate.start()
        self._gate_started = time.monotonic()
        try:
            timeout = self._count_time + 5.0
            with gevent.Timeout(timeout, RuntimeError(f"{self.name}: gate did not finish")):
                while not gate.is_task_done():
                    gevent.sleep(POLL)
        finally:
            gate.stop()
        row = {name: float(task.read()) for name, task in self._count_tasks.items()}
        for cnt_name, channel in self._channels.items():
            if channel == TIMER:
                row[cnt_name] = float(self._count_time)  # ct(1): an int would be refused by the data stream
        self._data.append(row)

    def _store_partial(self):
        """Count interrupted (scan aborted): keep the counts so far, sec = time the gate was open."""
        try:
            elapsed = min(time.monotonic() - self._gate_started, float(self._count_time))
            row = {name: float(task.read()) for name, task in self._count_tasks.items()}
            for cnt_name, channel in self._channels.items():
                if channel == TIMER:
                    row[cnt_name] = float(elapsed)
            self._data.append(row)
        except Exception as exc:
            print(f"WARNING: {self.name}: counts of the interrupted point not read: {exc}")
        finally:
            self._gate_started = None

    def _stop(self):
        if self._counting is not None:
            running = not self._counting.ready()
            self._counting.kill()  # its finally stops the gate: the counters pause
            self._counting = None
            if running and self._gate_started is not None:
                self._store_partial()
        if self._gate_task is not None:
            try:
                self._gate_task.stop()  # gate output back to idle (low)
            except Exception:
                pass
        self._close_tasks()
