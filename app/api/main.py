"""
FastAPI backend. Run with:
    uvicorn app.api.main:app --reload --port 8000

Endpoints:
    GET  /api/signal?symbol=NIFTY&mode=SCALP
    GET  /api/signal?symbol=NIFTY&mode=SMART_TRADE
    POST /api/risk/record  {"pnl": -450}
    GET  /api/risk/summary
"""

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os

from app.data_feed.simulator import SimulatorFeed
from app.data_feed.kite_feed import KiteFeed, SESSION_FILE
from app.signal_engine.orchestrator import generate_signal
from app.signal_engine.risk import RiskManager
from app.signal_engine.strike_selector import pick_strike, fixed_points_levels
from app.signal_engine.modes import current_expiry_date, current_expiry_date_iso
from app.signal_engine.backtest import run_backtest
from app.signal_engine import journal
from app.signal_engine.target_monitor import check_and_alert_t1
from app.telegram_alerts import (
    format_signal_message,
    format_t1_hit_message,
    send_signal_alert,
    telegram_configured,
)

app = FastAPI(title="Trading Signal Engine")

# --- shared state ---
# If app/kite_session.json exists (created by scripts/generate_session.py),
# use live Kite data. Otherwise fall back to the simulator so the app still
# runs standalone. Run scripts/generate_session.py each morning to refresh
# the token - Kite access tokens expire daily.
DATA_SOURCE = "simulator"
if os.path.exists(SESSION_FILE):
    try:
        feed = KiteFeed.from_session_file()
        DATA_SOURCE = "kite"
    except Exception as e:
        print(f"[warn] Found kite_session.json but failed to load it ({e}); "
              f"falling back to simulator.")
        feed = SimulatorFeed()
else:
    feed = SimulatorFeed()

risk_mgr = RiskManager()

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


class TradeResult(BaseModel):
    pnl: float


class ManualTradeRequest(BaseModel):
    symbol: str = "NIFTY"
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
    symbol: str = "NIFTY"
    mode: str = "SCALP"
    dry_run: bool = False


@app.get("/api/signal")
def get_signal(symbol: str = Query("NIFTY"), mode: str = Query("SCALP")):
    mode = mode.upper()
    if mode not in ("SCALP", "SMART_TRADE"):
        return {"error": "mode must be SCALP or SMART_TRADE"}
    try:
        signal = generate_signal(feed, symbol, mode, risk_mgr)
        # Side-effect: check open trades for Target 1 hits and alert once
        try:
            t1_events = check_and_alert_t1(feed)
            signal["t1_alerts"] = [e for e in t1_events if e.get("hit")]
        except Exception as e:
            signal["t1_alerts"] = []
            signal["t1_alert_error"] = str(e)
        return signal
    except Exception as e:
        # Keep the dashboard usable and avoid flooding the terminal with
        # ASGI stack traces when the feed has no data (e.g. bad token).
        return {"error": str(e), "data_source": DATA_SOURCE}


@app.get("/api/telegram/preview")
def telegram_preview(symbol: str = Query("NIFTY"), mode: str = Query("SCALP")):
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


@app.get("/api/report/performance")
def performance_report(
    symbol: str = Query("NIFTY"),
    mode: str = Query("SCALP"),
    step_minutes: int = Query(5, ge=1, le=30),
):
    """
    Backtest win/loss rate on recent 1m candles from the active feed.
    WIN = T1 before SL within hold window.
    """
    mode = mode.upper()
    try:
        df = feed.get_ohlcv_1m(symbol, lookback_minutes=375)  # ~1 full session
        return run_backtest(df, symbol=symbol, mode=mode, step_minutes=step_minutes)
    except Exception as e:
        return {"error": str(e), "data_source": DATA_SOURCE}


@app.post("/api/risk/record")
def record_trade(result: TradeResult):
    risk_mgr.record_trade_result(result.pnl)
    return risk_mgr.daily_summary()


@app.get("/api/risk/summary")
def risk_summary():
    return risk_mgr.daily_summary()


@app.get("/api/status")
def status():
    return {
        "data_source": DATA_SOURCE,
        "telegram_configured": telegram_configured(),
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
def telegram_t1_preview(symbol: str = Query("NIFTY"), mode: str = Query("SCALP")):
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


@app.post("/api/journal/exit")
def journal_exit(trade: TradeExit):
    return journal.log_exit(trade.trade_id, trade.exit_price, trade.lot_size, trade.points_per_lot_value)


@app.get("/api/journal")
def journal_list(result: str | None = Query(None)):
    return journal.list_trades(result)


@app.get("/api/journal/export.csv")
def journal_export():
    csv_data = journal.export_csv()
    return Response(content=csv_data, media_type="text/csv",
                     headers={"Content-Disposition": "attachment; filename=trade_journal.csv"})


@app.get("/api/journal/daily-summary")
def journal_daily_summary(day: str | None = Query(None)):
    return journal.daily_summary(day)


@app.post("/api/risk/reset")
def reset_day():
    risk_mgr.reset_day()
    return risk_mgr.daily_summary()


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
