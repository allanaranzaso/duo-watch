import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import duo_watch as dw  # noqa: E402


def store(sid, name, parts):
    return {"storeNumber": sid, "storeName": name, "partsAvailability": parts}


def payload(*stores):
    return {"body": {"content": {"pickupMessage": {"stores": list(stores)}}}}


AVAIL = {"pickupDisplay": "available", "pickupSearchQuote": "<span>Available Today</span>"}
UNAVAIL = {"pickupDisplay": "unavailable", "pickupSearchQuote": "Currently unavailable"}
INELIGIBLE = {"pickupDisplay": "ineligible", "pickupSearchQuote": "Not available for pickup"}

PARTS_256 = {"MK244VC/A": "iPhone Duo 256GB Star White", "MK254VC/A": "iPhone Duo 256GB Night Sky"}
WATCH = set(dw.LOWER_MAINLAND_STORES)


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

    def test_bad_shape_raises_blocked(self):
        with self.assertRaises(dw.BlockedError):
            dw.parse_hits({"head": {"status": "503"}}, PARTS_256, WATCH)


class UrlTests(unittest.TestCase):
    def test_build_url(self):
        url = dw.build_url(["MK244VC/A", "MK254VC/A"])
        self.assertTrue(url.startswith("https://www.apple.com/ca/shop/fulfillment-messages?"))
        self.assertIn("parts.0=MK244VC%2FA", url)
        self.assertIn("parts.1=MK254VC%2FA", url)
        self.assertIn("store=R460", url)
        self.assertIn("searchNearby=true", url)


class DedupeTests(unittest.TestCase):
    """Alert once per in-stock streak; alert again if it sells out and comes back."""

    def setUp(self):
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "t", "DUO_STORAGE": "256GB"}, clear=True):
            self.cfg = dw.Config.from_env()

    def run_sequence(self, payloads):
        sent = []
        alerted = set()
        with mock.patch.object(dw, "fetch_availability", side_effect=payloads), \
             mock.patch.object(dw, "alert_for", side_effect=lambda cfg, h: sent.append(h.store_id)):
            for _ in payloads:
                dw.check_once(self.cfg, alerted)
        return sent

    def test_sequence(self):
        in_stock = payload(store("R460", "Metrotown", {"MK244VC/A": AVAIL}))
        out = payload(store("R460", "Metrotown", {"MK244VC/A": UNAVAIL}))
        sent = self.run_sequence([out, in_stock, in_stock, in_stock, out, in_stock])
        self.assertEqual(sent, ["R460", "R460"])  # first appearance + restock only


if __name__ == "__main__":
    unittest.main(verbosity=2)
