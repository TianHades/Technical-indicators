"""Trade Sentry — 计算引擎。

指标计算 + 市场状态分类 + 16 条确定性规则。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from trade_sentry.config import Config, get_config
from trade_sentry.schemas import (
    Direction, Verdict, RuleCategory, TrendRegime, VolatilityLevel,
    TradingPlan, MarketData, IndicatorSnapshot, MarketRegime, RuleResult,
)
from trade_sentry.storage import (
    count_today_audits, load_recent_audits, get_position_average,
    get_symbol_touches,
)


# ═══════════════════════════════════════════════════════════════
# A. 指标计算
# ═══════════════════════════════════════════════════════════════

def _to_df(data: list[dict]) -> pd.DataFrame:
    """将 dict 列表转为带 date 索引的 DataFrame（按日期升序）。"""
    df = pd.DataFrame(data)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        df = df.set_index("date")
    required = ["open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            df[col] = np.nan
    return df


def compute_indicators(market_data: MarketData) -> IndicatorSnapshot:
    """从 MarketData 计算全套技术指标。"""
    daily = _to_df(market_data.daily)
    weekly = _to_df(market_data.weekly)

    snap = IndicatorSnapshot()

    # ── 均线 ──
    if len(daily) >= 200:
        snap.sma_20 = float(daily["close"].rolling(20).mean().iloc[-1])
        snap.sma_50 = float(daily["close"].rolling(50).mean().iloc[-1])
        snap.sma_200 = float(daily["close"].rolling(200).mean().iloc[-1])
    elif len(daily) >= 50:
        snap.sma_20 = float(daily["close"].rolling(20).mean().iloc[-1])
        snap.sma_50 = float(daily["close"].rolling(50).mean().iloc[-1])

    if len(daily) >= 26:
        snap.ema_12 = float(daily["close"].ewm(span=12, adjust=False).mean().iloc[-1])
        snap.ema_26 = float(daily["close"].ewm(span=26, adjust=False).mean().iloc[-1])

    # 均线排列
    if snap.sma_20 and snap.sma_50 and snap.sma_200:
        if snap.sma_20 > snap.sma_50 > snap.sma_200:
            snap.ma_alignment = "多头"
        elif snap.sma_20 < snap.sma_50 < snap.sma_200:
            snap.ma_alignment = "空头"
        else:
            snap.ma_alignment = "交叉"

    # ── KDJ (Stochastic) ──
    if len(daily) >= 9:
        stoch_df = ta.stoch(daily["high"], daily["low"], daily["close"],
                            k=9, d=3, smooth_k=3)
        if stoch_df is not None and len(stoch_df) > 0:
            snap.kdj_k = float(stoch_df.iloc[-1, 0]) if not pd.isna(stoch_df.iloc[-1, 0]) else None
            snap.kdj_d = float(stoch_df.iloc[-1, 1]) if not pd.isna(stoch_df.iloc[-1, 1]) else None
            # J = 3K - 2D
            if snap.kdj_k is not None and snap.kdj_d is not None:
                snap.kdj_j = 3 * snap.kdj_k - 2 * snap.kdj_d

    # ── RSI ──
    if len(daily) >= 14:
        rsi_series = ta.rsi(daily["close"], length=14)
        snap.rsi_14 = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else None

    # ── MACD ──
    if len(daily) >= 26:
        macd_df = ta.macd(daily["close"], fast=12, slow=26, signal=9)
        if macd_df is not None and len(macd_df) > 0:
            snap.macd = float(macd_df.iloc[-1, 0]) if not pd.isna(macd_df.iloc[-1, 0]) else None
            snap.macd_signal = float(macd_df.iloc[-1, 2]) if not pd.isna(macd_df.iloc[-1, 2]) else None
            snap.macd_histogram = float(macd_df.iloc[-1, 1]) if not pd.isna(macd_df.iloc[-1, 1]) else None
            # Store full series for divergence detection
            hist_series = macd_df.iloc[:, 1].dropna()
            if len(hist_series) >= 20:
                snap._macd_hist_series = hist_series.values[-20:].tolist()
                snap._price_series = daily["close"].values[-20:].tolist()

    # ── 布林带 ──
    if len(daily) >= 20:
        bb_df = ta.bbands(daily["close"], length=20, std=2)
        if bb_df is not None and len(bb_df) > 0:
            snap.bollinger_lower = float(bb_df.iloc[-1, 0])   # BBL
            snap.bollinger_middle = float(bb_df.iloc[-1, 1])   # BBM
            snap.bollinger_upper = float(bb_df.iloc[-1, 2])    # BBU
            if snap.bollinger_middle and snap.bollinger_middle > 0:
                snap.bollinger_width_pct = float(
                    (snap.bollinger_upper - snap.bollinger_lower)
                    / snap.bollinger_middle * 100
                )

    # ── ATR ──
    if len(daily) >= 14:
        atr_series = ta.atr(daily["high"], daily["low"], daily["close"], length=14)
        snap.atr_14 = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else None

    # ── 成交量 ──
    if len(daily) >= 20:
        avg_vol = daily["volume"].rolling(20).mean().iloc[-1]
        cur_vol = daily["volume"].iloc[-1]
        if avg_vol > 0:
            snap.volume_ratio = float(cur_vol / avg_vol)

    # ── ADX + ATR 分位数 ──
    if len(daily) >= 14:
        adx_series = ta.adx(daily["high"], daily["low"], daily["close"], length=14)
        if adx_series is not None and len(adx_series) > 0:
            snap.adx_value = float(adx_series.iloc[-1, 0])

    if snap.atr_14 and len(daily) >= 250:
        atr_history = ta.atr(daily["high"], daily["low"], daily["close"], length=14)
        if atr_history is not None and len(atr_history) > 0:
            valid = atr_history.dropna()
            snap.atr_percentile = float((valid < snap.atr_14).mean() * 100)

    # ── 支撑阻力 ──
    snap.nearest_support, snap.nearest_resistance = _find_support_resistance(daily)
    latest_close = float(daily["close"].iloc[-1])
    if snap.nearest_resistance and snap.nearest_resistance > 0:
        snap.distance_to_resistance_pct = float(
            (snap.nearest_resistance - latest_close) / latest_close * 100
        )
    if snap.nearest_support and snap.nearest_support > 0:
        snap.distance_to_support_pct = float(
            (latest_close - snap.nearest_support) / latest_close * 100
        )

    # ── 蜡烛图形态（最近 3 根日K）──
    snap.candlestick_patterns = _detect_candle_patterns(daily)

    # ── 蜡烛图形态（最近 3 根周K）──
    snap.weekly_candlestick_patterns = _detect_candle_patterns(weekly)

    # ── 多周期趋势 ──
    snap.daily_trend = _classify_trend(daily)
    snap.weekly_trend = _classify_trend(weekly)

    # ── 周线指标 ──
    if len(weekly) >= 14:
        wrsi = ta.rsi(weekly["close"], length=14)
        snap.weekly_rsi = float(wrsi.iloc[-1]) if not pd.isna(wrsi.iloc[-1]) else None

    if len(weekly) >= 26:
        wmacd = ta.macd(weekly["close"], fast=12, slow=26, signal=9)
        if wmacd is not None and len(wmacd) > 0:
            snap.weekly_macd_histogram = float(wmacd.iloc[-1, 1]) if not pd.isna(wmacd.iloc[-1, 1]) else None

    if len(weekly) >= 9:
        wsk = ta.stoch(weekly["high"], weekly["low"], weekly["close"], k=9, d=3, smooth_k=3)
        if wsk is not None and len(wsk) > 0:
            wk = float(wsk.iloc[-1, 0]) if not pd.isna(wsk.iloc[-1, 0]) else None
            wd = float(wsk.iloc[-1, 1]) if not pd.isna(wsk.iloc[-1, 1]) else None
            snap.weekly_kdj_k = wk
            snap.weekly_kdj_d = wd
            if wk is not None and wd is not None:
                snap.weekly_kdj_j = 3 * wk - 2 * wd

    if len(weekly) >= 20:
        wma20 = float(weekly["close"].rolling(20).mean().iloc[-1])
        snap.weekly_ma20 = wma20
        latest_wclose = float(weekly["close"].iloc[-1])
        if len(weekly) >= 50:
            wma50 = float(weekly["close"].rolling(50).mean().iloc[-1])
            snap.weekly_ma50 = wma50
            if wma20 > wma50:
                snap.weekly_ma_alignment = "bullish"
            elif wma20 < wma50:
                snap.weekly_ma_alignment = "bearish"
        elif latest_wclose > wma20:
            snap.weekly_ma_alignment = "多头(仅MA20)"
        elif latest_wclose < wma20:
            snap.weekly_ma_alignment = "空头(仅MA20)"

        # 周线 ATR
        watr = ta.atr(weekly["high"], weekly["low"], weekly["close"], length=14)
        if watr is not None and len(watr) > 0:
            snap.weekly_atr = float(watr.iloc[-1]) if not pd.isna(watr.iloc[-1]) else None

        # 周线量比
        if len(weekly) >= 20:
            wavg_vol = weekly["volume"].rolling(20).mean().iloc[-1]
            wcur_vol = weekly["volume"].iloc[-1]
            if wavg_vol > 0:
                snap.weekly_volume_ratio = float(wcur_vol / wavg_vol)

    return snap


def _find_support_resistance(df: pd.DataFrame) -> tuple[float | None, float | None]:
    """基于 60 日高低点 + 均线确认计算支撑阻力。"""
    if len(df) < 60:
        return None, None

    recent = df.iloc[-60:]
    high_candidate = float(recent["high"].max())
    low_candidate = float(recent["low"].min())

    # 统计触碰次数
    high_touches = (recent["high"] >= high_candidate * 0.99).sum()
    low_touches = (recent["low"] <= low_candidate * 1.01).sum()

    resistance = high_candidate if high_touches >= 2 else None
    support = low_candidate if low_touches >= 2 else None

    # MA200 叠加强化
    if len(df) >= 200:
        ma200 = float(df["close"].rolling(200).mean().iloc[-1])
        if resistance and abs(resistance - ma200) / resistance < 0.02:
            pass  # MA200 附近阻力更可靠
        if support and abs(support - ma200) / support < 0.02:
            pass  # MA200 附近支撑更可靠

    return support, resistance


def _classify_trend(df: pd.DataFrame) -> str:
    """单周期趋势方向判断。"""
    if len(df) < 20:
        return "横盘"

    close = df["close"]
    ma20 = close.rolling(20).mean()

    # MA20 最近 5 日斜率
    if len(ma20.dropna()) >= 5:
        recent_ma = ma20.dropna().iloc[-5:]
        x = np.arange(5)
        slope = np.polyfit(x, recent_ma.values, 1)[0]
    else:
        slope = 0

    latest_close = float(close.iloc[-1])
    latest_ma20 = float(ma20.iloc[-1])

    if slope > 0 and latest_close > latest_ma20:
        return "上涨"
    elif slope < 0 and latest_close < latest_ma20:
        return "下跌"
    return "横盘"


_CDL_NAMES = {
    "doji": "十字星", "dojistar": "十字星",
    "hammer": "锤子线(看涨)", "invertedhammer": "倒锤子(看涨)",
    "hangingman": "上吊线(看跌)", "shootingstar": "流星线(看跌)",
    "engulfing": "吞没形态", "harami": "孕线",
    "morningstar": "启明星(看涨)", "morningdojistar": "十字启明星(看涨)",
    "eveningstar": "黄昏之星(看跌)", "eveningdojistar": "十字黄昏星(看跌)",
    "belt hold": "大阳线(看涨)", "belthold": "大阳线",
    "darkcloudcover": "乌云盖顶(看跌)", "piercing": "刺透形态(看涨)",
    "spinning top": "纺锤线", "spinningtop": "纺锤线",
    "marubozu": "光头光脚", "longline": "长实体线",
    "inside": "内包线(孕线)", "hikkake": "陷阱信号",
    "3whitesoldiers": "三白兵(看涨)", "3blackcrows": "三只乌鸦(看跌)",
    "3outside": "三线反包", "3inside": "三线内包",
    "abandonedbaby": "弃婴形态", "breakaway": "突破形态",
    "kick": "跳空反转", "xsidegap3methods": "跳空三法",
    "tristar": "三星线", "uniques3river": "独三河(看涨)",
    "closingmarubozu": "收盘光头光脚", "marubozu": "光头光脚",
    "highwave": "高浪线", "rickshawman": "长腿十字",
    "takuri": "探底线(看涨)", "ladderbottom": "梯底(看涨)",
    "stalledpattern": "停顿形态(看跌)", "advanceblock": "推进受阻(看跌)",
    "counterattack": "反击线", "separatinglines": "分离线",
    "matchinglow": "等低线(看涨)", "homingpigeon": "归鸽(看涨)",
}


def _detect_candle_patterns(df: pd.DataFrame) -> list[str]:
    """检测最近 3 根 K 线的蜡烛图形态。"""
    if len(df) < 3:
        return []
    patterns = ta.cdl_pattern(
        df["open"], df["high"], df["low"], df["close"], name="all"
    )
    if patterns is None or len(patterns) == 0:
        return []
    detected = []
    for i in range(max(0, len(patterns) - 3), len(patterns)):
        row = patterns.iloc[i]
        for col in patterns.columns:
            val = row[col]
            if val == 0 or pd.isna(val):
                continue
            raw = col.replace("CDL_", "").replace("_", " ").strip().lower()
            cn_name = _CDL_NAMES.get(raw, raw.title())
            if "看涨" in cn_name or "看跌" in cn_name:
                detected.append(cn_name)
            else:
                direction = "看涨" if val > 0 else "看跌"
                detected.append(f"{cn_name}({direction})")
    return list(set(detected))[:5]


# ═══════════════════════════════════════════════════════════════
# B. 市场状态分类
# ═══════════════════════════════════════════════════════════════

def classify_regime(indicators: IndicatorSnapshot,
                    cfg: Config | None = None) -> MarketRegime:
    """双维度市场状态分类。"""
    if cfg is None:
        cfg = get_config()

    adx_threshold = cfg.regime("adx_trend_threshold") or 25
    adx_choppy = cfg.regime("adx_choppy_threshold") or 20
    atr_perc = cfg.regime("atr_volatile_percentile") or 80

    adx = indicators.adx_value or 0

    # 维度一：趋势方向
    if adx > adx_threshold:
        if indicators.ma_alignment == "多头":
            trend_regime = TrendRegime.TRENDING_UP
        elif indicators.ma_alignment == "空头":
            trend_regime = TrendRegime.TRENDING_DOWN
        else:
            trend_regime = (TrendRegime.TRENDING_UP
                            if indicators.daily_trend == "上涨"
                            else TrendRegime.TRENDING_DOWN)
    elif adx < adx_choppy:
        trend_regime = TrendRegime.CHOPPY
    else:
        # ADX 20-25 灰色地带：参考周线趋势打破僵局
        if indicators.weekly_trend == "下跌":
            trend_regime = TrendRegime.TRENDING_DOWN
        elif indicators.weekly_trend == "上涨":
            trend_regime = TrendRegime.TRENDING_UP
        else:
            trend_regime = TrendRegime.CHOPPY

    # 维度二：波动率水平
    vol_percentile = indicators.atr_percentile or 50
    if vol_percentile > atr_perc:
        vol_level = VolatilityLevel.EXTREME
    elif vol_percentile > atr_perc * 0.75:
        vol_level = VolatilityLevel.ELEVATED
    else:
        vol_level = VolatilityLevel.NORMAL

        desc_map = {
        (TrendRegime.TRENDING_UP, VolatilityLevel.NORMAL): "上涨趋势 + 正常波动",
        (TrendRegime.TRENDING_UP, VolatilityLevel.ELEVATED): "上涨趋势 + 波动偏高",
        (TrendRegime.TRENDING_UP, VolatilityLevel.EXTREME): "上涨趋势 + 高波动",
        (TrendRegime.TRENDING_DOWN, VolatilityLevel.NORMAL): "下跌趋势 + 正常波动",
        (TrendRegime.TRENDING_DOWN, VolatilityLevel.ELEVATED): "下跌趋势 + 波动偏高",
        (TrendRegime.TRENDING_DOWN, VolatilityLevel.EXTREME): "下跌趋势 + 高波动",
        (TrendRegime.CHOPPY, VolatilityLevel.NORMAL): "震荡 + 正常波动",
        (TrendRegime.CHOPPY, VolatilityLevel.ELEVATED): "震荡 + 波动偏高",
        (TrendRegime.CHOPPY, VolatilityLevel.EXTREME): "震荡 + 高波动",
    }

    desc = desc_map.get((trend_regime, vol_level), "未知")
    if trend_regime == TrendRegime.CHOPPY:
        if indicators.daily_trend == "下跌" and indicators.weekly_trend == "下跌":
            desc = desc.replace("震荡", "震荡偏空")
        elif indicators.daily_trend == "上涨" and indicators.weekly_trend == "上涨":
            desc = desc.replace("震荡", "震荡偏多")

    return MarketRegime(
        trend_regime=trend_regime,
        adx_value=adx,
        ma_alignment=indicators.ma_alignment,
        volatility_level=vol_level,
        volatility_proxy="atr_percentile",
        volatility_value=vol_percentile,
        regime_confidence=0.7,
        regime_description=desc,
    )


# ═══════════════════════════════════════════════════════════════
# C. 规则函数
# ═══════════════════════════════════════════════════════════════

def _v(verdict: Verdict, rule_id: str, name: str, category: RuleCategory,
       detail: str, suggestion: str = "") -> RuleResult:
    """快捷构造 RuleResult。"""
    return RuleResult(rule_id=rule_id, rule_name=name, category=category,
                      verdict=verdict, detail=detail, suggestion=suggestion)


# ── 趋势 ──

def rule_T01(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """逆势操作检查。"""
    if plan.direction == Direction.BUY:
        if regime.trend_regime == TrendRegime.TRENDING_DOWN:
            near_support = (
                (ind.distance_to_support_pct is not None
                 and ind.distance_to_support_pct < 2)
            )
            if near_support:
                return _v(Verdict.WARN, "T01", "逆势操作", RuleCategory.TREND,
                          "下跌趋势中做多，但接近支撑位",
                          "关注支撑位是否有效，严格设置止损")
            return _v(Verdict.BLOCK, "T01", "逆势操作", RuleCategory.TREND,
                      "下跌趋势中做多，且不在支撑位附近",
                      "等待趋势转多或价格回落到支撑位再考虑")
    else:
        if regime.trend_regime == TrendRegime.TRENDING_UP:
            near_resistance = (
                (ind.distance_to_resistance_pct is not None
                 and ind.distance_to_resistance_pct < 2)
            )
            if near_resistance:
                return _v(Verdict.WARN, "T01", "逆势操作", RuleCategory.TREND,
                          "上涨趋势中做空，但接近阻力位",
                          "关注阻力位是否有效")
            return _v(Verdict.BLOCK, "T01", "逆势操作", RuleCategory.TREND,
                      "上涨趋势中做空，且不在阻力位附近",
                      "等待趋势转空或价格反弹到阻力位再考虑")
    return _v(Verdict.PASS, "T01", "逆势操作", RuleCategory.TREND, "趋势方向一致")


def rule_T04(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """ADX 弱趋势。"""
    adx = regime.adx_value
    if adx < 20:
        return _v(Verdict.WARN, "T04", "ADX 弱趋势", RuleCategory.TREND,
                  f"ADX={adx:.1f}，趋势信号噪音大",
                  "ADX<20 时趋势策略可信度低，考虑减少仓位或等待趋势明确")
    return _v(Verdict.PASS, "T04", "ADX 弱趋势", RuleCategory.TREND, f"ADX={adx:.1f}")


# ── 动量 ──

def rule_M01(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """RSI 超买追涨。"""
    rsi = ind.rsi_14
    if rsi is None:
        return _v(Verdict.PASS, "M01", "RSI 超买追涨", RuleCategory.MOMENTUM, "RSI 数据不足")
    if plan.direction == Direction.BUY and rsi > 80:
        return _v(Verdict.BLOCK, "M01", "RSI 超买追涨", RuleCategory.MOMENTUM,
                  f"RSI={rsi:.1f}，处于超买区买入",
                  "RSI>80 时追涨风险高，等待回调到 70 以下再考虑")
    return _v(Verdict.PASS, "M01", "RSI 超买追涨", RuleCategory.MOMENTUM, f"RSI={rsi:.1f}")


def rule_M02(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """RSI 超卖杀跌。"""
    rsi = ind.rsi_14
    if rsi is None:
        return _v(Verdict.PASS, "M02", "RSI 超卖杀跌", RuleCategory.MOMENTUM, "RSI 数据不足")
    if plan.direction == Direction.SELL and rsi < 20:
        return _v(Verdict.BLOCK, "M02", "RSI 超卖杀跌", RuleCategory.MOMENTUM,
                  f"RSI={rsi:.1f}，处于超卖区卖出",
                  "RSI<20 时恐慌杀跌风险高，等待反弹到 30 以上再考虑")
    return _v(Verdict.PASS, "M02", "RSI 超卖杀跌", RuleCategory.MOMENTUM, f"RSI={rsi:.1f}")


def rule_M03(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """MACD 顶背离 — 价格新高但 MACD 柱不新高。"""
    if plan.direction != Direction.BUY:
        return _v(Verdict.PASS, "M03", "MACD 顶背离", RuleCategory.MOMENTUM, "非买入操作")

    prices = ind._price_series
    macd_hists = ind._macd_hist_series
    if not prices or not macd_hists or len(prices) < 20:
        return _v(Verdict.PASS, "M03", "MACD 顶背离", RuleCategory.MOMENTUM, "MACD 序列数据不足")

    # 找最近两个局部高点（各看 10 根 bar）
    n = len(prices)
    half = n // 2
    first_half_prices = prices[:half]
    second_half_prices = prices[half:]
    first_half_macd = macd_hists[:half]
    second_half_macd = macd_hists[half:]

    try:
        price_peak1 = max(first_half_prices)
        price_peak2 = max(second_half_prices)
        macd_peak1 = max(first_half_macd)
        macd_peak2 = max(second_half_macd)
    except ValueError:
        return _v(Verdict.PASS, "M03", "MACD 顶背离", RuleCategory.MOMENTUM, "数据不足")

    if price_peak2 > price_peak1 and macd_peak2 < macd_peak1:
        return _v(Verdict.BLOCK, "M03", "MACD 顶背离", RuleCategory.MOMENTUM,
                  f"价格新高 {price_peak2:.2f} > {price_peak1:.2f}，"
                  f"但 MACD 柱 {macd_peak2:.4f} < {macd_peak1:.4f}",
                  "顶背离信号，买入可能追在顶部")

    return _v(Verdict.PASS, "M03", "MACD 顶背离", RuleCategory.MOMENTUM, "未检测到背离")


def rule_M04(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """MACD 底背离 — 价格新低但 MACD 柱不新低。"""
    if plan.direction != Direction.SELL:
        return _v(Verdict.PASS, "M04", "MACD 底背离", RuleCategory.MOMENTUM, "非卖出操作")

    prices = ind._price_series
    macd_hists = ind._macd_hist_series
    if not prices or not macd_hists or len(prices) < 20:
        return _v(Verdict.PASS, "M04", "MACD 底背离", RuleCategory.MOMENTUM, "MACD 序列数据不足")

    n = len(prices)
    half = n // 2
    try:
        price_trough1 = min(prices[:half])
        price_trough2 = min(prices[half:])
        macd_trough1 = min(macd_hists[:half])
        macd_trough2 = min(macd_hists[half:])
    except ValueError:
        return _v(Verdict.PASS, "M04", "MACD 底背离", RuleCategory.MOMENTUM, "数据不足")

    if price_trough2 < price_trough1 and macd_trough2 > macd_trough1:
        return _v(Verdict.BLOCK, "M04", "MACD 底背离", RuleCategory.MOMENTUM,
                  f"价格新低 {price_trough2:.2f} < {price_trough1:.2f}，"
                  f"但 MACD 柱 {macd_trough2:.4f} > {macd_trough1:.4f}",
                  "底背离信号，卖出可能割在地板上")

    return _v(Verdict.PASS, "M04", "MACD 底背离", RuleCategory.MOMENTUM, "未检测到背离")


# ── 关键位 ──

def rule_S01(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """阻力/支撑位检查。

    v1 简化: "突破回踩确认→PASS" 未实现，仅用 dist>2% 判断。
    get_symbol_touches() 用审计记录数近似 K 线触碰次数。
    """
    if plan.direction == Direction.BUY:
        dist = ind.distance_to_resistance_pct
        if dist is None or dist > 2:
            return _v(Verdict.PASS, "S01", "阻力位追高", RuleCategory.SUPPORT_RESISTANCE,
                      f"距阻力位 {dist:.1f}%" if dist else "无明确阻力位")
        # 检查历史触碰次数
        touches = get_symbol_touches(plan.symbol, "resistance")
        if touches >= 3:
            return _v(Verdict.BLOCK, "S01", "阻力位追高", RuleCategory.SUPPORT_RESISTANCE,
                      f"距阻力位 {dist:.1f}%，已多次测试未突破 ({touches} 次)",
                      "阻力位多次受阻，等待有效突破后再入场")
        return _v(Verdict.WARN, "S01", "阻力位追高", RuleCategory.SUPPORT_RESISTANCE,
                  f"距阻力位 {dist:.1f}%，首次/再次触碰",
                  "接近阻力位，关注是否能有效突破")
    else:
        dist = ind.distance_to_support_pct
        if dist is None or dist > 2:
            return _v(Verdict.PASS, "S01", "支撑位割肉", RuleCategory.SUPPORT_RESISTANCE,
                      f"距支撑位 {dist:.1f}%" if dist else "无明确支撑位")
        touches = get_symbol_touches(plan.symbol, "support")
        if touches >= 3:
            return _v(Verdict.BLOCK, "S01", "支撑位割肉", RuleCategory.SUPPORT_RESISTANCE,
                      f"距支撑位 {dist:.1f}%，多次测试 ({touches} 次)，可能形成支撑",
                      "支撑位附近卖出可能卖在地板价")
        return _v(Verdict.WARN, "S01", "支撑位割肉", RuleCategory.SUPPORT_RESISTANCE,
                  f"距支撑位 {dist:.1f}%",
                  "接近支撑位卖出需谨慎，确认不是恐慌性操作")


# ── 成交量 ──

def rule_V01(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime) -> RuleResult:
    """无量突破。"""
    vol_ratio = ind.volume_ratio
    dist_r = ind.distance_to_resistance_pct
    dist_s = ind.distance_to_support_pct

    near_key_level = ((dist_r is not None and dist_r < 2) or
                      (dist_s is not None and dist_s < 2))
    if near_key_level and vol_ratio is not None and vol_ratio < 0.7:
        return _v(Verdict.WARN, "V01", "无量突破", RuleCategory.VOLUME,
                  f"接近关键位但量比仅 {vol_ratio:.1%}",
                  "突破关键位需成交量配合，无量突破容易失败")
    return _v(Verdict.PASS, "V01", "无量突破", RuleCategory.VOLUME,
              f"量比 {vol_ratio:.1%}" if vol_ratio else "成交量数据不足")


# ── 止损 ──

def rule_STOP01(plan: TradingPlan, ind: IndicatorSnapshot,
                regime: MarketRegime) -> RuleResult:
    """无止损计划（仅对买入检查，卖出无需止损）。"""
    if plan.direction == Direction.SELL:
        return _v(Verdict.PASS, "STOP01", "止损计划", RuleCategory.STOP,
                  "卖出无需止损")
    if plan.stop_loss is None:
        return _v(Verdict.WARN, "STOP01", "无止损计划", RuleCategory.STOP,
                  "未设置止损价",
                  "建议基于 ATR(14) 设置止损：入场价 - 2×ATR")
    loss_pct = abs(plan.entry_price - plan.stop_loss) / plan.entry_price * 100
    return _v(Verdict.PASS, "STOP01", "止损计划", RuleCategory.STOP,
              f"止损 {plan.stop_loss} (-{loss_pct:.1f}%)")


# ── 频率 ──

def rule_F01(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime, cfg: Config | None = None) -> RuleResult:
    """过度交易。"""
    if cfg is None:
        cfg = get_config()
    max_trades = cfg.threshold("max_trades_per_day") or 5
    today_count = count_today_audits()
    if today_count >= max_trades:
        return _v(Verdict.BLOCK, "F01", "过度交易", RuleCategory.FREQUENCY,
                  f"今日已提交 {today_count} 笔计划（上限 {max_trades}）",
                  "暂停交易，明天再评估")
    return _v(Verdict.PASS, "F01", "过度交易", RuleCategory.FREQUENCY,
              f"今日第 {today_count + 1} 笔（上限 {max_trades}）")


def rule_F02(plan: TradingPlan, ind: IndicatorSnapshot,
             regime: MarketRegime, cfg: Config | None = None) -> RuleResult:
    """交易间隔过短（v1 简化版：仅看计划提交时间差）。"""
    if cfg is None:
        cfg = get_config()
    cooldown = cfg.threshold("cooldown_minutes") or 30
    recent = load_recent_audits(n=1, symbol=plan.symbol)
    if not recent:
        return _v(Verdict.PASS, "F02", "交易间隔", RuleCategory.FREQUENCY, "首次交易该标的")
    last_ts = recent[0].get("timestamp", "")
    try:
        from datetime import datetime
        last_dt = datetime.fromisoformat(last_ts)
        elapsed = (plan.planned_at - last_dt).total_seconds() / 60
        if elapsed < cooldown:
            return _v(Verdict.WARN, "F02", "交易间隔过短", RuleCategory.FREQUENCY,
                      f"距上次同标的操作仅 {elapsed:.0f} 分钟（冷却 {cooldown} 分钟）",
                      f"等待 {cooldown - elapsed:.0f} 分钟后再操作")
    except (ValueError, TypeError):
        pass
    return _v(Verdict.PASS, "F02", "交易间隔", RuleCategory.FREQUENCY, "间隔检查通过")


# ── 仓位 ──

def rule_SZ01(plan: TradingPlan, ind: IndicatorSnapshot,
              regime: MarketRegime, cfg: Config | None = None) -> RuleResult:
    """单笔仓位超限。"""
    if cfg is None:
        cfg = get_config()
    max_pct = cfg.threshold("max_position_pct") or 20
    if plan.position_pct > max_pct:
        return _v(Verdict.BLOCK, "SZ01", "单笔仓位超限", RuleCategory.SIZING,
                  f"计划仓位 {plan.position_pct}% > 上限 {max_pct}%",
                  f"将仓位降至 {max_pct}% 以内")
    return _v(Verdict.PASS, "SZ01", "单笔仓位", RuleCategory.SIZING,
              f"{plan.position_pct}%（上限 {max_pct}%）")


def rule_SZ02(plan: TradingPlan, ind: IndicatorSnapshot,
              regime: MarketRegime, cfg: Config | None = None) -> RuleResult:
    """总仓位超限。"""
    if plan.current_holdings_pct is None:
        return _v(Verdict.PASS, "SZ02", "总仓位", RuleCategory.SIZING, "未提供当前持仓，跳过")
    if cfg is None:
        cfg = get_config()
    max_total = cfg.threshold("max_total_position_pct") or 80
    total = plan.current_holdings_pct + plan.position_pct
    if total > max_total:
        return _v(Verdict.BLOCK, "SZ02", "总仓位超限", RuleCategory.SIZING,
                  f"当前持仓 {plan.current_holdings_pct}% + 计划 {plan.position_pct}%"
                  f" = {total}% > 上限 {max_total}%",
                  f"减少仓位使总仓位不超过 {max_total}%")
    return _v(Verdict.PASS, "SZ02", "总仓位", RuleCategory.SIZING,
              f"合计 {total}%（上限 {max_total}%）")


def rule_SZ03(plan: TradingPlan, ind: IndicatorSnapshot,
              regime: MarketRegime) -> RuleResult:
    """仓位异常放大。"""
    avg_pos = get_position_average(10)
    if avg_pos is None:
        return _v(Verdict.PASS, "SZ03", "仓位异常放大", RuleCategory.SIZING, "历史数据不足")
    if plan.position_pct > avg_pos * 1.5:
        return _v(Verdict.WARN, "SZ03", "仓位异常放大", RuleCategory.SIZING,
                  f"本次仓位 {plan.position_pct}% > 历史均值 {avg_pos:.0f}% × 1.5",
                  "确认是否因过度自信而放大仓位")
    return _v(Verdict.PASS, "SZ03", "仓位正常", RuleCategory.SIZING,
              f"{plan.position_pct}%（均值 {avg_pos:.0f}%）")


# 情绪检测已移至 LLM 审查（rule_E01/E02 已移除）

# ═══════════════════════════════════════════════════════════════
# D. 规则引擎编排
# ═══════════════════════════════════════════════════════════════

# 规则注册表：所有规则函数 (func, needs_config)
_RULES: list[tuple[str, callable, bool]] = [
    ("T01", rule_T01, False),
    ("T04", rule_T04, False),
    ("M01", rule_M01, False),
    ("M02", rule_M02, False),
    ("M03", rule_M03, False),
    ("M04", rule_M04, False),
    ("S01", rule_S01, False),
    ("V01", rule_V01, False),
    ("STOP01", rule_STOP01, False),
    ("F01", rule_F01, True),
    ("F02", rule_F02, True),
    ("SZ01", rule_SZ01, True),
    ("SZ02", rule_SZ02, True),
    ("SZ03", rule_SZ03, False),
    # E01/E02 已移除 — 情绪判断交给 LLM，不自评
]


class RuleEngine:
    """规则引擎编排器。"""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self.disabled = set(self.cfg.disabled_rules)

    def check_all(self, plan: TradingPlan, indicators: IndicatorSnapshot,
                  regime: MarketRegime) -> list[RuleResult]:
        """顺序执行所有启用的规则，收集结果。"""
        results = []
        for rule_id, rule_fn, needs_cfg in _RULES:
            if rule_id in self.disabled:
                continue
            try:
                if needs_cfg:
                    result = rule_fn(plan, indicators, regime, self.cfg)
                else:
                    result = rule_fn(plan, indicators, regime)
                # 检查是否被用户永久降级
                if (result.verdict == Verdict.BLOCK
                        and rule_id in set(self.cfg.block_to_warn_rules)):
                    result = RuleResult(
                        rule_id=result.rule_id, rule_name=result.rule_name,
                        category=result.category, verdict=Verdict.WARN,
                        detail=result.detail + " (用户已降级)",
                        suggestion=result.suggestion,
                    )
                results.append(result)
            except Exception as e:
                results.append(RuleResult(
                    rule_id=rule_id, rule_name="规则执行错误",
                    category=RuleCategory.TREND, verdict=Verdict.PASS,
                    detail=f"规则执行异常: {e}",
                ))
        return results

    def incremental_check(self, plan: TradingPlan, indicators: IndicatorSnapshot,
                          regime: MarketRegime,
                          changed_fields: set[str]) -> list[RuleResult]:
        """增量重审——仅重跑受修改字段影响的规则。

        Args:
            changed_fields: 被修改的字段名集合 (如 {"entry_price", "position_pct"})
        """
        field_to_rules = {
            "symbol": {"T01", "T04", "M01", "M02", "M03", "M04", "S01", "V01",
                       "STOP01", "F01", "F02", "SZ01", "SZ02", "SZ03"},
            "direction": {"T01", "S01"},
            "entry_price": {"S01", "M01", "M02", "M03", "M04"},
            "position_pct": {"SZ01", "SZ02", "SZ03"},
            "stop_loss": {"STOP01"},
            "current_holdings_pct": {"SZ02"},
            "emotion_self_rating": set(),
            "reasoning": set(),
        }

        affected = set()
        for field in changed_fields:
            affected |= field_to_rules.get(field, set())

        if not affected:
            return []

        results = []
        for rule_id, rule_fn, needs_cfg in _RULES:
            if rule_id not in affected or rule_id in self.disabled:
                continue
            try:
                results.append(
                    rule_fn(plan, indicators, regime, self.cfg) if needs_cfg
                    else rule_fn(plan, indicators, regime)
                )
            except Exception:
                pass
        return results
