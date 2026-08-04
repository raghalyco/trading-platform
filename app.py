"""
Local web dashboard: a "Run Scan" button that shows today's stocks passing
the scanner (like a Chartink screener result), and a "Run Backtest" button
that shows historical win rate / P&L / trade log for the same rules.

Run with: python app.py
Then open: http://127.0.0.1:5000

Single entry point: this file bootstraps itself on a fresh clone (installs
missing pip packages, creates .env from .env.example, creates cache/data/
results dirs) — see bootstrap.py. The only thing it can't automate is your
actual Kite API key/secret, which you fill in once.
"""
import bootstrap
bootstrap.bootstrap()

import sys
import datetime as dt
from datetime import datetime, time as dtime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from flask import Flask, jsonify, render_template
from flask.json.provider import DefaultJSONProvider

import config
import backtest
import live_scan
import universe as universe_mod
import intraday_engine
import smart_money_pipeline
import smart_money_backtest
from kite_auth import get_kite_session
from kite_client import KiteDataClient


class ISODateJSONProvider(DefaultJSONProvider):
    """Flask's default JSON provider serializes any raw date/datetime object
    using HTTP-date format (e.g. 'Thu, 31 Jul 2025 00:00:00 GMT') — that's
    where the GMT timestamps were coming from. This forces plain ISO date
    strings ('2025-07-31') instead, no matter which field it is."""
    def default(self, obj):
        if isinstance(obj, (dt.date, dt.datetime)):
            return obj.isoformat()
        return super().default(obj)


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.json = ISODateJSONProvider(app)


def _check_credentials():
    placeholders = {
        "KITE_API_KEY": "your_api_key_here",
        "KITE_API_SECRET": "your_api_secret_here",
    }
    missing_or_placeholder = []
    for var, placeholder in placeholders.items():
        val = getattr(config, var, "")
        if not val or val == placeholder:
            missing_or_placeholder.append(var)

    if missing_or_placeholder:
        env_path = Path(__file__).resolve().parent / ".env"
        print("FATAL: the following credentials are missing or still contain "
              "placeholder values:")
        for var in missing_or_placeholder:
            print(f"  - {var}")
        print(f"\nExpected a real .env file at: {env_path}")
        print(f"  exists: {env_path.exists()}")
        print("\nFix: copy .env.example to .env in THIS folder "
              f"({env_path.parent}) and fill in your real Kite Connect "
              "api_key/api_secret from developers.kite.trade, then rerun.")
        sys.exit(1)


_check_credentials()

# --- One login for the whole server session (access token is valid all day) ---
print("Logging into Kite...")
try:
    _kite = get_kite_session()
    _client = KiteDataClient(_kite)
    print("Logged in.")
except Exception as e:
    print(f"FATAL: could not log into Kite: {e}")
    sys.exit(1)

# Universe is rebuilt once per server run (cheap for nifty500, avoid
# re-fetching on every button click). Restart the server if you want a
# fresh Nifty 500 list pulled (it's also cached 24h on disk regardless).
print(f"Building universe (mode={config.UNIVERSE_MODE})...")
_universe_df = universe_mod.build_universe(_client)
print(f"Universe ready: {len(_universe_df)} symbols.")

_last_scan_cache = {"generated_at": None, "results": []}
_sector_cache = {"generated_at": None, "results": []}
_trending_cache = {"generated_at": None, "sectors": {}}
_stock_for_day_cache = {"generated_at": None, "payload": None}


def _refresh_sector_cache():
    print("Warming Sector Analysis cache...")
    results = live_scan.run_sector_scan(_client)
    _sector_cache["generated_at"] = datetime.now().isoformat()
    _sector_cache["results"] = results
    print(f"Sector Analysis ready: {len(results)} sector(s).")
    return _sector_cache


def _refresh_trending_cache():
    print("Warming Trending Stocks by Sector cache...")
    sectors = live_scan.run_trending_scan(_client, _universe_df)
    _trending_cache["generated_at"] = datetime.now().isoformat()
    _trending_cache["sectors"] = sectors
    print(f"Trending Stocks ready: {len(sectors)} sector(s).")
    return _trending_cache


def _want_refresh() -> bool:
    """POST always refreshes; GET refreshes only with ?refresh=1."""
    from flask import request
    if request.method == "POST":
        return True
    return request.args.get("refresh", "0") in ("1", "true", "True")


# Precompute Sector + Trending so the dashboard can paint immediately.
# With Flask's reloader: warm only in the child (WERKZEUG_RUN_MAIN=true).
# With debug off: warm in this process. API handlers also fill an empty cache
# on first request as a safety net.
import os as _os
if (
    _os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    or _os.environ.get("FLASK_DEBUG", "1") in ("0", "false", "False")
):
    _refresh_sector_cache()
    _refresh_trending_cache()


def _is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


@app.route("/")
def index():
    return render_template("index.html", universe_size=len(_universe_df),
                            universe_mode=config.UNIVERSE_MODE,
                            ema10_max_distance=config.EMA10_MAX_DISTANCE_PCT,
                            nday_proximity=config.NDAY_PROXIMITY_PCT)


@app.route("/api/scan", methods=["POST", "GET"])
def api_scan():
    hits = live_scan.run_scan(_client, _universe_df)
    if _is_market_open():
        hits = live_scan.enrich_with_live_quotes(_client, hits)
    else:
        for h in hits:
            h["ltp"] = h["close"]

    hits.sort(key=lambda h: h["rsi14"], reverse=True)
    _last_scan_cache["generated_at"] = datetime.now().isoformat()
    _last_scan_cache["results"] = hits

    up = sum(1 for h in hits if (h.get("pct_change") or 0) > 0)
    down = sum(1 for h in hits if (h.get("pct_change") or 0) < 0)

    return jsonify({
        "generated_at": _last_scan_cache["generated_at"],
        "market_open": _is_market_open(),
        "universe_size": len(_universe_df),
        "num_signals": len(hits),
        "up": up,
        "down": down,
        "results": hits,
    })


@app.route("/api/scan_ema10", methods=["POST", "GET"])
def api_scan_ema10():
    hits = live_scan.run_ema10_scan(_client, _universe_df)
    if _is_market_open():
        hits = live_scan.enrich_with_live_quotes(_client, hits)
    else:
        for h in hits:
            h["ltp"] = h["close"]

    hits.sort(key=lambda h: h["distance_from_ema10_pct"])
    generated_at = datetime.now().isoformat()

    up = sum(1 for h in hits if (h.get("pct_change") or 0) > 0)
    down = sum(1 for h in hits if (h.get("pct_change") or 0) < 0)

    return jsonify({
        "generated_at": generated_at,
        "market_open": _is_market_open(),
        "universe_size": len(_universe_df),
        "num_signals": len(hits),
        "up": up,
        "down": down,
        "results": hits,
    })


@app.route("/api/scan_nday", methods=["POST", "GET"])
def api_scan_nday():
    from flask import request
    lookback = request.args.get("lookback", default=10, type=int)
    if lookback not in config.NDAY_LOOKBACKS:
        return jsonify({"error": f"lookback must be one of {config.NDAY_LOOKBACKS}"}), 400

    hits = live_scan.run_nday_scan(_client, _universe_df, lookback)
    if _is_market_open():
        hits = live_scan.enrich_with_live_quotes(_client, hits)
    else:
        for h in hits:
            h["ltp"] = h["close"]

    hits.sort(key=lambda h: h["signal_date"], reverse=True)
    generated_at = datetime.now().isoformat()

    up = sum(1 for h in hits if (h.get("pct_change") or 0) > 0)
    down = sum(1 for h in hits if (h.get("pct_change") or 0) < 0)

    return jsonify({
        "lookback": lookback,
        "generated_at": generated_at,
        "market_open": _is_market_open(),
        "universe_size": len(_universe_df),
        "num_signals": len(hits),
        "up": up,
        "down": down,
        "results": hits,
    })


@app.route("/api/scan_sector", methods=["POST", "GET"])
def api_scan_sector():
    refresh = _want_refresh()
    if refresh or not _sector_cache["generated_at"]:
        _refresh_sector_cache()
    return jsonify({
        "generated_at": _sector_cache["generated_at"],
        "market_open": _is_market_open(),
        "num_sectors": len(_sector_cache["results"]),
        "results": _sector_cache["results"],
        "cached": not refresh,
    })


@app.route("/api/intraday/start", methods=["POST"])
def api_intraday_start():
    result = intraday_engine.engine.start(_kite, _client, _universe_df)
    status_code = 400 if result.get("error") else 200
    return jsonify(result), status_code


@app.route("/api/intraday/stop", methods=["POST"])
def api_intraday_stop():
    result = intraday_engine.engine.stop()
    return jsonify(result)


@app.route("/api/intraday/status", methods=["GET"])
def api_intraday_status():
    return jsonify(intraday_engine.engine.get_status())


@app.route("/api/scan_trending", methods=["POST", "GET"])
def api_scan_trending():
    refresh = _want_refresh()
    if refresh or not _trending_cache["generated_at"]:
        _refresh_trending_cache()
    return jsonify({
        "generated_at": _trending_cache["generated_at"],
        "market_open": _is_market_open(),
        "num_sectors": len(_trending_cache["sectors"]),
        "sectors": _trending_cache["sectors"],
        "cached": not refresh,
    })


@app.route("/api/smart_money/scan", methods=["POST", "GET"])
def api_smart_money_scan():
    """Full requirement flow: top sectors → leaders → Smart Money gates → signals.
    Pass ?telegram=0 to skip Telegram even if SMART_MONEY_SEND_TELEGRAM is on."""
    from flask import request
    send_tg = request.args.get("telegram", "1") not in ("0", "false", "False")
    result = smart_money_pipeline.run_pipeline(
        _client, _universe_df, send_telegram=send_tg and config.SMART_MONEY_SEND_TELEGRAM
    )
    result["market_open"] = _is_market_open()
    return jsonify(result)


@app.route("/api/stock_for_day", methods=["POST", "GET"])
def api_stock_for_day():
    """Full-universe Smart Money structure scan — BUY-eligible stocks only."""
    refresh = _want_refresh()
    if refresh or not _stock_for_day_cache["generated_at"]:
        print("Running Stock for day scan (full universe + Smart Money)...")
        payload = smart_money_pipeline.scan_stock_for_day(
            _client, _universe_df, send_telegram=config.STOCK_FOR_DAY_SEND_TELEGRAM
        )
        _stock_for_day_cache["generated_at"] = payload["generated_at"]
        _stock_for_day_cache["payload"] = payload
    out = dict(_stock_for_day_cache["payload"] or {})
    out["market_open"] = _is_market_open()
    out["cached"] = not refresh
    return jsonify(out)


@app.route("/api/stock_for_day/backtest", methods=["POST", "GET"])
def api_stock_for_day_backtest():
    """3-month Smart Money success-rate backtest on Nifty 50/100."""
    try:
        result = smart_money_backtest.run_stock_for_day_backtest(_client)
        result["market_open"] = _is_market_open()
        return jsonify(result)
    except Exception as e:
        print(f"[stock_for_day/backtest] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": str(e),
            "summary": {"total_trades": 0},
            "trades": [],
            "window_start": None,
            "window_end": None,
        }), 500


@app.route("/api/backtest", methods=["POST", "GET"])
def api_backtest():
    trades_df = backtest.run_backtest(_client, _universe_df)
    summary = backtest.summarize(trades_df)
    trades = [] if trades_df.empty else trades_df.to_dict(orient="records")
    trades.sort(key=lambda t: t["signal_date"], reverse=True)
    return jsonify({
        "window_start": str(config.BACKTEST_START),
        "window_end": str(config.BACKTEST_END),
        "summary": summary,
        "trades": trades,
    })


if __name__ == "__main__":
    # Local-dev auto rebuild: restart the process whenever .py / template files
    # change. Access token is cached in cache/access_token.json, so reloader
    # restarts do not re-prompt for Kite login. Set FLASK_DEBUG=0 to disable.
    import os
    debug = os.environ.get("FLASK_DEBUG", "1") not in ("0", "false", "False")
    app.jinja_env.auto_reload = True
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=debug,
        use_reloader=debug,
        extra_files=[
            str(Path(__file__).resolve().parent / "templates" / "index.html"),
            str(Path(__file__).resolve().parent / "config.py"),
        ],
    )
