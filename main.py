"""Myanmar 2D live runner.

Fetches the current set/value, merges today's daily result, and posts the
live JSON to save.php?name=lv. Clock is Myanmar time (UTC+6:30).

Repeats every 0.5s from 09:30 through 12:10 and from 13:50 through 16:30.
Saturday and Sunday stay idle. No socket server.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = "http://213.136.80.25:8080"
SITE = "https://shwemyanmar2d.us"
LIVE_URL = f"{SITE}/save.php?name=lv"
POLL_SECONDS = 0.5
TIMEOUT = (5, 15)
TZ = ZoneInfo("Asia/Yangon")

MORNING_LIVE_START = clock_time(9, 30, 0)
MORNING_LIVE_END = clock_time(12, 1, 0)
MORNING_BREAK_END = clock_time(13, 50, 0)
EVENING_LIVE_END = clock_time(16, 30, 0)
MORNING_POLL_END = clock_time(12, 10, 0)

PLACEHOLDER = "----.--"
HOLIDAY = "holiday"
PRE_MORNING = "pre_morning"
MORNING_LIVE = "morning_live"
MORNING_BREAK = "morning_break"
EVENING_LIVE = "evening_live"
EVENING_BREAK = "evening_break"

WINDOW_LABEL = {
    HOLIDAY: "international holiday",
    PRE_MORNING: "Pre Morning",
    MORNING_LIVE: "Morning Live",
    MORNING_BREAK: "morning break",
    EVENING_LIVE: "evening live",
    EVENING_BREAK: "evening break",
}

BASE_DIR = Path(__file__).resolve().parent
LIVE_FILE = BASE_DIR / "live.json"
UNDATE_FILE = BASE_DIR / "undate.json"
UPDATETIME_FILE = BASE_DIR / "updatetime"

DEFAULT_UNDATE = {
    "date": "",
    "1200set": PLACEHOLDER,
    "1200value": PLACEHOLDER,
    "1200": "--",
    "430set": PLACEHOLDER,
    "430value": PLACEHOLDER,
    "430": "--",
    "930modern": "---",
    "930internet": "---",
    "200modern": "---",
    "200internet": "---",
    "1100": "--",
    "300": "--",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("liverunner")


def clock_of(now: datetime) -> clock_time:
    return now.time().replace(microsecond=0)


def window_of(now: datetime) -> str:
    """Which job the current Myanmar clock should run."""
    if now.weekday() >= 5:
        return HOLIDAY
    current = clock_of(now)
    if current < MORNING_LIVE_START:
        return PRE_MORNING
    if current <= MORNING_LIVE_END:
        return MORNING_LIVE
    if current <= MORNING_BREAK_END:
        return MORNING_BREAK
    if current <= EVENING_LIVE_END:
        return EVENING_LIVE
    return EVENING_BREAK


def in_poll_window(now: datetime) -> bool:
    """True while the original 500ms timer keeps fetching."""
    if now.weekday() >= 5:
        return False
    current = clock_of(now)
    if MORNING_LIVE_START <= current <= MORNING_POLL_END:
        return True
    if MORNING_BREAK_END <= current <= EVENING_LIVE_END:
        return True
    return False


def format_updatetime(now: datetime) -> str:
    hour = now.hour
    if hour == 0:
        clock_hour = 12
        suffix = "AM"
    elif hour < 12:
        clock_hour = hour
        suffix = "AM"
    elif hour == 12:
        clock_hour = 12
        suffix = "PM"
    else:
        clock_hour = hour - 12
        suffix = "PM"
    return f"{now:%Y/%m/%d} {clock_hour:02d}:{now.minute:02d}:{now.second:02d} {suffix}"


def as_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"missing set/value: {raw!r}")
    if isinstance(raw, float):
        return f"{raw:.2f}"
    return str(raw).strip()


def getlive(set_text: str, value_text: str) -> str:
    """Last digit of set + the digit immediately before the value decimal."""
    if not set_text:
        raise ValueError("empty set")
    dot = value_text.find(".")
    if dot <= 0:
        raise ValueError(f"value has no decimal point: {value_text!r}")
    return set_text[-1] + value_text[dot - 1]


def build_live_map(
    window: str,
    row: dict,
    set_text: str,
    value_text: str,
    updated_at: str,
    from_fallback: bool,
) -> dict | None:
    """Build the object that used to be written to the live file."""
    row = dict(row)
    if window == MORNING_LIVE:
        live = dict(row)
        live["status"] = "On"
        live["1200set"] = set_text
        live["1200value"] = value_text
        live["live"] = getlive(set_text, value_text)
        live["updatetime"] = updated_at
        return live

    if window == MORNING_BREAK:
        if row.get("1200set") == PLACEHOLDER:
            live = {
                "1200set": set_text,
                "1200value": value_text,
                "live": getlive(set_text, value_text),
                "status": "On",
            }
            for key, value in row.items():
                if key not in ("1200set", "1200value"):
                    live[key] = value
            live["updatetime"] = updated_at
            return live
        live = {"live": row.get("1200"), "status": "OFF"}
        live.update(row)
        live["updatetime"] = updated_at
        return live

    if window == EVENING_LIVE:
        live = dict(row)
        live["430set"] = set_text
        live["430value"] = value_text
        live["live"] = getlive(set_text, value_text)
        live["status"] = "On"
        live["updatetime"] = updated_at
        return live

    if window == EVENING_BREAK:
        # No daily row after 16:30 keeps the template result and turns live off.
        # A row that is still "----.--" keeps the API number on.
        if not from_fallback and row.get("430set") == PLACEHOLDER:
            live = {
                key: value
                for key, value in row.items()
                if key not in ("430set", "430value")
            }
            live["430set"] = set_text
            live["430value"] = value_text
            live["live"] = getlive(set_text, value_text)
            live["status"] = "On"
            live["updatetime"] = updated_at
            return live
        live = dict(row)
        live["live"] = row.get("430")
        live["status"] = "OFF"
        live["updatetime"] = updated_at
        return live

    return None


def read_json(response: requests.Response) -> object:
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        snippet = " ".join(response.text[:200].split())
        raise ValueError(f"response is not JSON ({response.status_code}): {snippet}") from exc


def parse_api(payload: object) -> tuple[str, str]:
    if not isinstance(payload, dict) or "set" not in payload or "value" not in payload:
        raise ValueError(f"API JSON must contain set and value, got {payload!r}"[:300])
    return as_text(payload["set"]), as_text(payload["value"])


def parse_daily(payload: object) -> dict | None:
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list):
        raise ValueError("daily results must be a JSON array")
    if not payload:
        return None
    row = payload[-1]
    if not isinstance(row, dict):
        raise ValueError("daily result row must be an object")
    return row


def load_fallback(now: datetime) -> dict:
    if UNDATE_FILE.exists():
        loaded = json.loads(UNDATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("undate.json must be an object")
        row = loaded
    else:
        row = dict(DEFAULT_UNDATE)
    row["date"] = now.strftime("%Y/%m/%d")
    return row


class LiveRunner:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "newliverunner/1.0"
        self.last_payload: str | None = None

    def remember_updatetime(self, window: str, now: datetime) -> str:
        stamp = format_updatetime(now)
        if window in (MORNING_LIVE, EVENING_LIVE):
            UPDATETIME_FILE.write_text(stamp, encoding="utf-8")
            return stamp
        if UPDATETIME_FILE.exists():
            saved = UPDATETIME_FILE.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        UPDATETIME_FILE.write_text(stamp, encoding="utf-8")
        return stamp

    def fetch_api(self) -> tuple[str, str]:
        response = self.session.get(API_URL, timeout=TIMEOUT)
        set_text, value_text = parse_api(read_json(response))
        log.info("API GET SUCCESS set=%s value=%s", set_text, value_text)
        return set_text, value_text

    def fetch_daily(self, now: datetime) -> dict | None:
        day = now.strftime("%Y/%m/%d")
        query = f"SELECT * FROM `dailyresults` WHERE date='{day}'"
        response = self.session.get(SITE, params={"q": query}, timeout=TIMEOUT)
        return parse_daily(read_json(response))

    def publish(self, window: str, live_map: dict) -> None:
        payload = json.dumps(live_map, ensure_ascii=False, separators=(",", ":"))
        if payload == self.last_payload:
            return
        LIVE_FILE.write_text(payload, encoding="utf-8")
        summary = "posted %s live=%s status=%s" % (
            WINDOW_LABEL[window],
            live_map.get("live"),
            live_map.get("status"),
        )
        if self.dry_run:
            log.info("dry-run %s %s", summary, payload)
            self.last_payload = payload
            return
        response = self.session.post(
            LIVE_URL,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=TIMEOUT,
        )
        if response.status_code >= 400:
            snippet = " ".join(response.text[:300].split())
            raise RuntimeError(f"save.php {response.status_code}: {snippet}")
        self.last_payload = payload
        log.info("%s -> %s", summary, response.status_code)

    def cycle(self, now: datetime) -> None:
        window = window_of(now)
        if window in (HOLIDAY, PRE_MORNING):
            return
        set_text, value_text = self.fetch_api()
        row = self.fetch_daily(now)
        from_fallback = row is None
        if from_fallback:
            row = load_fallback(now)
            log.info("no daily row for %s, using undate", now.strftime("%Y/%m/%d"))
        updated_at = self.remember_updatetime(window, now)
        live_map = build_live_map(
            window, row, set_text, value_text, updated_at, from_fallback
        )
        if live_map is None:
            return
        self.publish(window, live_map)

    def run_forever(self) -> None:
        log.info("started. polling %.1fs, posting to %s", POLL_SECONDS, LIVE_URL)
        last_window: str | None = None
        while True:
            started = time.monotonic()
            now = datetime.now(TZ)
            window = window_of(now)
            try:
                if window != last_window:
                    log.info(WINDOW_LABEL[window])
                    last_window = window
                    self.cycle(now)
                elif in_poll_window(now):
                    self.cycle(now)
            except requests.Timeout:
                log.warning("request timed out")
            except Exception:
                log.exception("cycle failed")
            remaining = POLL_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)


def self_test() -> None:
    assert getlive("1571.48", "39979.82") == "89"
    assert getlive("1566.10", "65443.68") == "03"

    def at(day: int, hour: int, minute: int, second: int = 0, month: int = 10) -> datetime:
        return datetime(2026, month, day, hour, minute, second, tzinfo=TZ)

    assert at(31, 10, 0).weekday() == 5
    assert window_of(at(31, 10, 0)) == HOLIDAY
    assert window_of(at(1, 10, 0, month=11)) == HOLIDAY
    assert not in_poll_window(at(31, 10, 0))

    wednesday = 28
    assert at(wednesday, 10, 0).weekday() == 2
    assert window_of(at(wednesday, 9, 29, 59)) == PRE_MORNING
    assert not in_poll_window(at(wednesday, 9, 29, 59))
    assert window_of(at(wednesday, 9, 30, 0)) == MORNING_LIVE
    assert window_of(at(wednesday, 12, 1, 0)) == MORNING_LIVE
    assert in_poll_window(at(wednesday, 12, 1, 0))
    assert window_of(at(wednesday, 12, 1, 1)) == MORNING_BREAK
    assert in_poll_window(at(wednesday, 12, 10, 0))
    assert window_of(at(wednesday, 12, 10, 1)) == MORNING_BREAK
    assert not in_poll_window(at(wednesday, 12, 10, 1))
    assert window_of(at(wednesday, 13, 50, 0)) == MORNING_BREAK
    assert in_poll_window(at(wednesday, 13, 50, 0))
    assert window_of(at(wednesday, 13, 50, 1)) == EVENING_LIVE
    assert window_of(at(wednesday, 16, 30, 0)) == EVENING_LIVE
    assert in_poll_window(at(wednesday, 16, 30, 0))
    assert window_of(at(wednesday, 16, 30, 1)) == EVENING_BREAK
    assert not in_poll_window(at(wednesday, 16, 30, 1))

    noon = at(wednesday, 12, 0, 0)
    midnight = at(wednesday, 0, 5, 9)
    evening = at(wednesday, 16, 29, 54)
    assert format_updatetime(noon) == "2026/10/28 12:00:00 PM"
    assert format_updatetime(midnight) == "2026/10/28 12:05:09 AM"
    assert format_updatetime(evening) == "2026/10/28 04:29:54 PM"

    stamp = "2026/10/29 10:15:00 AM"
    morning_row = {
        "date": "2026/10/29",
        "1200set": PLACEHOLDER,
        "1200value": PLACEHOLDER,
        "1200": "--",
        "430set": PLACEHOLDER,
        "430value": PLACEHOLDER,
        "430": "--",
        "930modern": "839",
    }
    live = build_live_map(MORNING_LIVE, morning_row, "1600.55", "12345.67", stamp, True)
    assert live is not None
    assert live["1200set"] == "1600.55"
    assert live["1200value"] == "12345.67"
    assert live["live"] == "55"
    assert live["status"] == "On"
    assert live["1200"] == "--"
    assert live["930modern"] == "839"
    assert list(live).index("status") < list(live).index("live")

    waiting = build_live_map(MORNING_BREAK, morning_row, "1600.55", "12345.67", stamp, False)
    assert waiting is not None
    assert waiting["status"] == "On"
    assert waiting["live"] == "55"
    assert list(waiting)[:4] == ["1200set", "1200value", "live", "status"]

    published = dict(morning_row)
    published["1200set"] = "1571.48"
    published["1200value"] = "39979.82"
    published["1200"] = "89"
    official = build_live_map(MORNING_BREAK, published, "1600.55", "12345.67", stamp, False)
    assert official is not None
    assert official["status"] == "OFF"
    assert official["live"] == "89"
    assert official["1200set"] == "1571.48"
    assert list(official)[:2] == ["live", "status"]

    evening = build_live_map(EVENING_LIVE, published, "1566.10", "65443.68", stamp, False)
    assert evening is not None
    assert evening["430set"] == "1566.10"
    assert evening["430value"] == "65443.68"
    assert evening["live"] == "03"
    assert evening["status"] == "On"
    assert evening["1200"] == "89"

    still_open = build_live_map(EVENING_BREAK, morning_row, "1566.10", "65443.68", stamp, False)
    assert still_open is not None
    assert still_open["status"] == "On"
    assert still_open["live"] == "03"
    assert still_open["430set"] == "1566.10"

    closed = dict(published)
    closed["430set"] = "1566.10"
    closed["430value"] = "65443.68"
    closed["430"] = "03"
    done = build_live_map(EVENING_BREAK, closed, "1000.00", "1000.00", stamp, False)
    assert done is not None
    assert done["status"] == "OFF"
    assert done["live"] == "03"
    assert done["430set"] == "1566.10"

    fallback = build_live_map(EVENING_BREAK, dict(DEFAULT_UNDATE), "1566.10", "65443.68", stamp, True)
    assert fallback is not None
    assert fallback["status"] == "OFF"
    assert fallback["live"] == "--"
    assert build_live_map(HOLIDAY, morning_row, "1.0", "1.0", stamp, False) is None
    print("self-test ok")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch live set/value and post JSON to save.php?name=lv")
    parser.add_argument("--once", action="store_true", help="run one cycle, then exit")
    parser.add_argument("--dry-run", action="store_true", help="write live.json and do not post")
    parser.add_argument("--self-test", action="store_true", help="check the clock and JSON rules")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return
    runner = LiveRunner(dry_run=args.dry_run)
    if args.once:
        now = datetime.now(TZ)
        log.info(WINDOW_LABEL[window_of(now)])
        runner.cycle(now)
        return
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
