#!/usr/bin/env python3
"""
picohid.py -- host-side driver for the pico_kit bridge.

Speaks the framed packet protocol from firmware/code.py over the board's CDC
data channel. Deliberately dependency-free: a CDC ACM port is just a character
device, so termios does everything pyserial would.

    from picohid import PicoHID

    with PicoHID() as hid:
        hid.type("hello world\\n")
        hid.tap("ctrl+alt+t")
        hid.move(120, -40)
        hid.click("left")
        hid.send_raw(1, b"\\x02\\x00\\x04\\x00\\x00\\x00\\x00\\x00")  # shift+a, verbatim

Run `python3 picohid.py --help` for the command line.
"""

import argparse
import fcntl
import glob
import os
import select
import struct
import sys
import termios
import threading
import time
import tty

import keymap

MAGIC = b"\xab\xcd"

CMD_KEYBOARD = 0x01
CMD_MOUSE = 0x02
CMD_CONSUMER = 0x03
CMD_RAW = 0x10
CMD_RELEASE_ALL = 0x20
CMD_PING = 0x21
CMD_GET_LEDS = 0x22
CMD_SET_IDLE = 0x23
CMD_INFO = 0x24

ST_OK = 0x00
STATUS_TEXT = {
    0x80: "bad checksum",
    0x81: "unknown command or report id",
    0x82: "wrong payload length",
    0x83: "USB host not ready",
}

REPORT_KEYBOARD = 1
REPORT_MOUSE = 2
REPORT_CONSUMER = 3

# 2e8a is the RP2040 bootrom and MicroPython; 239a is what CircuitPython
# enumerates as once it is running.
BOARD_VIDS = ("2e8a", "239a")

# Keep well under the firmware's 2000 ms watchdog so held keys never drop.
KEEPALIVE_INTERVAL = 0.5


class PicoHIDError(RuntimeError):
    pass


class PortBusy(PicoHIDError):
    """Another process already holds the board's control channel."""


def _holder_of(path):
    """Best-effort pid holding `path` open, for a useful error message."""
    try:
        target = os.path.realpath(path)
    except OSError:
        return None
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            for descriptor in glob.glob(entry + "/fd/*"):
                if os.path.realpath(descriptor) == target:
                    pid = os.path.basename(entry)
                    with open(entry + "/cmdline", "rb") as handle:
                        command = handle.read().replace(b"\0", b" ").decode(
                            "utf8", "replace").strip()
                    return "pid %s (%s)" % (pid, command or "?")
        except OSError:
            continue
    return None


def _read_file(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def candidate_ports():
    """Ports worth probing, most likely first.

    Two rules. A board's console sits on a lower interface number than its data
    channel, so when one board exposes several ports the lowest is the console
    and gets dropped -- probing it would write frame bytes into the REPL and can
    interrupt the running firmware. Whatever is left is tried highest interface
    first, known board vendors before anything else.
    """
    ports = []
    for path in sorted(glob.glob("/dev/ttyACM*")):
        device = "/sys/class/tty/%s/device" % os.path.basename(path)
        raw_interface = _read_file(os.path.join(device, "bInterfaceNumber"))
        try:
            interface = int(raw_interface, 16)
        except (TypeError, ValueError):
            interface = -1
        ports.append({
            "path": path,
            "board": os.path.realpath(os.path.join(device, "..")),
            "vendor": _read_file(os.path.join(device, "..", "idVendor")),
            "interface": interface,
        })

    siblings = {}
    for port in ports:
        siblings.setdefault(port["board"], []).append(port["interface"])

    usable = [
        port for port in ports
        if len(siblings[port["board"]]) == 1
        or port["interface"] != min(siblings[port["board"]])
    ]
    usable.sort(key=lambda port: (
        0 if port["vendor"] in BOARD_VIDS else 1, -port["interface"],
    ))
    return [port["path"] for port in usable]


class _Port:
    """Minimal raw-mode serial port over a tty character device."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # One writer at a time. The protocol is request/response with no
            # sequence numbers, so two processes sharing the port do not merely
            # interleave -- each consumes the other's replies and both desync.
            # flock is advisory, which is enough: every tool here goes through
            # this class.
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder = _holder_of(path)
                raise PortBusy(
                    "%s is already open by another process%s"
                    % (path, ": " + holder if holder else ""))
            tty.setraw(self.fd)
            attributes = termios.tcgetattr(self.fd)
            # Any speed works on CDC ACM, but never 1200: CircuitPython treats a
            # 1200-baud open on its console port as "reboot into the bootloader".
            attributes[4] = termios.B115200
            attributes[5] = termios.B115200
            attributes[6][termios.VMIN] = 0
            attributes[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attributes)
        except Exception:
            os.close(self.fd)
            raise

    def write(self, data):
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.fd, view):]
            except BlockingIOError:
                select.select([], [self.fd], [], 1.0)

    def read(self, count, deadline):
        out = bytearray()
        while len(out) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not select.select([self.fd], [], [], remaining)[0]:
                break
            try:
                chunk = os.read(self.fd, count - len(out))
            except BlockingIOError:
                continue
            if chunk:
                out.extend(chunk)
        return bytes(out)

    def flush_input(self):
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def close(self):
        os.close(self.fd)


def _checksum(head, payload):
    crc = head ^ len(payload)
    for byte in payload:
        crc ^= byte
    return crc


class PicoHID:
    """A connection to the board, plus conveniences built on raw reports."""

    def __init__(self, port=None, timeout=0.5, keepalive=True):
        self.timeout = timeout
        self._lock = threading.RLock()
        self._last_traffic = 0.0
        self._modifiers = 0
        self._keys = []
        self._buttons = 0
        self._closing = threading.Event()

        self.port = self._connect(port)

        self._keepalive = None
        if keepalive:
            self._keepalive = threading.Thread(target=self._keepalive_loop, daemon=True)
            self._keepalive.start()

    def _connect(self, port):
        candidates = [port] if port else candidate_ports()
        if not candidates:
            raise PicoHIDError(
                "no /dev/ttyACM* found -- is the board plugged in and running code.py?"
            )
        failures = []
        for path in candidates:
            try:
                attempt = _Port(path)
            except PortBusy as error:
                failures.append(str(error))
                continue
            except OSError as error:
                failures.append("%s: %s" % (path, error))
                continue
            self.port = attempt
            try:
                self._transact(CMD_PING)
                return attempt
            except PicoHIDError as error:
                failures.append("%s: %s" % (path, error))
                attempt.close()
        raise PicoHIDError("no responding board. Tried:\n  " + "\n  ".join(failures))

    # -- protocol -----------------------------------------------------------

    def _transact(self, command, payload=b""):
        with self._lock:
            frame = bytearray(MAGIC)
            frame.append(command)
            frame.append(len(payload))
            frame.extend(payload)
            frame.append(_checksum(command, payload))

            self.port.flush_input()
            self.port.write(frame)
            self._last_traffic = time.monotonic()

            deadline = time.monotonic() + self.timeout
            header = self.port.read(4, deadline)
            if len(header) < 4:
                raise PicoHIDError("timed out waiting for a reply")
            if header[0:2] != MAGIC:
                raise PicoHIDError("garbled reply header %r" % (header,))

            status, length = header[2], header[3]
            rest = self.port.read(length + 1, deadline)
            if len(rest) < length + 1:
                raise PicoHIDError("truncated reply")

            reply, crc = rest[:length], rest[length]
            if crc != _checksum(status, reply):
                raise PicoHIDError("bad checksum on reply")
            if status != ST_OK:
                raise PicoHIDError(STATUS_TEXT.get(status, "error 0x%02x" % status))
            return reply

    def _keepalive_loop(self):
        while not self._closing.wait(KEEPALIVE_INTERVAL / 2):
            if time.monotonic() - self._last_traffic < KEEPALIVE_INTERVAL:
                continue
            try:
                self._transact(CMD_PING)
            except (PicoHIDError, OSError):
                return

    # -- raw reports --------------------------------------------------------

    def send_raw(self, report_id, report):
        """Send one report verbatim. The exact bytes land on the USB wire."""
        return self._transact(CMD_RAW, bytes([report_id]) + bytes(report))

    def keyboard_report(self, modifiers=0, keys=()):
        keys = list(keys)[:6]
        if len(keys) > 6:
            raise ValueError("a keyboard report holds at most 6 keys")
        payload = bytes([modifiers, 0] + keys + [0] * (6 - len(keys)))
        self._transact(CMD_KEYBOARD, payload)
        return payload

    def mouse_report(self, buttons=0, dx=0, dy=0, wheel=0, pan=0):
        payload = struct.pack("<Bhhbb", buttons, dx, dy, wheel, pan)
        self._transact(CMD_MOUSE, payload)
        return payload

    def consumer_report(self, usage):
        payload = struct.pack("<H", usage)
        self._transact(CMD_CONSUMER, payload)
        return payload

    def release_all(self):
        self._modifiers, self._keys, self._buttons = 0, [], 0
        self._transact(CMD_RELEASE_ALL)

    def ping(self):
        self._transact(CMD_PING)

    def leds(self):
        """Host LED state: dict of num/caps/scroll/compose/kana booleans."""
        bits = self._transact(CMD_GET_LEDS)[0]
        names = ("num_lock", "caps_lock", "scroll_lock", "compose", "kana")
        return {name: bool(bits & (1 << i)) for i, name in enumerate(names)}

    def set_idle_release(self, milliseconds):
        """Watchdog window. 0 disables auto-release of held keys."""
        self._transact(CMD_SET_IDLE, struct.pack("<H", milliseconds))

    def info(self):
        data = self._transact(CMD_INFO)
        reports = {data[3 + i * 2]: data[4 + i * 2] for i in range(data[2])}
        return {"version": "%d.%d" % (data[0], data[1]), "reports": reports}

    # -- keyboard -----------------------------------------------------------

    def _flush_keyboard(self):
        self.keyboard_report(self._modifiers, self._keys)

    def press(self, combo):
        modifiers, keys = keymap.parse_combo(combo)
        self._modifiers |= modifiers
        for usage in keys:
            if usage not in self._keys:
                self._keys.append(usage)
        self._flush_keyboard()

    def release(self, combo):
        modifiers, keys = keymap.parse_combo(combo)
        self._modifiers &= ~modifiers
        self._keys = [usage for usage in self._keys if usage not in keys]
        self._flush_keyboard()

    def tap(self, combo, hold=0.02):
        self.press(combo)
        time.sleep(hold)
        self.release(combo)

    def type(self, text, delay=0.006):
        """Type literal text on a US layout."""
        for character in text:
            entry = keymap.CHAR_MAP.get(character)
            if entry is None:
                raise ValueError("no US-layout key for %r" % character)
            usage, needs_shift = entry
            self.keyboard_report(0x02 if needs_shift else 0x00, [usage])
            time.sleep(delay)
            self.keyboard_report(self._modifiers, self._keys)
            time.sleep(delay)

    # -- mouse --------------------------------------------------------------

    def move(self, dx=0, dy=0, step=None):
        """Relative move. `step` splits it into smaller reports, like real motion."""
        if step is None:
            self.mouse_report(self._buttons, dx, dy)
            return
        remaining_x, remaining_y = dx, dy
        while remaining_x or remaining_y:
            chunk_x = max(-step, min(step, remaining_x))
            chunk_y = max(-step, min(step, remaining_y))
            self.mouse_report(self._buttons, chunk_x, chunk_y)
            remaining_x -= chunk_x
            remaining_y -= chunk_y
            time.sleep(0.002)

    def button_down(self, button="left"):
        self._buttons |= keymap.MOUSE_BUTTON[str(button).lower()]
        self.mouse_report(self._buttons)

    def button_up(self, button="left"):
        self._buttons &= ~keymap.MOUSE_BUTTON[str(button).lower()]
        self.mouse_report(self._buttons)

    def click(self, button="left", hold=0.02):
        self.button_down(button)
        time.sleep(hold)
        self.button_up(button)

    def scroll(self, amount=0, pan=0):
        self.mouse_report(self._buttons, 0, 0, amount, pan)

    # -- lifecycle ----------------------------------------------------------

    def close(self):
        self._closing.set()
        if self._keepalive is not None:
            self._keepalive.join(timeout=1.0)
        try:
            self.release_all()
        except (PicoHIDError, OSError):
            pass
        self.port.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Drive the Pico as a USB keyboard and mouse.",
        epilog="All numbers for `raw` are hex; the first is the report id.",
    )
    parser.add_argument("-p", "--port", help="serial port (default: autodetect)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="firmware version and report layout")
    sub.add_parser("leds", help="read the host's keyboard LED state")
    sub.add_parser("release", help="release every held key and button")

    p = sub.add_parser("type", help="type literal text")
    p.add_argument("text")
    p.add_argument("--delay", type=float, default=0.006)

    p = sub.add_parser("tap", help="tap a key combo, e.g. ctrl+alt+t")
    p.add_argument("combo", nargs="+")

    p = sub.add_parser("move", help="move the mouse by a relative amount")
    p.add_argument("dx", type=int)
    p.add_argument("dy", type=int)
    p.add_argument("--step", type=int, default=None, help="split into steps of this size")

    p = sub.add_parser("click", help="click a mouse button")
    p.add_argument("button", nargs="?", default="left")

    p = sub.add_parser("scroll", help="scroll the wheel")
    p.add_argument("amount", type=int)

    p = sub.add_parser("raw", help="send an exact report: REPORT_ID BYTE BYTE ...")
    p.add_argument("bytes", nargs="+", metavar="HEX")

    args = parser.parse_args(argv)

    try:
        hid = PicoHID(port=args.port)
    except PicoHIDError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1

    with hid:
        if args.command == "info":
            details = hid.info()
            print("firmware %s" % details["version"])
            for report_id, length in sorted(details["reports"].items()):
                print("  report %d: %d bytes" % (report_id, length))
        elif args.command == "leds":
            for name, state in hid.leds().items():
                print("%-12s %s" % (name, "on" if state else "off"))
        elif args.command == "release":
            hid.release_all()
        elif args.command == "type":
            hid.type(args.text, delay=args.delay)
        elif args.command == "tap":
            for combo in args.combo:
                hid.tap(combo)
        elif args.command == "move":
            hid.move(args.dx, args.dy, step=args.step)
        elif args.command == "click":
            hid.click(args.button)
        elif args.command == "scroll":
            hid.scroll(args.amount)
        elif args.command == "raw":
            values = [int(token, 16) for token in args.bytes]
            hid.send_raw(values[0], bytes(values[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
