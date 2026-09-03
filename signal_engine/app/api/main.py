"""
FastAPI backend. Run with:
    uvicorn app.api.main:app --reload --port 8000

Endpoints:
    GET  /api/signal?symbol=NIFTY&mode=SCALP
    GET  /api/signal?symbol=NIFTY&mode=SMART_TRADE
    POST /api/risk/record  {"pnl": -450}
    GET  /api/risk/summary
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import secrets
import sys
import threading
import time
import pandas as pd

from app.data_feed.simulator import SimulatorFeed
from app.data_feed.kite_feed import KiteFeed, SESSION_FILE
from app.signal_engine.orchestrator import generate_signal
from app.signal_engine.risk import RiskManager
from app.signal_engine.strike_selector import pick_strike, fixed_points_levels
from app.signal_engine.modes import (
    current_expiry_date,
    current_expiry_date_iso,
    default_symbol_for_today,
    is_expiry_today,
)
from app.signal_engine.backtest import run_backtest, build_backtest_trade_chart
from app.signal_engine import journal
from app.signal_engine.target_monitor import check_and_alert_t1
from app.signal_engine.live_capture import capture_entry, poll_open_positions
from app.signal_engine.auto_trade import maybe_auto_enter
from app.signal_engine.trade_chart import build_trade_chart
from app.config import CONFIG
from app.telegram_alerts import (
    format_signal_message,
    format_t1_hit_message,
    send_signal_alert,
    telegram_configured,
)

# Repo root on sys.path so `shared.kite_auth` (the same daily-login module
# used by scanner/ and execution/) is importable - mirrors the same trick
# KiteFeed.from_shared_auth() already does internally.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from shared import kite_auth as shared_kite_auth  # noqa: E402

app = FastAPI(title="Trading Signal Engine")

# Phone-friendly daily re-login: Kite access tokens expire every day, and
# this app deliberately does NOT automate the actual login (Zerodha's bot
# detection can block automated logins) - a human still completes it in a
# real browser each morning. This just moves "paste the token back" off of
# SSH and onto a bookmarked URL on your phone.
#
# MUST be set via env (e.g. in trading-platform/.env as
# SIGNAL_ENGINE_ADMIN_TOKEN=<a long random string>) - fails closed (404) if
# unset, rather than falling back to a guessable default.
ADMIN_TOKEN = os.environ.get("SIGNAL_ENGINE_ADMIN_TOKEN", "")

# --- shared state ---
# Preferred: authenticate through trading-platform/shared/kite_auth.py, the
# one daily login shared with scanner/ and execution/ (run
# `python -m shared.kite_auth` from the trading-platform root each morning).
# Falls back to the legacy per-app kite_session.json if present, then to the
# simulator so the app still runs standalone with neither.
DATA_SOURCE = "simulator"
try:
    feed = KiteFeed.from_shared_auth()
    DATA_SOURCE = "kite"
except Exception as shared_exc:
    if os.path.exists(SESSION_FILE):
        try:
            feed = KiteFeed.from_session_file()
            DATA_SOURCE = "kite"
        except Exception as e:
            print(f"[warn] Found kite_session.json but failed to load it ({e}); "
                  f"falling back to simulator.")
            feed = SimulatorFeed()
    else:
        print(f"[info] No shared Kite auth available ({shared_exc}); using simulator.")
        feed = SimulatorFeed()

risk_mgr = RiskManager()

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _background_scan_loop():
    """Runs entirely on the server, independent of any browser/phone
    having the dashboard open. Without this, auto-capture only ever fired
    because the dashboard's own JS polls /api/signal every few seconds -
    close every tab and nothing ran at all. This loop is what makes
    "capture data every day on EC2 without anyone accessing it" actually
    true, rather than accidentally depending on a UI staying open.

    References `feed`/`risk_mgr`/`DATA_SOURCE` by module-level name (not
    captured at thread-start) so it automatically picks up the live
    KiteFeed the /admin/<token>/login flow swaps in later, without a
    restart - and, critically, does NOTHING until that login has actually
    happened. Before that, `feed` is still SimulatorFeed; running the
    capture logic against it would silently log FAKE simulated trades
    into the real journal (this happened once already with manual test
    data - the whole point of this loop is to avoid a repeat of that).
    """
    from app.signal_engine.session import get_session_label

    print(f"[background] loop thread started "
          f"(every {CONFIG.auto_trade.background_poll_seconds}s, "
          f"active during market hours once today's Kite login is done)", flush=True)
    was_live = False
    while True:
        try:
            if DATA_SOURCE != "kite":
                if was_live:
                    print("[background] lost live feed (token expired?) - paused until re-login.", flush=True)
                    was_live = False
                time.sleep(CONFIG.auto_trade.background_poll_seconds)
                continue
            if not was_live:
                print("[background] live Kite feed detected - capturing until market close (15:30 IST).", flush=True)
                was_live = True

            session = get_session_label()
            if session != "MARKET CLOSED":
                for symbol in CONFIG.instruments:
                    try:
                        signal = generate_signal(feed, symbol, "SCALP", risk_mgr)
                        poll_open_positions(feed, auto_exit=True)
                        auto = maybe_auto_enter(feed, signal, risk_mgr)
                        if auto and auto.get("ok") and not auto.get("skipped"):
                            print(f"[background] auto-captured {symbol} trade #{auto.get('trade_id')}", flush=True)
                    except Exception as e:
                        print(f"[background] {symbol} cycle failed: {e}", flush=True)
        except Exception as e:
            print(f"[background] loop error: {e}", flush=True)
        time.sleep(CONFIG.auto_trade.background_poll_seconds)


@app.on_event("startup")
def _start_background_loop():
    if not CONFIG.auto_trade.enabled:
        print("[background] AUTO_TRADE disabled - background loop not started.")
        return
    threading.Thread(target=_background_scan_loop, daemon=True).start()


class AdminLoginRequest(BaseModel):
    pasted: str  # request_token, or the full Kite redirect URL


class TradeResult(BaseModel):
    pnl: float


class ManualTradeRequest(BaseModel):
    symbol: str = "SENSEX"
    side: str = "CE"
    otm_steps: int = 0
    sl_points: float
    t1_points: float
    t2_points: float


class TradeEntry(BaseModel):
    symbol: str
    side: str
    signal_source: str
    mode: str
    entry_price: float
    rr: float
    target1: float | None = None
    target2: float | None = None
    stop_loss: float | None = None
    contract: str | None = None
    expiry: str | None = None
    strike: int | None = None
    entry_premium: float | None = None
    t1_premium: float | None = None
    t2_premium: float | None = None
    sl_premium: float | None = None


class TradeExit(BaseModel):
    trade_id: int
    exit_price: float
    lot_size: int = 1
    points_per_lot_value: float = 1.0


class TelegramSendRequest(BaseModel):
    symbol: str = "SENSEX"
    mode: str = "SCALP"
    dry_run: bool = False


class LiveEnterRequest(BaseModel):
    symbol: str = "SENSEX"
    mode: str = "SCALP"
    otm_steps: int = 0  # 0=ATM, 1=1 OTM, 2=2 OTM
    lots: int = 1
    send_telegram: bool = True


@app.get("/api/signal")
def get_signal(
    symbol: str = Query("SENSEX"),
    mode: str = Query("SCALP"),
    otm_steps: int = Query(0, ge=0, le=5),
):
    mode = mode.upper()
    if mode not in ("SCALP", "SMART_TRADE", "GBB"):
        return {"error": "mode must be SCALP, SMART_TRADE, or GBB"}
    try:
        signal = generate_signal(feed, symbol, mode, risk_mgr, otm_steps=otm_steps)
        signal["auto_trade_enabled"] = CONFIG.auto_trade.enabled

        # 1) Manage open ATM/OTM positions (auto exit on premium T1/SL)
        try:
            live = poll_open_positions(feed, auto_exit=True)
            signal["live_positions"] = live
            signal["t1_alerts"] = [
                p for p in live if p.get("exited") and p.get("hit") == "T1"
            ]
        except Exception as e:
            signal["live_positions"] = []
            signal["t1_alerts"] = []
            signal["live_poll_error"] = str(e)

        # 2) Auto-enter new ATM/OTM capture when gates pass
        try:
            auto = maybe_auto_enter(feed, signal, risk_mgr, otm_steps=otm_steps)
            signal["auto_trade"] = auto
            if auto and auto.get("ok") and not auto.get("skipped"):
                # Refresh open list after new entry
                signal["live_positions"] = poll_open_positions(feed, auto_exit=False)
        except Exception as e:
            signal["auto_trade"] = {"ok": False, "error": str(e)}

        return signal
    except Exception as e:
        return {"error": str(e), "data_source": DATA_SOURCE}


@app.post("/api/live/enter")
def live_enter(req: LiveEnterRequest):
    """
    Capture a live trade: ATM (otm_steps=0) or OTM, with live option premium
    when Kite is available. Arms T1/SL monitoring on subsequent /api/signal polls.
    """
    mode = req.mode.upper()
    try:
        signal = generate_signal(
            feed, req.symbol, mode, risk_mgr, otm_steps=req.otm_steps
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if signal.get("error"):
        return {"ok": False, "error": signal["error"]}
    return capture_entry(
        feed, signal, risk_mgr,
        otm_steps=req.otm_steps,
        lots=req.lots,
        send_telegram=req.send_telegram,
    )


@app.get("/api/live/positions")
def live_positions(auto_exit: bool = Query(True)):
    """Snapshot open live captures; optionally auto-exit on T1/SL."""
    return {
        "data_source": DATA_SOURCE,
        "positions": poll_open_positions(feed, auto_exit=auto_exit),
    }


@app.get("/api/telegram/preview")
def telegram_preview(symbol: str = Query("SENSEX"), mode: str = Query("SCALP")):
    """Format the RSTA-style Telegram message without sending."""
    mode = mode.upper()
    try:
        signal = generate_signal(feed, symbol, mode, risk_mgr)
    except Exception as e:
        return {"error": str(e)}
    if signal.get("error"):
        return signal
    return {
        "configured": telegram_configured(),
        "message": format_signal_message(signal),
        "signal": {
            "verdict": signal.get("verdict"),
            "score": signal.get("score"),
            "base_score": signal.get("base_score"),
            "max_components": signal.get("max_components"),
            "side": signal.get("side"),
            "mode": signal.get("mode"),
        },
    }


@app.post("/api/telegram/send")
def telegram_send(req: TelegramSendRequest):
    """
    Send current signal to TELEGRAM_CHAT_ID (default @testalgotradinganand).
    Set dry_run=true to return the message without calling Telegram.
    Requires TELEGRAM_BOT_TOKEN (+ TELEGRAM_CHAT_ID) for a live send.
    """
    mode = req.mode.upper()
    try:
        signal = generate_signal(feed, req.symbol, mode, risk_mgr)
    except Exception as e:
        return {"error": str(e)}
    if signal.get("error"):
        return signal
    # Force dry_run when credentials missing so callers still get the text
    dry = req.dry_run or not telegram_configured()
    result = send_signal_alert(signal, dry_run=dry)
    result["configured"] = telegram_configured()
    return result


def _aligned_vix_series(feed, days: int, interval: str, index_df):
    """Fetches India VIX history and reindexes it onto index_df's exact
    timestamps (forward-filled) so it's safely position-aligned even if
    the two symbols' bars don't line up 1:1. Returns None (not raises) on
    any failure - callers fall back to index-only backtesting rather than
    hard-failing the whole report over a VIX fetch hiccup."""
    try:
        getter = getattr(feed, "get_ohlcv_history", None)
        if getter is None:
            return None
        vix_df = getter("INDIA VIX", days=days, interval=interval)
        if vix_df is None or vix_df.empty:
            return None
        vix_s = vix_df.set_index(pd.to_datetime(vix_df["timestamp"]))["close"]
        idx_ts = pd.to_datetime(index_df["timestamp"])
        aligned = vix_s.reindex(idx_ts, method="ffill")
        aligned.index = index_df.index
        return aligned
    except Exception as e:
        print(f"[warn] VIX history fetch failed, falling back to index-only backtest: {e}")
        return None


@app.get("/api/report/performance")
def performance_report(
    symbol: str = Query("SENSEX"),
    mode: str = Query("SCALP"),
    months: int = Query(3, ge=1, le=6),
    step_minutes: int = Query(15, ge=5, le=60),
    mark_to_market: bool = Query(True),
    min_score: int = Query(5, ge=0, le=9),
):
    """
    Backtest over the last N months using 5-minute candles.
    Default mark_to_market=True so hold-end close decides WIN/LOSS instead
    of leaving most trades as TIMEOUT (more honest for coarse bars).
    Simulates OPTION PREMIUM P&L (Black-Scholes, real VIX history) when
    VIX history is available - falls back to index points otherwise.
    """
    mode = mode.upper()
    days = months * 30
    try:
        getter = getattr(feed, "get_ohlcv_history", None)
        if getter is not None:
            df = getter(symbol, days=days, interval="5minute")
            bar_minutes = 5
            vix_series = _aligned_vix_series(feed, days, "5minute", df)
        else:
            df = feed.get_ohlcv_1m(symbol, lookback_minutes=min(days * 75, 2000))
            bar_minutes = 1
            vix_series = None
        report = run_backtest(
            df, symbol=symbol, mode=mode,
            step_minutes=step_minutes, bar_minutes=bar_minutes,
            mark_to_market=mark_to_market, min_score=min_score,
            vix_series=vix_series,
        )
        report["months"] = months
        report["days"] = days
        report["data_source"] = DATA_SOURCE
        return report
    except Exception as e:
        return {"error": str(e), "data_source": DATA_SOURCE}


@app.get("/api/report/performance/trade-chart")
def performance_trade_chart(
    trade_index: int = Query(..., ge=0),
    symbol: str = Query("SENSEX"),
    mode: str = Query("SCALP"),
    months: int = Query(3, ge=1, le=6),
    step_minutes: int = Query(15, ge=5, le=60),
    mark_to_market: bool = Query(True),
    min_score: int = Query(5, ge=0, le=9),
):
    """
    Candles + entry/exit/target/stop overlay for one backtest trade, in the
    same shape as /api/journal/{id}/chart so chart.html can render either.
    Re-runs the walk-forward simulation with identical params to reproduce
    the exact trade the caller saw in /api/report/performance's samples.
    """
    mode = mode.upper()
    days = months * 30
    try:
        getter = getattr(feed, "get_ohlcv_history", None)
        if getter is not None:
            df = getter(symbol, days=days, interval="5minute")
            bar_minutes = 5
            vix_series = _aligned_vix_series(feed, days, "5minute", df)
        else:
            df = feed.get_ohlcv_1m(symbol, lookback_minutes=min(days * 75, 2000))
            bar_minutes = 1
            vix_series = None
        return build_backtest_trade_chart(
            df, symbol=symbol, mode=mode, trade_index=trade_index,
            step_minutes=step_minutes, bar_minutes=bar_minutes,
            mark_to_market=mark_to_market, min_score=min_score,
            vix_series=vix_series,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/risk/record")
def record_trade(result: TradeResult):
    risk_mgr.record_trade_result(result.pnl)
    return risk_mgr.daily_summary()


@app.get("/api/risk/summary")
def risk_summary():
    return risk_mgr.daily_summary()


@app.get("/api/status")
def status():
    default_symbol = default_symbol_for_today()
    return {
        "data_source": DATA_SOURCE,
        "telegram_configured": telegram_configured(),
        "auto_trade_enabled": CONFIG.auto_trade.enabled,
        "auto_trade": {
            "enabled": CONFIG.auto_trade.enabled,
            "min_confidence_pct": CONFIG.auto_trade.min_confidence_pct,
            "default_otm_steps": CONFIG.auto_trade.default_otm_steps,
            "max_open_positions": CONFIG.auto_trade.max_open_positions,
        },
        "default_symbol": default_symbol,
        "expiry_today": {
            "NIFTY": is_expiry_today("NIFTY"),
            "SENSEX": is_expiry_today("SENSEX"),
            "active": default_symbol if (
                is_expiry_today("NIFTY") or is_expiry_today("SENSEX")
            ) else None,
        },
    }


@app.post("/api/manual-trade")
def manual_trade(req: ManualTradeRequest):
    spot = feed.get_spot_price(req.symbol)
    strike = pick_strike(spot, req.symbol, req.otm_steps, req.side)
    expiry_display = current_expiry_date(req.symbol)

    if DATA_SOURCE != "kite":
        return {
            "error": "Not on live Kite data (simulator mode) - no real premium "
                     "available. Fixed-points levels need a real entry premium, "
                     "not a fabricated one.",
            "spot": round(spot, 2),
            "picked_strike": strike,
            "expiry_display": expiry_display,
        }

    expiry_iso = current_expiry_date_iso(req.symbol)
    try:
        quote = feed.get_option_ltp(req.symbol, expiry_iso, strike, req.side)
    except Exception as e:
        return {
            "error": f"Could not fetch live premium: {e}",
            "spot": round(spot, 2),
            "picked_strike": strike,
            "expiry_display": expiry_display,
        }

    levels = fixed_points_levels(
        entry_price=quote["ltp"], side=req.side,
        sl_points=req.sl_points, t1_points=req.t1_points, t2_points=req.t2_points,
    )

    return {
        "spot": round(spot, 2),
        "picked_strike": strike,
        "expiry_display": expiry_display,
        "tradingsymbol": quote["tradingsymbol"],
        "levels": levels,
    }


@app.post("/api/journal/entry")
def journal_entry(trade: TradeEntry):
    trade_id = journal.log_entry(
        trade.symbol, trade.side, trade.signal_source, trade.mode,
        trade.entry_price, trade.rr,
        target1=trade.target1, target2=trade.target2,
        stop_loss=trade.stop_loss, contract=trade.contract,
        expiry=trade.expiry, strike=trade.strike,
        entry_premium=trade.entry_premium, t1_premium=trade.t1_premium,
        t2_premium=trade.t2_premium, sl_premium=trade.sl_premium,
    )
    return {"trade_id": trade_id, "watching_t1": trade.target1 is not None or trade.t1_premium is not None}


@app.post("/api/telegram/check-t1")
def telegram_check_t1(dry_run: bool = Query(False)):
    """Scan open trades for Target 1 hits and send alerts (once per trade)."""
    events = check_and_alert_t1(feed, dry_run=dry_run or not telegram_configured())
    return {
        "configured": telegram_configured(),
        "checked": len(events),
        "hits": [e for e in events if e.get("hit")],
        "events": events,
    }


@app.get("/api/telegram/t1-preview")
def telegram_t1_preview(symbol: str = Query("SENSEX"), mode: str = Query("SCALP")):
    """
    Preview the Target-1-hit Telegram message using the current signal's
    premium levels (simulates a hit at premium T1).
    """
    mode = mode.upper()
    try:
        signal = generate_signal(feed, symbol, mode, risk_mgr)
    except Exception as e:
        return {"error": str(e)}
    rec = signal.get("recommendation") or {}
    pl = rec.get("levels_premium") or {}
    prem = rec.get("premium") or {}
    entry_prem = pl.get("entry") or prem.get("mid")
    t1_prem = pl.get("target1")
    mock_trade = {
        "id": "PREVIEW",
        "symbol": symbol,
        "side": signal.get("side"),
        "mode": mode,
        "strike": rec.get("strike"),
        "expiry": rec.get("expiry"),
        "contract": rec.get("contract"),
        "entry_premium": entry_prem,
        "t1_premium": t1_prem,
        "entry_price": entry_prem,
        "target1": t1_prem,
    }
    return {
        "message": format_t1_hit_message(mock_trade),
        "entry_message": format_signal_message(signal),
        "mock_trade": mock_trade,
    }


@app.post("/api/journal/repair")
def journal_repair(trade_id: int | None = Query(None)):
    """Recompute premium P&L from Kite tape (T1/SL first-touch)."""
    from app.signal_engine.repair_trades import repair_all_premium_trades, repair_premium_trade
    if trade_id is not None:
        return repair_premium_trade(feed, trade_id)
    return {"ok": True, "repairs": repair_all_premium_trades(feed)}


@app.post("/api/live/exit")
def live_exit(trade_id: int = Query(...)):
    """
    Exit an open capture using live option LTP (not index / estimate).
    Falls back to last known premium mid only if quote fails.
    """
    from app.signal_engine.live_capture import LOT_SIZES, _live_option_quote

    trade = journal.get_trade(trade_id)
    if not trade:
        return {"ok": False, "error": f"Trade #{trade_id} not found"}
    if trade.get("result") != "OPEN":
        return {"ok": False, "error": f"Trade #{trade_id} is already {trade.get('result')}"}

    symbol = trade["symbol"]
    side = trade["side"]
    strike = trade.get("strike")
    lot_size = LOT_SIZES.get(symbol, 65)

    ltp = None
    quote = _live_option_quote(feed, symbol, strike, side) if strike else None
    if quote and quote.get("ltp") is not None:
        ltp = float(quote["ltp"])
    if ltp is None and trade.get("entry_premium") is not None:
        return {"ok": False, "error": "No live option LTP — cannot exit accurately"}

    if ltp is None:
        return {"ok": False, "error": "No exit price available"}

    info = journal.log_exit(
        trade_id,
        exit_price=ltp,
        lot_size=1,
        points_per_lot_value=float(lot_size),
    )
    return {"ok": True, "exit_premium": ltp, **info}


@app.post("/api/journal/exit")
def journal_exit(trade: TradeExit):
    # Prefer live premium exit path when trade has entry_premium
    existing = journal.get_trade(trade.trade_id)
    if existing and existing.get("entry_premium") is not None and existing.get("result") == "OPEN":
        # Keep lot multiplier from request, but pricing is premium-based in log_exit
        from app.signal_engine.live_capture import LOT_SIZES
        lot = LOT_SIZES.get(existing.get("symbol") or "NIFTY", 65)
        # If caller passed an index-looking exit (> 5x premium), reject
        ep = float(existing["entry_premium"])
        if trade.exit_price > ep * 5:
            return {
                "ok": False,
                "error": (
                    f"Exit {trade.exit_price} looks like index/estimate, not premium "
                    f"(entry premium {ep}). Use POST /api/live/exit?trade_id=…"
                ),
            }
        return journal.log_exit(
            trade.trade_id, trade.exit_price, 1, float(lot),
        )
    return journal.log_exit(trade.trade_id, trade.exit_price, trade.lot_size, trade.points_per_lot_value)


@app.get("/api/journal")
def journal_list(
    result: str | None = Query(None),
    symbol: str | None = Query(None),
):
    return journal.list_trades(result, symbol)


@app.get("/api/journal/{trade_id}/chart")
def journal_trade_chart(trade_id: int):
    """Candles + entry/exit/target/stop overlay data for the P&L chart page."""
    return build_trade_chart(feed, trade_id)


@app.get("/chart")
def trade_chart_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "chart.html"))


@app.get("/api/journal/export.csv")
def journal_export(symbol: str | None = Query(None)):
    csv_data = journal.export_csv(symbol)
    filename = f"trade_journal_{symbol.upper()}.csv" if symbol else "trade_journal.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.delete("/api/journal/{trade_id}")
def journal_delete(trade_id: int):
    """Delete a single journal record - used by the Journal tab's per-row
    delete button. 404s if the trade doesn't exist (already deleted, or a
    bad id) rather than silently succeeding."""
    deleted = journal.delete_trade(trade_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No trade with id {trade_id}")
    return {"deleted": True, "id": trade_id}


@app.post("/api/journal/purge")
def journal_purge(days: int = Query(7, ge=1)):
    """Delete trades older than `days` days (default 7) - use after a code
    update to clear out stale/test entries so the journal starts fresh."""
    deleted = journal.purge_older_than(days)
    return {"deleted": deleted, "kept_days": days}


@app.get("/api/journal/daily-summary")
def journal_daily_summary(
    day: str | None = Query(None),
    symbol: str | None = Query(None),
):
    return journal.daily_summary(day, symbol)


@app.post("/api/risk/reset")
def reset_day():
    risk_mgr.reset_day()
    return risk_mgr.daily_summary()


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


def _admin_login_page(message: str = "", is_error: bool = False) -> str:
    login_url = ""
    login_url_error = ""
    try:
        login_url = shared_kite_auth.get_manual_login_url()
    except Exception as e:
        login_url_error = str(e)

    banner = ""
    if message:
        color = "#f0554a" if is_error else "#22c58a"
        banner = f'<p style="color:{color};font-weight:600">{message}</p>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Engine — Daily Login</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background:#0d0d0c; color:#f2f1ec;
    max-width:480px; margin:0 auto; padding:24px 16px; }}
  h1 {{ font-size:18px; }}
  a.btn {{ display:block; text-align:center; background:#5b8cff; color:#fff;
    text-decoration:none; padding:14px; border-radius:10px; font-weight:700; margin:16px 0; }}
  input {{ width:100%; padding:12px; border-radius:8px; border:1px solid #2b2b27;
    background:#171715; color:#f2f1ec; font-size:15px; box-sizing:border-box; margin-bottom:10px; }}
  button {{ width:100%; padding:14px; border-radius:10px; border:none; background:#22c58a;
    color:#03110b; font-weight:700; font-size:15px; }}
  p.hint {{ color:#9a998f; font-size:13px; line-height:1.5; }}
</style></head>
<body>
  <h1>Signal Engine — Daily Kite Login</h1>
  {banner}
  {'<p style="color:#f0554a">Could not build login URL: ' + login_url_error + '</p>' if login_url_error else f'<a class="btn" href="{login_url}" target="_blank" rel="noopener">1. Open Kite Login ↗</a>'}
  <p class="hint">Log in with your Zerodha user ID, password, and TOTP as usual.
  You'll land on a blank/error redirect page — that's expected. Copy the
  FULL URL from your browser's address bar (or just the request_token
  value) and paste it below.</p>
  <input id="pasted" placeholder="Paste redirect URL or request_token" autocomplete="off">
  <button onclick="submitLogin()">2. Complete Login</button>
  <p class="hint" id="status"></p>
<script>
async function submitLogin() {{
  const pasted = document.getElementById('pasted').value.trim();
  const statusEl = document.getElementById('status');
  if (!pasted) {{ statusEl.textContent = 'Paste the redirect URL or request_token first.'; return; }}
  statusEl.textContent = 'Submitting…';
  try {{
    const res = await fetch(window.location.pathname, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{pasted}}),
    }});
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    statusEl.style.color = '#22c58a';
    statusEl.textContent = '✓ Logged in — live Kite data is now active.';
  }} catch (e) {{
    statusEl.style.color = '#f0554a';
    statusEl.textContent = 'Failed: ' + (e.message || e);
  }}
}}
</script>
</body></html>"""


@app.get("/admin/{token}/login", response_class=HTMLResponse)
def admin_login_page(token: str):
    if not ADMIN_TOKEN or not secrets.compare_digest(token, ADMIN_TOKEN):
        return HTMLResponse("Not found", status_code=404)
    return _admin_login_page()


@app.post("/admin/{token}/login")
def admin_login_submit(token: str, req: AdminLoginRequest):
    if not ADMIN_TOKEN or not secrets.compare_digest(token, ADMIN_TOKEN):
        return HTMLResponse("Not found", status_code=404)

    global feed, DATA_SOURCE
    try:
        shared_kite_auth.exchange_request_token(req.pasted)
    except Exception as e:
        return {"error": str(e)}

    # The running process's `feed` object has the OLD token baked into its
    # KiteConnect client - writing the new token to disk alone does nothing
    # for an already-running process. Re-resolve it now so this request
    # actually starts using live data immediately, not after a restart.
    try:
        feed = KiteFeed.from_shared_auth()
        DATA_SOURCE = "kite"
    except Exception as e:
        return {"error": f"Token saved, but failed to activate live feed: {e}"}

    return {"ok": True, "data_source": DATA_SOURCE}


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
