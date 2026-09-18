"""
suite.py -- the validation suite, and a headless runner for it.

Each check drives the board, then compares what the kernel received against
what should have arrived. Comparison rules differ by event type:

  EV_KEY  must match exactly. An unexpected keypress is a failure -- that is
          how you catch a descriptor that reports the wrong usage.
  EV_REL  expected non-zero deltas must be present exactly. Hi-res wheel
          companions (REL_WHEEL_HI_RES) are tolerated, since whether the
          kernel emits them depends on its version, not on our firmware.

Run it directly for a headless report, or drive it from the GTK app.
"""

import os
import sys
import time

from evdev import ecodes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))

import keymap  # noqa: E402

PASS, FAIL, ERROR, SKIP = "pass", "fail", "error", "skip"

# Wheel hi-res companions vary by kernel version, so they never count against us.
TOLERATED_REL = {
    getattr(ecodes, "REL_WHEEL_HI_RES", -1),
    getattr(ecodes, "REL_HWHEEL_HI_RES", -2),
}


def key(name):
    """HID key name -> evdev keycode. The two naming schemes line up by design."""
    return ecodes.ecodes.get("KEY_" + name.upper())


def tap_events(*names, modifiers=()):
    """Expected events for pressing modifiers + keys, then releasing all."""
    out = []
    for modifier in modifiers:
        out.append((ecodes.EV_KEY, key(modifier), 1))
    for name in names:
        out.append((ecodes.EV_KEY, key(name), 1))
    for name in names:
        out.append((ecodes.EV_KEY, key(name), 0))
    for modifier in reversed(modifiers):
        out.append((ecodes.EV_KEY, key(modifier), 0))
    return out


def button_events(code, held=True):
    out = [(ecodes.EV_KEY, code, 1)]
    if held:
        out.append((ecodes.EV_KEY, code, 0))
    return out


class Check:
    def __init__(self, category, name, action, expect=None, expect_error=None,
                 detail=""):
        self.category = category
        self.name = name
        self.action = action
        self.expect = expect or []
        self.expect_error = expect_error
        self.detail = detail


class Result:
    def __init__(self, check):
        self.check = check
        self.status = SKIP
        self.expected = []
        self.actual = []
        self.missing = []
        self.unexpected = []
        self.message = ""
        self.duration = 0.0


def _split(events):
    keys = [e for e in events if e[0] == ecodes.EV_KEY]
    rels = [e for e in events if e[0] == ecodes.EV_REL and e[2] != 0
            and e[1] not in TOLERATED_REL]
    return keys, rels


def _multiset_diff(expected, actual):
    remaining = list(actual)
    missing = []
    for item in expected:
        if item in remaining:
            remaining.remove(item)
        else:
            missing.append(item)
    return missing, remaining


def evaluate(check, events):
    """Compare captured events against the check's expectation."""
    result = Result(check)
    result.expected = list(check.expect)
    result.actual = list(events)

    expected_keys, expected_rels = _split(check.expect)
    actual_keys, actual_rels = _split(events)

    missing_keys, extra_keys = _multiset_diff(expected_keys, actual_keys)
    missing_rels, extra_rels = _multiset_diff(expected_rels, actual_rels)

    result.missing = missing_keys + missing_rels
    result.unexpected = extra_keys + extra_rels
    result.status = PASS if not result.missing and not result.unexpected else FAIL
    return result


def build(hid):
    """The whole suite, as a flat list of checks."""
    checks = []
    add = lambda *a, **k: checks.append(Check(*a, **k))

    # -- modifiers, one bit at a time -------------------------------------
    modifiers = ["leftctrl", "leftshift", "leftalt", "leftmeta",
                 "rightctrl", "rightshift", "rightalt", "rightmeta"]
    for name in modifiers:
        bit = keymap.MOD_BIT[keymap.HID_USAGE[name]]
        add("Modifiers", name,
            (lambda b=bit: (hid.keyboard_report(b, []), hid.keyboard_report(0, []))),
            tap_events(modifiers=[name])[:1] + [(ecodes.EV_KEY, key(name), 0)],
            detail="modifier bit 0x%02x alone" % bit)

    # -- letters, unshifted and shifted -----------------------------------
    for letter in "abcdefghijklmnopqrstuvwxyz":
        add("Letters", letter, (lambda c=letter: hid.tap(c)), tap_events(letter))
    for letter in "abcdefghijklmnopqrstuvwxyz":
        upper = letter.upper()
        add("Letters (shifted)", upper, (lambda c=upper: hid.tap(c)),
            tap_events(letter, modifiers=["leftshift"]))

    # -- digits and the symbols above them --------------------------------
    for digit in "1234567890":
        add("Digits", digit, (lambda c=digit: hid.tap(c)), tap_events(digit))
    for symbol, base in zip("!@#$%^&*()", "1234567890"):
        add("Digits (shifted)", symbol, (lambda c=symbol: hid.tap(c)),
            tap_events(base, modifiers=["leftshift"]))

    # -- punctuation -------------------------------------------------------
    punctuation = {
        "-": "minus", "=": "equal", "[": "leftbrace", "]": "rightbrace",
        "\\": "backslash", ";": "semicolon", "'": "apostrophe", "`": "grave",
        ",": "comma", ".": "dot", "/": "slash",
    }
    for char, name in punctuation.items():
        add("Punctuation", "%s (%s)" % (char, name),
            (lambda c=char: hid.tap(c)), tap_events(name))
    shifted = {"_": "minus", "+": "equal", "{": "leftbrace", "}": "rightbrace",
               "|": "backslash", ":": "semicolon", '"': "apostrophe",
               "~": "grave", "<": "comma", ">": "dot", "?": "slash"}
    for char, name in shifted.items():
        add("Punctuation (shifted)", char, (lambda c=char: hid.tap(c)),
            tap_events(name, modifiers=["leftshift"]))

    # -- control and whitespace -------------------------------------------
    for name in ["enter", "esc", "backspace", "tab", "space", "delete",
                 "insert", "sysrq", "pause"]:
        add("Control", name, (lambda n=name: hid.tap(n)), tap_events(name))

    # -- function keys -----------------------------------------------------
    for index in range(1, 13):
        name = "f%d" % index
        add("Function keys", name, (lambda n=name: hid.tap(n)), tap_events(name))

    # -- navigation --------------------------------------------------------
    for name in ["up", "down", "left", "right", "home", "end", "pageup", "pagedown"]:
        add("Navigation", name, (lambda n=name: hid.tap(n)), tap_events(name))

    # -- keypad ------------------------------------------------------------
    keypad = ["kp0", "kp1", "kp2", "kp3", "kp4", "kp5", "kp6", "kp7", "kp8", "kp9",
              "kpdot", "kpplus", "kpminus", "kpasterisk", "kpslash", "kpenter"]
    for name in keypad:
        add("Keypad", name, (lambda n=name: hid.tap(n)), tap_events(name))
    for name in ["numlock", "scrolllock"]:
        # Caps Lock is deliberately absent. It is very commonly remapped (to
        # Escape or Ctrl), so a failure here would report the user's keymap
        # rather than anything about the firmware.
        add("Locks", name, (lambda n=name: hid.tap(n)), tap_events(name))

    # -- combinations ------------------------------------------------------
    combos = [
        ("ctrl+c", ["leftctrl"], ["c"]),
        ("ctrl+shift+t", ["leftctrl", "leftshift"], ["t"]),
        ("ctrl+alt+t", ["leftctrl", "leftalt"], ["t"]),
        ("shift+f5", ["leftshift"], ["f5"]),
        ("all four modifiers + a",
         ["leftctrl", "leftshift", "leftalt", "leftmeta"], ["a"]),
    ]
    for label, mods, keys in combos:
        combo = "+".join(mods + keys)
        add("Combinations", label, (lambda c=combo: hid.tap(c)),
            tap_events(*keys, modifiers=mods))

    # -- n-key rollover ----------------------------------------------------
    six = ["a", "b", "c", "d", "e", "f"]
    def press_six():
        hid.keyboard_report(0, [keymap.HID_USAGE[n] for n in six])
        hid.keyboard_report(0, [])
    add("Rollover", "6 keys at once", press_six,
        [(ecodes.EV_KEY, key(n), 1) for n in six]
        + [(ecodes.EV_KEY, key(n), 0) for n in six],
        detail="the report array holds exactly 6 keycodes")

    def press_seven():
        hid.keyboard_report(0, [keymap.HID_USAGE[n] for n in six + ["g"]])
        hid.keyboard_report(0, [])
    add("Rollover", "7 keys truncates to 6", press_seven,
        [(ecodes.EV_KEY, key(n), 1) for n in six]
        + [(ecodes.EV_KEY, key(n), 0) for n in six],
        detail="the 7th key must be dropped, not corrupt the report")

    # -- mouse buttons -----------------------------------------------------
    buttons = [("left", ecodes.BTN_LEFT), ("right", ecodes.BTN_RIGHT),
               ("middle", ecodes.BTN_MIDDLE), ("back", ecodes.BTN_SIDE),
               ("forward", ecodes.BTN_EXTRA)]
    for name, code in buttons:
        add("Mouse buttons", name, (lambda n=name: hid.click(n)),
            button_events(code))

    def double_click():
        hid.click("left"); time.sleep(0.02); hid.click("left")
    add("Mouse buttons", "double click", double_click,
        button_events(ecodes.BTN_LEFT) * 2)

    def drag():
        hid.button_down("left")
        hid.mouse_report(0x01, 25, 25)
        hid.button_up("left")
    add("Mouse buttons", "drag (down, move, up)", drag,
        [(ecodes.EV_KEY, ecodes.BTN_LEFT, 1),
         (ecodes.EV_REL, ecodes.REL_X, 25), (ecodes.EV_REL, ecodes.REL_Y, 25),
         (ecodes.EV_KEY, ecodes.BTN_LEFT, 0)],
        detail="button state must survive intervening motion")

    # -- movement, including past the 8-bit boundary -----------------------
    for delta in [1, -1, 127, -127, 128, -128, 1000, -1000, 32767, -32767]:
        add("Mouse X", "dx = %d" % delta,
            (lambda d=delta: hid.mouse_report(0, d, 0)),
            [(ecodes.EV_REL, ecodes.REL_X, delta)],
            detail="values past +-127 prove the 16-bit descriptor works")
    for delta in [1, -1, 127, -127, 128, -128, 1000, -1000, 32767, -32767]:
        add("Mouse Y", "dy = %d" % delta,
            (lambda d=delta: hid.mouse_report(0, 0, d)),
            [(ecodes.EV_REL, ecodes.REL_Y, delta)])
    for dx, dy in [(10, 10), (-10, 10), (10, -10), (-10, -10), (500, -500)]:
        add("Mouse diagonal", "(%d, %d)" % (dx, dy),
            (lambda x=dx, y=dy: hid.mouse_report(0, x, y)),
            [(ecodes.EV_REL, ecodes.REL_X, dx), (ecodes.EV_REL, ecodes.REL_Y, dy)])

    # -- wheel and pan -----------------------------------------------------
    for amount in [1, -1, 5, -5, 127, -127]:
        add("Wheel", "wheel = %d" % amount,
            (lambda a=amount: hid.scroll(a)),
            [(ecodes.EV_REL, ecodes.REL_WHEEL, amount)])
    for amount in [1, -1, 5, -5]:
        add("Pan", "pan = %d" % amount,
            (lambda a=amount: hid.scroll(0, a)),
            [(ecodes.EV_REL, ecodes.REL_HWHEEL, amount)])

    # -- consumer / media --------------------------------------------------
    media = [("volume up", 0x00E9, "volumeup"), ("volume down", 0x00EA, "volumedown"),
             ("mute", 0x00E2, "mute"), ("play/pause", 0x00CD, "playpause"),
             ("next track", 0x00B5, "nextsong"), ("previous track", 0x00B6, "previoussong")]
    for label, usage, keyname in media:
        def send_media(u=usage):
            hid.consumer_report(u)
            hid.consumer_report(0)
        add("Consumer", label, send_media,
            [(ecodes.EV_KEY, key(keyname), 1), (ecodes.EV_KEY, key(keyname), 0)],
            detail="consumer page usage 0x%04X" % usage)

    # -- protocol level, no input events expected --------------------------
    add("Protocol", "release_all is silent when idle", hid.release_all, [],
        detail="must not emit phantom key-up events")
    add("Protocol", "wrong report length rejected",
        (lambda: hid.send_raw(1, b"\x00\x00")), [],
        expect_error="length",
        detail="firmware must reject a malformed report, not forward it")
    add("Protocol", "unknown report id rejected",
        (lambda: hid.send_raw(9, b"\x00")), [],
        expect_error="unknown",
        detail="firmware must reject an unknown report id")
    add("Protocol", "ping round trip", hid.ping, [])
    add("Protocol", "info round trip", hid.info, [])
    add("Protocol", "LED state readable", hid.leds, [])

    return checks


def run(hid, capture, checks, on_result=None, settle=0.06, should_stop=None):
    """Execute checks in order, yielding a Result for each."""
    results = []
    for check in checks:
        result = Result(check)
        if should_stop is not None and should_stop():
            result.status = SKIP
            result.message = "stopped"
            results.append(result)
            if on_result:
                on_result(result)
            continue

        capture.drain()
        started = time.monotonic()
        try:
            check.action()
        except Exception as error:                     # noqa: BLE001
            if check.expect_error and check.expect_error in str(error).lower():
                result.status = PASS
                result.message = "rejected as expected: %s" % error
            else:
                result.status = ERROR
                result.message = "%s: %s" % (type(error).__name__, error)
            result.duration = time.monotonic() - started
            results.append(result)
            if on_result:
                on_result(result)
            continue

        if check.expect_error:
            result.status = FAIL
            result.message = "expected rejection (%s) but the call succeeded" % check.expect_error
            result.duration = time.monotonic() - started
            results.append(result)
            if on_result:
                on_result(result)
            continue

        events = [(e.type, e.code, e.value) for _role, e in capture.collect(settle=settle)]
        result = evaluate(check, events)
        result.duration = time.monotonic() - started
        results.append(result)
        if on_result:
            on_result(result)
    return results


def _headless(argv):
    import argparse

    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "host"))
    import capture as capture_module
    from picohid import PicoHID, PicoHIDError

    parser = argparse.ArgumentParser(description="Validate the Pico HID bridge.")
    parser.add_argument("-p", "--port", help="serial port (default: autodetect)")
    parser.add_argument("--category", action="append",
                        help="only run these categories (repeatable)")
    parser.add_argument("--live", action="store_true",
                        help="do NOT grab the devices -- events reach the desktop")
    parser.add_argument("--json", metavar="PATH", help="write a JSON report")
    args = parser.parse_args(argv)

    try:
        hid = PicoHID(port=args.port)
    except PicoHIDError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2

    checks = build(hid)
    if args.category:
        wanted = {c.lower() for c in args.category}
        checks = [c for c in checks if c.category.lower() in wanted]

    try:
        capture = capture_module.Capture(exclusive=not args.live)
    except capture_module.CaptureError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2

    tally = {PASS: 0, FAIL: 0, ERROR: 0, SKIP: 0}
    failures = []
    category = None

    def report(result):
        nonlocal category
        if result.check.category != category:
            category = result.check.category
            print("\n%s" % category)
        tally[result.status] += 1
        mark = {PASS: "  ok  ", FAIL: " FAIL ", ERROR: "ERROR ", SKIP: " skip "}[result.status]
        print("  %s %-28s %s" % (mark, result.check.name, result.message))
        if result.status in (FAIL, ERROR):
            failures.append(result)

    with capture, hid:
        if not args.live:
            print("Devices grabbed: test input cannot reach the desktop.")
        started = time.monotonic()
        results = run(hid, capture, checks, on_result=report)
        elapsed = time.monotonic() - started

    print("\n%d checks in %.1fs -- %d passed, %d failed, %d errors, %d skipped"
          % (len(results), elapsed, tally[PASS], tally[FAIL], tally[ERROR], tally[SKIP]))

    for result in failures:
        print("\n%s / %s" % (result.check.category, result.check.name))
        if result.check.detail:
            print("  intent:     %s" % result.check.detail)
        if result.missing:
            print("  missing:    %s" % _pretty(result.missing))
        if result.unexpected:
            print("  unexpected: %s" % _pretty(result.unexpected))
        if result.message:
            print("  note:       %s" % result.message)

    if args.json:
        import json
        with open(args.json, "w") as handle:
            json.dump([{
                "category": r.check.category, "name": r.check.name,
                "status": r.status, "detail": r.check.detail,
                "missing": _pretty(r.missing), "unexpected": _pretty(r.unexpected),
                "message": r.message, "duration_ms": round(r.duration * 1000, 1),
            } for r in results], handle, indent=2)
        print("\nwrote %s" % args.json)

    return 1 if tally[FAIL] or tally[ERROR] else 0


def _pretty(events):
    out = []
    for kind, code, value in events:
        table = ecodes.bytype.get(kind, {})
        name = table.get(code, code)
        if isinstance(name, (list, tuple)):
            name = name[0]
        out.append("%s=%s" % (name, value))
    return ", ".join(out)


if __name__ == "__main__":
    sys.exit(_headless(sys.argv[1:]))
