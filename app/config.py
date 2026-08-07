"""
Central configuration for the signal engine.
Tune these without touching logic files.
"""

from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    capital: float = 100000.0          # total trading capital (₹)
    risk_per_trade_pct: float = 1.0    # % of capital risked per trade
    daily_max_loss_pct: float = 3.0    # daily circuit breaker
    min_rr: float = 1.5                # minimum acceptable reward:risk
    max_trades_per_day: int = 8


@dataclass
class ScalpConfig:
    timeframe: str = "1m"
    max_hold_minutes: int = 5
    # Multipliers apply to 5-min ATR (not 1-min) - a 1-min ATR is too small
    # and produces targets/SL an order of magnitude smaller than realistic
    # index-option scalp levels. These values are calibrated to roughly
    # match observed reference targets (T1~50-55, T2~85-98, SL~24-27 at
    # NIFTY ~24000, VIX~13) - tune further against your own live results.
    target1_atr_mult: float = 3.6
    target2_atr_mult: float = 6.2
    stop_atr_mult: float = 1.7


@dataclass
class SmartTradeConfig:
    timeframe: str = "5m"
    # Same 5-min ATR baseline as SCALP, sized wider since SMART TRADE holds
    # longer and trails rather than exiting at T1.
    target1_atr_mult: float = 5.0
    target2_atr_mult: float = 9.0
    stop_atr_mult: float = 2.5
    trail_after_t1: bool = True
    trail_atr_mult: float = 1.8        # trailing distance once T1 hit


@dataclass
class SignalConfig:
    # weight of each component in the 0-7 score
    components: tuple = ("EMA", "MACD", "15M", "5M", "1M", "VOL", "RSI")
    strong_buy_threshold: int = 6      # score >= this -> STRONG BUY
    moderate_buy_threshold: int = 4    # score >= this -> MODERATE/BUY
    caution_range_pct: float = 0.3     # candle range below this % => "choppy"
    pa_bonus_max: int = 2              # bonus points if price-action agrees


@dataclass
class OptionPricingConfig:
    # KNOWN LIMITATION - see option_pricing.py docstring. Default 1.0 = no
    # adjustment (will under-estimate near-expiry premiums badly, as shown
    # by testing against a real screenshot: ~35 estimated vs ~250 actual).
    # One calibration point suggested ~5.0 for same-day expiry, but that's
    # a single data point, not a validated model - verify against real
    # quotes yourself before trusting this for anything.
    iv_multiplier: float = 1.0
    risk_free_rate: float = 0.065


@dataclass
class AutoTradeConfig:
    # When True, TAKE signals during market hours auto-open an ATM/OTM
    # option capture (journal + optional Telegram). Does NOT place broker
    # orders unless you wire kite.place_order separately.
    enabled: bool = True
    min_confidence_pct: int = 75
    require_take: bool = True
    default_otm_steps: int = 0  # 0=ATM
    max_open_positions: int = 1
    send_telegram: bool = True
    # Skip auto-entry in these session labels
    skip_sessions: tuple = ("MARKET CLOSED", "DEAD ZONE")


@dataclass
class AppConfig:
    risk: RiskConfig = field(default_factory=RiskConfig)
    scalp: ScalpConfig = field(default_factory=ScalpConfig)
    smart: SmartTradeConfig = field(default_factory=SmartTradeConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    option_pricing: OptionPricingConfig = field(default_factory=OptionPricingConfig)
    auto_trade: AutoTradeConfig = field(default_factory=AutoTradeConfig)
    instruments: tuple = ("NIFTY", "SENSEX")
    refresh_seconds: int = 6


CONFIG = AppConfig()

# Env overrides (set on EC2 / .env) — load .env first if present
import os as _os

def _load_dotenv() -> None:
    _env = _os.path.join(_os.path.dirname(__file__), "..", ".env")
    if not _os.path.isfile(_env):
        return
    try:
        with open(_env, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
                if _k and _k not in _os.environ:
                    _os.environ[_k] = _v
    except OSError:
        pass

_load_dotenv()

_at = _os.getenv("AUTO_TRADE", "").strip().lower()
if _at in ("1", "true", "yes", "on"):
    CONFIG.auto_trade.enabled = True
elif _at in ("0", "false", "no", "off"):
    CONFIG.auto_trade.enabled = False
