"""Trade Sentry — LLM 审查器。

汇总规则结果 + 市场上下文 → LLM 综合审查。

支持多 LLM 提供商，在 .env 中配置 TRADE_SENTRY_LLM_PROVIDER:

  DeepSeek (默认):
    TRADE_SENTRY_LLM_PROVIDER=deepseek
    DEEPSEEK_API_KEY=sk-...

  Anthropic:
    TRADE_SENTRY_LLM_PROVIDER=anthropic
    ANTHROPIC_API_KEY=sk-ant-...
    TRADE_SENTRY_MODEL=claude-sonnet-4-6

  OpenAI 兼容 (OpenRouter / 中转 等):
    TRADE_SENTRY_LLM_PROVIDER=openai
    OPENAI_API_KEY=sk-...
    TRADE_SENTRY_API_BASE=https://你的地址
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from trade_sentry.schemas import (
    TradingPlan, MarketRegime, RuleResult, LLMReviewResult,
)

# ── 默认配置 ──────────────────────────────────────────

_DEFAULT_MODELS = {
    "deepseek": "deepseek-v4-pro",
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
}

_DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
}


def _load_dotenv() -> None:
    """从项目根目录 .env 文件加载环境变量（仅设置尚未存在的变量）。"""
    for candidate in [Path.cwd() / ".env",
                      Path(__file__).parent.parent / ".env"]:
        if not candidate.is_file():
            continue
        with open(candidate, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().lstrip("=").strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
        break  # 只加载第一个找到的 .env


# 模块加载时自动读取
_load_dotenv()

# 绕过系统代理（与 input.py 一致）
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")


def _build_prompt(plan: TradingPlan, regime: MarketRegime,
                  rule_results: list[RuleResult],
                  indicators, recent_trades: list[dict]) -> str:
    """构建 LLM 审查 prompt。"""

    # 规则结果摘要
    rules_text = "\n".join(
        f"- [{r.verdict.value.upper()}] {r.rule_id} {r.rule_name}: {r.detail}"
        for r in rule_results
    )

    # 技术指标摘要
    ind_lines = []
    if indicators.rsi_14 is not None:
        ind_lines.append(f"  RSI(14): {indicators.rsi_14:.1f}")
    if indicators.kdj_k is not None:
        ind_lines.append(f"  KDJ: K={indicators.kdj_k:.1f} D={indicators.kdj_d:.1f} "
                          f"J={indicators.kdj_j:.1f}")
    if indicators.macd_histogram is not None:
        ind_lines.append(f"  MACD柱: {indicators.macd_histogram:.4f}")
    if indicators.sma_20 and indicators.sma_50:
        ind_lines.append(f"  MA20: {indicators.sma_20:.2f}  MA50: {indicators.sma_50:.2f}")
    if indicators.atr_14:
        ind_lines.append(f"  ATR(14): {indicators.atr_14:.2f}")
    if indicators.volume_ratio is not None:
        ind_lines.append(f"  量比: {indicators.volume_ratio:.1%}")
    if indicators.bollinger_width_pct is not None:
        ind_lines.append(f"  布林带宽: {indicators.bollinger_width_pct:.1f}%")
    if indicators.nearest_support:
        ind_lines.append(f"  最近支撑: {indicators.nearest_support:.2f} "
                          f"(距当前 {indicators.distance_to_support_pct:.1f}%)")
    if indicators.nearest_resistance:
        ind_lines.append(f"  最近阻力: {indicators.nearest_resistance:.2f} "
                          f"(距当前 {indicators.distance_to_resistance_pct:.1f}%)")
    if indicators.weekly_rsi is not None:
        ind_lines.append(f"  周线 RSI(14): {indicators.weekly_rsi:.1f}")
    if indicators.weekly_macd_histogram is not None:
        ind_lines.append(f"  周线 MACD柱: {indicators.weekly_macd_histogram:.4f}")
    if indicators.weekly_kdj_k is not None:
        ind_lines.append(f"  周线 KDJ: K={indicators.weekly_kdj_k:.1f} "
                          f"D={indicators.weekly_kdj_d:.1f} J={indicators.weekly_kdj_j:.1f}")
    if indicators.weekly_ma20:
        ind_lines.append(f"  周线 MA20: {indicators.weekly_ma20:.2f}"
                          + (f"  MA50: {indicators.weekly_ma50:.2f}" if indicators.weekly_ma50 else ""))
    ind_lines.append(f"  周线均线: {indicators.weekly_ma_alignment}"
                      + (" (数据不足50根，仅MA20参考)" if "仅MA20" in str(indicators.weekly_ma_alignment) else ""))
    if indicators.weekly_atr:
        ind_lines.append(f"  周线 ATR(14): {indicators.weekly_atr:.2f}")
    if indicators.weekly_volume_ratio is not None:
        ind_lines.append(f"  周线量比: {indicators.weekly_volume_ratio:.1%}")
    ind_lines.append(f"  周线趋势: {indicators.weekly_trend}")
    ind_lines.append(f"  日线趋势: {indicators.daily_trend}")
    if indicators.candlestick_patterns:
        ind_lines.append(f"  日线K线形态: {', '.join(indicators.candlestick_patterns)}")
    if indicators.weekly_candlestick_patterns:
        ind_lines.append(f"  周线K线形态: {', '.join(indicators.weekly_candlestick_patterns)}")
    ind_text = "\n".join(ind_lines) if ind_lines else "(数据不足)"

    # 历史交易摘要
    if recent_trades:
        history_text = "\n".join(
            f"- {t.get('timestamp','')[:10]} {t.get('symbol','')} "
            f"{t.get('direction','')} @{t.get('position_pct','')}% "
            f"verdict={t.get('verdict','')}"
            for t in recent_trades[-5:]
        )
    else:
        history_text = "(无历史记录)"

    stop_info = f"{plan.stop_loss} (-{abs(plan.entry_price - plan.stop_loss) / plan.entry_price * 100:.1f}%)" if plan.stop_loss else "未设置"

    return f"""你是严格的交易纪律审查官。你的任务是检查规则引擎的结果是否合理，
而非主动寻找额外问题。如果计划没有明显问题，就说没有。

## 市场环境
{regime.regime_description}

## 当前技术指标
{ind_text}

## 规则检查结果
{rules_text}

## 历史交易（近 5 笔）
{history_text}

## 本次交易计划
- 标的: {plan.symbol}
- 方向: {plan.direction.value.upper()}
- 入场价: {plan.entry_price}
- 仓位: {plan.position_pct}%
- 止损: {stop_info}
- 理由: {plan.reasoning}

请审查并给出详细报告:

1. **技术指标解读**（面向初学者。**周线权重高于日线**——周线决定中期趋势方向，日线决定短期入场时机。周线看涨但日线超买 = 等回调再买，周线看跌但日线超卖 = 反弹是离场机会而非抄底。日线和周线矛盾时，以周线为准）:
   - 先看周线：RSI/MACD/KDJ/均线排列各自给出什么中期信号？周线趋势方向是什么？
   - 再看日线：日线的短期信号是支持还是对抗周线方向？如果对抗，日线只是短期噪音
   - 综合判断：周线定方向，日线定时机。周线看涨时，日线回调到支撑位是买点；周线看跌时，日线反弹到阻力位是卖点
   - 日线和周线的K线形态分别有哪些值得注意的反转或持续信号？两者是否一致还是矛盾？
   - 最近的支撑和阻力在哪？

2. **操作建议**（周线定方向，日线定时机。**优先参考周线判断大方向**）:
   - 趋势跟踪：周线趋势是否明确？日线是否提供了合理的入场点？
   - 区间交易：周线震荡还是趋势？震荡市适合区间交易，趋势市不适合
   - 逆势反弹：周线方向是什么？日线逆势信号只是在周线大方向下的短期修正
   - 什么情况下"什么都不做"是最好的选择？（周线方向与日线信号矛盾、ADX过低、量能不足时建议观望）

3. **情绪/偏误检测**: 从交易理由中推断是否存在 FOMO、恐慌、贪婪、
   报复性交易、确认偏误、过度自信、锚定效应、损失厌恶等。

4. **规则核验**: 规则引擎是否有误报？

5. **综合评分** (1-10)。给出整体合理性评分并简要说明理由。

   {'本次是 BUY，评估买入是否合理。' if plan.direction.value == 'buy' else '本次是 SELL，评估卖出已持仓是否合理。考虑: 下跌趋势支持卖出、阻力位是好卖点、超买/死叉/看跌K线支持卖出、支撑位附近是坏卖点。'}

6. **关键关切** + **最终建议**。

请以 JSON 格式回复。indicator_analysis 和 trading_advice 要写得详细，面向初学者：
{{"indicator_analysis": "...", "trading_advice": "...", "overall_reasonableness": <1-10>, "key_concerns": ["..."], "cognitive_bias_detected": ["..."], "alternative_plan": "..." or null, "final_advice": "评分理由和最终建议（自然语言，不写公式）"}}"""


def _parse_response(text: str) -> LLMReviewResult:
    """从 LLM 响应中提取 JSON 结果（含截断容错）。"""
    import re

    # 预处理：修复 JSON 字符串中未转义的控制字符
    # DeepSeek 等模型可能在 JSON 字符串值中输出真实的 \n \r \t
    def _sanitize_json(s: str) -> str:
        """将 JSON 字符串值中的控制字符转义。"""
        result = []
        in_string = False
        escape_next = False
        for ch in s:
            if escape_next:
                result.append(ch)
                escape_next = False
                continue
            if ch == '\\':
                result.append(ch)
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                continue
            if in_string:
                if ch == '\n':
                    result.append('\\n')
                elif ch == '\r':
                    result.append('\\r')
                elif ch == '\t':
                    result.append('\\t')
                else:
                    result.append(ch)
            else:
                result.append(ch)
        return ''.join(result)

    text = _sanitize_json(text)

    # 尝试直接解析
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试修复截断的 JSON（补上缺失的引号和括号）
        fixed = text.rstrip()
        # 补全截断的列表
        open_brackets = fixed.count("[") - fixed.count("]")
        open_braces = fixed.count("{") - fixed.count("}")
        # 如果最后一个非空白字符不是结束符，尝试截断到最后一个完整字段
        if fixed.rstrip().endswith(","):
            fixed = fixed.rstrip()[:-1]  # 去掉尾部逗号
        fixed += "]" * open_brackets
        fixed += "}" * open_braces
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError:
            # 逐字段正则提取（处理严重断裂的 JSON）
            data = {}
            for field, pattern in [
                ("overall_reasonableness", r'"overall_reasonableness":\s*(\d+)'),
                ("indicator_analysis", r'"indicator_analysis":\s*"((?:[^"\\]|\\.)*)"'),
                ("trading_advice", r'"trading_advice":\s*"((?:[^"\\]|\\.)*)"'),
                ("final_advice", r'"final_advice":\s*"((?:[^"\\]|\\.)*)"'),
            ]:
                m = re.search(pattern, text, re.DOTALL)
                if m:
                    val = m.group(1)
                    if field == "overall_reasonableness":
                        data[field] = int(val)
                    else:
                        data[field] = val.replace("\\n", "\n").replace('\\"', '"')
            if "overall_reasonableness" not in data:
                data["overall_reasonableness"] = 5
            # 列表字段尝试提取
            for list_field in ["key_concerns", "cognitive_bias_detected"]:
                items = re.findall(r'"' + list_field + r'":\s*\[(.*?)\]', text, re.DOTALL)
                if items:
                    data[list_field] = re.findall(r'"((?:[^"\\]|\\.)*)"', items[0])

    # 安全提取数值
    score = data.get("overall_reasonableness", 5)
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 5
    score = max(1, min(10, score))

    return LLMReviewResult(
        overall_reasonableness=score,
        indicator_analysis=data.get("indicator_analysis", ""),
        trading_advice=data.get("trading_advice", ""),
        key_concerns=data.get("key_concerns", []) or [],
        cognitive_bias_detected=data.get("cognitive_bias_detected", []) or [],
        alternative_plan=data.get("alternative_plan"),
        final_advice=data.get("final_advice", ""),
    )


def _call_openai_compatible(prompt: str, api_key: str, model: str,
                            base_url: str) -> Optional[str]:
    """OpenAI 兼容 API（DeepSeek / OpenRouter / 中转 等）。

    使用标准库 http.client 直连，避免 httpx/openai SDK 的系统代理干扰。
    """
    import json as _json
    import http.client
    import ssl
    from urllib.parse import urlparse

    if not base_url:
        base_url = "https://api.deepseek.com"
    parsed = urlparse(base_url)
    host = parsed.hostname or "api.deepseek.com"
    port = parsed.port or 443
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = path + "/chat/completions"

    body = _json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个专业的交易纪律审查官。只回复 JSON，不要有其他内容。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4096,
        "temperature": 0.3,
    })

    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=60)
    try:
        conn.request("POST", path, body=body.encode("utf-8"), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        data = _json.loads(raw)
        if "choices" not in data:
            raise RuntimeError(f"API error: {data.get('error', {}).get('message', raw[:200])}")
        return data["choices"][0]["message"]["content"]
    finally:
        conn.close()


def _call_anthropic(prompt: str, api_key: str, model: str,
                    base_url: str | None) -> Optional[str]:
    """Anthropic SDK。"""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    response = client.messages.create(
        model=model, max_tokens=4096,
        system="你是一个专业的交易纪律审查官。只回复 JSON，不要有其他内容。",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def llm_review(plan: TradingPlan, regime: MarketRegime,
               rule_results: list[RuleResult],
               indicators=None) -> Optional[LLMReviewResult]:
    """调用 LLM 进行综合审查。

    提供商检测顺序:
    1. TRADE_SENTRY_LLM_PROVIDER 环境变量 → deepseek / anthropic / openai
    2. 未设置时自动检测: 有 ANTHROPIC_API_KEY → anthropic
                       有 DEEPSEEK_API_KEY → deepseek
                       有 OPENAI_API_KEY → openai

    Returns:
        LLMReviewResult 或 None（API 不可用时）。
    """
    from trade_sentry.storage import load_recent_audits

    recent = load_recent_audits(n=5)
    prompt = _build_prompt(plan, regime, rule_results, indicators, recent)

    provider = os.environ.get("TRADE_SENTRY_LLM_PROVIDER", "")
    if not provider:
        # 自动检测
        if os.environ.get("DEEPSEEK_API_KEY"):
            provider = "deepseek"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            return None

    model = os.environ.get("TRADE_SENTRY_MODEL", _DEFAULT_MODELS.get(provider, ""))
    base_url = os.environ.get("TRADE_SENTRY_API_BASE", _DEFAULT_BASE_URLS.get(provider, ""))

    api_key_env = f"{provider.upper()}_API_KEY"
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return None

    try:
        if provider == "anthropic":
            text = _call_anthropic(prompt, api_key, model, base_url or None)
        else:
            text = _call_openai_compatible(prompt, api_key, model, base_url)
        return _parse_response(text) if text else None
    except Exception:
        return None
