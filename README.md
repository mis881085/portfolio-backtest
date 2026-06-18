# Portfolio Backtest

互動式投資組合回測工具。透過 Python 腳本從 Yahoo Finance 抓取歷史資料，再由單頁 HTML 介面進行回測、視覺化與比較。

線上版本：**https://mis881085.github.io/portfolio-backtest/**

---

## 功能

- 支援台股（.TW）與美股混合組合
- 年末再平衡策略（期初 All-in，每年 12 月底依目標比例再投入）
- 考慮換匯價差、台股手續費與交易稅、美股配息預扣稅、複委託費用
- 互動式淨值走勢圖（可縮放、平移）
- 績效摘要：年化報酬、年化標準差、最大跌幅
- 各年度報酬率表（hover 顯示再平衡明細）

---

## 安裝套件

```bash
pip install yfinance pandas numpy
```

---

## 使用方式

### 1. 匯出資料

```bash
python export_data.py <ticker1> <ticker2> ... --out <輸出目錄>
```

**範例：**

```bash
# 台股 ETF
python export_data.py 0050.TW 006208.TW --out data

# 美股 ETF
python export_data.py VT VTI SPY BNDW --out data

# 混合（台美股 + 指定輸出目錄）
python export_data.py 0050.TW 006208.TW VT VTI SPY BNDW --out data

# 個股
python export_data.py 2880.TW 2892.TW --out data
```

執行後會在指定目錄產生：
- `data_<ticker>.json`：各標的歷史收盤價與股息
- `data_FX_USDTWD.json`：USD/TWD 匯率歷史資料

> **注意**：台股代號須補前導零，例如 `006208.TW`、`00878.TW`，而非 `6208.TW`。

### 2. 開啟 index.html

直接在瀏覽器開啟 `index.html`，或使用線上版本：
https://mis881085.github.io/portfolio-backtest/

**操作步驟：**

1. **載入資料**：將 `data/` 資料夾內的 JSON 檔案拖放至上傳區（可一次多選），或從瀏覽器直接拖入 raw 檔案連結
2. **設定投資組合**：新增投資組合，選擇標的與各標的比重（須加總為 100%）
3. **調整回測參數**：期初金額、手續費、稅率等（可依需求展開修改）
4. **執行回測**：點擊「執行回測」，結果自動顯示於下方

---

## 已知問題

- **Yahoo Finance 不支援台灣債券 ETF**：帶有 `B` 字尾的台灣債券 ETF（如 `00933B.TW`、`00679B.TW`）在 Yahoo Finance 無資料，`export_data.py` 無法下載。如需使用此類標的，須自行從其他來源（如證交所、CMoney）取得日線資料，並手動整理成以下 JSON 格式：

```json
{
  "ticker": "00933B.TW",
  "currency": "TWD",
  "generated": "2026-06-19",
  "data": [
    { "date": "2022-06-01", "close": 10.00, "div": 0.0 },
    { "date": "2022-06-02", "close": 10.02, "div": 0.0 }
  ]
}
```
