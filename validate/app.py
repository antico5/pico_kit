#!/usr/bin/env python3
"""
app.py -- GTK4 front end for the validation suite.

Must run under the system interpreter, which is the one with gi and evdev:

    /usr/bin/python3 validate/app.py

Isolated mode (the default) grabs the board's input devices, so test keystrokes
and mouse motion never reach the desktop. Your real keyboard and mouse keep
working normally while a run is in progress.
"""

import json
import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "host"))

import capture as capture_module  # noqa: E402
import suite  # noqa: E402
from picohid import PicoHID, PicoHIDError  # noqa: E402

CSS = b"""
.status-pass  { color: #2ea043; font-weight: bold; }
.status-fail  { color: #f85149; font-weight: bold; }
.status-error { color: #d29922; font-weight: bold; }
.status-skip  { color: #8b949e; }
.category     { font-weight: bold; opacity: 0.75; }
.log          { font-family: monospace; font-size: 11px; }
.detail       { opacity: 0.7; font-size: 11px; }
.banner-bad   { background: #4a1d1d; padding: 8px; }
.banner-good  { background: #14301c; padding: 8px; }
"""

MARK = {suite.PASS: "PASS", suite.FAIL: "FAIL", suite.ERROR: "ERR", suite.SKIP: "--"}
STYLE = {suite.PASS: "status-pass", suite.FAIL: "status-fail",
         suite.ERROR: "status-error", suite.SKIP: "status-skip"}


class Row(Gtk.ListBoxRow):
    def __init__(self, check):
        super().__init__()
        self.check = check
        self.result = None

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(3); box.set_margin_bottom(3)
        box.set_margin_start(8); box.set_margin_end(8)

        self.status = Gtk.Label(label="--", xalign=0.0)
        self.status.set_size_request(46, -1)
        self.status.add_css_class("status-skip")
        box.append(self.status)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.name = Gtk.Label(label=check.name, xalign=0.0)
        text.append(self.name)
        self.note = Gtk.Label(label=check.detail, xalign=0.0)
        self.note.add_css_class("detail")
        self.note.set_wrap(True)
        self.note.set_visible(bool(check.detail))
        text.append(self.note)
        box.append(text)

        self.set_child(box)

    def apply(self, result):
        self.result = result
        self.status.set_label(MARK[result.status])
        for name in STYLE.values():
            self.status.remove_css_class(name)
        self.status.add_css_class(STYLE[result.status])

        parts = []
        if result.missing:
            parts.append("missing: " + suite._pretty(result.missing))
        if result.unexpected:
            parts.append("unexpected: " + suite._pretty(result.unexpected))
        if result.message:
            parts.append(result.message)
        note = "  |  ".join(parts) or self.check.detail
        self.note.set_label(note)
        self.note.set_visible(bool(note))


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Pico HID Validator")
        self.set_default_size(1100, 780)

        self.hid = None
        self.capture = None
        self.rows = []
        self.results = []
        self.worker = None
        self.stop_flag = threading.Event()

        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        self.run_button = Gtk.Button(label="Run all")
        self.run_button.add_css_class("suggested-action")
        self.run_button.connect("clicked", self.on_run)
        header.pack_start(self.run_button)

        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", self.on_stop)
        header.pack_start(self.stop_button)

        self.isolate = Gtk.ToggleButton(label="Isolated")
        self.isolate.set_active(True)
        self.isolate.set_tooltip_text(
            "Grab the board's input devices so test events never reach the "
            "desktop. Turn off to let them through (they will type into "
            "whatever window has focus).")
        header.pack_start(self.isolate)

        self.failures_only = Gtk.ToggleButton(label="Failures only")
        self.failures_only.connect("toggled", lambda *_: self.refilter())
        header.pack_end(self.failures_only)

        self.export = Gtk.Button(label="Export")
        self.export.connect("clicked", self.on_export)
        header.pack_end(self.export)

        self.summary = Gtk.Label(label="not connected")
        header.pack_end(self.summary)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)

        self.banner = Gtk.Label(xalign=0.0)
        self.banner.set_wrap(True)
        self.banner.set_visible(False)
        outer.append(self.banner)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_text("idle")
        outer.append(self.progress)

        panes = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        panes.set_vexpand(True)
        outer.append(panes)

        self.listbox = Gtk.ListBox()
        self.listbox.set_filter_func(self.filter_row)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.listbox)
        scroller.set_vexpand(True)
        panes.set_start_child(scroller)

        self.log = Gtk.TextView(editable=False, monospace=True)
        self.log.add_css_class("log")
        self.log.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_child(self.log)
        log_scroller.set_size_request(-1, 220)
        panes.set_end_child(log_scroller)
        panes.set_position(520)

        self.connect_board()

    # -- setup -------------------------------------------------------------

    def connect_board(self):
        try:
            self.hid = PicoHID()
        except PicoHIDError as error:
            return self.show_banner("Cannot reach the board: %s" % error, bad=True)

        try:
            nodes = capture_module.find_board_nodes()
            probe = capture_module.Capture(exclusive=False)
            probe.stop()
        except capture_module.CaptureError as error:
            return self.show_banner(str(error), bad=True)

        info = self.hid.info()
        self.show_banner(
            "Connected: firmware %s on %s.  Input nodes: %s"
            % (info["version"], self.hid.port.path,
               ", ".join("%s=%s" % kv for kv in sorted(nodes.items()))),
            bad=False)

        for check in suite.build(self.hid):
            if not self.rows or self.rows[-1].check.category != check.category:
                label = Gtk.Label(label=check.category, xalign=0.0)
                label.add_css_class("category")
                label.set_margin_top(12)
                label.set_margin_start(8)
                heading = Gtk.ListBoxRow()
                heading.set_activatable(False)
                heading.set_child(label)
                heading.check = None
                self.listbox.append(heading)
            row = Row(check)
            self.rows.append(row)
            self.listbox.append(row)

        self.summary.set_label("%d checks ready" % len(self.rows))

    def show_banner(self, text, bad):
        self.banner.set_label(text)
        self.banner.remove_css_class("banner-bad")
        self.banner.remove_css_class("banner-good")
        self.banner.add_css_class("banner-bad" if bad else "banner-good")
        self.banner.set_visible(True)

    def write_log(self, text):
        buffer = self.log.get_buffer()
        buffer.insert(buffer.get_end_iter(), text + "\n")
        self.log.scroll_to_iter(buffer.get_end_iter(), 0.0, False, 0.0, 0.0)

    # -- filtering ---------------------------------------------------------

    def filter_row(self, row):
        if getattr(row, "check", None) is None:
            return True
        if not self.failures_only.get_active():
            return True
        return row.result is not None and row.result.status in (suite.FAIL, suite.ERROR)

    def refilter(self):
        self.listbox.invalidate_filter()

    # -- running -----------------------------------------------------------

    def on_run(self, _button):
        if self.hid is None or self.worker is not None:
            return
        self.stop_flag.clear()
        self.results = []
        for row in self.rows:
            row.result = None
            row.status.set_label("--")
        self.run_button.set_sensitive(False)
        self.stop_button.set_sensitive(True)
        self.export.set_sensitive(False)
        self.write_log("=== run started %s ==="
                       % time.strftime("%H:%M:%S"))

        self.worker = threading.Thread(target=self.run_suite, daemon=True)
        self.worker.start()

    def on_stop(self, _button):
        self.stop_flag.set()
        self.write_log("stop requested")

    def run_suite(self):
        exclusive = self.isolate.get_active()
        try:
            self.capture = capture_module.Capture(exclusive=exclusive)
            self.capture.start()
        except capture_module.CaptureError as error:
            GLib.idle_add(self.show_banner, str(error), True)
            GLib.idle_add(self.finish)
            return

        if exclusive:
            GLib.idle_add(self.write_log,
                          "devices grabbed -- test input is isolated from the desktop")
        else:
            GLib.idle_add(self.write_log,
                          "LIVE mode -- events are reaching the desktop")

        checks = [row.check for row in self.rows]
        index = {id(check): row for check, row in zip(checks, self.rows)}
        done = [0]

        def on_result(result):
            done[0] += 1
            GLib.idle_add(self.apply_result, index[id(result.check)], result, done[0],
                          len(checks))

        try:
            self.results = suite.run(self.hid, self.capture, checks,
                                     on_result=on_result,
                                     should_stop=self.stop_flag.is_set)
        finally:
            self.capture.stop()
            self.capture = None
            GLib.idle_add(self.finish)

    def apply_result(self, row, result, done, total):
        row.apply(result)
        self.progress.set_fraction(done / total)
        self.progress.set_text("%d / %d" % (done, total))

        line = "%-5s %-22s %-28s" % (MARK[result.status], result.check.category,
                                     result.check.name)
        extras = []
        if result.missing:
            extras.append("missing " + suite._pretty(result.missing))
        if result.unexpected:
            extras.append("unexpected " + suite._pretty(result.unexpected))
        if result.message:
            extras.append(result.message)
        if extras:
            line += "  " + " | ".join(extras)
        self.write_log(line)

        tally = {}
        for candidate in self.rows:
            if candidate.result is not None:
                tally[candidate.result.status] = tally.get(candidate.result.status, 0) + 1
        self.summary.set_label(
            "%d pass  %d fail  %d err"
            % (tally.get(suite.PASS, 0), tally.get(suite.FAIL, 0),
               tally.get(suite.ERROR, 0)))
        return False

    def finish(self):
        self.worker = None
        self.run_button.set_sensitive(True)
        self.stop_button.set_sensitive(False)
        self.export.set_sensitive(True)
        self.progress.set_text("done")
        self.write_log("=== run finished ===")
        self.refilter()
        return False

    # -- export ------------------------------------------------------------

    def on_export(self, _button):
        if not self.results:
            return
        path = os.path.join(os.path.expanduser("~"),
                            "pico-kit-validation-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        with open(path, "w") as handle:
            json.dump([{
                "category": r.check.category, "name": r.check.name,
                "status": r.status, "detail": r.check.detail,
                "missing": suite._pretty(r.missing),
                "unexpected": suite._pretty(r.unexpected),
                "message": r.message,
                "duration_ms": round(r.duration * 1000, 1),
            } for r in self.results], handle, indent=2)
        self.write_log("exported %s" % path)
        self.show_banner("Report written to %s" % path, bad=False)


class Application(Gtk.Application):
    def __init__(self, autorun=False):
        super().__init__(application_id="dev.picohid.Validator")
        self.autorun = autorun

    def do_activate(self):
        window = Window(self)

        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS)
        except TypeError:                       # newer bindings want str
            provider.load_from_data(CSS.decode())
        Gtk.StyleContext.add_provider_for_display(
            window.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        window.present()
        if self.autorun and window.hid is not None:
            GLib.timeout_add(400, lambda: (window.on_run(None), False)[1])


if __name__ == "__main__":
    argv = [a for a in sys.argv if a != "--autorun"]
    sys.exit(Application(autorun="--autorun" in sys.argv).run(argv))
