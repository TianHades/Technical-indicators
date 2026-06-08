"""Trade Sentry — 飞书机器人。

接收飞书消息 → 异步执行完整审查 → 卡片消息返回。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime
from typing import Optional

from trade_sentry.config import get_config
from trade_sentry.input import fetch_market_data, get_stock_name, validate_stock_code
from trade_sentry.engine import compute_indicators, classify_regime, RuleEngine
from trade_sentry.schemas import TradingPlan, Direction, Verdict


# ── 消息解析 ──────────────────────────────────────────

def _parse_message(text: str) -> dict | None:
    """解析用户消息。

    格式: 股票代码 [buy|sell] [价格] [仓位%] [止损] [日期]
    示例: 600036                  → BUY 默认
          600036 sell             → SELL 默认
          600036 38.5 10% 37.0    → BUY 入场38.5 仓位10% 止损37
          600036 2025-01-24       → BUY 回测
    返回: {symbol, direction, price, position, stop_loss, as_of} 或 None
    """
    text = text.strip()

    # 提取日期
    as_of = None
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if date_match:
        as_of = date_match.group(1)
        text = text.replace(date_match.group(1), "").strip()

    # 提取方向
    direction = Direction.BUY
    for kw in ["sell", "卖出"]:
        if kw in text.lower():
            direction = Direction.SELL
            text = re.sub(r"\b" + kw + r"\b", "", text, flags=re.IGNORECASE).strip()
            break

    # 提取股票代码（第一部分）
    parts = text.split()
    if not parts:
        return None
    symbol = parts[0]
    try:
        validate_stock_code(symbol)
    except ValueError:
        return None

    # 解析可选参数：价格 仓位 止损
    price = None
    position = None
    stop_loss = None
    for p in parts[1:]:
        p = p.strip().rstrip("%")
        try:
            val = float(p)
            if price is None:
                price = val
            elif position is None:
                position = val if val <= 100 else position  # 仓位 ≤100%
                if val > 100 and price is not None:
                    # 可能是止损价
                    stop_loss = val
            elif stop_loss is None:
                stop_loss = val
        except ValueError:
            pass

    return {
        "symbol": symbol, "direction": direction,
        "price": price, "position": position,
        "stop_loss": stop_loss, "as_of": as_of,
    }


# ── 审查执行 ──────────────────────────────────────────

def _run_review(symbol: str, direction: Direction = Direction.BUY,
                price: Optional[float] = None, position: Optional[float] = None,
                stop_loss: Optional[float] = None, as_of: Optional[str] = None) -> dict:
    """执行完整审查，返回结构化结果字典。支持可选参数覆盖默认值。"""
    cfg = get_config()
    md = fetch_market_data(symbol, cfg, as_of=as_of)
    ind = compute_indicators(md)
    regime = classify_regime(ind, cfg)

    sorted_daily = sorted(md.daily, key=lambda b: str(b.get("date", "")))
    close = float(sorted_daily[-1].get("close", 0))

    is_buy = direction == Direction.BUY
    entry_price = price if price else close
    pos_pct = position if position else 10
    sl = stop_loss if stop_loss else (round(entry_price * 0.9, 2) if is_buy else None)

    plan = TradingPlan(
        symbol=symbol, direction=direction, entry_price=entry_price,
        position_pct=pos_pct, stop_loss=sl,
        reasoning="长期看好该股票业绩" if is_buy else "中短期认为其可能会下跌",
        emotion_self_rating=3,
    )

    engine = RuleEngine(cfg)
    results = engine.check_all(plan, ind, regime)

    pn = sum(1 for r in results if r.verdict == Verdict.PASS)
    wn = sum(1 for r in results if r.verdict == Verdict.WARN)
    bn = sum(1 for r in results if r.verdict == Verdict.BLOCK)
    rule_score = max(1, 10 - wn - bn * 3)

    if bn > 0:
        verdict = "BLOCK"
    elif wn > 0:
        verdict = "WARN"
    else:
        verdict = "PASS"

    blocks = [f"{r.rule_id} {r.rule_name}" for r in results if r.verdict == Verdict.BLOCK]
    warns = [f"{r.rule_id} {r.rule_name}" for r in results if r.verdict == Verdict.WARN]

    # LLM（完整字段，与 CLI 一致）
    llm_score = rule_score
    indicator_analysis = ""
    trading_advice = ""
    cognitive_bias = []
    key_concerns = []
    final_advice = ""
    try:
        from trade_sentry.reviewer import llm_review
        llm_result = llm_review(plan, regime, results, ind)
        if llm_result:
            llm_score = llm_result.overall_reasonableness
            indicator_analysis = llm_result.indicator_analysis or ""
            trading_advice = llm_result.trading_advice or ""
            cognitive_bias = llm_result.cognitive_bias_detected or []
            key_concerns = llm_result.key_concerns or []
            final_advice = llm_result.final_advice or ""
    except Exception:
        pass

    stock_name = get_stock_name(symbol)
    name_str = f" [{stock_name}]" if stock_name else ""

    return {
        "symbol": symbol, "name": stock_name, "name_str": name_str,
        "close": close, "direction": direction.value.upper(),
        "entry_price": entry_price, "position_pct": pos_pct, "stop_loss": sl,
        "regime": regime.regime_description,
        "rsi_d": ind.rsi_14, "rsi_w": ind.weekly_rsi,
        "macd_d": ind.macd_histogram, "macd_w": ind.weekly_macd_histogram,
        "kdj_k_d": ind.kdj_k, "kdj_d_d": ind.kdj_d, "kdj_j_d": ind.kdj_j,
        "kdj_k_w": ind.weekly_kdj_k, "kdj_d_w": ind.weekly_kdj_d, "kdj_j_w": ind.weekly_kdj_j,
        "sma_20": ind.sma_20, "sma_50": ind.sma_50, "sma_200": ind.sma_200,
        "wma_20": ind.weekly_ma20, "wma_50": ind.weekly_ma50,
        "bb_lower": ind.bollinger_lower, "bb_mid": ind.bollinger_middle,"bb_upper": ind.bollinger_upper,
        "bb_width": ind.bollinger_width_pct,
        "support": ind.nearest_support, "support_pct": ind.distance_to_support_pct,
        "resistance": ind.nearest_resistance, "resistance_pct": ind.distance_to_resistance_pct,
        "daily_trend": ind.daily_trend, "weekly_trend": ind.weekly_trend,
        "ma_align": ind.ma_alignment, "weekly_ma_align": ind.weekly_ma_alignment,
        "atr": ind.atr_14, "vol_ratio": ind.volume_ratio,
        "candle_d": ind.candlestick_patterns, "candle_w": ind.weekly_candlestick_patterns,
        "pn": pn, "wn": wn, "bn": bn, "verdict": verdict,
        "blocks": blocks, "warns": warns, "rule_score": rule_score,
        "llm_score": llm_score,
        "indicator_analysis": indicator_analysis,
        "trading_advice": trading_advice,
        "cognitive_bias": cognitive_bias,
        "key_concerns": key_concerns,
        "final_advice": final_advice,
        "as_of": as_of,
    }


# ── 飞书卡片格式化 ────────────────────────────────────

def _v(val, fmt=".2f"):
    if val is None:
        return "—"
    return f"{val:{fmt}}"

def _format_feishu_card(r: dict) -> dict:
    """构建飞书卡片消息 JSON——完整版。"""
    header_color = "red" if r["verdict"] == "BLOCK" else (
        "yellow" if r["verdict"] == "WARN" else "green")
    dir_label = "买入" if r["direction"] == "BUY" else "卖出(平仓)"
    as_of_str = f"  回测: {r['as_of']}" if r["as_of"] else ""
    stop_str = f"止损 {r['close']*0.9:.2f}" if r["direction"] == "BUY" else "不设止损"

    title = f"{r['symbol']}{r['name_str']} {dir_label} 入场{r['close']:.2f}"

    # 市场状态 + 规则摘要
    block_warn_lines = ""
    for b in r["blocks"]:
        block_warn_lines += f"[BLOCK] {b}\n"
    for w in r["warns"]:
        block_warn_lines += f"[WARN] {w}\n"

    entry_price = r.get("entry_price", r["close"])
    position_pct = r.get("position_pct", 10)
    sl_display = f"止损 {r['stop_loss']:.2f}" if r.get("stop_loss") else ("止损 {:.2f}".format(r['close']*0.9) if r['direction']=='BUY' else "不设止损")
    section1 = (
        f"**市场状态**: {r['regime']}\n"
        f"{dir_label} 入场{entry_price:.2f} 仓位{position_pct:.0f}% {sl_display}{as_of_str}\n"
        f"规则: [PASS]{r['pn']} [WARN]{r['wn']} [BLOCK]{r['bn']}"
        f"  **{r['verdict']} {r.get('llm_score', r['rule_score'])}/10**"
    )

    # 指标快照（与 CLI 完全一致）
    candles_d = ", ".join(r.get('candle_d', [])[:3]) if r.get('candle_d') else "—"
    candles_w = ", ".join(r.get('candle_w', [])[:3]) if r.get('candle_w') else "—"
    section2 = (
        f"**━━ 技术指标 ━━**\n"
        f"MA 日 {_v(r.get('sma_20'))}/{_v(r.get('sma_50'))}/{_v(r.get('sma_200'))}"
        f"  周 {_v(r.get('wma_20'))}/{_v(r.get('wma_50'))}\n"
        f"均线 日{r['ma_align']} / 周{r['weekly_ma_align']}  "
        f"趋势 日{r['daily_trend']} / 周{r['weekly_trend']}\n"
        f"RSI 日{_v(r['rsi_d'],'.1f')} / 周{_v(r['rsi_w'],'.1f')}  "
        f"MACD 日{_v(r['macd_d'],'.4f')} / 周{_v(r['macd_w'],'.4f')}\n"
        f"KDJ 日 K{_v(r['kdj_k_d'],'.1f')} D{_v(r['kdj_d_d'],'.1f')} J{_v(r['kdj_j_d'],'.1f')}"
        f"  周 K{_v(r['kdj_k_w'],'.1f')} D{_v(r['kdj_d_w'],'.1f')} J{_v(r['kdj_j_w'],'.1f')}\n"
        f"布林 下{_v(r.get('bb_lower'))} 中{_v(r.get('bb_mid'))} 上{_v(r.get('bb_upper'))}"
        f" 带宽{_v(r.get('bb_width'),'.1f')}%\n"
        f"ATR {_v(r['atr'])}  量比 {_v(r['vol_ratio'],'.1%')}\n"
        f"支撑 {_v(r['support'])} (距{_v(r['support_pct'],'.1f')}%)  "
        f"阻力 {_v(r['resistance'])} (距{_v(r['resistance_pct'],'.1f')}%)\n"
        f"K线 日[{candles_d}]  周[{candles_w}]"
    )

    # 拦截/警告详情
    if block_warn_lines:
        section3 = f"**━━ 拦截/警告详情 ━━**\n{block_warn_lines}"
    else:
        section3 = ""

    # LLM 完整分析（与 CLI 一致：指标解读 + 操作建议 + 偏误 + 关切 + 最终建议）
    llm_parts = []
    if r.get("indicator_analysis"):
        llm_parts.append(f"**━━ 技术指标解读 ━━**\n{r['indicator_analysis']}")
    if r.get("trading_advice"):
        llm_parts.append(f"**━━ 操作建议 ━━**\n{r['trading_advice']}")
    if r.get("cognitive_bias"):
        llm_parts.append(f"**━━ 偏误检测 ━━**\n" + "\n".join(f"· {b}" for b in r["cognitive_bias"]))
    if r.get("key_concerns"):
        llm_parts.append(f"**━━ 关键关切 ━━**\n" + "\n".join(f"· {c}" for c in r["key_concerns"]))
    if r.get("final_advice"):
        llm_parts.append(f"**━━ 最终建议 · 评分 {r.get('llm_score', r['rule_score'])}/10 ━━**\n{r['final_advice']}")
    section4 = "\n\n".join(llm_parts) if llm_parts else ""

    # 组装所有内容
    content_blocks = [section1, section2]
    if section3:
        content_blocks.append(section3)
    if section4:
        content_blocks.append(section4)
    full_content = "\n\n".join(content_blocks)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": header_color,
        },
        "elements": [
            {
                "tag": "markdown",
                "content": full_content,
            },
            {
                "tag": "hr",
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text",
                     "content": f"Trade Sentry · {datetime.now().strftime('%H:%M')}"},
                ],
            },
        ],
    }


# ── 飞书 SDK 客户端 ────────────────────────────────────

def _get_client() -> "lark_oapi.Client":
    """获取飞书 SDK 客户端（自动管理 token）。"""
    import lark_oapi as lark
    return lark.Client.builder() \
        .app_id(os.environ.get("FEISHU_APP_ID", "")) \
        .app_secret(os.environ.get("FEISHU_APP_SECRET", "")) \
        .build()


def _send_message(target_id: str, card: dict, id_type: str = "chat_id") -> None:
    """通过飞书 SDK 发送卡片消息。"""
    import lark_oapi.api.im.v1 as im_v1
    import logging

    client = _get_client()
    req = im_v1.CreateMessageRequest.builder() \
        .receive_id_type(id_type) \
        .request_body(
            im_v1.CreateMessageRequestBody.builder()
            .receive_id(target_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        ).build()

    resp = client.im.v1.message.create(req)
    if resp.success():
        logging.info(f"  -> Card sent OK: {resp.data.message_id}")
    else:
        logging.error(f"  -> Send FAILED: code={resp.code} msg={resp.msg}")


# ── 事件处理 ──────────────────────────────────────────

def _event_to_dict(event) -> dict:
    """将 SDK 事件对象转为字典。"""
    if event is None:
        return {}
    if isinstance(event, dict):
        return event
    try:
        return vars(event)
    except TypeError:
        return {}


def _on_message(event_obj) -> None:
    """处理飞书消息事件。SDK 对象用 vars() 取内部字段。"""
    import logging

    # SDK 对象属性在 vars(event_obj).get('_d', {}) 或直接 vars()
    ev_data = vars(event_obj)
    # P2ImMessageReceiveV1 的内部数据可能在各式各样的 key 下
    event = ev_data.get("event", {})
    if hasattr(event, "__dict__"):
        event = vars(event)
    if isinstance(event, dict):
        message = event.get("message", {})
        if hasattr(message, "__dict__"):
            message = vars(message)
        if isinstance(message, dict):
            chat_id = message.get("chat_id", "")
            sender = message.get("sender", {})
            if hasattr(sender, "__dict__"):
                sender = vars(sender)
            sender_id = sender.get("sender_id", {})
            if hasattr(sender_id, "__dict__"):
                sender_id = vars(sender_id)
            sender_open_id = sender_id.get("open_id", "") if isinstance(sender_id, dict) else ""
            msg_type = message.get("message_type", "?")
            content_str = message.get("content", "{}")
        else:
            logging.info(f"MSG: message is not dict: {type(message)}")
            return
    else:
        logging.info(f"MSG: event is not dict: {type(event)}")
        return

    # 解析消息内容
    content = {}
    try:
        content = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        pass

    # 提取文本（处理富文本中的 @提及）
    text = ""
    if "elements" in content:
        for el in content.get("elements", []):
            if el.get("tag") == "at":
                continue
            text += el.get("text", "")
    else:
        text = content.get("text", "")

    text = text.strip()
    logging.info(f"MSG chat={chat_id[:20] if chat_id else '?'} sender={sender_open_id[:20] if sender_open_id else '?'} type={msg_type} text='{text[:80]}'")

    # 优先用 sender_open_id（私信），其次 chat_id（群聊）
    target_id = sender_open_id or chat_id
    id_type = "open_id" if sender_open_id else "chat_id"
    if not text or not target_id:
        return

    # 去掉 @机器人 前缀（群聊中消息可能是 "@bot 600036" 格式）
    text = re.sub(r"@\S+\s*", "", text).strip()

    parsed = _parse_message(text)
    if parsed is None:
        logging.info(f"  -> parse failed for '{text[:30]}'")
        return

    def _do_review():
        try:
            import logging, traceback
            logging.info(f"Starting review for {parsed['symbol']} {parsed['direction'].value} as_of={parsed.get('as_of')}")
            result = _run_review(**parsed)
            card = _format_feishu_card(result)
            logging.info(f"Review done: {result['pn']}P/{result['wn']}W/{result['bn']}B score={result['rule_score']}")
            _send_message(target_id, card, id_type)
            logging.info("Card sent!")
        except Exception as e:
            traceback.print_exc()
            logging.error(f"Review failed: {e}")

    threading.Thread(target=_do_review, daemon=True).start()


def start_bot() -> None:
    """启动飞书机器人（WebSocket 长连接模式）。"""
    from lark_oapi import EventDispatcherHandler
    from lark_oapi.ws import Client as WsClient

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("请在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

    # 事件分发器——注册消息事件
    verify_token = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
    handler = EventDispatcherHandler.builder(
        encrypt_key="",
        verification_token=verify_token,
    ).register_p2_im_message_receive_v1(
        _on_message
    ).build()

    # 调试：打印所有收到的事件类型
    import logging
    logging.basicConfig(level=logging.INFO, format="[BOT] %(message)s")
    logging.info("Bot ready — waiting for messages...")

    # 启动 WebSocket 长连接
    client = WsClient(app_id, app_secret, event_handler=handler)

    print("Trade Sentry Feishu Bot starting (WebSocket long-connection)...")
    client.start()
