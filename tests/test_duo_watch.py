import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import duo_watch as dw  # noqa: E402


def store(sid, name, parts):
    return {"storeNumber": sid, "storeName": name, "partsAvailability": parts}


def payload(*stores):
    """Current /shop/retail/pickup-message shape (verified Oct 27 2026)."""
    return {"body": {"stores": list(stores), "storesCount": len(stores)}}


def old_payload(*stores):
    """Retired /shop/fulfillment-messages shape."""
    return {"body": {"content": {"pickupMessage": {"stores": list(stores)}}}}


AVAIL = {"pickupDisplay": "available", "pickupSearchQuote": "<span>Available Today</span>"}
UNAVAIL = {"pickupDisplay": "unavailable", "pickupSearchQuote": "Currently unavailable"}
INELIGIBLE = {"pickupDisplay": "ineligible", "pickupSearchQuote": "Not available for pickup"}

PARTS_256 = {"MK244VC/A": "iPhone Duo 256GB Star White", "MK254VC/A": "iPhone Duo 256GB Night Sky"}
WATCH = set(dw.LOWER_MAINLAND_STORES)

IN = payload(store("R460", "Metrotown", {"MK244VC/A": AVAIL}))
OUT = payload(store("R460", "Metrotown", {"MK244VC/A": UNAVAIL}))
BASE_UTC = datetime(2026, 10, 27, 14, 2, 11, tzinfo=timezone.utc)


def make_config():
    with mock.patch.dict(os.environ, {"NTFY_TOPIC": "t", "DUO_STORAGE": "256GB"}, clear=True):
        return dw.Config.from_env()


class FakeClock:
    """Stands in for both clocks: monotonic seconds t, and UTC = BASE_UTC + t."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def now(self):
        return self.t, BASE_UTC + timedelta(seconds=self.t)

    def sleep(self, seconds):
        self.t += seconds


class ConfigTests(unittest.TestCase):
    def env(self, **kw):
        base = {"NTFY_TOPIC": "t"}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_storage_filter_both_colors(self):
        with self.env(DUO_STORAGE="256GB"):
            cfg = dw.Config.from_env()
        self.assertEqual(set(cfg.parts), {"MK244VC/A", "MK254VC/A"})
        self.assertEqual(cfg.watch_stores, WATCH)

    def test_storage_aliases_and_color(self):
        with self.env(DUO_STORAGE="512, 1t", DUO_COLORS="night sky"):
            cfg = dw.Config.from_env()
        self.assertEqual(set(cfg.parts), {"MK274VC/A", "MK294VC/A"})

    def test_missing_storage_exits(self):
        with self.env(), self.assertRaises(SystemExit):
            dw.Config.from_env()

    def test_bad_storage_exits(self):
        with self.env(DUO_STORAGE="128GB"), self.assertRaises(SystemExit):
            dw.Config.from_env()

    def test_poll_floor(self):
        with self.env(DUO_STORAGE="256GB", POLL_SECONDS="5"):
            self.assertEqual(dw.Config.from_env().poll_seconds, 30)


class BagUrlTests(unittest.TestCase):
    """Alerts for DUO_BAG_COLOR open the bag; everything else opens the product page."""

    def env(self, **kw):
        base = {"NTFY_TOPIC": "t", "DUO_STORAGE": "256GB"}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def click_for(self, cfg, part):
        with mock.patch.object(dw, "notify") as notify:
            dw.alert_for(cfg, dw.Hit("R460", "Metrotown", part, "Available Today"))
        return notify.call_args.kwargs["click"]

    def test_matching_colour_opens_bag(self):
        with self.env(DUO_BAG_COLOR="night sky"):
            cfg = dw.Config.from_env()
        self.assertEqual(cfg.bag_color, "Night Sky")
        self.assertEqual(self.click_for(cfg, "MK254VC/A"), dw.BAG_URL)

    def test_other_colour_opens_product_page(self):
        with self.env(DUO_BAG_COLOR="Night Sky"):
            cfg = dw.Config.from_env()
        self.assertEqual(self.click_for(cfg, "MK244VC/A"), dw.product_url("MK244VC/A"))

    def test_unset_opens_product_page_for_both(self):
        with self.env():
            cfg = dw.Config.from_env()
        self.assertIsNone(cfg.bag_color)
        for part in ("MK244VC/A", "MK254VC/A"):
            self.assertEqual(self.click_for(cfg, part), dw.product_url(part))

    def test_bag_colour_not_watched_exits(self):
        with self.env(DUO_COLORS="Star White", DUO_BAG_COLOR="Night Sky"), \
             self.assertRaises(SystemExit) as cm:
            dw.Config.from_env()
        self.assertIn("DUO_BAG_COLOR", str(cm.exception.code))

    def test_unknown_bag_colour_exits(self):
        with self.env(DUO_BAG_COLOR="Gold"), self.assertRaises(SystemExit) as cm:
            dw.Config.from_env()
        self.assertIn("DUO_BAG_COLOR", str(cm.exception.code))


class ParseTests(unittest.TestCase):
    def test_available_in_watched_store(self):
        p = payload(
            store("R460", "Metrotown", {"MK244VC/A": AVAIL, "MK254VC/A": UNAVAIL}),
            store("R280", "Pacific Centre", {"MK244VC/A": UNAVAIL, "MK254VC/A": UNAVAIL}),
        )
        hits, seen = dw.parse_hits(p, PARTS_256, WATCH)
        self.assertEqual(hits, [dw.Hit("R460", "Metrotown", "MK244VC/A", "Available Today")])
        self.assertEqual(seen, {"R460", "R280"})

    def test_ignores_unwatched_store_and_unwatched_part(self):
        p = payload(
            store("R209", "Chinook Centre", {"MK244VC/A": AVAIL}),
            store("R460", "Metrotown", {"MK2C4VC/A": AVAIL}),  # 2TB, not watched
        )
        hits, _ = dw.parse_hits(p, PARTS_256, WATCH)
        self.assertEqual(hits, [])

    def test_ineligible_is_not_stock(self):
        p = payload(store("R460", "Metrotown", {"MK244VC/A": INELIGIBLE}))
        self.assertEqual(dw.parse_hits(p, PARTS_256, WATCH)[0], [])

    def test_legacy_quote_shape(self):
        p = payload(
            store("R421", "Richmond Centre", {"MK244VC/A": {"storePickupQuote": "Today at Apple Richmond Centre"}}),
            store("R488", "Coquitlam Centre", {"MK244VC/A": {"storePickupQuote": "Currently unavailable at Apple Coquitlam"}}),
        )
        hits, _ = dw.parse_hits(p, PARTS_256, WATCH)
        self.assertEqual([h.store_id for h in hits], ["R421"])

    def test_old_response_shape_still_parses(self):
        p = old_payload(store("R280", "Pacific Centre", {"MK254VC/A": AVAIL}))
        hits, _ = dw.parse_hits(p, PARTS_256, WATCH)
        self.assertEqual([(h.store_id, h.part) for h in hits], [("R280", "MK254VC/A")])

    def test_bad_shape_raises_blocked(self):
        with self.assertRaises(dw.BlockedError):
            dw.parse_hits({"head": {"status": "503"}}, PARTS_256, WATCH)


class UrlTests(unittest.TestCase):
    def test_build_url(self):
        url = dw.build_url(["MK244VC/A", "MK254VC/A"])
        self.assertTrue(url.startswith("https://www.apple.com/ca/shop/retail/pickup-message?"))
        self.assertNotIn("fulfillment-messages", url)
        self.assertIn("parts.0=MK244VC%2FA", url)
        self.assertIn("parts.1=MK254VC%2FA", url)
        self.assertIn("store=R460", url)
        self.assertIn("searchNearby=true", url)


class DedupeTests(unittest.TestCase):
    """Alert once per in-stock streak; alert again if it sells out and comes back."""

    def setUp(self):
        self.cfg = make_config()

    def run_sequence(self, payloads):
        sent = []
        tracker = dw.RestockTracker()
        with mock.patch.object(dw, "fetch_availability", side_effect=payloads), \
             mock.patch.object(dw, "alert_for", side_effect=lambda cfg, h: sent.append(h.store_id)), \
             mock.patch.object(dw, "sold_out_alert"), \
             mock.patch.object(dw, "_now", FakeClock().now):
            for _ in payloads:
                dw.check_once(self.cfg, tracker)
        return sent

    def test_sequence(self):
        sent = self.run_sequence([OUT, IN, IN, IN, OUT, IN])
        self.assertEqual(sent, ["R460", "R460"])  # first appearance + restock only


class SellOutTests(unittest.TestCase):
    """Notice when an in-stock pair disappears, and how long it lasted."""

    STEP = 77  # seconds between checks

    def setUp(self):
        self.cfg = make_config()

    def run_sequence(self, results):
        """results: payloads, or exceptions for fetch_availability to raise."""
        clock = FakeClock()
        tracker = dw.RestockTracker()
        sold = []
        with mock.patch.object(dw, "fetch_availability", side_effect=results), \
             mock.patch.object(dw, "alert_for"), \
             mock.patch.object(dw, "sold_out_alert", side_effect=lambda cfg, r: sold.append(r)), \
             mock.patch.object(dw, "_now", clock.now):
            for _ in results:
                try:
                    dw.check_once(self.cfg, tracker)
                except dw.BlockedError:
                    pass
                clock.sleep(self.STEP)
        return tracker, sold

    def test_sell_out_records_times_and_duration(self):
        tracker, sold = self.run_sequence([IN, IN, OUT])
        self.assertEqual(len(sold), 1)
        r = sold[0]
        self.assertEqual((r.store_id, r.part, r.label), ("R460", "MK244VC/A", "iPhone Duo 256GB Star White"))
        self.assertEqual(r.duration, 2 * self.STEP)
        self.assertEqual(r.first_utc, BASE_UTC)
        self.assertEqual(r.sold_utc, BASE_UTC + timedelta(seconds=2 * self.STEP))
        self.assertEqual(tracker.open, {})
        self.assertEqual(tracker.history, [r])

    def test_each_restock_is_tracked_separately(self):
        tracker, sold = self.run_sequence([IN, OUT, IN, OUT])
        self.assertEqual(len(tracker.history), 2)
        self.assertEqual(sold, tracker.history)
        self.assertEqual([r.duration for r in sold], [self.STEP, self.STEP])
        self.assertEqual(sold[1].first_utc, BASE_UTC + timedelta(seconds=2 * self.STEP))

    def test_failed_check_is_not_a_sell_out(self):
        tracker, sold = self.run_sequence([IN, dw.BlockedError("HTTP 503"), OUT])
        self.assertEqual(len(sold), 1)
        self.assertEqual(sold[0].duration, 2 * self.STEP)  # spans the failed check

    def test_store_missing_from_response_stays_open(self):
        other_store_only = payload(store("R280", "Pacific Centre", {"MK244VC/A": UNAVAIL}))
        tracker, sold = self.run_sequence([IN, other_store_only])
        self.assertEqual(sold, [])
        self.assertIn(("R460", "MK244VC/A"), tracker.open)

    def test_format_duration(self):
        self.assertEqual(dw.format_duration(47), "~47s")
        self.assertEqual(dw.format_duration(192), "~3m 12s")
        self.assertEqual(dw.format_duration(3900), "~1h 05m")

    def test_sold_out_push_is_low_priority_without_click(self):
        r = dw.Restock("R460", "Metrotown", "MK244VC/A", "iPhone Duo 256GB Star White",
                       0.0, BASE_UTC, 47.0, BASE_UTC + timedelta(seconds=47))
        with mock.patch.object(dw, "notify") as notify:
            dw.sold_out_alert(self.cfg, r)
        _, kwargs = notify.call_args
        self.assertEqual(kwargs["priority"], "low")
        self.assertEqual(kwargs["tags"], "hourglass")
        self.assertIsNone(kwargs.get("click"))
        self.assertIn("Metrotown", kwargs["title"])
        self.assertIn("~47s", kwargs["message"])
        self.assertIn("14:02:11–14:02:58 UTC", kwargs["message"])


class StepSummaryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "summary.md")

    def write(self, tracker, env=None, now_t=600.0):
        clock = FakeClock()
        clock.t = now_t
        env = {"GITHUB_STEP_SUMMARY": self.path} if env is None else env
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(dw, "_now", clock.now):
            dw.write_step_summary(self.cfg, tracker)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_table_with_closed_and_open_restocks(self):
        tracker = dw.RestockTracker(history=[
            dw.Restock("R460", "Metrotown", "MK244VC/A", "iPhone Duo 256GB Star White",
                       0.0, BASE_UTC, 47.0, BASE_UTC + timedelta(seconds=47)),
            dw.Restock("R280", "Pacific Centre", "MK254VC/A", "iPhone Duo 256GB Night Sky",
                       300.0, BASE_UTC + timedelta(seconds=300)),
        ])
        self.write(tracker)
        out = self.read()
        self.assertIn("| Store | Model | First seen (UTC) | Sold out (UTC) | Approx. duration |", out)
        self.assertIn("| Metrotown | iPhone Duo 256GB Star White | 2026-10-27 14:02:11 | 2026-10-27 14:02:58 | ~47s |", out)
        self.assertIn("| Pacific Centre | iPhone Duo 256GB Night Sky | 2026-10-27 14:07:11 | still in stock | > ~5m 00s |", out)

    def test_no_restocks(self):
        self.write(dw.RestockTracker())
        self.assertIn("No restocks this run.", self.read())
        self.assertNotIn("| Store |", self.read())

    def test_skipped_without_env_var(self):
        self.write(dw.RestockTracker(), env={})
        self.assertFalse(os.path.exists(self.path))

    def test_appends_to_existing_summary(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("earlier step\n")
        self.write(dw.RestockTracker())
        out = self.read()
        self.assertTrue(out.startswith("earlier step\n"))
        self.assertIn("No restocks this run.", out)


class RunLoopTests(unittest.TestCase):
    def test_run_writes_summary_at_deadline(self):
        cfg = make_config()
        cfg.run_minutes = 3  # 180s window at 75s polls: checks at t=0, 75, 150
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "summary.md")
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": path}), \
                 mock.patch.object(dw, "fetch_availability", side_effect=[IN, OUT, IN]), \
                 mock.patch.object(dw, "notify") as notify, \
                 mock.patch.object(dw.random, "uniform", return_value=0) as uniform, \
                 mock.patch.object(dw.time, "monotonic", clock.monotonic), \
                 mock.patch.object(dw.time, "sleep", clock.sleep), \
                 mock.patch.object(dw, "_now", clock.now):
                dw.run(cfg)
            with open(path, encoding="utf-8") as f:
                out = f.read()

        uniform.assert_called_with(0, 5)
        self.assertEqual(clock.t, 180)
        priorities = [c.kwargs.get("priority", "urgent") for c in notify.call_args_list]
        self.assertEqual(priorities, ["urgent", "low", "urgent"])  # in, sold out, in again
        self.assertIn("| Metrotown | iPhone Duo 256GB Star White | 2026-10-27 14:02:11 | 2026-10-27 14:03:26 | ~1m 15s |", out)
        self.assertIn("still in stock", out)


class RateLimitTests(unittest.TestCase):
    """Rate-limit responses get a long cooldown; other failures keep the short backoff."""

    def http_error(self, code):
        return urllib.error.HTTPError(dw.ENDPOINT, code, "error", {}, None)

    def test_541_is_rate_limited(self):
        with mock.patch.object(dw.urllib.request, "urlopen", side_effect=self.http_error(541)), \
             self.assertRaises(dw.BlockedError) as cm:
            dw.fetch_availability(["MK244VC/A"])
        self.assertTrue(cm.exception.rate_limited)
        self.assertEqual(str(cm.exception), "HTTP 541")

    def test_500_is_not_rate_limited(self):
        with mock.patch.object(dw.urllib.request, "urlopen", side_effect=self.http_error(500)), \
             self.assertRaises(dw.BlockedError) as cm:
            dw.fetch_availability(["MK244VC/A"])
        self.assertFalse(cm.exception.rate_limited)

    def sleeps_for(self, results, run_minutes):
        """Run the loop on a fake clock and return each sleep it asked for."""
        cfg = make_config()
        cfg.run_minutes = run_minutes
        clock = FakeClock()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock.sleep(seconds)

        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(dw, "fetch_availability", side_effect=results), \
             mock.patch.object(dw, "notify"), \
             mock.patch.object(dw.random, "uniform", return_value=0), \
             mock.patch.object(dw.time, "monotonic", clock.monotonic), \
             mock.patch.object(dw.time, "sleep", sleep), \
             mock.patch.object(dw, "_now", clock.now):
            dw.run(cfg)
        return sleeps

    def test_rate_limit_cools_down_then_resumes_polling(self):
        blocked = dw.BlockedError("HTTP 541", rate_limited=True)
        sleeps = self.sleeps_for([blocked, blocked] + [OUT] * 5, run_minutes=17)
        # 5 min, 10 min, then back to the normal 75s poll until the 1020s deadline.
        self.assertEqual(sleeps, [300, 600, 75, 45])

    def test_other_errors_keep_short_backoff(self):
        sleeps = self.sleeps_for([dw.BlockedError("network error")] + [OUT] * 5, run_minutes=4)
        self.assertEqual(sleeps, [150, 75, 15])  # poll_seconds * 2, then normal polling


if __name__ == "__main__":
    unittest.main(verbosity=2)
