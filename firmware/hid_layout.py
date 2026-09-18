"""
hid_layout.py -- the one place the USB HID report layouts are defined.

Imported by boot.py (to build the usb_hid.Device objects) and by code.py (to
validate incoming packet lengths and route them to the right device), so the
two can never drift apart.

  keyboard  report ID 1, 8 bytes  [mods, reserved, k1..k6]
  mouse     report ID 2, 7 bytes  [buttons, dx:i16le, dy:i16le, wheel:i8, pan:i8]
  consumer  report ID 3, 2 bytes  [usage:u16le]
"""

KEYBOARD_DESCRIPTOR = bytes((
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,        #   Usage Minimum (LeftControl)
    0x29, 0xE7,        #   Usage Maximum (RightGUI)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs)     -- byte 0: modifier bits
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x03,        #   Input (Const,Var,Abs)    -- byte 1: reserved
    0x95, 0x05,        #   Report Count (5)
    0x75, 0x01,        #   Report Size (1)
    0x05, 0x08,        #   Usage Page (LEDs)
    0x19, 0x01,        #   Usage Minimum (NumLock)
    0x29, 0x05,        #   Usage Maximum (Kana)
    0x91, 0x02,        #   Output (Data,Var,Abs)    -- host -> device LED bits
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x03,        #   Report Size (3)
    0x91, 0x03,        #   Output (Const,Var,Abs)   -- LED padding
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,        #   Usage Minimum (0)
    0x2A, 0xFF, 0x00,  #   Usage Maximum (255)
    0x81, 0x00,        #   Input (Data,Ary,Abs)     -- bytes 2..7: keycodes
    0xC0,              # End Collection
))

MOUSE_DESCRIPTOR = bytes((
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (Button 1)
    0x29, 0x05,        #     Usage Maximum (Button 5)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x05,        #     Report Count (5)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- byte 0 bits 0..4: buttons
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x03,        #     Report Size (3)
    0x81, 0x03,        #     Input (Const,Var,Abs)  -- byte 0 bits 5..7: padding
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x16, 0x01, 0x80,  #     Logical Minimum (-32767)
    0x26, 0xFF, 0x7F,  #     Logical Maximum (32767)
    0x75, 0x10,        #     Report Size (16)
    0x95, 0x02,        #     Report Count (2)
    0x81, 0x06,        #     Input (Data,Var,Rel)   -- bytes 1..4: dx, dy
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x06,        #     Input (Data,Var,Rel)   -- byte 5: vertical wheel
    0x05, 0x0C,        #     Usage Page (Consumer)
    0x0A, 0x38, 0x02,  #     Usage (AC Pan)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x06,        #     Input (Data,Var,Rel)   -- byte 6: horizontal pan
    0xC0,              #   End Collection
    0xC0,              # End Collection
))

CONSUMER_DESCRIPTOR = bytes((
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x03,        #   Report ID (3)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x03,  #   Logical Maximum (1023)
    0x19, 0x00,        #   Usage Minimum (0)
    0x2A, 0xFF, 0x03,  #   Usage Maximum (1023)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x00,        #   Input (Data,Ary,Abs)     -- bytes 0..1: usage code
    0xC0,              # End Collection
))

REPORT_KEYBOARD = 1
REPORT_MOUSE = 2
REPORT_CONSUMER = 3

# report_id -> (usage_page, usage, in_report_length, out_report_length)
LAYOUT = {
    REPORT_KEYBOARD: (0x01, 0x06, 8, 1),
    REPORT_MOUSE: (0x01, 0x02, 7, 0),
    REPORT_CONSUMER: (0x0C, 0x01, 2, 0),
}

DESCRIPTORS = {
    REPORT_KEYBOARD: KEYBOARD_DESCRIPTOR,
    REPORT_MOUSE: MOUSE_DESCRIPTOR,
    REPORT_CONSUMER: CONSUMER_DESCRIPTOR,
}
