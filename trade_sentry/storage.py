"""Trade Sentry — 审计记录读写。

JSONL 格式存储审查记录，提供查询接口供 engine.py 规则使用。
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from trade_sentry.schemas import AuditRecord


def _audit_path() -> Path:
    return Path(os.environ.get("TRADE_SENTRY_DATA_DIR", "data")) / "audit.jsonl"


def save_audit(record: AuditRecord) -> str:
    """追加一条审查记录到 JSONL 文件。返回 audit_id。"""
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    line = record.model_dump_json()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return record.audit_id


def _load_all() -> list[dict]:
    """加载全部审计记录。"""
    path = _audit_path()
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def load_recent_audits(n: int = 20, symbol: Optional[str] = None) -> list[dict]:
    """读取最近 N 条审查记录。可按标的过滤。"""
    all_records = _load_all()
    if symbol:
        all_records = [r for r in all_records if r.get("symbol") == symbol]
    return all_records[-n:]


def count_today_audits() -> int:
    """统计今日实际执行的交易次数。

    只统计 user_action 为 "accepted" 或 "overridden" 的记录，
    "pending"（仅审查未决策）不计入。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    all_records = _load_all()
    return sum(1 for r in all_records
               if r.get("timestamp", "").startswith(today)
               and r.get("user_action", "") in ("accepted", "overridden"))


def get_position_average(n: int = 10) -> Optional[float]:
    """计算近 N 笔已确认交易的仓位均值。数据不足返回 None。"""
    all_records = _load_all()
    accepted = [r for r in all_records
                if r.get("user_action") in ("accepted", "overridden")]
    if len(accepted) < n:
        return None
    positions = [r.get("position_pct", 0) for r in accepted[-n:]]
    return sum(positions) / len(positions)


def get_emotion_trend(n: int = 3) -> Optional[list[int]]:
    """获取近 N 次情绪自评序列。不足返回 None。"""
    all_records = _load_all()
    if len(all_records) < n:
        return None
    return [r.get("emotion", 3) for r in all_records[-n:]]


def get_symbol_touches(symbol: str, level_field: str, pct_threshold: float = 2.0,
                       n: int = 5) -> int:
    """统计同一标的近期触碰同一关键位的次数。供 S01 使用。"""
    recent = load_recent_audits(n=n, symbol=symbol)
    # 基于 position_pct 和 plan 中的 stop_loss 做粗略判断
    # v1 简化实现：计数同标的记录数
    return len(recent)
