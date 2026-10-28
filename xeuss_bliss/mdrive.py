"""BLISS motor controller for the IMS / Schneider MDrive motors of the Xeuss 2.0 (xeuss20).

All motors share one RS-422/485 line (FTDI adapter, /dev/ttyUSB0, 9600 8N1) in party-line
mode, as in SPEC (RS_MDRIVE, controller MDRIVE). Every MDrive has a one-character device
name; SPEC channel N maps to name 1..9 for N = 1..9 and A, B, C ... for N = 10, 11, 12 ...
(SPEC 5.03.02-1 change notes), e.g. MDRIVE:0/10 -> "A". A command is "<name><command>\\n";
the reply to a PR (print) query is "\\r\\n<value>\\r\\n".

Two firmware generations answer to the same commands here:
    3.014                    classic MDrive (slits, fil, om, bsx, bsz, x)
    "LMDOM 5.010, Hw: 2.8"   Lexium MDrive (detx, detz, z)

Positions are in motor microsteps (register P, MS = 256), as in SPEC's config (steps per
unit). Velocity is VM (steps/s), acceleration A (steps/s^2).

SAFETY: `read_only: true` (the default) makes the controller refuse every command that can
move a motor or change its position register. Velocity/acceleration from the YAML are then
only remembered, not sent. Set `read_only: false` only after the moves have been tested
with someone watching the instrument.

YAML example::

    - controller:
        class: MDrive
        package: xeuss_bliss.mdrive
        plugin: emotion
        serial:
          url: /dev/ttyUSB0
          baudrate: 9600
        read_only: true
        axes:
          - name: s1bot
            device_name: A
            steps_per_unit: 102400
            sign: 1
            velocity: 0.75
            acceleration: 6.0
            low_limit: -11      # dial units, as SPEC
            high_limit: 11
            base_rate: 100      # SPEC base rate, steps/s (VI)
            backlash: -0.125    # SPEC backlash -12800 steps / steps_per_unit
            backlash_is_dial: true

Backlash: BLISS overshoots to target - backlash when a move goes against the sign of
`backlash`, so every move ends travelling in the direction of its sign.

A power-cycled MDrive loses EM=1/PY=1 (SPEC sets them at start-up) and stops answering
in party-line mode; this controller then fails to initialise instead of reading a wrong
position. Let SPEC re-initialise it, or add that set-up here when moves are enabled.
"""

import gevent.lock

from bliss.comm.util import SERIAL, get_comm
from bliss.common.axis.state import AxisState
from bliss.controllers.motor import Controller


class MDriveReadOnlyError(RuntimeError):
    pass


class MDrive(Controller):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = gevent.lock.RLock()
        self._comm = None
        self._remembered = {}

    # --- setup -------------------------------------------------------------------

    def initialize(self):
        self._comm = get_comm(self.config.config_dict, SERIAL, baudrate=9600, timeout=1)
        self.read_only = bool(self.config.get("read_only", True))

    def initialize_axis(self, axis):
        axis.device_name = str(axis.config.get("device_name"))
        if len(axis.device_name) != 1:
            raise ValueError(f"{axis.name}: device_name must be one character, got {axis.device_name!r}")
        # start velocity in steps/s (MDrive VI), SPEC's "base rate"
        axis.base_rate = int(axis.config.get("base_rate", 100))

    def initialize_hardware_axis(self, axis):
        # only check that the motor answers; nothing is written
        axis.firmware = self._query(axis, "VR")

    def finalize(self):
        if self._comm is not None:
            self._comm.close()

    # --- communication -----------------------------------------------------------

    def _send(self, axis, command):
        """Send a command without a value (set or move). With EM=1 the MDrive answers every
        command line with an empty line "\\r\\n", which is read here. Not reading it caused the
        first move test to fail (2026-10-20): the replies arrived after the next flush() and
        were taken as the answers to PR MV / PR P, BLISS aborted the move, s1bot stopped
        after 594 steps."""
        with self._lock:
            self._comm.flush()
            reply = self._comm.write_readline(f"{axis.device_name}{command}\n".encode(), eol=b"\r\n")
        if reply.strip():
            raise RuntimeError(f"{axis.name} ({axis.device_name}): unexpected reply to {command!r}: {reply!r}")

    def _query(self, axis, variable):
        """PR query; returns the printed value as a string. Reply: "\\r\\n<value>\\r\\n"."""
        with self._lock:
            self._comm.flush()  # empties the input buffer
            lines = self._comm.write_readlines(
                f"{axis.device_name}PR {variable}\n".encode(), 2, eol=b"\r\n"
            )
        value = lines[-1].decode(errors="replace").strip()
        if value in ("", "?"):
            raise RuntimeError(f"{axis.name} ({axis.device_name}): no valid answer to 'PR {variable}': {lines!r}")
        return value

    def _check_writable(self, axis, what):
        if self.read_only:
            raise MDriveReadOnlyError(
                f"{axis.name}: '{what}' refused - MDrive controller is read_only (see xeuss_bliss/mdrive.py)"
            )

    # --- read --------------------------------------------------------------------

    def read_position(self, axis):
        return int(self._query(axis, "P"))

    def read_velocity(self, axis):
        if self.read_only and (axis.name, "velocity") in self._remembered:
            return self._remembered[(axis.name, "velocity")]
        return float(self._query(axis, "VM"))

    def read_acceleration(self, axis):
        if self.read_only and (axis.name, "acceleration") in self._remembered:
            return self._remembered[(axis.name, "acceleration")]
        return float(self._query(axis, "A"))

    def state(self, axis):
        moving = self._query(axis, "MV")
        return AxisState("MOVING") if moving == "1" else AxisState("READY")

    def get_axis_info(self, axis):
        return (
            f"MDrive '{axis.device_name}' firmware {getattr(axis, 'firmware', '?')}, "
            f"P={self._query(axis, 'P')} steps, read_only={self.read_only}"
        )

    def get_id(self, axis):
        return f"MDrive {axis.device_name} {getattr(axis, 'firmware', '?')}"

    # --- write (refused while read_only) ----------------------------------------

    def set_velocity(self, axis, new_velocity):
        if self.read_only:
            self._remembered[(axis.name, "velocity")] = new_velocity
            return
        self._send(axis, f"VM={int(round(new_velocity))}")

    def set_acceleration(self, axis, new_acc):
        if self.read_only:
            self._remembered[(axis.name, "acceleration")] = new_acc
            return
        acc = int(round(new_acc))
        self._send(axis, f"A={acc}")
        self._send(axis, f"D={acc}")

    def set_position(self, axis, new_position):
        self._check_writable(axis, "set position register")
        self._send(axis, f"P={int(round(new_position))}")
        return self.read_position(axis)

    def start_one(self, motion):
        # same sequence as SPEC: VM, VI, A, D, then MA (absolute move in steps)
        axis = motion.axis
        self._check_writable(axis, "move")
        velocity = int(round(axis.velocity * axis.steps_per_unit))
        acceleration = int(round(axis.acceleration * axis.steps_per_unit))
        self._send(axis, f"VM={velocity}")
        self._send(axis, f"VI={min(axis.base_rate, velocity)}")
        self._send(axis, f"A={acceleration}")
        self._send(axis, f"D={acceleration}")
        self._send(axis, f"MA {int(round(motion.target_pos))}")

    def start_all(self, *motions):
        for motion in motions:
            self.start_one(motion)

    def stop(self, axis):
        if self.read_only:
            return  # BLISS cannot have started a move
        self._send(axis, "SL 0")  # slew at velocity 0 = decelerated stop

    def stop_all(self, *motions):
        for motion in motions:
            self.stop(motion.axis)

    def set_on(self, axis):
        pass

    def set_off(self, axis):
        pass
