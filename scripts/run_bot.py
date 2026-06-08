#!/usr/bin/env python3
"""Trade Sentry — 飞书机器人启动脚本（WebSocket 长连接模式）。

用法:
    python scripts/run_bot.py
"""

from trade_sentry.bot import start_bot

if __name__ == "__main__":
    start_bot()
