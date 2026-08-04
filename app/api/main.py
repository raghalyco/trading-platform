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
from app.signal_engine import journal

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


class TradeExit(BaseModel):
    trade_id: int
    exit_price: float
    lot_size: int = 1
    points_per_lot_value: float = 1.0


@app.get("/api/signal")
def get_signal(symbol: str = Query("NIFTY"), mode: str = Query("SCALP")):
    mode = mode.upper()
    if mode not in ("SCALP", "SMART_TRADE"):
        return {"error": "mode must be SCALP or SMART_TRADE"}
    return generate_signal(feed, symbol, mode, risk_mgr)


@app.post("/api/risk/record")
def record_trade(result: TradeResult):
    risk_mgr.record_trade_result(result.pnl)
    return risk_mgr.daily_summary()


@app.get("/api/risk/summary")
def risk_summary():
    return risk_mgr.daily_summary()


@app.get("/api/status")
def status():
    return {"data_source": DATA_SOURCE}


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
    )
    return {"trade_id": trade_id}


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
