#!/usr/bin/env python3
"""
TASI automation: daily price data + background signal and price alerts.

Two modes (run by the GitHub Actions workflow, or by hand):

  python tasi_daily.py daily    Download prices for all stocks in tadawul_stocks.csv,
                                save data/tasi_data.json (the web page reads it for
                                near-instant scans), find signals and send alerts for
                                the NEW ones.
  python tasi_daily.py alerts   Check the price alerts in alerts.json against live
                                prices and notify when a price passes your level.

Options:  --force      run the alerts check even outside market hours
          --limit N    only the first N stocks (quick test)
          --dry-run    print notifications instead of sending them

Notification channels are read from environment variables (GitHub Secrets):
  NTFY_TOPIC, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK
Only the standard library is used, so nothing needs to be installed.
"""
import argparse
import csv
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("TASI_ROOT", Path(__file__).resolve().parent))
DATA_DIR = ROOT / "data"
DATA_FILE = DATA_DIR / "tasi_data.json"
SIGNALS_FILE = DATA_DIR / "signals_latest.json"
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.json"
ALERTS_FILE = ROOT / "alerts.json"
STOCKS_FILE = ROOT / "tadawul_stocks.csv"

KEEP = 420  # trading days of detail stored per stock (about 1.7 years)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

DEFAULT_CFG = {
    "include_reits": False,
    "lookback_days": 3,
    "signals": {
        "ma": [20, 50, 200],
        "rsi": {"enabled": True, "period": 14, "level": 50},
        "fib": [50, 61.8],
        "fib_window": 120,
        "fib_min_swing_pct": 10,
        "low52": True,
        "atl": True,
        "below": False,
    },
    "notify": {
        "only_new": True,
        "notify_on_first_run": False,
        "min_volume_x": 0,
        "max_lines": 15,
        "watchlist_only": False,
        "watchlist": [],
    },
}


# ----------------------------------------------------------------- helpers
def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_config():
    return deep_merge(DEFAULT_CFG, load_json(CONFIG_FILE, {}))


def load_stocks(cfg):
    stocks = {}
    with open(STOCKS_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["Sector"] == "REITs" and not cfg["include_reits"]:
                continue
            stocks[r["Code"]] = {"name": r["Company"].title(), "sector": r["Sector"]}
    return stocks


def err_text(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return f"{type(e).__name__}: {e}"[:120]


def http_get_json(url, retries=2, timeout=12):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
        if attempt < retries - 1:
            time.sleep(1 + attempt + random.random())
    raise last


def day_int(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.year * 10000 + d.month * 100 + d.day


def day_str(d):
    return f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}"


# --------------------------------------------------------------- downloads
def fetch_history(code):
    last = None
    for host, tries in (("query1", 2), ("query2", 1)):
        try:
            j = http_get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{code}.SR?range=max&interval=1d", retries=tries)
            return parse_history(j)
        except Exception as e:  # try the other host
            last = e
    raise last


def parse_history(j):
    res = j["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        lo = q["low"][i] if q.get("low") and q["low"][i] is not None else c
        hi = q["high"][i] if q.get("high") and q["high"][i] is not None else c
        v = q["volume"][i] if q.get("volume") and q["volume"][i] is not None else None
        bars.append((day_int(t), round(c, 3), round(lo, 3), round(hi, 3), v))
    if not bars:
        raise ValueError("no bars")
    n = len(bars)
    off = max(0, n - KEEP)
    keep = bars[off:]
    return {
        "d": [b[0] for b in keep], "c": [b[1] for b in keep], "l": [b[2] for b in keep],
        "h": [b[3] for b in keep], "v": [b[4] for b in keep],
        "off": off, "olderMin": min(b[2] for b in bars[:off]) if off else None, "since": bars[0][0],
    }


def download_all(stocks, previous, workers=4):
    """Download every stock. Gives up early when Yahoo refuses us, and never runs longer than the time budget."""
    out, failed = {}, []
    deadline = time.time() + float(os.environ.get("TASI_BUDGET_MIN", "12")) * 60
    lock = threading.Lock()
    st = {"ok": 0, "bad": 0, "abort": False, "first_err": None}
    total = len(stocks)

    def job(code):
        if st["abort"]:
            return code, None, RuntimeError("skipped: Yahoo is refusing requests")
        if time.time() > deadline:
            return code, None, TimeoutError("time budget used up")
        time.sleep(random.random() * 0.3)
        rec = err = None
        try:
            rec = fetch_history(code)
        except Exception as e:
            err = e
        with lock:
            if rec:
                st["ok"] += 1
            else:
                st["bad"] += 1
                if st["first_err"] is None:
                    st["first_err"] = err_text(err)
                if st["ok"] == 0 and st["bad"] >= 10:
                    st["abort"] = True
            n = st["ok"] + st["bad"]
            if n % 20 == 0 or n == total:
                print(f"  progress {n}/{total} (ok {st['ok']}, failed {st['bad']})", flush=True)
        return code, rec, err

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, rec, err in ex.map(job, list(stocks)):
            if rec:
                out[code] = rec
            else:
                failed.append(code)
                if code in previous:          # keep the older copy rather than lose the stock
                    out[code] = dict(previous[code], stale=True)
    return out, failed, st


# ----------------------------------------------------------------- signals
def sma(c, n, i):
    return None if i < n - 1 else sum(c[i - n + 1:i + 1]) / n


def rsi_series(c, p):
    out = [None] * len(c)
    if len(c) <= p:
        return out
    g = l = 0.0
    for i in range(1, p + 1):
        d = c[i] - c[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    g /= p
    l /= p
    out[p] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(p + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (p - 1) + (d if d > 0 else 0)) / p
        l = (l * (p - 1) + (-d if d < 0 else 0)) / p
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def rel_vol(S, i):
    if i < 21:
        return None
    vals = [x for x in S["v"][i - 20:i] if x is not None]
    cur = S["v"][i]
    if not vals or cur is None or sum(vals) <= 0:
        return None
    return cur / (sum(vals) / len(vals))


def analyse(code, S, sg, lb):
    """Python port of the web page's signal logic."""
    hits = []
    c, lo, hi, d = S["c"], S["l"], S["h"], S["d"]
    last = len(c) - 1
    since = day_str(S["since"])

    def add(sig, direction, i, **extra):
        hits.append(dict(code=code, sig=sig, dir=direction, date=day_str(d[i]), rv=rel_vol(S, i), since=since, **extra))

    for n in sg["ma"]:
        if len(c) < n + lb + 1:
            continue
        for k in range(lb):
            i = last - k
            a, b = sma(c, n, i), sma(c, n, i - 1)
            if a is None or b is None:
                continue
            dt, dp = c[i] - a, c[i - 1] - b
            if dp <= 0 < dt:
                direction = "ABOVE"
            elif sg["below"] and dp >= 0 > dt:
                direction = "BELOW"
            else:
                continue
            cur = sma(c, n, last)
            add(f"MA{n}", direction, i, close=c[last], ref=cur, pct=(c[last] / cur - 1) * 100)
            break

    fibs, fn, fm = sg["fib"], sg["fib_window"], sg["fib_min_swing_pct"]
    if fibs and len(c) > fn + lb + 2:
        frm = last - fn + 1
        iH = iL = frm
        for j in range(frm, last + 1):
            if hi[j] > hi[iH]:
                iH = j
            if lo[j] < lo[iL]:
                iL = j
        H, L = hi[iH], lo[iL]
        rng = H - L
        if rng > 0 and rng / H * 100 >= fm:
            down = iL > iH
            for lv in fibs:
                level = L + lv / 100 * rng if down else H - lv / 100 * rng
                for k in range(lb):
                    i = last - k
                    if i <= (iL if down else iH):
                        break
                    prev, cur = c[i - 1], c[i]
                    direction = None
                    if down and prev <= level < cur:
                        direction = "ABOVE"
                    elif (not down) and sg["below"] and prev >= level > cur:
                        direction = "BELOW"
                    if direction:
                        add(f"FIB{lv:g}", direction, i, close=c[last], ref=level, pct=(c[last] / level - 1) * 100)
                        break

    rs = sg["rsi"]
    if rs["enabled"] and len(c) > rs["period"] + lb + 2:
        r = rsi_series(c, rs["period"])
        for k in range(lb):
            i = last - k
            a, b = r[i], r[i - 1]
            if a is None or b is None:
                continue
            direction = None
            if b <= rs["level"] < a:
                direction = "ABOVE"
            elif sg["below"] and b >= rs["level"] > a:
                direction = "BELOW"
            if direction:
                add("RSI", direction, i, close=c[last], ref=r[last], pct=r[last] - rs["level"])
                break

    off, older = S.get("off", 0), S.get("olderMin")
    for on, sig in ((sg["atl"], "ATL"), (sg["low52"], "LOW52")):
        if not on:
            continue
        for k in range(lb):
            i = last - k
            if sig == "ATL":
                if off + i < 252:
                    break
                prev = min(lo[:i])
                if older is not None:
                    prev = min(prev, older)
            else:
                if i < 252:
                    break
                prev = min(lo[i - 252:i])
            if lo[i] < prev:
                add(sig, "LOW", i, close=c[last], ref=prev, pct=(lo[i] / prev - 1) * 100)
                break
    return hits


def signal_key(h):
    return f"{h['code']}|{h['sig']}|{h['dir']}|{h['date']}"


def sig_name(sig):
    if sig.startswith("MA"):
        return sig[2:] + "-day average"
    return {"ATL": "new all-time low", "LOW52": "new 52-week low", "RSI": "RSI cross"}.get(
        sig, "Fib " + sig[3:] + "%" if sig.startswith("FIB") else sig)


# ----------------------------------------------------------- notifications
def post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=dict({"User-Agent": UA}, **headers), method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status


def notify(text, dry_run=False):
    if dry_run:
        print("--- NOTIFICATION (dry run) ---\n" + text + "\n------------------------------")
        return ["dry-run"]
    res = []
    env = os.environ.get
    jobs = []
    if env("NTFY_TOPIC"):
        jobs.append(("ntfy", lambda: post("https://ntfy.sh/" + env("NTFY_TOPIC"), text.encode("utf-8"), {"Title": "TASI screener"})))
    if env("TELEGRAM_TOKEN") and env("TELEGRAM_CHAT_ID"):
        jobs.append(("telegram", lambda: post(f"https://api.telegram.org/bot{env('TELEGRAM_TOKEN')}/sendMessage",
                     json.dumps({"chat_id": env("TELEGRAM_CHAT_ID"), "text": text[:4000]}).encode(), {"Content-Type": "application/json"})))
    if env("DISCORD_WEBHOOK"):
        jobs.append(("discord", lambda: post(env("DISCORD_WEBHOOK"), json.dumps({"content": text[:1900]}).encode(),
                     {"Content-Type": "application/json"})))
    if not jobs:
        print("No notification channel configured (set NTFY_TOPIC, TELEGRAM_TOKEN + TELEGRAM_CHAT_ID, or DISCORD_WEBHOOK).")
    for name, fn in jobs:
        try:
            res.append(f"{name}: {fn()}")
        except Exception as e:
            res.append(f"{name}: failed ({e})")
    print("Notifications:", ", ".join(res) or "none")
    return res


# -------------------------------------------------------------- daily mode
def run_daily(args):
    cfg = load_config()
    stocks = load_stocks(cfg)
    if args.limit:
        stocks = dict(list(stocks.items())[:args.limit])
    prev_db = load_json(DATA_FILE, {}).get("stocks", {})
    print(f"Downloading {len(stocks)} stocks...")
    data, failed, info = download_all(stocks, prev_db)
    if info["abort"] or info["ok"] == 0:
        print("\nYahoo Finance is refusing or not answering requests from this server "
              f"(first error: {info['first_err'] or 'time budget used up'}).\n"
              "This sometimes happens on GitHub's servers. Try again later, or run this script on your own "
              "computer (python tasi_daily.py daily) and upload data/tasi_data.json to the repository.", file=sys.stderr)
        return 1
    if len(failed) > len(stocks) * 0.6:
        print(f"More than 60% of downloads failed (first error: {info['first_err']}); keeping the old data file.", file=sys.stderr)
        return 1
    DATA_DIR.mkdir(exist_ok=True)
    payload = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "keep": KEEP,
               "failed": failed, "stocks": data}
    DATA_FILE.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Saved {DATA_FILE.name}: {len(data)} stocks, {len(failed)} failed.")

    lb = cfg["lookback_days"]
    hits = []
    for code, S in data.items():
        if S.get("stale") or len(S["c"]) < 30:
            continue
        hits += analyse(code, S, cfg["signals"], lb)
    SIGNALS_FILE.write_text(json.dumps({"generated": payload["generated"], "signals": [
        {k: (round(v, 4) if isinstance(v, float) else v) for k, v in h.items()} for h in hits]}, ensure_ascii=False), encoding="utf-8")
    print(f"{len(hits)} signal(s) found.")

    state = load_json(STATE_FILE, {})
    nf = cfg["notify"]
    first_run = "signals_sent" not in state
    sent_before = set(state.get("signals_sent", []))
    chosen = hits
    if nf["only_new"]:
        chosen = [h for h in chosen if signal_key(h) not in sent_before]
    if nf["min_volume_x"]:
        chosen = [h for h in chosen if h["rv"] is not None and h["rv"] >= nf["min_volume_x"]]
    if nf["watchlist_only"]:
        chosen = [h for h in chosen if h["code"] in set(map(str, nf["watchlist"]))]
    if first_run and not nf["notify_on_first_run"]:
        print("First run: saving a baseline, no notification sent.")
    elif chosen:
        notify(build_message(chosen, stocks, nf["max_lines"]), args.dry_run)
    else:
        print("Nothing new to notify.")
    state["signals_sent"] = sorted(signal_key(h) for h in hits)
    state["daily_run"] = payload["generated"]
    save_state(state)
    return 0


def build_message(list_, stocks, max_lines):
    lines = []
    for h in list_[:max_lines]:
        name = stocks.get(h["code"], {}).get("name", "")
        direction = "" if h["dir"] == "LOW" else " " + h["dir"].lower()
        vol = f", vol {h['rv']:.1f}x" if h["rv"] is not None else ""
        lines.append(f"{h['code']} {name}: {sig_name(h['sig'])}{direction} ({h['date']}){vol}")
    if len(list_) > max_lines:
        lines.append(f"...and {len(list_) - max_lines} more")
    return f"TASI screener: {len(list_)} signal(s)\n" + "\n".join(lines)


def save_state(state):
    old = STATE_FILE.read_text(encoding="utf-8") if STATE_FILE.exists() else ""
    new = json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False)
    if new != old:
        STATE_FILE.write_text(new, encoding="utf-8")


# ------------------------------------------------------------- alerts mode
def market_open(now_utc=None):
    t = (now_utc or datetime.now(timezone.utc)) + timedelta(hours=3)  # Riyadh time, no daylight saving
    mins = t.hour * 60 + t.minute
    return t.weekday() in (6, 0, 1, 2, 3) and 9 * 60 + 45 <= mins <= 15 * 60 + 15


def fetch_quote(code):
    j = http_get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.SR?range=1d&interval=5m")
    res = j["chart"]["result"][0]
    meta = res.get("meta", {})
    q = res["indicators"]["quote"][0]
    closes = [x for x in (q.get("close") or []) if x is not None]
    price = meta.get("regularMarketPrice", closes[-1] if closes else None)
    if price is None:
        raise ValueError("no price")
    highs = [x for x in (q.get("high") or []) if x is not None][-4:]   # about the last 20 minutes
    lows = [x for x in (q.get("low") or []) if x is not None][-4:]
    return {"price": price, "high": max(highs) if highs else price, "low": min(lows) if lows else price}


def alert_key(a):
    return f"{a['code']}|{float(a['price']):.10g}|{a.get('dir', 'auto')}"


def evaluate_alert(a, st, q):
    """Return True when the price has passed the alert level."""
    target, direction = float(a["price"]), a.get("dir", "auto")
    first_look = "base" not in st
    if first_look:
        st["base"] = q["price"]
    if direction == "auto":
        direction = "up" if target >= st["base"] else "down"
    st["rdir"] = direction
    if direction == "up":
        return q["price"] >= target or (not first_look and q["high"] >= target)
    return q["price"] <= target or (not first_look and q["low"] <= target)


def run_alerts(args):
    alerts = load_json(ALERTS_FILE, [])
    if not alerts:
        print("alerts.json has no alerts.")
        return 0
    if not (args.force or market_open()):
        print("Outside market hours (Sun-Thu about 09:45-15:15 Riyadh); nothing to do. Use --force to override.")
        return 0
    state = load_json(STATE_FILE, {})
    st_alerts = state.setdefault("alerts", {})
    keys = {alert_key(a) for a in alerts}
    for k in list(st_alerts):
        if k not in keys:
            del st_alerts[k]

    waiting = [a for a in alerts if st_alerts.get(alert_key(a), {}).get("status") != "triggered"]
    codes = sorted({a["code"] for a in waiting})
    quotes = {}
    for code in codes:
        try:
            quotes[code] = fetch_quote(code)
        except Exception as e:
            print(f"  no price for {code}: {e}", file=sys.stderr)
    names = {c: s["name"] for c, s in load_stocks({"include_reits": True}).items()}
    fired = []
    for a in waiting:
        q = quotes.get(a["code"])
        if not q:
            continue
        st = st_alerts.setdefault(alert_key(a), {"status": "waiting"})
        if evaluate_alert(a, st, q):
            st.update(status="triggered", hit_price=q["price"], hit_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            fired.append((a, st))
    if fired:
        lines = [f"\U0001F7E2 {a['code']} {names.get(a['code'], '')}: passed {float(a['price']):.2f} going "
                 f"{'up' if st['rdir'] == 'up' else 'down'} (price now {st['hit_price']:.2f})" for a, st in fired]
        notify(f"PRICE ALERT: {len(fired)} share(s) passed your price\n" + "\n".join(lines), args.dry_run)
    else:
        print(f"Checked {len(codes)} share(s); no alert triggered.")
    save_state(state)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["daily", "alerts"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):   # emoji-safe output on Windows consoles
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(run_daily(args) if args.mode == "daily" else run_alerts(args))


if __name__ == "__main__":
    main()
