# Architecture

Why the pieces are shaped the way they are, and what the options are for
growing this into something an automation kit drives.

## Layering

```
  automation code
        |
  host/picohid.py      driver: framing, port locking, key/mouse helpers
        |  CDC serial, framed packets
  firmware/code.py     bridge: parse frame -> usb_hid.send_report()
        |  USB HID
      the host OS
        |
  validate/            reads the board's own evdev nodes back, to check
                       what the kernel actually received
```

The validation layer is deliberately not in the data path. It exists to answer
"did the OS receive what we sent", which is a different question from "did the
call succeed", and it is the only one worth trusting.

## Decisions worth explaining

### Report layouts live in one file

`firmware/hid_layout.py` is imported by both `boot.py` (which builds the
`usb_hid.Device` objects) and `code.py` (which validates incoming lengths and
routes by report ID). A descriptor and the code that feeds it drifting apart is
a genuinely nasty class of bug — the device enumerates fine and then reports
garbage — so the two cannot be edited independently.

### 16-bit mouse deltas, not the classic 8-bit

A classic mouse report carries `int8` X/Y, capping a single report at +-127
pixels. Large movements then arrive as a stutter of many reports, which at 125
reports/second is visible. `int16` makes any movement one report. The cost is
losing boot-protocol compatibility, which this device does not have anyway (see
[LIMITATIONS.md](LIMITATIONS.md)).

### A watchdog releases held keys

If the host goes quiet for 2000 ms with a key or button held, the firmware
releases everything. Without it, a controller that dies mid-keystroke leaves a
key **physically** stuck down on the machine being typed into, and the only cure
is unplugging the board. `set_idle_release(0)` disables it; the driver's
keepalive thread refreshes the timer so deliberate long holds still work.

### The port is locked exclusively

The protocol is request/response with **no sequence numbers**, so two processes
sharing the port do not politely interleave — each consumes the other's replies
and both desync immediately. This was not theoretical: the validator GUI holds
the port open with a keepalive thread, and any CLI command run alongside it
corrupted the stream, producing `garbled reply header b'\x00\xcd\x00\x00'`.

A `threading.RLock` does not help, because it protects threads within one
process. `PicoHID` now takes an exclusive `flock` and reports the holder:

```
/dev/ttyACM1 is already open by another process: pid 500922 (python3 validate/app.py)
```

`flock` is advisory, which is sufficient here because every tool in this project
goes through the same class.

### The host driver has no dependencies

A CDC ACM port is an ordinary character device, so `termios` does everything
`pyserial` would. This keeps the driver runnable under any Python on the system
— which mattered in practice, since the validation harness must run under the
system interpreter (where `gi` and `evdev` live) while the CLI is convenient
under whichever Python is on `PATH`.

## Scaling to an automation kit

### The decision is about process count, not speed

IPC overhead is negligible against the hardware budget — a Unix socket round
trip is 7.4 us and a TCP loopback round trip 20.2 us, against 8000 us per HID
report. **A server hop costs about a quarter of one percent.** Latency should
not drive this choice.

What drives it is exclusivity:

| Shape | What works |
|---|---|
| One automation process | The library. Nothing to build; `import picohid`. |
| Several concurrent processes | A server is **required** — a library physically cannot serve them, the second gets `PortBusy`. |
| Automation on another machine | A server, with authentication. |

### Recommended shape

Not either/or. The library stays the core, and a server becomes a thin wrapper
around the same `PicoHID` object — roughly 150 lines, holding the port and
serialising client requests onto it. Clients that can link the library do so;
clients that cannot, or that must share, use the socket.

### If you expose it over TCP, treat it as privileged

This is an input-injection endpoint. Anything that can reach the port can type
into the machine as a hardware keyboard, which a local firewall will not save
you from once a client is compromised. Bind loopback by default; require an
explicit flag plus a shared secret for anything else.

### A batch command will become necessary

Today each report costs one serial round trip (1.93 ms), comfortably hidden
behind the 8 ms endpoint wait. If the polling rate is ever raised (see
[PERFORMANCE.md](PERFORMANCE.md)), the serial round trip becomes the bottleneck
and strict request/response caps throughput below what USB allows. A batched or
pipelined command — many reports per exchange, acknowledged once — is the way
out, and is easier to design in now than to retrofit.

## Porting to Windows

The firmware is unaffected; the board enumerates identically. The work is all
host-side:

- `host/picohid.py` uses `termios`, `tty` and `fcntl`, all POSIX-only. It needs
  a `pyserial` backend behind the existing `_Port` interface (open, write, read,
  flush_input, close — a small surface).
- Locking comes free: Windows opens COM ports exclusively by default.
- `_holder_of()` walks `/proc` to name the process holding the port. It should
  degrade to returning `None` rather than being ported.
- Port discovery reads `/sys/class/tty/*/device`. On Windows this becomes
  enumerating COM ports; the "drop the lowest interface number" rule for finding
  the data channel still applies but must be derived differently.
- `validate/` is permanently Linux-only. It is built on evdev, which has no
  Windows equivalent — there is no way to read back what the OS received.

## Known gaps

See [LIMITATIONS.md](LIMITATIONS.md) for the full list with evidence. The two
worth acting on are the missing System Control collection (trivial, ~15
descriptor bytes) and boot protocol support (awkward, and costs you the mouse).
