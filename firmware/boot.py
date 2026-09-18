"""
boot.py -- runs once at power-up, before code.py.

Declares the USB personality of the board: a composite device with a HID
keyboard, a HID mouse, a HID consumer control, and two serial channels.

  CDC console  the REPL and tracebacks       (first /dev/ttyACM*)
  CDC data     the packet channel code.py    (second /dev/ttyACM*)
               listens on

Keeping control traffic on the data channel means it never collides with
tracebacks or REPL echo. The report layouts live in hid_layout.py.
"""

import usb_cdc
import usb_hid

import hid_layout

DEVICES = tuple(
    usb_hid.Device(
        report_descriptor=hid_layout.DESCRIPTORS[report_id],
        usage_page=usage_page,
        usage=usage,
        report_ids=(report_id,),
        in_report_lengths=(in_length,),
        out_report_lengths=(out_length,),
    )
    for report_id, (usage_page, usage, in_length, out_length)
    in sorted(hid_layout.LAYOUT.items())
)

usb_hid.enable(DEVICES)
usb_cdc.enable(console=True, data=True)
