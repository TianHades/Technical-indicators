# Trade Sentry

交易执行前的 sanity check——输入交易计划，自动拉行情、算指标、跑规则、AI 审查，输出 PASS / WARN / BLOCK。

**核心理念**：确定性规则拦住 ~80% 的技术面硬伤（数学可验证的），LLM 捕捉剩余的 ~20%（需要语义理解的）。系统给建议，用户做最终决策。

## 快速开始

### 1. 环境准备

```bash
# Python 3.11+
git clone <repo-url> && cd NetFinance
pip install -e .
```

### 2. 配置

编辑 `config.yaml`，设置你的市场（A/US/HK）和风险偏好：

```yaml
user:
  market: "A"           # A | US | HK
  account_size: 100000
thresholds:
  max_position_pct: 20
  max_trades_per_day: 5
```

> A 股用户如需更高数据质量，可在 `.env` 中配置 `TUSHARE_TOKEN`（需在 [tushare.pro](https://tushare.pro) 注册获取）。未配置时自动使用 akshare（免费，无需 token）。

### 3. 第一次审查

```bash
# 交互式（推荐）
trade-sentry check

# 命令行
trade-sentry check --symbol 600036 --direction buy --price 38.5 \
    --position 15 --stop 37.0 --current-holdings 45 \
    --reason "银行股估值修复" --emotion 2

# 离线模式（跳过 LLM，仅规则检查）
trade-sentry check --symbol AAPL --direction buy --price 195.0 \
    --position 10 --stop 190.0 --reason "回调支撑位" --emotion 1 --no-llm

# JSON 输出
trade-sentry check ... --json
```

审查报告示例：

```
═══════════════════════════════════════════
 Trade Sentry 审查报告  #20260606-001
═══════════════════════════════════════════
 标的: 600036  方向: BUY  价格: 38.50
 仓位: 15%  止损: 37.00  (-3.9%)

 市场状态: 下跌趋势 + 正常波动

 规则检查:
 ✅ T04 ADX 有效性         通过
 ✅ M01 RSI 极端区         通过 (RSI=52)
 ⚠️  T01 逆势操作          下跌趋势中做多，建议关注支撑位
 ❌ S01 阻力位追高          距阻力位 1.2%，多次测试未突破
 ✅ STOP01 止损计划         通过

 LLM 审查: 合理性评分 5/10 — 阻力位附近入场成功率低

 结论: ⚠️ WARN (11 通过 / 3 警告 / 2 拦截)
═══════════════════════════════════════════
```

## 文档导航

| 文档 | 内容 |
|------|------|
| [框架设计](docs/TRADE_SENTRY_FRAMEWORK.md) | 系统架构、7 个模块设计、16 条规则清单、数据流、开发阶段 |
| [规则参考手册](docs/RULES_REFERENCE.md) | 每条规则的判定标准、阈值、触发条件、建议操作 |
| [开源项目借鉴](docs/REFERENCE_PROJECTS_ANALYSIS.md) | 10 个参考项目的结构分析、借鉴方式、映射关系 |

## 依赖

| 包 | 用途 | 必需 |
|----|------|------|
| `pandas-ta-classic` | 252 个技术指标 | ✅ |
| `yfinance` | 美股数据 | ✅ |
| `akshare` | A 股数据 | ✅ |
| `pydantic` | 数据模型 | ✅ |
| `pyyaml` | 配置文件 | ✅ |
| `rich` | 终端美化 | ✅ |
| `tushare` | A 股数据（增强） | 可选 |

LLM 审查需要 Claude API 访问权限（通过 Claude Code 环境）。使用 `--no-llm` 可跳过。

## 许可

MIT
