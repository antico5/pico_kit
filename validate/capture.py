"""
capture.py -- read what the kernel actually received from the board.

The board has its own evdev nodes, separate from the real keyboard and mouse,
which makes them the honest place to check our work: this is the raw input
layer, before keyboard layout, before any Caps-Lock-to-Escape style remap, and
with no dependence on which window has focus.

Opening those nodes also lets us EVIOCGRAB them. A grabbed device is delivered
to this process alone, so a test run cannot type into whatever window happens
to be focused, cannot move the cursor, and cannot trigger a desktop shortcut.
The user's real keyboard and mouse are untouched throughout.
"""

import queue
import re
import threading
import time

import evdev
from evdev import ecodes

KEYBOARD_SUFFIX = "Keyboard"
MOUSE_SUFFIX = "Mouse"

# Noise every device emits that says nothing about correctness.
IGNORED_TYPES = (ecodes.EV_SYN, ecodes.EV_MSC)


class CaptureError(RuntimeError):
    pass


def find_board_nodes(name_hint="Pico"):
    """Map 'Keyboard'/'Mouse' to the board's event node paths.

    Parsed out of /proc/bus/input/devices rather than by opening every device,
    so this still works before the permissions are sorted out and can give a
    precise error when they are not.
    """
    found = {}
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError as error:
        raise CaptureError("cannot read /proc/bus/input/devices: %s" % error)

    for block in blocks:
        name = re.search(r'N: Name="([^"]*)"', block)
        node = re.search(r"(event\d+)", block)
        if not name or not node or name_hint not in name.group(1):
            continue
        path = "/dev/input/" + node.group(1)
        if name.group(1).endswith(MOUSE_SUFFIX):
            found[MOUSE_SUFFIX] = path
        elif name.group(1).endswith(KEYBOARD_SUFFIX):
            found[KEYBOARD_SUFFIX] = path
        else:
            found.setdefault(name.group(1), path)
    return found


def describe(event):
    """Human-readable event name, e.g. 'KEY_A' or 'REL_X'."""
    lookup = ecodes.bytype.get(event.type, {})
    name = lookup.get(event.code, event.code)
    if isinstance(name, (list, tuple)):
        name = name[0]
    return str(name)


class Capture:
    """Grabs the board's input devices and queues everything they emit."""

    def __init__(self, exclusive=True, name_hint="Pico"):
        self.exclusive = exclusive
        self.events = queue.Queue()
        self.devices = []
        self._stop = threading.Event()
        self._threads = []

        nodes = find_board_nodes(name_hint)
        if not nodes:
            raise CaptureError(
                "no input devices matching %r. Is the board plugged in and "
                "running the firmware?" % name_hint
            )

        for role, path in sorted(nodes.items()):
            try:
                device = evdev.InputDevice(path)
            except PermissionError:
                raise CaptureError(
                    "cannot open %s (%s).\n"
                    "Install the udev rule and reload it:\n"
                    "  sudo cp 99-pico-kit.rules /etc/udev/rules.d/\n"
                    "  sudo udevadm control --reload-rules && sudo udevadm trigger"
                    % (path, role)
                )
            except OSError as error:
                raise CaptureError("cannot open %s: %s" % (path, error))
            self.devices.append((role, device))

    def start(self):
        for role, device in self.devices:
            if self.exclusive:
                try:
                    device.grab()
                except OSError as error:
                    raise CaptureError(
                        "cannot take exclusive access to %s: %s" % (device.path, error)
                    )
            thread = threading.Thread(
                target=self._pump, args=(role, device), daemon=True
            )
            thread.start()
            self._threads.append(thread)
        return self

    def _pump(self, role, device):
        try:
            for event in device.read_loop():
                if self._stop.is_set():
                    return
                if event.type in IGNORED_TYPES:
                    continue
                self.events.put((role, event))
        except OSError:
            return          # device went away, e.g. the board was unplugged

    def drain(self):
        """Discard anything queued so far."""
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return

    def collect(self, settle=0.12, limit=2.0):
        """Gather events until the device has been quiet for `settle` seconds."""
        gathered = []
        deadline = time.monotonic() + limit
        last = time.monotonic()
        while time.monotonic() < deadline:
            timeout = max(0.0, settle - (time.monotonic() - last))
            try:
                gathered.append(self.events.get(timeout=timeout or 0.01))
                last = time.monotonic()
            except queue.Empty:
                if gathered:
                    break
                if time.monotonic() - last >= settle:
                    break
        return gathered

    def stop(self):
        self._stop.set()
        for _role, device in self.devices:
            try:
                if self.exclusive:
                    device.ungrab()
            except OSError:
                pass
            try:
                device.close()
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exception):
        self.stop()
