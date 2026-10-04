# 漫畫改編工作站

把漫畫一話改編成 AI 影片的分鏡腳本與台詞，用持久化記憶持續修正問題。

**版本 0.3.0 — 角色位架構**

> **v0.3.0 重大變更**：角色外觀不再由分鏡層負責。
> 系統改用**角色位（cast slot）**：只輸出 `女1`／`男1`／`路人` 這類位置編號，
> 外觀一律留白，待後期以參考圖（reference）套用。詳見 [角色位架構](docs/CAST_SLOTS.md)。

---

## 角色位架構（v0.3.0）

### 為什麼這樣設計

原本的設計要求把完整角色外觀逐字嵌入每個鏡頭的提示詞，靠文字保證全片一致性。
這條規則在「後期用參考圖決定角色」的流程裡不成立——文字描述與參考圖會互相衝突。

**新的分工**：

```
漫畫 → 分鏡層（讀劇情、排角色位、寫台詞）  →  合成層（套用參考圖、決定外觀）
        ↑ 不描述任何外貌                          ↑ 唯一的外觀真相來源
```

### 實際長什麼樣

```json
"characters": [
  {"id": "女1", "gender": "female", "count": 8, "first_page": 1},
  {"id": "男1", "gender": "male",   "count": 3, "first_page": 1},
  {"id": "路人", "gender": "crowd",  "count": 12, "first_page": 3}
],
"shots": [
  {"id": "SH03", "character_ids": ["女1", "男1"],
   "prompt": "two figures facing each other in a classroom at dusk, tense atmosphere, ..."}
]
```

**`character_ids` 就是餵給合成模型的 reference 清單**——這個鏡頭該套哪幾張參考圖。

### 三件事因此改變了

| 項目 | 以前 | 現在 |
|---|---|---|
| 角色資料 | 姓名＋外觀＋性格＋定裝照提示詞 | 只有編號、性別、出場次數 |
| `R-CHAR-DRIFT` 規則 | 修法是「把外觀貼回每個提示詞」 | 修法是「檢查參考圖對應」 |
| 負面提示詞 | 含 `inconsistent character design` | 已移除（外觀不由本層負責） |

### 已知限制

**模型仍可能自行描述外觀。** 實測中模型會無視禁令寫出
`a man wearing a black cap and a dark blue shirt`。系統會偵測並在介面標示
（`appearance_leaks`），但**刻意不自動刪除**——用規則剝英文外觀詞會誤傷敘事描述
（`close-up of the eyes` 是敘事，`dark eyes` 才是外觀）。請用「⚠ 修復」重寫。

**舊專案已遷移但提示詞未清洗。** v0.2 資料的角色欄位會自動轉成角色位，
但既有鏡頭提示詞裡的外觀描述保留在內部 `_prompt_legacy`，需逐鏡重寫。

---

## 快速開始

```bash
cd manga-studio
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn python-multipart pillow requests
.venv/bin/python manga_studio.py
```

開啟 `http://<主機>:8090`

### 必要依賴

| 套件 | 用途 | 備註 |
|---|---|---|
| `fastapi` | Web 框架 | |
| `uvicorn` | ASGI 伺服器 | |
| `python-multipart` | 檔案上傳 | **常見遺漏**，缺了會在啟動時報錯 |
| `pillow` | 圖片縮放與編碼 | |
| `requests` | 呼叫 vLLM | |

系統 Python 可能受 PEP 668 保護，需 `pip install --break-system-packages` 或用 venv。

---

## 系統需求

**後端模型**：OpenAI 相容的 vLLM 端點，需支援視覺輸入與結構化輸出。

```bash
# 驗證端點
curl -s http://127.0.0.1:8081/v1/models
```

**結構化輸出必須可用**（vLLM 0.6+ 支援）：

```python
response_format={"type": "json_schema",
                 "json_schema": {"name": "x", "schema": {...}, "strict": True}}
```

---

## 四大功能

### 1. 漫畫 → 分鏡

上傳多頁漫畫，產出**角色位**、場景表、逐鏡描述與台詞。

**核心觀念：這不是「拆分」，是「改編」。**

漫畫分格是排版結果，不是導演意圖。系統會強制：

- 合併連續且同場景、景別相近的鏡頭（合併後不超過 8 秒）
- 對白鏡頭依字數計算時長（0.4 秒/字 + 1 秒停頓）
- 開場場景建立鏡頭保底 4 秒，結尾 4 秒留餘韻
- 強制指定運鏡，避免整片 static

20 頁漫畫約 40–60 格，產出 8–15 個鏡頭，單鏡 3–6 秒，成片 45–90 秒。

### 2. 角色位編號的穩定性

20 頁分 4 批處理，編號**由程式層分配器決定**，不由模型決定。

模型每批只標註性別（`female`／`male`／`crowd`），
系統依**首次出場順序**編成 `女1`／`男1`／`路人`，並在後續批次的提示詞中
帶入已確立的角色位，確保第 20 頁的第 1 號角色仍是同一個人。

光靠「餵前一批結果給模型看」不足以保證編號穩定——模型會自行發明編號。
這是 [PITFALLS 第十條](docs/PITFALLS.md)的教訓：光靠提示詞約束不夠，
輸出結構要在程式層強制。

### 3. 全域美術基準

產出總結式提示詞文件，供後期統一全片風格。

```
1. 全域美術基準      英文風格錨點，套在每個鏡頭 prompt 最前
2. 角色位對照表     編號、出場次數、情緒定位（不含外觀）
3. 場景速查         描述 + 光線 + 構圖 + 排除物件
4. 色彩與情緒曲線   從頭到尾的情緒走向
5. 統一負面提示詞   不含角色外觀類負面詞
```

> 實測：LLM 回應可能被非確定性截斷（本專案曾收到只回 30 字的情況）。
> 系統會驗證五個段標題是否齊全，缺失則自動重試。

### 4. 問題修復

描述發現的問題 → 根因分析 → 提示詞修補 → 寫入記憶。

三層防線：

| 層 | 機制 | 何時生效 |
|---|---|---|
| 規則庫 | 9 條症狀→根因→修法的固定對應 | 永遠可用，不經模型 |
| 模型分析 | 給規則庫+提示詞+問題，產出結構化診斷 | 主路徑 |
| 規則複核 | 只在模型列出 BLOCKER 卻判 PASS 時介入 | 保險 |

### 5. 記憶庫

每次修復寫入 `repair_memory.json`（持久化，重啟保留）。

- 同鏡頭重複修復 → `hits` 累加
- 同症狀再次出現 → 歸納為通則，注入後續診斷
- `/api/lessons` 蒸餾成可重複套用的規律

---

## 設定

全部走環境變數：

```bash
VLLM_BASE=http://127.0.0.1:8081    # 模型端點
VLLM_MODEL=qwen3.8-27b-abliterated-mtp
IMG_MAX_EDGE=1024                 # 分鏡頁送進模型的圖片邊長
REVIEW_MAX_EDGE=1792              # 審查圖（需看清手指、徽章等細節）
IMG_QUALITY=82
MAX_TOKENS=6000
BATCH_PAGES=6                     # 每批幾頁
MEMORY_PATH=./repair_memory.json
PROJECTS_DIR=./projects
HOST=0.0.0.0
PORT=8090
```

---

## 專案結構

```
manga-studio/
├── manga_studio.py      # 主程式：API、分鏡生成、序列重整、審查、修復、記憶
├── repair_module.py     # 根因規則庫（9 條）與診斷提示詞
├── index.html           # 前端
├── scripts/
│   ├── vision_bench.py   # 視覺能力基準測試
│   └── comfy_bridge.py   # ComfyUI 生圖（尚未整合進主程式）
├── docs/
│   ├── CAST_SLOTS.md     # 角色位架構設計（v0.3.0 必讀）
│   ├── PITFALLS.md       # 踩過的坑（重要）
│   └── API.md            # API 規格
├── projects/            # 分鏡結果存檔
└── repair_memory.json   # 記憶體
```

---

## API

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/api/analyze` | 上傳漫畫，背景任務生成 |
| GET | `/api/status/{pid}` | 查詢進度 |
| GET | `/api/project/{pid}` | 取回結果 |
| GET | `/api/projects` | 列出已存檔專案 |
| GET | `/api/export/{pid}?fmt=` | 匯出 md / csv / json |
| DELETE | `/api/project/{pid}` | 刪除專案 |
| POST | `/api/review` | 視覺審查 |
| POST | `/api/repair` | 根因分析與修補 |
| GET | `/api/memory` | 記憶庫內容 |
| GET | `/api/lessons` | 蒸餾通則 |
| DELETE | `/api/memory` | 清空記憶 |

---

## 已知限制

**視覺計數能力不可靠。** 實測能數對漫畫分格，但手指數會出錯（畫 7 根答 5 根）。計數類問題需人工核對。

**審查判定需人工確認。** 模型能準確指出瑕疵並描述位置，但嚴重度分級會偏保守（傾向標 MINOR）。

**產出影片需外部工具。** 本系統只到靜態分鏡圖。動態化需要 Qwen-Image-2.1 之外的影片模型（Veo / Kling / Wan / LTX 等）。

**成本估算**：100 張分鏡圖約 $0.45（`gemini-3.1-flash-image`）。圖像生成不是瓶頸。

---

## 相關文件

- [踩過的坑](docs/PITFALLS.md) — **必讀**，記錄了多次錯誤判斷的過程
- [API 規格](docs/API.md)