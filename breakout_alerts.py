"""
breakout_alerts.py  -  Fortuno Breakout Alerts
===============================================
Watches the stocks from your scanner (scan_results.csv) during market hours.
The second a stock trades above its breakout level (PDH = previous day high), it sends
ONE alert to your phone with exactly what to do:

    NET BREAKOUT - buy 85
    Buy 85 shares at $142.31
    Stop loss: $140.59
    Risk: $146 (0.5% of $10,000)
    Level $142.00 | SUPER HOT | Technology

Rules built in (from your strategy):
  * Stop loss = just below the LOW of the 1-minute candle that broke the level.
  * Shares = (account x risk %) / (entry - stop). Risk % comes from your scanner (market + sector heat).
  * If a stock GAPS UP above the level at the open, it waits for the first 1-minute candle to finish,
    and the stop goes below that first candle's low.
  * One alert per stock per day.

IMPORTANT: this uses Yahoo's free data, which is NOT guaranteed real-time and can lag or
rate-limit. Fine for testing and learning. For real trading you want a live broker feed
(like the Webull API), and you should always double-check the price in your broker app.

How to run:
  python3 breakout_alerts.py            -> watches all day (9:30-4:00 ET), checks every 30 seconds
  python3 breakout_alerts.py --once     -> checks one time right now (any hour) and prints what it finds
  python3 breakout_alerts.py --once --no-push   -> same, but doesn't send anything to your phone
  python3 breakout_alerts.py --demo     -> sends one FAKE sample alert so you can see how it looks
  python3 breakout_alerts.py --stop-at 14:55   -> stops at that ET time (the cloud uses this to hand over)
  python3 breakout_alerts.py --selftest -> while the market is open: sends 3 TEST alerts from live 1-minute prices
Leave the Terminal window open while it watches. Press Ctrl+C to stop.
"""
import os, sys, json, math, time, datetime as dt
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf

# ----------------------------- SETTINGS (edit these) -----------------------------
def _topic():
    """Your private ntfy topic. Cloud: the NTFY_TOPIC secret. On your Mac: the file ntfy_topic.txt."""
    t = os.environ.get("NTFY_TOPIC", "").strip()
    if not t:
        try:
            t = open("ntfy_topic.txt").read().strip()
        except Exception:
            t = ""
    return t or "PASTE-YOUR-TOPIC-NAME-HERE"


NTFY_TOPIC       = _topic()              # not typed here on purpose: this file goes on public GitHub
ACCOUNT_SIZE     = 10_000
MAX_POSITION_PCT = 100                   # biggest position, as % of the account (100 = no margin)
SCAN_CSV         = "scan_results.csv"
CHECK_EVERY      = 30                    # seconds between checks
MAX_WATCH        = 60                    # watch at most this many stocks from the scan
STALE_DAYS       = 4                     # warn if the scan is older than this
TZ               = ZoneInfo("America/New_York")
# ---------------------------------------------------------------------------------

OPEN_T, CLOSE_T = dt.time(9, 30), dt.time(16, 0)


def load_watch():
    """{ticker: {level, risk, status, sector}} from the scanner's CSV."""
    df = pd.read_csv(SCAN_CSV, index_col=0)
    age = (time.time() - os.path.getmtime(SCAN_CSV)) / 86400
    if age > STALE_DAYS:
        print(f"  WARNING: scan_results.csv is {age:.0f} days old. Run the scanner for fresh levels.")
    watch = {}
    for tk, r in df.head(MAX_WATCH).iterrows():
        try:
            level = float(r["breakout_level"])
            risk = float(r.get("risk_pct", 0.25))
        except Exception:
            continue
        if level > 0:
            try:
                score = float(r.get("score"))
            except Exception:
                score = None
            watch[str(tk)] = {"level": level, "risk": risk, "status": str(r.get("status", "")),
                              "sector": str(r.get("sector", "")), "score": score}
    return watch


def get_bars(tickers):
    """Today's 1-minute candles for each ticker: {ticker: DataFrame(Open, High, Low, Close)}."""
    out = {}
    try:
        df = yf.download(tickers, period="1d", interval="1m", group_by="ticker",
                         progress=False, auto_adjust=False, threads=True)
    except Exception as e:
        print(f"  (data problem: {e})")
        return out
    for tk in tickers:
        try:
            sub = df[tk] if tk in df.columns.get_level_values(0) else df
            sub = sub[["Open", "High", "Low", "Close"]].dropna()
            if len(sub):
                out[tk] = sub
        except Exception:
            continue
    return out


def check_breakout(bars, level, risk_pct, now):
    """Returns a dict describing the breakout, or None if it has not broken out (or can't be sized)."""
    if bars is None or len(bars) == 0:
        return None
    first = bars.iloc[0]
    gap = float(first["Open"]) > level
    if gap:
        # Gapped above the level: wait until the first 1-minute candle has finished.
        first_done = len(bars) >= 2 or (now - bars.index[0].to_pydatetime()).total_seconds() >= 60
        if not first_done:
            return None
        entry = float(bars["Close"].iloc[-1])
        stop = round(float(first["Low"]) - 0.01, 2)
        when = bars.index[0]
        kind = "Gap up, waited for the first 1-minute candle"
    else:
        hit = bars[bars["High"] >= level + 0.01]
        if hit.empty:
            return None
        brk = hit.iloc[0]
        when = hit.index[0]
        entry = round(max(level + 0.01, float(brk["Open"])), 2)
        stop = round(float(brk["Low"]) - 0.01, 2)
        kind = "Broke the level"
    per_share = entry - stop
    if per_share <= 0:
        return None
    shares = math.floor(ACCOUNT_SIZE * risk_pct / 100 / per_share)
    cap = math.floor(ACCOUNT_SIZE * MAX_POSITION_PCT / 100 / entry)
    capped = shares > cap
    shares = min(shares, cap)
    return {"entry": entry, "stop": stop, "shares": shares, "capped": capped, "kind": kind, "when": when,
            "risk_dollars": shares * per_share, "now_price": float(bars["Close"].iloc[-1])}


def format_alert(ticker, info, w):
    r = info
    if r["shares"] < 1:
        title = f"{ticker} BREAKOUT - stop too wide"
        body = [f"{r['kind']}, but the stop (${r['stop']:.2f}) is too far from ${r['entry']:.2f}",
                f"to buy even 1 share within your {w['risk']:g}% risk. Skip or size by hand."]
    else:
        title = f"{ticker} BREAKOUT - buy {r['shares']}"
        body = [f"Buy {r['shares']} shares at ${r['entry']:.2f}",
                f"Stop loss: ${r['stop']:.2f}",
                f"Risk: ${r['risk_dollars']:,.0f} ({w['risk']:g}% of ${ACCOUNT_SIZE:,})"]
        if r["capped"]:
            body.append(f"(size capped at {MAX_POSITION_PCT}% of your account)")
    score_txt = f"Setup {w['score']:g}/10 | " if w.get("score") is not None and w["score"] == w["score"] else ""
    body.append(f"{score_txt}Level ${w['level']:.2f} | {w['status']} | {w['sector']}")
    t = r["when"]
    try:
        t = t.tz_convert(TZ)
    except Exception:
        pass
    body.append(f"{r['kind']} ({t:%I:%M %p}). Now trading ${r['now_price']:.2f}")
    return title, "\n".join(body)


def push(title, body, no_push=False):
    print("\n" + "=" * 50 + f"\n{title}\n{body}\n" + "=" * 50)
    if no_push:
        return
    try:
        r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), timeout=15,
                          headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high",
                                   "Tags": "chart_with_upwards_trend"})
        print("  Phone alert sent." if r.ok else f"  Phone alert problem: {r.status_code} {r.text}")
    except Exception as e:
        print(f"  Phone alert failed: {e}")


def sent_file(now):
    return f"alerts_sent_{now:%Y-%m-%d}.json"


def load_sent(now):
    try:
        return set(json.load(open(sent_file(now))))
    except Exception:
        return set()


def save_sent(now, sent):
    try:
        json.dump(sorted(sent), open(sent_file(now), "w"))
    except Exception:
        pass


CLOUD = bool(os.environ.get("FORTUNO_CLOUD"))


def _git(*a):
    import subprocess
    return subprocess.run(["git", *a], capture_output=True, text=True)


def cloud_pull(now, sent):
    """Cloud only: two alert jobs overlap for a while. Before sending, fetch the other job's 'already alerted' list."""
    if not CLOUD:
        return
    _git("pull", "--rebase", "--autostash", "origin", "main")
    sent |= load_sent(now)


def cloud_push(now):
    """Cloud only: share the 'already alerted' list right away so the other job will not repeat the alert."""
    if not CLOUD:
        return
    f = sent_file(now)
    _git("config", "user.name", "fortuno-bot")
    _git("config", "user.email", "fortuno-bot@users.noreply.github.com")
    _git("add", f)
    _git("commit", "-m", "alert sent " + f)
    for _ in range(3):
        if _git("pull", "--rebase", "--autostash", "origin", "main").returncode == 0 and _git("push").returncode == 0:
            break
        time.sleep(3)


def one_pass(watch, sent, now, no_push=False):
    bars = get_bars(list(watch))
    found = 0
    for tk, w in watch.items():
        if tk in sent:
            continue
        info = check_breakout(bars.get(tk), w["level"], w["risk"], now)
        if info:
            if not no_push:
                cloud_pull(now, sent)
                if tk in sent:                 # the other job already sent this one
                    continue
            title, body = format_alert(tk, info, w)
            push(title, body, no_push)
            sent.add(tk)
            save_sent(now, sent)
            if not no_push:
                cloud_push(now)
            found += 1
    save_sent(now, sent)
    return found, len(bars)


def selftest(no_push=False):
    """Checks the live-data path for real: pulls today's 1-minute bars for 3 big stocks and sends 3 TEST alerts.
    Run it while the market is open. It also tells you how old the newest price is."""
    now = dt.datetime.now(TZ)
    bars = get_bars(["AAPL", "MSFT", "NVDA"])
    if not bars:
        push("Alert self-test FAILED", "No 1-minute prices came back. The market may be closed, or Yahoo is blocking this computer.", no_push)
        return
    for tk, b in bars.items():
        level = round(float(b["Close"].median()) * 0.995, 2)       # a level the stock already traded above
        w = {"level": level, "risk": 0.5, "status": "TEST", "sector": "self-test", "score": None}
        info = check_breakout(b, level, 0.5, now)
        lag = (now - b.index[-1].to_pydatetime()).total_seconds() / 60
        if info:
            title, body = format_alert(tk, info, w)
            push("TEST " + title, body + f"\nNewest 1-minute price is {lag:.0f} min old.", no_push)


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest("--no-push" in args)
        return
    if "--demo" in args:
        w = {"level": 142.00, "risk": 0.5, "status": "SUPER HOT", "sector": "Technology"}
        info = {"entry": 142.01, "stop": 140.59, "shares": 35, "capped": False, "kind": "Broke the level",
                "when": pd.Timestamp.now(tz=TZ), "risk_dollars": 35 * 1.42, "now_price": 142.40}
        title, body = format_alert("NET", info, w)
        push("DEMO " + title, body + "\n(this is a DEMO, not a real signal)", "--no-push" in args)
        return
    stop_at = None
    if "--stop-at" in args:
        try:
            h, m = args[args.index("--stop-at") + 1].split(":")
            stop_at = dt.time(int(h), int(m))
        except Exception:
            print("Use --stop-at like --stop-at 14:50")
            return
    if os.environ.get("FORTUNO_CLOUD") and "--once" not in args:
        today = f"{dt.datetime.now(TZ):%Y-%m-%d}"
        try:
            scan_day = open("scan_date.txt").read().strip()
        except Exception:
            scan_day = ""
        if scan_day != today:
            print(f"The morning scan did not finish today (last good scan: {scan_day or 'none'}). Alerts are OFF.")
            if dt.datetime.now(TZ).time() < dt.time(11, 0):
                push("Breakout alerts are OFF today",
                     "The morning scan did not finish, so I do not have today's breakout levels. "
                     "Do not rely on alerts today.")
            return
    try:
        watch = load_watch()
    except Exception as e:
        print(f"Could not read {SCAN_CSV} ({e}). Run breakout_scanner.py first.")
        return
    if not watch:
        print("The scan has no A+ setups to watch today. No alerts will fire.")
        return
    print(f"Watching {len(watch)} stocks: {', '.join(list(watch)[:12])}{'...' if len(watch) > 12 else ''}")

    if "--once" in args:
        now = dt.datetime.now(TZ)
        found, got = one_pass(watch, load_sent(now), now, "--no-push" in args)
        print(f"Checked {got} stocks with data. Breakouts found: {found}.")
        return

    print("Alerts are on. Leave this window open. Press Ctrl+C to stop.")
    while True:
        now = dt.datetime.now(TZ)
        if now.weekday() >= 5:
            print("Weekend: market closed. Run me again on a weekday.")
            return
        if now.time() >= CLOSE_T:
            print("Market closed for today. Done.")
            return
        if stop_at and now.time() >= stop_at:
            print(f"Reached {stop_at:%H:%M}. Handing over to the next run.")
            return
        if now.time() < OPEN_T:
            secs = (dt.datetime.combine(now.date(), OPEN_T, TZ) - now).total_seconds()
            print(f"  {now:%I:%M %p}: waiting for the open ({secs / 60:.0f} min)...")
            time.sleep(min(secs, 300))
            continue
        sent = load_sent(now)
        found, got = one_pass(watch, sent, now)
        print(f"  {now:%I:%M:%S %p}: checked {got} stocks, {len(sent)} alerted today.")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
