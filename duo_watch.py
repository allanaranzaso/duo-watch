#!/usr/bin/env python3
"""
duo-watch: polls Apple Canada's pickup-availability endpoint for the iPhone Duo
and sends an ntfy push the moment a watched Lower Mainland store shows stock.

Stdlib only (no pip install needed), so it runs anywhere Python 3.10+ does:
GitHub Actions, your Mac, a Raspberry Pi, a VPS.

Config is all environment variables (see README.md):
  DUO_STORAGE    required  e.g. "256GB" or "512GB,1TB"
  DUO_COLORS     optional  "Star White,Night Sky" (default: both)
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
from dataclasses import dataclass
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

# Apple's own store list (apple.com/autopush/ca/retail/storelist/stores.xml).
LOWER_MAINLAND_STORES: dict[str, str] = {
    "R460": "Metrotown",
    "R280": "Pacific Centre",
    "R421": "Richmond Centre",
    "R488": "Coquitlam Centre",
    "R601": "Guildford Town Centre",
}

ENDPOINT = "https://www.apple.com/ca/shop/fulfillment-messages"

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
    "Referer": "https://www.apple.com/ca/shop/buy-iphone",
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
        )


def _norm_storage(s: str) -> str:
    s = s.strip().upper().replace(" ", "")
    return {"256": "256GB", "512": "512GB", "1T": "1TB", "2T": "2TB", "1000GB": "1TB", "2000GB": "2TB"}.get(s, s)


# --------------------------------------------------------------------------- #
# Apple endpoint
# --------------------------------------------------------------------------- #

class BlockedError(Exception):
    """Apple returned something other than usable JSON (rate limit / bot block)."""


def build_url(parts: list[str], anchor_store: str = ANCHOR_STORE) -> str:
    params: list[tuple[str, str]] = [
        ("fae", "true"),
        ("pl", "true"),
        ("mts.0", "regular"),
        ("mts.1", "compact"),
        ("searchNearby", "true"),
        ("store", anchor_store),
    ]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    return f"{ENDPOINT}?{urllib.parse.urlencode(params)}"


def fetch_availability(parts: list[str], timeout: int = 20) -> dict:
    req = urllib.request.Request(build_url(parts), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise BlockedError(f"HTTP {e.code}") from e
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
    try:
        stores = payload["body"]["content"]["pickupMessage"]["stores"]
    except (KeyError, TypeError) as e:
        raise BlockedError("unexpected JSON shape (no pickupMessage.stores)") from e

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

def product_url(part: str) -> str:
    return f"https://www.apple.com/ca/shop/product/{part}"


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
        click=product_url(hit.part),
    )


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def check_once(cfg: Config, already_alerted: set[tuple[str, str]]) -> list[Hit]:
    payload = fetch_availability(list(cfg.parts))
    hits, seen = parse_hits(payload, cfg.parts, cfg.watch_stores)
    missing = cfg.watch_stores - seen
    if missing:
        log(f"note: response didn't include {', '.join(sorted(missing))}")

    current = {(h.store_id, h.part) for h in hits}
    new_hits = [h for h in hits if (h.store_id, h.part) not in already_alerted]
    for h in new_hits:
        log(f"IN STOCK: {cfg.parts[h.part]} @ {h.store_name} ({h.quote})")
        alert_for(cfg, h)

    # Track only what's in stock right now: anything that sells out drops off,
    # so if it comes back later you get a fresh alert.
    already_alerted.clear()
    already_alerted.update(current)
    if not hits:
        log(f"no stock ({len(seen & cfg.watch_stores)}/{len(cfg.watch_stores)} watched stores reported)")
    return new_hits


def run(cfg: Config) -> None:
    log(f"watching {', '.join(cfg.parts.values())}")
    log(f"stores: {', '.join(LOWER_MAINLAND_STORES.get(s, s) for s in sorted(cfg.watch_stores))}")
    deadline = time.monotonic() + cfg.run_minutes * 60 if cfg.run_minutes > 0 else None

    alerted: set[tuple[str, str]] = set()
    failures = 0
    warned_blocked = False

    while deadline is None or time.monotonic() < deadline:
        try:
            check_once(cfg, alerted)
            if warned_blocked:
                notify(cfg, "✅ Duo watcher recovered", "Apple is answering again; checks resumed.",
                       priority="default", tags="white_check_mark")
            failures, warned_blocked = 0, False
            sleep_for = cfg.poll_seconds + random.uniform(0, 20)
        except BlockedError as e:
            failures += 1
            log(f"check failed ({failures} in a row): {e}")
            if failures == 5 and not warned_blocked:
                notify(cfg, "⚠️ Duo watcher can't reach Apple",
                       f"Last error: {e}. Checks will keep retrying, but you may be missing stock.",
                       priority="high", tags="warning")
                warned_blocked = True
            # Back off: 2, 4, 8 ... up to 10 minutes.
            sleep_for = min(600, cfg.poll_seconds * 2 ** min(failures, 3)) + random.uniform(0, 30)

        if deadline is not None:
            sleep_for = min(sleep_for, max(0, deadline - time.monotonic()))
        time.sleep(sleep_for)

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
            hits = check_once(cfg, set())
        except BlockedError as e:
            sys.exit(f"check failed: {e}")
        if not hits:
            log("nothing in stock right now")
        return

    run(cfg)


if __name__ == "__main__":
    main()
