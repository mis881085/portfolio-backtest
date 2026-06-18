# -*- coding: utf-8 -*-
"""
export_data.py — 為 HTML 回測介面匯出乾淨的 JSON 資料檔

用法：
    python export_data.py 0050.TW 6208.TW VT BNDW
    → 輸出 data_0050.TW.json, data_6208.TW.json, data_VT.json, data_BNDW.json
      以及 data_FX_USDTWD.json

所有資料清洗邏輯與 retire_backtest.py 完全一致。
每個 ticker 只輸出自身市場的交易日（不插入其他市場的日期），
HTML 回測引擎自行對齊日期。
"""

import json
import argparse
import os
from datetime import date
import yfinance as yf
import pandas as pd
import numpy as np

# ============================================================
# 常數（與 retire_backtest.py 保持一致）
# ============================================================
START = "2009-01-01"

MANUAL_DIVS: dict[str, dict[str, float]] = {
    "0050.TW": {
        "2009-10-23": 0.25,    # 原始 1.00 ÷ 4
        "2010-10-25": 0.55,    # 原始 2.20 ÷ 4
        "2011-10-26": 0.4875,  # 原始 1.95 ÷ 4
        "2012-10-24": 0.4625,  # 原始 1.85 ÷ 4
    },
}
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
    div = div.copy()
    for d in div[div > 0].index:
        y = div[d] / close[d]
        if y > 0.06:
            for ratio in [2, 3, 4, 5, 10]:
                if div[d] / ratio / close[d] <= 0.06:
                    print(f"[修補] {name} {d:%Y-%m-%d} 配息殖利率 {y:.1%} 異常 → ÷{ratio} 校正為 {div[d]/ratio:.3f}")
                    div[d] /= ratio
                    break
    return div


def export_ticker(tkr: str, raw: pd.DataFrame, out_dir: str) -> None:
    """清洗單一 ticker 並輸出 JSON（只含自身市場交易日）"""
    idx   = raw.index
    close = raw["Close"].copy()
    d     = raw["Dividends"].fillna(0).copy()

    thresh = (
        pd.Series(np.where(idx < pd.Timestamp("2015-06-01"), 0.075, 0.11), index=idx)
        if _is_tw(tkr) else 0.15
    )

    if _is_tw(tkr):
        close, d = _fix_split_stitch(close, d, tkr)

    close = _despike(close, thresh)

    if tkr in MANUAL_DIVS:
        for ds, amt in MANUAL_DIVS[tkr].items():
            ts = pd.Timestamp(ds)
            if ts < idx[0] or ts > idx[-1]:
                continue
            nearest = idx[idx.searchsorted(ts)]
            if d.at[nearest] == 0:
                d.at[nearest] = amt
                print(f"[修補] {tkr} {nearest:%Y-%m-%d} 補上 Yahoo 缺漏配息 {amt:.2f}")

    if _is_tw(tkr):
        d = _fix_div_scale(d, close, tkr)

    records = [
        {
            "date":  dt.strftime("%Y-%m-%d"),
            "close": round(float(close[dt]), 6),
            "div":   round(float(d[dt]), 6),
        }
        for dt in idx
        if not pd.isna(close[dt])
    ]

    payload = {
        "ticker":    tkr,
        "currency":  "TWD" if _is_tw(tkr) else "USD",
        "generated": date.today().isoformat(),
        "data":      records,
    }

    safe_name = tkr.replace("/", "_")
    fname = f"{out_dir}/data_{safe_name}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[輸出] {fname}  （{len(records)} 筆，{records[0]['date']} ~ {records[-1]['date']}）")


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="匯出 yfinance 資料為 JSON（供 HTML 回測介面使用）",
        epilog="範例：python export_data.py 0050.TW 6208.TW VT BNDW",
    )
    parser.add_argument("tickers", nargs="+", help="標的代號，例如：0050.TW VT BNDW")
    parser.add_argument("--out", default=".", help="輸出目錄（預設為當前目錄）")
    args = parser.parse_args()

    tickers = list(dict.fromkeys(args.tickers))   # 去重保序
    os.makedirs(args.out, exist_ok=True)
    print(f"下載標的：{', '.join(tickers)}\n")

    for tkr in tickers:
        print(f"  下載 {tkr} ...")
        h = yf.Ticker(tkr).history(start=START, auto_adjust=False, actions=True)
        if h.empty:
            hint = "（台股 6 碼代號需補前導零，例如 006208.TW、00878.TW）" if _is_tw(tkr) else ""
            raise RuntimeError(f"[錯誤] {tkr} 無法取得資料，請確認 ticker 是否正確 {hint}")
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
