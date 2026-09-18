# pico_kit

Turns a Raspberry Pi Pico (RP2040) into a USB keyboard and mouse you drive by
sending it exact HID reports. The host OS sees a genuine HID device — there is
no software input injection on its side, so it works in places where `xdotool`
and friends do not (other users' sessions, the login screen, a VM with USB
passthrough, anything reading raw input).

One USB cable does both jobs: it carries the control channel *and* delivers the
resulting keystrokes.

```
  ┌─────────────┐
  │ this laptop │
  └──────┬──────┘
         │ one USB cable
         │  ├─ CDC serial   host → board: "send this report"
         │  └─ HID kbd+mouse board → host: real input events
         ▼
   ┌────────────┐
   │ Pico RP2040│
   └────────────┘
```

## Install

Hold **BOOTSEL** while plugging the board in, then:

```sh
./flash.sh
```

That installs CircuitPython 10.3.1 and copies the firmware. Unplug and replug
when it finishes — `boot.py` defines the USB descriptors, and those only take
effect on a hard reset. Then:

```sh
python3 host/picohid.py info
```

## Serial port permissions

The CDC ports are `root:dialout`. Rather than joining `dialout` (which needs a
full re-login), install the bundled udev rule once:

```sh
sudo cp 99-pico-kit.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

It grants the port to whoever is logged in at the physical seat, and takes
effect the next time the board is plugged in.

To update just the firmware later (CircuitPython already installed):

```sh
./flash.sh --code-only
```

## Command line

```sh
python3 host/picohid.py type "hello world"
python3 host/picohid.py tap ctrl+alt+t
python3 host/picohid.py move 200 -100 --step 20
python3 host/picohid.py click left
python3 host/picohid.py scroll -3
python3 host/picohid.py leds            # read the host's caps/num lock state
python3 host/picohid.py release         # panic button: drop everything held

# exact report bytes: first value is the report id, rest is verbatim
python3 host/picohid.py raw 01 02 00 04 00 00 00 00 00   # shift+a
```

## Library

```python
from picohid import PicoHID

with PicoHID() as hid:
    hid.type("hello world\n")
    hid.tap("ctrl+alt+t")
    hid.press("shift"); hid.move(0, -50); hid.release("shift")
    hid.click("right")
    hid.send_raw(1, b"\x02\x00\x04\x00\x00\x00\x00\x00")   # byte-exact
```

`PicoHID()` autodetects the port, and a background thread keeps the connection
alive so held keys stay held. Leaving the `with` block releases everything.

## One process at a time

The control channel is request/response with no sequence numbers, so two
processes sharing it do not politely interleave -- each consumes the other's
replies and both desync. `PicoHID` therefore takes an exclusive `flock` on the
port; a second opener fails immediately with a message naming the process that
holds it, rather than silently corrupting the stream.

This matters in practice because the validator GUI keeps the port open, with a
keepalive thread, for as long as its window is up. Close it before using the
CLI.

## Throughput

The HID endpoint declares `bInterval = 8`, so the board delivers at most
**125 reports/second**. That is a hardcoded constant in CircuitPython's
descriptor template, not a hardware limit -- the RP2040 is a full-speed device,
where `bInterval` may legally be 1 (1000 Hz).

The control path is not the bottleneck: a ping round trip measures ~1.9 ms
(~520/s), while any command that emits a HID report measures exactly 125/s,
because `send_report()` blocks until the endpoint drains.

## Report layouts

Defined once in `firmware/hid_layout.py`, imported by both `boot.py` and
`code.py` so they cannot drift apart.

| ID | Device   | Len | Layout                                                    |
|----|----------|-----|-----------------------------------------------------------|
| 1  | keyboard | 8   | `mods, reserved, k1..k6` — the standard boot layout        |
| 2  | mouse    | 7   | `buttons, dx:i16le, dy:i16le, wheel:i8, pan:i8`            |
| 3  | consumer | 2   | `usage:u16le` — media keys                                 |

The mouse uses 16-bit deltas rather than the classic 8-bit ones, so a large
move is one report instead of a stutter of ±127 steps.

## Wire protocol

Identical framing in both directions:

```
AB CD  <byte>  <len>  <payload[len]>  <crc>
```

`<byte>` is a command going out and a status coming back; `crc` is the XOR of
`<byte>`, `<len>` and every payload byte. The firmware resynchronises on the
`AB CD` magic, so a garbled or partial write cannot wedge it.

| Cmd  | Name        | Payload                          |
|------|-------------|----------------------------------|
| 0x01 | keyboard    | 8-byte report                    |
| 0x02 | mouse       | 7-byte report                    |
| 0x03 | consumer    | 2-byte report                    |
| 0x10 | raw         | `report_id` + verbatim report    |
| 0x20 | release all | —                                |
| 0x21 | ping        | —                                |
| 0x22 | get LEDs    | — → 1 byte of host LED state     |
| 0x23 | set idle    | `u16le` watchdog ms, 0 disables  |
| 0x24 | info        | — → version and report lengths   |

Statuses: `0x00` ok, `0x80` bad checksum, `0x81` unknown command or report id,
`0x82` wrong length, `0x83` USB host not ready.

## The watchdog

If the host goes quiet for 2000 ms while a key or button is held, the firmware
releases everything. Without it, a controller that dies mid-keystroke leaves a
key physically stuck down on the machine you are typing into, and the only cure
is unplugging the board. `hid.set_idle_release(0)` disables it.

## Validation harness

`validate/` drives the board and checks what the **kernel actually received**,
rather than trusting that a report went out. It reads the board's own evdev
nodes, which is the honest place to look: raw input, before keyboard layout and
before any personal remap, and independent of which window has focus.

```sh
/usr/bin/python3 validate/app.py          # GTK4 app
/usr/bin/python3 validate/suite.py        # same checks, headless
/usr/bin/python3 validate/suite.py --category Wheel --json report.json
```

It needs the system interpreter: `gi` and `evdev` live there, not in the
Homebrew Python.

**Isolated mode** (the default) takes exclusive access to the board's input
devices with `EVIOCGRAB`. Test events then reach this harness and nothing else
-- no keystrokes land in whatever window is focused, no cursor motion, no
desktop shortcuts fire -- while your real keyboard and mouse keep working. Pass
`--live` (or untoggle **Isolated**) to let events through to the desktop.

210 checks across 22 categories:

| Area | Covers |
|------|--------|
| Keyboard | all 8 modifier bits, a-z, A-Z, digits, shifted symbols, punctuation, F1-F12, navigation, keypad, locks, combos |
| Rollover | 6 simultaneous keys, and that a 7th is dropped cleanly |
| Mouse | 5 buttons, double click, drag across motion |
| Motion | ±1 to ±32767 on each axis, diagonals, wheel, horizontal pan |
| Consumer | volume, mute, play/pause, track skip |
| Protocol | malformed length and unknown report id are rejected, not forwarded |

The motion range matters: values past ±127 are the ones that prove the 16-bit
descriptor works, since a classic 8-bit mouse report cannot express them.

## Tests

```sh
python3 tests/test_protocol.py
```

Runs the real `firmware/code.py` in a thread with CircuitPython stubbed out,
wired directly to the real `host/picohid.py`. Covers framing, checksums,
resync, every command and the watchdog — no hardware needed.

## Documentation

- [docs/FINDINGS.md](docs/FINDINGS.md) - platform gotchas that cost real time:
  charge-only cables, CircuitPython's bytearray, the board's two USB identities,
  isolating test input with `EVIOCGRAB`
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md) - measured limits and what causes
  them; why 125 Hz, and what would move it
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md) - what a real keyboard and mouse do
  that this cannot, each claim backed by a descriptor reading
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - design rationale, scaling to
  multiple processes, and porting to Windows

## Layout

```
firmware/hid_layout.py   report descriptors and layout table (shared)
firmware/boot.py         builds the USB devices, enables the CDC data channel
firmware/code.py         the packet bridge
host/picohid.py          driver and CLI (no dependencies)
host/keymap.py           US-layout key names and character mapping
tests/test_protocol.py   loopback test
validate/capture.py      reads the board's own evdev nodes, grabs them
validate/suite.py        the 210 checks, plus a headless runner
validate/app.py          GTK4 front end
flash.sh                 installer
99-pico-kit.rules        udev rule for serial and input access
```
