#!/usr/bin/env python3
"""Tray indicator showing live Claude plan usage in the GNOME panel.

Reads cached state written by monitor.py (no network of its own),
refreshes the panel label every 60 s, and provides a right-click menu to
trigger an immediate refresh or open the raw response.
"""
from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import gi

# Prefer Ayatana (current Ubuntu); fall back to legacy AppIndicator3.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3  # type: ignore

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

REPO_DIR = Path(__file__).resolve().parent
ICON_DIR = REPO_DIR / "icons"
ICONS = {
    "green": ICON_DIR / "claudometer-green.svg",
    "yellow": ICON_DIR / "claudometer-yellow.svg",
    "red": ICON_DIR / "claudometer-red.svg",
    "grey": ICON_DIR / "claudometer-grey.svg",
}

def _xdg(env: str, fallback: str) -> Path:
    return Path(os.environ.get(env) or os.path.expanduser(fallback))


CONFIG_PATH = _xdg("XDG_CONFIG_HOME", "~/.config") / "claudometer" / "config.ini"
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / "claudometer"
STATE_PATH = STATE_DIR / "state.json"
LAST_RESPONSE_PATH = STATE_DIR / "last_response.json"
MONITOR_SCRIPT = REPO_DIR / "monitor.py"

REFRESH_SEC = 60
IDLE_RESET = "idle"


def load_windows() -> list[str]:
    if not CONFIG_PATH.exists():
        return ["five_hour", "weekly"]
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cp.read(CONFIG_PATH)
    raw = cp["claudometer"].get("windows", "five_hour, weekly")
    return [x.strip() for x in raw.split(",") if x.strip()]


def pretty_window(canon: str) -> str:
    return {
        "five_hour": "5h",
        "weekly": "W",
        "weekly_opus": "WO",
    }.get(canon, canon)


def parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_eta(ts: str) -> str:
    if ts == IDLE_RESET:
        return "idle"
    dt = parse_iso(ts)
    if dt is None:
        return "?"
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "now"
    d, secs = divmod(int(secs), 86400)
    h, secs = divmod(secs, 3600)
    m = secs // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    return f"{m}m"


def fmt_local(ts: str) -> str:
    if ts == IDLE_RESET:
        return "no active window"
    dt = parse_iso(ts)
    return dt.astimezone().strftime("%a %H:%M") if dt else "?"


def latest_per_window(state: dict) -> dict[str, dict]:
    """Return the most-recent reading per window from history."""
    latest: dict[str, dict] = {}
    for h in state.get("history", []):
        prev = latest.get(h["window"])
        if prev is None or h["ts"] > prev["ts"]:
            latest[h["window"]] = h
    return latest


def color_for_remaining(remaining: float) -> str:
    if remaining < 20:
        return "red"
    if remaining < 50:
        return "yellow"
    return "green"


class Tray:
    def __init__(self) -> None:
        self.windows = load_windows()
        self.indicator = AppIndicator3.Indicator.new(
            "claudometer",
            str(ICONS["grey"]),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label("…", "5h 100% (0h0m) · W 100% (0d0h)")
        self.indicator.set_title("Claudometer")
        self._build_menu()
        self._refresh()
        GLib.timeout_add_seconds(REFRESH_SEC, self._tick)

    def _build_menu(self) -> None:
        self.menu = Gtk.Menu()

        # One detail item per configured window. Filled in by _refresh.
        self.detail_items: list[Gtk.MenuItem] = []
        for _ in self.windows:
            mi = Gtk.MenuItem(label="…")
            mi.set_sensitive(False)
            self.menu.append(mi)
            self.detail_items.append(mi)

        self.updated_item = Gtk.MenuItem(label="Last updated: never")
        self.updated_item.set_sensitive(False)
        self.menu.append(self.updated_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        refresh = Gtk.MenuItem(label="Refresh now")
        refresh.connect("activate", self._on_refresh)
        self.menu.append(refresh)

        view = Gtk.MenuItem(label="View raw response")
        view.connect("activate", self._on_view_raw)
        self.menu.append(view)

        logs = Gtk.MenuItem(label="View monitor logs")
        logs.connect("activate", self._on_view_logs)
        self.menu.append(logs)

        self.menu.append(Gtk.SeparatorMenuItem())

        quit_ = Gtk.MenuItem(label="Quit tray")
        quit_.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def _tick(self) -> bool:
        self._refresh()
        return True  # keep firing

    def _refresh(self) -> None:
        try:
            state = json.loads(STATE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.indicator.set_label("?", "no data")
            self.indicator.set_icon_full(str(ICONS["grey"]), "no data yet")
            for mi in self.detail_items:
                mi.set_label("(no data — run install.sh)")
            self.updated_item.set_label("Last updated: never")
            return

        latest = latest_per_window(state)
        worst_remaining = 100.0
        label_segments: list[str] = []

        for i, window in enumerate(self.windows):
            r = latest.get(window)
            if r is None:
                self.detail_items[i].set_label(f"{pretty_window(window)}: no data")
                continue
            remaining = max(0.0, 100.0 - float(r["used_pct"]))
            worst_remaining = min(worst_remaining, remaining)
            eta = fmt_eta(r["reset_at"])
            label_segments.append(f"{pretty_window(window)} {remaining:.0f}% ({eta})")
            self.detail_items[i].set_label(
                f"{pretty_window(window)}: {remaining:.0f}% left · "
                f"resets {fmt_local(r['reset_at'])} (in {eta})"
            )

        if label_segments:
            label = " · ".join(label_segments)
            self.indicator.set_label(label, "5h 100% (0h0m) · W 100% (0d0h)")
            self.indicator.set_icon_full(
                str(ICONS[color_for_remaining(worst_remaining)]),
                f"{worst_remaining:.0f}% remaining (worst window)",
            )
        else:
            self.indicator.set_label("?", "no readings")
            self.indicator.set_icon_full(str(ICONS["grey"]), "no readings")

        last_ts = max((h["ts"] for h in state.get("history", [])), default=0)
        if last_ts:
            local = datetime.fromtimestamp(last_ts).astimezone().strftime("%H:%M:%S")
            self.updated_item.set_label(f"Last updated: {local}")

    def _on_refresh(self, *_: object) -> None:
        # Trigger the existing systemd unit if installed; otherwise run the
        # script directly. Either way, the next _tick will pick up new state.
        try:
            subprocess.Popen(
                ["systemctl", "--user", "start", "claudometer.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            subprocess.Popen(
                [sys.executable, str(MONITOR_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        # Optimistic: refresh in 8 s after the poll likely finishes.
        GLib.timeout_add_seconds(8, lambda: (self._refresh(), False)[1])

    def _on_view_raw(self, *_: object) -> None:
        if LAST_RESPONSE_PATH.exists():
            subprocess.Popen(["xdg-open", str(LAST_RESPONSE_PATH)])

    def _on_view_logs(self, *_: object) -> None:
        # Open a terminal showing the last 100 lines of the monitor's journal.
        for term in ("gnome-terminal", "x-terminal-emulator", "xterm"):
            if subprocess.run(["which", term], capture_output=True).returncode == 0:
                subprocess.Popen(
                    [term, "--", "bash", "-c",
                     "journalctl --user -u claudometer.service -n 100 --no-pager; "
                     "echo; read -p 'Press enter to close...'"]
                )
                return


def main() -> int:
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
