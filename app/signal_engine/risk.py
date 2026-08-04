"""
Pre-trade risk gate. Tracks daily P&L and trade count; blocks new entries
once daily max-loss or trade-count limits are hit. This answers your open
design question on 'daily max-loss cutoff' directly.
"""

from dataclasses import dataclass, field
from app.config import CONFIG


@dataclass
class DailyState:
    realized_pnl: float = 0.0
    trades_taken: int = 0
    locked_out: bool = False
    lock_reason: str = ""


class RiskManager:
    def __init__(self):
        self.cfg = CONFIG.risk
        self.state = DailyState()

    def reset_day(self):
        self.state = DailyState()

    def record_trade_result(self, pnl: float):
        self.state.realized_pnl += pnl
        self.state.trades_taken += 1
        self._check_lockout()

    def _check_lockout(self):
        max_loss = -abs(self.cfg.capital * self.cfg.daily_max_loss_pct / 100)
        if self.state.realized_pnl <= max_loss:
            self.state.locked_out = True
            self.state.lock_reason = "Daily max-loss hit"
        elif self.state.trades_taken >= self.cfg.max_trades_per_day:
            self.state.locked_out = True
            self.state.lock_reason = "Max trades/day reached"

    def can_enter(self, proposed_rr: float) -> tuple[bool, str]:
        if self.state.locked_out:
            return False, self.state.lock_reason
        if proposed_rr < self.cfg.min_rr:
            return False, f"R:R {proposed_rr} below minimum {self.cfg.min_rr}"
        return True, "OK"

    def risk_amount_per_trade(self) -> float:
        return self.cfg.capital * self.cfg.risk_per_trade_pct / 100

    def position_size(self, sl_points: float, lot_size: int, points_per_lot_value: float = 1.0) -> int:
        """
        Rough lot-sizing: how many lots keep the trade within risk_per_trade.
        points_per_lot_value = value of 1 index point per lot (e.g. NIFTY lot size).
        """
        risk_amt = self.risk_amount_per_trade()
        if sl_points <= 0:
            return 0
        max_lots = risk_amt / (sl_points * points_per_lot_value)
        return max(1, int(max_lots))

    def daily_summary(self) -> dict:
        return {
            "realized_pnl": round(self.state.realized_pnl, 2),
            "trades_taken": self.state.trades_taken,
            "locked_out": self.state.locked_out,
            "lock_reason": self.state.lock_reason,
            "daily_limit": round(self.cfg.capital * self.cfg.daily_max_loss_pct / 100, 2),
        }
