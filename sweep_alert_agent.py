#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sweep_alert_agent.py — HYBRID SMC-MACRO TRADING BOT (v1.0)
==========================================================
7-layer Smart-Money-Concepts + Macro alert pipeline for Gold (XAUUSD) and the
US index CFDs (US500 / US100 / US30). Implements the "HYBRID SMC-MACRO TRADING
BOT — Technical Specification v1.0".

THE LIVE ALERT BOT IS THIS ONE FILE. The strategy/, agents/, execution/ folders
and main.py / main_live.py are dead code — nothing this bot runs at runtime
imports them. All signal logic belongs in this file.

Operating mode: ALERT_ONLY. The bot analyses the market and sends a Telegram
signal; it NEVER places an order. SEMI_AUTO / FULL_AUTO real-order execution,
the inbound Telegram command interface (/status, /stats, …), the trade-outcome
P&L database, and the loss circuit-breakers are execution-tier and intentionally
deferred until this alert pipeline is validated on ~30 setups (per the spec's
own rollout checklist).

Pipeline (each layer must pass before the next; a failure aborts the scan for
that instrument):
  Layer 1  Macro Filter        — news (±60m HIGH) · DXY bias (XAUUSD) · VIX
  Layer 2  HTF Structure Bias  — Weekly → Daily swings, BOS / CHOCH
  Layer 3  Liquidity Mapping   — 4H BSL / SSL pools, nearest unmitigated draw
  Layer 4  POI Detection+Score — 4H Order Block / FVG / Breaker, graded A+/A/B/C
  Layer 5  LTF Entry Signal    — 15M sweep → 15M CHOCH → 5M FVG 50% entry
  Layer 6  Risk Validation     — SL beyond OB, TP1/TP2 liquidity, RR gate
  Layer 7  Checklist Gate      — 10 conditions, 5 critical, >= 8/10

Run:  python sweep_alert_agent.py [--test | --once]
Env:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
      CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD
Deps: pip install pandas requests
"""

import os, sys, csv, json, time, traceback, threading, base64
from datetime import datetime, timedelta, timezone
import requests, pandas as pd

# =====================================================================================
# CONFIG
# =====================================================================================

# Per-instrument configuration. XAUUSD is the only instrument the DXY bias filter
# applies to (indices skip it, per spec 2.2). pip / pip_value drive the informational
# SL-in-pips and lot-size figures shown in the alert (ALERT_ONLY — never an order).
INSTRUMENTS = {
    "XAUUSD": {"epic": "GOLD",  "pip": 0.01, "pip_value": 0.01, "dxy_filter": True},
    "US500":  {"epic": "US500", "pip": 1.0,  "pip_value": 0.01, "dxy_filter": False},
    "US100":  {"epic": "US100", "pip": 1.0,  "pip_value": 0.01, "dxy_filter": False},
    "US30":   {"epic": "US30",  "pip": 1.0,  "pip_value": 0.01, "dxy_filter": False},
}

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "5000"))
RISK_PCT        = 0.01     # 1% risk per trade (spec 7.4)

# Structure / detection parameters (spec sections 3–6)
WEEKLY_LOOKBACK   = 20     # weekly candles for swing structure
DAILY_LOOKBACK    = 40     # daily candles for swing structure
LIQ_LOOKBACK_4H   = 60     # 4H candles for liquidity pools
SWING_L = 2; SWING_R = 2   # fractal: 2 left + 2 right
SWING_MIN_SEP     = 3      # min candles between accepted swings
EQUAL_LEVEL_TOL   = 0.001  # 0.1% — equal highs/lows grouping
POI_NEAR_PCT      = 0.003  # price within 0.3% of a POI arms Layer 5
LIQ_NEAR_PCT      = 0.002  # 0.2% — POI "near liquidity" confluence
FIB_LOW, FIB_HIGH = 0.50, 0.705   # Fib confluence band (spec 5.2)
IMPULSE_MIN       = 3      # a "strong move" is >= 3 candles
CHOCH_MAX_CANDLES = 5      # 15M CHOCH must occur within 5 candles of the sweep
FVG_RETRACE_MAX   = 8      # 5M candles to retrace into the FVG (order expiry proxy)
POI_STALE_CANDLES = 20     # 4H POI older than this with no interaction → grade -1

# Risk / RR (spec 7.2–7.3)
SL_BUFFER_PIPS = 4         # 3–5 pips beyond the OB / FVG boundary (mid of range)
RR_TP1_MIN     = 1.5
RR_TP2_MIN     = 2.5

# Macro thresholds (spec 2 + tables 2/3)
NEWS_IMPACT_CCY   = ("USD", "EUR", "GBP")
NEWS_WINDOW_MIN   = 60     # ±60 minutes around a HIGH-impact event
DXY_EMA           = 20
DXY_NEUTRAL_PCT   = 0.003  # within 0.3% of the 20-EMA → NEUTRAL
VIX_CAUTION       = 20     # 20–25 → lot ×0.75
VIX_HIGH          = 25     # >25  → lot ×0.50
VIX_HALT          = 35     # >35  → halt new entries

# Grades that proceed to Layer 5 entry search
PROCEED_GRADES = ("A", "A+")

DEDUP_HOURS       = 4      # don't re-alert the same fingerprint within 4H (spec 13 notes)
MAX_PER_INSTR_DAY = 1      # spec 9.3 exposure limit (informational in ALERT_ONLY)
SCAN_EVERY_MIN    = 5      # LTF cadence (spec 11.2)

STATE_FILE   = "smc_state.json"
SIGNALS_CSV  = "smc_signals_log.csv"
DUBAI        = timezone(timedelta(hours=4))

# Capital.com epics for the macro symbols (best-effort; degrade gracefully if absent)
DXY_EPIC = os.getenv("DXY_EPIC", "DXY")
VIX_EPIC = os.getenv("VIX_EPIC", "VIX")

# =====================================================================================
# Capital.com data layer (shared singleton session)  [preserved infra]
# =====================================================================================

_CAP_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_CAP_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_CAP_TIMEOUT   = 15
_CAP_PING_INT  = 8 * 60

class _CapSession:
    def __init__(self):
        self._base = ""; self._cst = ""; self._token = ""
        self._lock = threading.Lock(); self._started = False

    def init(self):
        with self._lock:
            if self._cst: return
            use_demo = os.getenv("CAPITAL_DEMO", "").lower() == "true"
            self._base = _CAP_DEMO_BASE if use_demo else _CAP_LIVE_BASE
            self._login()
            if not self._started:
                threading.Thread(target=self._keepalive, daemon=True).start()
                self._started = True

    def _login(self):
        api_key    = os.getenv("CAPITAL_API_KEY", "").strip()
        identifier = os.getenv("CAPITAL_IDENTIFIER", "").strip()
        password   = os.getenv("CAPITAL_PASSWORD", "").strip()
        r = requests.post(f"{self._base}/session",
            headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
            json={"identifier": identifier, "password": password, "encryptedPassword": False},
            timeout=_CAP_TIMEOUT)
        r.raise_for_status()
        self._cst   = r.headers["CST"]
        self._token = r.headers["X-SECURITY-TOKEN"]

    def _headers(self):
        with self._lock:
            return {"CST": self._cst, "X-SECURITY-TOKEN": self._token,
                    "Content-Type": "application/json"}

    def get(self, path, params=None):
        r = requests.get(f"{self._base}{path}", headers=self._headers(),
                         params=params, timeout=_CAP_TIMEOUT)
        if r.status_code == 401:
            with self._lock: self._login()
            r = requests.get(f"{self._base}{path}", headers=self._headers(),
                             params=params, timeout=_CAP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _keepalive(self):
        while True:
            time.sleep(_CAP_PING_INT)
            try: requests.get(f"{self._base}/ping", headers=self._headers(), timeout=_CAP_TIMEOUT)
            except: pass

_cap = _CapSession()
_cap_ready = False

def _ensure_capital():
    global _cap_ready
    if _cap_ready: return True
    key = os.getenv("CAPITAL_API_KEY", "").strip()
    if not key: return False
    try:
        _cap.init(); _cap_ready = True; return True
    except Exception as e:
        print(f"[Capital.com] login failed: {e}", flush=True); return False

# All timeframes the pipeline needs (spec 11.2).
_CAP_RESOLUTION = {
    "5m": "MINUTE_5", "15m": "MINUTE_15", "1h": "HOUR",
    "4h": "HOUR_4",   "1d": "DAY",        "1w": "WEEK",
}

def _cap_fetch(epic, resolution, count):
    """Fetch OHLC candles, oldest-first. Capital.com does NOT reliably return prices
    in order and its snapshotTime is server-local — use snapshotTimeUTC and sort by
    the real candle time (spec 12 / known candle-ordering bug)."""
    data = _cap.get(f"/prices/{epic}",
                    params={"resolution": _CAP_RESOLUTION[resolution], "max": count})
    rows = []
    for p in data.get("prices", []):
        def mid(s):
            b = s.get("bid") or 0; a = s.get("ask") or 0
            return (float(b) + float(a)) / 2 if b and a else float(b or a or 0)
        try:
            ts = pd.Timestamp(p.get("snapshotTimeUTC") or p["snapshotTime"], tz="UTC")
            rows.append({"ts": ts,
                         "Open": mid(p["openPrice"]), "High": mid(p["highPrice"]),
                         "Low": mid(p["lowPrice"]),  "Close": mid(p["closePrice"])})
        except Exception:
            pass
    if not rows: return None
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df

# =====================================================================================
# Utilities + Telegram  [preserved infra]
# =====================================================================================

def now_utc(): return datetime.now(timezone.utc)
def log(msg): print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}", flush=True)
def fmt_both(dt): return f"{dt.strftime('%H:%M')} UTC ({dt.astimezone(DUBAI).strftime('%H:%M')} Dubai)"

def is_market_closed(now):
    """Closed on the weekend window (Fri 21:00 UTC → Sun 22:00 UTC) and during the
    daily maintenance close (21:00–22:00 UTC)."""
    wd = now.weekday(); hm = now.hour * 60 + now.minute
    if 21 * 60 <= hm < 22 * 60: return True   # daily close
    if wd == 5: return True                    # Saturday
    if wd == 4 and hm >= 21 * 60: return True  # Friday after 21:00
    if wd == 6 and hm < 22 * 60: return True   # Sunday before 22:00
    return False

def tg_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        log("Telegram env vars missing."); print(text, flush=True); return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                          timeout=15)
        return r.status_code == 200
    except requests.RequestException:
        return False

# =====================================================================================
# State  (dedup fingerprints, per-day counts, macro cache)
# =====================================================================================

def default_state(today_str):
    return {"date": today_str, "signals_sent": 0, "per_instrument": {},
            "fingerprints": {}, "macro": None, "macro_ts": None}

def load_state():
    today = now_utc().date().isoformat()
    try:
        with open(STATE_FILE, "r") as f: st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_state(today)
    if st.get("date") != today:
        # New day: reset counters but keep macro cache (it re-fetches on staleness).
        ns = default_state(today)
        ns["macro"] = st.get("macro"); ns["macro_ts"] = st.get("macro_ts")
        # keep only fingerprints still inside the dedup window
        cutoff = now_utc() - timedelta(hours=DEDUP_HOURS)
        ns["fingerprints"] = {k: v for k, v in st.get("fingerprints", {}).items()
                              if _parse_ts(v) and _parse_ts(v) > cutoff}
        return ns
    return st

def save_state(st):
    try:
        with open(STATE_FILE, "w") as f: json.dump(st, f, indent=2)
    except OSError: pass

def _parse_ts(s):
    if not s: return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

# =====================================================================================
# Indicators / structure primitives
# =====================================================================================

def ema(series, n): return series.ewm(span=n, adjust=False).mean()

def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])

def swing_points(df, left=SWING_L, right=SWING_R, min_sep=SWING_MIN_SEP):
    """Return (swing_highs, swing_lows) as lists of (idx, price), oldest-first.
    A swing high's HIGH is strictly greater than `left` bars before and `right` after
    (mirror for lows). Accepted swings of the same type are kept >= min_sep apart."""
    H = df["High"].values; L = df["Low"].values; n = len(df)
    highs, lows = [], []
    for i in range(left, n - right):
        window_h = H[i - left:i + right + 1]
        window_l = L[i - left:i + right + 1]
        if H[i] == window_h.max() and (window_h == H[i]).sum() == 1:
            if not highs or (i - highs[-1][0]) >= min_sep:
                highs.append((i, float(H[i])))
        if L[i] == window_l.min() and (window_l == L[i]).sum() == 1:
            if not lows or (i - lows[-1][0]) >= min_sep:
                lows.append((i, float(L[i])))
    return highs, lows

def structure_bias(df, lookback):
    """Table 4: HH+HL → BULLISH, LH+LL → BEARISH, else NEUTRAL. Needs >=2 of each swing."""
    seg = df.tail(lookback)
    highs, lows = swing_points(seg)
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL", highs, lows
    hh = highs[-1][1] > highs[-2][1]; hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]; ll = lows[-1][1] < lows[-2][1]
    if hh and hl: return "BULLISH", highs, lows
    if lh and ll: return "BEARISH", highs, lows
    return "NEUTRAL", highs, lows

def bos_choch(df, highs, lows, prior_bias):
    """Detect the most recent BOS / CHOCH on the daily close (spec 3.3).
    Returns (event, level) where event ∈ {BULLISH_BOS,BEARISH_BOS,BULLISH_CHOCH,
    BEARISH_CHOCH,None}. CHOCH flips the working bias."""
    if not highs or not lows: return None, None
    close = float(df["Close"].iloc[-1])
    last_sh = highs[-1][1]; last_sl = lows[-1][1]
    if close > last_sh:
        return ("BULLISH_CHOCH", last_sh) if prior_bias == "BEARISH" else ("BULLISH_BOS", last_sh)
    if close < last_sl:
        return ("BEARISH_CHOCH", last_sl) if prior_bias == "BULLISH" else ("BEARISH_BOS", last_sl)
    return None, None

# =====================================================================================
# Layer 1 — Macro Filter (news · DXY · VIX)
# =====================================================================================

_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"   # Forex Factory JSON mirror

def _fetch_news():
    try:
        r = requests.get(_FF_URL, timeout=_CAP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        out = []
        for e in r.json():
            if str(e.get("impact", "")).lower() != "high": continue
            if str(e.get("country", "")).upper() not in NEWS_IMPACT_CCY: continue
            try:
                dt = datetime.fromisoformat(e.get("date"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                out.append({"title": str(e.get("title", "event")), "time": dt.astimezone(timezone.utc)})
            except Exception:
                pass
        return out
    except Exception:
        return None   # unreachable → treat as no block (fail-open, logged by caller)

def news_block(now, cached_events):
    """Return the event title if a HIGH-impact USD/EUR/GBP event is within ±60m."""
    for e in (cached_events or []):
        if abs((now - e["time"]).total_seconds()) <= NEWS_WINDOW_MIN * 60:
            return e["title"]
    return None

def dxy_bias(dxy_daily):
    """Table 2: DXY 20-EMA + last-3-close direction → BULLISH_USD / BEARISH_USD / NEUTRAL."""
    if dxy_daily is None or len(dxy_daily) < DXY_EMA + 3:
        return "NEUTRAL"
    e = float(ema(dxy_daily["Close"], DXY_EMA).iloc[-1])
    px = float(dxy_daily["Close"].iloc[-1])
    c = dxy_daily["Close"].values
    asc  = c[-1] > c[-2] > c[-3]
    desc = c[-1] < c[-2] < c[-3]
    if abs(px - e) / e <= DXY_NEUTRAL_PCT: return "NEUTRAL"
    if px > e and asc:  return "BULLISH_USD"
    if px < e and desc: return "BEARISH_USD"
    return "NEUTRAL"

def vix_action(vix_value):
    """Table 3 → (lot_modifier, halt_bool, label)."""
    if vix_value is None:            return 1.0, False, "n/a"
    if vix_value > VIX_HALT:         return 0.0, True,  f"{vix_value:.1f} HALT"
    if vix_value > VIX_HIGH:         return 0.5, False, f"{vix_value:.1f} high"
    if vix_value >= VIX_CAUTION:     return 0.75, False, f"{vix_value:.1f} caution"
    return 1.0, False, f"{vix_value:.1f} normal"

def macro_filter(now, state, use_capital):
    """Layer 1. Cached per session (6h) with on-demand refresh. Returns a dict:
    {news_block, dxy, vix, vix_mod, vix_halt, vix_label}."""
    ts = _parse_ts(state.get("macro_ts"))
    if state.get("macro") and ts and (now - ts) < timedelta(hours=6):
        return state["macro"]

    events = _fetch_news()
    if events is None:
        log("News feed unreachable — proceeding without a news block (fail-open).")
        events = []

    dxy = "NEUTRAL"; vix_val = None
    if use_capital:
        try:
            d = _cap_fetch(DXY_EPIC, "1d", 40)
            dxy = dxy_bias(d)
        except Exception:
            log(f"DXY ({DXY_EPIC}) unavailable — DXY bias = NEUTRAL.")
        try:
            v = _cap_fetch(VIX_EPIC, "1d", 5)
            if v is not None and len(v): vix_val = float(v["Close"].iloc[-1])
        except Exception:
            log(f"VIX ({VIX_EPIC}) unavailable — VIX filter disabled.")

    vix_mod, vix_halt, vix_label = vix_action(vix_val)
    macro = {
        "news_events": [{"title": e["title"], "time": e["time"].isoformat()} for e in events],
        "dxy": dxy, "vix": vix_val, "vix_mod": vix_mod,
        "vix_halt": vix_halt, "vix_label": vix_label,
    }
    state["macro"] = macro; state["macro_ts"] = now.isoformat()
    return macro

def _news_events_from_macro(macro):
    out = []
    for e in macro.get("news_events", []):
        t = _parse_ts(e.get("time"))
        if t: out.append({"title": e["title"], "time": t})
    return out

# =====================================================================================
# Layer 2 — HTF bias (Weekly → Daily, BOS/CHOCH)
# =====================================================================================

def htf_bias(weekly, daily):
    """Combine weekly + daily structure (Table 5). Returns a dict with the resolved
    bias, the tradable side, a confidence penalty, and the stored BOS/CHOCH level."""
    wbias, _, _ = structure_bias(weekly, WEEKLY_LOOKBACK)
    dbias, dh, dl = structure_bias(daily, DAILY_LOOKBACK)
    event, level = bos_choch(daily, dh, dl, dbias)
    # CHOCH flips the working daily bias to the new direction.
    if event == "BULLISH_CHOCH": dbias = "BULLISH"
    elif event == "BEARISH_CHOCH": dbias = "BEARISH"

    penalty = 0; result = "NO_BIAS"; side = None
    if wbias == "BULLISH" and dbias == "BULLISH": result, side = "STRONG_BULL", "long"
    elif wbias == "BEARISH" and dbias == "BEARISH": result, side = "STRONG_BEAR", "short"
    elif wbias == "BULLISH" and dbias == "NEUTRAL": result, side, penalty = "WEAK_BULL", "long", 1
    elif wbias == "BEARISH" and dbias == "NEUTRAL": result, side, penalty = "WEAK_BEAR", "short", 1
    elif {wbias, dbias} == {"BULLISH", "BEARISH"}: result = "CONFLICT"
    return {"weekly": wbias, "daily": dbias, "result": result, "side": side,
            "penalty": penalty, "event": event, "level": level}

# =====================================================================================
# Layer 3 — Liquidity mapping (4H)
# =====================================================================================

def _group_levels(points, tol=EQUAL_LEVEL_TOL):
    """Cluster swing points whose prices are within tol of each other. Returns
    [{level, touches, priority}] with priority by touch count (spec 4.1)."""
    pools = []
    for _, price in points:
        placed = False
        for pool in pools:
            if abs(price - pool["level"]) / pool["level"] <= tol:
                pool["level"] = (pool["level"] * pool["touches"] + price) / (pool["touches"] + 1)
                pool["touches"] += 1; placed = True; break
        if not placed:
            pools.append({"level": price, "touches": 1})
    for p in pools:
        p["priority"] = "HIGH" if p["touches"] >= 3 else "MEDIUM" if p["touches"] == 2 else "LOW"
    return pools

def liquidity_map(df4h):
    """Layer 3. BSL (above, from swing highs) and SSL (below, from swing lows) over the
    last LIQ_LOOKBACK_4H candles. Marks each pool SWEPT if a later candle traded through
    it; the nearest UNMITIGATED pool each side is the directional draw (spec 4.3)."""
    seg = df4h.tail(LIQ_LOOKBACK_4H)
    highs, lows = swing_points(seg)
    bsl = _group_levels(highs); ssl = _group_levels(lows)
    hi = seg["High"].values; lo = seg["Low"].values

    def _swept(level, side):
        # side "bsl": swept if any subsequent HIGH exceeded it; "ssl": any LOW below it.
        arr = hi if side == "bsl" else lo
        return bool((arr > level).any()) if side == "bsl" else bool((arr < level).any())

    # Determine "swept" using the bar AFTER the level formed: approximate by whole-seg
    # extreme beyond the level (a level equal to the running extreme is unmitigated).
    seg_hi = float(seg["High"].max()); seg_lo = float(seg["Low"].min())
    for p in bsl: p["status"] = "UNMITIGATED" if p["level"] >= seg_hi - 1e-9 else "SWEPT"
    for p in ssl: p["status"] = "UNMITIGATED" if p["level"] <= seg_lo + 1e-9 else "SWEPT"
    return {"bsl": bsl, "ssl": ssl}

def nearest_pools(liq, price):
    """Nearest UNMITIGATED BSL above and SSL below the current price."""
    above = [p for p in liq["bsl"] if p["status"] == "UNMITIGATED" and p["level"] > price]
    below = [p for p in liq["ssl"] if p["status"] == "UNMITIGATED" and p["level"] < price]
    nb = min(above, key=lambda p: p["level"] - price) if above else None
    ns = max(below, key=lambda p: p["level"] - price) if below else None
    return nb, ns

def major_daily_target(liq, side):
    """Highest-priority pool in the trade direction — TP2 reference (spec 7.2)."""
    pools = liq["bsl"] if side == "long" else liq["ssl"]
    if not pools: return None
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    return max(pools, key=lambda p: (rank[p["priority"]], p["touches"]))["level"]

# =====================================================================================
# Layer 4 — POI detection & scoring (4H)
# =====================================================================================

def find_order_block(df, side):
    """Last opposite-colour candle before a >=IMPULSE_MIN-candle impulse in `side`'s
    direction. OB zone = that candle's full high–low body (spec table 7)."""
    o = df["Open"].values; c = df["Close"].values
    h = df["High"].values; l = df["Low"].values
    n = len(df)
    for i in range(n - IMPULSE_MIN - 1, 0, -1):
        if side == "long":
            impulse = all(c[i + k] > o[i + k] for k in range(1, IMPULSE_MIN + 1)) and \
                      c[i + IMPULSE_MIN] > h[i]
            if impulse and c[i] < o[i]:   # last down candle before the up-move
                return {"top": float(h[i]), "bottom": float(l[i]), "idx": i}
        else:
            impulse = all(c[i + k] < o[i + k] for k in range(1, IMPULSE_MIN + 1)) and \
                      c[i + IMPULSE_MIN] < l[i]
            if impulse and c[i] > o[i]:   # last up candle before the down-move
                return {"top": float(h[i]), "bottom": float(l[i]), "idx": i}
    return None

def find_fvg(df, side, start=None, end=None):
    """3-candle Fair Value Gap (spec table 7 / 6.3). Bullish: gap between candle-1 HIGH
    and candle-3 LOW; bearish: between candle-1 LOW and candle-3 HIGH. Scans the given
    slice most-recent-first and returns {top, bottom, mid, idx}."""
    h = df["High"].values; l = df["Low"].values
    n = len(df)
    lo_i = 2 if start is None else max(2, start)
    hi_i = n if end is None else min(n, end)
    for i in range(hi_i - 1, lo_i - 1, -1):   # i = candle-3 index
        c1_h, c1_l = h[i - 2], l[i - 2]
        c3_h, c3_l = h[i], l[i]
        if side == "long" and c3_l > c1_h:    # bullish gap
            top, bottom = float(c3_l), float(c1_h)
            return {"top": top, "bottom": bottom, "mid": bottom + (top - bottom) * 0.5, "idx": i}
        if side == "short" and c3_h < c1_l:   # bearish gap
            top, bottom = float(c1_l), float(c3_h)
            return {"top": top, "bottom": bottom, "mid": top - (top - bottom) * 0.5, "idx": i}
    return None

def _overlap_pct(a_top, a_bot, b_top, b_bot):
    inter = max(0.0, min(a_top, b_top) - max(a_bot, b_bot))
    span = max(a_top, b_top) - min(a_bot, b_bot)
    return inter / span if span > 0 else 0.0

def fib_band(df4h, side):
    """0.50–0.705 retracement band of the last major 4H impulse (swing low→high for a
    long, high→low for a short)."""
    seg = df4h.tail(LIQ_LOOKBACK_4H)
    highs, lows = swing_points(seg)
    if not highs or not lows: return None
    sh = highs[-1][1]; sl = lows[-1][1]
    if sh <= sl: return None
    if side == "long":
        return (sh - (sh - sl) * FIB_HIGH, sh - (sh - sl) * FIB_LOW)   # (low_price, high_price)
    return (sl + (sh - sl) * FIB_LOW, sl + (sh - sl) * FIB_HIGH)

def score_poi(df4h, side, ob, fvg, liq, price):
    """Table 8 confluence scoring. Daily-bias alignment is REQUIRED upstream (this POI is
    only built for the biased side). Returns (score, grade, factors)."""
    score = 0.0; factors = []
    if ob:
        score += 1.0; factors.append("OB")
    if ob and fvg and _overlap_pct(ob["top"], ob["bottom"], fvg["top"], fvg["bottom"]) >= 0.30:
        score += 1.0; factors.append("FVG")
    band = fib_band(df4h, side)
    poi_level = ob["mid"] if ob and "mid" in ob else (
        (ob["top"] + ob["bottom"]) / 2 if ob else price)
    if band and band[0] <= poi_level <= band[1]:
        score += 1.0; factors.append("Fib")
    # Untouched: price hasn't reacted inside the OB body since it formed.
    if ob:
        after = df4h.iloc[ob["idx"] + 1:]
        touched = ((after["Low"] <= ob["top"]) & (after["High"] >= ob["bottom"])).any() if len(after) else False
        if not touched:
            score += 0.5; factors.append("Untouched")
    # Near a HIGH-priority pool.
    pools = liq["bsl"] + liq["ssl"]
    if any(p["priority"] == "HIGH" and abs(poi_level - p["level"]) / poi_level <= LIQ_NEAR_PCT for p in pools):
        score += 0.5; factors.append("NearLiq")
    grade = "A+" if score >= 3.0 else "A" if score >= 2.0 else "B" if score >= 1.0 else "C"
    return round(score, 1), grade, factors

# =====================================================================================
# Layer 5 — LTF entry (15M sweep → 15M CHOCH → 5M FVG)
# =====================================================================================

def detect_ltf_sequence(df15, df5, side, poi, ssl_level, bsl_level):
    """Reconstruct Sweep → CHOCH → FVG over recent candles. Returns a dict with the
    entry price + trigger flags, or None if the sequence is incomplete/expired.
    All three steps must fall within the POI zone / cadence limits (spec 6)."""
    h = df15["High"].values; l = df15["Low"].values; c = df15["Close"].values
    n = len(df15)
    ptop, pbot = poi["top"], poi["bottom"]

    # --- Step 1: 15M liquidity sweep, inside the POI zone, in the last ~10 candles ---
    sweep_i = None
    for i in range(n - 1, max(-1, n - 12), -1):
        in_zone = pbot <= c[i] <= ptop
        if side == "long" and ssl_level is not None:
            if l[i] < ssl_level and c[i] > ssl_level and in_zone: sweep_i = i; break
        if side == "short" and bsl_level is not None:
            if h[i] > bsl_level and c[i] < bsl_level and in_zone: sweep_i = i; break
    if sweep_i is None:
        return None

    # --- Step 2: 15M CHOCH within CHOCH_MAX_CANDLES of the sweep (spec 6.2) ---
    # After the sweep, a swing forms in the trade direction; the CHOCH is the candle
    # that CLOSES beyond the last such swing high/low formed after the sweep. We track
    # the running post-sweep extreme (seeded by the sweep candle) and fire when a close
    # breaks it. The breaking candle's high/low becomes the Structure Reference Point.
    choch = False; srp = None
    ref = h[sweep_i] if side == "long" else l[sweep_i]
    end = min(n, sweep_i + 1 + CHOCH_MAX_CANDLES)
    for j in range(sweep_i + 1, end):
        if side == "long":
            if c[j] > ref: choch = True; srp = float(h[j]); break
            ref = max(ref, h[j])
        else:
            if c[j] < ref: choch = True; srp = float(l[j]); break
            ref = min(ref, l[j])
    if not choch:
        return None

    # --- Step 3: 5M FVG in the CHOCH impulse; entry at its 50% ---
    fvg = find_fvg(df5, side, start=max(2, len(df5) - 12))
    if fvg is None:
        return None
    price_now = float(df5["Close"].iloc[-1])
    # FVG must not be fully filled already, and price must still be able to retrace in.
    if side == "long" and price_now < fvg["bottom"]:
        return None
    if side == "short" and price_now > fvg["top"]:
        return None
    return {"sweep_idx": sweep_i, "srp": srp, "fvg": fvg, "entry": fvg["mid"],
            "sweep": True, "choch": True, "fvg_ok": True}

# =====================================================================================
# Layer 6 — Risk (SL / TP / RR / sizing)
# =====================================================================================

def build_risk(side, entry, ob, fvg, liq, price, pip, pip_value, vix_mod):
    """SL beyond the OB body (or FVG if OB-less), TP1 = nearest liquidity draw,
    TP2 = major daily liquidity. Validates RR (spec 7). Returns a dict or None."""
    buf = SL_BUFFER_PIPS * pip
    if ob:
        stop = ob["bottom"] - buf if side == "long" else ob["top"] + buf
    else:
        stop = fvg["bottom"] - buf if side == "long" else fvg["top"] + buf

    # TP1 = nearest unmitigated pool in the DIRECTION of travel (BSL above for a long,
    # SSL below for a short). TP2 = the major daily liquidity target.
    nb, ns = nearest_pools(liq, price)
    tp1 = (nb["level"] if nb else None) if side == "long" else (ns["level"] if ns else None)
    tp2 = major_daily_target(liq, side)

    if tp1 is None or tp2 is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    rr1 = abs(tp1 - entry) / risk
    rr2 = abs(tp2 - entry) / risk
    if rr1 < RR_TP1_MIN or rr2 < RR_TP2_MIN:
        return None

    sl_pips = risk / pip
    risk_amount = ACCOUNT_BALANCE * RISK_PCT * (vix_mod if vix_mod > 0 else 1.0)
    lot = round(risk_amount / (sl_pips * pip_value), 2) if sl_pips > 0 and pip_value > 0 else 0.0
    return {"stop": stop, "tp1": tp1, "tp2": tp2, "rr1": round(rr1, 2), "rr2": round(rr2, 2),
            "sl_pips": round(sl_pips, 1), "lot": lot, "risk_amount": round(risk_amount, 2)}

# =====================================================================================
# Layer 7 — Checklist gate (10 conditions, 5 critical, >= 8/10)
# =====================================================================================

def checklist(instr, side, macro, bias, poi_grade, risk, seq):
    """Returns (passed, score, critical_ok, items). Items 1–5 critical (all must pass);
    total >= 8/10 to send, 7/10 → alert-only note (spec section 8 / table 13)."""
    dxy_ok = True
    if INSTRUMENTS[instr]["dxy_filter"]:
        dxy_ok = (side == "long" and macro["dxy"] != "BULLISH_USD") or \
                 (side == "short" and macro["dxy"] != "BEARISH_USD")
    critical = [
        ("no_news",        macro.get("news_hit") is None),
        ("dxy_aligned",    dxy_ok),
        ("daily_bias",     (side == "long" and bias["daily"] == "BULLISH") or
                           (side == "short" and bias["daily"] == "BEARISH")),
        ("poi_grade",      poi_grade in PROCEED_GRADES),
        ("rr_ok",          risk is not None and risk["rr2"] >= RR_TP2_MIN),
    ]
    standard = [
        ("weekly_bias",    (side == "long" and bias["weekly"] == "BULLISH") or
                           (side == "short" and bias["weekly"] == "BEARISH")),
        ("sweep",          bool(seq and seq.get("sweep"))),
        ("choch",          bool(seq and seq.get("choch"))),
        ("fvg",            bool(seq and seq.get("fvg_ok"))),
        ("vix_ok",         not macro.get("vix_halt")),
    ]
    critical_ok = all(v for _, v in critical)
    score = sum(1 for _, v in critical if v) + sum(1 for _, v in standard if v)
    passed = critical_ok and score >= 8
    return passed, score, critical_ok, critical + standard

# =====================================================================================
# Signal logging + Telegram alert
# =====================================================================================

CSV_FIELDS = ["ts_utc", "instrument", "direction", "grade", "poi_score", "score",
              "entry", "stop", "tp1", "tp2", "rr1", "rr2", "sl_pips", "lot",
              "weekly_bias", "daily_bias", "dxy", "vix", "poi_factors",
              "sweep", "choch", "fvg", "checklist", "fingerprint"]

def log_signal(row):
    new = not os.path.exists(SIGNALS_CSV)
    try:
        with open(SIGNALS_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new: w.writeheader()
            w.writerow(row)
    except Exception:
        pass

def alert_text(instr, side, sig):
    poi_desc = " + ".join(sig["poi_factors"]) if sig["poi_factors"] else "OB only"
    exp = (now_utc() + timedelta(minutes=FVG_RETRACE_MAX * 5))
    return (
        f"🔔 NEW SIGNAL — {instr}\n"
        f"Direction : {'LONG' if side == 'long' else 'SHORT'}\n"
        f"Grade     : {sig['grade']}\n"
        f"Score     : {sig['score']}/10\n"
        f"Entry     : {sig['entry']:.2f}\n"
        f"SL        : {sig['risk']['stop']:.2f}  ({sig['risk']['sl_pips']:.0f} pips)\n"
        f"TP1       : {sig['risk']['tp1']:.2f}  (RR {sig['risk']['rr1']:.1f}:1)\n"
        f"TP2       : {sig['risk']['tp2']:.2f}  (RR {sig['risk']['rr2']:.1f}:1)\n"
        f"Lot Size  : {sig['risk']['lot']:.2f}\n"
        f"Bias      : {sig['bias']['weekly']} + {sig['bias']['daily']}\n"
        f"POI       : {poi_desc}\n"
        f"Trigger   : Sweep ✓ | CHOCH ✓ | FVG entry ✓\n"
        f"DXY       : {sig['macro']['dxy']}\n"
        f"VIX       : {sig['macro']['vix_label']}\n"
        f"⏰ Order expires ~{fmt_both(exp)} (8×5M)\n"
        f"[ALERT_ONLY — no order placed]"
    )

# =====================================================================================
# Per-instrument scan (Layers 1→7)
# =====================================================================================

def scan_instrument(instr, cfg, now, macro, state):
    """Run the full pipeline for one instrument. Returns a signal dict to alert, or None.
    Logs the layer reached on abort."""
    epic = cfg["epic"]
    try:
        weekly = _cap_fetch(epic, "1w", WEEKLY_LOOKBACK + 10)
        daily  = _cap_fetch(epic, "1d", DAILY_LOOKBACK + 10)
        df4h   = _cap_fetch(epic, "4h", LIQ_LOOKBACK_4H + 10)
        df15   = _cap_fetch(epic, "15m", 60)
        df5    = _cap_fetch(epic, "5m", 60)
    except Exception as e:
        log(f"{instr}: data fetch error: {e}"); return None
    if any(x is None or len(x) < 6 for x in (weekly, daily, df4h, df15, df5)):
        log(f"{instr}: insufficient candles"); return None

    price = float(df5["Close"].iloc[-1])

    # Layer 2 — HTF bias
    bias = htf_bias(weekly, daily)
    if bias["side"] is None:
        log(f"{instr}: L2 bias {bias['result']} — skip"); return None
    side = bias["side"]

    # Layer 1 detail that depends on side — DXY veto for XAUUSD (critical item 2)
    macro_local = dict(macro)
    macro_local["news_hit"] = news_block(now, _news_events_from_macro(macro))

    # Layer 3 — liquidity
    liq = liquidity_map(df4h)
    nb, ns = nearest_pools(liq, price)

    # Layer 4 — POI detection + scoring on 4H, on the biased side
    ob  = find_order_block(df4h, side)
    fvg4 = find_fvg(df4h, side)
    if ob is None and fvg4 is None:
        log(f"{instr}: L4 no POI"); return None
    poi_zone = ob if ob else fvg4
    poi_score, grade, factors = score_poi(df4h, side, ob, fvg4, liq, price)
    if grade not in PROCEED_GRADES:
        log(f"{instr}: L4 POI grade {grade} ({poi_score}) — no entry"); return None

    # Price must be within POI_NEAR_PCT of the POI zone to arm Layer 5
    zmid = (poi_zone["top"] + poi_zone["bottom"]) / 2
    if abs(price - zmid) / zmid > POI_NEAR_PCT:
        log(f"{instr}: L5 price not at POI ({grade}) — waiting"); return None

    # Layer 5 — LTF entry sequence
    seq = detect_ltf_sequence(df15, df5, side, poi_zone,
                              ns["level"] if ns else None,
                              nb["level"] if nb else None)
    if seq is None:
        log(f"{instr}: L5 sequence incomplete"); return None
    entry = seq["entry"]

    # Layer 6 — risk
    risk = build_risk(side, entry, ob, seq["fvg"], liq, price,
                      cfg["pip"], cfg["pip_value"], macro_local.get("vix_mod", 1.0))
    if risk is None:
        log(f"{instr}: L6 RR/target validation failed"); return None

    # Layer 7 — checklist gate
    passed, score, critical_ok, items = checklist(instr, side, macro_local, bias, grade, risk, seq)
    if not passed:
        log(f"{instr}: L7 checklist {score}/10 critical_ok={critical_ok} — abort"); return None

    return {"instrument": instr, "side": side, "entry": entry, "grade": grade,
            "poi_score": poi_score, "poi_factors": factors, "score": score,
            "risk": risk, "bias": bias, "macro": macro_local, "seq": seq,
            "checklist": items,
            "fingerprint": f"{instr}:{side}:{round(zmid, 2)}"}

# =====================================================================================
# GitHub status push (dashboard) — preserved infra
# =====================================================================================

STATUS_BRANCH = "status"
STATUS_FILE   = "bot_status.json"

def read_recent_signals(n=10):
    rows = []
    try:
        with open(SIGNALS_CSV, "r") as f:
            for row in csv.DictReader(f):
                rows.append({k: row.get(k, "") for k in
                             ("ts_utc", "instrument", "direction", "grade", "score",
                              "entry", "stop", "tp1", "tp2")})
    except Exception:
        pass
    return rows[-n:]

def push_status_json(state, now):
    token = os.getenv("GITHUB_TOKEN"); repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo: return
    macro = state.get("macro") or {}
    payload = {
        "scanned_at_utc": now.isoformat(timespec="seconds"),
        "market_open": not is_market_closed(now),
        "mode": "ALERT_ONLY",
        "today": {"signals_sent": state.get("signals_sent", 0)},
        "macro": {"dxy": macro.get("dxy"), "vix": macro.get("vix_label")},
        "recent_signals": read_recent_signals(),
    }
    content = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    url = f"https://api.github.com/repos/{repo}/contents/{STATUS_FILE}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, params={"ref": STATUS_BRANCH}, timeout=10)
        sha = r.json().get("sha") if r.ok else None
        body = {"message": f"status {now.strftime('%H:%M UTC')}", "content": content, "branch": STATUS_BRANCH}
        if sha: body["sha"] = sha
        requests.put(url, headers=headers, json=body, timeout=10)
    except Exception as e:
        log(f"push_status_json failed (non-critical): {e}")

# =====================================================================================
# Scan cycle
# =====================================================================================

def run_cycle(loop_mode):
    now = now_utc()
    if is_market_closed(now):
        if loop_mode: log(f"Market closed ({now.strftime('%A %H:%M')} UTC).")
        return
    use_capital = _ensure_capital()
    if not use_capital:
        log("Capital.com unavailable — cannot scan (spec: Capital.com only, no Yahoo)."); return

    state = load_state()
    macro = macro_filter(now, state, use_capital)

    # Global halts (Layer 1): a HIGH-impact event now, or VIX > 35.
    news_hit = news_block(now, _news_events_from_macro(macro))
    if news_hit:
        log(f"NEWS BLOCK — {news_hit} within ±{NEWS_WINDOW_MIN}m."); save_state(state)
        push_status_json(state, now); return
    if macro.get("vix_halt"):
        log(f"VIX HALT — {macro.get('vix_label')}."); save_state(state)
        push_status_json(state, now); return

    for instr, cfg in INSTRUMENTS.items():
        time.sleep(1)
        try:
            sig = scan_instrument(instr, cfg, now, macro, state)
        except Exception as e:
            log(f"{instr}: scan error: {e}"); continue
        if not sig:
            continue

        fp = sig["fingerprint"]
        last = _parse_ts(state["fingerprints"].get(fp))
        if last and (now - last) < timedelta(hours=DEDUP_HOURS):
            log(f"{instr}: dedup — {fp} alerted <{DEDUP_HOURS}h ago"); continue
        if state["per_instrument"].get(instr, 0) >= MAX_PER_INSTR_DAY:
            log(f"{instr}: daily cap reached ({MAX_PER_INSTR_DAY})"); continue

        if tg_send(alert_text(instr, sig["side"], sig)):
            log(f"SIGNAL {instr} {sig['side']} grade {sig['grade']} score {sig['score']}/10")
            state["fingerprints"][fp] = now.isoformat()
            state["per_instrument"][instr] = state["per_instrument"].get(instr, 0) + 1
            state["signals_sent"] = state.get("signals_sent", 0) + 1
            log_signal({
                "ts_utc": now.isoformat(timespec="seconds"), "instrument": instr,
                "direction": sig["side"], "grade": sig["grade"], "poi_score": sig["poi_score"],
                "score": sig["score"], "entry": round(sig["entry"], 2),
                "stop": round(sig["risk"]["stop"], 2), "tp1": round(sig["risk"]["tp1"], 2),
                "tp2": round(sig["risk"]["tp2"], 2), "rr1": sig["risk"]["rr1"], "rr2": sig["risk"]["rr2"],
                "sl_pips": sig["risk"]["sl_pips"], "lot": sig["risk"]["lot"],
                "weekly_bias": sig["bias"]["weekly"], "daily_bias": sig["bias"]["daily"],
                "dxy": sig["macro"]["dxy"], "vix": sig["macro"]["vix_label"],
                "poi_factors": "+".join(sig["poi_factors"]),
                "sweep": True, "choch": True, "fvg": True,
                "checklist": ";".join(f"{k}={int(v)}" for k, v in sig["checklist"]),
                "fingerprint": fp,
            })

    save_state(state)
    push_status_json(state, now)

# =====================================================================================
# Entry points
# =====================================================================================

def self_test():
    log("Self-test — SMC-Macro v1.0")
    ok_tg = tg_send("SMC-Macro bot v1.0: Telegram OK ✔ (ALERT_ONLY)")
    ok_cap = _ensure_capital()
    n = 0
    if ok_cap:
        try:
            d = _cap_fetch(INSTRUMENTS["XAUUSD"]["epic"], "1d", 40)
            n = len(d) if d is not None else 0
        except Exception as e:
            log(f"Capital fetch failed: {e}")
    log(f"Telegram: {'OK' if ok_tg else 'FAIL'} | Capital: {'OK ' + str(n) + ' daily bars' if n else 'FAIL'}")
    if ok_tg:
        tg_send("SMC-Macro v1.0 ready — 7-layer pipeline, 4 instruments, ALERT_ONLY.\n"
                "Layers: Macro · HTF Bias · Liquidity · POI · LTF Entry · Risk · Checklist")

def main():
    args = set(a.lower() for a in sys.argv[1:])
    if "--test" in args:
        self_test(); return
    if "--once" in args:
        try: run_cycle(False)
        except Exception: log("Crash:\n" + traceback.format_exc())
        return
    log(f"SMC-Macro v1.0 loop: every {SCAN_EVERY_MIN}min · 4 instruments · ALERT_ONLY")
    tg_send(f"SMC-Macro bot v1.0 online — 7-layer SMC pipeline × {len(INSTRUMENTS)} instruments\n"
            f"Mode: ALERT_ONLY · scan every {SCAN_EVERY_MIN}min")
    while True:
        try: run_cycle(True)
        except Exception: log("Crash (alive):\n" + traceback.format_exc())
        time.sleep(SCAN_EVERY_MIN * 60)

if __name__ == "__main__":
    main()
