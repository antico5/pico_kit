# Performance

All figures measured on an RP2040 running CircuitPython 10.3.1, full-speed USB,
against a Linux 5.15 host.

## The one number that matters

```
3-1:1.5/ep_86   type=Interrupt   interval=8ms   bInterval=08
```

**The board delivers at most 125 HID reports per second.** Everything else is
downstream of this.

It is not a hardware limit. The RP2040 is a full-speed device (12 Mbps), where
`bInterval` counts whole milliseconds and may legally be `1`. The `8` is a
hardcoded constant in CircuitPython's HID descriptor template:

```c
0x08,        // 24 bInterval 8 (unit depends on device speed)
0x08,        // 31 bInterval 8 (unit depends on device speed)
```

It is not exposed through `usb_hid`, so changing it means building CircuitPython
from source.

## Where the time goes

| Operation | Median | Rate | Limited by |
|---|---|---|---|
| `ping` (no HID report) | 1.93 ms | 520/s | serial + firmware |
| `mouse_report` | 8 ms | 125/s | the USB endpoint |
| `keyboard_report` | 8 ms | 125/s | the USB endpoint |

Ping is ~4x faster because it never touches the endpoint. Anything that emits a
report clamps to exactly 125/s, because `send_report()` blocks until the
endpoint drains. **The control path has roughly 4x headroom that USB refuses to
use.**

## Typing throughput

Naively, each character costs two reports — press, then release — so 125
reports/s yields ~62 chars/s. A rolling transition does better: `[a]` -> `[b]`
is a single report that releases `a` and presses `b`, costing n+1 reports for n
characters instead of 2n. Repeated characters still need an intervening release.

```
two reports per char:   0.68s for 43 chars ->  63 chars/s
rolling transitions:    0.35s for 43 chars -> 122 chars/s
```

Verified against captured events: all 43 keydowns arrived, in order.

## IPC is free at this scale

Measured round trips on the same machine, versus the hardware budget:

| | Median | p99 | Share of one report |
|---|---|---|---|
| Unix domain socket | 7.4 us | 22.8 us | 0.09% |
| TCP loopback | 20.2 us | 40.8 us | 0.25% |
| **USB endpoint** | **8000 us** | — | 100% |

**Do not choose an architecture here on latency grounds.** A server hop costs a
quarter of one percent of what you already pay per report. Choose on whether
several processes need to share the board — see [ARCHITECTURE.md](ARCHITECTURE.md).

## If you raise the polling rate

Building CircuitPython with `bInterval = 1` would give 1000 Hz, but note the
second-order effect: at 1 ms per report the **serial round trip (1.93 ms)
becomes the new bottleneck**. Exploiting 1000 Hz therefore also requires
pipelined or fire-and-forget commands rather than the current strict
request/response, which is worth knowing before designing a protocol extension.

## Suite runtime

The 210-check validation suite completes in **17.2 s**, dominated by the 60 ms
settle window each check waits to be sure no further events arrive.
