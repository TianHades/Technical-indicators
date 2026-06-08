# Trade Sentry — 交易计划审查系统 框架设计（v2 精简版）

> **定位**: 交易执行前，对交易计划做快速 sanity check，拦截技术面硬伤和非理性行为。
> **核心比喻**: ESLint for Trading。只做一件事：**别犯低级错误**。
> **设计原则**: v1 做对核心闭环。高级功能（影子分析、MCP 集成）作为远期 Roadmap，不进入 v1。

---

## 目录

1. [系统总览](#1-系统总览)
2. [架构](#2-架构)
3. [模块设计](#3-模块设计)
4. [规则清单](#4-规则清单)
5. [数据流](#5-数据流)
6. [后处理闭环](#6-后处理闭环)
7. [引用清单：复用 vs 自建](#7-引用清单复用-vs-自建)
8. [错误处理策略](#8-错误处理策略)
9. [开发阶段](#9-开发阶段)
10. [远期 Roadmap](#10-远期-roadmap)

---

## 1. 系统总览

```
┌────────────────────────────────────────────┐
│              Trade Sentry v1               │
│          交易计划审查系统 (精简版)           │
│                                            │
│  输入: 交易计划 (标的/方向/价格/仓位/理由/情绪) │
│  处理: 数据拉取 → 指标计算 → 规则检查 → LLM审查│
│  输出: PASS / WARN / BLOCK + 修改建议        │
│                                            │
│  7 个 Python 文件，~1,800 行，14 条规则       │
└────────────────────────────────────────────┘
```

**分层逻辑**:

| 层 | 做什么 | 为什么这样分层 |
|----|--------|--------------|
| 数据+指标 | 拉行情、算指标、判市场状态 | 所有规则的输入基础 |
| 规则引擎 | 16 条确定性规则，逐条过 | 零成本、零延迟、100% 可复现 |
| LLM 审查 | 读规则结果+上下文，给最终意见 | 捕捉规则覆盖不到的语义层面 |
| 输出 | 终端报告+审计日志 | 可读、可追溯 |

**核心理念**:
- 确定性规则拦住 ~80% 的技术面硬伤（数学可验证的）
- LLM 捕捉剩余的 ~20%（需要语义理解的：理由是否自洽、是否有认知偏误）
- 系统给建议，用户做最终决策。BLOCK 级别的覆盖记录不可删除。

### 输入

```python
class TradingPlan:
    symbol: str              # 标的代码
    direction: Direction     # BUY / SELL
    entry_price: float       # 计划入场价
    position_pct: float      # 仓位比例 (总资金的 %)
    stop_loss: float | None  # 止损价
    take_profit: float | None# 止盈价
    reasoning: str           # 交易理由 (自由文本)
    emotion_self_rating: int # 情绪自评 1(冷静)-5(极度情绪化)
    planned_at: datetime     # 计划制定时间
```

### 输出

```python
class ReviewReport:
    audit_id: str            # 审计追踪 ID
    verdict: Verdict         # PASS / WARN / BLOCK
    overall_score: float     # 合理性总分 (1-10)
    rule_results: list[RuleResult]  # 每条规则的检查结果
    key_concerns: list[str]  # LLM 发现的主要问题 (来自 LLMReviewResult)
    suggestions: list[str]   # 修改建议
```

---

## 2. 架构

```
┌─────────────────┐
│  用户输入计划     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  config.py       │ ← 用户自定义阈值
│  (YAML 加载)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  input.py        │ 行情拉取 (yfinance/akshare) + CLI
│  数据获取 + CLI   │ 交互式输入 / 命令行参数
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  engine.py       │
│  ├ 指标计算       │ pandas-ta 封装 → IndicatorSnapshot
│  ├ 市场状态       │ ADX + ATR 分位数 → MarketRegime
│  └ 14 条规则      │ 每规则独立函数 → RuleResult[]
│                  │ 规则通过 storage.py 读取历史记录
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  storage.py      │ ← 审计记录读写 (JSONL)
│  历史记录查询     │   被 engine / reviewer / output 共同依赖
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  reviewer.py     │ LLM 审查 (Claude API)
│  语义判断         │ 规则校误 + 自洽性 + 重复犯错
│  综合评分         │ 内部调 storage 获取历史上下文
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  storage.py      │ save_audit() → 追加 JSONL
│  (写入审计记录)   │ (无论是否使用 LLM 都写入)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  output.py       │ 终端报告 (rich) + JSON 导出
│  格式化输出       │ 渲染结果
└────────┬────────┘
         │
         ▼
┌─────────────────┐   ┌──────────────────────┐
│  审查报告         │──►│ 用户修改计划 → 重审   │
│  PASS/WARN/BLOCK │   │ (增量执行，非全量)    │
└─────────────────┘   └──────────────────────┘

以上所有模块共享 schemas.py (Pydantic 数据模型)
```

**模块拆分逻辑**:
- `config.py` (~40行): 纯配置。YAML reader + 默认值合并。
- `input.py` (~250行): 数据获取 + CLI。合并了数据源和交互入口——两者都处理"外部输入"。
- `engine.py` (~600行): 计算核心。指标计算 + 市场状态 + 14 条规则。所有纯计算逻辑集中于此。
- `storage.py` (~80行): 审计记录读写。独立于 output.py 是因为 engine.py 的规则也需要读历史记录。
- `reviewer.py` (~300行): LLM 审查。独立是因为调用外部 API，有延迟和成本。
- `output.py` (~100行): 终端报告 + JSON 导出。只做渲染，不做持久化。
- `schemas.py` (~180行): 共享数据模型。

---

## 3. 模块设计

### config.py

**职责**: 加载用户配置，提供默认值合并。

```yaml
# config.yaml (用户可编辑)
user:
  market: "A"                  # "A" | "US" | "HK"
  account_size: 100000

thresholds:
  max_position_pct: 20
  max_total_position_pct: 80
  max_trades_per_day: 5
  cooldown_minutes: 30
  emotion_warn_threshold: 4

rules:
  disabled: []                 # 禁用的规则 ID
  block_to_warn: []            # 用户主动永久降级的规则 ID
  auto_downgrade_after: 3      # 连续覆盖 N 次 BLOCK 后自动降级 (0=禁用)
```

**工作量**: ~40 行

---

### input.py

**职责**: 行情数据获取 + CLI 交互。

**数据获取**:

| 市场 | 数据源（按优先级） | 备注 |
|------|-------------------|------|
| A 股 | akshare → Tushare | akshare 默认免费无 token；Tushare 可选（需 token，数据质量更高） |
| 美股 | yfinance | 免费，稳定 |
| 港股 | yfinance → akshare → Tushare | 多级 fallback，自动选择首个可用源 |

```python
# 统一接口
def fetch_market_data(symbol: str, market: str, lookback_days: int = 250) -> MarketData:
    """返回日线+周线 OHLCV。本地 SQLite 缓存避免重复请求。"""
```

**CLI 交互**:

```bash
# 交互式
$ trade-sentry check
  标的代码: 600036
  方向 (buy/sell): buy
  入场价: 38.50
  仓位比例 (%): 15
  当前持仓比例 (%) [回车跳过]: 45
  止损价 (回车跳过): 37.00
  交易理由: 银行股估值修复，日线MACD金叉
  情绪自评 (1-5): 2

# 命令行
$ trade-sentry check --symbol 600036 --direction buy --price 38.5 \\
    --position 15 --current-holdings 45 --stop 37.0 \\
    --reason "银行股估值修复" --emotion 2

# 跳过 LLM（快速自检）
$ trade-sentry check ... --no-llm

# 输出 JSON
$ trade-sentry check ... --json
```

> `--current-holdings` 为可选参数。跳过时 SZ02（总仓位超限）自动 PASS。
> 不自动从审计记录推算持仓——因为"已确认执行"不等于"仍持有"。

**数据源对比**:

| 维度 | akshare | Tushare | yfinance |
|------|---------|---------|----------|
| 覆盖市场 | A/港/US | A/港(需VIP) | US/港 |
| 费用 | 免费 | 免费(需注册获token) | 免费 |
| 数据质量 | 中等，偶有缺失 | 高，官方数据 | 高(US)、中(HK) |
| 稳定性 | 中等，接口可能变更 | 高，版本化管理 | 高 |
| 速率限制 | 有，较宽松 | 0.5s/次，积分制 | 有，较宽松 |
| 安装难度 | `pip install akshare` | `pip install tushare` + token | `pip install yfinance` |
| 适用场景 | 默认免费方案 | 需要高质量 A 股数据时 | US/HK 首选 |

**Fallback 策略**:

```
A 股: Tushare (如有 token) → akshare (默认)
      逻辑: 检查 TUSHARE_TOKEN 环境变量 → 有则优先 Tushare，失败降级 akshare
      
美股: yfinance → Tushare us_daily (如配置 token)
      逻辑: yfinance 已非常可靠，Tushare 作为备用

港股: yfinance → akshare → Tushare hk_daily (需 broker 权限，通常不可用)
      逻辑: yfinance 优先，失败尝试 akshare
```

> 数据源选择不影响上层逻辑。所有源统一输出 `MarketData` 格式（日线+周线 OHLCV），
> `engine.py` 不感知数据来源。

**工作量**: ~250 行（数据 100 + CLI 100 + 输入校验 50）

---

### engine.py

**职责**: 指标计算 + 市场状态分类 + 14 条规则执行。这是系统的**计算核心**。

**子模块 A — 指标计算** (~100行):

```python
def compute_indicators(market_data: MarketData) -> IndicatorSnapshot:
    """
    输入: MarketData (日线+周线 OHLCV)
    输出: IndicatorSnapshot (趋势/动量/波动率/成交量/支撑阻力/形态/多周期)
    
    核心依赖: pandas-ta (252 个指标) + 自写的支撑阻力/多周期判断
    """
```

**子模块 B — 市场状态** (~80行):

```python
def classify_regime(indicators: IndicatorSnapshot) -> MarketRegime:
    """
    双维度分类:
    
    维度一: 趋势方向
      ADX>25 + 均线多头 → trending_up
      ADX>25 + 均线空头 → trending_down
      ADX<20 → choppy
      ADX 20-25 → 沿用上一周期状态
    
    维度二: 波动率水平 (正交)
      用 ATR 分位数 / Bollinger Width (A股) 或 VIX (美股)
      >80分位 → extreme / >60分位 → elevated / 否则 → normal
    """
```

**子模块 C — 规则执行** (~470行):

```python
# 所有规则统一签名
# 需要历史的规则内部调用 storage.get_*() 函数
def rule_xxx(plan: TradingPlan, indicators: IndicatorSnapshot, 
             regime: MarketRegime) -> RuleResult:
    """返回 PASS/WARN/BLOCK + 详情 + 建议"""

# RuleEngine 编排器
class RuleEngine:
    def __init__(self, config: UserConfig):
        self.rules = [...]  # 按 config.rules.disabled 过滤
        self.config = config
    
    def check_all(self, plan, indicators, regime) -> list[RuleResult]:
        """顺序执行所有启用的规则，收集结果。不与 LLM 交互。"""
```

**工作量**: ~600 行（指标 100 + 状态 60 + 规则 16×~22 行=350 + 框架 90）

**关键算法指引**:

*支撑阻力位计算* (~50 行，属于指标计算部分):
```python
def find_support_resistance(df: pd.DataFrame) -> tuple[float | None, float | None]:
    """
    基于近期高低点 + 均线位置计算最近的有效支撑和阻力。

    算法:
    1. 找最近 60 日的最高点 → 阻力位候选
    2. 找最近 60 日的最低点 → 支撑位候选
    3. 检查阻力位是否有 ≥2 次触碰（高点距阻力位 <1%）→ 确认为有效阻力
    4. 检查支撑位是否有 ≥2 次触碰 → 确认为有效支撑
    5. 叠加 MA200 作为长期支撑/阻力参考
       - 若 MA200 在阻力候选 2% 以内 → 加强阻力信号
       - 若 MA200 在支撑候选 2% 以内 → 加强支撑信号
    6. 返回 (nearest_support, nearest_resistance)
       - 若无法确认，返回 None
    """
```

*多周期趋势判断* (~30 行，属于指标计算部分):
```python
def classify_trend(df: pd.DataFrame) -> str:
    """
    判断单一周期的趋势方向。返回 "up" / "down" / "sideways"。

    算法:
    1. 计算 MA20、MA50 斜率（最近 5 日线性回归斜率）
    2. 检查价格相对于 MA20 的位置
    3. 判定:
       - MA20 斜率 > 0 AND 价格 > MA20 → "up"
       - MA20 斜率 < 0 AND 价格 < MA20 → "down"
       - 否则 → "sideways"
    4. 对周线和日线分别调用此函数，结果存入 IndicatorSnapshot
    """
```

> 这两个算法不依赖 ML，纯数学计算。pandas-ta 提供 MA 值，斜率用 numpy.polyfit 计算。

---

### storage.py

**职责**: 审计记录的读写。独立于 output.py（渲染）和 engine.py（计算），因为两端都需要访问历史数据。

```python
def save_audit(report: ReviewReport) -> str:
    """追加一条审查记录到 JSONL 文件。返回 audit_id。"""

def load_recent_audits(n: int = 20, symbol: str | None = None) -> list[dict]:
    """读取最近 N 条审查记录。可按标的过滤。供 engine.py 规则使用。"""

def count_today_audits() -> int:
    """统计今日审查次数。供 F01 使用。"""

def get_position_average(n: int = 10) -> float | None:
    """计算近 N 笔已确认交易的仓位均值。供 SZ03/E01 使用。数据不足返回 None。"""

def get_emotion_trend(n: int = 3) -> list[int] | None:
    """获取近 N 次情绪自评序列。供 E02 使用。不足返回 None。"""
```

**存储格式** (JSONL):
```json
{"audit_id":"20260606-001","timestamp":"2026-06-06T10:30:00","symbol":"600036","direction":"buy","position_pct":15,"emotion":2,"verdict":"warn","overridden_rules":[],"user_action":"accepted"}
```
> `overridden_rules`: 用户覆盖的 BLOCK 规则 ID 列表。空数组表示接受审查结果。
> `auto_downgrade_after` 机制通过统计此字段实现——同一 rule_id 出现 N 次则自动降级。

**工作量**: ~80 行

---

### reviewer.py

**职责**: 汇总规则结果 + 上下文 → LLM 综合审查。

```python
def llm_review(
    plan: TradingPlan,
    regime: MarketRegime,
    rule_results: list[RuleResult],
) -> LLMReviewResult:
    """
    组装 prompt → 调用 Claude API → 解析返回。
    内部调用 storage.load_recent_audits() 获取历史交易上下文。
    市场环境信息由 regime.regime_description 提供，无需原始指标数据。
    
    LLM 审查的重点（规则引擎做不到的）:
    1. 规则校误: 是否有规则误报（PASS 应为 WARN / WARN 应为 PASS）？
    2. 语义判断: 交易理由是否自洽？是否存在认知偏误？
    3. 重复犯错: 本次理由与历史亏损交易的逻辑是否实质相同？（原 B01）
    4. 综合评分: 结合规则结果 + 市场环境 + 历史上下文
    """
```

**Prompt 结构**:
```
[系统指令] 你是严格的交易纪律审查官。你的任务是检查规则引擎的结果是否合理，
而非主动寻找额外问题。如果计划没有明显问题，就说没有。

[市场环境] {regime_description}

[规则检查结果] {rule_results_formatted}
（包含每条规则的判定、详情和建议）

[历史交易] {recent_trades_summary}

[本次交易计划] {plan}

请审查:
1. 规则引擎是否误报？上述规则结果中，你认为是否有 PASS 但实际该 WARN、
   或 WARN/BLOCK 但实际该 PASS 的情况？如有，请指出并说原因。
2. 用户的交易理由是否自洽？是否存在明显的认知偏误（确认偏误、过度自信、锚定效应等）？
3. 综合合理性评分 (1-10) + 如果评分 ≤5 请给出修改建议。
```

**工作量**: ~300 行（prompt 模板 80 + API 调用 80 + 解析 100 + 重试 40）

---

### output.py

**职责**: 格式化输出审查报告。持久化由 storage.py 负责。

```python
def render_terminal(report: ReviewReport) -> None:
    """rich 库彩色终端输出。PASS=绿 WARN=黄 BLOCK=红。每条规则一行。"""

def render_json(report: ReviewReport) -> str:
    """JSON 输出，供程序消费。"""
```

**工作量**: ~100 行

---

**终端输出示例**:
```
═══════════════════════════════════════════
 Trade Sentry 审查报告  #20260606-001
═══════════════════════════════════════════
 标的: 600036  方向: BUY  价格: 38.50
 仓位: 15%  止损: 37.00  (-3.9%)

 市场状态: 下跌趋势 + 正常波动

 规则检查:
 ✅ T01 逆势操作          通过
 ⚠️  T04 ADX 有效性         ADX=18，趋势信号可信度低
 ✅ M01 RSI 极端区         通过 (RSI=52)
 ✅ M02 RSI 极端区         通过
 ❌ S01 阻力位追高          距阻力位 1.2%，且多次测试未突破
 ✅ STOP01 止损计划        通过
 ...

 LLM 审查意见:
 合理性评分: 5/10
 主要关切:
   - 在下跌趋势中做多银行股，理由"估值修复"缺乏催化剂支撑
   - 阻力位附近入场，成功率低
 建议: 等待站上阻力位后再入场，或降低仓位至 5%

 结论: ⚠️ WARN (11 通过 / 3 警告 / 2 拦截)
═══════════════════════════════════════════
```

---

### schemas.py

**职责**: 所有 Pydantic 数据模型定义。

```python
class Direction(Enum): BUY = "buy"; SELL = "sell"
class Verdict(Enum): PASS = "pass"; WARN = "warn"; BLOCK = "block"

class TradingPlan(BaseModel): ...
class MarketData(BaseModel): ...
class IndicatorSnapshot(BaseModel): ...
class MarketRegime(BaseModel): ...
class RuleResult(BaseModel): ...
class LLMReviewResult(BaseModel): ...
class ReviewReport(BaseModel): ...
class AuditRecord(BaseModel): ...
```

**工作量**: ~180 行

---

## 4. 规则清单

共 16 条，按类别分组。每条规则独立函数，统一签名。

### A. 趋势环境 (2 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **T01** | 逆势操作 | 做多时 regime=trending_down：检查是否在支撑位附近（MA200/前低/布林下轨）且有反转K线。有支撑+反转 → WARN；无支撑无反转 → BLOCK。做空时对称。 | 条件 BLOCK/WARN | 否 |
| **T04** | ADX 弱趋势 | ADX<20 → WARN。无论用户理由怎么写，信号噪音大是客观事实。用户可自行判断忽略，但系统必须提示。 | WARN | 否 |

> T02/T03 (多周期一致性、均线排列) 已合并到 T01 的趋势判断逻辑中，不单独为规则。

### B. 超买超卖 (4 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **M01** | RSI 超买追涨 | RSI>80 且 direction=BUY | BLOCK | 否 |
| **M02** | RSI 超卖杀跌 | RSI<20 且 direction=SELL | BLOCK | 否 |
| **M03** | MACD 顶背离 | 近 20 日价格新高、MACD 柱不新高 → 买入触发 | BLOCK | 否 |
| **M04** | MACD 底背离 | 近 20 日价格新低、MACD 柱不新低 → 卖出触发 | BLOCK | 否 |

> M05 (随机指标) 与 M01/M02 高度重叠，已移除。

### C. 关键位 (1 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **S01** | 阻力/支撑位 | 买入价距阻力位 <2%。首次触碰 → WARN；多次测试未突破 → BLOCK。卖出时对称检查支撑位。"多次测试"通过审计记录中同标的的交易次数近似判断。**v1 简化**："突破回踩确认→PASS"逻辑未实现（需追踪 K 线级别的突破确认），当前距关键位 >2% 即 PASS。 | 条件 WARN/BLOCK | 是（多次测试判断） |

> S02-S04 (布林带) 与 S01 逻辑重叠，已合并到 S01 的支撑阻力综合计算中。

### D. 成交量 (1 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **V01** | 无量突破 | 价格突破关键位（S01 的阻力/支撑）但当日量 < 20日均量的 70% | WARN | 否 |

> V02/V03 (放量滞涨/放量不跌) 假阳性高，v1 不做。远期可通过 LLM 看图判断。

### E. 止损 (1 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **STOP01** | 无止损计划 | stop_loss 为空 | WARN | 否 |

### F. 交易频率 (2 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **F01** | 过度交易 | 当日计划提交数 > `max_trades_per_day`。需要最少 1 天的审计记录。 | BLOCK | 是（≥1天） |
| **F02** | 交易间隔过短 | ⚠️ v1 简化版：同一标的两次计划提交间隔 < `cooldown_minutes`。不依赖执行结果，仅看计划时间差。完整版需执行日志支持。 | WARN | 是（≥1笔） |

### G. 仓位 (3 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **SZ01** | 单笔仓位超限 | 计划仓位 > `max_position_pct`% | BLOCK | 否 |
| **SZ02** | 总仓位超限 | 需用户提供当前持仓比例（CLI 可选输入项）。跳过时 SZ02 自动 PASS。不自动从审计记录推算——因为"已确认执行"不等于"仍持有"。 | BLOCK | 是（用户输入） |
| **SZ03** | 仓位异常放大 | 本次仓位 > 近 10 笔均值的 1.5 倍。需 ≥ 10 条审计记录。 | WARN | 是（≥10笔） |

### H. 情绪 (2 条)

| ID | 规则名 | 逻辑 | 判定 | 需历史 |
|----|--------|------|------|--------|
| **E01** | 高情绪状态 | 分层判断：①情绪 ≥ `emotion_warn_threshold` → 至少 WARN（无历史时也触发）；②情绪 ≥ 阈值 且 仓位 > 近 10 笔均值的 1.2 倍 → 升级为 BLOCK。分层保证了首次使用也能捕获"情绪高涨时交易"的风险。 | WARN/BLOCK | 是（BLOCK 升级需 ≥10笔） |
| **E02** | 情绪趋势恶化 | 近 3 次情绪自评持续上升。需 ≥ 3 条记录。 | WARN | 是（≥3笔） |

### I. 行为模式 (0 条 / 已移至 LLM)

> 原 B01 (重复犯错)、B02 (确认偏误)、B03 (盈利后过度自信) 需要跨交易语义分析，移至 `reviewer.py` 的 LLM 审查中处理，不在确定性规则层。

---

**历史数据依赖汇总**:

| 最小记录数 | 激活的规则 | 累计 |
|-----------|-----------|------|
| 0 笔 | T01, T04, M01-M04, S01(简版), V01, STOP01, SZ01, E01(简版), F01 | 11 条 |
| 1 笔 | +F02 | 12 条 |
| 3 笔 | +E02 | 13 条 |
| 10 笔 | +SZ03, +E01(完整版), +S01(完整版) | 16 条 |

> F01 在首次使用时即工作（当日提交数从 0 开始累计，第 2 笔起可能触发 BLOCK）。
> 首次使用即有 11 条核心规则工作，覆盖所有**不依赖跨日历史**的技术面检查和基础纪律检查。

---

## 5. 数据流

```
用户输入 TradingPlan (+ current_holdings 可选)
    │
    ├── config.py     → 加载阈值
    ├── input.py      → 拉行情 → MarketData
    │                   (yfinance/akshare → SQLite 缓存)
    │
    ├── engine.py     → compute_indicators() → IndicatorSnapshot
    │                → classify_regime()     → MarketRegime
    │                → storage.load_*()      → 历史记录（供规则消费）
    │                → RuleEngine.check_all() → 16 个 RuleResult
    │                   (纯计算 + 存储读取，~200ms)
    │
    ├── reviewer.py   → llm_review(plan, regime, rule_results)
    │                   → LLMReviewResult
    │                   (调用 Claude API + storage 读取历史，~2-5s)
    │
    ├── storage.py    → save_audit()         → 追加 JSONL
    │
    └── output.py     → render_terminal()    → 终端报告
                     → render_json()        → JSON 导出
```

**调用顺序**: 严格串行。engine 必须在 reviewer 之前（LLM 需要规则结果作为输入）。

**可跳过**: `--no-llm` 标志跳过 reviewer.py。使用该标志时：仅执行 engine.py 规则检查 → storage.py 写入审计记录 → output.py 渲染报告。整体审查分数置为规则评分（非 LLM 评分）。

**首次使用**: 无历史交易记录时，依赖历史的规则自动 PASS（不足数据判断），不影响其他规则。审计记录始终写入，逐步积累数据。

---

## 6. 后处理闭环

```
审查报告
    │
    ├── PASS → 确认执行
    ├── WARN → 接受 / 修改计划重审 / 放弃
    └── BLOCK → 修改计划重审 / 强制覆盖(需确认+理由) / 放弃
```

**强制覆盖规则**:
- BLOCK 覆盖需两步确认 + 填写理由
- 覆盖记录永久写入审计日志（不可删除）
- 连续 `auto_downgrade_after` 次覆盖同一规则 → 自动降级为 WARN
- `block_to_warn` 列表中的规则永久降级（用户主动设置）

**重审逻辑**: 修改计划后增量重审——仅重跑受修改字段影响的规则。

**字段→规则映射表**（完整，供实现时参考）:

| 修改的字段 | 需重跑的规则 |
|-----------|------------|
| `symbol` | 全部规则（不同标的 = 全新审查） |
| `direction` | T01, S01 |
| `entry_price` | S01, M01, M02, M03, M04 |
| `position_pct` | SZ01, SZ02, SZ03, E01 |
| `stop_loss` | STOP01 |
| `take_profit` | (无规则依赖，仅 LLM 参考) |
| `reasoning` | (无规则依赖，仅 LLM 参考) |
| `emotion_self_rating` | E01, E02 |
| `current_holdings` | SZ02 |

> 若 M4/M5 结论发生变化 → 重新触发 M7 LLM 审查。若结论未变 → 复用上次 LLM 结果。

---

## 7. 引用清单：复用 vs 自建

### 7.1 直接引用 (pip install)

| 包 | 用途 | 所在文件 |
|----|------|---------|
| `pandas-ta-classic` | 252 个技术指标 | engine.py |
| `yfinance` | 美股数据 | input.py |
| `akshare` | A 股数据（默认） | input.py |
| `tushare` | A 股数据（可选，需 token） | input.py |
| `pydantic` | 数据模型 | schemas.py |
| `rich` | 终端美化 | output.py |
| `pyyaml` | 配置加载 | config.py |

### 7.2 架构借鉴 (不引用代码)

| 开源项目 | 借什么 | 对应部分 |
|---------|--------|---------|
| **Execution-Discipline-Agent** | 规则函数模式 + 合规评分公式 | engine.py 规则框架 |
| **OpenAlice** | Guards 管道模式 + "ESLint for Trading" 比喻 | engine.py 编排逻辑 |
| **swarm-trader** | 市场状态分类 + 11 条硬规则的 rule type 分类 | engine.py regime + 规则分类 |
| **daily_stock_analysis** | LLM 交易决策 prompt 结构 | reviewer.py prompt |
| **claude-trading-skills** | 四维度审查框架 | reviewer.py 审查维度 |
| **cc-trading** | 交易日志字段 schema | storage.py 审计记录格式 |

### 7.3 设计模式借鉴（Trutle 项目）

> 来源: `D:\workspace\Trutle` — AI 辅助的 A/港/美股基本面分析系统。

| 借鉴内容 | 说明 | 对 Trade Sentry 的价值 |
|---------|------|----------------------|
| **`validate_stock_code()`** | 多市场代码标准化逻辑：6xxxxx→.SH, 0xxxxx/3xxxxx→.SZ, 1-5位→.HK, 字母→.US | 可复用其逻辑，避免从零写代码验证 |
| **Tushare 数据源** | 通过 Tushare Pro API 获取 A 股 `daily`/`weekly` OHLCV 数据 | 作为 akshare 之外的补充/替代方案，数据质量更高（需 token） |
| **多源 fallback 链** | 港股: yfinance → Tushare hk_daily → akshare；美股: yfinance → Tushare us_daily | Trade Sentry 的 input.py 可采用类似的多级降级策略 |
| **`.env` 加载模式** | 纯标准库实现的 `.env` 读取，无需 python-dotenv | 管理 Tushare token 等可选 API 密钥 |
| **Rate limiting** | 0.5s 延迟装饰器 + 指数退避重试 | Tushare 有频率限制，需遵守 |

### 7.4 需要全新写的

| 文件 | 内容 | 行数 | 难度 |
|------|------|------|------|
| `config.py` | YAML 配置加载 + 默认值合并 | ~40 | ★☆☆ |
| `schemas.py` | 8 个 Pydantic 数据模型 | ~180 | ★☆☆ |
| `input.py` | 行情获取 (yfinance/akshare) + CLI + 缓存 | ~250 | ★★☆ |
| `engine.py` | 指标封装 + 市场状态 + 14 条规则 + 编排框架 | ~600 | ★★★ |
| `storage.py` | 审计记录 JSONL 读写 + 查询函数 | ~80 | ★☆☆ |
| `reviewer.py` | LLM prompt + API 调用 + 解析 + 重试 | ~300 | ★★☆ |
| `output.py` | 终端报告 (rich) + JSON 导出 | ~100 | ★☆☆ |
| `config.yaml` | 默认配置文件模板 | ~20 | ★☆☆ |
| **合计** | | **~1,800 行**（实际 1,845 行） | |

---

## 8. 错误处理策略

### 数据获取失败

```
Tushare token 已配置但 API 调用失败
  → 降级到 akshare
  → akshare 也失败 → 降级到 yfinance (仅限 HK/US)
  → 全部失败 → 中断审查，提示用户检查网络或稍后重试
  → 不返回不完整的审查结果（宁可不出报告，不出错误报告）

美股/HK: yfinance → Tushare → akshare
A 股: Tushare → akshare (没有 yfinance 这个 fallback)
```

| 失败场景 | 处理方式 | 用户提示 |
|---------|---------|---------|
| 所有数据源不可用 | 中断，非 0 退出 | "无法获取 {symbol} 的行情数据，请检查网络连接或稍后重试" |
| 数据不足 250 日 | 降低回溯天数要求，最少 60 日。少于 60 日中断。 | "数据仅覆盖 {n} 个交易日（需 ≥60），指标可能不可靠" |
| 缺失周线数据 | 从日线重采样生成周线 | 透明处理，不提示 |
| 缓存过期但网络不可用 | 使用过期缓存，标注数据时间 | "⚠️ 使用 {date} 的缓存数据" |

### LLM 调用失败

| 场景 | 处理 |
|------|------|
| Claude API 超时 (30s) | 重试 1 次，仍失败 → 跳过 LLM，仅输出规则结果。标注"LLM 审查不可用" |
| Token 耗尽 / 权限不足 | 跳过 LLM，提示用户检查 Claude 订阅状态 |
| LLM 返回格式异常 | 丢弃 LLM 结果，仅输出规则结果。记录原始响应用于调试 |

### 配置错误

| 场景 | 处理 |
|------|------|
| config.yaml 不存在 | 使用全部默认值，提示用户可创建配置文件 |
| config.yaml 格式错误 | 打印具体错误行，退出 |
| 规则 ID 拼写错误 (disabled/block_to_warn) | 打印警告，忽略无效 ID，继续运行 |

---

## 9. 开发阶段

### Phase 0：骨架 + 数据（1 天）
- [ ] `config.py` + `config.yaml`
- [ ] `schemas.py`：所有 Pydantic 模型
- [ ] `input.py`：yfinance + akshare 数据获取 + SQLite 缓存 + CLI 骨架
- [ ] `storage.py`：JSONL 读写 + 基础查询
- [ ] 验证：能成功拉取 A 股和美股日线数据

### Phase 1：指标 + 规则（2 天）
- [ ] `engine.py` A 部分：`compute_indicators()` 封装 pandas-ta
- [ ] `engine.py` B 部分：`classify_regime()` 双维度分类
- [ ] `engine.py` C 部分：14 条规则逐一实现 + `RuleEngine` 编排（含 storage 调用）
- [ ] 集成测试：输入一个 TradingPlan → 输出 16 条 RuleResult

### Phase 2：LLM + 输出（1.5 天）
- [ ] `reviewer.py`：prompt 设计 + Claude API + 解析
- [ ] `output.py`：终端报告 + JSON 导出
- [ ] `--no-llm` 离线模式
- [ ] 端到端测试：完整的一次审查流程

### Phase 3：打磨（1 天）
- [ ] CLI 交互式输入完善
- [ ] 真实场景测试（用自己近期的交易计划跑一遍）
- [ ] 规则阈值调整（基于实际使用反馈）
- [ ] 文档

**总计: ~5.5 天**

---

## 10. 远期 Roadmap

以下功能不进入 v1，在核心闭环稳定后再考虑：

| 功能 | 优先级 | 说明 |
|------|--------|------|
| **MCP 集成** (tradingview-mcp) | 低 | v1 的 yfinance/akshare 已足够。实时数据和高级筛选是锦上添花 |
| **影子分析器** (原 M6) | 中 | 需要 100+ 笔交易才有统计意义，v1 应聚焦单笔检查。作为独立子项目更合理 |
| **Web UI** (Streamlit) | 低 | CLI 是交易者最快的交互方式。Web UI 在需求明确后再做 |
| **K 线形态规则** (原 P01-P03) | 低 | 机械形态识别假阳性太高，不如 LLM 看图判断。等图像模型成熟后再考虑 |
| **放量异常规则** (原 V02-V03) | 低 | 假阳性高，v1 不做。远期可通过 LLM 看图判断 |
| **多角色辩论** (如 TradingAgents) | 中 | 有趣的思路，但成本和延迟增加显著。单 LLM 审查已能覆盖 80% 场景 |
| **移动端** | 低 | 命令行 + 终端即可 |
