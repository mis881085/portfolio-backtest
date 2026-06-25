# -*- coding: utf-8 -*-
"""
export_data.py — 為 HTML 回測介面匯出乾淨的 JSON 資料檔

用法：
    python export_data.py 0050.TW 6208.TW VT BNDW
    → 輸出 data_0050.TW.json, data_6208.TW.json, data_VT.json, data_BNDW.json
      以及 data_FX_USDTWD.json

台股（.TW/.TWO）會自動同時嘗試 yfinance 與 FinMind（無 token），
比較兩者歷史年跨度，選擇較早的來源輸出。

所有資料清洗邏輯與 retire_backtest.py 完全一致。
每個 ticker 只輸出自身市場的交易日（不插入其他市場的日期），
HTML 回測引擎自行對齊日期。
"""

import json
import argparse
import os
import time
from datetime import date
import requests
import yfinance as yf
import pandas as pd
import numpy as np

# ============================================================
# 常數（與 retire_backtest.py 保持一致）
# ============================================================
START = "2000-01-01"

MANUAL_DIVS: dict[str, dict[str, float]] = {
    "0050.TW": {
        "2009-10-23": 0.25,    # 原始 1.00 ÷ 4
        "2010-10-25": 0.55,    # 原始 2.20 ÷ 4
        "2011-10-26": 0.4875,  # 原始 1.95 ÷ 4
        "2012-10-24": 0.4625,  # 原始 1.85 ÷ 4
    },
}

# FinMind（無 token）每次請求後等待秒數，避免被限流
FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
FINMIND_DELAY = 3.5
# ============================================================


def _is_tw(ticker: str) -> bool:
    return ticker.endswith(".TW") or ticker.endswith(".TWO")


def _despike(s: pd.Series, thresh) -> pd.Series:
    r = s.pct_change()
    if not np.isscalar(thresh):
        thresh = thresh.reindex(s.index)
    bad = (r.abs() > thresh) & (r.shift(-1).abs() > thresh * 0.8) & (r * r.shift(-1) < 0)
    return s.mask(bad).ffill()


def _fix_split_stitch(close: pd.Series, div: pd.Series, name: str) -> tuple[pd.Series, pd.Series]:
    r = close.pct_change()
    for b in r[(r < -0.5) | (r > 1.0)].index:
        factor = close[b] / close[close.index < b].iloc[-1]
        close = close.where(close.index >= b, close * factor)
        div   = div.where(div.index >= b, div * factor)
        print(f"[修補] {name} {b:%Y-%m-%d} 偵測到 ×{factor:.4f} 價格斷層，已將斷層前縮放（含股息）")
    return close, div


def _fix_div_scale(div: pd.Series, close: pd.Series, name: str) -> pd.Series:
    # 門檻 15%：涵蓋台灣金融股高殖利率（8～12%），仍可攔截資料來源錯誤（>15% 屬異常）
    div = div.copy()
    for d in div[div > 0].index:
        y = div[d] / close[d]
        if y > 0.15:
            for ratio in [2, 3, 4, 5, 10]:
                if div[d] / ratio / close[d] <= 0.15:
                    print(f"[修補] {name} {d:%Y-%m-%d} 配息殖利率 {y:.1%} 異常 → ÷{ratio} 校正為 {div[d]/ratio:.3f}")
                    div[d] /= ratio
                    break
    return div


def _apply_manual_divs(tkr: str, d: pd.Series, close: pd.Series) -> pd.Series:
    """補上 MANUAL_DIVS 中記錄的缺漏配息。"""
    if tkr not in MANUAL_DIVS:
        return d
    for ds, amt in MANUAL_DIVS[tkr].items():
        ts = pd.Timestamp(ds)
        if ts < close.index[0] or ts > close.index[-1]:
            continue
        nearest = close.index[close.index.searchsorted(ts)]
        if d.at[nearest] == 0:
            d.at[nearest] = amt
            print(f"[修補] {tkr} {nearest:%Y-%m-%d} 補上缺漏配息 {amt:.2f}")
    return d


def _write_json(tkr: str, currency: str, close: pd.Series, d: pd.Series, out_dir: str) -> None:
    """將清洗後的 close / div series 輸出為標準 JSON 格式。"""
    records = [
        {
            "date":  dt.strftime("%Y-%m-%d"),
            "close": round(float(close[dt]), 6),
            "div":   round(float(d[dt]), 6),
        }
        for dt in close.index
        if not pd.isna(close[dt])
    ]
    payload = {
        "ticker":    tkr,
        "currency":  currency,
        "generated": date.today().isoformat(),
        "data":      records,
    }
    safe_name = tkr.replace("/", "_")
    fname = f"{out_dir}/data_{safe_name}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[輸出] {fname}  （{len(records)} 筆，{records[0]['date']} ~ {records[-1]['date']}）")


# ============================================================
# yfinance 路徑
# ============================================================
def export_ticker(tkr: str, raw: pd.DataFrame, out_dir: str) -> None:
    """清洗單一美股 ticker（yfinance raw DataFrame）並輸出 JSON。"""
    idx   = raw.index
    close = raw["Close"].copy()
    d     = raw["Dividends"].fillna(0).copy()
    close = _despike(close, 0.15)
    _write_json(tkr, "USD", close, d, out_dir)


def _fetch_yfinance_tw(tkr: str, start: str) -> tuple[pd.Series, pd.Series] | None:
    """yfinance 路徑：下載並清洗台股資料，回傳 (close, div)；失敗回傳 None。"""
    try:
        print(f"  [yfinance] 下載 {tkr} ...")
        h = yf.Ticker(tkr).history(start=start, auto_adjust=False, actions=True)
        if h.empty:
            return None
        h.index = h.index.tz_localize(None)
        first = h["Close"].first_valid_index()
        if first is None:
            return None
        if first > pd.Timestamp(start):
            print(f"  [yfinance 警告] {tkr} 最早資料為 {first:%Y-%m-%d}")
        idx   = h.index
        close = h["Close"].copy()
        d     = h["Dividends"].fillna(0).copy()
        thresh = pd.Series(np.where(idx < pd.Timestamp("2015-06-01"), 0.075, 0.11), index=idx)
        close, d = _fix_split_stitch(close, d, tkr)
        close = _despike(close, thresh)
        d = _apply_manual_divs(tkr, d, close)
        d = _fix_div_scale(d, close, tkr)
        return close, d
    except Exception as e:
        print(f"  [yfinance 失敗] {tkr}: {e}")
        return None


# ============================================================
# FinMind 路徑（台股專用）
# ============================================================
def _finmind_req(dataset: str, data_id: str, start: str) -> list:
    """FinMind API 單次請求，附帶限速延遲。"""
    try:
        resp = requests.get(
            FINMIND_URL,
            params={"dataset": dataset, "data_id": data_id, "start_date": start},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"FinMind 網路錯誤 [{dataset}/{data_id}]: {e}") from e

    if body.get("status") != 200:
        raise RuntimeError(f"FinMind API 錯誤 [{dataset}/{data_id}]: {body.get('msg')}")

    time.sleep(FINMIND_DELAY)   # 避免高速頻繁請求被封鎖
    return body.get("data", [])


def _fetch_finmind_tw(tkr: str, start: str) -> tuple[pd.Series, pd.Series] | None:
    """
    FinMind 路徑：下載並清洗台股資料，回傳 (close, div)；失敗回傳 None。

    資料來源：
      - TaiwanStockPrice    → 日線收盤價（未復權）
      - TaiwanStockDividend → 股利政策
          · CashEarningsDistribution + CashExDividendTradingDate   → 現金股利（配息）
          · StockEarningsDistribution + StockExDividendTradingDate  → 股票股利（配股）
            配股換算為等值現金（配股比例 × 除權日收盤價）加入 div，
            確保 TRI = (close + div) / prevClose 正確還原除權前報酬。
    """
    stock_id = tkr.split(".")[0]
    try:
        # ── 1. 日線 ──────────────────────────────────────────
        print(f"  [FinMind] 下載 {tkr} 日線 ...")
        price_rows = _finmind_req("TaiwanStockPrice", stock_id, start)
        if not price_rows:
            print(f"  [FinMind 警告] {tkr} 無日線資料")
            return None

        df = pd.DataFrame(price_rows)[["date", "close"]].copy()
        df["date"]  = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.set_index("date").sort_index()
        close = df["close"].astype(float)

        first_date = close.index[0]
        if first_date > pd.Timestamp(start):
            print(f"  [FinMind 警告] {tkr} 最早資料為 {first_date:%Y-%m-%d}")

        # ── 2. 配息（現金）───────────────────────────────────────
        print(f"  [FinMind] 下載 {tkr} 股利 ...")
        div_rows = _finmind_req("TaiwanStockDividend", stock_id, start)

        cash_d = pd.Series(0.0, index=close.index)
        trading_dates = close.index
        stock_events: list[tuple[pd.Timestamp, float]] = []  # (除權日, ratio)

        for row in div_rows:
            # 現金股利（配息）
            cash = float(row.get("CashEarningsDistribution") or 0)
            cash_ex = (row.get("CashExDividendTradingDate") or "").strip()
            if cash > 0 and cash_ex:
                div_ts = pd.Timestamp(cash_ex)
                if div_ts in cash_d.index:
                    cash_d[div_ts] += cash
                else:
                    loc = trading_dates.searchsorted(div_ts)
                    if loc < len(trading_dates):
                        tgt = trading_dates[loc]
                        cash_d[tgt] += cash
                        print(f"  [修補] {tkr} 除息日 {div_ts.date()} 非交易日 → 移至 {tgt.date()}")

            # 股票股利（配股）：先記錄 (ex_date, ratio)，等 close 調整後再換算
            stock = float(row.get("StockEarningsDistribution") or 0)
            stock_ex = (row.get("StockExDividendTradingDate") or "").strip()
            if stock > 0 and stock_ex:
                stock_events.append((pd.Timestamp(stock_ex), stock / 10))

        # ── 3. 清洗 pipeline（僅針對現金股利）────────────────────
        thresh = pd.Series(
            np.where(close.index < pd.Timestamp("2015-06-01"), 0.075, 0.11),
            index=close.index,
        )
        close, cash_d = _fix_split_stitch(close, cash_d, tkr)
        close = _despike(close, thresh)
        cash_d = _apply_manual_divs(tkr, cash_d, close)
        cash_d = _fix_div_scale(cash_d, close, tkr)

        # ── 4. 配股 → 等值現金（用已調整的 close 計算，不受 _fix_div_scale 影響）──
        stock_d = pd.Series(0.0, index=close.index)
        for ex_ts, ratio in stock_events:
            if ex_ts in close.index:
                tgt = ex_ts
            else:
                loc = trading_dates.searchsorted(ex_ts)
                if loc >= len(trading_dates):
                    continue
                tgt = trading_dates[loc]
                print(f"  [修補] {tkr} 除權日 {ex_ts.date()} 非交易日 → 移至 {tgt.date()}")
            cash_equiv = ratio * float(close[tgt])
            stock_d[tgt] += cash_equiv
            print(f"  [配股] {tkr} {tgt:%Y-%m-%d} {ratio*10:.2f} 元/股 → 等值 {cash_equiv:.4f}")

        return close, cash_d + stock_d
    except Exception as e:
        print(f"  [FinMind 失敗] {tkr}: {e}")
        return None


def export_ticker_tw(tkr: str, start: str, out_dir: str) -> None:
    """台股自動選源：同時嘗試 yfinance 與 FinMind，選歷史年跨度較長者輸出。"""
    yf_result = _fetch_yfinance_tw(tkr, start)
    fm_result = _fetch_finmind_tw(tkr, start)

    if yf_result is None and fm_result is None:
        raise RuntimeError(f"[錯誤] {tkr} yfinance 與 FinMind 均無法取得資料，請確認代號是否正確")

    if yf_result is None:
        close, d = fm_result
        print(f"  → yfinance 無資料，採用 FinMind（起始 {close.index[0]:%Y-%m-%d}）")
    elif fm_result is None:
        close, d = yf_result
        print(f"  → FinMind 無資料，採用 yfinance（起始 {close.index[0]:%Y-%m-%d}）")
    else:
        yf_close, yf_d = yf_result
        fm_close, fm_d = fm_result
        if fm_close.index[0] < yf_close.index[0]:
            close, d = fm_close, fm_d
            print(f"  → FinMind 較早（{fm_close.index[0]:%Y-%m-%d}）vs yfinance（{yf_close.index[0]:%Y-%m-%d}），採用 FinMind")
        else:
            close, d = yf_close, yf_d
            print(f"  → yfinance 較早或相同（{yf_close.index[0]:%Y-%m-%d}）vs FinMind（{fm_close.index[0]:%Y-%m-%d}），採用 yfinance")

    _write_json(tkr, "TWD", close, d, out_dir)


# ============================================================
# 匯率
# ============================================================
def export_fx(out_dir: str) -> None:
    """匯出 USD/TWD 匯率 JSON"""
    print("  下載 USD/TWD 匯率 ...")
    raw = yf.Ticker("TWD=X").history(start=START)
    raw.index = raw.index.tz_localize(None)

    close = _despike(raw["Close"].dropna(), 0.05)
    records = [
        {"date": dt.strftime("%Y-%m-%d"), "rate": round(float(close[dt]), 6)}
        for dt in close.index
    ]

    payload = {
        "pair":      "USD_TWD",
        "generated": date.today().isoformat(),
        "data":      records,
    }

    fname = f"{out_dir}/data_FX_USDTWD.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[輸出] {fname}  （{len(records)} 筆，{records[0]['date']} ~ {records[-1]['date']}）")


# ============================================================
# 主程式
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="匯出資料為 JSON（供 HTML 回測介面使用）",
        epilog=(
            "範例：\n"
            "  python export_data.py 0050.TW 006208.TW VT BNDW\n"
            "  python export_data.py 0050.TW --start 2000-01-01 --out data\n"
            "\n"
            "台股（.TW/.TWO）會自動同時嘗試 yfinance 與 FinMind，選歷史較長者輸出。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tickers", nargs="+", help="標的代號，例如：0050.TW VT BNDW")
    parser.add_argument("--out",   default=".", help="輸出目錄（預設為當前目錄）")
    parser.add_argument("--start", default=None, help="資料起始日期 YYYY-MM-DD（預設：2000-01-01）")
    args = parser.parse_args()

    if args.start:
        global START
        START = args.start

    tickers = list(dict.fromkeys(args.tickers))   # 去重保序
    os.makedirs(args.out, exist_ok=True)

    print(f"下載標的：{', '.join(tickers)}，起始日期：{START}\n")

    for tkr in tickers:
        if _is_tw(tkr):
            export_ticker_tw(tkr, START, args.out)
        else:
            print(f"  下載 {tkr} ...")
            h = yf.Ticker(tkr).history(start=START, auto_adjust=False, actions=True)
            if h.empty:
                raise RuntimeError(f"[錯誤] {tkr} 無法取得資料，請確認 ticker 是否正確")
            h.index = h.index.tz_localize(None)
            first = h["Close"].first_valid_index()
            if first and first > pd.Timestamp(START):
                print(f"  [警告] {tkr} 最早資料為 {first:%Y-%m-%d}")
            export_ticker(tkr, h, args.out)

    print()
    export_fx(args.out)
    print("\n全部完成！")


if __name__ == "__main__":
    main()
