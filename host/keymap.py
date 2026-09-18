"""
keymap.py - key naming and US-layout character mapping.

Standard US-layout HID usage tables, kept self-contained on purpose.

HID usages (Keyboard/Keypad page 0x07) are addressed by name. Names are the
evdev KEY_* names lower-cased without the prefix ("a", "leftctrl", "f5"),
plus friendly aliases ("ctrl", "esc", "win", ...).
"""

HID_USAGE = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    HID_USAGE[_c] = 4 + _i
for _i, _c in enumerate("1234567890"):
    HID_USAGE[_c] = 30 + _i
for _i in range(12):
    HID_USAGE[f"f{_i + 1}"] = 58 + _i
for _i in range(12):
    HID_USAGE[f"f{_i + 13}"] = 104 + _i
for _i in range(9):
    HID_USAGE[f"kp{_i + 1}"] = 89 + _i
HID_USAGE.update({
    "enter": 40, "esc": 41, "backspace": 42, "tab": 43, "space": 44,
    "minus": 45, "equal": 46, "leftbrace": 47, "rightbrace": 48, "backslash": 49,
    "semicolon": 51, "apostrophe": 52, "grave": 53, "comma": 54, "dot": 55, "slash": 56,
    "capslock": 57, "sysrq": 70, "scrolllock": 71, "pause": 72, "insert": 73, "home": 74,
    "pageup": 75, "delete": 76, "end": 77, "pagedown": 78,
    "right": 79, "left": 80, "down": 81, "up": 82,
    "numlock": 83, "kpslash": 84, "kpasterisk": 85, "kpminus": 86, "kpplus": 87,
    "kpenter": 88, "kp0": 98, "kpdot": 99, "102nd": 100, "compose": 101, "power": 102,
    "kpequal": 103,
    "leftctrl": 224, "leftshift": 225, "leftalt": 226, "leftmeta": 227,
    "rightctrl": 228, "rightshift": 229, "rightalt": 230, "rightmeta": 231,
})

ALIASES = {
    "ctrl": "leftctrl", "control": "leftctrl", "lctrl": "leftctrl", "rctrl": "rightctrl",
    "shift": "leftshift", "lshift": "leftshift", "rshift": "rightshift",
    "alt": "leftalt", "lalt": "leftalt", "ralt": "rightalt", "altgr": "rightalt",
    "win": "leftmeta", "meta": "leftmeta", "super": "leftmeta", "cmd": "leftmeta",
    "lwin": "leftmeta", "rwin": "rightmeta",
    "escape": "esc", "return": "enter", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "pgdown": "pagedown", "caps": "capslock",
    "printscreen": "sysrq", "prtsc": "sysrq", "print": "sysrq",
    "menu": "compose", "app": "compose", "apps": "compose", "application": "compose",
    "bksp": "backspace", "spacebar": "space",
    "num0": "kp0", "num1": "kp1", "num2": "kp2", "num3": "kp3", "num4": "kp4",
    "num5": "kp5", "num6": "kp6", "num7": "kp7", "num8": "kp8", "num9": "kp9",
    "numpad0": "kp0", "numpad1": "kp1", "numpad2": "kp2", "numpad3": "kp3", "numpad4": "kp4",
    "numpad5": "kp5", "numpad6": "kp6", "numpad7": "kp7", "numpad8": "kp8", "numpad9": "kp9",
    "numenter": "kpenter", "numdot": "kpdot", "numplus": "kpplus", "numminus": "kpminus",
    "numslash": "kpslash", "numstar": "kpasterisk",
}

class KeyNameError(ValueError):
    pass


MOD_BIT = {224: 0x01, 225: 0x02, 226: 0x04, 227: 0x08, 228: 0x10, 229: 0x20, 230: 0x40, 231: 0x80}

# US-English layout: character -> (usage, needs_shift)
_UNSHIFTED = {
    " ": "space", "-": "minus", "=": "equal", "[": "leftbrace", "]": "rightbrace",
    "\\": "backslash", ";": "semicolon", "'": "apostrophe", "`": "grave", ",": "comma",
    ".": "dot", "/": "slash", "\n": "enter", "\r": "enter", "\t": "tab", "\x1b": "esc",
    "\x08": "backspace",
}
_SHIFTED = {
    "_": "minus", "+": "equal", "{": "leftbrace", "}": "rightbrace", "|": "backslash",
    ":": "semicolon", '"': "apostrophe", "~": "grave", "<": "comma", ">": "dot", "?": "slash",
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0",
}
CHAR_MAP = {}
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    CHAR_MAP[_c] = (HID_USAGE[_c], False)
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    CHAR_MAP[_c] = (HID_USAGE[_c.lower()], True)
for _c, _n in _UNSHIFTED.items():
    CHAR_MAP[_c] = (HID_USAGE[_n], False)
for _c, _n in _SHIFTED.items():
    CHAR_MAP[_c] = (HID_USAGE[_n], True)

MOUSE_BUTTON = {"left": 0x01, "right": 0x02, "middle": 0x04, "side": 0x08, "extra": 0x10,
                "back": 0x08, "forward": 0x10, "l": 0x01, "r": 0x02, "m": 0x04,
                "1": 0x01, "2": 0x02, "3": 0x04, "4": 0x08, "5": 0x10}


def key_usage(name):
    """Resolve a key name (or single character) to (usage, needs_shift)."""
    n = name.strip()
    # Single characters go through CHAR_MAP first: it is case-sensitive, so "A"
    # resolves to a+shift. Lower-casing first would silently produce "a".
    if len(n) == 1 and n in CHAR_MAP:
        return CHAR_MAP[n]
    low = n.lower()
    low = ALIASES.get(low, low)
    if low in HID_USAGE:
        return HID_USAGE[low], False
    raise KeyNameError(f"unknown key '{name}'")


def parse_combo(combo):
    """'ctrl+shift+t' -> (modifier_mask, [usages]). A trailing '+' means the '+' key."""
    parts = [p for p in combo.split("+") if p != ""]
    if combo.endswith("+") or combo == "+":
        parts.append("+")
    if not parts:
        raise KeyNameError("empty key combination")
    mods, keys = 0, []
    for p in parts:
        usage, shift = key_usage(p)
        if shift:
            mods |= MOD_BIT[225]
        if usage in MOD_BIT:
            mods |= MOD_BIT[usage]
        elif usage not in keys:
            keys.append(usage)
    if len(keys) > 6:
        raise KeyNameError("at most 6 non-modifier keys at once")
    return mods, keys
