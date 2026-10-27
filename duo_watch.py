#!/usr/bin/env python3
"""
duo-watch: polls Apple Canada's pickup-availability endpoint for the iPhone Duo
and sends an ntfy push the moment a watched Lower Mainland store shows stock.

Stdlib only (no pip install needed), so it runs anywhere Python 3.10+ does:
GitHub Actions, your Mac, a Raspberry Pi, a VPS.

Config is all environment variables (see README.md):
  DUO_STORAGE    required  e.g. "256GB" or "512GB,1TB"
  DUO_COLORS     optional  "Star White,Night Sky" (default: both)
  DUO_BAG_COLOR  optional  "Star White" or "Night Sky": alerts for it open your bag
  NTFY_TOPIC     required  your secret ntfy topic name
  NTFY_SERVER    optional  default https://ntfy.sh
  WATCH_STORES   optional  comma list of store IDs (default: all 5 Lower Mainland stores)
  POLL_SECONDS   optional  seconds between checks (default 75, plus jitter)
  RUN_MINUTES    optional  how long to keep looping before exiting (default 0 = forever)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Static data
# --------------------------------------------------------------------------- #

# Canadian (A3719, VC/A) iPhone Duo part numbers. Source: EveryMac's A3719 page.
# Double-check yours: on apple.com/ca, pick the config, and the part number is
# in the URL / "Check availability" request.
PARTS: dict[tuple[str, str], str] = {
    ("256GB", "Star White"): "MK244VC/A",
    ("512GB", "Star White"): "MK264VC/A",
    ("1TB", "Star White"): "MK284VC/A",
    ("2TB", "Star White"): "MK2A4VC/A",
    ("256GB", "Night Sky"): "MK254VC/A",
    ("512GB", "Night Sky"): "MK274VC/A",
    ("1TB", "Night Sky"): "MK294VC/A",
    ("2TB", "Night Sky"): "MK2C4VC/A",
}
PART_COLORS: dict[str, str] = {part: color for (_, color), part in PARTS.items()}

# Apple's own store list (apple.com/autopush/ca/retail/storelist/stores.xml).
LOWER_MAINLAND_STORES: dict[str, str] = {
    "R460": "Metrotown",
    "R280": "Pacific Centre",
    "R421": "Richmond Centre",
    "R488": "Coquitlam Centre",
    "R601": "Guildford Town Centre",
}

# Apple retired /shop/fulfillment-messages in Oct 2026 (it now always returns
# HTTP 541). This is what the store page itself calls now.
ENDPOINT = "https://www.apple.com/ca/shop/retail/pickup-message"

# Responses that mean "you're asking too often". 541 is Apple's own: the
# pickup-message endpoint starts returning it after hours of fast polling.
RATE_LIMIT_CODES = {403, 429, 503, 541}

# Metrotown sits in the middle of the five stores, so a "nearby" search
# anchored there returns all of them in one request.
ANCHOR_STORE = "R460"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/19.0 Safari/605.1.15"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": "https://www.apple.com/ca/shop/buy-iphone/iphone-duo",
}

UNAVAILABLE_PREFIXES = ("unavailable", "currently unavailable", "not available", "apple store pickup is currently unavailable")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    parts: dict[str, str]          # part number -> human label
    watch_stores: set[str]
    ntfy_server: str
    ntfy_topic: str
    poll_seconds: int
    run_minutes: int
    bag_color: str | None = None   # alerts for this colour open the bag

    @classmethod
    def from_env(cls, require_topic: bool = True) -> "Config":
        storage_raw = os.environ.get("DUO_STORAGE", "").strip()
        if not storage_raw:
            sys.exit("DUO_STORAGE is not set. Use one or more of: 256GB, 512GB, 1TB, 2TB")
        storages = {_norm_storage(s) for s in storage_raw.split(",") if s.strip()}
        bad = storages - {"256GB", "512GB", "1TB", "2TB"}
        if bad:
            sys.exit(f"Unknown DUO_STORAGE value(s): {', '.join(sorted(bad))}")

        colors_raw = os.environ.get("DUO_COLORS", "").strip()
        colors = {c.strip().title() for c in colors_raw.split(",") if c.strip()} or {"Star White", "Night Sky"}
        bad = colors - {"Star White", "Night Sky"}
        if bad:
            sys.exit(f"Unknown DUO_COLORS value(s): {', '.join(sorted(bad))}")

        bag_color = os.environ.get("DUO_BAG_COLOR", "").strip().title() or None
        if bag_color and bag_color not in {"Star White", "Night Sky"}:
            sys.exit(f"Unknown DUO_BAG_COLOR value: {bag_color}")
        if bag_color and bag_color not in colors:
            sys.exit(f"DUO_BAG_COLOR is {bag_color}, but DUO_COLORS only watches {', '.join(sorted(colors))}")

        parts = {
            part: f"iPhone Duo {storage} {color}"
            for (storage, color), part in PARTS.items()
            if storage in storages and color in colors
        }

        stores_raw = os.environ.get("WATCH_STORES", "").strip()
        watch = {s.strip().upper() for s in stores_raw.split(",") if s.strip()} or set(LOWER_MAINLAND_STORES)

        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if require_topic and not topic:
            sys.exit("NTFY_TOPIC is not set.")

        return cls(
            parts=parts,
            watch_stores=watch,
            ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=topic,
            poll_seconds=max(30, int(os.environ.get("POLL_SECONDS", "75"))),
            run_minutes=int(os.environ.get("RUN_MINUTES", "0")),
            bag_color=bag_color,
        )


def _norm_storage(s: str) -> str:
    s = s.strip().upper().replace(" ", "")
    return {"256": "256GB", "512": "512GB", "1T": "1TB", "2T": "2TB", "1000GB": "1TB", "2000GB": "2TB"}.get(s, s)


# --------------------------------------------------------------------------- #
# Apple endpoint
# --------------------------------------------------------------------------- #

class BlockedError(Exception):
    """Apple returned something other than usable JSON (rate limit / bot block)."""

    def __init__(self, msg: str, rate_limited: bool = False):
        super().__init__(msg)
        self.rate_limited = rate_limited


def build_url(parts: list[str], anchor_store: str = ANCHOR_STORE) -> str:
    params: list[tuple[str, str]] = [
        ("pl", "true"),
        ("mts.0", "regular"),
        ("store", anchor_store),
        ("searchNearby", "true"),
    ]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    return f"{ENDPOINT}?{urllib.parse.urlencode(params)}"


def fetch_availability(parts: list[str], timeout: int = 20) -> dict:
    req = urllib.request.Request(build_url(parts), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise BlockedError(f"HTTP {e.code}", rate_limited=e.code in RATE_LIMIT_CODES) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise BlockedError(f"network error: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise BlockedError("response was not JSON (likely a bot-check page)") from e


@dataclass(frozen=True)
class Hit:
    store_id: str
    store_name: str
    part: str
    quote: str


def is_available(info: dict) -> bool:
    """Decide if one store/part entry means 'you can reserve pickup now'."""
    display = str(info.get("pickupDisplay", "")).lower()
    if display:
        return display == "available"
    # Older response shape: no pickupDisplay, only a human-readable quote.
    quote = str(info.get("pickupSearchQuote") or info.get("storePickupQuote") or "").strip().lower()
    return bool(quote) and not quote.startswith(UNAVAILABLE_PREFIXES)


def parse_hits(payload: dict, parts: dict[str, str], watch_stores: set[str]) -> tuple[list[Hit], set[str]]:
    """Return (available hits in watched stores, store IDs seen in the response)."""
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise BlockedError("unexpected JSON shape (no body)")
    if isinstance(body.get("stores"), list):           # current pickup-message shape
        stores = body["stores"]
    else:                                              # old fulfillment-messages shape
        try:
            stores = body["content"]["pickupMessage"]["stores"]
        except (KeyError, TypeError) as e:
            raise BlockedError("unexpected JSON shape (no stores list)") from e

    hits: list[Hit] = []
    seen: set[str] = set()
    for store in stores or []:
        sid = str(store.get("storeNumber", "")).upper()
        seen.add(sid)
        if sid not in watch_stores:
            continue
        name = store.get("storeName") or LOWER_MAINLAND_STORES.get(sid, sid)
        for part, info in (store.get("partsAvailability") or {}).items():
            if part in parts and isinstance(info, dict) and is_available(info):
                quote = info.get("pickupSearchQuote") or info.get("storePickupQuote") or "Available"
                hits.append(Hit(sid, name, part, _strip_html(str(quote))))
    return hits, seen


def _strip_html(s: str) -> str:
    out, skip = [], False
    for ch in s:
        if ch == "<":
            skip = True
        elif ch == ">":
            skip = False
        elif not skip:
            out.append(ch)
    return "".join(out).strip()


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

BAG_URL = "https://www.apple.com/ca/shop/bag"


def product_url(part: str) -> str:
    return f"https://www.apple.com/ca/shop/product/{part}"


def click_url(cfg: Config, part: str) -> str:
    """The bag for the colour you've pre-loaded there, the product page otherwise."""
    if cfg.bag_color and PART_COLORS.get(part) == cfg.bag_color:
        return BAG_URL
    return product_url(part)


PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def notify(cfg: Config, title: str, message: str, *, priority: str = "urgent",
           tags: str = "iphone,rotating_light", click: str | None = None) -> None:
    # JSON publishing (POST to the server root) rather than headers, because
    # HTTP headers are latin-1 and choke on emoji / non-ASCII titles.
    body = {
        "topic": cfg.ntfy_topic,
        "title": title,
        "message": message,
        "priority": PRIORITY[priority],
        "tags": [t for t in tags.split(",") if t],
    }
    if click:
        body["click"] = click
    req = urllib.request.Request(
        cfg.ntfy_server,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # a failed push should never kill the watcher
        log(f"ntfy push failed: {e}")


def alert_for(cfg: Config, hit: Hit) -> None:
    label = cfg.parts[hit.part]
    notify(
        cfg,
        title=f"🚨 {label} at {hit.store_name}",
        message=f"{hit.quote}\nTap to open the Apple Store page and reserve pickup.",
        click=click_url(cfg, hit.part),
    )


def sold_out_alert(cfg: Config, r: Restock) -> None:
    # The "hourglass" tag makes ntfy prefix the title with ⌛ itself.
    notify(
        cfg,
        title=f"Sold out: {r.label} at {r.store_name}",
        message=f"In stock {format_duration(r.duration)} ({r.window()}).\n"
                f"Accurate to about ±{cfg.poll_seconds}s (one poll interval).",
        priority="low",
        tags="hourglass",
    )


# --------------------------------------------------------------------------- #
# Restock tracking
# --------------------------------------------------------------------------- #

def _now() -> tuple[float, datetime]:
    """Monotonic seconds (for durations) and UTC wall clock (for display)."""
    return time.monotonic(), datetime.now(timezone.utc)


@dataclass
class Restock:
    """One in-stock streak of a (store, part) pair, as seen by our checks.

    Times are when a check *noticed* the change, so each end lags the real
    event by up to one poll interval and the duration is approximate.
    """
    store_id: str
    store_name: str
    part: str
    label: str
    first_mono: float
    first_utc: datetime
    sold_mono: float | None = None
    sold_utc: datetime | None = None

    @property
    def duration(self) -> float:
        return (self.sold_mono or 0.0) - self.first_mono

    def window(self) -> str:
        end = f"{self.sold_utc:%H:%M:%S}" if self.sold_utc else "now"
        return f"in stock {self.first_utc:%H:%M:%S}–{end} UTC"


@dataclass
class RestockTracker:
    # (store_id, part) -> streak that's in stock right now. A pair drops out
    # when it sells out, so if it comes back later you get a fresh alert.
    open: dict[tuple[str, str], Restock] = field(default_factory=dict)
    # Every streak seen this run (open and closed), for the run summary.
    history: list[Restock] = field(default_factory=list)


def format_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 60:
        return f"~{s}s"
    if s < 3600:
        return f"~{s // 60}m {s % 60:02d}s"
    return f"~{s // 3600}h {s % 3600 // 60:02d}m"


def write_step_summary(cfg: Config, tracker: RestockTracker) -> None:
    """Append a markdown table of this run's restocks to the Actions job summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["### duo-watch run summary", ""]
    if not tracker.history:
        lines.append("No restocks this run.")
    else:
        now_mono, _ = _now()
        lines += [
            "| Store | Model | First seen (UTC) | Sold out (UTC) | Approx. duration |",
            "|---|---|---|---|---|",
        ]
        for r in tracker.history:
            if r.sold_utc is None:
                sold, dur = "still in stock", f"> {format_duration(now_mono - r.first_mono)}"
            else:
                sold, dur = f"{r.sold_utc:%Y-%m-%d %H:%M:%S}", format_duration(r.duration)
            lines.append(f"| {r.store_name} | {r.label} | {r.first_utc:%Y-%m-%d %H:%M:%S} | {sold} | {dur} |")
        lines += ["", f"_Durations are accurate to about one poll interval (~{cfg.poll_seconds}s)._"]
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except OSError as e:  # a summary problem should never kill the watcher
        log(f"couldn't write step summary: {e}")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def check_once(cfg: Config, tracker: RestockTracker) -> list[Hit]:
    payload = fetch_availability(list(cfg.parts))
    hits, seen = parse_hits(payload, cfg.parts, cfg.watch_stores)
    missing = cfg.watch_stores - seen
    if missing:
        log(f"note: response didn't include {', '.join(sorted(missing))}")

    mono, utc = _now()
    current = {(h.store_id, h.part) for h in hits}

    # Sold out = the store reported back and the pair isn't available any more.
    # A store missing from the response tells us nothing, so its pairs stay open.
    for key in sorted(tracker.open):
        if key in current or key[0] not in seen:
            continue
        r = tracker.open.pop(key)
        r.sold_mono, r.sold_utc = mono, utc
        log(f"SOLD OUT: {r.label} @ {r.store_name} after {format_duration(r.duration)} ({r.window()})")
        sold_out_alert(cfg, r)

    new_hits = [h for h in hits if (h.store_id, h.part) not in tracker.open]
    for h in new_hits:
        log(f"IN STOCK: {cfg.parts[h.part]} @ {h.store_name} ({h.quote})")
        alert_for(cfg, h)
        r = Restock(h.store_id, h.store_name, h.part, cfg.parts[h.part], mono, utc)
        tracker.open[(h.store_id, h.part)] = r
        tracker.history.append(r)

    if not hits:
        log(f"no stock ({len(seen & cfg.watch_stores)}/{len(cfg.watch_stores)} watched stores reported)")
    return new_hits


def run(cfg: Config) -> None:
    log(f"watching {', '.join(cfg.parts.values())}")
    log(f"stores: {', '.join(LOWER_MAINLAND_STORES.get(s, s) for s in sorted(cfg.watch_stores))}")
    deadline = time.monotonic() + cfg.run_minutes * 60 if cfg.run_minutes > 0 else None

    tracker = RestockTracker()
    failures = 0
    warned_blocked = False

    while deadline is None or time.monotonic() < deadline:
        try:
            check_once(cfg, tracker)
            if warned_blocked:
                notify(cfg, "✅ Duo watcher recovered", "Apple is answering again; checks resumed.",
                       priority="default", tags="white_check_mark")
            failures, warned_blocked = 0, False
            sleep_for = cfg.poll_seconds + random.uniform(0, 5)
        except BlockedError as e:
            failures += 1
            if e.rate_limited:
                # Poking Apple while it's rate-limiting us only stretches the
                # block, so cool down 5, 10, 20, then 30 minutes.
                sleep_for = min(1800, 300 * 2 ** (failures - 1)) + random.uniform(0, 60)
            else:
                # Back off: 2, 4, 8 ... up to 10 minutes.
                sleep_for = min(600, cfg.poll_seconds * 2 ** min(failures, 3)) + random.uniform(0, 30)
            why = " (rate-limited)" if e.rate_limited else ""
            log(f"check failed ({failures} in a row): {e}{why}; retrying in {format_duration(sleep_for)}")
            if failures == 5 and not warned_blocked:
                notify(cfg, "⚠️ Duo watcher can't reach Apple",
                       f"Last error: {e}. Checks will keep retrying, but you may be missing stock.",
                       priority="high", tags="warning")
                warned_blocked = True

        if deadline is not None:
            sleep_for = min(sleep_for, max(0, deadline - time.monotonic()))
        time.sleep(sleep_for)

    write_step_summary(cfg, tracker)
    log("run window finished, exiting")


def main() -> None:
    ap = argparse.ArgumentParser(description="iPhone Duo pickup-stock watcher (Apple Canada)")
    ap.add_argument("--once", action="store_true", help="check one time, print results, exit")
    ap.add_argument("--test-notify", action="store_true", help="send a test push to your ntfy topic and exit")
    args = ap.parse_args()

    cfg = Config.from_env()

    if args.test_notify:
        notify(cfg, "👋 Duo watcher test", "If you can read this, alerts are wired up.",
               priority="default", tags="tada", click="https://www.apple.com/ca/iphone/")
        log("test notification sent")
        return

    if args.once:
        try:
            hits = check_once(cfg, RestockTracker())
        except BlockedError as e:
            sys.exit(f"check failed: {e}")
        if not hits:
            log("nothing in stock right now")
        return

    run(cfg)


if __name__ == "__main__":
    main()
