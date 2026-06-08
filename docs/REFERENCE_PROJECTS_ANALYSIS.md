# 可借鉴开源项目深度结构分析（v2 精简版）

> 本文档对 Trade Sentry v2 精简框架中引用的关键开源项目进行逐项分析。
> **v2 变更**: 框架从 10 模块精简为 6 文件（config / input / engine / storage / reviewer / output
> + schemas），影子分析器（原 M6）移至远期 Roadmap。本文档的映射关系已同步更新。
> **最近修正**: 存储层从 output.py 独立为 storage.py，解决 engine.py 规则读取历史记录的依赖。

---

## 目录

1. [Execution-Discipline-Agent — 规则引擎骨架](#1-execution-discipline-agent)
2. [OpenAlice — Guards 管道模式](#2-openalice)
3. [swarm-trader — 市场状态 + 硬规则 + 日志](#3-swarm-trader)
4. [Vibe-Trading — Shadow Account（远期参考）](#4-vibe-trading)
5. [其他快速参考项目](#5-其他快速参考项目)
6. [Trutle — 数据层设计模式](#6-trutle--数据层设计模式)
7. [汇总对照表](#7-汇总对照表)

---

## 1. Execution-Discipline-Agent

### 基本信息

| 属性 | 值 |
|------|---|
| 仓库 | `QuantTradingOS/Execution-Discipline-Agent` |
| Stars | 新项目（早期） |
| 语言 | Python 100% |
| 状态 | v1，功能完整 |

### 目录结构

```
Execution-Discipline-Agent/
├── app.py                     # Streamlit 界面
├── src/
│   ├── agent.py               # ★ 编排层
│   ├── rules.py               # ★ 5 条合规规则
│   ├── schemas.py             # Pydantic 模型
│   └── memory.py              # 持久化状态
├── data/                      # 示例数据
└── state/memory.json          # 合规历史
```

### 🎯 核心借鉴：规则函数模式

**这是 Trade Sentry `engine.py` 最直接的设计参考。**

**规则函数模式**（可直接复用）:
```python
# 每条规则：独立函数，统一签名，返回违规列表
def check_regime_allowed(trades, allowed_regimes) -> list[Violation]
def check_missing_stops(trades, stop_required) -> list[Violation]
def check_position_sizing(trades, position_limits, account_size) -> list[Violation]
def check_r_multiple(trades) -> list[Violation]
```

**Violation 数据结构** — 对应我们的 `RuleResult`:
```
violation_type   → rule_id
detail          → detail
trade_index     → 原项目是批量检查，我们改为单笔
```

**合规评分公式** — 可直接用于 `output.py`:
```
compliance_score = 1.0 - (violations / total_checks)
```

**数据模型设计** — `schemas.py` 参考:
```
Trade (pydantic)      → TradingPlan (我们)
TradingPlan (pydantic) → UserConfig (我们)
Violation             → RuleResult (我们)
DisciplineReport      → ReviewReport (我们)
```

**映射到 Trade Sentry v2**:
```
Execution-Discipline-Agent      →  Trade Sentry v2
─────────────────────────────────────────────────
src/agent.py (编排层)            →  engine.py 的 RuleEngine 类
src/rules.py (规则函数)          →  engine.py 的 16 个 rule_xxx() 函数
src/schemas.py (Pydantic)       →  schemas.py
src/memory.py (持久化)          →  storage.py 的 save_audit()
data/trades.example.csv          →  用户交易日志格式参考
data/plan.example.json           →  config.yaml 结构参考
```

**借鉴方式**: 架构借鉴 — 理解设计后在 engine.py 中实现类似结构。

---

## 2. OpenAlice

### 基本信息

| 属性 | 值 |
|------|---|
| 仓库 | `TraderAlice/OpenAlice` |
| Stars | 4,900+ |
| 语言 | TypeScript 81% + Python 17% |
| 状态 | 🔥🔥 极活跃 (1,067 commits) |

### 核心架构：Guards 管道

```
UTA Service 进程
├── IBroker 实现 (CCXT/Alpaca/IBKR/...)
├── Trading-as-Git 状态机
├── Guards 管道  ← ★ 核心参考
│   ├── MaxPositionSize
│   ├── Cooldown
│   └── SymbolWhitelist
```

### 🎯 核心借鉴：管道执行 + 硬拒绝

**设计理念**（直接复用）:
> Think of it as ESLint for Trading.

**管道模式**:
- 多个 guard 串联执行
- 任一失败则拦截（我们改为收集所有失败再汇总，用户体验更好）
- Per-account 配置（我们改为 per-user config.yaml）

**Guard 类型 → 我们的规则分类**:

| OpenAlice Guard | Trade Sentry 规则 |
|-----------------|------------------|
| MaxPositionSize | SZ01 + SZ02 |
| Cooldown | F02 |
| SymbolWhitelist | (v1 不做，远期可选) |
| Guard Pipeline | engine.py RuleEngine 编排逻辑 |

**借鉴方式**: 架构借鉴。

---

## 3. swarm-trader

### 基本信息

| 属性 | 值 |
|------|---|
| 仓库 | `zhound420/swarm-trader` |
| Stars | 40 |
| 语言 | Python 69% |
| 状态 | 活跃开发中 |

### 目录结构

```
swarm-trader/
├── src/
│   ├── config.py              # ★ MODES dict — 单源配置
│   └── agents/
│       └── market_regime.py   # ★ 市场状态分类器
├── risk_manager.py            # ★ 11 条硬规则 (code-enforced)
├── trade_journal.py           # ★ 交易日志
├── trade_alerts.py            # ★ 异常检测 (mode-aware)
└── trading_mode.json          # 模式引导
```

### 🎯 核心借鉴一：risk_manager.py — 规则类型分类

**设计原则**（直接用于 engine.py）:
> 每条规则在代码层强制执行，没有 agent 可以绕过。

**规则类型分类**:

| 类型 | 含义 | 我们的规则 |
|------|------|-----------|
| `hard_block` | 无条件禁止 | T01 (无条件部分), M01, M02, M03, M04, SZ01, SZ02 |
| `circuit_breaker` | 触发熔断 | (v1 不做日亏损熔断，远期) |
| `conditional` | 条件性禁止 | T01 (条件部分), S01, E01 |
| `limit` | 阈值限制 | F01, F02, SZ03, V01 |

### 🎯 核心借鉴二：market_regime.py

swarm-trader 使用单一维度的三状态分类（trending / range-bound / volatile）。

Trade Sentry 将其改进为**双维度正交分类**：
- 维度一（趋势方向）: trending_up / trending_down / choppy
- 维度二（波动率水平）: normal / elevated / extreme

这样"上涨趋势+高波动"和"上涨趋势+正常波动"被视为不同环境，规则可以更精细地调整严格程度。swarm-trader 的单维度模型无法表达这种组合。

### 🎯 核心借鉴三：trade_journal.py / trade_alerts.py

- 日志字段设计 → storage.py 审计记录结构
- 异常检测维度 → engine.py V01 规则

**映射**:
```
swarm-trader                 →  Trade Sentry v2
─────────────────────────────────────────────
risk_manager.py              →  engine.py 规则框架 + 规则分类
market_regime.py             →  engine.py classify_regime()
trade_journal.py             →  storage.py 审计记录格式
config.py (MODES dict)       →  config.yaml 用户配置
```

**借鉴方式**: 架构借鉴。

---

## 4. Vibe-Trading

### 基本信息

| 属性 | 值 |
|------|---|
| 仓库 | `HKUDS/Vibe-Trading` |
| Stars | 10,900+ |
| 语言 | Python 89% |
| 状态 | v0.1.9 |

### ⚠️ 远期参考

Vibe-Trading 的 Shadow Account（5 步影子分析）和 Pre-Trade Gate 概念非常有价值，但：

- **Shadow Account** 需要 100+ 笔历史交易才有统计意义
- **Pre-Trade Gate** 已融入 Trade Sentry 的后处理闭环（BLOCK 覆盖机制）

**v1 阶段暂不深入借鉴其代码结构**。Phase 5（远期）实现影子分析器时，Vibe-Trading 将是最核心的参考项目。

**当前阶段仅借鉴**:
- 行为诊断维度的命名（用于未来 M6 设计）
- 审计日志的全链路记录思路（已融入 storage.py）

---

## 5. 其他快速参考项目

| 项目 | 借鉴内容 | 适用文件 |
|------|---------|---------|
| **daily_stock_analysis** (`ZhuLinsen/daily_stock_analysis`) | LLM 交易决策 prompt 结构 + AI 仪表盘布局 | reviewer.py |
| **claude-trading-skills** (`tradermonty/claude-trading-skills`) | 四维度审查框架 (流程/风险/执行/证据) | reviewer.py prompt 维度 |
| **cc-trading** (`liugangdao/cc-trading`) | 交易日志字段设计 [Go 项目，仅 schema 借鉴] | storage.py 审计记录 |
| **qullamaggie_scanner** | 20 点评分制思路（多维度加权评分） | engine.py 可选的评分增强 |
| **stock-screener** (`xang1234/stock-screener`) | 多条件筛选的 Filter 分类组织 | engine.py 规则分类体系 |

---

## 6. Trutle — 数据层设计模式

### 基本信息

| 属性 | 值 |
|------|---|
| 位置 | `D:\workspace\Trutle` |
| 语言 | Python |
| 定位 | AI 辅助的 A/港/美股基本面分析系统 |
| 相关文件 | `scripts/tushare_collector.py`, `scripts/tushare_modules/`, `scripts/config.py` |

### 目录结构（数据层）

```
Trutle/
├── scripts/
│   ├── tushare_collector.py     # ★ 数据采集门面，TushareClient 类
│   ├── config.py                # ★ 环境变量加载 + validate_stock_code()
│   └── tushare_modules/
│       ├── infrastructure.py    # 市场检测、货币检测、财年推断
│       ├── financials.py        # A 股 daily/weekly API 调用、HK/US 市场数据
│       ├── yfinance_integration.py  # yfinance 周线数据
│       ├── akshare_hk.py        # akshare 港股数据
│       ├── constants.py         # VIP 端点映射、港股/美股字段映射
│       ├── other_data.py        # 业务分部、股东数据
│       ├── derived_metrics.py   # 衍生指标（价格分位、PEG 等）
│       └── assembly.py          # 数据包组装器
```

### 🎯 核心借鉴一：股票代码标准化

`validate_stock_code()` 是 Trade Sentry `input.py` 最直接可复用的函数：

```python
# 逻辑流程
"600887"   → 6开头 → "600887.SH"    # A股上海
"000858"   → 0开头 → "000858.SZ"    # A股深圳
"300750"   → 3开头 → "300750.SZ"    # A股创业板
"700"      → 1-5位 → "00700.HK"     # 港股(零填充到5位)
"AAPL"     → 纯字母 → "AAPL.US"     # 美股
"600887.SH"→ 已有后缀 → 不处理       # 已是标准格式
```

> Trade Sentry 可直接复用此逻辑，避免从零实现多市场代码解析。

### 🎯 核心借鉴二：Tushare daily/weekly API

```python
# A股日线 (1年回溯) — Tushare
df = self._safe_call("daily", ts_code=ts_code,
                     start_date=year_ago, end_date=today,
                     fields="ts_code,trade_date,open,high,low,close,vol,amount")

# A股周线 (10年回溯) — Tushare
df = self._safe_call("weekly", ts_code=ts_code,
                     start_date=ten_years_ago, end_date=today,
                     fields="ts_code,trade_date,open,high,low,close,vol,amount")
```

字段与 Trade Sentry 的 `MarketData` 完全匹配（OHLCV），可直接映射。

### 🎯 核心借鉴三：多源 fallback 链

```
港股: yfinance (yf.Ticker.history) → Tushare hk_daily (需broker权限) → akshare
美股: Tushare us_daily (含PE/PB/市值) → yfinance
A股: Tushare daily/weekly (需token) → 本项目不使用akshare做A股日线
```

Trade Sentry 的 fallback 策略（见框架文档 §3 input.py 数据源对比表）直接借鉴了此模式并做了适配——将 akshare 提升为 A 股默认源。

### 🎯 核心借鉴四：缓存 + 限速

```python
# 7天文件缓存（静态数据）
BASIC_CACHE_TTL = 7 * 86400

# 同日 Parquet 缓存（批量数据）
if cache_date == today:
    return pd.read_parquet(cache_file)

# 0.5s 速率限制装饰器
@rate_limit
def _safe_call(self, api_name, **kwargs):
    ...
```

Trade Sentry 使用 SQLite 替代文件缓存（更统一），但 TTL 概念和同日缓存策略可参考。

### 映射

```
Trutle                        →  Trade Sentry
─────────────────────────────────────────────
config.py (validate_stock_code) → input.py (CLI 输入校验)
financials.py (daily/weekly)    → input.py (fetch_market_data)
yfinance_integration.py         → input.py (美股数据)
akshare_hk.py                   → input.py (港股 fallback)
_safe_call() + @rate_limit      → input.py (API 调用封装)
```

**借鉴方式**: 逻辑借鉴 + 代码复用（`validate_stock_code` 可直接移植）。

---

## 7. 汇总对照表

### 7.1 每个源文件 → 主要借鉴项目

| Trade Sentry 文件 | 主要借鉴项目 | 借鉴程度 | 借什么 |
|-------------------|-------------|---------|--------|
| **config.py** | 原生 YAML + swarm-trader config 思路 | 自建 | YAML schema 设计 |
| **schemas.py** | Execution-Discipline-Agent `schemas.py` | 架构借鉴 | Violation/Report 数据模型结构 |
| **input.py** | akshare + yfinance + tushare | **直接引用** | pip 包，多源 fallback 链 |
| | Trutle `validate_stock_code()` | 代码复用 | 多市场代码标准化逻辑 |
| **engine.py** (指标) | pandas-ta-classic + TA-Lib | **直接引用** | 252 个指标计算 |
| **engine.py** (状态) | swarm-trader `market_regime.py` | 架构借鉴 | ADX + 波动率双维分类 |
| **engine.py** (规则) | Execution-Discipline-Agent `rules.py` | 架构借鉴 | 规则函数签名 + 编排模式 |
| | OpenAlice Guards 管道 | 架构借鉴 | 管道执行 + 硬拒绝语义 |
| | swarm-trader `risk_manager.py` | 架构借鉴 | 规则类型分类 (hard_block/conditional/limit) |
| **storage.py** | Execution-Discipline-Agent `memory.py` | 架构借鉴 | 持久化格式 + 查询接口 |
| | cc-trading | schema 借鉴 | 交易日志字段设计 |
| **reviewer.py** | daily_stock_analysis | prompt 借鉴 | LLM 审查维度和 prompt 模板 |
| | claude-trading-skills | 框架借鉴 | 四维度审查框架 |
| **output.py** | rich 库 | **直接引用** | 终端美化输出 |

### 7.2 远期参考（不进 v1）

| 项目 | 何时用 | 做什么 |
|------|--------|--------|
| Vibe-Trading Shadow Account | Phase 5+ | 影子策略提取、行为画像 |
| deltalytix | Phase 5+ | 交易统计仪表盘 |
| TradingAgents | Phase 5+ | 多角色辩论式审查 |
| tradingview-mcp | Phase 4+ | 实时行情补充 |
| momentum-mcp | Phase 4+ | 异常检测补充 |

### 7.3 借鉴深度分级

| 深度 | 含义 | 涉及项目 |
|------|------|---------|
| **🔴 直接引用** | pip install / import 直接用 | pandas-ta, yfinance, akshare, rich |
| **🟡 架构借鉴** | 理解设计后自写类似结构 | Execution-Discipline-Agent, OpenAlice, swarm-trader |
| **🟢 思路借鉴** | 理解概念，独立设计 | daily_stock_analysis, claude-trading-skills, cc-trading |
| **⚪ 远期参考** | v1 不用，记录在案 | Vibe-Trading, deltalytix, TradingAgents |
