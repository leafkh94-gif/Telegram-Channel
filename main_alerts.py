"""
main_alerts.py — multi-market alert bot (no execution, no broker login).

Watches Gold, S&P 500, Nasdaq 100, and Dow Jones via Yahoo Finance (free).
When the multi-agent Orchestrator detects a high-quality setup it sends a
Telegram message with entry price, take profit, stop loss, and agent reasoning.

Signal pipeline:
  MarketAgent  — ADX regime + liquidity sweep + S/R confluence
  NewsAgent    — ForexFactory calendar + RSS breaking headlines
  RiskAgent    — ATR-based lot sizing
  Orchestrator — combines all three; fires only when all agree

Usage:
  python main_alerts.py

Required .env keys:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Optional .env keys:
  ACCOUNT_SIZE_USD      (default 2000)
  RISK_PER_TRADE_PCT    (default 0.01 = 1%)

No Capital.com or TradingView account required.
"""
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

from agents.orchestrator import Orchestrator
from agents.base import TradeDecision
from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from strategy.base import TF_H1
from strategy.indicators import atr as _atr
from strategy.market_hours import is_tradeable
from strategy.yahoo_feed import YahooFinanceFeed

# ── Configuration ─────────────────────────────────────────────────────────────

SCAN_INTERVAL_S      = 15 * 60        # seconds between full watchlist scans
SCAN_ALIGN_OFFSET_S  = 90             # seconds after each :00/:15/:30/:45 boundary
ALERT_COOLDOWN_S     = 60 * 60        # minimum seconds before re-alerting same instrument
HEARTBEAT_INTERVAL_S = 24 * 60 * 60   # liveness ping every 24h if no alerts fired
TP_ATR_MULT          = 3.0            # take-profit = entry ± (ATR × 3.0)
SL_ATR_MULT          = 1.5            # stop-loss   = entry ± (ATR × 1.5)
COOLDOWN_FILE        = os.getenv("COOLDOWN_FILE", ".alert_cooldown.json")
ACCOUNT_SIZE_USD     = float(os.getenv("ACCOUNT_SIZE_USD", "2000"))


@dataclass
class _Instrument:
    epic: str
    name: str
    _last_alert: float   = field(default=0.0, init=False, repr=False)
    _scans_done: int     = field(default=0,   init=False, repr=False)
    _scans_skipped: int  = field(default=0,   init=False, repr=False)
    _last_block: str     = field(default="",  init=False, repr=False)

    def on_cooldown(self) -> bool:
        return time.time() - self._last_alert < ALERT_COOLDOWN_S

    def mark_alerted(self) -> None:
        self._last_alert = time.time()

    def record_scan(self, block_reason: str = "") -> None:
        self._scans_done += 1
        if block_reason:
            self._last_block = block_reason

    def record_skip(self) -> None:
        self._scans_skipped += 1

    def reset_counters(self) -> None:
        self._scans_done    = 0
        self._scans_skipped = 0
        self._last_block    = ""


WATCHLIST: list[_Instrument] = [
    _Instrument("GOLD",  "Gold (XAU/USD)"),
    _Instrument("US500", "S&P 500"),
    _Instrument("US100", "Nasdaq 100"),
    _Instrument("US30",  "Dow Jones (US30)"),
]

# ── Cooldown persistence ──────────────────────────────────────────────────────

def _load_cooldowns(instruments: list) -> None:
    global _last_heartbeat
    try:
        with open(COOLDOWN_FILE) as f:
            data = json.load(f)
        for instr in instruments:
            ts = data.get(instr.epic, 0.0)
            if ts:
                instr._last_alert = float(ts)
            instr._scans_done    = int(data.get(f"{instr.epic}_scans",   0))
            instr._scans_skipped = int(data.get(f"{instr.epic}_skipped", 0))
            instr._last_block    = str(data.get(f"{instr.epic}_block",   ""))
        _last_heartbeat = float(data.get("_last_heartbeat", 0.0))
        logging.getLogger(__name__).info("Cooldown state restored from %s", COOLDOWN_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _save_cooldown(instr) -> None:
    try:
        try:
            with open(COOLDOWN_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[instr.epic]              = instr._last_alert
        data[f"{instr.epic}_scans"]   = instr._scans_done
        data[f"{instr.epic}_skipped"] = instr._scans_skipped
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(data, f)
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not save cooldown state: %s", exc)


def _save_scan_counters(instruments: list) -> None:
    """Persist scan/skip counters after every loop so they survive runner restarts."""
    try:
        try:
            with open(COOLDOWN_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        for instr in instruments:
            data[f"{instr.epic}_scans"]   = instr._scans_done
            data[f"{instr.epic}_skipped"] = instr._scans_skipped
            data[f"{instr.epic}_block"]   = instr._last_block
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


# ── Scan timing ───────────────────────────────────────────────────────────────

def _sleep_until_next_scan(logger: logging.Logger) -> None:
    """
    Sleep until SCAN_ALIGN_OFFSET_S past the next SCAN_INTERVAL_S boundary.
    Aligning to :00/:15/:30/:45 UTC guarantees scans see completed H1 candles
    rather than landing at a random mid-candle point.
    Sleeps in 1 s slices so SIGTERM/SIGINT still shut down promptly.
    """
    now = time.time()
    next_boundary = (int(now) // SCAN_INTERVAL_S + 1) * SCAN_INTERVAL_S
    target = next_boundary + SCAN_ALIGN_OFFSET_S
    if target - now < 60:
        target += SCAN_INTERVAL_S
    logger.debug("Sleeping %.0fs until next aligned scan", target - now)
    while _running and time.time() < target:
        time.sleep(1)


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_running = True


def _handle_shutdown(sig, frame):  # noqa: ARG001
    global _running
    logging.getLogger(__name__).info("Shutdown signal — stopping alert loop")
    _running = False


# ── Alert formatting ──────────────────────────────────────────────────────────

def _build_message(instr: _Instrument, decision: TradeDecision,
                   entry: float, tp: float, sl: float) -> tuple[str, str]:
    """Return (html, plain) alert strings including per-agent reasoning."""
    import datetime
    now       = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    emoji     = "🟢" if decision.action == "buy" else "🔴"
    dir_label = "BUY" if decision.action == "buy" else "SELL"
    risk      = abs(entry - sl)
    reward    = abs(entry - tp)
    rr        = reward / risk if risk > 0 else 0.0
    tp_pct    = (reward / entry) * 100
    sl_pct    = (risk   / entry) * 100

    # For BUY:  TP is above entry (+), SL is below entry (-)
    # For SELL: TP is below entry (-), SL is above entry (+)
    is_buy    = decision.action == "buy"
    tp_sign   = "+" if is_buy else "-"
    sl_sign   = "-" if is_buy else "+"

    # Agent verdict lines
    verdict_lines = []
    for v in decision.verdicts:
        verdict_lines.append(
            f"{v.emoji()} <b>{v.agent.capitalize():<7}</b> {v.reason}  "
            f"[{v.confidence:.0%}]"
        )
    verdict_block = "\n".join(verdict_lines) if verdict_lines else "n/a"

    html_lines = [
        f"{emoji} <b>TRADE SETUP — {instr.name}</b>",
        f"<i>Signal detected: {now}</i>",
        "",
        f"Direction:    <b>{dir_label}</b>",
        f"Entry:        <b>{entry:,.2f}</b>",
        f"Take Profit:  <b>{tp:,.2f}</b>  ({tp_sign}{tp_pct:.1f}%)",
        f"Stop Loss:    <b>{sl:,.2f}</b>  ({sl_sign}{sl_pct:.1f}%)",
        f"R:R Ratio:    1 : {rr:.1f}",
        f"Size:         <b>{decision.lots:.2f} lots</b>  "
        f"(1% risk on ${ACCOUNT_SIZE_USD:,.0f})",
        "",
        f"📊 <b>Agent Analysis:</b>",
        verdict_block,
        "",
        "<i>Alert only — always confirm before trading.</i>",
    ]
    plain_lines = [line.replace("<b>", "").replace("</b>", "")
                       .replace("<i>", "").replace("</i>", "")
                   for line in html_lines]
    return "\n".join(html_lines), "\n".join(plain_lines)


def _notify(notifier, html: str, plain: str) -> None:
    if hasattr(notifier, "send_html"):
        notifier.send_html(html)
    else:
        notifier.send(plain)


# ── Heartbeat ─────────────────────────────────────────────────────────────────

_last_heartbeat: float = 0.0


def _maybe_send_heartbeat(notifier, instruments: list, logger: logging.Logger) -> None:
    global _last_heartbeat
    if time.time() - _last_heartbeat < HEARTBEAT_INTERVAL_S:
        return
    if any(time.time() - i._last_alert < HEARTBEAT_INTERVAL_S for i in instruments):
        _last_heartbeat = time.time()
        return

    status_lines = []
    for instr in instruments:
        scanned  = instr._scans_done
        skipped  = instr._scans_skipped
        if skipped > 0 and scanned == 0:
            if instr.epic in {"US500", "US100", "US30"}:
                note = "outside NYSE hours (open Mon–Fri 9:30–15:30 ET)"
            else:
                note = "outside trading session"
        elif scanned > 0:
            block = f" ({instr._last_block})" if instr._last_block else ""
            note  = f"scanned {scanned}×, no setup{block}"
        else:
            note = "no data yet"
        status_lines.append(f"  • <b>{instr.name}</b>: {note}")
        instr.reset_counters()

    status_block = "\n".join(status_lines)
    html  = ("🤖 <b>Alert bot — daily check-in</b>\n"
             "No trade setups detected in the last 24h.\n\n"
             f"<b>Last 24h per instrument:</b>\n{status_block}")
    plain = html.replace("<b>", "").replace("</b>", "")
    _notify(notifier, html, plain)
    _last_heartbeat = time.time()
    # persist heartbeat timestamp so it survives runner restarts
    try:
        try:
            with open(COOLDOWN_FILE) as f:
                _cd = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _cd = {}
        _cd["_last_heartbeat"] = _last_heartbeat
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(_cd, f)
    except OSError:
        pass
    logger.info("Daily heartbeat sent")


# ── US index consensus ────────────────────────────────────────────────────────

_US_INDEX_EPICS = frozenset({"US500", "US100", "US30"})

# ── Per-instrument scan ───────────────────────────────────────────────────────

def _evaluate_one(instr: _Instrument, feed: YahooFinanceFeed,
                  orchestrator: Orchestrator,
                  logger: logging.Logger):
    """
    Pre-flight checks, then delegate to Orchestrator.
    Returns (candles, TradeDecision) if a signal is approved, else None.

    Pre-flight (before fetching data — cheap):
      1. Cooldown  — skip if already alerted within ALERT_COOLDOWN_S
      2. Hours     — skip outside the instrument's trading session

    Orchestrator (after fetching candles):
      3. MarketAgent  — ADX + liquidity sweep + S/R confluence
      4. NewsAgent    — ForexFactory calendar + RSS breaking headlines
      5. RiskAgent    — lot sizing; blocks if ATR too large for account
    """
    if instr.on_cooldown():
        logger.debug("%s: cooldown active — skipping", instr.epic)
        return None

    if not is_tradeable(instr.epic):
        logger.info("%s: outside trading hours — skipping", instr.epic)
        instr.record_skip()
        return None

    try:
        candles  = feed.get_candles()
        decision = orchestrator.decide(instr.epic, candles)
        if decision.action == "skip":
            logger.info("%s: no signal — %s", instr.epic, decision.reason)
            instr.record_scan(decision.reason)
            return None
        instr.record_scan()
        return candles, decision
    except Exception as exc:
        logger.error("%s: evaluation error: %s", instr.epic, exc)
        return None


def _send_alert(instr: _Instrument, candles: dict, decision: TradeDecision,
                notifier, logger: logging.Logger) -> None:
    """Compute ATR-based TP/SL and send the Telegram alert."""
    try:
        h1         = candles.get(TF_H1, [])
        atr_series = _atr(h1, period=14)
        valid_atr  = [v for v in atr_series if v == v]
        if not valid_atr:
            logger.warning("%s: ATR unavailable — skipping alert", instr.epic)
            return

        current_atr = valid_atr[-1]
        entry       = h1[-1].close

        if decision.action == "buy":
            tp = entry + TP_ATR_MULT * current_atr
            sl = entry - SL_ATR_MULT * current_atr
        else:
            tp = entry - TP_ATR_MULT * current_atr
            sl = entry + SL_ATR_MULT * current_atr

        html, plain = _build_message(instr, decision, entry, tp, sl)
        _notify(notifier, html, plain)
        instr.mark_alerted()
        _save_cooldown(instr)
        logger.info(
            "Alert sent: %s %s  entry=%.2f  tp=%.2f  sl=%.2f  lots=%.2f  conf=%.0f%%",
            instr.epic, decision.action.upper(),
            entry, tp, sl, decision.lots, decision.confidence * 100,
        )
    except Exception as exc:
        logger.error("%s: alert error: %s", instr.epic, exc)


# ── Health server (keeps Render free tier alive) ──────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass


def _start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.getLogger(__name__).info("Health server listening on port %d", port)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    _start_health_server()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")

    if bot_token and chat_id:
        notifier = TelegramNotifier(bot_token, chat_id)
        logger.info("Telegram notifier ready (chat_id=%s)", chat_id)
    else:
        notifier = NullNotifier()
        logger.warning(
            "No Telegram credentials — alerts will be logged only.\n"
            "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to your .env file."
        )

    feeds: dict[str, YahooFinanceFeed] = {
        instr.epic: YahooFinanceFeed(instr.epic) for instr in WATCHLIST
    }

    _load_cooldowns(WATCHLIST)
    orchestrator = Orchestrator()

    import datetime as _dt
    _startup_time = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _notify(
        notifier,
        f"🟡 <b>Alert bot started</b> — <i>{_startup_time}</i>\n"
        "Watching Gold, S&amp;P 500, Nasdaq 100, Dow Jones. "
        "Multi-agent system active. Scanning every 15 min.",
        f"Alert bot started {_startup_time}. Multi-agent system active. Scanning every 15 min.",
    )
    logger.info("Startup notification sent")

    epic_list     = ", ".join(i.epic for i in WATCHLIST)
    max_runtime_s = int(os.getenv("MAX_RUNTIME_S", "0"))
    start_time    = time.time()

    logger.info("Alert bot running — watching %s, scanning every %ds%s",
                epic_list, SCAN_INTERVAL_S,
                f", max runtime {max_runtime_s}s" if max_runtime_s else "")

    while _running:
        # ── Phase 1: evaluate each instrument ────────────────────────────────
        pending: dict[str, tuple] = {}   # epic -> (instr, candles, decision)
        for instr in WATCHLIST:
            if not _running:
                break
            result = _evaluate_one(instr, feeds[instr.epic], orchestrator, logger)
            if result is not None:
                candles, decision = result
                pending[instr.epic] = (instr, candles, decision)
            time.sleep(3)   # stagger requests to avoid Yahoo Finance rate limits

        # ── Phase 2: US index consensus — suppress lone contradicting signal ─
        us_pending = {e: v for e, v in pending.items() if e in _US_INDEX_EPICS}
        if len(us_pending) >= 2:
            buy_count  = sum(1 for _, _, d in us_pending.values() if d.action == "buy")
            sell_count = sum(1 for _, _, d in us_pending.values() if d.action == "sell")
            if buy_count != sell_count:
                consensus = "buy" if buy_count > sell_count else "sell"
                for epic in list(pending.keys()):
                    if epic in _US_INDEX_EPICS and pending[epic][2].action != consensus:
                        logger.info(
                            "%s: suppressed — contradicts US index consensus "
                            "(%d buy / %d sell → %s)",
                            epic, buy_count, sell_count, consensus,
                        )
                        del pending[epic]

        # ── Phase 3: send all approved alerts ────────────────────────────────
        for epic, (instr, candles, decision) in pending.items():
            _send_alert(instr, candles, decision, notifier, logger)

        _maybe_send_heartbeat(notifier, WATCHLIST, logger)

        # ── Phase 4: persist scan counters so they survive runner restarts ────
        _save_scan_counters(WATCHLIST)

        if max_runtime_s and (time.time() - start_time) >= max_runtime_s:
            logger.info("Max runtime reached — exiting cleanly for handoff.")
            break

        if _running:
            _sleep_until_next_scan(logger)

    logger.info("Alert bot stopped cleanly.")


if __name__ == "__main__":
    main()
