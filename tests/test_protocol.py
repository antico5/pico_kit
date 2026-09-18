#!/usr/bin/env python3
"""
Loopback test: the real host driver against the real firmware loop.

Everything CircuitPython-specific is stubbed, so firmware/code.py runs
unmodified in a thread on this machine with its serial port wired straight to
picohid's. That exercises the framing, checksums, resync, every command and the
watchdog without needing the board plugged in.
"""

import os
import struct
import sys
import threading
import time
import types
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "..", "host"))
sys.path.insert(0, os.path.join(ROOT, "..", "firmware"))

import hid_layout  # noqa: E402  (needs the firmware path above)


class CircuitPythonBytearray(bytearray):
    """A bytearray that behaves the way CircuitPython's does.

    MicroPython, and so CircuitPython, implements no __delitem__ for bytearray.
    CPython does, so firmware using `del buf[0]` sails through a desktop test
    and then dies on the board. Slices return this same type so the restriction
    survives the reslicing a parser does.
    """

    def __delitem__(self, index):
        raise TypeError("'bytearray' object doesn't support item deletion")

    def __getitem__(self, index):
        result = bytearray.__getitem__(self, index)
        if isinstance(index, slice):
            return CircuitPythonBytearray(result)
        return result


class Pipe:
    """One direction of a serial link."""

    def __init__(self):
        self.buffer = bytearray()
        self.lock = threading.Lock()

    def put(self, data):
        with self.lock:
            self.buffer.extend(data)

    def take(self, count):
        with self.lock:
            chunk = bytes(self.buffer[:count])
            del self.buffer[:count]
            return chunk

    def pending(self):
        with self.lock:
            return len(self.buffer)


class FakeDevice:
    """Stands in for usb_hid.Device, recording what the firmware sends."""

    def __init__(self, usage_page, usage, report_id):
        self.usage_page = usage_page
        self.usage = usage
        self.report_id = report_id
        self.sent = []
        self.led_report = None

    def send_report(self, report, report_id=None):
        self.sent.append((report_id, bytes(report)))

    def get_last_received_report(self, report_id=None):
        return self.led_report


class FirmwareSerial:
    """usb_cdc.data as seen from inside the firmware."""

    def __init__(self, inbound, outbound):
        self.inbound, self.outbound = inbound, outbound
        self.timeout = 0

    @property
    def in_waiting(self):
        return self.inbound.pending()

    def read(self, count):
        return self.inbound.take(count)

    def write(self, data):
        self.outbound.put(data)


class HostPort:
    """The _Port interface picohid expects, backed by the same pipes."""

    def __init__(self, inbound, outbound):
        self.inbound, self.outbound = inbound, outbound

    def write(self, data):
        self.outbound.put(data)

    def read(self, count, deadline):
        out = bytearray()
        while len(out) < count and time.monotonic() < deadline:
            chunk = self.inbound.take(count - len(out))
            if chunk:
                out.extend(chunk)
            else:
                time.sleep(0.0005)
        return bytes(out)

    def flush_input(self):
        self.inbound.take(self.inbound.pending())

    def close(self):
        pass


def install_circuitpython_stubs(serial, devices):
    usb_cdc = types.ModuleType("usb_cdc")
    usb_cdc.data = serial
    usb_cdc.console = None

    usb_hid = types.ModuleType("usb_hid")
    usb_hid.devices = devices

    supervisor = types.ModuleType("supervisor")
    supervisor.ticks_ms = lambda: int(time.monotonic() * 1000) & ((1 << 29) - 1)

    sys.modules["usb_cdc"] = usb_cdc
    sys.modules["usb_hid"] = usb_hid
    sys.modules["supervisor"] = supervisor
    # No `board` module, so the firmware's status_led() returns None, exactly as
    # it would on a board without an onboard LED.
    sys.modules.pop("board", None)


class ProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.to_firmware, cls.to_host = Pipe(), Pipe()

        cls.devices = [
            FakeDevice(page, usage, report_id)
            for report_id, (page, usage, _in, _out) in sorted(hid_layout.LAYOUT.items())
        ]
        cls.keyboard, cls.mouse, cls.consumer = cls.devices

        install_circuitpython_stubs(
            FirmwareSerial(cls.to_firmware, cls.to_host), cls.devices
        )

        # Run code.py with CircuitPython's stricter bytearray in place of the
        # builtin. Without this the test passes on constructs the board rejects
        # at runtime -- `del buf[0]` being the one that actually bit us.
        def run_firmware():
            source = open(os.path.join(ROOT, "..", "firmware", "code.py")).read()
            namespace = {"__name__": "__main__", "bytearray": CircuitPythonBytearray}
            exec(compile(source, "code.py", "exec"), namespace)
        cls.firmware_thread = threading.Thread(target=run_firmware, daemon=True)
        cls.firmware_thread.start()
        time.sleep(0.1)

        import picohid
        picohid._Port = lambda _path: HostPort(cls.to_host, cls.to_firmware)
        cls.picohid = picohid
        cls.hid = picohid.PicoHID(port="loopback", keepalive=False)

    def setUp(self):
        for device in self.devices:
            device.sent.clear()

    def test_info_reports_layout(self):
        details = self.hid.info()
        self.assertEqual(details["version"], "1.0")
        self.assertEqual(details["reports"], {1: 8, 2: 7, 3: 2})

    def test_keyboard_report_is_verbatim(self):
        self.hid.keyboard_report(0x02, [0x04])
        self.assertEqual(
            self.keyboard.sent, [(1, bytes([0x02, 0, 0x04, 0, 0, 0, 0, 0]))]
        )

    def test_raw_passthrough_is_byte_exact(self):
        report = bytes([0x01, 0x00, 0x1D, 0x1B, 0x06, 0x00, 0x00, 0x00])
        self.hid.send_raw(1, report)
        self.assertEqual(self.keyboard.sent, [(1, report)])

    def test_mouse_packs_signed_16_bit_deltas(self):
        self.hid.mouse_report(buttons=0x01, dx=-300, dy=1200, wheel=-3, pan=2)
        self.assertEqual(
            self.mouse.sent, [(2, struct.pack("<Bhhbb", 0x01, -300, 1200, -3, 2))]
        )

    def test_consumer_report(self):
        self.hid.consumer_report(0x00E9)
        self.assertEqual(self.consumer.sent, [(3, struct.pack("<H", 0x00E9))])

    def test_wrong_length_is_rejected(self):
        with self.assertRaises(self.picohid.PicoHIDError) as caught:
            self.hid.send_raw(1, b"\x00\x00")
        self.assertIn("length", str(caught.exception))
        self.assertEqual(self.keyboard.sent, [])

    def test_unknown_report_id_is_rejected(self):
        with self.assertRaises(self.picohid.PicoHIDError) as caught:
            self.hid.send_raw(9, b"\x00")
        self.assertIn("unknown", str(caught.exception))

    def test_bad_checksum_is_rejected(self):
        frame = bytearray(self.picohid.MAGIC)
        frame += bytes([self.picohid.CMD_KEYBOARD, 8]) + bytes(8) + b"\xff"
        self.to_firmware.put(frame)
        time.sleep(0.05)
        self.assertEqual(self.keyboard.sent, [])
        self.hid.port.flush_input()

    def test_resyncs_after_leading_garbage(self):
        self.to_firmware.put(b"\x00\xff\xab\x99junk")
        time.sleep(0.02)
        self.hid.port.flush_input()
        self.hid.keyboard_report(0, [0x05])
        self.assertEqual(self.keyboard.sent[-1], (1, bytes([0, 0, 0x05, 0, 0, 0, 0, 0])))

    def test_type_emits_shift_for_uppercase(self):
        self.hid.type("aA", delay=0)
        pressed = [report for _id, report in self.keyboard.sent if report[2] != 0]
        self.assertEqual(pressed[0][0], 0x00)   # 'a' unshifted
        self.assertEqual(pressed[1][0], 0x02)   # 'A' with left shift
        self.assertEqual(pressed[0][2], pressed[1][2])

    def test_leds_decode_from_host_report(self):
        self.keyboard.led_report = bytes([0x02])
        self.assertTrue(self.hid.leds()["caps_lock"])
        self.keyboard.led_report = bytes([0x00])
        self.assertFalse(self.hid.leds()["caps_lock"])

    def test_watchdog_releases_held_keys(self):
        self.hid.set_idle_release(120)
        try:
            self.hid.keyboard_report(0x02, [0x04])     # hold shift+a
            self.mouse.sent.clear()
            self.hid.mouse_report(buttons=0x01)        # hold left button
            self.keyboard.sent.clear()
            self.mouse.sent.clear()
            time.sleep(0.45)
            self.assertEqual(self.keyboard.sent[-1], (1, bytes(8)))
            self.assertEqual(self.mouse.sent[-1], (2, bytes(7)))
        finally:
            self.hid.set_idle_release(2000)

    def test_watchdog_leaves_idle_devices_alone(self):
        self.hid.set_idle_release(120)
        try:
            self.hid.release_all()
            self.keyboard.sent.clear()
            time.sleep(0.45)
            self.assertEqual(self.keyboard.sent, [], "should not resend when nothing held")
        finally:
            self.hid.set_idle_release(2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
