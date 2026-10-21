"""
Breakout Scanner  (v7)
----------------------
Finds uptrending, tight-consolidating stocks that are close to breaking out,
and shows each one's BREAKOUT LEVEL (PDH = previous day high).

MUST PASS (all on DAILY candles):
  1. Price             > $3
  2. ADR (20-day)      > 2%                 (the stock actually moves)
  3. Avg volume (50d)  > 500,000 shares
  4. Market cap        >= $2 billion
  5. Price             > 200 SMA            (the uptrend check)
  6. 200 SMA is not falling (today vs 20 days ago)
  7. TIGHT, last 5 days:
       a) Shrink      <= 0.7   candles are calmer than normal
       b) Base/ADR    <= 1.5   the whole 5-day box is small
       c) Close position >= 0.25   closed in the top 3/4 of the box (not the bottom)
  8. Double-check: every finalist is re-downloaded alone and re-tested.

LABELS ONLY (not required):
  - Hot sector: SUPER HOT SECTOR / HOT SECTOR / not hot
  - LEADER: made a new 52-week high in the last 20 days AND is still within 10% of it
  - EMA cluster: 10 EMA, 20 EMA and 50 SMA all within 1 ADR of each other
  - 10 EMA xN: N of the last 10 days wicked the 10 EMA but closed above it

NEW IN v5:
  - MARKET ANALYSIS box at the top: is QQQ above its 20 EMA? (GOOD / BAD market),
    plus a one-line breakdown and a headline.
  - Big, easy-to-read results page saved as scan_results.html (set OPEN_BROWSER = True to auto-open).
  - v6: strict tightness numbers back, new 10/20 EMA rule, market tier GOOD / NEUTRAL / BAD.
  - v7: SETUP SCORE out of 10. Only A+ setups (score 6+, best 10) are shown and watched by the alerts.
        Everything else that passed is saved in scan_all_passers.csv. Sector week/month % shown next to each stock.
  - RISK % per stock (market + sector decide it) and a position-size calculator (--size).

HOW TO RUN:
  Position size for a breakout:    python3 breakout_scanner.py --size NET --low 346.80 --account 25000
                                   (--low = low of the 1-min candle that broke the level;
                                    add --entry 350.40 if you want a specific entry price)
  Full scan:                       python3 breakout_scanner.py
  Full scan as of a past day:      python3 breakout_scanner.py --date 2026-10-01
  Why did a stock pass or fail?    python3 breakout_scanner.py --check ALAB XMTR MPWR
  ...as of a past day:             python3 breakout_scanner.py --check ALAB MPWR --date 2026-10-01

Setup (once):   pip3 install yfinance pandas requests rich
"""

import argparse
import html
import io
import logging
import os
import time
import warnings
import webbrowser
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import yfinance as yf
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Hide Yahoo's noisy "401 Unauthorized / Invalid Crumb" error spam (harmless).
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.simplefilter("ignore", FutureWarning)

# ----------------------------- SETTINGS ------------------------------------
MIN_PRICE = 3.0
MIN_ADR_PCT = 2.0
MIN_AVG_VOLUME = 500_000
MIN_MARKET_CAP = 2_000_000_000
ADR_DAYS = 20          # ADR lookback
VOLUME_DAYS = 50       # avg volume lookback

# --- Tightness (the consolidation part). Change these to experiment! ---
TIGHT_DAYS = 5            # how many recent days make up the "base" (try 4, 5, 10)
MAX_SHRINK = 0.8          # a) avg daily range of those days must be <= 70% of normal ADR
MAX_BASE_ADR = 1.5        # b) the whole base must be <= 1.5 ADRs tall (1.0 is very strict)
MIN_CLOSE_POSITION = 0.25 # c) close must be above the bottom quarter of the base (0 = off)
SMA200_SLACK = 1.0        # 200 SMA must not be falling (0.98 would allow a 2% dip)

# --- 10 / 20 EMA rule: 10 EMA above the 20 EMA (stacked up) OR the two are close together ---
EMA_MAX_GAP_ADR = 0.5     # if the 10 is BELOW the 20, it may be at most this many ADRs below it

# --- Labels (these never remove a stock, they only add info) ---
LEADER_NEW_HIGH_DAYS = 20       # LEADER: made a new 52-week high within this many trading days...
LEADER_MAX_BELOW_HIGH_PCT = 10  # ...and is still within this % of that high
CLUSTER_MAX_ADR = 1.0           # EMA cluster: 10 EMA, 20 EMA, 50 SMA within this many ADRs
EMA_TEST_DAYS = 10              # look back this many days for 10 EMA wick-and-hold days

# --- Setup score (out of 10). Only A+ setups get shown on the list and watched by the alerts ---
MIN_SCORE = 6             # a stock needs at least this many points to be called A+
MAX_SHOW = 10             # never show more than this many A+ setups (best first)

# --- Risk / position size. EDIT ACCOUNT_SIZE to your real number! ---
ACCOUNT_SIZE = 10_000     # your account size in dollars
RISK_GOOD_MARKET = {"SUPER HOT": 1.0, "HOT": 0.75, "not hot": 0.5, "unknown": 0.5}  # % of account
RISK_NEUTRAL_MARKET = {"SUPER HOT": 0.5, "HOT": 0.25, "not hot": 0.25, "unknown": 0.25}  # pullback / basing market
RISK_BAD_MARKET = 0.25    # % of account when the market is clearly bad
MARKET_CLUSTER_PCT = 2.0  # QQQ 10 EMA, 20 EMA, 50 SMA within this % of price = "bunched up"
NEWS_NEGATIVE_MIN = 3     # this many negative headlines (of the latest 5) steps risk down one level
NEG_WORDS = ("plunge", "tumble", "selloff", "sell-off", "slump", "crash", "tank", "sink", "fear",
             "panic", "rout", "slide", "plummet", "worst", "recession", "tariff", "downgrade", "falls", "drops")
OPEN_BROWSER = False      # True = auto-open the results page (it opens in your default browser)
MAX_POSITION_PCT = 100    # warn if one position is bigger than this % of the account

BATCH_SIZE = 150       # tickers downloaded per batch (smaller = fewer blocks)
BATCH_PAUSE = 3        # seconds to rest between batches
RETRY_ROUNDS = 3       # how many times to retry tickers Yahoo blocked
RETRY_PAUSE = 30       # seconds to wait before each retry round
HOT_TOP_N = 3          # a sector is "hot" if it ranks in the top N of 11

AS_OF = None           # set by --date. None = use the latest finished day.

# Sector name -> sector ETF used to measure that sector's performance
SECTOR_ETFS = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Consumer Cyclical": "XLY",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}

# Nasdaq.com uses slightly different sector names - translate them to ours
NASDAQ_SECTOR_MAP = {
    "Technology": "Technology",
    "Health Care": "Healthcare",
    "Finance": "Financial Services",
    "Consumer Discretionary": "Consumer Cyclical",
    "Consumer Services": "Consumer Cyclical",
    "Consumer Staples": "Consumer Defensive",
    "Telecommunications": "Communication Services",
    "Industrials": "Industrials",
    "Capital Goods": "Industrials",
    "Transportation": "Industrials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Public Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Basic Materials",
}

console = Console()


# ----------------------------- STEP 1: UNIVERSE ----------------------------
def get_all_tickers() -> list[str]:
    """Every NASDAQ / NYSE / AMEX ticker (no ETFs, test issues, warrants, units, rights)."""
    urls = [
        "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
    ]
    tickers = []
    for url in urls:
        text = requests.get(url, timeout=30).text
        df = pd.read_csv(io.StringIO(text), sep="|")
        df = df[~df.iloc[:, 0].astype(str).str.startswith("File Creation")]  # footer row
        sym_col = "Symbol" if "Symbol" in df.columns else "ACT Symbol"
        df = df[df["Test Issue"] == "N"]
        df = df[df["ETF"] == "N"]
        tickers += df[sym_col].tolist()

    clean = []
    for t in tickers:
        if not isinstance(t, str):            # skip blank rows
            continue
        if "$" in t or " " in t:              # skip preferreds
            continue
        if "." in t:
            t = t.replace(".", "-")           # Yahoo uses BRK-B not BRK.B
        if "-" in t and t.split("-")[-1] in ("W", "U", "R", "WS", "RT"):
            continue                          # warrants / units / rights
        if len(t) == 5 and t[-1] in ("W", "U", "R"):
            continue                          # SPAC warrants / units / rights
        clean.append(t)
    return sorted(set(clean))


# ----------------------------- STEP 2: THE RULES ---------------------------
def cut_to_date(df, as_of):
    """Keep only candles on or before a date (for the --date 'time machine')."""
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    return df[idx.normalize() <= pd.Timestamp(as_of)]


def drop_unfinished_candle(df: pd.DataFrame) -> pd.DataFrame:
    """If the market is open right now, today's candle is half-finished - ignore it."""
    now = pd.Timestamp.now(tz="America/New_York")
    market_open = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)
    if market_open and df.index[-1].date() == now.date():
        return df.iloc[:-1]
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Pick which candles to use: up to --date if given, else the last FINISHED day."""
    if AS_OF:
        return cut_to_date(df, AS_OF)
    return drop_unfinished_candle(df)


def evaluate(df: pd.DataFrame):
    """
    Measure everything on DAILY candles.
    Returns (metrics, checks):
      metrics = all the numbers and labels for this stock (None if not enough history)
      checks  = list of (rule name, what it needs, actual value, passed?)
    """
    df = prepare(df)
    if len(df) < 220:                       # 200 days for the SMA + 20 more to compare it
        return None, []

    close = df["Close"]
    price = close.iloc[-1]
    sma200 = close.tail(200).mean()
    sma200_before = close.iloc[-220:-20].mean()      # the 200 SMA as it was 20 days ago
    sma50 = close.tail(50).mean()
    ema10_series = close.ewm(span=10, adjust=False).mean()
    ema10 = ema10_series.iloc[-1]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]

    # Each day's range as a percent: how far it moved from low to high
    daily_range = (df["High"] / df["Low"] - 1) * 100
    adr = daily_range.tail(ADR_DAYS).mean()          # ADR% = average daily range, 20 days
    avg_vol = df["Volume"].tail(VOLUME_DAYS).mean()

    # ---- Tightness: the last N finished days are the "base" (a box) ----
    base = df.tail(TIGHT_DAYS)
    base_high = base["High"].max()
    base_low = base["Low"].min()
    base_range_pct = (base_high / base_low - 1) * 100
    base_adr = base_range_pct / adr if adr > 0 else float("inf")           # box height in ADRs
    shrink = daily_range.tail(TIGHT_DAYS).mean() / adr if adr > 0 else float("inf")
    if base_high > base_low:
        close_pos = (price - base_low) / (base_high - base_low)  # 0 = at the low, 1 = at the high
    else:
        close_pos = 1.0

    # ---- Labels ----
    gain_3m = (price / close.iloc[-64] - 1) * 100            # 63 trading days ~ 3 months
    close_52w = close.tail(252)
    below_high = (1 - price / close_52w.max()) * 100         # % below the 52-week closing high
    days_since_high = int(close_52w.values[::-1].argmax())   # 0 = today IS the 52-week high
    leader = bool(days_since_high <= LEADER_NEW_HIGH_DAYS and below_high <= LEADER_MAX_BELOW_HIGH_PCT)

    spread_pct = (max(ema10, ema20, sma50) - min(ema10, ema20, sma50)) / price * 100
    cluster_adr = spread_pct / adr if adr > 0 else float("inf")
    ema_cluster = bool(cluster_adr <= CLUSTER_MAX_ADR)

    recent = df.tail(EMA_TEST_DAYS)
    ema10_recent = ema10_series.tail(EMA_TEST_DAYS)
    ema10_tests = int(((recent["Low"] <= ema10_recent) & (recent["Close"] >= ema10_recent)).sum())

    ema_gap_adr = ((ema20 - ema10) / price * 100) / adr if (ema10 < ema20 and adr > 0) else 0.0
    ema_ok = bool(ema10 >= ema20 or ema_gap_adr <= EMA_MAX_GAP_ADR)

    checks = [
        ("Price", f"> ${MIN_PRICE:g}", f"${price:.2f}", bool(price > MIN_PRICE)),
        ("ADR (20-day)", f"> {MIN_ADR_PCT:g}%", f"{adr:.1f}%", bool(adr > MIN_ADR_PCT)),
        ("Avg volume (50-day)", f"> {MIN_AVG_VOLUME / 1e3:,.0f}K",
         f"{avg_vol / 1e3:,.0f}K", bool(avg_vol > MIN_AVG_VOLUME)),
        ("Price above 200 SMA", "price > 200 SMA",
         f"${price:.2f} vs ${sma200:.2f}", bool(price > sma200)),
        ("200 SMA not falling", f"now >= {SMA200_SLACK:g} x 20 days ago",
         f"${sma200:.2f} vs ${sma200_before:.2f}", bool(sma200 >= sma200_before * SMA200_SLACK)),
        ("10/20 EMA stacked or close", f"10>=20 or gap<={EMA_MAX_GAP_ADR:g} ADR",
         f"10 EMA ${ema10:.2f} vs 20 EMA ${ema20:.2f}", ema_ok),
        ("Shrink (candles calm?)", f"<= {MAX_SHRINK:g}", f"{shrink:.2f}", bool(shrink <= MAX_SHRINK)),
        ("Base/ADR (box small?)", f"<= {MAX_BASE_ADR:g}", f"{base_adr:.2f}", bool(base_adr <= MAX_BASE_ADR)),
        ("Close position in box", f">= {MIN_CLOSE_POSITION:g}", f"{close_pos:.2f}",
         bool(close_pos >= MIN_CLOSE_POSITION)),
    ]
    metrics = {
        "price": price, "breakout_level": df["High"].iloc[-1],   # PDH = high of the last finished day
        "sma200": sma200, "sma50": sma50, "ema10": ema10, "ema20": ema20,
        "adr": adr, "avg_vol": avg_vol, "gain_3m": gain_3m, "below_high": below_high,
        "days_since_high": days_since_high,
        "leader": leader, "ema_cluster": ema_cluster, "cluster_adr": cluster_adr,
        "ema10_tests": ema10_tests,
        "shrink": shrink, "base_adr": base_adr, "close_pos": close_pos,
        "base_range_pct": base_range_pct,
    }
    return metrics, checks


def score_setup(m, status="unknown"):
    """Setup score out of 10, and the reasons. The must-pass rules already decided the stock is OK;
    the score decides how close it is to your ideal picture (tight, clustered, tested the 10 EMA, leader, hot sector)."""
    pts, why = 0.0, []

    def add(p, label):
        nonlocal pts
        pts += p
        why.append(label)

    if m["shrink"] <= 0.5:
        add(1, "very calm candles")
    if m["base_adr"] <= 1.0:
        add(1, "tight box")
    if m["close_pos"] >= 0.6:
        add(1, "closing near the top")
    if m["ema10"] >= m["ema20"]:
        add(1, "10 EMA above 20 EMA")
    if m["ema_cluster"]:
        add(1, "EMA cluster")
    if m["ema10_tests"] >= 1:
        add(min(int(m["ema10_tests"]), 2), f"10 EMA test x{int(m['ema10_tests'])}")
    if m["leader"]:
        add(1, "LEADER")
    if status == "SUPER HOT":
        add(2, "SUPER HOT sector")
    elif status == "HOT":
        add(1, "HOT sector")
    return pts, why


def passes_technical_filters(df: pd.DataFrame):
    """Returns the stock's numbers if it passes every must-pass rule, else None."""
    metrics, checks = evaluate(df)
    if metrics is None or not all(c[3] for c in checks):
        return None
    return metrics


def download_batch(batch: list[str], passed: dict) -> list[str]:
    """Download one batch. Returns the tickers that got NO data (blocked or delisted)."""
    no_data = []
    try:
        data = yf.download(batch, period="2y" if AS_OF else "1y", interval="1d",
                           group_by="ticker", threads=True, progress=False, auto_adjust=True)
    except Exception:
        return list(batch)                  # whole batch failed -> retry later

    for t in batch:
        try:
            df = data[t] if len(batch) > 1 else data
            df = df.dropna(subset=["Close", "High", "Low", "Volume"])
            if df.empty:
                no_data.append(t)
                continue
            result = passes_technical_filters(df)
            if result:
                passed[t] = result
        except Exception:
            no_data.append(t)
    return no_data


def scan_technicals(tickers: list[str]):
    """Scan everything slowly, then retry whatever Yahoo blocked."""
    passed = {}
    missed = []
    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    for n, batch in enumerate(batches, 1):
        console.print(f"[cyan]Scanning batch {n}/{len(batches)}  "
                      f"({len(passed)} passing so far)[/cyan]")
        missed += download_batch(batch, passed)
        time.sleep(BATCH_PAUSE)

    # Retry rounds for anything with no data (mostly blocked ones; a few are truly delisted)
    for r in range(1, RETRY_ROUNDS + 1):
        if len(missed) < 5:
            break
        console.print(f"[yellow]Retry {r}/{RETRY_ROUNDS}: {len(missed)} tickers had no data. "
                      f"Waiting {RETRY_PAUSE}s so Yahoo calms down...[/yellow]")
        time.sleep(RETRY_PAUSE)
        retry_list, missed = missed, []
        for i in range(0, len(retry_list), 75):
            missed += download_batch(retry_list[i:i + 75], passed)
            time.sleep(BATCH_PAUSE)

    return pd.DataFrame.from_dict(passed, orient="index"), len(missed)


# ----------------------------- STEP 3: MARKET CAP + SECTOR -----------------
def get_nasdaq_reference() -> pd.DataFrame:
    """
    One single download from Nasdaq.com with market cap + sector for every stock.
    Much better than asking Yahoo about each stock one by one (that's what got blocked).
    """
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25000&download=true"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Accept": "application/json"}
    try:
        data = requests.get(url, headers=headers, timeout=30).json()["data"]
        rows = data.get("rows") or data["table"]["rows"]
        out = {}
        for r in rows:
            sym = str(r.get("symbol", "")).strip().replace("/", "-").replace(".", "-")
            try:
                mcap = float(str(r.get("marketCap", "")).replace(",", ""))
            except ValueError:
                mcap = float("nan")
            out[sym] = {"mcap": mcap, "sector": r.get("sector", "")}
        return pd.DataFrame.from_dict(out, orient="index")
    except Exception as e:
        console.print(f"[yellow]Couldn't get Nasdaq reference data ({e}). "
                      f"Falling back to Yahoo for each stock (slower).[/yellow]")
        return pd.DataFrame(columns=["mcap", "sector"])


def get_market_cap(ticker: str):
    """Backup: market cap from Yahoo. Returns None (not 0!) if Yahoo won't answer."""
    for attempt in range(3):
        try:
            return ticker, float(yf.Ticker(ticker).fast_info["marketCap"])
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return ticker, None


def get_sector(ticker: str):
    """Backup: sector from Yahoo."""
    for attempt in range(3):
        try:
            return ticker, yf.Ticker(ticker).info.get("sector") or "Unknown"
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return ticker, "Unknown"


def add_cap_and_sector(df: pd.DataFrame):
    console.print(f"[cyan]Getting market cap + sector for {len(df)} stocks...[/cyan]")
    ref = get_nasdaq_reference()
    mcap_lookup = ref["mcap"].to_dict()
    sector_lookup = ref["sector"].to_dict()

    df["mcap"] = [mcap_lookup.get(t, float("nan")) for t in df.index]
    df["sector"] = [NASDAQ_SECTOR_MAP.get(sector_lookup.get(t, ""), None) for t in df.index]

    # Backup for anything Nasdaq didn't have
    need_cap = [t for t in df.index if pd.isna(df.at[t, "mcap"])]
    if need_cap:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for t, cap in pool.map(get_market_cap, need_cap):
                if cap:
                    df.at[t, "mcap"] = cap

    unknown = int(df["mcap"].isna().sum())     # still couldn't get market cap
    df = df[df["mcap"] >= MIN_MARKET_CAP].copy()

    need_sector = [t for t in df.index if df.at[t, "sector"] is None or pd.isna(df.at[t, "sector"])]
    if need_sector:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for t, sec in pool.map(get_sector, need_sector):
                df.at[t, "sector"] = sec
    df["sector"] = df["sector"].fillna("Unknown")
    return df, unknown


def get_history(ticker: str):
    """One stock's clean daily candles (2 years), with retries. None if Yahoo won't answer."""
    for attempt in range(3):
        try:
            hist = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=True)
            hist = hist.dropna(subset=["Close", "High", "Low", "Volume"])
            if len(hist) > 0:
                return hist
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def recheck_finalists(df: pd.DataFrame):
    """
    Double-check every finalist ONE BY ONE on its own clean DAILY candles.
    The big scan downloads 150 stocks at a time, so this guards against bad batch data.
    Anything that fails the re-check is thrown out; passing stocks get fresh numbers.
    """
    console.print(f"[cyan]Double-checking {len(df)} finalists one by one on daily candles...[/cyan]")
    out = df.copy()
    dropped, not_rechecked = [], 0
    for t in list(out.index):
        fresh = get_history(t)
        if fresh is None:                    # Yahoo wouldn't answer - keep it, but say so
            not_rechecked += 1
            continue
        result = passes_technical_filters(fresh)
        if result is None:
            used = prepare(fresh)
            p = used["Close"].iloc[-1]
            sma = used["Close"].tail(200).mean()
            console.print(f"[yellow]Dropped {t}: failed the re-check "
                          f"(price {p:.2f}, 200 SMA {sma:.2f})[/yellow]")
            dropped.append(t)
            continue
        for k, v in result.items():          # use the clean numbers from here on
            out.loc[t, k] = v
    return out.drop(index=dropped), not_rechecked, dropped


# ----------------------------- STEP 4: HOT SECTORS -------------------------
def get_sector_strength() -> pd.DataFrame:
    """1-week (5 trading days) and 1-month (21 trading days) % change per sector ETF."""
    etfs = list(SECTOR_ETFS.values())
    data = yf.download(etfs, period="1y", interval="1d", progress=False,
                       auto_adjust=True)["Close"]
    if AS_OF:
        data = cut_to_date(data, AS_OF)
    week = (data.iloc[-1] / data.iloc[-6] - 1) * 100
    month = (data.iloc[-1] / data.iloc[-22] - 1) * 100

    rows = []
    for sector, etf in SECTOR_ETFS.items():
        rows.append({"sector": sector, "etf": etf, "week": week[etf], "month": month[etf]})
    s = pd.DataFrame(rows)
    s["week_rank"] = s["week"].rank(ascending=False)
    s["month_rank"] = s["month"].rank(ascending=False)

    def label(r):
        hot_w = r["week_rank"] <= HOT_TOP_N and r["week"] > 0
        hot_m = r["month_rank"] <= HOT_TOP_N and r["month"] > 0
        if hot_w and hot_m:
            return "SUPER HOT"      # strong on BOTH week and month
        if hot_w or hot_m:
            return "HOT"            # strong on one timeframe
        return "not hot"

    s["status"] = s.apply(label, axis=1)
    return s.set_index("sector")


# ----------------------------- MARKET ANALYSIS -----------------------------
def get_headlines():
    """Latest QQQ/SPY news headlines from Yahoo (empty list if Yahoo won't give them)."""
    for sym in ("QQQ", "SPY"):
        try:
            titles = []
            for item in (yf.Ticker(sym).news or [])[:5]:
                title = item.get("title") or (item.get("content") or {}).get("title")
                if title:
                    titles.append(str(title))
            if titles:
                return titles
        except Exception:
            pass
    return []


def get_market_analysis() -> dict:
    """
    Market tier from several factors (not just one line):
      GOOD    = QQQ above its 20 EMA (your rule)
      NEUTRAL = QQQ below the 20 EMA but still above its 50 SMA (pullback in an uptrend)
                OR its 10/20/50 are bunched together (basing)
      BAD     = QQQ below the 20 EMA AND below the 50 SMA, not bunched up
    Very negative news steps the tier down one level.
    """
    out = {"ok": False}
    try:
        data = yf.download(["QQQ", "SPY"], period="1y", interval="1d", progress=False,
                           auto_adjust=True)["Close"]
        if AS_OF:
            data = cut_to_date(data, AS_OF)
        data = data.dropna()
        q = data["QQQ"]
        price = float(q.iloc[-1])
        ema10 = float(q.ewm(span=10, adjust=False).mean().iloc[-1])
        ema20 = float(q.ewm(span=20, adjust=False).mean().iloc[-1])
        sma50 = float(q.tail(50).mean())
        gap = (price / ema20 - 1) * 100
        spread = (max(ema10, ema20, sma50) - min(ema10, ema20, sma50)) / price * 100
        above20, above50 = price > ema20, price > sma50
        bunched = spread <= MARKET_CLUSTER_PCT

        if above20:
            tier = "GOOD"
            reason = f"QQQ is {gap:.1f}% above its 20 EMA"
        elif above50:
            tier = "NEUTRAL"
            reason = "QQQ is below its 20 EMA but still above its 50 SMA (a pullback in an uptrend)"
        elif bunched:
            tier = "NEUTRAL"
            reason = "QQQ is below its 20 EMA but the 10/20/50 are bunched together (basing)"
        else:
            tier = "BAD"
            reason = "QQQ is below both its 20 EMA and its 50 SMA"

        titles = [] if AS_OF else get_headlines()
        neg = sum(1 for t in titles if any(w in t.lower() for w in NEG_WORDS))
        news_note = ""
        if len(titles) >= NEWS_NEGATIVE_MIN and neg >= NEWS_NEGATIVE_MIN and tier != "BAD":
            tier = {"GOOD": "NEUTRAL", "NEUTRAL": "BAD"}[tier]
            news_note = f"; news is mostly negative ({neg} of {len(titles)} headlines), so risk steps down one level"

        meaning = {"GOOD": "breakouts have the wind at their back",
                   "NEUTRAL": "be pickier and use smaller size",
                   "BAD": "breakouts fail more often, keep size small"}[tier]
        out.update(ok=True, tier=tier, good=(tier == "GOOD"), date=q.index[-1].date(),
                   headline=titles[0] if titles else None,
                   summary=f"{tier} market: {reason}{news_note} - {meaning}.")
    except Exception as e:
        out["error"] = str(e)
    return out


def risk_for(market: dict, status: str) -> float:
    """Risk % of account: market tier decides the table, sector heat picks the row."""
    tier = market.get("tier") if market.get("ok") else "BAD"
    if tier == "GOOD":
        return RISK_GOOD_MARKET.get(status, 0.5)
    if tier == "NEUTRAL":
        return RISK_NEUTRAL_MARKET.get(status, 0.25)
    return RISK_BAD_MARKET


def size_position(account: float, risk_pct: float, entry: float, stop: float):
    """Shares so that if price falls to the stop you lose exactly risk_pct of the account."""
    per_share = entry - stop
    if per_share <= 0:
        return None
    risk_dollars = account * risk_pct / 100
    shares = int(risk_dollars // per_share)
    return {"shares": shares, "risk_dollars": shares * per_share, "budget": risk_dollars,
            "per_share": per_share, "position": shares * entry,
            "position_pct": shares * entry / account * 100}


def show_market(m: dict):
    if not m.get("ok"):
        console.print(f"[yellow]Market analysis unavailable ({m.get('error', 'no data')}).[/yellow]")
        return
    color = {"GOOD": "green", "NEUTRAL": "yellow", "BAD": "red"}[m["tier"]]
    lines = [f"[bold {color}]{m['summary']}[/bold {color}]"]
    if m.get("headline"):
        lines.append(f"Headline: {m['headline']}")
    console.print(Panel("\n".join(lines), title=f"MARKET ANALYSIS  ({m['date']})",
                        border_style=color, padding=(1, 2)))


def size_command(ticker: str, low: float, account: float, entry, pdh):
    """--size: print the alert card (what the phone notification will say)."""
    t = ticker.upper().replace(".", "-")
    market = get_market_analysis()
    sectors = get_sector_strength()
    ref = get_nasdaq_reference()
    sector = NASDAQ_SECTOR_MAP.get(ref["sector"].get(t, "") if not ref.empty else "", None)
    if sector is None:
        _, sector = get_sector(t)
    status = sectors["status"].get(sector, "unknown")
    hist = get_history(t)
    if pdh is None and hist is not None:
        pdh = float(prepare(hist)["High"].iloc[-1])
    if entry is None:
        entry = pdh
    if entry is None:
        console.print("[red]Couldn't get a price. Add --entry 123.45 and try again.[/red]")
        return
    risk_pct = risk_for(market, status)
    r = size_position(account, risk_pct, entry, low)
    show_market(market)
    if r is None:
        console.print(f"[red]Stop (${low:.2f}) must be BELOW entry (${entry:.2f}).[/red]")
        return
    mk = market.get("tier", "UNKNOWN") if market.get("ok") else "UNKNOWN"
    body = (f"[bold green]{t}  -  BUY {r['shares']:,} shares[/bold green]\n"
            f"Entry ~${entry:.2f}" + (f"   (broke PDH ${pdh:.2f})" if pdh else "") + "\n"
            f"Stop  ${low:.2f}   (low of the breakout candle)  -  ${r['per_share']:.2f} risk per share\n"
            f"Max loss if stopped: [bold]${r['risk_dollars']:,.0f}[/bold] = {risk_pct}% of ${account:,.0f}\n"
            f"Position size: ${r['position']:,.0f} ({r['position_pct']:.0f}% of account)\n"
            f"Market: {mk}   |   Sector: {sector} ({HOT_LABEL.get(status, status)})")
    console.print(Panel(body, title="TRADE ALERT PREVIEW", border_style="cyan", padding=(1, 2)))
    if r["position_pct"] > MAX_POSITION_PCT:
        console.print(f"[yellow]Warning: this position is {r['position_pct']:.0f}% of your account - "
                      f"more than you have in buying power. Stop is very tight; size it down.[/yellow]")
    if r["shares"] == 0:
        console.print("[yellow]Risk budget is smaller than 1 share's risk. Stop is too wide for this risk.[/yellow]")


# ----------------------------- STEP 5: DISPLAY -----------------------------
STATUS_COLOR = {"SUPER HOT": "bold red", "HOT": "yellow", "unknown": "dim"}
HOT_LABEL = {"SUPER HOT": "SUPER HOT SECTOR", "HOT": "HOT SECTOR",
             "not hot": "not hot", "unknown": "unknown"}


def fmt_price(p: float) -> str:
    return f"${p:,.0f}" if p >= 1000 else f"${p:.2f}"


def fmt_cap(c: float) -> str:
    return f"${c / 1e12:.2f}T" if c >= 1e12 else f"${c / 1e9:.1f}B"


def show_sector_table(sectors: pd.DataFrame):
    t = Table(title="Sector Performance", header_style="bold white")
    for col in ["Sector", "ETF", "Week %", "Month %", "Status"]:
        t.add_column(col)
    for sector, r in sectors.sort_values("month", ascending=False).iterrows():
        color = STATUS_COLOR.get(r["status"], "white")
        t.add_row(sector, r["etf"], f"{r['week']:+.2f}", f"{r['month']:+.2f}",
                  f"[{color}]{r['status']}[/{color}]")
    console.print(t)


def fmt_pct(x, digits=1):
    return "-" if x is None or pd.isna(x) else f"{x:+.{digits}f}%"


def to_level(r):
    """How far the price is below the breakout level, in % (positive = still has to climb)."""
    return (r["breakout_level"] / r["price"] - 1) * 100


def show_results(df: pd.DataFrame, title=None):
    """Terminal table: fewer columns, roomy, wraps instead of cutting off. Full detail is in the HTML page."""
    title = title or f"A+ Setups ({len(df)})  -  best score first"
    if AS_OF:
        title += f"  -  AS OF {AS_OF}"
    t = Table(title=title, header_style="bold white", padding=(0, 2), show_lines=True, expand=True)
    t.add_column("Ticker", no_wrap=True, min_width=7)
    t.add_column("Score", no_wrap=True, justify="right", min_width=6)
    t.add_column("Price", no_wrap=True, justify="right", min_width=9)
    t.add_column("BREAKOUT LEVEL\n(PDH)", no_wrap=True, justify="right", min_width=14)
    t.add_column("Hot Sector?\n(sector week | month)", min_width=22, overflow="fold")
    t.add_column("Risk", no_wrap=True, justify="right", min_width=6)
    t.add_column("Leader?", no_wrap=True, min_width=8)
    t.add_column("Notes + where the score came from", overflow="fold", ratio=2)

    for ticker, r in df.iterrows():
        color = STATUS_COLOR.get(r["status"], "white")
        label = HOT_LABEL.get(r["status"], r["status"])
        notes = []
        if r["ema_cluster"]:
            notes.append("EMA cluster")
        if int(r["ema10_tests"]) >= 1:
            notes.append(f"10 EMA x{int(r['ema10_tests'])}")
        t.add_row(
            f"[bold green]{ticker}[/bold green]",
            f"[bold]{r['score']:g}/10[/bold]",
            fmt_price(r["price"]),
            f"[bold cyan]{fmt_price(r['breakout_level'])}[/bold cyan]\n[dim]{to_level(r):+.1f}% away[/dim]",
            f"[{color}]{label}[/{color}]\n[bold]{r['sector']}[/bold]\n[dim]week {fmt_pct(r['sector_week'])} | month {fmt_pct(r['sector_month'])}[/dim]",
            f"{r['risk_pct']:g}%",
            "[bold green]LEADER[/bold green]" if r["leader"] else "-",
            "\n".join(notes) + "\n[dim]Score from: " + (r["why"] if r["why"] else "nothing yet") + "[/dim]",
        )
    console.print(t)
    console.print("[dim]Score = how close to your ideal picture (out of 10). Risk = % of your account to risk "
                  "(market + sector). Full details: open scan_results.html.[/dim]")


def write_html(df: pd.DataFrame, sectors: pd.DataFrame, market: dict, path: str, rest=None, closest=False):
    """Big, readable web page with everything. Opens in your browser."""
    e = html.escape
    def build_rows(frame):
        rows = []
        for ticker, r in frame.iterrows():
            cls = {"SUPER HOT": "super", "HOT": "hot"}.get(r["status"], "")
            notes = []
            if r["ema_cluster"]:
                notes.append("EMA cluster")
            if int(r["ema10_tests"]) >= 1:
                notes.append(f"10 EMA x{int(r['ema10_tests'])}")
            rows.append(
                f"<tr class='{cls}'><td class='tk'>{e(ticker)}</td><td><b>{r['score']:g}/10</b></td>"
                f"<td>{fmt_price(r['price'])}</td>"
                f"<td class='lvl'>{fmt_price(r['breakout_level'])}</td><td>{to_level(r):+.1f}%</td>"
                f"<td><b>{e(HOT_LABEL.get(r['status'], r['status']))}</b></td>"
                f"<td>{e(r['sector'])}</td><td>{fmt_pct(r['sector_week'])}</td><td>{fmt_pct(r['sector_month'])}</td>"
                f"<td>{r['risk_pct']:g}%</td><td>{'LEADER' if r['leader'] else '-'}</td>"
                f"<td>{r['adr']:.1f}%</td><td>{r['gain_3m']:+.0f}%</td>"
                f"<td>{r['shrink']:.2f}</td><td>{r['base_adr']:.2f}</td>"
                f"<td>{r['avg_vol'] / 1e6:.1f}M</td><td>{fmt_cap(r['mcap'])}</td>"
                f"<td>{e(', '.join(notes) or '-')}</td></tr>")
        return "".join(rows)

    head = ("<tr><th>Ticker</th><th>Score</th><th>Price</th><th>BREAKOUT LEVEL (PDH)</th><th>To level</th>"
            "<th>Hot Sector?</th><th>Sector</th><th>Sector week</th><th>Sector month</th>"
            "<th>Risk</th><th>Leader?</th><th>ADR</th><th>3M</th><th>Shrink</th><th>Base/ADR</th>"
            "<th>Avg Vol</th><th>Mkt Cap</th><th>Notes</th></tr>")
    rows = build_rows(df)
    rest_html = ""
    if rest is not None and len(rest):
        rest_html = (f"<h2>Passed the rules but NOT A+ ({len(rest)})</h2>"
                     f"<p class='small'>Score below {MIN_SCORE}, so they are not on your watchlist or alerts.</p>"
                     f"<div class='wrap'><table>{head}{build_rows(rest)}</table></div>")
    if closest:
        top_title = f"No A+ setups today. Closest {len(df)} (do not force trades)"
    else:
        top_title = f"A+ Setups ({len(df)}) - best score first"
    sec_rows = "".join(
        f"<tr><td>{e(n)}</td><td>{x['etf']}</td><td>{x['week']:+.2f}%</td><td>{x['month']:+.2f}%</td>"
        f"<td><b>{e(HOT_LABEL.get(x['status'], x['status']))}</b></td></tr>"
        for n, x in sectors.sort_values("month", ascending=False).iterrows())
    if market.get("ok"):
        mcls = {"GOOD": "good", "NEUTRAL": "neutral", "BAD": "bad"}[market["tier"]]
        mk = (f"<h2>Market Analysis ({market['date']})</h2><p class='big'>{e(market['summary'])}</p>"
              + (f"<p>Headline: {e(market['headline'])}</p>" if market.get("headline") else ""))
    else:
        mcls, mk = "bad", "<h2>Market Analysis</h2><p>Unavailable right now.</p>"
    asof = f" - as of {AS_OF}" if AS_OF else ""
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Breakout Scanner</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0d0d0d;color:#eee;margin:0;padding:28px}}
h1{{color:#d4af37}} h2{{margin-top:0}}
.box{{border-radius:12px;padding:18px 24px;margin:18px 0}}
.good{{background:#0f2a17;border:2px solid #2ecc71}} .bad{{background:#2d1111;border:2px solid #e74c3c}}
.neutral{{background:#2a2412;border:2px solid #f1c40f}}
.big{{font-size:20px;font-weight:600}}
table{{border-collapse:collapse;width:100%;font-size:18px}}
th{{text-align:left;background:#1c1c1c;color:#d4af37;padding:12px 14px;position:sticky;top:0}}
td{{padding:12px 14px;border-bottom:1px solid #2a2a2a;white-space:nowrap}}
.tk{{font-size:22px;font-weight:800;color:#2ecc71}} .lvl{{color:#4dd0ff;font-weight:800;font-size:20px}}
tr.super td{{background:#2a1313}} tr.hot td{{background:#2a2412}}
.small{{font-size:15px;color:#aaa}} .wrap{{overflow-x:auto}}
</style></head><body>
<h1>Breakout Scanner v7{e(asof)}</h1>
<div class="box {mcls}">{mk}</div>
<h2>{top_title}</h2>
<div class="wrap"><table>{head}{rows}</table></div>
<p class="small">Score = how close to your ideal picture, out of 10 (tight, 10 EMA above 20, EMA cluster, 10 EMA tests, LEADER, hot sector).
To level = how far below the breakout level the price is. Sector week/month = that sector ETF's move, to compare with the stock.
Risk = % of your account to risk (market + sector). Shrink and Base/ADR: lower = tighter.
LEADER = made a new 52-week high in the last {LEADER_NEW_HIGH_DAYS} days and is within {LEADER_MAX_BELOW_HIGH_PCT}% of it.
EMA cluster = 10 EMA, 20 EMA, 50 SMA within {CLUSTER_MAX_ADR:g} ADR. 10 EMA xN = days wicking the 10 EMA and closing above it.</p>
{rest_html}
<h2>Sector Performance</h2>
<table style="width:auto"><tr><th>Sector</th><th>ETF</th><th>Week</th><th>Month</th><th>Status</th></tr>{sec_rows}</table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


# ----------------------------- CHECK MODE ----------------------------------
def check_tickers(tickers: list[str]):
    """Show every rule, with the real numbers, for specific stocks. Answers 'why not?'"""
    suffix = f"  (as of {AS_OF})" if AS_OF else ""
    console.rule(f"[bold]Rule check{suffix}[/bold]")
    ref = get_nasdaq_reference()

    for raw in tickers:
        t = raw.upper().replace(".", "-")
        hist = get_history(t)
        if hist is None:
            console.print(f"[red]{t}: couldn't get data from Yahoo. Try again in a minute.[/red]")
            continue
        metrics, checks = evaluate(hist)
        if metrics is None:
            console.print(f"[yellow]{t}: not enough daily history "
                          f"(needs about 220 trading days up to that date).[/yellow]")
            continue

        # market cap check (from Nasdaq.com, Yahoo as backup)
        mcap = ref["mcap"].get(t) if not ref.empty else None
        if mcap is None or pd.isna(mcap):
            _, mcap = get_market_cap(t)
        used = prepare(hist)
        last_day = used.index[-1].date()

        table = Table(title=f"{t}   (last candle used: {last_day})", header_style="bold white")
        for col in ["Rule", "Needs", "Actual", "Result"]:
            table.add_column(col, no_wrap=True)
        failed = []
        for name, needs, actual, ok in checks:
            table.add_row(name, needs, actual, "[green]PASS[/green]" if ok else "[bold red]FAIL[/bold red]")
            if not ok:
                failed.append(name)
        if mcap:
            ok = mcap >= MIN_MARKET_CAP
            table.add_row("Market cap", f">= ${MIN_MARKET_CAP / 1e9:g}B", fmt_cap(mcap),
                          "[green]PASS[/green]" if ok else "[bold red]FAIL[/bold red]")
            if not ok:
                failed.append("Market cap")
        else:
            table.add_row("Market cap", f">= ${MIN_MARKET_CAP / 1e9:g}B", "unknown", "[yellow]?[/yellow]")
        console.print(table)

        console.print(f"  Breakout level (PDH): [bold cyan]{fmt_price(metrics['breakout_level'])}[/bold cyan]"
                      f"   |   LEADER: {'[green]yes[/green]' if metrics['leader'] else 'no'}"
                      f" (52-week high was {metrics['days_since_high']} days ago, {metrics['below_high']:.0f}% below it)")
        pts, _ = score_setup(metrics)
        console.print(f"  Setup score: [bold]{pts:g}/8[/bold] before sector points (hot sector adds 1, super hot adds 2; "
                      f"A+ needs {MIN_SCORE}+ out of 10)")
        console.print(f"  EMA cluster: {'[green]yes[/green]' if metrics['ema_cluster'] else 'no'}"
                      f" (10/20 EMA and 50 SMA spread = {metrics['cluster_adr']:.2f} ADR)"
                      f"   |   10 EMA tests in last {EMA_TEST_DAYS} days: {metrics['ema10_tests']}")
        if failed:
            console.print(f"  [bold red]REJECTED by: {', '.join(failed)}[/bold red]\n")
        else:
            console.print("  [bold green]WOULD APPEAR in the scan[/bold green]\n")


# ----------------------------- MAIN ----------------------------------------
def run_scan():
    start = time.time()
    console.rule("[bold]Breakout Scanner v7[/bold]")
    if AS_OF:
        console.print(f"[bold magenta]TIME MACHINE: running as of {AS_OF}[/bold magenta]")
    console.print(f"Tightness rules (last {TIGHT_DAYS} days): candles <= {MAX_SHRINK} x ADR, "
                  f"base <= {MAX_BASE_ADR} ADRs tall, close above the bottom "
                  f"{int(MIN_CLOSE_POSITION * 100)}% of the base")

    tickers = get_all_tickers()
    console.print(f"Universe: [bold]{len(tickers)}[/bold] tickers")

    candidates, still_missed = scan_technicals(tickers)
    if candidates.empty:
        console.print("[red]No stocks passed the technical filters.[/red]")
        return
    console.print(f"[bold]{len(candidates)}[/bold] passed price / trend / ADR / volume / tightness")

    candidates, unknown_caps = add_cap_and_sector(candidates)
    if candidates.empty:
        console.print("[red]Nothing left after the market cap filter.[/red]")
        return

    candidates, not_rechecked, dropped = recheck_finalists(candidates)
    if candidates.empty:
        console.print("[red]Nothing left after the double-check.[/red]")
        return

    sectors = get_sector_strength()
    candidates["status"] = candidates["sector"].map(sectors["status"]).fillna("unknown")
    candidates["sector_week"] = candidates["sector"].map(sectors["week"])      # blank if sector unknown
    candidates["sector_month"] = candidates["sector"].map(sectors["month"])
    candidates["leader_sort"] = candidates["leader"].apply(lambda x: 0 if x else 1)

    market = get_market_analysis()
    candidates["risk_pct"] = [risk_for(market, st) for st in candidates["status"]]

    scored = [score_setup(r, r["status"]) for _, r in candidates.iterrows()]
    candidates["score"] = [p for p, _ in scored]
    candidates["why"] = [", ".join(w) for _, w in scored]

    # Best score first. Ties: hotter sector, then best sector month, then leaders, then higher ADR.
    order = {"SUPER HOT": 0, "HOT": 1, "not hot": 2, "unknown": 3}
    candidates["sort"] = candidates["status"].map(order)
    candidates = candidates.sort_values(["score", "sort", "sector_month", "leader_sort", "adr"],
                                        ascending=[False, True, False, True, False], na_position="last")
    all_passers = candidates
    top = candidates[candidates["score"] >= MIN_SCORE].head(MAX_SHOW)
    rest = candidates.drop(index=top.index)
    console.print(f"[bold]{len(all_passers)}[/bold] stocks passed every must-pass rule. "
                  f"[bold green]{len(top)}[/bold green] scored {MIN_SCORE}+ out of 10 (A+ setups).")

    show_market(market)
    show_sector_table(sectors)
    if top.empty:
        console.print("[yellow]No A+ setups today. That is normal: no trade beats a forced trade. "
                      "Closest ones below (NOT A+).[/yellow]")
        show_results(all_passers.head(3), title="Closest 3 (NOT A+, do not force trades)")
    else:
        show_results(top)

    html_name = f"scan_results_{AS_OF}.html" if AS_OF else "scan_results.html"
    if top.empty:
        write_html(all_passers.head(3), sectors, market, html_name, rest=all_passers.iloc[3:], closest=True)
    else:
        write_html(top, sectors, market, html_name, rest=rest)
    full = os.path.abspath(html_name)
    if OPEN_BROWSER:
        try:
            webbrowser.open("file://" + full)
        except Exception:
            pass
    console.print(f"[bold]Big readable version saved:[/bold] {html_name}  "
                  f"(in VS Code's left sidebar, right-click it and choose Reveal in Finder, then double-click)")

    out_name = f"scan_results_{AS_OF}.csv" if AS_OF else "scan_results.csv"
    drop_cols = ["sort", "leader_sort"]
    top.drop(columns=drop_cols).to_csv(out_name)                    # only A+ setups: this is your watchlist
    all_name = f"scan_all_passers_{AS_OF}.csv" if AS_OF else "scan_all_passers.csv"
    all_passers.drop(columns=drop_cols).to_csv(all_name)            # everyone who passed, with scores
    console.print(f"\nSaved to [bold]{out_name}[/bold] (A+ only) and {all_name} (all)  "
                  f"(finished in {(time.time() - start) / 60:.1f} min)")

    # Honest report on anything we couldn't check
    if still_missed:
        console.print(f"[yellow]Note: {still_missed} tickers never returned data "
                      f"(most are delisted; if this number is big, run it again in a few minutes).[/yellow]")
    if unknown_caps:
        console.print(f"[yellow]Note: market cap could not be checked for {unknown_caps} stocks "
                      f"that passed the other filters. Run again if this is big.[/yellow]")
    n_unknown = int((candidates["sector"] == "Unknown").sum())
    if n_unknown:
        console.print(f"[yellow]Note: sector could not be found for {n_unknown} stocks "
                      f"(shown as 'unknown' at the bottom).[/yellow]")
    if dropped:
        console.print(f"[yellow]Note: {len(dropped)} stock(s) were thrown out by the double-check: "
                      f"{', '.join(dropped)}[/yellow]")
    if not_rechecked:
        console.print(f"[yellow]Note: {not_rechecked} stock(s) could not be double-checked "
                      f"(Yahoo wouldn't answer) - verify those on your chart.[/yellow]")


def main():
    global AS_OF
    ap = argparse.ArgumentParser(description="Breakout scanner")
    ap.add_argument("--date", help="run as if it were this day, e.g. 2026-10-01")
    ap.add_argument("--check", nargs="+", metavar="TICKER",
                    help="show every rule, with numbers, for these stocks")
    ap.add_argument("--size", metavar="TICKER", help="position-size calculator for a breakout")
    ap.add_argument("--low", type=float, help="with --size: low of the 1-min candle that broke the level (your stop)")
    ap.add_argument("--entry", type=float, help="with --size: entry price (default = the breakout level)")
    ap.add_argument("--pdh", type=float, help="with --size: breakout level (default = looked up)")
    ap.add_argument("--account", type=float, default=None, help="account size in $ (default = ACCOUNT_SIZE setting)")
    args = ap.parse_args()

    if args.date:
        try:
            pd.Timestamp(args.date)
        except Exception:
            console.print("[red]Date must look like 2026-10-01[/red]")
            return
        AS_OF = args.date

    if args.size:
        if args.low is None:
            console.print("[red]Add --low 346.80 (the low of the 1-minute candle that broke the level).[/red]")
            return
        size_command(args.size, args.low, args.account or ACCOUNT_SIZE, args.entry, args.pdh)
    elif args.check:
        check_tickers(args.check)
    else:
        run_scan()


if __name__ == "__main__":
    main()
