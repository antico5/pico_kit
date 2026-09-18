# Limitations

What a regular USB keyboard and mouse can do that this cannot. Each claim is
backed by a descriptor reading or a measurement, not by assumption.

## Confirmed gaps

### No boot protocol: nothing before the OS loads

```
3-1:1.5   class=03 subclass=00 protocol=00   HID
```

A real USB keyboard declares `subclass=01 protocol=01` (boot interface), and
sends reports with no report ID. This device declares `00/00` and uses report
IDs. Anything that speaks only the boot protocol sees nothing: BIOS/UEFI setup
screens, bootloaders such as GRUB, many KVM switches, IPMI/BMC consoles.

CircuitPython can provide this via `usb_hid.enable(devices, boot_device=1)`, but
the trade is severe and worth understanding before attempting it:

- `boot_device=1` is a boot **keyboard**, `2` is a boot **mouse**. Not both.
- While the host has boot protocol selected, `send_report()` on every non-boot
  device is **discarded** — a working boot keyboard means a dead mouse.
- The HID device must be USB interface 0, or CircuitPython enters safe mode.
  That conflicts with the CDC control channel's current position.

### Capped at 125 reports/second

`bInterval=8` in CircuitPython's descriptor template. Matches a regular office
mouse; a 1000 Hz gaming mouse is 8x finer. See [PERFORMANCE.md](PERFORMANCE.md).

### No smooth scrolling

The wheel moves in whole notches. `REL_WHEEL_HI_RES=120` does appear, but the
kernel synthesises it from a plain integer wheel report; there is no Resolution
Multiplier in the descriptor and no sub-notch granularity.

### No System Control collection

Usage page `0x01`, usages `0x81` Power / `0x82` Sleep / `0x83` Wake are absent,
so the power and sleep keys found on real keyboards cannot be sent. This is
about 15 descriptor bytes to add.

### Six-key rollover

Matches a regular keyboard exactly; only NKRO gaming keyboards exceed it. A 7th
simultaneous key is dropped cleanly rather than corrupting the report — tested.

### It does not exist on its own

The most practical difference. A real keyboard works standalone; this needs a
controller on the other end of the serial link. Host asleep, powered off, or
the driver not running, and the board does nothing.

## Unverified

### Wake from sleep

The configuration descriptor advertises the capability — `bmAttributes=0xa0`,
remote-wakeup bit set — but the kernel reports `power/wakeup: disabled` for the
device, and whether CircuitPython actually issues a wakeup signal was not
tested, as it requires suspending the host. Treat as unknown.

## Not gaps, despite appearances

- **Any keyboard key.** The descriptor declares Usage Minimum 0 / Maximum 255 on
  the keyboard page, so `send_raw()` can send every standard usage, including
  international and language keys. `keymap.py` simply does not name them all.
- **Media keys.** Volume, mute, play/pause, track skip all verified working.
- **Horizontal scroll / tilt.** Supported via AC Pan.
- **Five mouse buttons.** All verified, including back/forward.
- **Large or fast pointer movement.** *Better* than a classic mouse: 16-bit
  deltas express +-32767 in a single report where an 8-bit report needs a
  stutter of +-127 steps.
- **Reading host LED state.** Num/caps/scroll lock are readable via the HID
  output report.
- **Ctrl+Alt+Del, login screens, UAC.** This is a genuine HID device, so these
  work wherever a real keyboard does, once the OS is up.

## Unused USB interfaces

CircuitPython also exposes Audio and MIDI interfaces this project has no use
for:

```
3-1:1.6   class=01 subclass=01   Audio
3-1:1.7   class=01 subclass=03   MIDI
```

Harmless, but they consume USB endpoints and widen the surface slightly. They
can be switched off in `boot.py`.
