"""
code.py -- the HID packet bridge.

Reads framed commands off the CDC data channel and emits them as real USB HID
reports to whatever host the board is plugged into. The host sees a genuine
keyboard and mouse; there is no software injection involved on its side.

Wire format, identical in both directions:

    AB CD  <byte>  <len>  <payload[len]>  <crc>

    request   <byte> is a command   (CMD_*)
    response  <byte> is a status    (ST_*)
    crc       XOR of <byte>, <len>, and every payload byte

A watchdog releases every held key and button if the host goes quiet for
IDLE_RELEASE_MS. Without it, a controller that crashes mid-keystroke leaves a
key physically stuck down on the machine you are typing into -- and the only
cure is unplugging the board.
"""

import time

import supervisor
import usb_cdc
import usb_hid

import hid_layout

VERSION = (1, 0)

MAGIC_0 = 0xAB
MAGIC_1 = 0xCD

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
ST_ERR_CRC = 0x80
ST_ERR_CMD = 0x81
ST_ERR_LEN = 0x82
ST_ERR_USB = 0x83

# Host silence after which held keys and buttons are released. 0 disables the
# watchdog; the host can change it at runtime with CMD_SET_IDLE.
IDLE_RELEASE_MS = 2000

MAX_PAYLOAD = 64

# supervisor.ticks_ms() wraps at 2**29, so raw subtraction breaks roughly every
# six days of uptime. These constants are what CPython's ticks_diff idiom needs.
_TICKS_PERIOD = 1 << 29
_TICKS_MAX = _TICKS_PERIOD - 1
_TICKS_HALF = _TICKS_PERIOD // 2


def ticks_diff(later, earlier):
    """Signed millisecond difference that survives the ticks_ms wrap."""
    return ((later - earlier + _TICKS_HALF) & _TICKS_MAX) - _TICKS_HALF


def resolve_devices():
    """Map each report ID from hid_layout onto the live usb_hid.Device."""
    devices = {}
    for report_id, (usage_page, usage, in_length, _out) in hid_layout.LAYOUT.items():
        for device in usb_hid.devices:
            if device.usage_page == usage_page and device.usage == usage:
                devices[report_id] = (device, in_length)
                break
    return devices


def status_led():
    """The onboard LED, or None on a board that does not expose one."""
    try:
        import board
        import digitalio

        led = digitalio.DigitalInOut(board.LED)
        led.direction = digitalio.Direction.OUTPUT
        return led
    except (ImportError, AttributeError, ValueError):
        return None


def main():
    serial = usb_cdc.data
    led = status_led()

    if serial is None:
        # boot.py never ran, or ran without data=True. Blink fast forever so the
        # failure is visible on a board with no console attached.
        while True:
            if led is not None:
                led.value = not led.value
            time.sleep(0.1)

    serial.timeout = 0
    devices = resolve_devices()
    idle_release_ms = IDLE_RELEASE_MS

    # Cached zero report per device, used by release_all and the watchdog.
    zero_reports = {
        report_id: bytes(in_length) for report_id, (_dev, in_length) in devices.items()
    }
    held = set()
    last_activity = supervisor.ticks_ms()

    def respond(status, payload=b""):
        frame = bytearray((MAGIC_0, MAGIC_1, status, len(payload)))
        frame.extend(payload)
        crc = status ^ len(payload)
        for byte in payload:
            crc ^= byte
        frame.append(crc)
        serial.write(frame)

    def emit(report_id, report):
        """Push one report and remember whether it left anything held down."""
        entry = devices.get(report_id)
        if entry is None:
            return ST_ERR_CMD
        device, in_length = entry
        if len(report) != in_length:
            return ST_ERR_LEN
        try:
            device.send_report(report, report_id)
        except OSError:
            # Host not ready, or the interface is suspended.
            return ST_ERR_USB
        if report_id == hid_layout.REPORT_MOUSE:
            # Movement and wheel deltas are momentary; only buttons stay down.
            resting = report[0] == 0
        else:
            resting = report == zero_reports[report_id]
        if resting:
            held.discard(report_id)
        else:
            held.add(report_id)
        return ST_OK

    def release_all():
        for report_id in sorted(devices):
            try:
                devices[report_id][0].send_report(zero_reports[report_id], report_id)
            except OSError:
                pass
        held.clear()

    def handle(command, payload):
        nonlocal idle_release_ms

        if command == CMD_KEYBOARD:
            return emit(hid_layout.REPORT_KEYBOARD, payload), b""
        if command == CMD_MOUSE:
            return emit(hid_layout.REPORT_MOUSE, payload), b""
        if command == CMD_CONSUMER:
            return emit(hid_layout.REPORT_CONSUMER, payload), b""

        if command == CMD_RAW:
            # Exact passthrough: first byte selects the report, rest is verbatim.
            if not payload:
                return ST_ERR_LEN, b""
            return emit(payload[0], payload[1:]), b""

        if command == CMD_RELEASE_ALL:
            release_all()
            return ST_OK, b""

        if command == CMD_PING:
            return ST_OK, b""

        if command == CMD_GET_LEDS:
            entry = devices.get(hid_layout.REPORT_KEYBOARD)
            if entry is None:
                return ST_ERR_CMD, b""
            report = entry[0].get_last_received_report(hid_layout.REPORT_KEYBOARD)
            return ST_OK, bytes((report[0] if report else 0,))

        if command == CMD_SET_IDLE:
            if len(payload) != 2:
                return ST_ERR_LEN, b""
            idle_release_ms = payload[0] | (payload[1] << 8)
            return ST_OK, b""

        if command == CMD_INFO:
            info = bytearray((VERSION[0], VERSION[1], len(devices)))
            for report_id in sorted(devices):
                info.append(report_id)
                info.append(devices[report_id][1])
            return ST_OK, bytes(info)

        return ST_ERR_CMD, b""

    buffer = bytearray()

    while True:
        waiting = serial.in_waiting
        if waiting:
            buffer.extend(serial.read(waiting))
            last_activity = supervisor.ticks_ms()
            if led is not None:
                led.value = True

            while True:
                # Every discard below is a slice rather than a `del`, because
                # CircuitPython's bytearray has no __delitem__ -- `del buf[0]`
                # raises TypeError on the board while working fine on CPython.

                # Resync: discard anything that is not the start of a frame.
                start = 0
                while start < len(buffer) and buffer[start] != MAGIC_0:
                    start += 1
                if start:
                    buffer = buffer[start:]

                if len(buffer) >= 2 and buffer[1] != MAGIC_1:
                    buffer = buffer[1:]
                    continue
                if len(buffer) < 4:
                    break

                length = buffer[3]
                if length > MAX_PAYLOAD:
                    # Length is impossible, so this was not really a frame start.
                    buffer = buffer[2:]
                    continue

                total = 5 + length
                if len(buffer) < total:
                    break

                command = buffer[2]
                payload = bytes(buffer[4:4 + length])
                received_crc = buffer[total - 1]
                buffer = buffer[total:]

                crc = command ^ length
                for byte in payload:
                    crc ^= byte
                if crc != received_crc:
                    respond(ST_ERR_CRC)
                    continue

                status, reply = handle(command, payload)
                respond(status, reply)
        else:
            if led is not None:
                led.value = False
            if idle_release_ms and held:
                if ticks_diff(supervisor.ticks_ms(), last_activity) > idle_release_ms:
                    release_all()
            time.sleep(0.0005)


main()
