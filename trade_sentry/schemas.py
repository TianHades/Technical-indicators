"""Trade Sentry — 共享数据模型。

所有模块使用的 Pydantic 模型集中定义在此，保证类型安全和跨模块一致性。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── 枚举 ──────────────────────────────────────────────

class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Verdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


class TrendRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    CHOPPY = "choppy"


class VolatilityLevel(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    EXTREME = "extreme"


class RuleCategory(str, Enum):
    TREND = "trend"
    MOMENTUM = "momentum"
    SUPPORT_RESISTANCE = "support_resistance"
    VOLUME = "volume"
    STOP = "stop"
    FREQUENCY = "frequency"
    SIZING = "sizing"
    EMOTION = "emotion"


# ── 输入模型 ──────────────────────────────────────────

class TradingPlan(BaseModel):
    """用户提交的交易计划"""
    symbol: str = Field(..., description="标的代码")
    direction: Direction
    entry_price: float = Field(..., gt=0)
    position_pct: float = Field(..., gt=0, le=100)
    current_holdings_pct: Optional[float] = Field(None, ge=0, le=100)
    stop_loss: Optional[float] = Field(None, ge=0)
    take_profit: Optional[float] = Field(None, ge=0)
    reasoning: str = Field(..., min_length=1)
    emotion_self_rating: int = Field(..., ge=1, le=5)
    planned_at: datetime = Field(default_factory=datetime.now)


# ── 数据模型 ──────────────────────────────────────────

class MarketData(BaseModel):
    """拉取的行情数据"""
    symbol: str
    daily: list[dict]     # OHLCV 日线
    weekly: list[dict]    # OHLCV 周线
    fetched_at: datetime
    data_source: str


# ── 指标模型 ──────────────────────────────────────────

class IndicatorSnapshot(BaseModel):
    """技术指标快照 — 基于最新一根 K 线计算"""

    # 趋势类
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    ema_12: Optional[float] = None
    ema_26: Optional[float] = None
    ma_alignment: str = "交叉"

    # 动量类
    rsi_14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    kdj_k: Optional[float] = None
    kdj_d: Optional[float] = None
    kdj_j: Optional[float] = None

    # 波动率类
    bollinger_upper: Optional[float] = None
    bollinger_middle: Optional[float] = None
    bollinger_lower: Optional[float] = None
    atr_14: Optional[float] = None
    bollinger_width_pct: Optional[float] = None

    # 成交量类
    volume_ratio: Optional[float] = None

    # 支撑阻力
    nearest_resistance: Optional[float] = None
    nearest_support: Optional[float] = None
    distance_to_resistance_pct: Optional[float] = None
    distance_to_support_pct: Optional[float] = None

    # 蜡烛图形态
    candlestick_patterns: list[str] = Field(default_factory=list)
    weekly_candlestick_patterns: list[str] = Field(default_factory=list)

    # 周线指标
    weekly_rsi: Optional[float] = None
    weekly_macd_histogram: Optional[float] = None
    weekly_kdj_k: Optional[float] = None
    weekly_kdj_d: Optional[float] = None
    weekly_kdj_j: Optional[float] = None
    weekly_ma20: Optional[float] = None
    weekly_ma50: Optional[float] = None
    weekly_atr: Optional[float] = None
    weekly_volume_ratio: Optional[float] = None
    weekly_ma_alignment: str = "交叉"

    # 多周期趋势
    weekly_trend: str = "横盘"
    daily_trend: str = "横盘"

    # 市场状态（供 regime 分类使用）
    adx_value: Optional[float] = None
    atr_percentile: Optional[float] = None

    # 内部序列数据（供背离检测使用）
    _macd_hist_series: Optional[list[float]] = None
    _price_series: Optional[list[float]] = None

    model_config = {"extra": "ignore"}


# ── 市场状态模型 ──────────────────────────────────────

class MarketRegime(BaseModel):
    """双维度市场状态分类"""
    trend_regime: TrendRegime
    adx_value: float
    ma_alignment: str

    volatility_level: VolatilityLevel
    volatility_proxy: str
    volatility_value: float

    regime_confidence: float = Field(ge=0, le=1)
    regime_description: str


# ── 规则结果模型 ──────────────────────────────────────

class RuleResult(BaseModel):
    """单条规则的检查结果"""
    rule_id: str
    rule_name: str
    category: RuleCategory
    verdict: Verdict
    detail: str
    suggestion: str = ""


# ── LLM 审查模型 ──────────────────────────────────────

class LLMReviewResult(BaseModel):
    """LLM 综合审查结果"""
    overall_reasonableness: int = Field(ge=1, le=10)
    indicator_analysis: str = ""             # 技术指标详细解读（面向初学者）
    trading_advice: str = ""                 # 基于当前信号的操作建议
    key_concerns: list[str] = Field(default_factory=list)
    cognitive_bias_detected: list[str] = Field(default_factory=list)
    alternative_plan: Optional[str] = None
    final_advice: str = ""


# ── 输出模型 ──────────────────────────────────────────

class ReviewReport(BaseModel):
    """最终审查报告"""
    audit_id: str
    timestamp: datetime
    plan: TradingPlan
    regime: MarketRegime
    rule_results: list[RuleResult] = Field(default_factory=list)
    llm_review: Optional[LLMReviewResult] = None  # key_concerns 等通过此字段访问
    verdict: Verdict
    overall_score: float = Field(ge=1, le=10)
    suggestions: list[str] = Field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.rule_results if r.verdict == Verdict.PASS)

    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.rule_results if r.verdict == Verdict.WARN)

    @property
    def block_count(self) -> int:
        return sum(1 for r in self.rule_results if r.verdict == Verdict.BLOCK)


class AuditRecord(BaseModel):
    """审计日志记录"""
    audit_id: str
    timestamp: datetime
    symbol: str
    direction: Direction
    position_pct: float
    emotion: int
    verdict: Verdict
    overridden_rules: list[str] = Field(default_factory=list)
    user_action: str = "accepted"
    user_modification: Optional[str] = None
