# Findings

Things that cost real time to work out, recorded so they cost nobody else the
same. Every number here was measured on the hardware, not estimated.

## A charge-only USB cable looks exactly like a dead board

The board was invisible: nothing in `lsusb`, no `/dev/ttyACM*`, no `RPI-RP2`
drive. Holding BOOTSEL changed nothing.

The diagnostic that identified it:

```sh
journalctl -k --since "10 minutes ago" | grep -i usb
```

**Total silence across plug/unplug cycles is the tell.** A board that is broken,
mis-flashed, or wrongly powered still produces *something* — an enumeration
attempt, a `device descriptor read/64, error -71`, a port status change. Nothing
at all means the D+/D- lines never make contact, and no amount of BOOTSEL will
help. Suspect the cable first, especially USB-C cables bundled with phones,
power banks and LED strips: plenty carry power only.

Corollary: a board can be visibly powered and completely absent from the host.

## CircuitPython's bytearray has no `__delitem__`

This is a MicroPython limitation CPython does not share:

```python
buf = bytearray(b"abc")
del buf[0]          # fine on CPython, TypeError on the board
```

The frame parser used `del buffer[0]` to resynchronise. It passed a full
desktop test suite and raised `TypeError: 'bytearray' object doesn't support
item deletion` the moment it ran on hardware. All discards are slice-based now.

**The general trap:** a test suite that runs firmware on CPython cannot see
CPython-only constructs. `tests/test_protocol.py` executes `firmware/code.py`
with a `bytearray` subclass whose `__delitem__` raises, which closes this
specific hole. The broader lesson is to be suspicious of any desktop test of
embedded code that passes first time.

## `lsblk` does not always report FAT labels

`flash.sh` originally located the board by label alone and found nothing even
with `RPI-RP2` mounted and visible in the same `lsblk` output — the `LABEL`
column was simply empty, because it depends on udev having probed the volume.
The installer now falls back to matching the mount point's own name, since
automounters name the directory after the label anyway.

## The board has two USB identities

| State | VID:PID | Appears as |
|---|---|---|
| BOOTSEL (bootrom) | `2e8a:0003` | `RPI-RP2` mass storage |
| Running CircuitPython | `239a:80f4` | `CIRCUITPY` + serial + HID |

`2e8a` is Raspberry Pi; `239a` is Adafruit, because the VID belongs to whoever
built the firmware. Tooling that matches only `2e8a` silently fails to find a
board running CircuitPython. Both are matched in `99-pico-kit.rules`.

## Console and data serial ports

CircuitPython exposes two CDC pairs. The console is the **lower** interface
number, the data channel the higher:

```
3-1:1.0  CDC control    console  -> first  /dev/ttyACM*
3-1:1.2  CDC2 control   data     -> second /dev/ttyACM*
```

Probing the console with binary frames writes them into the REPL and can
interrupt running firmware, so `candidate_ports()` drops the lowest interface
whenever a board exposes more than one. Never open a CircuitPython console port
at **1200 baud** — that is the signal to reboot into the bootloader.

## Consumer/media usages ride on the keyboard node

A separate HID device with usage page `0x0C` does not get its own input device.
Volume, mute, play/pause and track skip all arrive on the board's *keyboard*
evdev node as ordinary `KEY_VOLUMEUP`-style events. Verified by capture.

## Hi-res scroll is synthesised by the kernel

```
wheel = 1  ->  REL_WHEEL=1, REL_WHEEL_HI_RES=120
```

The `120` is one full notch, manufactured by the kernel from a plain integer
wheel report. It is not evidence of fine-grained scrolling — real hi-res mice
declare a Resolution Multiplier and send sub-notch steps. See
[LIMITATIONS.md](LIMITATIONS.md).

## Test input can be isolated from the desktop entirely

The board gets its own evdev nodes, distinct from the real keyboard and mouse.
Opening those read-write allows `EVIOCGRAB`, after which their events are
delivered **only** to the grabbing process.

This is what makes an exhaustive test suite safe to run: 210 checks including
`ctrl+alt+t`, every function key and full-screen mouse sweeps, with nothing
typed into a focused window, no cursor movement and no desktop shortcuts fired.
The real keyboard and mouse keep working throughout.

It also produces a better oracle than capturing GUI key events: evdev is the
raw kernel view, before keyboard layout and before personal remaps, and it does
not depend on which window has focus.

## `pkill -f` matches its own command line

Cost three dead shells during this work:

```sh
pkill -f "validate/app.py"     # kills the shell running this command too
```

The usual `[v]alidate` bracket trick only helps if the pattern is the *only*
occurrence in the command line — a `pkill` and a launch in the same command
still self-matches on the launch half. Put the kill in a command of its own.
