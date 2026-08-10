"""
Same opening-range breakout + volume surge + VWAP engine as
intraday_monitor.py's standalone CLI version, refactored into a
start/stop-able class so it can run as a background WebSocket thread
inside the Flask app and be controlled/inspected via API endpoints.

A single shared `engine` instance is used by app.py. intraday_monitor.py
(the standalone CLI script) is unaffected and still works independently
for anyone who prefers running it outside the web app.
"""
import threading
from datetime import datetime, date as date_cls, time as dtime

from kiteconnect import KiteTicker

import config
import shortlist as shortlist_mod
import telegram_alerts
from charts import tradingview_chart_url
from support_bounce import local_chart_url
from intraday_state import SymbolState


def _parse_time(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


MARKET_OPEN_T = _parse_time(config.INTRADAY_MARKET_OPEN)
MARKET_CLOSE_T = _parse_time(config.INTRADAY_MARKET_CLOSE)


class IntradayEngine:
    def __init__(self):
        self.kws = None
        self.states = {}
        self.running = False
        self.range_locked = False
        self.first_tick_logged = False
        self.alert_count = 0
        self.alerts = []
        self.market_open_dt = None
        self.range_lock_dt = None
        self.market_close_dt = None
        self.error = None
        self._lock = threading.Lock()

    def start(self, kite, client, universe_df) -> dict:
        if self.running:
            return {"already_running": True}

        self.error = None
        shortlist = shortlist_mod.build_shortlist(client, universe_df)
        if not shortlist:
            self.error = "Shortlist is empty — nothing to monitor today."
            return {"error": self.error}

        with self._lock:
            self.states = {item["instrument_token"]: SymbolState(item["symbol"], item["instrument_token"],
                                                                   sources=item.get("sources", []))
                            for item in shortlist}
            self.range_locked = False
            self.first_tick_logged = False
            self.alert_count = 0
            self.alerts = []

        today = date_cls.today()
        self.market_open_dt = datetime.combine(today, MARKET_OPEN_T)
        self.market_close_dt = datetime.combine(today, MARKET_CLOSE_T)
        lock_minute = self.market_open_dt.minute + config.INTRADAY_OPENING_RANGE_MINUTES
        if lock_minute < 60:
            self.range_lock_dt = self.market_open_dt.replace(minute=lock_minute)
        else:
            self.range_lock_dt = self.market_open_dt.replace(
                hour=self.market_open_dt.hour + 1, minute=lock_minute - 60)

        tokens = list(self.states.keys())
        access_token = kite.access_token
        self.kws = KiteTicker(config.KITE_API_KEY, access_token)

        def on_connect(ws, response):
            print(f"[intraday] WebSocket connected, subscribing to {len(tokens)} symbols")
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        def on_ticks(ws, ticks):
            now = datetime.now()
            if not self.first_tick_logged and ticks:
                print(f"[intraday debug] first raw tick — verify field names against "
                      f"intraday_state.py's process_tick(): {ticks[0]}")
                self.first_tick_logged = True

            with self._lock:
                for tick in ticks:
                    s = self.states.get(tick.get("instrument_token"))
                    if s:
                        s.process_tick(tick, now)

                if not self.range_locked and now >= self.range_lock_dt:
                    for s in self.states.values():
                        s.lock_opening_range(self.market_open_dt)
                    self.range_locked = True
                    print(f"[intraday] opening range locked for {len(self.states)} symbols")

                if self.range_locked:
                    for s in self.states.values():
                        trigger = s.check_trigger()
                        if trigger:
                            self.alert_count += 1
                            self.alerts.append(trigger)
                            telegram_alerts.send_telegram_message(
                                telegram_alerts.format_intraday_alert(trigger))

        def on_close(ws, code, reason):
            print(f"[intraday] WebSocket closed: {code} {reason}")

        def on_error(ws, code, reason):
            print(f"[intraday] WebSocket error: {code} {reason}")

        self.kws.on_connect = on_connect
        self.kws.on_ticks = on_ticks
        self.kws.on_close = on_close
        self.kws.on_error = on_error
        self.kws.connect(threaded=True)
        self.running = True
        return {"started": True, "shortlist_size": len(shortlist)}

    def stop(self) -> dict:
        if self.kws:
            try:
                self.kws.close()
            except Exception as e:
                print(f"[intraday] error closing websocket: {e}")
        self.running = False
        return {"stopped": True}

    def get_status(self) -> dict:
        with self._lock:
            symbols = []
            for s in self.states.values():
                ltp = s.current_candle["close"] if s.current_candle else None
                cur_vol = s.current_candle["volume"] if s.current_candle else None

                distance_pct = None
                if ltp is not None and s.opening_range_high:
                    distance_pct = round((ltp - s.opening_range_high) / s.opening_range_high * 100, 2)

                vol_vs_avg = None
                if cur_vol is not None and s.opening_range_avg_minute_volume:
                    vol_vs_avg = round(cur_vol / s.opening_range_avg_minute_volume, 2)

                symbols.append({
                    "symbol": s.symbol,
                    "ltp": round(ltp, 2) if ltp is not None else None,
                    "opening_range_high": round(s.opening_range_high, 2) if s.opening_range_high else None,
                    "opening_range_low": round(s.opening_range_low, 2) if s.opening_range_low else None,
                    "opening_range_locked": s.opening_range_locked,
                    "distance_from_or_high_pct": distance_pct,
                    "vwap": round(s.vwap, 2) if s.vwap else None,
                    "vol_vs_avg": vol_vs_avg,
                    "alerted": s.alerted_today,
                    "sources": s.sources,
                    "chart_url": local_chart_url(s.symbol),
                    "tv_chart_url": tradingview_chart_url(s.symbol, interval="5"),
                })

            return {
                "running": self.running,
                "range_locked": self.range_locked,
                "alert_count": self.alert_count,
                "alerts": self.alerts[-20:],
                "num_symbols": len(self.states),
                "error": self.error,
                "market_open": str(self.market_open_dt.time()) if self.market_open_dt else None,
                "range_lock_time": str(self.range_lock_dt.time()) if self.range_lock_dt else None,
                "symbols": symbols,
            }


# Single shared instance used by app.py
engine = IntradayEngine()
