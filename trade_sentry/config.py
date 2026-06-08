"""Trade Sentry — 用户配置加载。

从 config.yaml 读取用户自定义阈值，缺失字段使用默认值填充。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "user": {},
    "thresholds": {
        "max_position_pct": 20,
        "max_total_position_pct": 80,
        "max_trades_per_day": 5,
        "cooldown_minutes": 30,
    },
    "rules": {
        "disabled": [],
        "block_to_warn": [],
        "auto_downgrade_after": 3,
    },
    "regime": {
        "adx_trend_threshold": 25,
        "adx_choppy_threshold": 20,
        "atr_volatile_percentile": 80,
    },
    "data": {
        "cache_days": 1,
        "lookback_days": 250,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """加载用户配置，缺失字段使用默认值。"""
    if config_path is None:
        for candidate in [Path.cwd() / "config.yaml",
                          Path(__file__).parent.parent / "config.yaml"]:
            if candidate.exists():
                config_path = candidate
                break

    if config_path and config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    else:
        user = {}
    return _deep_merge(DEFAULT_CONFIG, user)


class Config:
    """配置访问器。"""

    def __init__(self, config_dict: dict | None = None):
        self._data = config_dict or load_config()

    def threshold(self, key: str) -> Any:
        return self._data["thresholds"].get(key, DEFAULT_CONFIG["thresholds"].get(key))

    def rule(self, key: str) -> Any:
        return self._data["rules"].get(key, DEFAULT_CONFIG["rules"].get(key))

    def regime(self, key: str) -> Any:
        return self._data["regime"].get(key, DEFAULT_CONFIG["regime"].get(key))

    def user(self, key: str) -> Any:
        return self._data["user"].get(key, DEFAULT_CONFIG["user"].get(key))

    def data(self, key: str) -> Any:
        return self._data["data"].get(key, DEFAULT_CONFIG["data"].get(key))

    @property
    def disabled_rules(self) -> list[str]:
        return self.rule("disabled")

    @property
    def block_to_warn_rules(self) -> list[str]:
        return self.rule("block_to_warn")

    @property
    def auto_downgrade_after(self) -> int:
        return self.rule("auto_downgrade_after")


_config_instance: Config | None = None


def get_config(config_path: Path | None = None) -> Config:
    global _config_instance
    if _config_instance is None:
        _config_instance = Config(load_config(config_path))
    return _config_instance
