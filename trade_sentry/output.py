"""Trade Sentry — 输出格式化。

终端报告 (rich) + JSON 导出。
"""

from __future__ import annotations

import json
from datetime import datetime

from rich.console import Console
from rich.table import Table

from trade_sentry.schemas import Verdict, ReviewReport

console = Console(force_terminal=True)


def _color_for(verdict: Verdict) -> str:
    if verdict == Verdict.PASS:
        return "green"
    elif verdict == Verdict.WARN:
        return "yellow"
    return "red"


def _badge_for(verdict: Verdict) -> str:
    if verdict == Verdict.PASS:
        return "[green]PASS[/green]"
    elif verdict == Verdict.WARN:
        return "[yellow]WARN[/yellow]"
    return "[red]BLOCK[/red]"


def render_terminal(report: ReviewReport) -> None:
    """rich 彩色终端输出。"""
    console.print()
    console.rule(f"审查报告  #{report.audit_id}")

    plan = report.plan
    from trade_sentry.input import get_stock_name
    name = get_stock_name(plan.symbol)
    name_str = f" [{name}]" if name else ""
    console.print(f"  标的: {plan.symbol}{name_str}  "
                  f"方向: {plan.direction.value.upper()}  "
                  f"价格: {plan.entry_price}")
    console.print(f"  仓位: {plan.position_pct}%  "
                  f"止损: {plan.stop_loss or '未设置'}")
    console.print(f"  市场状态: {report.regime.regime_description}")
    console.print()

    # 规则结果表
    table = Table(show_header=False, box=None, padding=(0, 1))
    for r in report.rule_results:
        table.add_row(
            _badge_for(r.verdict),
            f"[bold]{r.rule_id}[/bold] {r.rule_name}",
            f"[dim]{r.detail}[/dim]",
        )
    console.print(table)
    console.print()

    # LLM 审查
    if report.llm_review:
        llm = report.llm_review
        console.print(f"[bold]技术指标解读[/bold]")
        console.print(f"  {llm.indicator_analysis or '(无)'}")
        console.print()
        console.print(f"[bold]操作建议[/bold]")
        console.print(f"  {llm.trading_advice or '(无)'}")
        console.print()
        console.print(f"[bold]情绪/偏误检测[/bold]")
        if llm.cognitive_bias_detected:
            for b in llm.cognitive_bias_detected:
                console.print(f"  - [yellow]{b}[/yellow]")
        else:
            console.print(f"  未检测到明显偏误")
        console.print()
        if llm.key_concerns:
            console.print(f"[bold]关键关切[/bold]")
            for c in llm.key_concerns:
                console.print(f"  - [yellow]{c}[/yellow]")
            console.print()
        console.print(f"[bold]综合评分: {llm.overall_reasonableness}/10[/bold]")
        if llm.final_advice:
            console.print(f"  [dim]{llm.final_advice}[/dim]")
        console.print()

    # 结论
    color = _color_for(report.verdict)
    console.print(
        f"  结论: [{color}]{report.verdict.value.upper()}[/{color}]  "
        f"({report.pass_count} 通过 / {report.warn_count} 警告 / "
        f"{report.block_count} 拦截)"
    )
    console.rule()


def render_json(report: ReviewReport) -> str:
    """JSON 输出。"""
    data = {
        "audit_id": report.audit_id,
        "timestamp": report.timestamp.isoformat(),
        "symbol": report.plan.symbol,
        "direction": report.plan.direction.value,
        "entry_price": report.plan.entry_price,
        "position_pct": report.plan.position_pct,
        "stop_loss": report.plan.stop_loss,
        "regime": report.regime.regime_description,
        "verdict": report.verdict.value,
        "overall_score": report.overall_score,
        "rule_results": [
            {
                "rule_id": r.rule_id,
                "rule_name": r.rule_name,
                "verdict": r.verdict.value,
                "detail": r.detail,
                "suggestion": r.suggestion,
            }
            for r in report.rule_results
        ],
        "llm_review": {
            "indicator_analysis": report.llm_review.indicator_analysis,
            "trading_advice": report.llm_review.trading_advice,
            "overall_reasonableness": report.llm_review.overall_reasonableness,
            "key_concerns": report.llm_review.key_concerns,
            "cognitive_bias_detected": report.llm_review.cognitive_bias_detected,
            "final_advice": report.llm_review.final_advice,
        } if report.llm_review else None,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)
