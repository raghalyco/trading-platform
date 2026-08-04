"""
Order Block (OB): the last opposite-colored candle immediately before a
strong 'displacement' move. E.g. a bullish OB is the last red candle
right before a big green candle that breaks the recent swing high -
the idea (from smart-money-concept trading) is that this candle marks
where large orders were absorbed before the move.

This is a simplified, rule-based approximation - real OB identification
in discretionary SMC trading involves more context (liquidity sweeps,
market structure shifts) than a fixed formula captures.
"""

import pandas as pd


def _body(row: pd.Series) -> float:
    return abs(row["close"] - row["open"])


def detect_order_block(df: pd.DataFrame, lookback: int = 20,
                        displacement_mult: float = 1.5) -> dict:
    """
    Scans the last `lookback` candles for the most recent order block.
    displacement_mult: how much bigger the displacement candle's body
    must be vs the recent average body, to count as a genuine impulse
    move rather than normal noise.
    """
    if len(df) < lookback + 2:
        return {"found": False, "reason": "Not enough candles for OB scan"}

    window = df.iloc[-lookback:]
    avg_body = window.apply(_body, axis=1).mean()
    if avg_body <= 0:
        return {"found": False, "reason": "No price movement in window"}

    # scan from most recent backwards, look for [opposite candle][displacement candle] pair
    for i in range(len(window) - 1, 0, -1):
        candle = window.iloc[i]
        prior = window.iloc[i - 1]

        candle_is_bull = candle["close"] > candle["open"]
        prior_is_bull = prior["close"] > prior["open"]
        candle_body = _body(candle)

        is_displacement = candle_body >= avg_body * displacement_mult
        is_opposite = candle_is_bull != prior_is_bull

        if is_displacement and is_opposite:
            side = "CE" if candle_is_bull else "PE"
            ob_high = float(prior["high"])
            ob_low = float(prior["low"])
            sl_points = round(ob_high - ob_low, 1)

            if sl_points <= 10:
                quality = "Strong"
            elif sl_points <= 30:
                quality = "Moderate"
            else:
                quality = "Weak"

            return {
                "found": True,
                "side": side,
                "zone_high": round(ob_high, 2),
                "zone_low": round(ob_low, 2),
                "sl_points": sl_points,
                "quality": quality,
                "label": f"{quality} OB ({sl_points}pts SL)",
            }

    return {"found": False, "reason": "No qualifying displacement candle found in lookback window"}
