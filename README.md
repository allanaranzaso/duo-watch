# duo-watch

Pings your iPhone the moment an iPhone Duo in your chosen storage shows up for
**in-store pickup** at a Lower Mainland Apple Store:

| Store ID | Store |
|---|---|
| R460 | Metrotown (Burnaby) |
| R280 | Pacific Centre (Vancouver) |
| R421 | Richmond Centre |
| R488 | Coquitlam Centre |
| R601 | Guildford Town Centre (Surrey) |

It checks every ~75–95 seconds and calls the same endpoint the "Check
availability" button on apple.com/ca uses. One request covers all five stores.

## How it works

```
GitHub Actions (cron every 2h)  →  duo_watch.py loops ~5h45m
        │                                │ every ~75s
        │                                ▼
        │                apple.com/ca/shop/retail/pickup-message
        │                                │ pickupDisplay == "available"?
        ▼                                ▼
 next run queued, starts         ntfy.sh  →  push on your iPhone
 when this one ends                         (tap → Apple product page)
```

- **One alert per restock.** You get pinged when stock first appears. If it
  sells out and comes back, you get pinged again. No spam while it sits there.
- **It tells you when it's blind.** After 5 failed checks in a row (blocked,
  network, Apple changed the format) you get a ⚠️ push. You get a ✅ when it recovers.
- **Stdlib only.** No dependencies. Runs on Python 3.10+ anywhere.

## Setup (about 10 minutes)

### 1. ntfy on your phone
1. Install **ntfy** from the App Store.
2. Tap **+** and subscribe to a topic that's hard to guess, like
   `duo-allan-7f3k9q`. Anyone who knows a topic name on ntfy.sh can read it,
   so don't use something like `iphone-duo`.

### 2. GitHub repo
1. Create a **public** repo. GitHub Actions is free and unlimited on public
   repos. A private repo would burn through the free minutes in a few days.
   Nothing secret lives in the code.
2. Push this folder's contents to it (including `.github/workflows/watch.yml`).

### 3. Settings → Secrets and variables → Actions
| Kind | Name | Value |
|---|---|---|
| **Secret** | `NTFY_TOPIC` | your topic, e.g. `duo-allan-7f3k9q` |
| **Variable** | `DUO_STORAGE` | `256GB`, `512GB`, `1TB` or `2TB` (comma-separate to watch several) |
| Variable (optional) | `DUO_COLORS` | `Star White` or `Night Sky` (default: both) |
| Variable (optional) | `WATCH_STORES` | e.g. `R460,R280` to narrow the stores (default: all 5) |
| Variable (optional) | `POLL_SECONDS` | default `75` (minimum 30) |

### 4. Smoke test (Actions tab → *iPhone Duo stock watch* → Run workflow)
1. Mode **test-notify**. You should get a 👋 push within seconds.
2. Mode **once**. Open the run log. You want to see
   `no stock (5/5 watched stores reported)`, which means Apple is answering
   GitHub's servers. If you see `check failed`, read "If Apple blocks
   GitHub" below.
3. Mode **watch**, or wait for the next cron tick. From then on it runs
   around the clock.

### 5. When you've got your phone
Actions tab → the workflow → **⋯ → Disable workflow**.

## Good to know

- **It only sees online-reservable pickup stock.** Units a store sells
  walk-in only, without opening them up for pickup reservations, won't show up.
  Since launch, Apple has been switching Duo pickup on and off. While it's off,
  the watcher stays quiet and starts alerting the moment Apple flips it on.
- **Part numbers** come from a third-party spec site (EveryMac). To
  double-check, pick your config on apple.com/ca. The `MK2…VC/A` code shows
  up in the URL or in the "Check availability" request in your browser's
  dev tools. Edit `PARTS` in `duo_watch.py` if one is off.
- **The endpoint is undocumented.** Apple can change or rate-limit it without
  notice. The ⚠️ push is there so you don't miss stock silently.
- **GitHub's terms:** Actions on GitHub-hosted runners isn't meant for
  activity unrelated to building, testing, or deploying the repo's software.
  Running a stock watcher around the clock is a grey area. It's low-stakes for
  a few weeks, but if you'd rather keep it clean, run it on your own machine
  (next section).

## Running it anywhere else (Mac, Raspberry Pi, VPS)

```bash
export DUO_STORAGE=512GB NTFY_TOPIC=duo-allan-7f3k9q
python3 duo_watch.py --test-notify   # check alerts work
python3 duo_watch.py --once          # one check, prints results
python3 duo_watch.py                 # loop forever (Ctrl-C to stop)
```

On a Mac, add `caffeinate -i` in front of the last command so the machine
doesn't sleep while it runs.

### If Apple blocks GitHub
Apple sometimes blocks datacenter IP addresses. If `--once` fails on Actions
but works on your laptop, that's the cause. Run it from home instead (any
always-on machine). The script doesn't change.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
