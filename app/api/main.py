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
from app.signal_engine.modes import (
    current_expiry_date,
    current_expiry_date_iso,
    default_symbol_for_today,
    is_expiry_today,
)
from app.signal_engine.backtest import run_backtest
from app.signal_engine import journal
from app.signal_engine.target_monitor import check_and_alert_t1
from app.signal_engine.live_capture import capture_entry, poll_open_positions
from app.signal_engine.auto_trade import maybe_auto_enter
from app.config import CONFIG
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


class LiveEnterRequest(BaseModel):
    symbol: str = "NIFTY"
    mode: str = "SCALP"
    otm_steps: int = 0  # 0=ATM, 1=1 OTM, 2=2 OTM
    lots: int = 1
    send_telegram: bool = True


@app.get("/api/signal")
def get_signal(
    symbol: str = Query("NIFTY"),
    mode: str = Query("SCALP"),
    otm_steps: int = Query(0, ge=0, le=5),
):
    mode = mode.upper()
    if mode not in ("SCALP", "SMART_TRADE"):
        return {"error": "mode must be SCALP or SMART_TRADE"}
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
    months: int = Query(3, ge=1, le=6),
    step_minutes: int = Query(15, ge=5, le=60),
    mark_to_market: bool = Query(True),
    min_score: int = Query(4, ge=0, le=9),
):
    """
    Backtest over the last N months using 5-minute candles.
    Default mark_to_market=True so hold-end close decides WIN/LOSS instead
    of leaving most trades as TIMEOUT (more honest for coarse bars).
    """
    mode = mode.upper()
    days = months * 30
    try:
        getter = getattr(feed, "get_ohlcv_history", None)
        if getter is not None:
            df = getter(symbol, days=days, interval="5minute")
            bar_minutes = 5
        else:
            df = feed.get_ohlcv_1m(symbol, lookback_minutes=min(days * 75, 2000))
            bar_minutes = 1
        report = run_backtest(
            df, symbol=symbol, mode=mode,
            step_minutes=step_minutes, bar_minutes=bar_minutes,
            mark_to_market=mark_to_market, min_score=min_score,
        )
        report["months"] = months
        report["days"] = days
        report["data_source"] = DATA_SOURCE
        return report
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
def journal_list(
    result: str | None = Query(None),
    symbol: str | None = Query(None),
):
    return journal.list_trades(result, symbol)


@app.get("/api/journal/export.csv")
def journal_export(symbol: str | None = Query(None)):
    csv_data = journal.export_csv(symbol)
    filename = f"trade_journal_{symbol.upper()}.csv" if symbol else "trade_journal.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


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


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
