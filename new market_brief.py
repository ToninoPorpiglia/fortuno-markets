"""
market_brief.py  -  Fortuno Markets AI
=======================================
Run it twice a day (Premarket Brief before the open, After-Close Brief after) and it:
  1. Pulls prices for the main indexes, QQQ, VIX and the sector ETFs.
  2. Checks your market rule (QQQ vs its 20 EMA / 50 SMA) the same way the scanner does.
  3. Marks which sectors are HOT / SUPER HOT (same rule as the scanner).
  4. Reads your newest scanner results (scan_results.csv) for the watchlist.
  5. (Optional) Asks Claude to research the news and write the plain-English analysis. Needs an API key.
  6. Saves a page (market_brief.html), uploads it to your website, and sends a short alert to your phone (ntfy).

How to run:
  python3 market_brief.py              -> picks Premarket/After-Close by the clock, sends phone alert
  python3 market_brief.py --no-push    -> don't send the phone alert (for testing)
  python3 market_brief.py --no-web     -> don't upload to your website
  python3 market_brief.py --no-ai      -> numbers + headlines only, no AI writing
  python3 market_brief.py --scan       -> runs your scanner first, then makes the brief (slow)
  python3 market_brief.py --evening    -> force the After-Close version  (--morning forces Premarket)
"""
import os, sys, json, html, base64, time, datetime as dt
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


NTFY_TOPIC   = _topic()       # not typed here on purpose: this file goes on public GitHub
GITHUB_USER  = "ToninoPorpiglia"              # your GitHub username
GITHUB_REPO  = "fortuno-markets"              # the repo that powers your website
TOKEN_FILE   = "github_token.txt"             # the file that holds your GitHub key
PUBLIC_SHOW_WATCHLIST = True                  # False = hide your scanner tickers on the public website
ACCOUNT_SIZE = 10_000
USE_AI       = True                           # needs ANTHROPIC_API_KEY. False = numbers only
AI_MODEL     = "claude-sonnet-5-5"
SCAN_CSV     = "scan_results.csv"             # made by breakout_scanner.py
SCAN_ALL_CSV = "scan_all_passers.csv"         # everyone who passed the rules (used for the "close but not A+" box)
LOGO_FILE    = "logo.png"                     # put your Fortuno Trading logo in this folder with this name (png or jpg)
OUT_HTML     = "market_brief.html"
ARCHIVE_DIR  = "briefs"                       # every brief is also saved here, one file per run
TZ           = ZoneInfo("America/New_York")
HOT_TOP_N    = 3                              # same as the scanner: top 3 sectors by week / month
# ---------------------------------------------------------------------------------

# name, symbol, plain-English explanation shown under the number
INDEX_TILES = [
    ("S&P 500", "^GSPC", "The 500 biggest US companies. This is what most people mean by 'the market'. VOO and SPY are funds that copy it, so they move the same way."),
    ("QQQ", "QQQ", "A fund that tracks the 100 biggest Nasdaq companies (mostly tech). This is your market gauge: above its 20 EMA = good market for breakouts."),
    ("Russell 2000", "^RUT", "2,000 small companies. Small stocks do best when investors feel confident. If they lag the S&P, the market is cautious and breakouts are harder."),
    ("VIX", "^VIX", "The fear gauge. Under about 15 is calm, over 20 is nervous, over 30 is panic."),
]
FUTURES = {"S&P 500": ("S&P 500 futures", "ES=F"), "QQQ": ("QQQ (Nasdaq futures)", "NQ=F"),
           "Russell 2000": ("Russell futures", "RTY=F")}
SECTORS = [("Technology", "XLK"), ("Health care", "XLV"), ("Financials", "XLF"),
           ("Cons. discretionary", "XLY"), ("Comm. services", "XLC"), ("Industrials", "XLI"),
           ("Cons. staples", "XLP"), ("Energy", "XLE"), ("Utilities", "XLU"),
           ("Real estate", "XLRE"), ("Materials", "XLB")]
GLOSSARY = [
    ("ETF", "A basket of stocks you can buy like one stock. Sector ETFs (XLK, XLF...) show how a whole sector is doing."),
    ("Futures", "Contracts that trade before the market opens. They show where stocks are likely to start the day."),
    ("20 EMA", "The average price of the last 20 days, giving more weight to recent days. Price above it means the short-term trend is up."),
    ("CPI", "Consumer Price Index: how fast everyday prices are rising. Hotter than expected means the Fed may raise rates."),
    ("Fed rate hike", "The Federal Reserve raises interest rates to slow inflation. Higher rates make borrowing cost more and usually hurt growth stocks the most."),
    ("10-year yield", "The interest the government pays to borrow for 10 years. When it rises, safe bonds look better, so stocks (especially tech) look less attractive."),
    ("Hot sector", "A sector whose ETF is in the top 3 for the week or the month and positive. Super hot means top 3 for both."),
]

CSS = """:root{
  --bg:#0a0a0a; --card:#141414; --fg:#f1efe6; --muted:#9d9a8e; --line:#2b2a24;
  --gold:#d4af37; --green:#1fa463; --up:#2ecc71; --down:#ff6a5c; --flat:#9d9a8e;
  --up-bg:#0f2a17; --down-bg:#3a1512; --flat-bg:#232320; --gold-bg:#2a2410;
  --display:'Bricolage Grotesque','Segoe UI',system-ui,sans-serif;
  --body:'IBM Plex Sans','Segoe UI',system-ui,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.55 var(--body);margin:0;
  padding-block:24px 56px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
main{max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:28px}
h1,h2{font-family:var(--display);margin:0;text-wrap:balance;letter-spacing:-.01em}
h1{font-size:clamp(28px,6vw,40px);line-height:1.1}
h1{color:var(--green)} h1 span{color:var(--gold)}
h2{font-size:21px;margin-bottom:12px;border-left:4px solid var(--green);padding-left:10px}
.eyebrow{font:500 12px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--green)}
.sub{color:var(--muted);margin:6px 0 0}
.verdict{display:flex;gap:14px;align-items:flex-start;background:var(--gold-bg);border:1px solid var(--gold);border-radius:10px;padding:16px;margin-top:16px}
.verdict b{font-family:var(--display);font-size:18px}
.verdict p{margin:4px 0 0}
.pill{display:inline-block;font:500 11px var(--mono);letter-spacing:.04em;text-transform:uppercase;border-radius:99px;padding:2px 9px;white-space:nowrap}
.bull{background:var(--up-bg);color:var(--up)} .bear{background:var(--down-bg);color:var(--down)} .neut{background:var(--flat-bg);color:var(--flat)}
.fact{background:var(--flat-bg);color:var(--muted)} .take{background:var(--gold-bg);color:var(--gold)}
.brief{background:var(--gold);color:#0a0a0a;font-weight:600}
ul.sum{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:8px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--green);border-radius:10px;padding:12px}
.tile .n{font:500 12px var(--mono);color:var(--gold)}
.tile .p{font:500 19px var(--mono);margin-top:2px;font-variant-numeric:tabular-nums}
.tile .c{font:500 13px var(--mono);font-variant-numeric:tabular-nums}
.tile .d{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4}
.up{color:var(--up)} .dn{color:var(--down)} .fl{color:var(--flat)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.bars{display:flex;flex-direction:column;gap:7px}
.row{display:grid;grid-template-columns:120px 1fr 62px;gap:10px;align-items:center;font-size:13px}
.row .lab{color:var(--muted)}
.track{position:relative;height:18px}
.track:before{content:"";position:absolute;left:50%;top:0;bottom:0;border-left:1px solid var(--line)}
.bar{position:absolute;top:2px;bottom:2px;border-radius:3px}
.bar.u{left:50%;background:var(--up)} .bar.d{right:50%;background:var(--down)}
.val{font:500 13px var(--mono);text-align:right;font-variant-numeric:tabular-nums}
.note{font-size:12.5px;color:var(--muted);margin:10px 0 0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:600px;font-size:13.5px;font-variant-numeric:tabular-nums}
th{font:500 11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--gold);text-align:left;padding:8px 10px;border-bottom:2px solid var(--green)}
td{padding:11px 10px;border-bottom:1px solid var(--line);vertical-align:top}
td.rk{font:500 15px var(--mono);color:var(--gold);width:34px}
.heat-super{background:var(--down-bg);color:var(--gold);border:1px solid var(--gold)} .heat-hot{background:var(--gold-bg);color:var(--gold)} .heat-no{background:var(--flat-bg);color:var(--flat)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.two>*{min-width:0}
.kv{display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-bottom:1px solid var(--line);font-size:14px}
.kv:last-child{border:0}.kv span:last-child{text-align:right;max-width:62%}
.you{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--green);border-radius:10px;padding:16px}
.you ul{margin:8px 0 0;padding-left:20px;display:flex;flex-direction:column;gap:6px}
details.gloss{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
details.gloss summary{cursor:pointer;font:600 16px var(--display);color:var(--gold)}
details.gloss dl{margin:12px 0 0;display:grid;gap:10px}
details.gloss dt{font-weight:600} details.gloss dd{margin:2px 0 0;color:var(--muted)}
.sources{font-size:12.5px;color:var(--muted)}
code{font-family:var(--mono);color:var(--gold)}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
"""


def esc(x):
    return html.escape(str(x))


# ------------------------------ 1. PRICES ------------------------------
def closes(symbol, period="10d"):
    try:
        return yf.Ticker(symbol).history(period=period)["Close"].dropna()
    except Exception:
        return None


def get_tiles(premarket):
    out = []
    for name, sym, desc in INDEX_TILES:
        label = name
        if premarket and name in FUTURES:
            label, sym = FUTURES[name]
        c = closes(sym)
        if c is None or len(c) < 2:
            continue
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        price = f"${last:,.2f}" if sym == "QQQ" else f"{last:,.2f}"
        out.append({"name": label, "price": price, "chg": (last / prev - 1) * 100, "unit": "%", "desc": desc})
    return out


def get_yield():
    c = closes("^TNX")
    if c is None or len(c) < 2:
        return None
    return {"level": float(c.iloc[-1]), "bp": (float(c.iloc[-1]) - float(c.iloc[-2])) * 100}


def get_sectors():
    """Same hot-sector rule as the scanner: top 3 by week (5d) and by month (21d), and positive."""
    rows = []
    for name, etf in SECTORS:
        c = closes(etf, "3mo")
        if c is None or len(c) < 23:
            continue
        rows.append({"name": name, "etf": etf, "today": (c.iloc[-1] / c.iloc[-2] - 1) * 100,
                     "week": (c.iloc[-1] / c.iloc[-6] - 1) * 100, "month": (c.iloc[-1] / c.iloc[-22] - 1) * 100})
    if not rows:
        return []
    df = pd.DataFrame(rows)
    top_w = set(df.nlargest(HOT_TOP_N, "week")["etf"]) & set(df[df.week > 0]["etf"])
    top_m = set(df.nlargest(HOT_TOP_N, "month")["etf"]) & set(df[df.month > 0]["etf"])
    for r in rows:
        w, m = r["etf"] in top_w, r["etf"] in top_m
        r["status"] = "SUPER HOT" if (w and m) else "HOT" if (w or m) else "not hot"
    order = {"SUPER HOT": 0, "HOT": 1, "not hot": 2}
    return sorted(rows, key=lambda r: (order[r["status"]], -r["month"]))


def qqq_tier():
    """Same idea as the scanner: GOOD / NEUTRAL / BAD, based on QQQ vs its 20 EMA and 50 SMA."""
    c = closes("QQQ", "1y")
    if c is None or len(c) < 60:
        return None
    price = float(c.iloc[-1])
    ema10 = float(c.ewm(span=10, adjust=False).mean().iloc[-1])
    ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
    sma50 = float(c.rolling(50).mean().iloc[-1])
    spread = (max(ema10, ema20, sma50) - min(ema10, ema20, sma50)) / price * 100
    tier = "GOOD" if price > ema20 else ("NEUTRAL" if (price > sma50 or spread <= 2.0) else "BAD")
    return {"tier": tier, "price": price, "ema20": ema20, "sma50": sma50}


def get_headlines(n=6):
    out = []
    try:
        for item in (yf.Ticker("SPY").news or [])[:n]:
            title = item.get("title") or (item.get("content") or {}).get("title")
            if title:
                out.append(title)
    except Exception:
        pass
    return out


# ------------------------------ 2. YOUR WATCHLIST ------------------------------
def get_watchlist(limit=12):
    try:
        df = pd.read_csv(SCAN_CSV, index_col=0)
    except Exception:
        return None
    rows = []
    for tk, r in df.head(limit).iterrows():
        def num(key):
            try:
                v = float(r.get(key))
                return None if v != v else v
            except Exception:
                return None
        notes = []
        if bool(r.get("ema_cluster", False)):
            notes.append("EMA cluster")
        if int(num("ema10_tests") or 0) >= 1:
            notes.append(f"10 EMA x{int(num('ema10_tests'))}")
        rows.append({"ticker": str(tk), "level": float(r.get("breakout_level", float("nan"))),
                     "price": num("price"), "score": num("score"),
                     "status": str(r.get("status", "")), "risk": float(r.get("risk_pct", 0.0)),
                     "sector": str(r.get("sector", "")), "leader": bool(r.get("leader", False)),
                     "sec_week": num("sector_week"), "sec_month": num("sector_month"),
                     "notes": ", ".join(notes)})
    try:
        ran = dt.datetime.fromtimestamp(os.path.getmtime(SCAN_CSV), TZ)
    except Exception:
        ran = None
    levels = {str(tk): float(r.get("breakout_level", float("nan"))) for tk, r in df.head(60).iterrows()}
    near = []
    try:
        alld = pd.read_csv(SCAN_ALL_CSV, index_col=0)
        alld = alld[~alld.index.isin(df.index)].head(3)
        for tk, r in alld.iterrows():
            try:
                sc = float(r.get("score"))
            except Exception:
                sc = None
            near.append({"ticker": str(tk), "score": sc, "level": float(r.get("breakout_level", float("nan"))),
                         "price": float(r.get("price", float("nan"))), "sector": str(r.get("sector", "")),
                         "status": str(r.get("status", "")), "why": str(r.get("why", ""))})
    except Exception:
        near = []
    return {"rows": rows, "total": len(df), "ran": ran, "levels": levels, "near": near}


# ------------------------------ 3. AI ANALYSIS (optional) ------------------------------
AI_PROMPT = """Act as an institutional-grade macro strategist, equity research analyst, and market news editor.
It is {now} Eastern Time. This is the {label}. Use web search to get CURRENT information, then write a concise
market intelligence briefing for a retail momentum-breakout trader who is a college student (account ${acct:,}).
Focus only on information with meaningful market implications: Fed and rate expectations, Treasury yields, inflation,
jobs, GDP and other economic releases, global markets, currencies, commodities, crypto, earnings and guidance, big
company news, M&A, AI developments, regulation, geopolitics, trade, tariffs, sector performance, volatility,
breadth, sentiment, and the largest stock movers with the reason they moved. Rank developments by expected market
impact. Separate facts from interpretation. If you cannot verify something, leave it out. Never invent numbers.

SCOPE: For index moves in the overview, talk only about the S&P 500 (VOO/SPY), QQQ and small stocks (Russell 2000). Do not write about the Dow or the 10-year yield on their own. The separate WORLD section is where global news goes: it may include Europe (for example France, Italy, Germany, the UK), Asia, China, Japan, the Middle East, oil, central banks, politics, trade and tariffs, anything that is really moving markets or could.

WRITING STYLE IS CRITICAL: write in simple, plain English a smart 19-year-old would follow. Whenever you use a term
like CPI, yield, basis points, PPI, hike, breadth, guidance, short squeeze, etc., explain it in a few plain words
right there. Short sentences. No jargon walls.

Live numbers from our data feed (reliable):
{numbers}
The trader's own market rule says QQQ is: {tier_line}.

Reply with ONLY one JSON object (no markdown fences) in exactly this shape:
{{
 "stance": "bullish" | "bearish" | "neutral" | "mixed",
 "verdict_title": "Market for your strategy: <short plain verdict, e.g. not a clean breakout market>",
 "stance_line": "one or two plain sentences: what kind of tape this is for a breakout trader and why",
 "overview": "3 to 4 plain sentences: what is going on in the market right now and why, like telling a friend",
 "summary": [ {{"tag": "fact" | "take", "text": "..."}} ],            // EXACTLY 3 items, the 3 most important things only
 "ranked": [ {{"what": "max 15 words", "why": "ONE short plain-English sentence on why it matters, jargon explained", "call": "bullish" | "bearish" | "neutral"}} ],  // exactly 5, most important first, keep it SHORT
 "movers_up": [ {{"name": "Company (TICK)", "why": "+x% - the reason in 1-2 plain sentences, with a bit of context"}} ],   // up to 5
 "movers_down": [ {{"name": "Company (TICK)", "why": "-x% - the reason in 1-2 plain sentences, with a bit of context"}} ], // up to 5
 "world": [ {{"where": "Paris, France", "what": "what happened, 1-2 plain sentences", "effect": "1-2 plain sentences: what this does to US stocks/QQQ/the S&P and what it means for a momentum trader"}} ],  // 5 to 7 items from DIFFERENT parts of the world, most market-relevant first
 "earnings_this_week": [ {{"date": "Wed Oct 21", "company": "Nvidia", "ticker": "NVDA", "know": "if already reported: how it went and what drove it, 1-2 sentences. If not yet: what investors are watching", "good": "1 sentence: what a strong report would mean for the market and why", "bad": "1 sentence: what a weak report would mean and why"}} ],  // up to 8 of the most market-moving companies reporting THIS week
 "earnings_next_week": [ {{"date": "Tue Oct 27", "company": "...", "ticker": "...", "know": "what investors are watching", "good": "...", "bad": "..."}} ],  // up to 8 for NEXT week
 "catalysts": [ {{"date": "Mon Oct 19", "event": "...", "why": "plain-English why it matters"}} ]  // next 1-2 weeks: data, Fed, earnings, auctions
}}"""


def ai_analysis(label, tiles, tier, yld):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not (USE_AI and key):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        nums = [f"- {t['name']}: {t['price']} ({t['chg']:+.2f}%)" for t in tiles]
        tier_line = (f"{tier['tier']} (QQQ ${tier['price']:.2f}, 20 EMA ${tier['ema20']:.2f}, 50 SMA ${tier['sma50']:.2f})"
                     if tier else "unknown")
        prompt = AI_PROMPT.format(now=dt.datetime.now(TZ).strftime("%A %b %d, %Y %I:%M %p"), label=label,
                                  acct=ACCOUNT_SIZE, numbers="\n".join(nums), tier_line=tier_line)
        messages = [{"role": "user", "content": prompt}]
        resp = None
        for _ in range(4):
            resp = client.messages.create(
                model=AI_MODEL, max_tokens=12000, messages=messages,
                tools=[{"type": "web_search_20260318", "name": "web_search", "max_uses": 14}])
            if resp.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
        text = "".join(b.text for b in resp.content if b.type == "text")
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        print(f"  (AI analysis skipped: {type(e).__name__}: {e})")
        return None


TIER_TEXT = {
    "GOOD": ("bullish", "Market for your strategy: good for breakouts",
             "QQQ is above its 20 EMA, so your market rule says go. Normal risk sizes."),
    "NEUTRAL": ("mixed", "Market for your strategy: not a clean breakout market",
                "QQQ is below its 20 EMA but still holding up. Be picky and use smaller risk."),
    "BAD": ("bearish", "Market for your strategy: poor, stay small or flat",
            "QQQ is below its 20 EMA and 50 SMA. Protect your money and use the smallest risk."),
}


MEGA_CAPS = [("NVDA", "Nvidia"), ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"), ("AMZN", "Amazon"),
             ("META", "Meta"), ("TSLA", "Tesla"), ("AVGO", "Broadcom"), ("NFLX", "Netflix"), ("AMD", "AMD"),
             ("ORCL", "Oracle"), ("JPM", "JPMorgan"), ("COST", "Costco"), ("CRM", "Salesforce")]


def earnings_fallback():
    """No-AI backup: dates for the biggest companies (from Yahoo), with a general 'why it matters' line."""
    today = dt.datetime.now(TZ).date()
    monday = today - dt.timedelta(days=today.weekday())
    this_end, next_end = monday + dt.timedelta(days=6), monday + dt.timedelta(days=13)
    this_w, next_w = [], []
    for sym, name in MEGA_CAPS:
        try:
            cal = yf.Ticker(sym).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            d = dates[0] if dates else None
            if d is None:
                continue
            d = d if isinstance(d, dt.date) else pd.Timestamp(d).date()
        except Exception:
            continue
        row = {"date": d.strftime("%a %b %d"), "company": name, "ticker": sym,
               "know": "Big company, so its report can move the whole market.",
               "good": "Strong sales and a good outlook usually lift the whole market, especially tech.",
               "bad": "A miss or a weak outlook can pull the whole market down, especially tech.", "_d": d}
        if monday <= d <= this_end:
            this_w.append(row)
        elif this_end < d <= next_end:
            next_w.append(row)
    for lst in (this_w, next_w):
        lst.sort(key=lambda r: r["_d"])
        for r in lst:
            r.pop("_d", None)
    return {"earnings_this_week": this_w, "earnings_next_week": next_w}


WORLD_FEEDS = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "World"),
    ("https://www.france24.com/en/rss", "France / Europe"),
    ("https://rss.dw.com/rdf/rss-en-all", "Germany / Europe"),
    ("https://www.theguardian.com/world/rss", "UK / World"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Middle East / World"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "Business"),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362", "World economy"),
]
PLACES = ["Paris", "France", "Milan", "Italy", "Rome", "Germany", "Berlin", "Brussels", "EU", "Europe", "London", "UK", "Britain",
          "China", "Beijing", "Taiwan", "Japan", "Tokyo", "India", "Korea", "Russia", "Ukraine", "Israel", "Gaza", "Iran",
          "Saudi", "Brazil", "Mexico", "Canada", "Turkey", "Australia", "Spain", "Switzerland"]
# (keywords, plain-English "what it could mean for markets and you")
WORLD_RULES = [
    (("oil", "opec", "crude", "gas price", "energy"), "Oil prices touch almost everything. Higher oil can hurt airlines and shoppers and help energy stocks (XLE). Lower oil does the opposite."),
    (("tariff", "trade war", "sanction", "export", "import ban", "trade deal"), "Trade fights make goods cost more for companies. That can hurt profits, especially for tech, retailers and factories."),
    (("central bank", "ecb", "bank of england", "bank of japan", "interest rate", "rate cut", "rate hike", "inflation"), "Interest rates change how expensive borrowing is. Higher rates usually hurt growth stocks like those in QQQ. Lower rates usually help."),
    (("war", "missile", "attack", "strike", "troops", "ceasefire", "conflict", "invasion", "military"), "Conflict makes investors nervous. Money often leaves stocks for safer places, and oil can jump. These moves can fade fast or grow."),
    (("china", "taiwan", "chip", "semiconductor"), "China and Taiwan matter for tech because so many chips and parts come from there. Trouble can hit Nvidia, Apple and the whole QQQ."),
    (("election", "vote", "parliament", "government", "budget", "minister", "president"), "Political uncertainty can move that country's money and stocks, and sometimes spills into US markets if investors get scared."),
    (("ai ", "artificial intelligence", "nvidia", "openai"), "AI news moves the big tech stocks, and those drive QQQ and a big part of the S&P 500."),
    (("bank", "debt", "default", "bond", "recession", "growth", "unemployment", "jobs"), "News about the economy's health changes how much companies are expected to earn, which moves stocks."),
]


def world_fallback(max_items=7):
    """Free, no-AI world headlines from public news feeds, each with a general 'what it could mean' line."""
    import xml.etree.ElementTree as ET
    import re as _re
    cands = []
    for url, src in WORLD_FEEDS:
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
        except Exception:
            continue
        n = 0
        for it in list(root.iter("item"))[:25]:
            title = (it.findtext("title") or "").strip()
            desc = _re.sub(r"<[^>]+>", "", (it.findtext("description") or "")).strip()
            text = (title + " " + desc).lower() + " "
            effect = next((e for kws, e in WORLD_RULES if any(k in text for k in kws)), None)
            if not title or not effect:
                continue
            where = next((pl for pl in PLACES if _re.search(r"\b" + pl + r"\b", title + " " + desc)), src)
            what = title if not desc else title + ". " + (desc[:170].rsplit(" ", 1)[0] + "..." if len(desc) > 170 else desc)
            cands.append({"where": where, "what": what, "effect": effect, "_src": src})
            n += 1
            if n >= 3:
                break
    out, seen_src, seen_eff, seen_what = [], {}, {}, set()
    for c in cands:                                   # mix of places and of reasons
        if c["what"] in seen_what or seen_src.get(c["_src"], 0) >= 2 or seen_eff.get(c["effect"], 0) >= 2:
            continue
        seen_what.add(c["what"])
        seen_src[c["_src"]] = seen_src.get(c["_src"], 0) + 1
        seen_eff[c["effect"]] = seen_eff.get(c["effect"], 0) + 1
        out.append({k: v for k, v in c.items() if k != "_src"})
        if len(out) >= max_items:
            break
    return out


def market_overview(tiles, tier, sectors):
    """A short plain-English summary of what is going on, built from the numbers."""
    by = {t["name"].split(" (")[0].replace(" futures", ""): t for t in tiles}
    sp, qq, ru = by.get("S&P 500"), by.get("QQQ"), by.get("Russell")
    bits = []
    if sp and qq:
        both = "up" if (sp["chg"] > 0.15 and qq["chg"] > 0.15) else "down" if (sp["chg"] < -0.15 and qq["chg"] < -0.15) else "mixed"
        if both == "up":
            bits.append(f"Stocks are higher: the S&P 500 is {sp['chg']:+.2f}% and QQQ (tech) is {qq['chg']:+.2f}%.")
        elif both == "down":
            bits.append(f"Stocks are lower: the S&P 500 is {sp['chg']:+.2f}% and QQQ (tech) is {qq['chg']:+.2f}%.")
        else:
            bits.append(f"The market is mixed: the S&P 500 is {sp['chg']:+.2f}% and QQQ (tech) is {qq['chg']:+.2f}%.")
    bits.append(bars_meaning(tiles))
    if sectors:
        hot = [x for x in sectors if x.get("status") in ("SUPER HOT", "HOT")]
        if hot:
            bits.append("Hottest sector right now: " + hot[0]["name"] + " (" + hot[0]["etf"] + ").")
    if tier:
        bits.append({"GOOD": "Your market rule says GOOD: QQQ is above its 20 EMA, so breakouts have support.",
                     "NEUTRAL": "Your market rule says NEUTRAL: be picky and keep risk small.",
                     "BAD": "Your market rule says BAD: stay small or flat."}[tier["tier"]])
    return " ".join(bits)


def fallback_ai(tiles, tier, headlines, yld, sectors=None):
    """Used when there is no AI: a plain summary from the numbers alone. Always aims for 3 items."""
    by = {t["name"].split(" (")[0].replace(" futures", ""): t for t in tiles}
    items = []
    if "S&P 500" in by:
        items.append({"tag": "fact", "text": f"The S&P 500 (what VOO and SPY copy) is {by['S&P 500']['chg']:+.2f}% versus the previous close."})
    if "QQQ" in by:
        extra = ""
        if tier:
            extra = " It is above its 20 EMA, which is good for breakouts." if tier["price"] > tier["ema20"] else " It is below its 20 EMA, so be careful."
        items.append({"tag": "fact", "text": f"QQQ (the big tech fund) is {by['QQQ']['chg']:+.2f}%.{extra}"})
    for h in headlines[:2]:
        items.append({"tag": "fact", "text": h})
    if sectors:
        hot = [x for x in sectors if x.get("status") in ("SUPER HOT", "HOT")]
        if hot:
            items.append({"tag": "fact", "text": f"Hottest sector: {hot[0]['name']} ({hot[0]['etf']}), {hot[0]['week']:+.1f}% this week and {hot[0]['month']:+.1f}% this month."})
    if "Russell" in by and "S&P 500" in by:
        d = by["Russell"]["chg"] - by["S&P 500"]["chg"]
        word = "ahead of" if d > 0.3 else "behind" if d < -0.3 else "about even with"
        items.append({"tag": "take", "text": f"Small stocks (Russell 2000, {by['Russell']['chg']:+.2f}%) are {word} the S&P. Small stocks leading means investors feel confident."})
    if "VIX" in by:
        v = float(by["VIX"]["price"].replace(",", ""))
        mood = "calm" if v < 15 else "a little nervous" if v < 20 else "nervous" if v < 30 else "in panic"
        items.append({"tag": "take", "text": f"The VIX (fear gauge) is {v:.1f}, so investors are {mood}."})
    stance, title, line = TIER_TEXT.get(tier["tier"] if tier else "", ("neutral", "Market for your strategy: unknown", "Could not check QQQ right now."))
    return {"stance": stance, "verdict_title": title, "stance_line": line, "summary": items[:3],
            "overview": market_overview(tiles, tier, sectors)}


# ------------------------------ 4. BUILD THE PAGE ------------------------------
def cls(v):
    return "up" if v > 0 else "dn" if v < 0 else "fl"


def bars_html(data, mx):
    out = []
    for name, v in data:
        w = min(abs(v) / mx * 50, 50)
        out.append(f"<div class='row'><div class='lab'>{esc(name)}</div><div class='track'>"
                   f"<div class='bar {'u' if v >= 0 else 'd'}' style='width:{w:.1f}%'></div></div>"
                   f"<div class='val {cls(v)}'>{v:+.2f}%</div></div>")
    return "".join(out)


def bars_meaning(tiles):
    """One or two plain sentences: what the up/down bars mean for the trader."""
    g = lambda p: next((t["chg"] for t in tiles if t["name"].startswith(p)), None)
    sp, nq, ru = g("S&P"), g("QQQ"), g("Russell")
    out = []
    if sp is not None and sp <= -1.0:
        out.append("A broad sell-off. Most breakouts fail on days like this, so be patient.")
    elif sp is not None and sp >= 1.0:
        out.append("A broad rally. This is the kind of day breakouts follow through.")
    if sp is not None and nq is not None:
        if nq < sp - 0.3:
            out.append("Tech (QQQ) is lagging the rest of the market. That makes momentum breakouts harder.")
        elif nq > sp + 0.3:
            out.append("Tech (QQQ) is leading. That is good for momentum stocks.")
    if sp is not None and ru is not None:
        if ru < sp - 0.3:
            out.append("Small stocks are lagging, which means investors are cautious.")
        elif ru > sp + 0.3:
            out.append("Small stocks are leading, a sign investors feel confident.")
    if not out:
        out.append("Nothing unusual. The market is moving together.")
    return " ".join(out)


def pill(call):
    c = str(call).lower()
    return f"<span class='pill {({'bullish': 'bull', 'bearish': 'bear'}).get(c, 'neut')}'>{esc(c.capitalize())}</span>"


LEADER_TAG = ' <span class="pill take">LEADER</span>'


def pct(v):
    return "—" if v is None else f"<span class='{cls(v)}'>{v:+.2f}%</span>"


def build_page(label, now, tiles, sectors, tier, watch, ai, ai_used, yld=None, tile_note="", public=False, recap=None):
    tile_html = "".join(
        f"<div class='tile'><div class='n'>{esc(t['name'])}</div><div class='p'>{esc(t['price'])}</div>"
        f"<div class='c {cls(t['chg'])}'>{t['chg']:+.2f}%</div><div class='d'>{esc(t.get('desc', ''))}</div></div>" for t in tiles)
    bar_data = sorted([(t["name"], t["chg"]) for t in tiles if t["name"] != "VIX"], key=lambda r: r[1], reverse=True)
    mx = max([abs(v) for _, v in bar_data] + [1.0])

    tier_txt = ""
    if tier:
        tier_txt = (f"<p class='note'>QQQ ${tier['price']:.2f} · 20 EMA ${tier['ema20']:.2f} · 50 SMA ${tier['sma50']:.2f} "
                    f"· tier: <b>{tier['tier']}</b></p>")
    summary = "".join(
        f"<li><span class='pill {'fact' if s.get('tag') == 'fact' else 'take'}'>{esc(s.get('tag', 'fact'))}</span> {esc(s.get('text', ''))}</li>"
        for s in ai.get("summary", []))

    sec_html = ""
    if sectors:
        body = "".join(
            f"<tr><td><b>{esc(s['name'])}</b></td><td>{esc(s['etf'])}</td><td>{pct(s.get('today'))}</td>"
            f"<td>{pct(s.get('week'))}</td><td>{pct(s.get('month'))}</td>"
            f"<td><span class='pill {({'SUPER HOT': 'heat-super', 'HOT': 'heat-hot'}).get(s.get('status'), 'heat-no')}'>{esc(s.get('status') or '—')}</span></td></tr>"
            for s in sectors)
        sec_html = ("<section><h2>Sectors, hottest first</h2><div class='panel scroll'><table><thead><tr><th>Sector</th><th>ETF</th>"
                    "<th>Today</th><th>This week</th><th>This month</th><th>Heat</th></tr></thead>"
                    f"<tbody>{body}</tbody></table>"
                    "<p class='note'>A sector is HOT if its ETF is in the top 3 for the week or the month (and positive). SUPER HOT means top 3 for both. "
                    "Trading in a hot sector is what lets you raise your risk.</p></div></section>")

    ranked = ""
    if ai.get("ranked"):
        body = "".join(
            f"<tr><td class='rk'>{i}</td><td>{esc(r.get('what', ''))}</td><td>{esc(r.get('why', ''))}</td>"
            f"<td>{pill(r.get('call', 'neutral'))}</td></tr>"
            for i, r in enumerate(ai["ranked"][:5], 1))
        ranked = ("<section><h2>What matters most</h2><div class='panel scroll'><table><thead><tr>"
                  "<th></th><th>What happened</th><th>Why it matters</th><th>Call</th></tr></thead>"
                  f"<tbody>{body}</tbody></table></div></section>")

    def kv(items, k):
        return "".join(f"<div class='kv'><span><b>{esc(n)}</b></span><span class='{k}'>{esc(w)}</span></div>" for n, w in items)

    movers = ""
    if ai.get("movers_up") or ai.get("movers_down"):
        up = kv([(m.get("name", ""), m.get("why", "")) for m in ai.get("movers_up", [])], "up")
        dn = kv([(m.get("name", ""), m.get("why", "")) for m in ai.get("movers_down", [])], "dn")
        movers = f"<section><h2>Big movers and why</h2><div class='two'><div class='panel'>{up}</div><div class='panel'>{dn}</div></div></section>"

    facts = ai.get("rates_facts") or ([{"k": "10-year yield", "v": f"{yld['level']:.2f}% ({yld['bp']:+.1f} basis points)"}] if yld else [])
    facts_html = "".join(f"<div class='kv'><span>{esc(f.get('k', ''))}</span><span>{esc(f.get('v', ''))}</span></div>" for f in facts)
    foryou = "".join(f"<li>{esc(x)}</li>" for x in ai.get("rates_for_you", []))
    rates = ""
    overview_txt = ai.get("overview") or market_overview(tiles, tier, sectors)
    overview_html = "<section><h2>What is going on</h2><div class='panel'><p style='margin:0'>" + esc(overview_txt) + "</p></div></section>"

    world_html = ""
    if ai.get("world"):
        body = "".join(f"<tr><td><b>{esc(w.get('where', ''))}</b></td><td>{esc(w.get('what', ''))}</td><td>{esc(w.get('effect', ''))}</td></tr>"
                       for w in ai["world"][:7])
        world_html = ("<section><h2>Around the world</h2><div class='panel scroll'><table><thead><tr><th>Where</th><th>What is happening</th>"
                      f"<th>What it could mean for markets and you</th></tr></thead><tbody>{body}</tbody></table>"
                      + (f"<p class='note'>{esc(ai['world_note'])}</p>" if ai.get("world_note") else "") + "</div></section>")
    elif not ai_used:
        world_html = ("<section><h2>Around the world</h2><div class='panel'><p class='note'>World news and plain-English explanations "
                      "show up here when the AI writer is turned on.</p></div></section>")

    def earn_table(rows, title):
        if not rows:
            return ""
        body = "".join(f"<tr><td>{esc(e.get('date', ''))}</td><td><b>{esc(e.get('company', ''))}</b><br><span class='note'>{esc(e.get('ticker', ''))}</span></td>"
                       f"<td>{esc(e.get('know', ''))}</td><td class='up'>{esc(e.get('good', ''))}</td><td class='dn'>{esc(e.get('bad', ''))}</td></tr>"
                       for e in rows[:8])
        return (f"<h3 style='margin:14px 0 6px'>{esc(title)}</h3><table><thead><tr><th>Date</th><th>Company</th><th>What to know</th>"
                f"<th>If it is good</th><th>If it is bad</th></tr></thead><tbody>{body}</tbody></table>")

    earn_html = ""
    etw, enw = ai.get("earnings_this_week") or [], ai.get("earnings_next_week") or []
    if etw or enw:
        earn_html = ("<section><h2>Earnings: this week and next</h2><div class='panel scroll'>"
                     + earn_table(etw, "This week") + earn_table(enw, "Next week")
                     + "<p class='note'>Earnings = a company reporting how much it sold and earned. Big companies move the whole market, "
                       "and what they say about the future (guidance) often matters more than the past quarter.</p></div></section>")

    cats = ""
    if ai.get("catalysts"):
        body = "".join(f"<tr><td>{esc(c.get('date', ''))}</td><td>{esc(c.get('event', ''))}</td><td>{esc(c.get('why', ''))}</td></tr>"
                       for c in ai["catalysts"])
        cats = ("<section><h2>Coming up in the next 1-2 weeks</h2><div class='panel scroll'><table><thead><tr><th>Date</th><th>Event</th>"
                f"<th>Why you care</th></tr></thead><tbody>{body}</tbody></table></div></section>")

    ran_txt = watch["ran"].strftime("%a %b %d at %I:%M %p ET") if (watch and watch.get("ran")) else "at an unknown time"
    if public and not PUBLIC_SHOW_WATCHLIST:
        wl = "<div class='panel'><p>The watchlist is private on the public website.</p></div>"
    elif watch and watch["rows"]:
        def away(r):
            return "-" if not r.get("price") else f"{(r['level'] / r['price'] - 1) * 100:+.1f}%"
        def mv(v):
            return "-" if v is None else f"<span class='{cls(v)}'>{v:+.1f}%</span>"
        body = "".join(
            f"<tr><td><b>{esc(r['ticker'])}</b>{LEADER_TAG if r['leader'] else ''}</td>"
            f"<td>{'-' if r.get('score') is None else format(r['score'], 'g') + '/10'}</td>"
            f"<td>${r['level']:,.2f}</td><td>{away(r)}</td>"
            f"<td>{esc(r['sector'])}<br><span class='note'>{esc(r['status'])}</span></td>"
            f"<td>{mv(r.get('sec_week'))}</td><td>{mv(r.get('sec_month'))}</td>"
            f"<td>{esc(r.get('notes') or '-')}</td></tr>"
            for r in watch["rows"])
        wl = (f"<div class='panel scroll'><table><thead><tr><th>Ticker</th><th>Score</th><th>Breakout level (PDH)</th><th>To level</th>"
              f"<th>Sector / heat</th><th>Sector week</th><th>Sector month</th><th>Notes</th>"
              f"</tr></thead><tbody>{body}</tbody></table>"
              f"<p class='note'>A+ setups only ({len(watch['rows'])}), scanner last ran {ran_txt}. Score is out of 10. PDH = previous day high, your breakout level; "
              f"'To level' is how far the price still has to climb. Compare each stock's sector week and month to its own move. "
              f"When one breaks, the breakout alert on your phone gives shares, entry, stop loss and risk.</p></div>")
    elif watch is not None:
        wl = ("<div class='panel'><p>No A+ setups from the last scan. That is normal. No trade beats a forced trade.</p>"
              f"<p class='note'>Scanner last ran {ran_txt}.</p></div>")
    else:
        wl = ("<div class='panel'><p>No scan results found. Run <code>python3 breakout_scanner.py</code> first, "
              "then run this again.</p></div>")

    if watch and watch.get("near") and not (public and not PUBLIC_SHOW_WATCHLIST):
        nrows = ""
        for r in watch["near"]:
            lvl = "-" if r["level"] != r["level"] else "$" + format(r["level"], ",.2f")
            sc = "-" if r["score"] is None else format(r["score"], "g") + "/10"
            nrows += ("<tr><td><b>" + esc(r["ticker"]) + "</b></td><td>" + sc + "</td><td>" + lvl + "</td><td>"
                      + esc(r["sector"]) + "<br><span class='note'>" + esc(r["status"]) + "</span></td><td>"
                      + esc(r["why"] or "-") + "</td></tr>")
        wl += ("<div class='panel scroll' style='margin-top:12px'><p><b>Close, but NOT A+</b> (watch only, no phone alerts)</p>"
               "<table><thead><tr><th>Ticker</th><th>Score</th><th>Breakout level</th><th>Sector / heat</th><th>Score came from</th></tr></thead>"
               "<tbody>" + nrows + "</tbody></table></div>")

    logo_html = ""
    try:
        import base64
        with open(LOGO_FILE, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        mime = "image/jpeg" if LOGO_FILE.lower().endswith((".jpg", ".jpeg")) else "image/png"
        logo_html = '<img src="data:' + mime + ';base64,' + b64 + '" alt="Fortuno Trading" style="height:140px;max-width:100%;width:auto;margin:0 0 12px -6px;display:block;mix-blend-mode:lighten;background:#0a0a0a">'
    except Exception:
        logo_html = ""

    ai_note = ("Analysis written by Claude from web search. It can be wrong, so check anything you act on."
               if ai_used else "No AI analysis this run (no API key, or it was turned off): numbers and headlines only.")
    vtitle = ai.get("verdict_title") or "Market for your strategy"
    recap_html = ""
    if recap:
        recap_html = ("<section><h2>Since the premarket brief</h2><div class='panel'><ul class='sum'>"
                      + "".join(f"<li>{esc(x)}</li>" for x in recap) + "</ul></div></section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fortuno Markets AI</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style></head><body><main>
<header>{logo_html}<div class="eyebrow">{esc(now.strftime('%A, %B %d, %Y · %I:%M %p ET'))}</div>
<h1>Fortuno <span>Markets AI</span></h1>
<div class="verdict"><span class="pill brief">{esc(label)}</span><div><b>{esc(vtitle)}</b><p>{esc(ai.get('stance_line', ''))}</p>{tier_txt}</div></div></header>
{overview_html}
<section><h2>The 3 things to know</h2><ul class="sum">{summary}</ul>
<p class="note"><span class="pill fact">fact</span> = reported data. <span class="pill take">take</span> = interpretation.</p></section>
{recap_html}
<section><h2>Scoreboard</h2><div class="tiles">{tile_html}</div>
<p class="note">{esc(tile_note or 'Change is versus the previous close.')}</p></section>
<section><h2>Who is up, who is down</h2><div class="panel"><div class="bars">{bars_html(bar_data, mx)}</div>
<p class="note"><b>What this means for you:</b> {esc(bars_meaning(tiles))} You want the S&amp;P, QQQ and small stocks all green before pressing your risk.</p></div></section>
{sec_html}{ranked}{world_html}{movers}{earn_html}{rates}{cats}
<section><h2>Fortuno watchlist</h2>{wl}</section>
<section class="sources">{esc(ai_note)} Not financial advice.</section>
</main></body></html>"""


# ------------------------------ 4b. "WHAT CHANGED SINCE THE PREMARKET BRIEF" ------------------------------
STATE_FILE = "brief_state.json"


def save_state(now, tiles, tier):
    """The Premarket Brief saves a few numbers so the After-Close Brief can compare."""
    try:
        g = lambda p: next((t["chg"] for t in tiles if t["name"].startswith(p)), None)
        with open(STATE_FILE, "w") as f:
            json.dump({"date": f"{now:%Y-%m-%d}", "tier": tier["tier"] if tier else None, "sp_fut": g("S&P")}, f)
    except Exception:
        pass


def broke_levels(levels):
    """Which watchlist stocks traded above their breakout level on the last session."""
    try:
        tks = list(levels)[:60]
        df = yf.download(tks, period="5d", group_by="ticker", progress=False, auto_adjust=True, threads=True)
        out = []
        for tk in tks:
            try:
                sub = df[tk] if tk in df.columns.get_level_values(0) else df
                high = float(sub["High"].dropna().iloc[-1])
                if high > levels[tk]:
                    out.append(f"{tk} (high ${high:,.2f}, level ${levels[tk]:,.2f})")
            except Exception:
                continue
        return out
    except Exception:
        return None


def make_recap(now, tiles, tier, watch):
    items = []
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        if st.get("date") != f"{now:%Y-%m-%d}":
            st = None
    except Exception:
        st = None
    sp = next((t["chg"] for t in tiles if t["name"].startswith("S&P")), None)
    if st:
        if st.get("tier") and tier:
            if st["tier"] == tier["tier"]:
                items.append(f"Your market rule did not change: still {tier['tier']}.")
            else:
                items.append(f"Your market rule changed during the day: {st['tier']} this morning, {tier['tier']} now.")
        if st.get("sp_fut") is not None and sp is not None:
            diff = sp - st["sp_fut"]
            verdict = ("Stocks did better than the morning pointed to." if diff > 0.5
                       else "Stocks did worse than the morning pointed to." if diff < -0.5 else "About what the morning pointed to.")
            items.append(f"Before the open, S&P futures were {st['sp_fut']:+.2f}%. The S&P finished {sp:+.2f}%. {verdict}")
    else:
        items.append("No Premarket Brief was saved today, so this only covers how the day ended.")
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if watch and watch.get("levels") and watch.get("ran") and watch["ran"] < open_time:
        broke = broke_levels(watch["levels"])
        if broke is not None:
            items.append("Watchlist stocks that traded above their breakout level: " + (", ".join(broke) if broke else "none."))
    return items


# ------------------------------ 5. PHONE ALERT ------------------------------
PLAIN = {"GOOD": "Good market for breakouts", "NEUTRAL": "Be careful, smaller size", "BAD": "Poor market, stay small or flat"}


def push(label, tiles, tier, watch, ai, click_url=""):
    if NTFY_TOPIC.startswith("PASTE"):
        print("  (phone alert skipped: put your topic name in NTFY_TOPIC at the top of this file)")
        return
    lines = []
    if tier:
        lines.append(f"Market: {tier['tier']} - {PLAIN[tier['tier']]}")
    else:
        lines.append("Market: " + str(ai.get("stance", "unknown")).capitalize())
    sp = next((t for t in tiles if t["name"].startswith("S&P")), None)
    if sp:
        lines.append(f"{sp['name']} {sp['chg']:+.1f}%")
    if watch and watch["rows"]:
        lines.append(f"{watch['total']} setups to watch: " + ", ".join(r["ticker"] for r in watch["rows"][:4]))
    lines.append("Tap for the full report" if click_url else "Full report saved as market_brief.html on your Mac")
    headers = {"Title": label.encode("ascii", "ignore").decode(), "Priority": "default"}
    if click_url:
        headers["Click"] = click_url
    try:
        r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data="\n".join(lines).encode("utf-8"), headers=headers, timeout=15)
        print("  Phone alert sent." if r.ok else f"  Phone alert problem: {r.status_code} {r.text}")
    except Exception as e:
        print(f"  Phone alert failed: {e}")


# ------------------------------ 6. PUBLISH TO YOUR WEBSITE ------------------------------
def publish_to_github(page_html, now):
    """Uploads the page as index.html to your GitHub repo. GitHub Pages then shows it as your website."""
    try:
        token = os.environ.get("GITHUB_TOKEN", "").strip() or open(TOKEN_FILE).read().strip()
    except Exception:
        print(f"  (website upload skipped: no {TOKEN_FILE} file found)")
        return ""
    api = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/index.html"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        r = requests.get(api, headers=headers, timeout=20)
        sha = r.json().get("sha") if r.status_code == 200 else None
        if r.status_code not in (200, 404):
            print(f"  Website upload problem: {r.status_code} {r.text[:200]}")
            return ""
        body = {"message": f"Brief {now:%Y-%m-%d %H:%M}", "branch": "main",
                "content": base64.b64encode(page_html.encode("utf-8")).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(api, headers=headers, json=body, timeout=30)
        if r.status_code in (200, 201):
            url = f"https://{GITHUB_USER.lower()}.github.io/{GITHUB_REPO}/"
            print(f"  Website updated: {url}  (can take about a minute to show)")
            return url
        print(f"  Website upload problem: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  Website upload failed: {e}")
    return ""


# ------------------------------ MAIN ------------------------------
def flag_time(args, name):
    """--not-before 08:55 / --publish-at 09:00 (New York time) -> datetime.time or None."""
    if name in args:
        try:
            h, m = args[args.index(name) + 1].split(":")
            return dt.time(int(h), int(m))
        except Exception:
            print(f"  (could not read {name}; use it like {name} 09:00)")
    return None


def wait_until(t, why, max_minutes=120):
    """Sleeps until clock time t (New York). Skips the wait if t already passed or is far away."""
    if t is None:
        return
    now = dt.datetime.now(TZ)
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    secs = (target - now).total_seconds()
    if 0 < secs <= max_minutes * 60:
        print(f"  Waiting until {t:%I:%M %p} ET for {why} ({secs / 60:.0f} min)...", flush=True)
        time.sleep(secs)


def main():
    args = sys.argv[1:]
    now = dt.datetime.now(TZ)
    evening = "--evening" in args or ("--morning" not in args and now.hour >= 12)
    label = "After-Close Brief" if evening else "Premarket Brief"
    if "--scan" in args:
        print("Running your scanner first (this takes a few minutes)...")
        import subprocess
        subprocess.run([sys.executable, "breakout_scanner.py"], check=False)
    not_before, publish_at = flag_time(args, "--not-before"), flag_time(args, "--publish-at")
    wait_until(not_before, "fresh data")
    print(f"{label} - getting data...")

    tiles = get_tiles(premarket=not evening)
    if not tiles:
        print("Could not get any prices (Yahoo may be rate-limiting). Wait a minute and try again.")
        return
    yld, sectors, tier, watch = None, get_sectors(), qqq_tier(), get_watchlist()

    global USE_AI
    if "--no-ai" in args:
        USE_AI = False
    ai = ai_analysis(label, tiles, tier, yld)
    ai_used = ai is not None
    if ai is None:
        ai = fallback_ai(tiles, tier, get_headlines(), yld, sectors)
    if not (ai.get("earnings_this_week") or ai.get("earnings_next_week")):
        ai.update(earnings_fallback())
    if not ai.get("world"):
        ai["world"] = world_fallback()
        ai["world_note"] = "Headlines come from public news sites (BBC, France 24, DW, The Guardian, Al Jazeera, CNBC). The 'what it means' lines are general rules, not written about each story."
    note = ("Index numbers are FUTURES (they trade before the open); change is versus the last close." if not evening
            else "Change is today's move versus yesterday's close.")

    recap = None
    if evening:
        recap = make_recap(now, tiles, tier, watch)
    else:
        save_state(now, tiles, tier)

    wait_until(publish_at, "the planned send time")
    now = dt.datetime.now(TZ)                      # the page shows the real send time
    page = build_page(label, now, tiles, sectors, tier, watch, ai, ai_used, yld, note, recap=recap)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(page)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    arch = os.path.join(ARCHIVE_DIR, f"brief_{now:%Y-%m-%d}_{'aftermarket' if evening else 'premarket'}.html")
    with open(arch, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"  Saved {OUT_HTML} (and a copy in {ARCHIVE_DIR}/). Right-click it in VS Code > Reveal in Finder > double-click.")

    url = ""
    if "--no-web" not in args:
        public_page = build_page(label, now, tiles, sectors, tier, watch, ai, ai_used, yld, note, public=True, recap=recap)
        url = publish_to_github(public_page, now)

    if "--no-push" not in args:
        push(label, tiles, tier, watch, ai, url)


if __name__ == "__main__":
    main()
