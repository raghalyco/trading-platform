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
from typing import Optional

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
import support_bounce
import swing_trade
import darvax
import trade_tracker
import strategy_performance
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

_last_scan_cache = {"generated_at": None, "payload": None}
_ema10_cache = {"generated_at": None, "payload": None}
_nday_cache = {}  # lookback -> {generated_at, payload}
_sector_cache = {"generated_at": None, "results": []}
_trending_cache = {"generated_at": None, "sectors": {}}
# Per-universe cache: mode -> {generated_at, payload}
_stock_for_day_cache: dict = {}
_support_bounce_cache: dict = {}
_swing_trade_cache: dict = {}
_darvax_cache: dict = {}


def _enrich_hits(hits):
    if _is_market_open():
        return live_scan.enrich_with_live_quotes(_client, hits)
    for h in hits:
        h["ltp"] = h["close"]
    return hits


def _hits_payload(hits, generated_at=None, extra=None):
    hits = list(hits)
    up = sum(1 for h in hits if (h.get("pct_change") or 0) > 0)
    down = sum(1 for h in hits if (h.get("pct_change") or 0) < 0)
    payload = {
        "generated_at": generated_at or datetime.now().isoformat(),
        "market_open": _is_market_open(),
        "universe_size": len(_universe_df),
        "num_signals": len(hits),
        "up": up,
        "down": down,
        "results": hits,
        "charts": support_bounce.charts_snapshot([h["symbol"] for h in hits if h.get("symbol")]),
    }
    if extra:
        payload.update(extra)
    return payload


def _refresh_scan_cache():
    print("Warming 13-rule Breakout Scanner cache...")
    hits = _enrich_hits(live_scan.run_scan(_client, _universe_df))
    hits.sort(key=lambda h: h["rsi14"], reverse=True)
    payload = _hits_payload(hits)
    _last_scan_cache["generated_at"] = payload["generated_at"]
    _last_scan_cache["payload"] = payload
    print(f"Breakout Scanner ready: {len(hits)} signal(s).")
    return payload


def _refresh_ema10_cache():
    print("Warming EMA10 Pullback Scanner cache...")
    hits = _enrich_hits(live_scan.run_ema10_scan(_client, _universe_df))
    hits.sort(key=lambda h: h["distance_from_ema10_pct"])
    payload = _hits_payload(hits)
    _ema10_cache["generated_at"] = payload["generated_at"]
    _ema10_cache["payload"] = payload
    print(f"EMA10 Scanner ready: {len(hits)} signal(s).")
    return payload


def _refresh_nday_cache(lookback: int):
    print(f"Warming N-Day BO cache (lookback={lookback})...")
    hits = _enrich_hits(live_scan.run_nday_scan(_client, _universe_df, lookback))
    hits.sort(key=lambda h: h["signal_date"], reverse=True)
    payload = _hits_payload(hits, extra={"lookback": lookback})
    _nday_cache[lookback] = {"generated_at": payload["generated_at"], "payload": payload}
    print(f"N-Day BO ({lookback}D) ready: {len(hits)} signal(s).")
    return payload


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


def _rehydrate_payload_charts(payload: Optional[dict]) -> None:
    if not payload:
        return
    support_bounce.rehydrate_charts(payload.get("charts"))


def _find_cached_chart(symbol: str):
    """Look up chart geometry across all scanner caches."""
    symbol = str(symbol).upper()
    payload = support_bounce.get_chart_payload(symbol)
    if payload is not None:
        return payload
    caches = []
    for cached in _support_bounce_cache.values():
        caches.append(cached.get("payload") or {})
    for cached in _stock_for_day_cache.values():
        caches.append(cached.get("payload") or {})
    if _last_scan_cache.get("payload"):
        caches.append(_last_scan_cache["payload"])
    if _ema10_cache.get("payload"):
        caches.append(_ema10_cache["payload"])
    for cached in _nday_cache.values():
        caches.append(cached.get("payload") or {})
    for payload in caches:
        charts = payload.get("charts") or {}
        if symbol in charts:
            support_bounce.store_chart_payload(symbol, charts[symbol])
            return charts[symbol]
    return None


def _refresh_stock_for_day_cache(mode=None):
    mode = mode or (config.STOCK_FOR_DAY_UNIVERSE or "nifty100")
    try:
        mode = universe_mod.normalize_nifty_mode(mode)
    except ValueError:
        mode = "nifty100"
    print(f"Warming Stock for day cache ({mode})...")
    payload = smart_money_pipeline.scan_stock_for_day(
        _client,
        _universe_df,
        send_telegram=False,
        universe_mode=mode,
    )
    _stock_for_day_cache[mode] = {
        "generated_at": payload["generated_at"],
        "payload": payload,
    }
    support_bounce.rehydrate_charts(payload.get("charts"))
    print(f"Stock for day ready: {payload.get('num_buys', 0)} BUY / {payload.get('num_sells', 0)} SELL on {payload.get('universe_label', mode)}.")
    return payload


def _refresh_support_bounce_cache(mode=None):
    mode = mode or (config.SUPPORT_BOUNCE_UNIVERSE or "nifty100")
    try:
        mode = universe_mod.normalize_nifty_mode(mode)
    except ValueError:
        mode = "nifty100"
    print(f"Warming Previous Support Bounce cache ({mode})...")
    payload = support_bounce.scan_support_bounce(
        _client, _universe_df, universe_mode=mode
    )
    _support_bounce_cache[mode] = {
        "generated_at": payload["generated_at"],
        "payload": payload,
    }
    support_bounce.rehydrate_charts(payload.get("charts"))
    print(
        f"Support Bounce ready: {payload.get('num_results', 0)} hit(s) on "
        f"{payload.get('universe_label', mode)}."
    )
    return payload


def _refresh_swing_trade_cache(mode=None):
    mode = mode or (config.SWING_TRADE_UNIVERSE or "nifty200")
    try:
        mode = universe_mod.normalize_nifty_mode(mode)
    except ValueError:
        mode = "nifty200"
    print(f"Warming Swing Trade (Weekly) cache ({mode})...")
    payload = swing_trade.scan_swing_trade(
        _client, _universe_df, universe_mode=mode
    )
    _swing_trade_cache[mode] = {
        "generated_at": payload["generated_at"],
        "payload": payload,
    }
    swing_trade.rehydrate_charts(payload.get("charts"))
    print(
        f"Swing Trade ready: {payload.get('num_results', 0)} hit(s) on "
        f"{payload.get('universe_label', mode)}."
    )
    return payload


def _refresh_darvax_cache(mode=None):
    mode = mode or (config.DARVAX_UNIVERSE or "nifty200")
    try:
        mode = universe_mod.normalize_nifty_mode(mode)
    except ValueError:
        mode = "nifty200"
    print(f"Warming DarvaX cache ({mode})...")
    payload = darvax.scan_darvax(_client, _universe_df, universe_mode=mode)
    _darvax_cache[mode] = {
        "generated_at": payload["generated_at"],
        "payload": payload,
    }
    darvax.rehydrate_charts(payload.get("charts"))
    print(
        f"DarvaX ready: {payload.get('num_results', 0)} hit(s) on "
        f"{payload.get('universe_label', mode)}."
    )
    return payload


def _want_refresh() -> bool:
    """POST always refreshes; GET refreshes only with ?refresh=1."""
    from flask import request
    if request.method == "POST":
        return True
    return request.args.get("refresh", "0") in ("1", "true", "True")


def _is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


# Precompute scanner caches so every tab can paint immediately.
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
    _refresh_stock_for_day_cache()
    _refresh_support_bounce_cache()
    _refresh_swing_trade_cache()
    _refresh_darvax_cache()


@app.route("/")
def index():
    return render_template(
        "index.html",
        universe_size=len(_universe_df),
        universe_mode=config.UNIVERSE_MODE,
        ema10_max_distance=config.EMA10_MAX_DISTANCE_PCT,
        nday_proximity=config.NDAY_PROXIMITY_PCT,
        stock_for_day_universe=config.STOCK_FOR_DAY_UNIVERSE or "nifty100",
        support_bounce_universe=config.SUPPORT_BOUNCE_UNIVERSE or "nifty100",
        swing_trade_universe=config.SWING_TRADE_UNIVERSE or "nifty200",
        darvax_universe=config.DARVAX_UNIVERSE or "nifty200",
    )


@app.route("/api/scan", methods=["POST", "GET"])
def api_scan():
    refresh = _want_refresh()
    if refresh or not _last_scan_cache["generated_at"]:
        payload = _refresh_scan_cache()
    else:
        payload = dict(_last_scan_cache["payload"] or {})
    out = dict(payload)
    _rehydrate_payload_charts(out)
    out["market_open"] = _is_market_open()
    out["cached"] = not refresh
    return jsonify(out)


@app.route("/api/scan_ema10", methods=["POST", "GET"])
def api_scan_ema10():
    refresh = _want_refresh()
    if refresh or not _ema10_cache["generated_at"]:
        payload = _refresh_ema10_cache()
    else:
        payload = dict(_ema10_cache["payload"] or {})
    out = dict(payload)
    _rehydrate_payload_charts(out)
    out["market_open"] = _is_market_open()
    out["cached"] = not refresh
    return jsonify(out)


@app.route("/api/scan_nday", methods=["POST", "GET"])
def api_scan_nday():
    from flask import request
    lookback = request.args.get("lookback", default=10, type=int)
    if lookback not in config.NDAY_LOOKBACKS:
        return jsonify({"error": f"lookback must be one of {config.NDAY_LOOKBACKS}"}), 400

    refresh = _want_refresh()
    cached = _nday_cache.get(lookback) or {}
    if refresh or not cached.get("generated_at"):
        payload = _refresh_nday_cache(lookback)
    else:
        payload = dict(cached.get("payload") or {})
    out = dict(payload)
    _rehydrate_payload_charts(out)
    out["market_open"] = _is_market_open()
    out["cached"] = not refresh
    return jsonify(out)


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


def _stock_for_day_universe_arg() -> str:
    """Resolve ?universe= from request, falling back to config default."""
    from flask import request
    import universe as universe_mod
    raw = (request.args.get("universe") or config.STOCK_FOR_DAY_UNIVERSE or "nifty100")
    try:
        return universe_mod.normalize_nifty_mode(raw)
    except ValueError:
        return "nifty100"


def _support_bounce_universe_arg() -> str:
    from flask import request
    import universe as universe_mod
    raw = (request.args.get("universe") or config.SUPPORT_BOUNCE_UNIVERSE or "nifty100")
    try:
        return universe_mod.normalize_nifty_mode(raw)
    except ValueError:
        return "nifty100"


def _swing_trade_universe_arg() -> str:
    from flask import request
    import universe as universe_mod
    raw = (request.args.get("universe") or config.SWING_TRADE_UNIVERSE or "nifty200")
    try:
        return universe_mod.normalize_nifty_mode(raw)
    except ValueError:
        return "nifty200"


@app.route("/api/stock_for_day", methods=["POST", "GET"])
def api_stock_for_day():
    """Smart Money structure scan on selected Nifty universe — BUY-eligible only."""
    try:
        mode = _stock_for_day_universe_arg()
        refresh = _want_refresh()
        cached = _stock_for_day_cache.get(mode) or {}
        if refresh or not cached.get("generated_at"):
            print(f"Running Stock for day scan ({mode})...")
            payload = smart_money_pipeline.scan_stock_for_day(
                _client,
                _universe_df,
                send_telegram=config.STOCK_FOR_DAY_SEND_TELEGRAM,
                universe_mode=mode,
            )
            cached = {"generated_at": payload["generated_at"], "payload": payload}
            _stock_for_day_cache[mode] = cached
        out = dict(cached.get("payload") or {})
        support_bounce.rehydrate_charts(out.get("charts"))
        for row in out.get("buys") or []:
            sym = row.get("symbol")
            if sym:
                row["chart_url"] = support_bounce.local_chart_url(sym)
        out["market_open"] = _is_market_open()
        out["cached"] = not refresh
        return jsonify(out)
    except Exception as e:
        print(f"[stock_for_day] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "buys": [], "num_buys": 0}), 500


@app.route("/api/support_bounce", methods=["POST", "GET"])
def api_support_bounce():
    """Previous support touch/bounce filtered by Smart Money buy zone."""
    try:
        mode = _support_bounce_universe_arg()
        refresh = _want_refresh()
        cached = _support_bounce_cache.get(mode) or {}
        if refresh or not cached.get("generated_at"):
            print(f"Running Support Bounce scan ({mode})...")
            payload = support_bounce.scan_support_bounce(
                _client, _universe_df, universe_mode=mode
            )
            cached = {"generated_at": payload["generated_at"], "payload": payload}
            _support_bounce_cache[mode] = cached
        out = dict(cached.get("payload") or {})
        # Keep chart geometry available even after a reloader restart / cold cache.
        support_bounce.rehydrate_charts(out.get("charts"))
        for row in out.get("results") or []:
            sym = row.get("symbol")
            if sym:
                row["chart_url"] = support_bounce.local_chart_url(sym)
        out["market_open"] = _is_market_open()
        out["cached"] = not refresh
        return jsonify(out)
    except Exception as e:
        print(f"[support_bounce] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "results": [], "num_results": 0}), 500


@app.route("/support-chart/<path:symbol>")
def support_chart_page(symbol):
    """Local candlestick chart with detected support / wedge lines drawn."""
    return render_template("support_chart.html", symbol=str(symbol).upper())


@app.route("/api/support_bounce/chart/<path:symbol>")
def api_support_bounce_chart(symbol):
    """OHLC + plot lines + Smart Money BUY/SELL marker for any scanner chart."""
    symbol = str(symbol).upper()
    try:
        payload = _find_cached_chart(symbol)
        need_refresh = (
            payload is None
            or not payload.get("candles")
            or not payload.get("smart_money_signal")
            or "markers" not in payload
        )
        if need_refresh:
            row = _universe_df[_universe_df["tradingsymbol"] == symbol]
            if row.empty:
                return jsonify({"error": f"Unknown symbol {symbol}"}), 404
            token = int(row.iloc[0]["instrument_token"])
            today = datetime.now().date()
            from_date = today - dt.timedelta(days=config.SUPPORT_BOUNCE_LOOKBACK_DAYS)
            daily = _client.get_daily_history(token, symbol, from_date, today)
            if daily is None or daily.empty or len(daily) < 30:
                return jsonify({"error": f"No history for {symbol}"}), 404
            # Rebuild pattern + always attach Pine BUY/SELL label/markers.
            support_bounce.annotate_result_with_pattern(symbol, daily, {"symbol": symbol})
            payload = support_bounce.get_chart_payload(symbol)
            if payload is None:
                # Still return OHLC + SM label even without a pattern event.
                payload = support_bounce.build_chart_payload(symbol, daily, {
                    "smart_money_signal": None,
                })
                support_bounce.store_chart_payload(symbol, payload)
        return jsonify(payload)
    except Exception as e:
        print(f"[support_bounce/chart] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/swing_trade", methods=["POST", "GET"])
def api_swing_trade():
    """Weekly resistance-breakout scan (descending trendline / horizontal box)
    confirmed by a volume spike."""
    try:
        mode = _swing_trade_universe_arg()
        refresh = _want_refresh()
        cached = _swing_trade_cache.get(mode) or {}
        if refresh or not cached.get("generated_at"):
            print(f"Running Swing Trade scan ({mode})...")
            payload = swing_trade.scan_swing_trade(
                _client, _universe_df, universe_mode=mode
            )
            cached = {"generated_at": payload["generated_at"], "payload": payload}
            _swing_trade_cache[mode] = cached
        out = dict(cached.get("payload") or {})
        swing_trade.rehydrate_charts(out.get("charts"))
        for row in out.get("results") or []:
            sym = row.get("symbol")
            if sym:
                row["chart_url"] = swing_trade.local_chart_url(sym)
        out["market_open"] = _is_market_open()
        out["cached"] = not refresh
        return jsonify(out)
    except Exception as e:
        print(f"[swing_trade] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "results": [], "num_results": 0}), 500


@app.route("/swing-chart/<path:symbol>")
def swing_chart_page(symbol):
    """Weekly candlestick chart with resistance line/box + breakout markers."""
    return render_template("swing_chart.html", symbol=str(symbol).upper())


@app.route("/api/swing_trade/chart/<path:symbol>")
def api_swing_trade_chart(symbol):
    """Weekly OHLC + volume + resistance geometry + BREAKOUT/RE-TEST markers."""
    symbol = str(symbol).upper()
    try:
        payload = swing_trade.get_chart_payload(symbol)
        need_refresh = payload is None or not payload.get("candles")
        if need_refresh:
            row = _universe_df[_universe_df["tradingsymbol"] == symbol]
            if row.empty:
                return jsonify({"error": f"Unknown symbol {symbol}"}), 404
            token = int(row.iloc[0]["instrument_token"])
            today = datetime.now().date()
            from_date = today - dt.timedelta(days=config.SWING_TRADE_LOOKBACK_DAYS)
            daily = _client.get_daily_history(token, symbol, from_date, today)
            if daily is None or daily.empty or len(daily) < 150:
                return jsonify({"error": f"No history for {symbol}"}), 404
            payload = swing_trade.rebuild_chart_for_symbol(symbol, daily)
            if payload is None:
                return jsonify({"error": f"Not enough weekly history for {symbol}"}), 404
        return jsonify(payload)
    except Exception as e:
        print(f"[swing_trade/chart] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _darvax_timeframe_arg() -> str:
    from flask import request
    tf = (request.args.get("timeframe") or request.args.get("tf") or "daily").strip().lower()
    return "weekly" if tf == "weekly" else "daily"


@app.route("/api/darvax", methods=["POST", "GET"])
def api_darvax():
    """Darvas Box breakout scan (3-session-stall box construction,
    volume-confirmed breakout, near-ATH bias) - daily or weekly candles."""
    try:
        mode = _swing_trade_universe_arg()  # same nifty50/100/200/500 selector pattern
        timeframe = _darvax_timeframe_arg()
        refresh = _want_refresh()
        cache_key = f"{mode}_{timeframe}"
        cached = _darvax_cache.get(cache_key) or {}
        if refresh or not cached.get("generated_at"):
            print(f"Running DarvaX scan ({mode}, {timeframe})...")
            payload = darvax.scan_darvax(_client, _universe_df, universe_mode=mode, timeframe=timeframe)
            cached = {"generated_at": payload["generated_at"], "payload": payload}
            _darvax_cache[cache_key] = cached
        out = dict(cached.get("payload") or {})
        darvax.rehydrate_charts(out.get("charts"))
        for row in out.get("results") or []:
            sym = row.get("symbol")
            if sym:
                row["chart_url"] = darvax.local_chart_url(sym, timeframe)
        out["market_open"] = _is_market_open()
        out["cached"] = not refresh
        return jsonify(out)
    except Exception as e:
        print(f"[darvax] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "results": [], "num_results": 0}), 500


@app.route("/darvax-chart/<path:symbol>")
def darvax_chart_page(symbol):
    """Candlestick chart with Darvas box, entry/exit/averaging basis."""
    return render_template("darvax_chart.html", symbol=str(symbol).upper())


@app.route("/api/darvax/chart/<path:symbol>")
def api_darvax_chart(symbol):
    symbol = str(symbol).upper()
    timeframe = _darvax_timeframe_arg()
    try:
        payload = darvax.get_chart_payload(symbol, timeframe=timeframe)
        need_refresh = payload is None or not payload.get("candles")
        if need_refresh:
            row = _universe_df[_universe_df["tradingsymbol"] == symbol]
            if row.empty:
                return jsonify({"error": f"Unknown symbol {symbol}"}), 404
            token = int(row.iloc[0]["instrument_token"])
            today = datetime.now().date()
            lookback = config.DARVAX_WEEKLY_LOOKBACK_DAYS if timeframe == "weekly" else config.DARVAX_LOOKBACK_DAYS
            from_date = today - dt.timedelta(days=lookback)
            daily = _client.get_daily_history(token, symbol, from_date, today)
            min_bars = 300 if timeframe == "weekly" else 60
            if daily is None or daily.empty or len(daily) < min_bars:
                return jsonify({"error": f"No history for {symbol}"}), 404
            payload = darvax.rebuild_chart_for_symbol(symbol, daily, timeframe=timeframe)
            if payload is None:
                return jsonify({"error": f"No Darvas box found for {symbol}"}), 404
        return jsonify(payload)
    except Exception as e:
        print(f"[darvax/chart] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/stock_for_day/backtest", methods=["POST", "GET"])
def api_stock_for_day_backtest():
    """3-month Smart Money success-rate backtest on selected Nifty universe."""
    mode = _stock_for_day_universe_arg()
    try:
        result = smart_money_backtest.run_stock_for_day_backtest(
            _client, universe_mode=mode
        )
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


@app.route("/api/tracked", methods=["GET", "POST"])
def api_tracked():
    """GET: list tracked trades (with a live LTP/P&L refresh).
    POST: track a new trade — {symbol, source, entry_price, stop_loss,
    target, chart_url} — the shared 'Track' button on every screener table."""
    from flask import request
    if request.method == "POST":
        try:
            body = request.get_json(force=True) or {}
            symbol = body.get("symbol")
            entry_price = body.get("entry_price")
            if not symbol or entry_price is None:
                return jsonify({"error": "symbol and entry_price are required"}), 400
            trade_id = trade_tracker.track_trade(
                symbol=symbol,
                source=body.get("source") or "unknown",
                entry_price=float(entry_price),
                stop_loss=float(body["stop_loss"]) if body.get("stop_loss") is not None else None,
                target=float(body["target"]) if body.get("target") is not None else None,
                chart_url=body.get("chart_url"),
                notes=body.get("notes"),
            )
            return jsonify({"ok": True, "trade_id": trade_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    try:
        trade_tracker.refresh_prices(_client, _universe_df)
        status = request.args.get("status")
        trades = trade_tracker.list_tracked(status)
        return jsonify({"trades": trades, "num_trades": len(trades)})
    except Exception as e:
        print(f"[tracked] failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "trades": []}), 500


@app.route("/api/tracked/<int:trade_id>/close", methods=["POST"])
def api_tracked_close(trade_id):
    from flask import request
    try:
        body = request.get_json(silent=True) or {}
        exit_price = body.get("exit_price")
        trade = trade_tracker.close_trade(
            trade_id, exit_price=float(exit_price) if exit_price is not None else None
        )
        if trade is None:
            return jsonify({"error": f"No tracked trade with id {trade_id}"}), 404
        return jsonify({"ok": True, "trade": trade})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tracked/<int:trade_id>", methods=["DELETE"])
def api_tracked_delete(trade_id):
    ok = trade_tracker.untrack(trade_id)
    if not ok:
        return jsonify({"error": f"No tracked trade with id {trade_id}"}), 404
    return jsonify({"ok": True})


def _largest_cached(cache: dict) -> dict:
    """Pick the cached scan entry with the most results, across whichever
    universe/timeframe combos have been run - avoids double-counting a
    symbol that appears in both e.g. nifty100 and nifty200 caches."""
    best = {}
    best_n = -1
    for entry in cache.values():
        results = (entry.get("payload") or {}).get("results") or []
        if len(results) > best_n:
            best_n = len(results)
            best = entry.get("payload") or {}
    return best


@app.route("/api/performance/strategy")
def api_performance_strategy():
    """Cross-screener performance: aggregates every tracked trade by which
    screener it was tracked from ('which strategy is actually working')."""
    try:
        return jsonify({"strategies": strategy_performance.strategy_performance()})
    except Exception as e:
        return jsonify({"error": str(e), "strategies": []}), 500


@app.route("/api/performance/industry")
def api_performance_industry():
    """Industry-level breakout performance for DarvaX or Swing Trade's
    most recently cached scan - 'which industries are producing stronger
    breakout candidates', using each result's own breakout price vs
    today's price (no extra data fetches)."""
    from flask import request
    source = (request.args.get("source") or "darvax").strip().lower()
    try:
        if source == "swing_trade":
            payload = _largest_cached(_swing_trade_cache)
            entry_field, current_field = "resistance_price", "current_price"
        else:
            source = "darvax"
            payload = _largest_cached(_darvax_cache)
            entry_field, current_field = "breakout_close", "current_close"

        results = payload.get("results") or []
        industries = strategy_performance.industry_breakout_performance(
            results, entry_field, current_field
        )
        return jsonify({
            "source": source,
            "universe_label": payload.get("universe_label"),
            "generated_at": payload.get("generated_at"),
            "num_breakouts": len(results),
            "industries": industries,
        })
    except Exception as e:
        return jsonify({"error": str(e), "industries": []}), 500


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
