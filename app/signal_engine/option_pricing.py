"""
Estimated option premium via Black-Scholes, using VIX as a rough IV proxy.

IMPORTANT honesty note: this is a MODEL ESTIMATE, not a live market price.
Real option premiums also reflect bid-ask spread, skew (OTM options don't
share the ATM/index VIX exactly), and liquidity effects this formula
doesn't capture. Every value this produces should be labeled "(est)" in
any UI, exactly like the reference app does - never presented as if it
were a live quote. For real trading decisions, use KiteFeed.get_option_ltp()
for the actual contract instead.
"""

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def estimate_premium(spot: float, strike: float, side: str, vix_pct: float,
                      expiry_date_iso: str, risk_free_rate: float = 0.065,
                      now: datetime | None = None, iv_multiplier: float = 1.0) -> float | None:
    """
    Black-Scholes estimate. vix_pct is used as a base for annualized IV.

    iv_multiplier: KNOWN LIMITATION, read before trusting this number.
    Raw index VIX is a ~30-day forward-looking measure. Options very close
    to their own expiry (same-day/next-day) carry much higher REALIZED
    implied volatility than the standing VIX suggests, due to gamma risk
    concentrating in the final hours. One calibration check against a real
    example (SENSEX ATM CE, same-day expiry) needed an effective IV of
    roughly 5x the index VIX to match the observed premium - but that's a
    SINGLE data point, not a validated model. Default here is 1.0 (no
    adjustment, will UNDER-estimate near-expiry premiums). If you're
    checking this against near-expiry options, try iv_multiplier=3-5 and
    compare against live/real premiums yourself to calibrate properly -
    don't trust either number without checking it against reality first.
    """
    now = now or datetime.now(IST)
    expiry_dt = datetime.strptime(expiry_date_iso, "%Y-%m-%d").replace(
        hour=15, minute=30, tzinfo=IST
    )
    seconds_to_expiry = (expiry_dt - now).total_seconds()
    if seconds_to_expiry <= 0:
        return None  # already expired

    t = seconds_to_expiry / (365 * 24 * 3600)
    iv = (vix_pct * iv_multiplier) / 100
    if iv <= 0 or t <= 0:
        return None

    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)

    if side == "CE":
        price = spot * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * t) * _norm_cdf(d2)
    else:
        price = strike * math.exp(-risk_free_rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

    return round(max(price, 0.05), 2)  # options don't price to exactly zero
