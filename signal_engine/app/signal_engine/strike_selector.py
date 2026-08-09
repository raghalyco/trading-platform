"""
Strike selection + fixed-points level calculator - a manual-trading-style
alternative to the ATR-based SCALP/SMART TRADE levels. Fixed points don't
adapt to volatility (same SL/target regardless of a quiet vs wild day),
which is a real tradeoff against the ATR-based approach, not a strict
upgrade - worth choosing deliberately.
"""

STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "SENSEX": 100,
    "BANKEX": 100,
}


def pick_strike(spot: float, symbol: str, otm_steps: int = 0, side: str = "CE") -> int:
    """
    Rounds spot to the nearest tradeable strike, then moves `otm_steps`
    strikes further out-of-the-money in the given direction.
    e.g. spot=24226, symbol=NIFTY, otm_steps=1, side=CE
         -> nearest strike 24250, one step further OTM (up) = 24300.
    otm_steps=0 means ATM (nearest strike, no offset).
    """
    step = STRIKE_STEP.get(symbol, 50)
    atm = round(spot / step) * step

    if otm_steps == 0:
        return atm

    direction = 1 if side == "CE" else -1  # CE: OTM is higher strikes, PE: OTM is lower
    return atm + direction * otm_steps * step


def fixed_points_levels(entry_price: float, side: str, sl_points: float,
                         t1_points: float, t2_points: float) -> dict:
    """
    entry_price should be the actual OPTION PREMIUM you're paying, not the
    index spot - sl_points/t1_points/t2_points are then genuinely points of
    premium, matching how you'd read them on your broker's order ticket.
    """
    sign = 1  # premium always moves the same direction as the option's own price,
              # regardless of CE/PE - you're long the option either way
    t1 = entry_price + sign * t1_points
    t2 = entry_price + sign * t2_points
    sl = entry_price - sign * sl_points

    rr = round(t1_points / sl_points, 1) if sl_points else 0.0

    return {
        "mode": "FIXED_POINTS",
        "side": side,
        "entry_premium": round(entry_price, 2),
        "target1_premium": round(t1, 2),
        "target2_premium": round(t2, 2),
        "stop_loss_premium": round(sl, 2),
        "sl_points": sl_points,
        "t1_points": t1_points,
        "t2_points": t2_points,
        "rr": rr,
    }
