"""Trade Sentry — 数据获取。

多市场行情拉取 + SQLite 缓存 + 股票代码标准化。
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Bypass system proxy for domestic Chinese finance APIs
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

# 加载 .env（Tushare token 等）
def _load_env() -> None:
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
        break

_load_env()

import pandas as pd
import yfinance as yf

from trade_sentry.config import get_config, Config
from trade_sentry.schemas import MarketData


# ── 代码标准化 ────────────────────────────────────────

def validate_stock_code(code: str) -> tuple[str, str]:
    """标准化股票代码。返回 (ts_code, market)。

    借鉴自 Trutle 项目的 validate_stock_code()。
    """
    code = code.strip().upper()

    # 已是 Tushare 格式
    if re.match(r"^\d{6}\.(SH|SZ)$", code):
        return code, "A"
    m = re.match(r"^(\d{1,5})\.HK$", code)
    if m:
        return f"{m.group(1).zfill(5)}.HK", "HK"

    # 纯数字 A 股
    if re.match(r"^\d{6}$", code):
        if code.startswith("6"):
            return f"{code}.SH", "A"
        elif code.startswith(("0", "3")):
            return f"{code}.SZ", "A"
        elif code.startswith(("15", "16", "18")):       # 深圳 ETF
            return f"{code}.SZ", "A"
        elif code.startswith(("51", "56", "58", "50")): # 上海 ETF
            return f"{code}.SH", "A"
        raise ValueError(f"无法识别的 A 股代码前缀: {code}。支持: 6(SH), 0/3(SZ), ETF:15xx/51xx等")

    # 纯数字港股
    if re.match(r"^\d{1,5}$", code):
        return f"{code.zfill(5)}.HK", "HK"

    # 美股
    if re.match(r"^[A-Z]{1,5}\.US$", code):
        return code, "US"
    if re.match(r"^[A-Z]{1,5}$", code):
        return f"{code}.US", "US"

    raise ValueError(f"无法识别的股票代码: '{code}'")


def to_yfinance_symbol(ts_code: str) -> str:
    """Tushare 格式转 yfinance 格式。"""
    parts = ts_code.split(".")
    if len(parts) == 2:
        num, market = parts
        if market == "HK":
            # 去掉前导零: 00700 → 0700
            stripped = num.lstrip("0") or "0"
            return f"{stripped}.HK"
        if market == "SH":
            return f"{num}.SS"
        if market == "SZ":
            return f"{num}.SZ"
    return ts_code


# ── 缓存 ──────────────────────────────────────────────

def _cache_path() -> Path:
    return Path(os.environ.get("TRADE_SENTRY_DATA_DIR", "data")) / "cache.db"


def _get_cached(code: str, max_age_hours: int = 24) -> Optional[dict]:
    """从 SQLite 缓存读取行情数据。"""
    db_path = _cache_path()
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT daily_json, weekly_json, fetched_at, data_source "
            "FROM market_cache WHERE code = ?", (code,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row[2])
        if (datetime.now() - fetched_at).total_seconds() > max_age_hours * 3600:
            return None
        return {
            "daily": json.loads(row[0]),
            "weekly": json.loads(row[1]),
            "fetched_at": row[2],
            "data_source": row[3],
        }
    finally:
        conn.close()


def _set_cache(code: str, daily: list[dict], weekly: list[dict],
               source: str) -> None:
    """写入 SQLite 缓存。"""
    db_path = _cache_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS market_cache "
            "(code TEXT PRIMARY KEY, daily_json TEXT, weekly_json TEXT, "
            "fetched_at TEXT, data_source TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO market_cache VALUES (?, ?, ?, ?, ?)",
            (code, json.dumps(daily, default=str),
             json.dumps(weekly, default=str),
             datetime.now().isoformat(), source)
        )
        conn.commit()
    finally:
        conn.close()


# ── 数据获取 ──────────────────────────────────────────

def _fetch_yfinance(symbol: str, lookback_days: int = 250) -> Optional[MarketData]:
    """通过 yfinance 拉取数据。"""
    try:
        ticker = yf.Ticker(to_yfinance_symbol(symbol))
        end = datetime.now()
        start = end - timedelta(days=lookback_days + 30)

        daily_df = ticker.history(start=start, end=end, interval="1d")
        weekly_df = ticker.history(start=start, end=end, interval="1wk")

        if daily_df.empty:
            return None

        def to_dicts(df):
            df = df.reset_index()
            df.columns = [c.lower().replace(" ", "_") for c in df.columns]
            if "date" in df.columns:
                df["date"] = df["date"].dt.strftime("%Y-%m-%d")
            return df.to_dict(orient="records")

        return MarketData(
            symbol=symbol, daily=to_dicts(daily_df), weekly=to_dicts(weekly_df),
            fetched_at=datetime.now(), data_source="yfinance"
        )
    except Exception:
        return None


def _fetch_akshare(symbol: str, market: str, lookback_days: int = 250) -> Optional[MarketData]:
    """通过 akshare 拉取 A 股数据。

    优先级: stock_zh_a_daily (新浪/腾讯源) → stock_zh_a_hist (东方财富源)
    因为部分网络环境 eastmoney API 不可达。
    """
    try:
        import akshare as ak
        code = symbol.split(".")[0]
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=lookback_days + 30)).strftime("%Y%m%d")

        df = None
        if market == "A":
            # Method 1: stock_zh_a_daily (uses Sina/Tencent source, more reliable)
            prefix = "sh" if symbol.endswith(".SH") else "sz"
            try:
                df = ak.stock_zh_a_daily(
                    symbol=f"{prefix}{code}", start_date=start_date,
                    end_date=end_date, adjust="qfq"
                )
            except Exception:
                pass

            # Method 2: stock_zh_a_hist (eastmoney source, may be blocked)
            if df is None or df.empty:
                try:
                    df = ak.stock_zh_a_hist(
                        symbol=code, period="daily",
                        start_date=start_date, end_date=end_date, adjust="qfq"
                    )
                except Exception:
                    pass
        else:
            # 港股：用 stock_hk_daily（新浪源，不受 eastmoney 拦截影响）
            try:
                df = ak.stock_hk_daily(symbol=code, adjust="qfq")
                if df is not None and not df.empty:
                    from datetime import date
                    s = date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:]))
                    e = date(int(end_date[:4]), int(end_date[4:6]), int(end_date[6:]))
                    df["date"] = pd.to_datetime(df["date"]).dt.date
                    df = df[(df["date"] >= s) & (df["date"] <= e)]
            except Exception:
                # fallback: stock_hk_hist (eastmoney 源，可能被拦截)
                df = ak.stock_hk_hist(symbol=code, period="daily",
                                      start_date=start_date, end_date=end_date,
                                      adjust="qfq")

        if df is None or df.empty:
            return None

        # 标准化列名
        col_map = {"日期": "date", "开盘": "open", "最高": "high",
                   "最低": "low", "收盘": "close", "成交量": "volume"}
        df = df.rename(columns=col_map)
        daily_dicts = df.to_dict(orient="records")

        # 从日线重采样生成周线
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        weekly = df.resample("W").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()
        weekly = weekly.reset_index()
        weekly["date"] = weekly["date"].dt.strftime("%Y-%m-%d")
        weekly_dicts = weekly.to_dict(orient="records")

        return MarketData(symbol=symbol, daily=daily_dicts, weekly=weekly_dicts,
                          fetched_at=datetime.now(), data_source="akshare")
    except Exception:
        return None


def _fetch_tushare(symbol: str, market: str, lookback_days: int = 250,
                   as_of: Optional[str] = None) -> Optional[MarketData]:
    """通过 Tushare 拉取 A 股数据（需 token）。

    as_of: 指定截止日期 "YYYYMMDD"——回测时用，None 表示到今天。
    """
    try:
        import tushare as ts
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            return None
        ts.set_token(token)
        pro = ts.pro_api(timeout=30)

        end_date = as_of if as_of else datetime.now().strftime("%Y%m%d")
        end_dt = datetime.strptime(end_date, "%Y%m%d")
        start_dt = end_dt - timedelta(days=lookback_days + 30)
        start_date = start_dt.strftime("%Y%m%d")

        is_etf = any(symbol.split(".")[0].startswith(p) for p in ["15","16","18","50","51","56","58"])
        if is_etf:
            daily_df = pro.fund_daily(ts_code=symbol, start_date=start_date, end_date=end_date,
                                      fields="ts_code,trade_date,open,high,low,close,vol")
            # ETF 无周线接口，从日线重采样
            weekly_df = None
        else:
            daily_df = pro.daily(ts_code=symbol, start_date=start_date, end_date=end_date,
                                 fields="ts_code,trade_date,open,high,low,close,vol,amount")
            time.sleep(0.5)
            weekly_start = (end_dt - timedelta(days=1825)).strftime("%Y%m%d")
            weekly_df = pro.weekly(ts_code=symbol, start_date=weekly_start, end_date=end_date,
                                   fields="ts_code,trade_date,open,high,low,close,vol,amount")

        if daily_df.empty:
            return None

        def to_dicts(df):
            df = df.rename(columns={"trade_date": "date", "vol": "volume"})
            if "amount" in df.columns:
                df = df.drop(columns=["amount", "ts_code"], errors="ignore")
            return df.to_dict(orient="records")

        daily_dicts = to_dicts(daily_df)

        if weekly_df is None:
            # ETF 无周线接口，从日线重采样
            import pandas as _pd
            df_daily = _pd.DataFrame(daily_dicts)
            df_daily["date"] = _pd.to_datetime(df_daily["date"])
            df_daily = df_daily.set_index("date").sort_index()
            w = df_daily.resample("W").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna().reset_index()
            w["date"] = w["date"].dt.strftime("%Y-%m-%d")
            weekly_dicts = w.to_dict(orient="records")
        else:
            weekly_dicts = to_dicts(weekly_df)

        return MarketData(symbol=symbol, daily=daily_dicts, weekly=weekly_dicts,
                          fetched_at=datetime.now(), data_source="tushare")
    except Exception:
        return None


def get_stock_name(raw_code: str) -> str:
    """获取股票/ETF 名称。Tushare(A股)→yfinance(港美股) fallback。"""
    ts_code, market = validate_stock_code(raw_code)

    # A股：Tushare
    if market == "A":
        try:
            import tushare as ts
            token = os.environ.get("TUSHARE_TOKEN", "")
            if token:
                ts.set_token(token)
                pro = ts.pro_api(timeout=10)
                code_num = ts_code.split(".")[0]
                if any(code_num.startswith(p) for p in ["15","16","18","50","51","56","58"]):
                    df = pro.fund_basic(ts_code=ts_code, fields="ts_code,name")
                else:
                    df = pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
                if not df.empty:
                    return str(df.iloc[0]["name"])
        except Exception:
            pass

    # 港股：akshare
    if market == "HK":
        try:
            import akshare as ak
            code_num = ts_code.split(".")[0]
            df = ak.stock_hk_company_profile_em(symbol=code_num)
            if not df.empty:
                name = str(df.iloc[0].get("公司名称", ""))
                if name:
                    return name
        except Exception:
            pass

    return ""


# ── 统一入口 ──────────────────────────────────────────

def fetch_market_data(raw_code: str, cfg: Optional[Config] = None,
                      lookback_days: int = 250,
                      as_of: Optional[str] = None) -> MarketData:
    """获取股票行情数据（带缓存和 fallback）。

    Args:
        raw_code: 用户输入的股票代码（任意格式）
        cfg: 配置对象（可选）
        lookback_days: 回溯天数
        as_of: 指定日期 "YYYY-MM-DD"——数据只取到该日为止。
               用于回测，跳过缓存。None 表示取最新数据。

    Returns:
        MarketData 对象

    Raises:
        RuntimeError: 所有数据源均不可用
    """
    if cfg is None:
        cfg = get_config()

    ts_code, market = validate_stock_code(raw_code)

    # 回测模式：跳过缓存，数据截断到指定日期
    if as_of:
        result = _fetch_for_date(ts_code, market, as_of, lookback_days)
        if result is None:
            raise RuntimeError(
                f"无法获取 {raw_code} 在 {as_of} 的历史行情数据。"
                f"请检查日期是否在交易日内。"
            )
        return result

    # 实时模式：检查缓存
    cache_ttl = cfg.data("cache_days") or 1
    cached = _get_cached(ts_code, max_age_hours=cache_ttl * 24)
    if cached:
        return MarketData(
            symbol=ts_code, daily=cached["daily"], weekly=cached["weekly"],
            fetched_at=datetime.fromisoformat(cached["fetched_at"]),
            data_source=cached["data_source"] + " (cached)"
        )

    result = _fetch_live(ts_code, market, lookback_days)
    if result is None:
        raise RuntimeError(
            f"无法获取 {raw_code} ({ts_code}) 的行情数据。"
            f"请检查网络连接或股票代码是否正确。"
        )

    # 写入缓存
    _set_cache(ts_code, result.daily, result.weekly, result.data_source)
    return result


def _fetch_live(ts_code: str, market: str, lookback_days: int) -> Optional[MarketData]:
    """实时数据获取（按市场 fallback）。"""
    if market == "A":
        result = _fetch_akshare(ts_code, market, lookback_days)
        if result is None:
            result = _fetch_tushare(ts_code, market, lookback_days)
    elif market == "US":
        result = _fetch_yfinance(ts_code, lookback_days)
        if result is None:
            result = _fetch_tushare(ts_code, market, lookback_days)
    elif market == "HK":
        result = _fetch_yfinance(ts_code, lookback_days)
        if result is None:
            result = _fetch_akshare(ts_code, market, lookback_days)
        if result is None:
            result = _fetch_tushare(ts_code, market, lookback_days)
    else:
        result = None
    return result


def _fetch_for_date(ts_code: str, market: str, as_of: str,
                    lookback_days: int) -> Optional[MarketData]:
    """获取指定日期之前的历史数据（用于回测）。

    A股优先用 Tushare（覆盖10年），港股美股优先用 akshare/yfinance。
    数据截断到 as_of，防止未来信息泄露。
    """
    from datetime import datetime as dt
    as_of_dt = dt.strptime(as_of, "%Y-%m-%d")

    def _date_str(b: dict) -> str:
        d = str(b.get("date", ""))[:10]
        return d.replace("-", "")  # 统一为 YYYYMMDD 格式

    def truncate(bars: list[dict]) -> list[dict]:
        cutoff = as_of.replace("-", "")
        return [b for b in bars if _date_str(b) <= cutoff]

    # A股：优先 Tushare（十年数据，以 as_of 为截止），失败降级 akshare
    if market == "A":
        tushare_as_of = as_of.replace("-", "")
        result = _fetch_tushare(ts_code, market, lookback_days + 30, as_of=tushare_as_of)
        if result is not None:
            daily = truncate(result.daily)
            if len(daily) >= 60 and _date_str(daily[0]) <= as_of.replace("-", ""):
                return MarketData(
                    symbol=ts_code, daily=daily, weekly=truncate(result.weekly),
                    fetched_at=dt.now(),
                    data_source=f"{result.data_source} (as_of={as_of})"
                )
        # Tushare 不可用 → 降级 akshare
        result = _fetch_akshare(ts_code, market, lookback_days + 30)
    else:
        result = _fetch_live(ts_code, market, lookback_days + 30)

    if result is None:
        return None

    daily = truncate(result.daily)
    weekly = truncate(result.weekly)

    if len(daily) < 60:
        return None

    return MarketData(
        symbol=ts_code, daily=daily, weekly=weekly,
        fetched_at=dt.now(), data_source=f"{result.data_source} (as_of={as_of})"
    )
