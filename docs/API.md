# API 規格

Base URL：`http://<host>:8090`

---

## 分鏡生成

### POST `/api/analyze`

上傳漫畫頁，背景任務生成。

| 參數 | 型別 | 說明 |
|---|---|---|
| `files` | File[] | 漫畫圖片（最多 40 頁） |
| `title` | string | 作品名稱（選填） |

回應：

```json
{"project_id": "abc123", "status": "running", "pages": 5}
```

### GET `/api/status/{pid}`

```json
{"status": "running|done|error", "progress": "3/5 頁", "error": null}
```

### GET `/api/project/{pid}`

```json
{
  "title": "...", "logline": "...", "estimated_duration": "...",
  "global_style": "...",
  "characters": [{"id","name","role","appearance_en","personality","ref_prompt"}],
  "scenes": [{"id","name","description_en"}],
  "shots": [{"id","page","duration","shot_size","camera",
             "character_ids","scene_id","action","expression",
             "lighting","mood","dialogue","sfx",
             "prompt","negative_prompt","video_prompt"}],
  "negative_prompt": "...",
  "art_bible": "## 1. 全域美術基準\n...",
  "page_count": 5
}
```

### GET `/api/projects`

```json
{"projects": [{"id","title","page_count","shots","characters","done_at"}]}
```

### GET `/api/export/{pid}?fmt=md|csv|json`

| fmt | 內容 |
|---|---|
| `md` | 完整 Markdown（美術基準 + 角色 + 場景 + 逐鏡描述） |
| `csv` | 鏡頭表格，可用 Excel 開啟 |
| `json` | 原始結構 |

### DELETE `/api/project/{pid}`

---

## 視覺審查

### POST `/api/review`

| 參數 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `image` | File | ✅ | 待審查的生成圖 |
| `target_prompt` | string | ✅ | 目標提示詞 |
| `characters` | string | | 角色設定（強烈建議填） |
| `shot` | string | | 該鏡頭的分鏡資料 |
| `extra` | string | | 補充說明 |

回應：

```json
{
  "observation": "純觀察，不含評價",
  "verdict": "PASS|NEEDS_FIX",
  "score": 75,
  "issues": [{"severity","category","observation","fix"}],
  "rewritten_prompt": "...",
  "negative_prompt": "...",
  "_enforced": false,
  "_notes": []
}
```

> `_enforced: true` 代表判定被規則層改過，`_notes` 說明原因。

---

## 問題修復

### POST `/api/repair`

| 參數 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `prompt` | string | ✅ | 使用的提示詞 |
| `symptom` | string | ✅ | 發現的問題（越具體越準） |
| `characters` | string | | 角色設定 |
| `shot_id` | string | | 鏡頭編號 |
| `image` | File | | 實際生成的圖（可選） |

回應：

```json
{
  "primary_cause": {"category","explanation","evidence"},
  "matched_rule": "R-CHAR-DRIFT",
  "rewritten_prompt": "...",
  "negative_prompt": "...",
  "changes": ["..."],
  "residual_risk": "...",
  "prevention": "...",
  "_rule_applied": "R-CHAR-DRIFT",
  "_fallback": false
}
```

> `_fallback: true` 代表模型診斷失敗，由規則庫接手。

### 常見症狀關鍵詞

系統會依這些詞比對規則庫：

- `R-CHAR-DRIFT` — 每格長得都不一樣、服裝變了、臉不像
- `R-SEED-DRIFT` — 畫風跟其他格不一致、風格不統一
- `R-HAND-DEFECT` — 手變成六根手指、手部畫錯
- `R-COMPOSITION` — 主體太小、出框、構圖不對
- `R-POSE` — 動作很怪、扭曲、姿勢不對
- `R-TEXT-ARTIFACT` — 有殘留文字、浮水印
- `R-LIGHTING` — 光線太平、沒有立體感
- `R-STYLE-MISS` — 畫風不對、太寫實
- `R-BACKGROUND` — 背景多出不該有的東西

---

## 記憶庫

### GET `/api/memory`

```json
{
  "total_repairs": 5,
  "distinct_prompts": 4,
  "lessons": [{"symptom","times","causes","fixes"}],
  "recent": [{"shot_id","symptom","explanation","rewritten_prompt","hits","ts"}],
  "cause_rules": [{"id","trigger","cause"}]
}
```

### GET `/api/lessons`

把累積記錄蒸餾成可重複套用的通則。

> 首次呼叫會呼叫模型，約需 10–60 秒。

```json
{"digest": "- 結構性元素用硬數值約束取代形容詞堆疊\n...", "has_memory": true}
```

### DELETE `/api/memory`

清空所有修正記錄（不可復原）。

---

## 錯誤格式

```json
{"detail": "錯誤訊息"}
```

常見狀況：

| 狀況 | 原因 | 處理 |
|---|---|---|
| `Form data requires python-multipart` | 啟動時缺套件 | `pip install python-multipart` |
| `502 模型呼叫失敗` | vLLM 沒開或逾時 | 檢查 `VLLM_BASE` |
| `專案不存在` | 重啟前沒存檔，或 ID 錯 | 用 `/api/projects` 查 |
| `404 Not Found` | 端點路由被誤刪 | 逐個 curl 驗證 |