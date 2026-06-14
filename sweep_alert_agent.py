#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sweep_alert_agent.py  v3.0 — Multi-Strategy Edition
====================================================
5-strategy intraday alert agent for Gold (XAUUSD) and US index CFDs.

STRATEGIES (from "The Short Trader's Playbook" + Smart Trading Framework):
  1. Supply Zone / Order Block Rejection
  2. Liquidity Sweep + Break of Structure (ICT/SMC)
  3. Double Top / Double Bottom
  4. Bear Flag / Bull Flag continuation
  5. Post-News Momentum Retest

SIGNAL FRAMEWORK (Smart Trading Framework):
  Price action confirmation = MANDATORY (each strategy provides this)
  + at least 2 of:  Indicator confirmation (RSI, MACD, EMA)
                     Multi-timeframe alignment (H4 bias)
                     Session timing (London / NY overlap)
  Weak (Price+1)   = skip     | Tradeable (Price+2) = WATCH
  Strong (Price+3) = A+       | RR >= 1:2.0 mandatory

SKILLS INTEGRATED:
  strategy-framework  — edge hypothesis, performance criteria, lifecycle
  risk-management     — loss-streak circuit breakers, daily/weekly limits
  trade-journal       — 18-field CSV, behavioral detection, review cadence
  exit-strategies     — scaled exits (TP1+trail), time stops, stop randomization

Run:  python sweep_alert_agent.py [--test | --once]
Env:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Deps: pip install yfinance pandas requests
"""

import os, sys, csv, json, time, math, random, traceback
from datetime import datetime, timedelta, timezone
import requests, pandas as pd, yfinance as yf

# =====================================================================================
# CONFIG
# =====================================================================================

SYMBOLS = {
    "GOLD":   {"yf":"GC=F",  "kind":"gold",  "cfd":"XAUUSD","per_lot_per_pt":100.0,"round_step":50.0},
    "SP500":  {"yf":"ES=F",  "kind":"index", "cfd":"US500", "per_lot_per_pt":1.0,  "round_step":100.0},
    "NASDAQ": {"yf":"NQ=F",  "kind":"index", "cfd":"US100", "per_lot_per_pt":1.0,  "round_step":250.0},
    "DOW":    {"yf":"YM=F",  "kind":"index", "cfd":"US30",  "per_lot_per_pt":1.0,  "round_step":500.0},
}
DXY_YF = "DX-Y.NYB"
ENABLE_SHORTS = True
ENABLE_LONGS  = True

# ---- CFD lots -----------------------------------------------------------------------
MIN_LOT_SIZE = 1.0;  LOT_STEP = 1.0

# ---- Time (UTC) ---------------------------------------------------------------------
# Market hours: Sun 22:00 UTC → Fri 21:00 UTC, daily close 21:00–22:00 UTC
# (= Dubai: Mon 02:00 → Sat 01:00, daily close 01:00–02:00)
HARD_FLAT_UTC="20:45"   # force-flat 15 min before daily close
MAX_HOLD_HOURS=4.0; SCAN_EVERY_MIN=15

# ---- Alert controls -----------------------------------------------------------------
SCORE_A_PLUS=75; SCORE_A_PLUS_FLOOR=65; SCORE_A_PLUS_CEIL=85
ADAPTIVE_THRESHOLD=True; SILENT_DAYS_TO_ADAPT=3
SCORE_WATCH=62; MAX_APLUS_PER_DAY=2; MAX_WATCH_PER_DAY=1
COOLDOWN_MIN_PER_SYMBOL=90; MIN_GAP_BETWEEN_ALERTS=30

# ---- Risk (playbook S3 + risk-management skill) ------------------------------------
ACCOUNT_SIZE=5000.0; RISK_PCT=1.0; MIN_RR=2.0
STOP_ATR_MULT_MAX=1.5; STOP_ATR_MULT_MIN=0.40; STOP_BUFFER_ATR=0.15
PACE_SAFETY=0.80

# ---- Sweep-specific -----------------------------------------------------------------
SWEEP_LOOKBACK=4; MAX_SWEEP_OVERSHOOT_ATR=1.5; MAX_EXTENSION_ATR=2.5

# ---- Scaled exits (exit-strategies) -------------------------------------------------
TP1_RR=2.0; TP2_RR=3.0; TRAIL_ATR_MULT=2.5; TIME_STOP_HOURS=3.0

# ---- Stop randomization (exit-strategies) -------------------------------------------
STOP_RANDOMIZE=True; STOP_RAND_MIN=0.05; STOP_RAND_MAX=0.15

# ---- Circuit breakers (risk-management) ---------------------------------------------
CONSEC_LOSS_REDUCE=3; CONSEC_LOSS_MIN_SIZE=5; CONSEC_LOSS_HALT=7
DAILY_LOSS_LIMIT_PCT=3.0; WEEKLY_LOSS_LIMIT_PCT=5.0
THREE_DAILY_LIMITS_HALT=True

# ---- News blackout ------------------------------------------------------------------
RECURRING_BLACKOUTS_UTC=[("12:25","13:05"),("13:25","14:05")]
EXTRA_BLACKOUTS_UTC=[]

STATE_FILE="agent_state.json"; SIGNALS_CSV="signals_log.csv"
DUBAI=timezone(timedelta(hours=4))

# =====================================================================================
# Utilities + Telegram (unchanged)
# =====================================================================================

def now_utc(): return datetime.now(timezone.utc)
def log(msg): print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}", flush=True)
def hhmm_today(now,hhmm):
    h,m=(int(x) for x in hhmm.split(":")); return now.replace(hour=h,minute=m,second=0,microsecond=0)
def fmt_both(dt): return f"{dt.strftime('%H:%M')} UTC ({dt.astimezone(DUBAI).strftime('%H:%M')} Dubai)"

def is_market_closed(now):
    """
    Market is closed when:
      - Weekend window: Friday 21:00 UTC → Sunday 22:00 UTC
        (Dubai: Saturday 01:00 → Monday 02:00)
      - Daily close:    21:00–22:00 UTC every day
        (Dubai: 01:00–02:00)
    """
    wd = now.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    hm = now.hour * 60 + now.minute
    daily_close_start = 21 * 60   # 21:00 UTC
    daily_close_end   = 22 * 60   # 22:00 UTC
    # Daily maintenance window
    if daily_close_start <= hm < daily_close_end:
        return True
    # Saturday: always closed
    if wd == 5:
        return True
    # Friday after 21:00 UTC: closed
    if wd == 4 and hm >= daily_close_start:
        return True
    # Sunday before 22:00 UTC: closed
    if wd == 6 and hm < daily_close_end:
        return True
    return False

def in_blackout(now):
    t=now.strftime("%H:%M")
    for s,e in RECURRING_BLACKOUTS_UTC:
        if s<=t<e: return True
    for s,e in EXTRA_BLACKOUTS_UTC:
        try:
            sdt=datetime.strptime(s,"%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            edt=datetime.strptime(e,"%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if sdt<=now<edt: return True
        except ValueError: pass
    return False

def recently_exited_blackout(now, minutes=45):
    """True if a blackout window ended within the last N minutes."""
    t=now.strftime("%H:%M")
    for _,e in RECURRING_BLACKOUTS_UTC:
        eh,em=(int(x) for x in e.split(":"))
        end=now.replace(hour=eh,minute=em,second=0,microsecond=0)
        if timedelta(0) <= (now - end) <= timedelta(minutes=minutes):
            return True
    return False

def tg_send(text):
    token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
    chat=os.environ.get("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat:
        log("Telegram env vars missing."); print(text, flush=True); return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id":chat,"text":text,"disable_web_page_preview":True},timeout=15)
        return r.status_code==200
    except requests.RequestException: return False

# =====================================================================================
# State + Circuit Breakers (risk-management skill — unchanged from v2)
# =====================================================================================

def default_state(today_str,threshold):
    return {"date":today_str,"threshold":threshold,"silent_days":0,"aplus_sent":0,
            "watch_sent":0,"last_alert_ts":{},"last_any_alert_ts":None,"sent_keys":[],
            "best_today":None,"digest_sent":False,"evaluated":0,
            "consecutive_losses":0,"daily_risk_exposure":0.0,
            "weekly_risk_exposure":0.0,"halted_until":None,"daily_limit_streak":0}

def load_state():
    today_str=now_utc().date().isoformat()
    try:
        with open(STATE_FILE,"r") as f: st=json.load(f)
    except (FileNotFoundError,json.JSONDecodeError):
        return default_state(today_str,float(SCORE_A_PLUS))
    if st.get("date")!=today_str:
        thr=float(st.get("threshold",SCORE_A_PLUS))
        silent=int(st.get("silent_days",0))
        dls=int(st.get("daily_limit_streak",0))
        if st.get("daily_risk_exposure",0)>=DAILY_LOSS_LIMIT_PCT: dls+=1
        else: dls=0
        if st.get("aplus_sent",0)==0 and st.get("watch_sent",0)==0: silent+=1
        else: silent=0
        if ADAPTIVE_THRESHOLD:
            if silent>=SILENT_DAYS_TO_ADAPT: thr=max(SCORE_A_PLUS_FLOOR,thr-2); silent=0
            elif st.get("aplus_sent",0)>=MAX_APLUS_PER_DAY: thr=min(SCORE_A_PLUS_CEIL,thr+1)
        consec=int(st.get("consecutive_losses",0))
        if consec>=CONSEC_LOSS_REDUCE: thr=min(SCORE_A_PLUS_CEIL,thr+5)
        ns=default_state(today_str,thr)
        ns["silent_days"]=silent; ns["consecutive_losses"]=consec
        ns["weekly_risk_exposure"]=float(st.get("weekly_risk_exposure",0))
        ns["daily_limit_streak"]=dls
        if now_utc().weekday()==0: ns["weekly_risk_exposure"]=0.0; ns["daily_limit_streak"]=0
        if THREE_DAILY_LIMITS_HALT and dls>=3:
            ns["halted_until"]=(now_utc()+timedelta(days=2)).date().isoformat()
        if consec>=CONSEC_LOSS_HALT:
            ns["halted_until"]=(now_utc()+timedelta(hours=24)).isoformat()
        return ns
    return st

def save_state(st):
    try:
        with open(STATE_FILE,"w") as f: json.dump(st,f,indent=2)
    except OSError: pass

def is_halted(st):
    h=st.get("halted_until")
    if not h: return False
    try:
        if "T" in h: return now_utc()<datetime.fromisoformat(h)
        return now_utc().date().isoformat()<=h
    except: return False

def is_watch_only(st):
    if int(st.get("consecutive_losses",0))>=CONSEC_LOSS_MIN_SIZE: return True
    if float(st.get("weekly_risk_exposure",0))>=WEEKLY_LOSS_LIMIT_PCT: return True
    return False

def read_outcomes():
    if not os.path.exists(SIGNALS_CSV): return []
    out=[]
    try:
        with open(SIGNALS_CSV,"r") as f:
            for row in csv.DictReader(f):
                if row.get("alerted") in ("A+","WATCH") and row.get("outcome") in ("W","L"):
                    out.append(row["outcome"])
    except: pass
    return out

def count_consec_losses(outcomes):
    c=0
    for o in reversed(outcomes):
        if o=="L": c+=1
        else: break
    return c

# =====================================================================================
# CSV logging (trade-journal 18-field)
# =====================================================================================

CSV_FIELDS=["ts_utc","symbol","cfd","side","strategy","score","setup_quality",
    "entry","stop","tp1","tp2","trailing_method","time_stop",
    "risk_pts","level","level_price","reasons","alerted",
    "outcome","exit_price","pnl_usd","hold_minutes","lessons"]

def log_csv(row):
    new=not os.path.exists(SIGNALS_CSV)
    try:
        with open(SIGNALS_CSV,"a",newline="") as f:
            w=csv.DictWriter(f,fieldnames=CSV_FIELDS,extrasaction="ignore")
            if new: w.writeheader()
            w.writerow(row)
    except: pass

# =====================================================================================
# Data layer
# =====================================================================================

def fetch(symbol,interval,period):
    for attempt in range(3):
        try:
            df=yf.Ticker(symbol).history(period=period,interval=interval,auto_adjust=False)
            if df is not None and len(df)>0:
                df=df[["Open","High","Low","Close"]].dropna()
                if df.index.tz is None: df.index=df.index.tz_localize("UTC")
                else: df.index=df.index.tz_convert("UTC")
                return df
        except Exception as e:
            log(f"fetch {symbol} {interval} #{attempt+1}: {e}")
        time.sleep(6*(attempt+1))
    return None

def closed_bars(df,bar_min,now):
    if df is None or len(df)==0: return df
    if (now-df.index[-1])<timedelta(minutes=bar_min): return df.iloc[:-1]
    return df

# =====================================================================================
# Indicators (Smart Trading Framework: RSI, MACD, EMA)
# =====================================================================================

def atr(df,n=14):
    h,l,c=df["High"],df["Low"],df["Close"]
    pc=c.shift(1)
    tr=pd.concat([(h-l),(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    return float(tr.ewm(alpha=1/n,adjust=False).mean().iloc[-1])

def adx(df,n=14):
    """Average Directional Index — trend strength. <20 = choppy, >25 = trending."""
    h,l,c=df["High"],df["Low"],df["Close"]
    up=h.diff(); dn=-l.diff()
    pdm=up.where((up>dn)&(up>0),0.0); ndm=dn.where((dn>up)&(dn>0),0.0)
    pc=c.shift(1)
    tr=pd.concat([(h-l),(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    atr_s=tr.ewm(alpha=1/n,adjust=False).mean()
    pdi=100*(pdm.ewm(alpha=1/n,adjust=False).mean()/atr_s)
    ndi=100*(ndm.ewm(alpha=1/n,adjust=False).mean()/atr_s)
    dx=100*((pdi-ndi).abs()/(pdi+ndi))
    return float(dx.ewm(alpha=1/n,adjust=False).mean().iloc[-1])

def rsi(df,n=14):
    delta=df["Close"].diff()
    gain=delta.where(delta>0,0.0).ewm(alpha=1/n,adjust=False).mean()
    loss=(-delta.where(delta<0,0.0)).ewm(alpha=1/n,adjust=False).mean()
    rs=gain/loss
    return 100-(100/(1+rs))

def macd_hist(df):
    e12=df["Close"].ewm(span=12,adjust=False).mean()
    e26=df["Close"].ewm(span=26,adjust=False).mean()
    sig=(e12-e26).ewm(span=9,adjust=False).mean()
    return e12-e26-sig  # histogram

def ema(series,n):
    return series.ewm(span=n,adjust=False).mean()

def pivots(df,k=3):
    highs,lows=[],[]
    H,L=df["High"].values,df["Low"].values
    for i in range(k,len(df)-k):
        if H[i]>=H[i-k:i+k+1].max(): highs.append((i,float(H[i])))
        if L[i]<=L[i-k:i+k+1].min(): lows.append((i,float(L[i])))
    return highs,lows

def htf_bias(d1):
    """Use actual Daily chart with EMA-50/200 (matches Trading Bot Strategy
    regime filter and Smart Trading Framework 'Daily defines direction')."""
    if d1 is None or len(d1)<210: return "neutral"
    e50=d1["Close"].ewm(span=50,adjust=False).mean()
    e200=d1["Close"].ewm(span=200,adjust=False).mean()
    close=float(d1["Close"].iloc[-1])
    slope50=float(e50.iloc[-1]-e50.iloc[-5])
    if close>e50.iloc[-1] and e50.iloc[-1]>e200.iloc[-1] and slope50>0: return "bull"
    if close<e50.iloc[-1] and e50.iloc[-1]<e200.iloc[-1] and slope50<0: return "bear"
    return "neutral"

def is_volatile(m15c,threshold=0.018):
    """Trading Bot Strategy Gate 2 — ATR/close > 1.8% = VOLATILE, skip."""
    a=atr(m15c.tail(120),14)
    c=float(m15c["Close"].iloc[-1])
    return (a/c)>threshold if c>0 else False

def is_choppy(m15c):
    """Smart Trading Framework 'When NOT to Trade' — choppy / no structure.
    ADX < 18 on M15 = no tradeable trend."""
    if len(m15c)<30: return False
    return adx(m15c.tail(60),14)<18

def dxy_state(m15_dxy):
    if m15_dxy is None or len(m15_dxy)<10: return "flat"
    c=m15_dxy["Close"]
    roc=(float(c.iloc[-1])/float(c.iloc[-8])-1.0)*100.0
    if roc>0.05: return "rising"
    if roc<-0.05: return "falling"
    return "flat"

# =====================================================================================
# Indicator confirmation score (Smart Trading Framework: RSI + MACD + EMA = 1 factor)
# =====================================================================================

def indicator_score(m15c, side):
    """RSI checks DIRECTION (falling/rising) not just level.
    Smart Trading Framework: 'RSI falling' for bearish, 'RSI rising' for bullish.
    Returns (points 0-10, reasons list). Needs >= 2/3 indicators aligned."""
    pts=0; reasons=[]
    r_series=rsi(m15c,14)
    r=float(r_series.iloc[-1]); r_prev=float(r_series.iloc[-3])
    h=macd_hist(m15c); h_now=float(h.iloc[-1]); h_prev=float(h.iloc[-2])
    e20=float(ema(m15c["Close"],20).iloc[-1]); c=float(m15c["Close"].iloc[-1])
    if side=="short":
        if r<r_prev: pts+=1  # RSI FALLING (not just level)
        if h_now<h_prev: pts+=1  # MACD histogram falling
        if c<e20: pts+=1  # price below EMA20
    else:
        if r>r_prev: pts+=1  # RSI RISING
        if h_now>h_prev: pts+=1  # MACD histogram rising
        if c>e20: pts+=1  # price above EMA20
    if pts>=2:
        score=10; reasons.append(f"indicators aligned ({pts}/3: RSI {'↓' if side=='short' else '↑'} {r:.0f}, MACD {'↓' if side=='short' else '↑'}, {'<' if side=='short' else '>'}EMA20)")
    elif pts==1:
        score=4; reasons.append(f"weak indicator conf ({pts}/3)")
    else:
        score=0
    return score, reasons

# =====================================================================================
# Liquidity levels (fractal — shared across strategies)
# =====================================================================================

def build_levels(d1,m15,now):
    buy_side,sell_side=[],[]
    today=now.date()
    dd=d1[[d.date()<today for d in d1.index]] if d1 is not None and len(d1) else None
    if dd is not None and len(dd)>=1:
        buy_side.append(("prev-day high",float(dd["High"].iloc[-1]),10))
        sell_side.append(("prev-day low",float(dd["Low"].iloc[-1]),10))
        cur_yw=now.isocalendar()[:2]; weeks={}
        for ts,row in dd.iterrows():
            yw=ts.isocalendar()[:2]
            if yw==cur_yw: continue
            hi,lo=weeks.get(yw,(-math.inf,math.inf))
            weeks[yw]=(max(hi,float(row["High"])),min(lo,float(row["Low"])))
        if weeks:
            lw=sorted(weeks.keys())[-1]
            buy_side.append(("prior-week high",weeks[lw][0],13))
            sell_side.append(("prior-week low",weeks[lw][1],13))
        pm_y,pm_m=(now.year,now.month-1) if now.month>1 else (now.year-1,12)
        pm=dd[[(t.year==pm_y and t.month==pm_m) for t in dd.index]]
        if len(pm):
            buy_side.append(("prior-month high",float(pm["High"].max()),15))
            sell_side.append(("prior-month low",float(pm["Low"].min()),15))
    if m15 is not None and len(m15):
        asia=m15[(m15.index.date==today)&(m15.index.hour<7)]
        if len(asia)>=4:
            buy_side.append(("Asia high",float(asia["High"].max()),6))
            sell_side.append(("Asia low",float(asia["Low"].min()),6))
        recent=m15.tail(96)
        if len(recent)>20:
            ph,pl=pivots(recent.iloc[:-4],k=3)
            px=float(recent["Close"].iloc[-1]); tol=0.0007*px
            for arr,out,label in ((ph,buy_side,"equal highs"),(pl,sell_side,"equal lows")):
                vals=sorted(v for _,v in arr); i=0
                while i<len(vals):
                    g=[vals[i]]; j=i+1
                    while j<len(vals) and vals[j]-g[0]<=tol: g.append(vals[j]); j+=1
                    if len(g)>=2:
                        lv=max(g) if out is buy_side else min(g)
                        out.append((label,float(lv),7))
                    i=j
    def dedup(levels):
        levels=sorted(levels,key=lambda x:-x[2]); kept=[]
        for n,p,w in levels:
            if any(abs(p-kp)<=0.0006*p for _,kp,_ in kept): continue
            kept.append((n,p,w))
        return kept
    return dedup(buy_side),dedup(sell_side)

# =====================================================================================
# STRATEGY 1: Supply Zone / Order Block Rejection
# =====================================================================================

def detect_zone_rejection(name,cfg,m15c,h1c,atr15,side):
    """Find H1 order blocks, check if M15 price is rejecting one."""
    if h1c is None or len(h1c)<30: return []
    candidates=[]
    a1h=atr(h1c.tail(60),14)
    close=float(m15c["Close"].iloc[-1])
    last=m15c.iloc[-1]
    body=abs(float(last["Open"])-float(last["Close"]))

    for i in range(5,len(h1c)-1):
        bar=h1c.iloc[i]
        bdy=abs(float(bar["Open"])-float(bar["Close"]))
        if bdy<1.2*a1h: continue  # need strong candle
        if side=="short" and float(bar["Close"])<float(bar["Open"]):
            # bearish OB: last up-candle before this drop = zone
            if i>0:
                prev=h1c.iloc[i-1]
                if float(prev["Close"])>=float(prev["Open"]):
                    zone_top=float(prev["High"]); zone_bot=float(prev["Open"])
                    if zone_bot<=close<=zone_top:
                        # check for rejection: bearish candle with upper wick
                        wick_up=float(last["High"])-max(float(last["Open"]),float(last["Close"]))
                        if body>0.3*atr15 and float(last["Close"])<float(last["Open"]):
                            candidates.append({
                                "strategy":"zone-rejection","side":"short",
                                "entry":close,"stop":zone_top+STOP_BUFFER_ATR*atr15,
                                "level_name":"H1 supply zone","level_price":zone_top,
                                "base_score":37,"pattern_bonus": min(8,int(wick_up/atr15*8)),
                                "reasons":[f"rejected H1 supply zone {zone_bot:.0f}-{zone_top:.0f}"]
                            })
        elif side=="long" and float(bar["Close"])>float(bar["Open"]):
            if i>0:
                prev=h1c.iloc[i-1]
                if float(prev["Close"])<=float(prev["Open"]):
                    zone_bot=float(prev["Low"]); zone_top=float(prev["Close"])
                    if zone_bot<=close<=zone_top:
                        wick_dn=min(float(last["Open"]),float(last["Close"]))-float(last["Low"])
                        if body>0.3*atr15 and float(last["Close"])>float(last["Open"]):
                            candidates.append({
                                "strategy":"zone-rejection","side":"long",
                                "entry":close,"stop":zone_bot-STOP_BUFFER_ATR*atr15,
                                "level_name":"H1 demand zone","level_price":zone_bot,
                                "base_score":37,"pattern_bonus":min(8,int(wick_dn/atr15*8)),
                                "reasons":[f"rejected H1 demand zone {zone_bot:.0f}-{zone_top:.0f}"]
                            })
    # keep only the freshest (closest zone)
    if candidates:
        candidates.sort(key=lambda x:abs(close-x["level_price"]))
        return [candidates[0]]
    return []

# =====================================================================================
# STRATEGY 2: Liquidity Sweep + BOS (from v2 — the original core)
# =====================================================================================

def detect_sweep(name,cfg,m15c,atr15,side,levels_same):
    close=float(m15c["Close"].iloc[-1])
    win=m15c.iloc[-SWEEP_LOOKBACK:]
    last=m15c.iloc[-1]
    sgn=-1 if side=="short" else 1

    best=None
    for lname,L,w in levels_same:
        if side=="short":
            swept=float(win["High"].max())>L+0.02*atr15 and close<L
            extreme=float(win["High"].max()); overshoot=extreme-L; extension=L-close
        else:
            swept=float(win["Low"].min())<L-0.02*atr15 and close>L
            extreme=float(win["Low"].min()); overshoot=L-extreme; extension=close-L
        if not swept: continue
        if overshoot>MAX_SWEEP_OVERSHOOT_ATR*atr15: continue
        if extension>MAX_EXTENSION_ATR*atr15: continue
        if best is None or w>best[2]: best=(lname,L,w,extreme)
    if best is None: return []
    lname,L,lweight,extreme=best

    body=float(last["Open"]-last["Close"])*sgn*-1
    rng=float(last["High"]-last["Low"])
    displacement=(body>0) and (body>=0.5*atr15 or (rng>=1.0*atr15 and body>=0.5*rng))
    look=m15c.iloc[-22:-1]; ph,pl=pivots(look,k=2)
    if side=="short": bos=bool(pl) and close<pl[-1][1]
    else: bos=bool(ph) and close>ph[-1][1]
    if not (displacement or bos): return []

    pb=0
    if displacement and bos: pb=8
    elif displacement: pb=5
    else: pb=3
    # check wick
    for _,b in win.iterrows():
        if side=="short" and float(b["High"])>L and float(b["Close"])<L: pb+=2; break
        if side=="long" and float(b["Low"])<L and float(b["Close"])>L: pb+=2; break

    entry=0.5*(extreme+close)
    return [{
        "strategy":"liquidity-sweep","side":side,
        "entry":entry,"stop":extreme,"extreme":extreme,
        "level_name":lname,"level_price":L,"level_weight":lweight,
        "base_score":38,"pattern_bonus":min(10,pb),
        "reasons":[f"swept {lname} {L:.2f}",
                   "displacement+BOS" if (displacement and bos) else ("displacement" if displacement else "BOS")]
    }]

# =====================================================================================
# STRATEGY 3: Double Top / Double Bottom
# =====================================================================================

def detect_double_pattern(name,cfg,m15c,atr15,side):
    """Strategy 3: Double Top/Bottom AND Head & Shoulders."""
    if len(m15c)<60: return []
    close=float(m15c["Close"].iloc[-1])
    recent=m15c.tail(80)
    ph,pl=pivots(recent,k=3)
    results=[]

    if side=="short":
        # --- Double Top ---
        if len(ph)>=2:
            for i in range(len(ph)-1):
                for j in range(i+1,len(ph)):
                    idx_i,p_i=ph[i]; idx_j,p_j=ph[j]
                    if abs(idx_j-idx_i)<8: continue
                    if abs(p_i-p_j)>0.003*max(p_i,p_j): continue
                    seg=recent.iloc[idx_i:idx_j+1]
                    neckline=float(seg["Low"].min())
                    if close<neckline:
                        pat_height=max(p_i,p_j)-neckline
                        results.append({"strategy":"double-top","side":"short",
                            "entry":close,"stop":max(p_i,p_j)+STOP_BUFFER_ATR*atr15,
                            "level_name":"double-top neckline","level_price":neckline,
                            "base_score":35,"pattern_bonus":min(8,int(pat_height/atr15*3)),
                            "reasons":[f"double top at {max(p_i,p_j):.2f}, neckline {neckline:.2f} broken"]})
                        break
                if results: break

        # --- Head & Shoulders ---
        if len(ph)>=3 and not results:
            for i in range(len(ph)-2):
                idx_l,p_l=ph[i]       # left shoulder
                idx_h,p_h=ph[i+1]     # head (must be highest)
                idx_r,p_r=ph[i+2]     # right shoulder
                if not (p_h>p_l and p_h>p_r): continue  # head must be highest
                if abs(p_l-p_r)>0.005*p_h: continue     # shoulders roughly equal
                if idx_h-idx_l<5 or idx_r-idx_h<5: continue
                seg=recent.iloc[idx_l:idx_r+1]
                neckline=float(seg["Low"].min())
                if close<neckline:
                    pat_height=p_h-neckline
                    results.append({"strategy":"head-shoulders","side":"short",
                        "entry":close,"stop":p_r+STOP_BUFFER_ATR*atr15,
                        "level_name":"H&S neckline","level_price":neckline,
                        "base_score":37,"pattern_bonus":min(10,int(pat_height/atr15*3)),
                        "reasons":[f"H&S: head {p_h:.2f}, shoulders {p_l:.2f}/{p_r:.2f}, neckline {neckline:.2f} broken"]})
                    break

    elif side=="long":
        # --- Double Bottom ---
        if len(pl)>=2:
            for i in range(len(pl)-1):
                for j in range(i+1,len(pl)):
                    idx_i,p_i=pl[i]; idx_j,p_j=pl[j]
                    if abs(idx_j-idx_i)<8: continue
                    if abs(p_i-p_j)>0.003*max(p_i,p_j): continue
                    seg=recent.iloc[idx_i:idx_j+1]
                    neckline=float(seg["High"].max())
                    if close>neckline:
                        pat_height=neckline-min(p_i,p_j)
                        results.append({"strategy":"double-bottom","side":"long",
                            "entry":close,"stop":min(p_i,p_j)-STOP_BUFFER_ATR*atr15,
                            "level_name":"double-bottom neckline","level_price":neckline,
                            "base_score":35,"pattern_bonus":min(8,int(pat_height/atr15*3)),
                            "reasons":[f"double bottom at {min(p_i,p_j):.2f}, neckline {neckline:.2f} broken"]})
                        break
                if results: break

        # --- Inverse Head & Shoulders ---
        if len(pl)>=3 and not results:
            for i in range(len(pl)-2):
                idx_l,p_l=pl[i]; idx_h,p_h=pl[i+1]; idx_r,p_r=pl[i+2]
                if not (p_h<p_l and p_h<p_r): continue
                if abs(p_l-p_r)>0.005*max(p_l,p_r): continue
                if idx_h-idx_l<5 or idx_r-idx_h<5: continue
                seg=recent.iloc[idx_l:idx_r+1]
                neckline=float(seg["High"].max())
                if close>neckline:
                    pat_height=neckline-p_h
                    results.append({"strategy":"inv-head-shoulders","side":"long",
                        "entry":close,"stop":p_r-STOP_BUFFER_ATR*atr15,
                        "level_name":"inv H&S neckline","level_price":neckline,
                        "base_score":37,"pattern_bonus":min(10,int(pat_height/atr15*3)),
                        "reasons":[f"inv H&S: head {p_h:.2f}, shoulders {p_l:.2f}/{p_r:.2f}, neckline {neckline:.2f} broken"]})
                    break

    return results[:1]  # best pattern only

# =====================================================================================
# STRATEGY 4: Bear Flag / Bull Flag
# =====================================================================================

def detect_flag(name,cfg,m15c,atr15,side):
    if len(m15c)<40: return []
    close=float(m15c["Close"].iloc[-1])
    # look for pole (sharp move) then flag (shallow consolidation)
    for pole_end in range(len(m15c)-15, len(m15c)-8):
        if pole_end<5: continue
        # pole = 3-8 bars before pole_end
        for pole_start in range(max(0,pole_end-8), pole_end-2):
            seg=m15c.iloc[pole_start:pole_end+1]
            if side=="short":
                pole_move=float(seg["High"].max()-seg["Close"].iloc[-1])
            else:
                pole_move=float(seg["Close"].iloc[-1]-seg["Low"].min())
            if pole_move<2.0*atr15: continue  # pole must be strong

            # flag = bars after pole_end until now-1
            flag=m15c.iloc[pole_end+1:-1]
            if len(flag)<3: continue
            flag_range=float(flag["High"].max()-flag["Low"].min())
            if flag_range>0.5*pole_move: continue  # flag must be tight

            # check for breakout of flag
            if side=="short" and close<float(flag["Low"].min()):
                pole_target=close-pole_move
                return [{"strategy":"bear-flag","side":"short",
                         "entry":close,"stop":float(flag["High"].max())+STOP_BUFFER_ATR*atr15,
                         "level_name":"flag low","level_price":float(flag["Low"].min()),
                         "base_score":36,"pattern_bonus":min(8,int(pole_move/atr15*2)),
                         "pole_target":pole_target,
                         "reasons":[f"bear flag: pole {pole_move:.1f}pts, flag broke at {float(flag['Low'].min()):.2f}"]}]
            elif side=="long" and close>float(flag["High"].max()):
                pole_target=close+pole_move
                return [{"strategy":"bull-flag","side":"long",
                         "entry":close,"stop":float(flag["Low"].min())-STOP_BUFFER_ATR*atr15,
                         "level_name":"flag high","level_price":float(flag["High"].max()),
                         "base_score":36,"pattern_bonus":min(8,int(pole_move/atr15*2)),
                         "pole_target":pole_target,
                         "reasons":[f"bull flag: pole {pole_move:.1f}pts, flag broke at {float(flag['High'].max()):.2f}"]}]
    return []

# =====================================================================================
# STRATEGY 5: Post-News Momentum Retest
# =====================================================================================

def detect_news_retest(name,cfg,m15c,atr15,side,now):
    """After a news blackout ends, check if a big move happened and price is retesting."""
    if not recently_exited_blackout(now, minutes=45): return []
    if len(m15c)<10: return []
    close=float(m15c["Close"].iloc[-1])

    # find the largest candle in the last 6 bars (the news bar)
    recent=m15c.iloc[-6:]
    ranges=[(float(b["High"]-b["Low"]),i) for i,(_,b) in enumerate(recent.iterrows())]
    max_rng,max_idx=max(ranges,key=lambda x:x[0])
    if max_rng<1.5*atr15: return []  # no significant news move

    news_bar=recent.iloc[max_idx]
    if side=="short" and float(news_bar["Close"])<float(news_bar["Open"]):
        # bearish news move — retest of the broken level (the news bar's open area)
        retest_level=float(news_bar["Open"])
        if abs(close-retest_level)<0.5*atr15:
            return [{"strategy":"news-retest","side":"short",
                     "entry":close,"stop":retest_level+0.5*atr15,
                     "level_name":"post-news retest","level_price":retest_level,
                     "base_score":34,"pattern_bonus":min(8,int(max_rng/atr15*3)),
                     "reasons":[f"post-news bearish move ({max_rng:.1f}pts), retesting {retest_level:.2f}"]}]
    elif side=="long" and float(news_bar["Close"])>float(news_bar["Open"]):
        retest_level=float(news_bar["Open"])
        if abs(close-retest_level)<0.5*atr15:
            return [{"strategy":"news-retest","side":"long",
                     "entry":close,"stop":retest_level-0.5*atr15,
                     "level_name":"post-news retest","level_price":retest_level,
                     "base_score":34,"pattern_bonus":min(8,int(max_rng/atr15*3)),
                     "reasons":[f"post-news bullish move ({max_rng:.1f}pts), retesting {retest_level:.2f}"]}]
    return []

# =====================================================================================
# Unified confluence scoring (Smart Trading Framework)
# =====================================================================================

def score_candidate(raw, cfg, symbol_name, m15c, atr15, atr1h, bias, dxy, now, flat_by, levels_opp):
    """Add confluence factors to a raw candidate from any strategy detector."""
    side=raw["side"]; sgn=-1 if side=="short" else 1
    score=float(raw["base_score"])+float(raw.get("pattern_bonus",0))
    reasons=list(raw["reasons"])

    # ---- Smart Trading Framework: 3 confirmation factors ----------------------------

    # Factor 1: Indicator confirmation (RSI + MACD + EMA)
    ind_pts, ind_reasons = indicator_score(m15c, side)
    score += ind_pts; reasons += ind_reasons

    # Factor 2: Multi-timeframe alignment
    if (side=="short" and bias=="bear") or (side=="long" and bias=="bull"):
        score+=15; reasons.append(f"HTF bias {bias} aligned")
    elif bias=="neutral": score+=5
    else: score-=8; reasons.append(f"counter-trend vs HTF {bias} (-8)")

    # Factor 3: Session timing
    hr=now.hour+now.minute/60.0
    if 12.0<=hr<16.0: score+=10; reasons.append("NY overlap")
    elif 7.0<=hr<12.0: score+=6; reasons.append("London session")

    # ---- Additional confluence (not part of the 3-factor count) ---------------------
    if cfg["kind"]=="gold":
        if (side=="short" and dxy=="rising") or (side=="long" and dxy=="falling"):
            score+=8; reasons.append(f"DXY {dxy} confluence")
        elif dxy!="flat": score-=4; reasons.append(f"DXY {dxy} against")

    step=cfg["round_step"]; L=raw["level_price"]
    nearest_round=round(L/step)*step
    if abs(L-nearest_round)<=0.3*atr15:
        score+=5; reasons.append(f"round number {nearest_round:.0f}")

    # ---- Finalize entry / stop / risk / targets -------------------------------------
    entry=raw["entry"]; stop_raw=raw["stop"]

    # stop randomization (exit-strategies)
    rand_off=random.uniform(STOP_RAND_MIN,STOP_RAND_MAX)*atr15 if STOP_RANDOMIZE else 0
    if side=="short": stop=stop_raw+rand_off
    else: stop=stop_raw-rand_off

    # for sweep strategy, use retrace entry
    if raw["strategy"]=="liquidity-sweep":
        extreme=raw.get("extreme",stop_raw)
        entry=0.5*(extreme+float(m15c["Close"].iloc[-1]))
        if side=="short":
            stop=extreme+STOP_BUFFER_ATR*atr15+rand_off
            entry=max(entry,stop-0.95*STOP_ATR_MULT_MAX*atr15)
        else:
            stop=extreme-STOP_BUFFER_ATR*atr15-rand_off
            entry=min(entry,stop+0.95*STOP_ATR_MULT_MAX*atr15)

    risk=abs(stop-entry)
    if risk<=0 or risk>STOP_ATR_MULT_MAX*atr15 or risk<STOP_ATR_MULT_MIN*atr15:
        return None

    tp1=entry+sgn*TP1_RR*risk
    # runner
    runner,runner_rr=None,0.0
    opp=[p for _,p,_ in levels_opp if (p<entry if side=="short" else p>entry)]
    if opp:
        runner=max(opp) if side=="short" else min(opp)
        runner_rr=abs(entry-runner)/risk
    # flag strategies use pole-projection target
    if "pole_target" in raw:
        tp2=raw["pole_target"]
    elif runner and runner_rr>=2.5:
        tp2=runner
    else:
        tp2=entry+sgn*TP2_RR*risk

    # pace check
    hours_avail=min(MAX_HOLD_HOURS,max(0,(flat_by-now).total_seconds()/3600))
    reachable=0.5*atr1h*hours_avail
    if TP1_RR*risk>PACE_SAFETY*reachable: return None

    time_stop_at=min(now+timedelta(hours=TIME_STOP_HOURS),flat_by)
    score=min(100.0,score)
    return {
        "symbol":symbol_name,"side":side,"score":round(score,1),
        "strategy":raw["strategy"],
        "entry":entry,"stop":stop,"tp1":tp1,"tp2":tp2,
        "be_stop":entry,"risk_pts":risk,
        "runner":runner,"runner_rr":round(runner_rr,2),
        "trail_desc":f"Chandelier {TRAIL_ATR_MULT}x ATR",
        "time_stop_desc":f"If flat after {fmt_both(time_stop_at)}, close",
        "level_name":raw["level_name"],"level_price":raw["level_price"],"atr15":atr15,
        "reasons":reasons,"flat_by":flat_by,
        "key":f"{symbol_name}:{side}:{raw['strategy']}:{round(raw['level_price'],1)}",
    }

# =====================================================================================
# Position sizing + alert text
# =====================================================================================

def size_line(cfg,risk_pts):
    risk_dollars=ACCOUNT_SIZE*RISK_PCT/100.0
    risk_per_lot=risk_pts*cfg["per_lot_per_pt"]
    if risk_per_lot<=0: return "Size: error."
    exact=risk_dollars/risk_per_lot
    lots=(exact//LOT_STEP)*LOT_STEP if LOT_STEP>0 else 0
    lots=round(lots,4)
    if lots>=MIN_LOT_SIZE:
        return f"Size @{RISK_PCT:.0f}% of ${ACCOUNT_SIZE:,.0f}: {lots:.2f} lot {cfg['cfd']} (risk ${lots*risk_per_lot:,.0f})"
    return (f"Size: 0 lots {cfg['cfd']} — 1 lot risks ${risk_per_lot:,.0f} > ${risk_dollars:,.0f} budget. "
            f"SKIP or set MIN_LOT_SIZE=0.01.")

STRAT_LABELS={
    "liquidity-sweep":"Strategy 2: Liquidity Sweep + BOS",
    "zone-rejection":"Strategy 1: Supply Zone Rejection",
    "double-top":"Strategy 3: Double Top",
    "double-bottom":"Strategy 3: Double Bottom",
    "head-shoulders":"Strategy 3: Head & Shoulders",
    "inv-head-shoulders":"Strategy 3: Inverse Head & Shoulders",
    "bear-flag":"Strategy 4: Bear Flag",
    "bull-flag":"Strategy 4: Bull Flag",
    "news-retest":"Strategy 5: Post-News Retest",
}

def alert_text(c,cfg,tier,now,st):
    arrow="SHORT" if c["side"]=="short" else "LONG"
    head="A+ SETUP" if tier=="A+" else "WATCH (heads-up only)"
    hold_to=min(now+timedelta(hours=MAX_HOLD_HOURS),c["flat_by"])
    cb=""
    consec=int(st.get("consecutive_losses",0))
    if consec>=CONSEC_LOSS_REDUCE: cb=f"\n⚠ CAUTION: {consec} consecutive losses — reduce size"
    lines=[
        f"[{head}] {arrow} {c['symbol']} ({cfg['cfd']}) — score {c['score']:.0f}/100",
        STRAT_LABELS.get(c["strategy"],c["strategy"]),
        "",
        f"Entry  {c['entry']:.2f}  (LIMIT pullback — never chase)",
        f"Stop   {c['stop']:.2f}  ({c['risk_pts']:.2f}pts = {c['risk_pts']/c['atr15']:.1f}x ATR15)",
        "",
        "EXIT PLAN:",
        f"  TP1  {c['tp1']:.2f}  (50%, move stop to BE {c['be_stop']:.2f})",
        f"  TP2  {c['tp2']:.2f}  (trail: {c['trail_desc']})",
        f"  Time: {c['time_stop_desc']}",
        "",
        size_line(cfg,c["risk_pts"]),
        f"FLAT BY {fmt_both(hold_to)}",
        "",
        "Why: "+"; ".join(c["reasons"]),
    ]
    if cb: lines.append(cb)
    lines+=["","Survival > Capital > Growth","Mark W/L in signals_log.csv"]
    return "\n".join(lines)

# =====================================================================================
# Scan cycle — runs all 5 strategies per instrument
# =====================================================================================

def run_cycle(loop_mode):
    now=now_utc()
    if is_market_closed(now):
        if loop_mode: log(f"Market closed ({now.strftime('%A %H:%M')} UTC).")
        return
    st=load_state()
    if is_halted(st):
        log(f"HALTED until {st.get('halted_until')}."); return
    outcomes=read_outcomes()
    if outcomes: st["consecutive_losses"]=count_consec_losses(outcomes)
    # Hard flat = 15 min before daily close (21:00 UTC)
    hard_flat=hhmm_today(now,HARD_FLAT_UTC)

    # Daily digest at end of each trading day (just before daily close)
    digest_time=hhmm_today(now,"20:30")
    if now>=digest_time and not st.get("digest_sent"):
        best=st.get("best_today")
        bt=f"best: {best['symbol']} {best['strategy']} {best['side']} scored {best['score']:.0f}" if best else "no qualifying setup"
        ws=""
        if outcomes:
            r=outcomes[-20:]; wins=r.count("W"); t=len(r)
            ws=f"\nRecent ({t}): WR {wins/t*100:.0f}% | streak: {count_consec_losses(outcomes)} L"
        tg_send(f"Daily digest v3.0\nA+: {st.get('aplus_sent',0)} | WATCH: {st.get('watch_sent',0)} | "
                f"evaluated: {st.get('evaluated',0)}\n{bt}\n"
                f"Threshold: {st.get('threshold',SCORE_A_PLUS):.0f}\n"
                f"Risk: {st.get('daily_risk_exposure',0):.1f}% day / {st.get('weekly_risk_exposure',0):.1f}% week"
                f"{ws}\nA no-trade day is a winning day.")
        st["digest_sent"]=True; save_state(st); return

    if in_blackout(now):
        log("News blackout."); return
    if float(st.get("daily_risk_exposure",0))>=DAILY_LOSS_LIMIT_PCT:
        log("Daily risk limit reached."); return
    watch_only=is_watch_only(st)

    dxy_df=fetch(DXY_YF,"15m","5d")
    dxy=dxy_state(closed_bars(dxy_df,15,now)) if dxy_df is not None else "flat"

    all_candidates=[]
    for name,cfg in SYMBOLS.items():
        time.sleep(2)
        m15=fetch(cfg["yf"],"15m","5d")
        h1=fetch(cfg["yf"],"1h","1mo")
        d1=fetch(cfg["yf"],"1d","6mo")
        if m15 is None or h1 is None or d1 is None: continue
        m15c=closed_bars(m15,15,now); h1c=closed_bars(h1,60,now)
        d1c=closed_bars(d1,1440,now)
        if m15c is None or h1c is None or len(m15c)<60 or len(h1c)<60: continue
        if (now-m15c.index[-1])>timedelta(minutes=45): continue

        atr15=atr(m15c.tail(120),14); atr1h=atr(h1c.tail(120),14)
        if atr15<=0 or atr1h<=0: continue

        # Volatility regime gate (Trading Bot Strategy Gate 2)
        if is_volatile(m15c):
            log(f"{name}: VOLATILE regime (ATR/close > 1.8%), skipping.")
            continue

        # Choppy market detection (Smart Trading Framework 'When NOT to Trade')
        choppy=is_choppy(m15c)

        bias=htf_bias(d1c)
        buy_side,sell_side=build_levels(d1c,m15c,now)

        sides=[]
        if ENABLE_SHORTS: sides.append(("short",buy_side,sell_side))
        if ENABLE_LONGS: sides.append(("long",sell_side,buy_side))

        for side,same,opp in sides:
            raws=[]
            raws+=detect_sweep(name,cfg,m15c,atr15,side,same)
            raws+=detect_zone_rejection(name,cfg,m15c,h1c,atr15,side)
            raws+=detect_double_pattern(name,cfg,m15c,atr15,side)
            raws+=detect_flag(name,cfg,m15c,atr15,side)
            raws+=detect_news_retest(name,cfg,m15c,atr15,side,now)
            for raw in raws:
                c=score_candidate(raw,cfg,name,m15c,atr15,atr1h,bias,dxy,now,hard_flat,opp)
                if c:
                    # penalize choppy markets (-10 score)
                    if choppy:
                        c["score"]=max(0,c["score"]-10)
                        c["reasons"].append("choppy market (-10)")
                    st["evaluated"]=st.get("evaluated",0)+1
                    all_candidates.append((c,cfg))

    # US Index Consensus Filter (Trading Bot Strategy Section 8)
    # If 2+ US indices fire signals, suppress any that contradict the consensus
    index_signals=[c for c,cfg in all_candidates if cfg["kind"]=="index" and c["score"]>=SCORE_WATCH]
    if len(index_signals)>=2:
        buy_count=sum(1 for c in index_signals if c["side"]=="long")
        sell_count=sum(1 for c in index_signals if c["side"]=="short")
        consensus_side="long" if buy_count>sell_count else "short" if sell_count>buy_count else None
        if consensus_side:
            before=len(all_candidates)
            all_candidates=[(c,cfg) for c,cfg in all_candidates
                            if cfg["kind"]!="index" or c["side"]==consensus_side or c["score"]<SCORE_WATCH]
            dropped=before-len(all_candidates)
            if dropped: log(f"Consensus filter: dropped {dropped} index signal(s) contradicting {consensus_side}")

    if not all_candidates: save_state(st); return
    all_candidates.sort(key=lambda x:-x[0]["score"])
    threshold=float(st.get("threshold",SCORE_A_PLUS))

    for c,cfg in all_candidates:
        if st.get("best_today") is None or c["score"]>st["best_today"]["score"]:
            st["best_today"]={"symbol":c["symbol"],"side":c["side"],"score":c["score"],
                              "strategy":c["strategy"]}
        if c["key"] in st.get("sent_keys",[]): continue
        last_sym=st.get("last_alert_ts",{}).get(c["symbol"])
        if last_sym and (now-datetime.fromisoformat(last_sym))<timedelta(minutes=COOLDOWN_MIN_PER_SYMBOL): continue
        last_any=st.get("last_any_alert_ts")
        if last_any and (now-datetime.fromisoformat(last_any))<timedelta(minutes=MIN_GAP_BETWEEN_ALERTS): continue

        tier=None
        if not watch_only and c["score"]>=threshold and st.get("aplus_sent",0)<MAX_APLUS_PER_DAY: tier="A+"
        elif SCORE_WATCH<=c["score"] and st.get("watch_sent",0)<MAX_WATCH_PER_DAY: tier="WATCH"
        if tier is None: continue

        if tg_send(alert_text(c,cfg,tier,now,st)):
            log(f"ALERT {tier} {c['symbol']} {c['strategy']} {c['side']} score {c['score']}")
            c["alerted"]=tier
            st["sent_keys"].append(c["key"])
            st.setdefault("last_alert_ts",{})[c["symbol"]]=now.isoformat()
            st["last_any_alert_ts"]=now.isoformat()
            if tier=="A+":
                st["aplus_sent"]=st.get("aplus_sent",0)+1
                st["daily_risk_exposure"]=float(st.get("daily_risk_exposure",0))+RISK_PCT
                st["weekly_risk_exposure"]=float(st.get("weekly_risk_exposure",0))+RISK_PCT
            else: st["watch_sent"]=st.get("watch_sent",0)+1

    for c,cfg in all_candidates:
        if c["score"]>=50:
            log_csv({"ts_utc":now.isoformat(timespec="seconds"),
                "symbol":c["symbol"],"cfd":cfg["cfd"],"side":c["side"],
                "strategy":c["strategy"],"score":c["score"],
                "setup_quality":"A+" if c["score"]>=threshold else "B",
                "entry":round(c["entry"],2),"stop":round(c["stop"],2),
                "tp1":round(c["tp1"],2),"tp2":round(c["tp2"],2),
                "trailing_method":c["trail_desc"],"time_stop":c["time_stop_desc"],
                "risk_pts":round(c["risk_pts"],2),"level":c["level_name"],
                "level_price":round(c["level_price"],2),
                "reasons":" | ".join(c["reasons"]),"alerted":c.get("alerted",""),
                "outcome":"","exit_price":"","pnl_usd":"","hold_minutes":"","lessons":""})
    save_state(st)

# =====================================================================================
# Entry points
# =====================================================================================

def self_test():
    log("Self-test v3.0…")
    ok_tg=tg_send("sweep_alert_agent v3.0 multi-strategy: Telegram OK ✔")
    df=fetch(SYMBOLS["GOLD"]["yf"],"15m","5d")
    ok_data=df is not None and len(df)>50
    log(f"Telegram: {'OK' if ok_tg else 'FAIL'} | Data: {'OK '+str(len(df))+' bars' if ok_data else 'FAIL'}")
    if ok_tg and ok_data:
        tg_send("v3.0 ready — 5 strategies:\n"
                "1. Zone Rejection\n2. Liquidity Sweep\n3. Double Top/Bottom\n"
                "4. Bear/Bull Flag\n5. Post-News Retest\n"
                f"Window: Sun 22:00–Fri 21:00 UTC, daily close 21:00–22:00 UTC\n"
                f"Circuit breakers: {CONSEC_LOSS_REDUCE}/{CONSEC_LOSS_MIN_SIZE}/{CONSEC_LOSS_HALT}")

def main():
    args=set(a.lower() for a in sys.argv[1:])
    if "--test" in args: self_test(); return
    if "--once" in args:
        try: run_cycle(False)
        except: log("Crash:\n"+traceback.format_exc())
        return
    log(f"v3.0 loop: {SCAN_EVERY_MIN}min, 5 strategies, Sun 22:00–Fri 21:00 UTC")
    tg_send(f"Agent v3.0 online — 5 strategies × {len(SYMBOLS)} instruments\n"
            f"Every {SCAN_EVERY_MIN}min | max {MAX_APLUS_PER_DAY} A+/day | circuit breakers ON")
    while True:
        try: run_cycle(True)
        except: log("Crash (alive):\n"+traceback.format_exc())
        time.sleep(SCAN_EVERY_MIN*60)

if __name__=="__main__":
    main()
