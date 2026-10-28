"""Xenocs GeniX3D X-ray generator (Xeuss 2.0, xeuss20): READ-ONLY status over Ethernet.

GeniX3D manual MP-GeniX3D version G (20/06/14), section 7.6 "Remote control": a Schneider Twido PLC behind an
Ethernet module, Modbus/TCP on port 502, default address 85.16.44.113 / 255.255.0.0 (crossed cable for a direct
link; changing the address: section 7.6.2, telnet). Twido memory bits %Mi = Modbus coils i (function 01),
memory words %MWi = holding registers i (function 03).

Read here:
    %M210 remote mode, %M211 X-rays on, %M212 standby, %M213 auto cycle, %M214 auto-cycle conditions OK,
    %M215 reset cycle, %M216 warm-up cycle, %M217 tube 50 W (1) / 30 W (0), %M219 any fault,
    %M220-%M232 faults (X-ray lamp, shutter lamp, sensor 2, tube position, vacuum, water flow, safety shutter,
    temperature, sensor 1, interlock relay, door sensor, filament, warm-up needed), %M234 PLC heartbeat
    (changes every second), %M235 interlock OK, %M236 safety shutter closed, %M237 open, %M239 override key on
    %MW50 kV x100, %MW51 mA x100, %MW52 kV setpoint x10, %MW53 mA setpoint x100, %MW55 minutes, %MW56 hours,
    %MW57 tube power (500 = 50 W, 300 = 30 W)

Not in the manual, found by test on 2026-10-23 (all %M0-%M255 / %MW0-%MW499 read with the enclosure door closed / open
/ closed): %M147 and %M187 = 1 with the Xeuss ENCLOSURE door closed, 0 with it open (the generator's green OK lamp blinks
then). Checked with two different doors: both act on the same bits (the doors are in one series chain), so the PLC knows
"a door is open", not which one. The documented "interlock OK" / "door sensor fault" bits do NOT follow the enclosure doors.
door_closed = both bits 1 = all enclosure doors closed.

NOT implemented on purpose: the commands %M240-%M252 (X-rays on/off, shutter, kV/mA, reset, warm-up). They need the
generator in "Remote" mode, which locks its front panel (manual 7.6, "Details").

    genix                 status table
    genix.status()        dict of all values
    genix.kv, genix.ma, genix.xray_on, genix.interlock_ok, genix.shutter_open, genix.faults
    genix.check()         raises if X-rays are off, the interlock is not OK or a fault is set (before measuring)

YAML:
    - name: genix
      plugin: bliss
      class: GeniX3D
      package: xeuss_bliss.genix
      host: 85.16.44.113
      port: 502
      unit: 1
"""

import time

import gevent.lock

from bliss.comm.modbus import ModbusTcp

BIT_START, BIT_COUNT = 210, 30  # %M210 .. %M239
DOOR_BITS = (147, 187)  # enclosure door closed (undocumented, see module doc)
WORD_START, WORD_COUNT = 50, 8  # %MW50 .. %MW57

BITS = {
    210: "remote_mode",
    211: "xray_on",
    212: "standby",
    213: "auto_cycle",
    214: "auto_conditions_ok",
    215: "reset_cycle",
    216: "warm_up_cycle",
    217: "tube_50w",
    219: "fault",
    234: "heartbeat",
    235: "interlock_ok",
    236: "shutter_closed",
    237: "shutter_open",
    239: "override_key",
}
FAULTS = {
    220: "X-ray lamp",
    221: "shutter lamp",
    222: "sensor 2",
    223: "tube position",
    224: "vacuum",
    225: "water flow",
    226: "safety shutter",
    227: "temperature",
    228: "sensor 1",
    229: "interlock relay",
    230: "door sensor",
    231: "filament",
    232: "tube warm-up needed",
}


class GeniX3D:
    def __init__(self, name, config):
        self._name = name
        self._config = config
        self._host = config.get("host", "85.16.44.113")
        self._port = int(config.get("port", 502))
        self._unit = int(config.get("unit", 1))
        self._modbus = ModbusTcp(self._host, unit=self._unit, port=self._port, timeout=float(config.get("timeout", 3.0)))
        self._checked = False
        self._lock = gevent.lock.RLock()  # beam guard reads per point and in the background at the same time

    @property
    def name(self):
        return self._name

    # --- reading ------------------------------------------------------------------------
    def _read(self):
        with self._lock:
            bits = self._modbus.read_coils(BIT_START, BIT_COUNT)
            door = self._modbus.read_coils(DOOR_BITS[0], DOOR_BITS[1] - DOOR_BITS[0] + 1)
            words = self._modbus.read_holding_registers(WORD_START, f"{WORD_COUNT}H")  # BLISS adds ">" (big-endian)
        bits = {BIT_START + i: int(b) for i, b in enumerate(bits)}
        bits.update({a: int(door[a - DOOR_BITS[0]]) for a in DOOR_BITS})
        return bits, {WORD_START + i: int(w) for i, w in enumerate(words)}

    def status(self):
        """All values as a dict (one Modbus read of the bits and one of the words)."""
        bits, words = self._read()
        if not self._checked:
            self._check_mapping(bits, words)
        values = {name: bool(bits[addr]) for addr, name in BITS.items()}
        values["door_closed"] = all(bits[a] for a in DOOR_BITS)  # both must say closed
        values.update(
            kv=words[50] / 100.0,
            ma=words[51] / 100.0,
            kv_setpoint=words[52] / 10.0,
            ma_setpoint=words[53] / 100.0,
            minutes=words[55],
            hours=words[56],
            tube_power_w=words[57] / 10.0,
            faults=[text for addr, text in FAULTS.items() if bits[addr]],
        )
        return values

    def _check_mapping(self, bits, words):
        """First contact: the tube power word must be 300 or 500 and the heartbeat bit must change within ~2 s,
        else the Modbus addresses do not match the manual's table (e.g. off by one) - refuse to report values."""
        if words[57] not in (300, 500):
            raise RuntimeError(f"{self._name}: %MW57 (tube power) = {words[57]}, expected 300 or 500 - Modbus "
                               f"address mapping does not match the GeniX3D manual; values not trusted")
        first = bits[234]
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if int(self._modbus.read_coils(234, 1)[0]) != first:
                self._checked = True
                return
        raise RuntimeError(f"{self._name}: heartbeat %M234 did not change in 2.5 s - PLC not running or Modbus "
                           f"address mapping does not match the manual; values not trusted")

    @property
    def kv(self):
        return self.status()["kv"]

    @property
    def ma(self):
        return self.status()["ma"]

    @property
    def xray_on(self):
        return self.status()["xray_on"]

    @property
    def interlock_ok(self):
        return self.status()["interlock_ok"]

    @property
    def shutter_open(self):
        return self.status()["shutter_open"]

    @property
    def door_closed(self):
        return self.status()["door_closed"]

    @property
    def faults(self):
        return self.status()["faults"]

    def check(self):
        """Raise if the source is not ready to measure: X-rays off, interlock not OK or a fault."""
        s = self.status()
        problems = []
        if not s["xray_on"]:
            problems.append("X-rays off" + (" (standby)" if s["standby"] else ""))
        if not s["door_closed"]:
            problems.append("enclosure door open")
        if not s["interlock_ok"]:
            problems.append("generator interlock not OK")
        if s["fault"] or s["faults"]:
            problems.append("fault: " + (", ".join(s["faults"]) or "unspecified"))
        if s["xray_on"]:
            from xeuss_bliss.shutter_preset import _setpoint_problem

            setpoint = _setpoint_problem(s)
            if setpoint:
                problems.append(setpoint)
        if problems:
            raise RuntimeError(f"{self._name}: " + "; ".join(problems))
        print(f"{self._name}: X-rays on, {s['kv']:.2f} kV, {s['ma']:.2f} mA, interlock OK, no fault")

    # --- display ------------------------------------------------------------------------------
    def __info__(self):
        try:
            s = self.status()
        except RuntimeError as exc:  # address check failed
            return f"GeniX3D '{self._name}' at {self._host}:{self._port}: ERROR - {exc}"
        except Exception as exc:
            return f"GeniX3D '{self._name}' at {self._host}:{self._port}: NO ANSWER ({type(exc).__name__}: {exc})"
        mode = "remote" if s["remote_mode"] else "local"
        state = "ON" if s["xray_on"] else ("STANDBY" if s["standby"] else "OFF")
        shutter = "open" if s["shutter_open"] else ("closed" if s["shutter_closed"] else "?")
        lines = [
            f"GeniX3D '{self._name}' at {self._host}:{self._port} ({mode} mode{', OVERRIDE KEY' if s['override_key'] else ''})",
            f"  X-rays      {state}{'  (warm-up cycle running)' if s['warm_up_cycle'] else ''}",
            f"  high voltage {s['kv']:6.2f} kV   (setpoint {s['kv_setpoint']:.1f} kV)",
            f"  current      {s['ma']:6.2f} mA   (setpoint {s['ma_setpoint']:.2f} mA)",
            f"  interlock    {'OK' if s['interlock_ok'] else 'NOT OK'}",
            f"  enclosure doors {'closed' if s['door_closed'] else 'OPEN (at least one)'}",
            f"  safety shutter {shutter}",
            f"  faults       {', '.join(s['faults']) if s['faults'] else ('fault bit set' if s['fault'] else 'none')}",
            f"  tube         {s['tube_power_w']:.0f} W, operating counter {s['hours']} h {s['minutes']} min",
        ]
        return "\n".join(lines)
