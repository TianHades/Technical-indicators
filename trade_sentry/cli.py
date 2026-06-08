"""Trade Sentry — CLI 入口。"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console

from trade_sentry.config import get_config
from trade_sentry.input import fetch_market_data, validate_stock_code
from trade_sentry.engine import compute_indicators, classify_regime, RuleEngine
from trade_sentry.schemas import (
    TradingPlan, Direction, Verdict, ReviewReport, AuditRecord,
)

console = Console(force_terminal=True)


def cmd_fetch(args):
    cfg = get_config()
    ts_code, market = validate_stock_code(args.symbol)
    from trade_sentry.input import get_stock_name
    name = get_stock_name(args.symbol)
    name_str = f" [{name}]" if name else ""
    console.print(f"Code: {args.symbol}{name_str} -> {ts_code} (Market: {market})")

    try:
        data = fetch_market_data(args.symbol, cfg)
        console.print(f"Source: {data.data_source}")
        console.print(f"Daily: {len(data.daily)} bars | Weekly: {len(data.weekly)} bars")
        if data.daily:
            d = data.daily[-1]
            console.print(f"Latest: {d.get('date','?')} O={d.get('open')} "
                          f"H={d.get('high')} L={d.get('low')} C={d.get('close')}")
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _prompt(msg: str) -> str:
    """交互式提示（写入 stderr 避免污染 JSON 输出）。"""
    sys.stderr.write(msg)
    sys.stderr.flush()
    return input().strip()


def _parse_pct(value: str, default: float | None = None) -> float | None:
    """解析百分比输入，自动去除 % 符号。"""
    if not value or not value.strip():
        return default
    value = value.strip().rstrip("%").strip()
    try:
        return float(value)
    except ValueError:
        return default


def _print_raw_indicators(ind) -> None:
    """打印原始技术指标值，方便判断数据是否缺失。"""
    from rich.table import Table as RTable
    t = RTable(title="原始指标数据", show_header=True, box=None, padding=(0, 1))
    t.add_column("类别", style="dim")
    t.add_column("指标")
    t.add_column("日线 | 周线")

    def v(val, fmt=".2f"):
        if val is None:
            return "[dim]N/A[/dim]"
        return f"{val:{fmt}}"

    # 均线
    t.add_row("均线", "MA20/MA50/MA200",
              f"日 {v(ind.sma_20)}/{v(ind.sma_50)}/{v(ind.sma_200)}  |  "
              f"周 {v(ind.weekly_ma20)}/{v(ind.weekly_ma50)}"
              + (" [dim](不足50根)[/dim]" if not ind.weekly_ma50 else ""))
    t.add_row("", "均线排列",
              f"日 {ind.ma_alignment}  |  周 {ind.weekly_ma_alignment}")
    t.add_row("", "趋势方向",
              f"日 {ind.daily_trend}  |  周 {ind.weekly_trend}")

    # 动量
    t.add_row("动量", "RSI(14)",
              f"日 {v(ind.rsi_14, '.1f')}  |  周 {v(ind.weekly_rsi, '.1f')}")
    t.add_row("", "MACD 柱",
              f"日 {v(ind.macd_histogram, '.4f')}  |  周 {v(ind.weekly_macd_histogram, '.4f')}")
    t.add_row("", "KDJ 日",
              f"K={v(ind.kdj_k,'.1f')} D={v(ind.kdj_d,'.1f')} J={v(ind.kdj_j,'.1f')}")
    t.add_row("", "KDJ 周",
              f"K={v(ind.weekly_kdj_k,'.1f')} D={v(ind.weekly_kdj_d,'.1f')} J={v(ind.weekly_kdj_j,'.1f')}")

    # 波动
    t.add_row("波动", "ATR(14)",
              f"日 {v(ind.atr_14)}  |  周 {v(ind.weekly_atr)}")
    t.add_row("", "布林带 日",
              f"下{v(ind.bollinger_lower)} 中{v(ind.bollinger_middle)} 上{v(ind.bollinger_upper)}"
              + (f" 带宽{v(ind.bollinger_width_pct,'.1f')}%" if ind.bollinger_width_pct else ""))

    # 量价
    t.add_row("量价", "量比",
              f"日 {v(ind.volume_ratio, '.1%')}  |  周 {v(ind.weekly_volume_ratio, '.1%')}")

    # 位置
    t.add_row("位置", "支撑",
              f"{v(ind.nearest_support)} (距{v(ind.distance_to_support_pct,'.1f')}%)" if ind.nearest_support else "[dim]无[/dim]")
    t.add_row("", "阻力",
              f"{v(ind.nearest_resistance)} (距{v(ind.distance_to_resistance_pct,'.1f')}%)" if ind.nearest_resistance else "[dim]无[/dim]")

    # K线
    if ind.candlestick_patterns:
        t.add_row("K线", "日线形态", ", ".join(ind.candlestick_patterns[:3]))
    if ind.weekly_candlestick_patterns:
        t.add_row("", "周线形态", ", ".join(ind.weekly_candlestick_patterns[:3]))
    console.print(t)
    console.print()


def _progress(args, msg: str) -> None:
    """输出进度信息。JSON 模式下写入 stderr 避免污染输出。"""
    if args.json:
        sys.stderr.write(f"[trade-sentry] {msg}\n")
        sys.stderr.flush()
    else:
        console.print(f"[dim]{msg}[/dim]")


def cmd_check(args):
    # 先获取标的代码和行情（入场价可默认前日收盘）
    sys.stderr.write("Trade Sentry — 交易计划审查\n")
    sys.stderr.write("-" * 50 + "\n")
    sys.stderr.flush()
    symbol = _prompt("  标的代码: ")
    direction_str = _prompt("  方向 [buy/sell, 回车=buy]: ").lower()
    if not direction_str:
        direction_str = "buy"

    as_of = getattr(args, "date", None)
    if as_of:
        sys.stderr.write(f"  回测日期: {as_of}\n")
        sys.stderr.flush()
    _progress(args, "正在获取行情数据...")
    try:
        cfg = get_config()
        market_data = fetch_market_data(symbol, cfg, as_of=as_of)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]错误: {e}[/red]")
        sys.exit(1)

    # 股票名称
    from trade_sentry.input import get_stock_name
    stock_name = get_stock_name(symbol)
    if stock_name:
        sys.stderr.write(f"  名称: {stock_name}\n")
        sys.stderr.flush()

    last_close = None
    if market_data.daily:
        sorted_daily = sorted(market_data.daily, key=lambda b: str(b.get("date", "")))
        last_close = float(sorted_daily[-1].get("close", 0))
        if last_close:
            sys.stderr.write(f"  前日收盘价: {last_close:.2f}\n")
            sys.stderr.flush()

    # 入场价（默认前日收盘）
    close_hint = f" [回车={last_close:.2f}]" if last_close else ""
    price_input = _prompt(f"  入场价{close_hint}: ")
    entry_price = float(price_input) if price_input else (last_close or 0)
    if not entry_price:
        entry_price = float(_prompt("  入场价 (必填): "))

    position_pct = _parse_pct(_prompt("  仓位比例 [回车=10%]: "), default=10.0)
    holding_input = _prompt("  当前持仓比例 [回车跳过]: ")
    current_holdings = _parse_pct(holding_input) if holding_input else None
    direction = Direction.BUY if direction_str == "buy" else Direction.SELL
    is_buy = direction == Direction.BUY

    if is_buy:
        default_sl = entry_price * 0.9
        sl_hint = f" [回车={default_sl:.2f}]"
    else:
        default_sl = None
        sl_hint = " [回车跳过]"
    stop_loss = _prompt(f"  止损价{sl_hint}: ")
    reasoning = _prompt("  交易理由 [回车=长期看好该股票业绩]: ")
    if not reasoning:
        reasoning = "长期看好该股票业绩" if is_buy else "中短期认为其可能会下跌"

    plan = TradingPlan(
        symbol=symbol, direction=direction,
        entry_price=entry_price, position_pct=position_pct,
        current_holdings_pct=current_holdings,
        stop_loss=float(stop_loss) if stop_loss else default_sl,
        reasoning=reasoning, emotion_self_rating=3,
        planned_at=datetime.fromisoformat(as_of) if as_of else datetime.now(),
    )

    # Engine
    _progress(args, "正在计算技术指标并运行规则...")
    indicators = compute_indicators(market_data)
    regime = classify_regime(indicators, cfg)
    engine = RuleEngine(cfg)
    rule_results = engine.check_all(plan, indicators, regime)

    # LLM Review (unless --no-llm)
    llm_result = None
    if not args.no_llm:
        _progress(args, "LLM 综合审查中...")
        from trade_sentry.reviewer import llm_review
        llm_result = llm_review(plan, regime, rule_results, indicators)
        if llm_result is None:
            console.print("[yellow]LLM 不可用，使用规则评分。[/yellow]")

    # Verdict
    pass_n = sum(1 for r in rule_results if r.verdict == Verdict.PASS)
    warn_n = sum(1 for r in rule_results if r.verdict == Verdict.WARN)
    block_n = sum(1 for r in rule_results if r.verdict == Verdict.BLOCK)
    if block_n > 0:
        final_verdict = Verdict.BLOCK
    elif warn_n > 0:
        final_verdict = Verdict.WARN
    else:
        final_verdict = Verdict.PASS

    # Score
    if llm_result:
        overall_score = float(llm_result.overall_reasonableness)
    else:
        overall_score = max(1.0, 10.0 - warn_n * 1.0 - block_n * 3.0)

    # Build report
    audit_id = uuid.uuid4().hex[:12]
    report = ReviewReport(
        audit_id=audit_id, timestamp=datetime.now(),
        plan=plan, regime=regime, rule_results=rule_results,
        llm_review=llm_result, verdict=final_verdict,
        overall_score=overall_score,
        suggestions=[],
    )

    # 原始指标数据（帮助用户判断数据完整性）
    if not args.json:
        _print_raw_indicators(indicators)

    # Output
    if args.json:
        from trade_sentry.output import render_json
        print(render_json(report))
    else:
        from trade_sentry.output import render_terminal
        render_terminal(report)

    # Post-review: 确认是否执行
    override_reason = None
    if args.json:
        user_action = "pending"
    elif final_verdict == Verdict.BLOCK:
        choice = _prompt("\n是否仍要执行? [y=强制覆盖 / Enter=放弃]: ").lower()
        if choice == "y":
            override_reason = _prompt("  覆盖理由: ")
            user_action = "overridden"
        else:
            console.print("[dim]已取消[/dim]")
            return
    else:
        choice = _prompt("\n是否执行此交易? [y=确认 / Enter=放弃]: ").lower()
        if choice == "y":
            user_action = "accepted"
        else:
            console.print("[dim]已取消[/dim]")
            return

    # Audit（只有确认执行或强制覆盖才计入交易次数）
    from trade_sentry.storage import save_audit
    record = AuditRecord(
        audit_id=audit_id, timestamp=report.timestamp,
        symbol=plan.symbol, direction=plan.direction,
        position_pct=plan.position_pct, emotion=plan.emotion_self_rating,
        verdict=final_verdict, user_action=user_action,
        user_modification=override_reason,
    )
    save_audit(record)
    console.print(f"[dim]审计 ID: {audit_id}[/dim]")


def cmd_batch(args):
    """批量回测：Phase1 串行取数据+规则，Phase2 并发 LLM。"""
    from datetime import datetime as dt, timedelta
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from rich.table import Table as RTable
    from trade_sentry.engine import compute_indicators, classify_regime, RuleEngine
    from trade_sentry.schemas import TradingPlan, Direction, Verdict

    # 交互式输入
    symbol = getattr(args, "symbol", None)
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)

    if not symbol:
        sys.stderr.write("Trade Sentry — 批量回测\n")
        sys.stderr.write("-" * 50 + "\n")
        sys.stderr.flush()
        symbol = _prompt("  标的代码: ")
        dir_str = _prompt("  方向 [buy/sell, 回车=buy]: ").lower()
        direction = Direction.SELL if dir_str == "sell" else Direction.BUY
        from_date = _prompt("  起始日期 (YYYY-MM-DD): ")
        to_date = _prompt("  结束日期 (YYYY-MM-DD): ")
        interval_str = _prompt("  间隔天数 [回车=1]: ")
        interval = int(interval_str) if interval_str else 1
        llm_str = _prompt("  是否启用 LLM? [y/N]: ").lower()
        use_llm = llm_str == "y"
    else:
        direction = Direction.BUY
        interval = getattr(args, "interval", 1) or 1
        use_llm = getattr(args, "llm", False)

    cfg = get_config()
    start = dt.strptime(from_date, "%Y-%m-%d")
    end = dt.strptime(to_date, "%Y-%m-%d")

    mode = f"每{interval}天 + LLM" if use_llm else f"每{interval}天"
    dir_label = "买入" if direction == Direction.BUY else "卖出(平仓)"
    is_buy = direction == Direction.BUY
    reason = "长期看好该股票业绩" if is_buy else "中短期认为其可能会下跌"
    sys.stderr.write(f"\n批量回测 {symbol}  {from_date} → {to_date}  ({mode})\n")
    sys.stderr.write(f"{dir_label} 仓位10% {'止损=收盘×90%' if is_buy else '不设止损'} 理由={reason}\n")
    sys.stderr.write("=" * 60 + "\n")
    sys.stderr.flush()
    sys.stderr.write(f"批量回测 {symbol}  {args.from_date} → {args.to_date}  ({mode})\n")
    sys.stderr.write("=" * 60 + "\n")
    sys.stderr.flush()

    # Phase 1: 串行取数据 + 跑规则（Tushare 限速 0.5s/次）
    snapshots = []  # (date_str, close, v, pn, wn, bn, rule_score, plan, regime, results, ind)
    indicators = []  # 存储每笔的 IndicatorSnapshot，供 CSV 导出
    day_count = 0
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        day_count += 1
        current += timedelta(days=1)

        if (day_count - 1) % interval != 0:
            continue

        try:
            md = fetch_market_data(symbol, cfg, as_of=date_str)
            if not md.daily:
                continue
            sorted_daily = sorted(md.daily, key=lambda b: str(b.get("date", "")))
            close = float(sorted_daily[-1].get("close", 0))
            if close <= 0:
                continue

            is_buy = direction == Direction.BUY
            plan = TradingPlan(
                symbol=symbol, direction=direction,
                entry_price=close, position_pct=10,
                stop_loss=round(close * 0.9, 2) if is_buy else None,
                reasoning="长期看好该股票业绩" if is_buy else "中短期认为其可能会下跌",
                emotion_self_rating=3,
                planned_at=current - timedelta(days=1),
            )
            ind = compute_indicators(md)
            regime = classify_regime(ind, cfg)
            engine = RuleEngine(cfg)
            results = engine.check_all(plan, ind, regime)

            pn = sum(1 for r in results if r.verdict == Verdict.PASS)
            wn = sum(1 for r in results if r.verdict == Verdict.WARN)
            bn = sum(1 for r in results if r.verdict == Verdict.BLOCK)
            rule_score = max(1, 10 - wn - bn * 3)
            v = "BLOCK" if bn > 0 else ("WARN" if wn > 0 else "PASS")

            snapshots.append((date_str, close, v, pn, wn, bn, rule_score,
                              plan, regime, results, ind))
            indicators.append(ind)
            sys.stderr.write(f"  {date_str}  C={close:.2f}  {v}  "
                             f"{pn}P/{wn}W/{bn}B  rule={rule_score}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"  {date_str}  SKIP: {e}\n")
            sys.stderr.flush()

    if not snapshots:
        console.print("[red]无有效数据[/red]")
        return

    # Phase 2: 并发 LLM（10 线程）
    llm_results: dict[int, any] = {}  # idx -> LLMReviewResult
    if use_llm:
        sys.stderr.write(f"\nLLM 并发审查中（10 线程）...\n")
        sys.stderr.flush()
        from trade_sentry.reviewer import llm_review

        def _call_llm(idx: int, plan, regime, results, ind):
            import time as _time
            last_err = None
            for attempt in range(3):
                try:
                    r = llm_review(plan, regime, results, ind)
                    if r is not None:
                        return idx, r
                except Exception as e:
                    last_err = e
                _time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s 递增等待
            sys.stderr.write(f"  [{snapshots[idx][0]}] LLM FAIL after 3 retries: {last_err}\n")
            sys.stderr.flush()
            return idx, None

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(_call_llm, i, s[7], s[8], s[9], s[10]): i
                for i, s in enumerate(snapshots)
            }
            for future in as_completed(futures):
                idx, result = future.result()
                llm_results[idx] = result
                llm_score = result.overall_reasonableness if result else None
                s = snapshots[idx]
                sys.stderr.write(f"  {s[0]}  LLM={llm_score}\n")
                sys.stderr.flush()

    # 汇总表 + 保存文件
    tag = f"{symbol}_{args.from_date}_{args.to_date}".replace("-", "")
    rows = []
    for i, s in enumerate(snapshots):
        date_str, close, v, pn, wn, bn, rule_score = s[:7]
        llm_r = llm_results.get(i) if use_llm else None
        llm_score = llm_r.overall_reasonableness if llm_r else None
        display_score = llm_score if llm_score is not None else rule_score
        rows.append((date_str, close, v, pn, wn, bn, display_score, llm_score, llm_r))

    table = RTable(title=f"批量回测结果 — {symbol} ({mode})")
    table.add_column("日期")
    table.add_column("收盘", justify="right")
    table.add_column("判定")
    table.add_column("P/W/B")
    table.add_column("规则", justify="right")
    if use_llm:
        table.add_column("LLM", justify="right")

    for row_data in rows:
        date_str, close, v, pn, wn, bn, score, llm_score, _ = row_data
        color = "green" if v == "PASS" else ("yellow" if v == "WARN" else "red")
        cells = [date_str, f"{close:.2f}", f"[{color}]{v}[/{color}]",
                 f"{pn}/{wn}/{bn}", str(score)]
        if use_llm:
            cells.append(str(llm_score) if llm_score is not None else "—")
        table.add_row(*cells)

    console.print(table)

    total = len(rows)
    pass_n = sum(1 for r in rows if r[2] == "PASS")
    warn_n = sum(1 for r in rows if r[2] == "WARN")
    block_n = sum(1 for r in rows if r[2] == "BLOCK")
    avg_score = sum(r[6] for r in rows) / total if total > 0 else 0
    console.print(f"\n共 {total} 个采样点 | PASS: {pass_n} ({pass_n/total*100:.0f}%) | "
                  f"WARN: {warn_n} ({warn_n/total*100:.0f}%) | "
                  f"BLOCK: {block_n} ({block_n/total*100:.0f}%) | "
                  f"均分: {avg_score:.1f}")

    # 保存 CSV 汇总表（含全部指标 + LLM 分析）
    import csv, os as _os
    _os.makedirs("data", exist_ok=True)
    csv_path = f"data/batch_{tag}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        base_header = [
            "日期", "收盘", "判定", "通过", "警告", "拦截", "规则评分",
            "日线K线形态", "周线K线形态",
        ]
        llm_header = ["LLM评分", "技术指标解读", "操作建议", "关键关切", "偏误检测", "最终建议"]
        indicator_header = [
            "市场状态", "RSI_日", "RSI_周", "MACD柱_日", "MACD柱_周",
            "KDJ_K_日", "KDJ_D_日", "KDJ_J_日", "KDJ_K_周", "KDJ_D_周", "KDJ_J_周",
            "MA20_日", "MA50_日", "MA20_周", "MA50_周", "均线排列_日", "均线排列_周",
            "日线趋势", "周线趋势", "ATR_日", "ATR_周", "量比_日", "量比_周",
            "支撑", "距支撑%", "阻力", "距阻力%",
        ]

        header = base_header
        if use_llm:
            header += llm_header
        header += indicator_header
        writer.writerow(header)

        for i, row_data in enumerate(rows):
            date_str, close, v, pn, wn, bn, score, llm_score, llm_r = row_data
            ind = indicators[i]
            base_row = [
                date_str, f"{close:.2f}", v, pn, wn, bn, score,
                ", ".join(ind.candlestick_patterns[:3]) if ind.candlestick_patterns else "",
                ", ".join(ind.weekly_candlestick_patterns[:3]) if ind.weekly_candlestick_patterns else "",
            ]
            indicator_row = [
                snapshots[i][8].regime_description if snapshots[i][8] else "",
                f"{ind.rsi_14:.1f}" if ind.rsi_14 else "",
                f"{ind.weekly_rsi:.1f}" if ind.weekly_rsi else "",
                f"{ind.macd_histogram:.4f}" if ind.macd_histogram else "",
                f"{ind.weekly_macd_histogram:.4f}" if ind.weekly_macd_histogram else "",
                f"{ind.kdj_k:.1f}" if ind.kdj_k else "",
                f"{ind.kdj_d:.1f}" if ind.kdj_d else "",
                f"{ind.kdj_j:.1f}" if ind.kdj_j else "",
                f"{ind.weekly_kdj_k:.1f}" if ind.weekly_kdj_k else "",
                f"{ind.weekly_kdj_d:.1f}" if ind.weekly_kdj_d else "",
                f"{ind.weekly_kdj_j:.1f}" if ind.weekly_kdj_j else "",
                f"{ind.sma_20:.2f}" if ind.sma_20 else "",
                f"{ind.sma_50:.2f}" if ind.sma_50 else "",
                f"{ind.weekly_ma20:.2f}" if ind.weekly_ma20 else "",
                f"{ind.weekly_ma50:.2f}" if ind.weekly_ma50 else "",
                ind.ma_alignment, ind.weekly_ma_alignment,
                ind.daily_trend, ind.weekly_trend,
                f"{ind.atr_14:.2f}" if ind.atr_14 else "",
                f"{ind.weekly_atr:.2f}" if ind.weekly_atr else "",
                f"{ind.volume_ratio:.1%}" if ind.volume_ratio is not None else "",
                f"{ind.weekly_volume_ratio:.1%}" if ind.weekly_volume_ratio is not None else "",
                f"{ind.nearest_support:.2f}" if ind.nearest_support else "",
                f"{ind.distance_to_support_pct:.1f}" if ind.distance_to_support_pct is not None else "",
                f"{ind.nearest_resistance:.2f}" if ind.nearest_resistance else "",
                f"{ind.distance_to_resistance_pct:.1f}" if ind.distance_to_resistance_pct is not None else "",
            ]
            r = base_row
            if use_llm:
                r += [
                    str(llm_score) if llm_score is not None else "",
                    llm_r.indicator_analysis.replace("\n", " ") if llm_r and llm_r.indicator_analysis else "",
                    llm_r.trading_advice.replace("\n", " ") if llm_r and llm_r.trading_advice else "",
                    "; ".join(llm_r.key_concerns) if llm_r and llm_r.key_concerns else "",
                    "; ".join(llm_r.cognitive_bias_detected) if llm_r and llm_r.cognitive_bias_detected else "",
                    llm_r.final_advice.replace("\n", " ") if llm_r and llm_r.final_advice else "",
                ]
            r += indicator_row
            writer.writerow(r)
    console.print(f"\n[dim]CSV 已保存: {csv_path}[/dim]")

    # 保存完整分析报告（含每笔 LLM 审查详情）
    if use_llm:
        md_path = f"data/batch_{tag}_full.md"
        lines = [f"# 批量回测完整报告 — {symbol}",
                 f"区间: {args.from_date} → {args.to_date} | 间隔: {interval} 天",
                 f"共 {total} 个采样点 | 均分: {avg_score:.1f}",
                 "", "---", ""]
        for row_data in rows:
            date_str, close, v, pn, wn, bn, score, _, llm_r = row_data
            lines.append(f"## {date_str}  收盘 {close:.2f}  判定 {v}  规则 {score}")
            if llm_r:
                lines.append(f"**LLM 评分: {llm_r.overall_reasonableness}**")
                if llm_r.indicator_analysis:
                    lines.append(f"\n### 技术指标解读\n\n{llm_r.indicator_analysis}")
                if llm_r.trading_advice:
                    lines.append(f"\n### 操作建议\n\n{llm_r.trading_advice}")
                if llm_r.cognitive_bias_detected:
                    lines.append(f"\n### 偏误检测\n\n" +
                                 "\n".join(f"- {b}" for b in llm_r.cognitive_bias_detected))
                if llm_r.key_concerns:
                    lines.append(f"\n### 关键关切\n\n" +
                                 "\n".join(f"- {c}" for c in llm_r.key_concerns))
                if llm_r.final_advice:
                    lines.append(f"\n### 最终建议\n\n{llm_r.final_advice}")
            lines.append("\n---\n")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        console.print(f"[dim]完整报告已保存: {md_path}[/dim]")


def main():
    parser = argparse.ArgumentParser(
        description="Trade Sentry - Pre-trade sanity check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  trade-sentry fetch --symbol 600036     # Fetch market data
  trade-sentry fetch --symbol AAPL       # US stock
  trade-sentry check                     # Interactive review
  trade-sentry check --no-llm            # Skip LLM, rules only
  trade-sentry check --json              # JSON output
        """,
    )
    subparsers = parser.add_subparsers(dest="command")

    f = subparsers.add_parser("fetch", help="Fetch market data")
    f.add_argument("--symbol", required=True, help="Stock code")
    f.set_defaults(func=cmd_fetch)

    c = subparsers.add_parser("check", help="Interactive review")
    c.add_argument("--no-llm", action="store_true", help="Skip LLM review")
    c.add_argument("--json", action="store_true", help="JSON output")
    c.add_argument("--date", help="回测日期 YYYY-MM-DD（数据只用到该日，防止未来信息）")
    c.set_defaults(func=cmd_check)

    b = subparsers.add_parser("batch", help="批量回测（交互式或命令行）")
    b.add_argument("--symbol", default=None, help="股票代码（不填则交互式输入）")
    b.add_argument("--from", dest="from_date", default=None, help="起始日期 YYYY-MM-DD")
    b.add_argument("--to", dest="to_date", default=None, help="结束日期 YYYY-MM-DD")
    b.add_argument("--interval", type=int, default=1, help="每隔几天采样一次 (默认1)")
    b.add_argument("--llm", action="store_true", help="启用 LLM 审查")
    b.set_defaults(func=cmd_batch)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
