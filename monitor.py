#!/usr/bin/env python3
"""Claude plan usage monitor.

Polls https://claude.ai/api/organizations/<org_id>/usage and emits desktop
notifications via notify-send for: window resets, remaining quota below 20%,
and burn-rate projections that would exhaust the window before its reset.

Run periodically from a systemd user timer. See ./README.md for setup.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USAGE_URL = "https://claude.ai/api/organizations/{org_id}/usage"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

def _xdg(env: str, fallback: str) -> Path:
    return Path(os.environ.get(env) or os.path.expanduser(fallback))


CONFIG_PATH = _xdg("XDG_CONFIG_HOME", "~/.config") / "claudometer" / "config.ini"
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / "claudometer"
STATE_PATH = STATE_DIR / "state.json"
LAST_RESPONSE_PATH = STATE_DIR / "last_response.json"

WINDOW_KEYWORDS = {
    "five_hour": ("five_hour", "5h", "5_hour", "session", "current_5h", "fivehour"),
    "weekly": ("weekly", "week", "7_day", "7day", "seven_day"),
    "weekly_opus": ("weekly_opus", "opus_weekly", "opus_week", "seven_day_opus"),
}

TIMESTAMP_KEYS = (
    "reset_at",
    "resets_at",
    "reset_time",
    "resetTime",
    "expires_at",
    "window_end",
    "windowEnd",
    "ends_at",
    "next_reset",
)

USED_KEYS = ("used", "usage", "used_count", "usage_count")
PERCENT_KEYS = (
    "used_percent",
    "used_percentage",
    "percent_used",
    "percentage_used",
    "percent",
    "percentage",
    "utilization",
    "usage_percent",
    "usage_percentage",
)
LIMIT_KEYS = ("limit", "max", "total", "cap", "quota")


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    org_id: str
    cookie_file: Path
    windows: list[str]
    alerts: list[str]
    low_remaining_pct: float = 20.0
    burn_rate_min_history_min: int = 30
    burn_rate_window_min: int = 60
    history_retention_min: int = 120
    auth_error_throttle_hours: int = 6
    http_timeout_sec: int = 15
    schema_error_throttle_hours: int = 6

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            die(
                f"Config not found at {path}. Run install.sh or copy "
                f"config.example.ini to {path}."
            )
        cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
        cp.read(path)
        s = cp["claudometer"]

        def _list(key: str, default: list[str]) -> list[str]:
            raw = s.get(key, "")
            items = [x.strip() for x in raw.split(",") if x.strip()]
            return items or default

        return cls(
            org_id=s["org_id"].strip(),
            cookie_file=Path(os.path.expanduser(s["cookie_file"].strip())),
            windows=_list("windows", ["five_hour", "weekly"]),
            alerts=_list("alerts", ["reset", "low_remaining", "burn_rate"]),
            low_remaining_pct=s.getfloat("low_remaining_pct", fallback=20.0),
            burn_rate_min_history_min=s.getint("burn_rate_min_history_min", fallback=30),
            burn_rate_window_min=s.getint("burn_rate_window_min", fallback=60),
            history_retention_min=s.getint("history_retention_min", fallback=120),
            auth_error_throttle_hours=s.getint("auth_error_throttle_hours", fallback=6),
            http_timeout_sec=s.getint("http_timeout_sec", fallback=15),
            schema_error_throttle_hours=s.getint("schema_error_throttle_hours", fallback=6),
        )


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"history": [], "fired": {}, "last_auth_error_ts": 0}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {"history": [], "fired": {}, "last_auth_error_ts": 0}


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def fetch_usage(cfg: Config) -> tuple[int, bytes]:
    cookie = cfg.cookie_file.read_text().strip()
    if not cookie:
        return 0, b""
    req = urllib.request.Request(
        USAGE_URL.format(org_id=cfg.org_id),
        headers={
            "Cookie": f"sessionKey={cookie}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://claude.ai/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.http_timeout_sec) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, str(e).encode()


# --------------------------------------------------------------------------- #
# Schema-tolerant parser
# --------------------------------------------------------------------------- #


@dataclass
class WindowReading:
    canonical: str            # five_hour | weekly | weekly_opus | <unmapped path>
    used_pct: float           # 0..100
    reset_at: str             # ISO-8601 string as found
    raw_path: str = ""        # JSON path it was extracted from (debugging)
    limit: float | None = None
    used: float | None = None


def _classify(path: str) -> str | None:
    p = path.lower()
    for canon, kws in WINDOW_KEYWORDS.items():
        if any(kw in p for kw in kws):
            if canon == "weekly" and "opus" in p:
                return "weekly_opus"
            return canon
    return None


def _to_pct(used: Any, limit: Any, percent: Any) -> float | None:
    if percent is not None:
        try:
            v = float(percent)
        except (TypeError, ValueError):
            return None
        if 0 <= v <= 1.0001:
            v *= 100
        return max(0.0, min(100.0, v))
    if used is None or limit in (None, 0):
        return None
    try:
        u, lim = float(used), float(limit)
    except (TypeError, ValueError):
        return None
    if lim <= 0:
        return None
    return max(0.0, min(100.0, 100 * u / lim))


def _normalize_reset_at(ts: Any) -> str:
    """Round to whole-minute precision so the API's microsecond drift between
    polls doesn't make every poll look like a fresh reset window."""
    s = str(ts)
    if s == IDLE_RESET:
        return s
    dt = parse_iso(s)
    if dt is None:
        return s
    dt = dt.replace(second=0, microsecond=0)
    return dt.isoformat()


def parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


IDLE_RESET = "idle"  # sentinel for windows that exist but have no active timer


def _add_reading(
    found: list[WindowReading],
    path: str,
    used: Any,
    limit: Any,
    percent: Any,
    ts: Any,
    label_hint: str = "",
) -> None:
    used_pct = _to_pct(used, limit, percent)
    if used_pct is None:
        return
    # Window with no active timer (e.g. 5h just reset and you haven't sent a
    # message yet): server returns resets_at=null, utilization=0. Record it as
    # idle so the tray still shows the window; skip alert evaluation later.
    if not ts:
        if used_pct > 0.5:
            return  # genuinely missing data, not idle
        ts = IDLE_RESET
    canon = _classify(label_hint) or _classify(path) or path or "unknown"
    try:
        lim_f = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        lim_f = None
    try:
        used_f = float(used) if used is not None else None
    except (TypeError, ValueError):
        used_f = None
    found.append(
        WindowReading(
            canonical=canon,
            used_pct=used_pct,
            reset_at=_normalize_reset_at(ts),
            raw_path=path,
            limit=lim_f,
            used=used_f,
        )
    )


def _scan_flat_prefixes(node: dict[str, Any], path: str, found: list[WindowReading]) -> None:
    """For shapes like {five_hour_used, five_hour_limit, five_hour_resets_at},
    group sibling keys by their shared prefix and emit a reading per group."""
    suffix_groups = (
        ("used", USED_KEYS),
        ("limit", LIMIT_KEYS),
        ("percent", PERCENT_KEYS),
        ("ts", TIMESTAMP_KEYS),
    )
    by_prefix: dict[str, dict[str, Any]] = {}
    for key, val in node.items():
        if isinstance(val, (dict, list)):
            continue
        for kind, suffixes in suffix_groups:
            for suf in suffixes:
                m = re.fullmatch(rf"(.+?)[._]?{re.escape(suf)}", key, re.IGNORECASE)
                if m:
                    prefix = m.group(1).strip("._-")
                    if prefix:
                        by_prefix.setdefault(prefix, {})[kind] = val
                    break
    for prefix, parts in by_prefix.items():
        if "ts" not in parts:
            continue
        if "used" not in parts and "percent" not in parts:
            continue
        _add_reading(
            found,
            f"{path}.{prefix}" if path else prefix,
            parts.get("used"),
            parts.get("limit"),
            parts.get("percent"),
            parts.get("ts"),
        )


def _walk(node: Any, path: str, found: list[WindowReading]) -> None:
    """Recursively look for objects that carry both a usage and a reset
    timestamp. Tag each by the JSON path it was found at."""
    if isinstance(node, dict):
        ts = next((node[k] for k in TIMESTAMP_KEYS if k in node), None)
        used = next((node[k] for k in USED_KEYS if k in node), None)
        percent = next((node[k] for k in PERCENT_KEYS if k in node), None)
        limit = next((node[k] for k in LIMIT_KEYS if k in node), None)
        label_hint = " ".join(
            str(node[k]) for k in ("name", "type", "id", "key", "window") if k in node
        )
        _add_reading(found, path, used, limit, percent, ts, label_hint)
        _scan_flat_prefixes(node, path, found)
        for k, v in node.items():
            _walk(v, f"{path}.{k}" if path else k, found)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]", found)


def parse_usage(payload: dict[str, Any]) -> list[WindowReading]:
    found: list[WindowReading] = []
    _walk(payload, "", found)
    # Deduplicate: prefer the most-specific (longest path) reading per canonical.
    by_canon: dict[str, WindowReading] = {}
    for r in found:
        prev = by_canon.get(r.canonical)
        if prev is None or len(r.raw_path) < len(prev.raw_path):
            by_canon[r.canonical] = r
    return list(by_canon.values())


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


def notify(title: str, body: str, urgency: str = "normal", icon: str = "dialog-information") -> None:
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name=Claudometer",
                f"--urgency={urgency}",
                f"--icon={icon}",
                title,
                body,
            ],
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"notify-send failed: {e}", file=sys.stderr)


def fmt_local(ts: str) -> str:
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%a %H:%M")


def hours_until(ts: str) -> float | None:
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600


# --------------------------------------------------------------------------- #
# Alert logic
# --------------------------------------------------------------------------- #


@dataclass
class Decision:
    fire: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    # (alert_kind, window, title, body, fired_key)


def _key(window: str, alert: str, reset_at: str) -> str:
    return f"{window}|{alert}|{reset_at}"


def evaluate(
    cfg: Config,
    state: dict[str, Any],
    readings: list[WindowReading],
    now_ts: float,
    forced: set[str],
) -> Decision:
    decision = Decision()
    fired: dict[str, float] = state.setdefault("fired", {})
    history: list[dict[str, Any]] = state.setdefault("history", [])

    # Append current readings to rolling history, evict old.
    cutoff = now_ts - cfg.history_retention_min * 60
    history[:] = [h for h in history if h["ts"] >= cutoff]
    for r in readings:
        history.append(
            {
                "ts": now_ts,
                "window": r.canonical,
                "used_pct": r.used_pct,
                "reset_at": r.reset_at,
            }
        )

    last_seen_reset: dict[str, str] = state.setdefault("last_reset_at", {})

    for r in readings:
        if r.canonical not in cfg.windows and not forced:
            continue

        prev_reset = last_seen_reset.get(r.canonical)
        prev_used_pct = _previous_used_pct(history, r.canonical, now_ts)
        is_idle = r.reset_at == IDLE_RESET

        # Reset (also fires when an active window transitions to idle).
        if "reset" in cfg.alerts:
            reset_now = (
                prev_reset is not None
                and prev_reset != IDLE_RESET
                and prev_reset != r.reset_at
            ) or (prev_used_pct is not None and prev_used_pct - r.used_pct > 50)
            k = _key(r.canonical, "reset", r.reset_at)
            if (reset_now and k not in fired) or "reset" in forced:
                next_reset = "soon (no active window yet)" if is_idle else fmt_local(r.reset_at)
                decision.fire.append(
                    (
                        "reset",
                        r.canonical,
                        f"Claude {_pretty(r.canonical)} window reset",
                        f"100% available. Next reset {next_reset}.",
                        k,
                    )
                )

        # Idle windows can't trigger low_remaining or burn_rate.
        last_seen_reset[r.canonical] = r.reset_at
        if is_idle and not forced:
            continue

        # Low remaining (configurable threshold).
        if "low_remaining" in cfg.alerts:
            k = _key(r.canonical, "low_remaining", r.reset_at)
            remaining = max(0.0, 100 - r.used_pct)
            triggered = remaining <= cfg.low_remaining_pct and k not in fired
            if triggered or "low_remaining" in forced:
                decision.fire.append(
                    (
                        "low_remaining",
                        r.canonical,
                        f"Claude {_pretty(r.canonical)}: {remaining:.0f}% left",
                        f"Threshold {cfg.low_remaining_pct:.0f}%. "
                        f"Resets {fmt_local(r.reset_at)}.",
                        k,
                    )
                )

        # Burn rate
        if "burn_rate" in cfg.alerts:
            rate = _burn_rate(history, r.canonical, now_ts, cfg)
            hrs = hours_until(r.reset_at)
            remaining = max(0.0, 100 - r.used_pct)
            will_exhaust = (
                rate is not None
                and hrs is not None
                and hrs > 0
                and rate > 0
                and rate * hrs > remaining
            )
            k = _key(r.canonical, "burn_rate", r.reset_at)
            if (will_exhaust and k not in fired) or "burn_rate" in forced:
                if rate and rate > 0:
                    eta_h = remaining / rate
                    eta_ts = datetime.now(timezone.utc).timestamp() + eta_h * 3600
                    eta_str = datetime.fromtimestamp(eta_ts).astimezone().strftime("%a %H:%M")
                    body_text = (
                        f"At {rate:.1f}%/h you'll hit 0 around {eta_str} "
                        f"(reset {fmt_local(r.reset_at)})."
                    )
                else:
                    body_text = (
                        f"Forced burn-rate alert (no rate yet). "
                        f"{remaining:.0f}% left, resets {fmt_local(r.reset_at)}."
                    )
                decision.fire.append(
                    (
                        "burn_rate",
                        r.canonical,
                        f"Claude {_pretty(r.canonical)} burning fast",
                        body_text,
                        k,
                    )
                )

    return decision


def _previous_used_pct(
    history: list[dict[str, Any]], window: str, now_ts: float
) -> float | None:
    prev = [h for h in history if h["window"] == window and h["ts"] < now_ts]
    if not prev:
        return None
    return prev[-1]["used_pct"]


def _burn_rate(
    history: list[dict[str, Any]], window: str, now_ts: float, cfg: Config
) -> float | None:
    """Percentage points consumed per hour over the last `burn_rate_window_min`,
    requiring at least `burn_rate_min_history_min` of data. None if not enough
    history, used_pct is going down (i.e. just reset), or rate is non-positive.
    """
    cutoff = now_ts - cfg.burn_rate_window_min * 60
    pts = [h for h in history if h["window"] == window and h["ts"] >= cutoff]
    if len(pts) < 2:
        return None
    span_sec = pts[-1]["ts"] - pts[0]["ts"]
    if span_sec < cfg.burn_rate_min_history_min * 60:
        return None
    delta = pts[-1]["used_pct"] - pts[0]["used_pct"]
    if delta <= 0:
        return None
    return delta * 3600 / span_sec


def _pretty(canon: str) -> str:
    return {
        "five_hour": "5h",
        "weekly": "weekly",
        "weekly_opus": "weekly Opus",
    }.get(canon, canon)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def maybe_auth_error_notify(state: dict[str, Any], cfg: Config, body: str) -> None:
    now = time.time()
    last = float(state.get("last_auth_error_ts", 0))
    if now - last < cfg.auth_error_throttle_hours * 3600:
        return
    notify(
        "Claudometer monitor: auth failed",
        body,
        urgency="critical",
        icon="dialog-error",
    )
    state["last_auth_error_ts"] = now


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--selftest", action="store_true",
                    help="fire a test notification and do one fetch+parse")
    ap.add_argument("--force-alert", action="append", default=[],
                    choices=["reset", "low_remaining", "burn_rate"],
                    help="emit this alert kind regardless of state (repeatable)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate but do not call notify-send or persist state")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.selftest:
        notify(
            "Claudometer monitor: self-test OK",
            "Notifications work. Fetching usage to validate cookie + schema...",
        )

    cfg = Config.load(args.config)
    state = load_state()

    # Prune stale history before any network call so that error-path
    # save_state() calls don't preserve entries older than the retention window.
    _cutoff = time.time() - cfg.history_retention_min * 60
    state["history"] = [h for h in state.get("history", []) if h["ts"] >= _cutoff]

    if not cfg.cookie_file.exists():
        maybe_auth_error_notify(
            state, cfg, f"Cookie file missing at {cfg.cookie_file}. Run install.sh."
        )
        save_state(state)
        return 2

    status, body = fetch_usage(cfg)
    if args.debug:
        print(f"HTTP {status}, {len(body)} bytes")

    if status != 200:
        LAST_RESPONSE_PATH.write_bytes(body[:200_000])
        maybe_auth_error_notify(
            state,
            cfg,
            f"HTTP {status} from claude.ai. Re-paste sessionKey cookie via install.sh.",
        )
        save_state(state)
        return 3

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        LAST_RESPONSE_PATH.write_bytes(body[:200_000])
        maybe_auth_error_notify(
            state, cfg, "Response was not JSON. See last_response.json."
        )
        save_state(state)
        return 4

    LAST_RESPONSE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))

    readings = parse_usage(payload)
    if args.debug:
        for r in readings:
            print(f"  {r.canonical} ({r.raw_path}): {r.used_pct:.1f}% used, "
                  f"reset {r.reset_at}")
    if not readings:
        now = time.time()
        last = float(state.get("last_schema_error_ts", 0))
        if now - last >= cfg.schema_error_throttle_hours * 3600:
            notify(
                "Claudometer monitor: schema mismatch",
                f"No usage windows parsed from response. See {LAST_RESPONSE_PATH}.",
                urgency="critical",
                icon="dialog-warning",
            )
            state["last_schema_error_ts"] = now
        save_state(state)
        return 5

    forced = set(args.force_alert)
    decision = evaluate(cfg, state, readings, time.time(), forced)

    for kind, window, title, body_text, fired_key in decision.fire:
        urgency = "normal" if kind == "reset" else "critical"
        icon = {
            "reset": "dialog-information",
            "low_remaining": "dialog-warning",
            "burn_rate": "dialog-warning",
        }[kind]
        if args.dry_run:
            print(f"[dry-run] {urgency} {title} :: {body_text}")
        else:
            notify(title, body_text, urgency=urgency, icon=icon)
            state.setdefault("fired", {})[fired_key] = time.time()

    if args.selftest:
        notify(
            "Claudometer monitor: fetch OK",
            f"Parsed {len(readings)} window(s): "
            + ", ".join(r.canonical for r in readings),
        )

    if not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
