"""TradingView chart link helpers.

Public tradingview.com URLs cannot set an exact zoom %, but we can open
with a fixed interval (and optional saved layout id) so charts land closer
to how our daily scanners look.
"""
import re
from typing import Optional
from urllib.parse import quote, urlparse

import config


def _layout_id(raw: str) -> str:
    """Accept either a bare layout id or a full TradingView chart URL."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" in raw or "/" in raw:
        path = urlparse(raw).path if "://" in raw else raw
        # .../chart/<id>/ or .../chart/<id>
        m = re.search(r"/chart/([^/?#]+)", path)
        if m:
            return m.group(1).strip("/")
        return path.strip("/").split("/")[-1]
    return raw.strip("/")


def tradingview_chart_url(symbol: str, interval: Optional[str] = None) -> str:
    """Build a TradingView chart URL for an NSE equity symbol."""
    symbol = (symbol or "").strip().upper()
    interval = interval or getattr(config, "TRADINGVIEW_INTERVAL", "D")
    base = getattr(config, "TRADINGVIEW_BASE_URL", "https://in.tradingview.com/chart/").rstrip("/") + "/"
    layout = _layout_id(getattr(config, "TRADINGVIEW_CHART_ID", "") or "")
    if layout:
        base = f"{base}{layout}/"
    sym = quote(f"NSE:{symbol}", safe=":")
    return f"{base}?symbol={sym}&interval={interval}"
