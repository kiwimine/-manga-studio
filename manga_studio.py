#!/usr/bin/env python3
"""
漫畫 → AI 影片 分鏡工作台
掛載本地 vLLM 多模態模型（OpenAI 相容 API）

功能：
  1. 上傳漫畫頁（可多頁）→ 讀圖 → 產出角色設定檔 / 場景表 / 逐鏡描述 + 圖像提示詞
  2. 上傳產出的分鏡圖 → 視覺回饋 → 指出問題 → 重寫提示詞
"""

import base64
import io
import json
import os
import re
import textwrap
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, Response
from PIL import Image

from repair_module import (
    CAUSE_RULES, match_causes, PROMPT_DIAGNOSE, SCHEMA_DIAGNOSE,
    PROMPT_HISTORY_SUMMARY,
)

# ─────────────────────────── 設定 ───────────────────────────

VLLM_BASE = os.environ.get("VLLM_BASE", "http://127.0.0.1:8081")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "qwen3.8-27b-abliterated-mtp")
# 分鏡頁送進模型的圖片邊長（控制 token 消耗；90k context 下不宜太大）
IMG_MAX_EDGE = int(os.environ.get("IMG_MAX_EDGE", "1024"))
# 審查圖需看清手指、徽章、細小文字，解析度給更高
REVIEW_MAX_EDGE = int(os.environ.get("REVIEW_MAX_EDGE", "1792"))
IMG_QUALITY = int(os.environ.get("IMG_QUALITY", "82"))
# 單次請求最大 tokens（逐鏡描述可能很長）
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "6000"))

app = FastAPI(title="漫畫分鏡工作台")

# 專案儲存
STORE: dict = {}

# ── 自我修復記憶體 ──
# 持久化到磁碟，重啟後仍保留。這是「記憶」的核心。
MEMORY_PATH = os.environ.get("MEMORY_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "repair_memory.json"))

# 鍵為正規化的提示詞指紋，值為修正記錄
PROJECT_INDEX: dict = {}         # pid -> 摘要（重啟後用來列清單）

REPAIR_MEMORY: dict = {}          # fingerprint -> {symptom, fix, cause, shot_id, ts, counts}
# 已驗證有效的修正（同一症狀出現過幾次）
PATTERN_LESSONS: dict = {}        # symptom_key -> {times, causes[], fixes[]}


def _norm_key(text: str) -> str:
    """把提示詞正規化成穩定指紋，讓同一鏡頭的歷次修正能累積。
    保留 CJK 字元，否則中文症狀會被清空。"""
    t = (text or "").lower()
    # 移除標點，保留中英文與數字
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t, flags=re.UNICODE).strip()
    if re.search(r"[\u4e00-\u9fff]", t):
        # 中文症狀：直接取前 24 個字元作為指紋
        return t.replace(" ", "")[:24]
    toks = [w for w in t.split() if len(w) > 3]
    return " ".join(toks[:24])


PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "projects"))


def persist_project(pid: str, result: dict):
    """把分鏡結果寫到磁碟，重啟後仍可取回。"""
    try:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        path = os.path.join(PROJECTS_DIR, f"{pid}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        print(f"[{pid}] 已存檔 → {path}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{pid}] 存檔失敗（不影響使用）：{e}", flush=True)


def load_projects():
    """啟動時載入磁碟上的專案，恢復清單。"""
    global PROJECT_INDEX
    try:
        for fn in os.listdir(PROJECTS_DIR):
            if not fn.endswith(".json"):
                continue
            pid = fn[:-5]
            try:
                with open(os.path.join(PROJECTS_DIR, fn), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            mtime = os.path.getmtime(os.path.join(PROJECTS_DIR, fn))
            STORE[pid] = {"status": "done", "progress": "done",
                          "error": None, "result": data,
                          "done_at": int(mtime)}
            PROJECT_INDEX[pid] = {
                "title": data.get("title") or "未命名",
                "page_count": data.get("page_count", 0),
                "shots": len(data.get("shots", [])),
                "characters": len(data.get("characters", [])),
                "done_at": int(mtime),
            }
        if PROJECT_INDEX:
            print(f"[projects] 恢復 {len(PROJECT_INDEX)} 個專案", flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[projects] 載入失敗（忽略）：{e}", flush=True)


def load_memory():
    global REPAIR_MEMORY, PATTERN_LESSONS
    try:
        with open(MEMORY_PATH, encoding="utf-8") as f:
            d = json.load(f)
        REPAIR_MEMORY = d.get("repairs", {})
        PATTERN_LESSONS = d.get("lessons", {})
        print(f"[memory] 載入 {len(REPAIR_MEMORY)} 筆修正記錄、"
              f"{len(PATTERN_LESSONS)} 條規律", flush=True)
    except FileNotFoundError:
        print("[memory] 尚無記錄檔，從空白開始", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 載入失敗（忽略）：{e}", flush=True)


def save_memory():
    try:
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump({"repairs": REPAIR_MEMORY, "lessons": PATTERN_LESSONS},
                      f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 寫入失敗（忽略）：{e}", flush=True)


def record_repair(fingerprint: str, shot_id: str, symptom: str,
                  diagnosis: dict, rewritten: str):
    """把一次修正寫入記憶。"""
    key = _norm_key(rewritten) or fingerprint
    REPAIR_MEMORY[key] = {
        "shot_id": shot_id,
        "symptom": symptom,
        "category": diagnosis.get("primary_cause", {}).get("category", ""),
        "explanation": diagnosis.get("primary_cause", {}).get("explanation", ""),
        "evidence": diagnosis.get("primary_cause", {}).get("evidence", ""),
        "rewritten_prompt": rewritten,
        "negative_prompt": diagnosis.get("negative_prompt", ""),
        "changes": diagnosis.get("changes", []),
        "prevention": diagnosis.get("prevention", ""),
        "ts": int(__import__("time").time()),
        "hits": REPAIR_MEMORY.get(key, {}).get("hits", 0) + 1,
    }

    sk = _norm_key(symptom)[:60]
    if sk:
        L = PATTERN_LESSONS.setdefault(sk, {"times": 0, "causes": [], "fixes": []})
        L["times"] += 1
        c = diagnosis.get("primary_cause", {}).get("category", "")
        if c and c not in L["causes"]:
            L["causes"].append(c)
        f_ = diagnosis.get("prevention", "") or diagnosis.get("negative_prompt", "")
        if f_ and f_ not in L["fixes"]:
            L["fixes"].append(f_)
        L["fixes"] = L["fixes"][:5]
    save_memory()


def memory_context_for(prompt: str, shot_id: str = "") -> str:
    """組出給模型的歷史記憶上下文。"""
    fp = _norm_key(prompt)
    parts = []
    # 同一鏡頭的歷次修正
    same = REPAIR_MEMORY.get(fp)
    if same:
        parts.append(f"【此鏡頭先前的修正記錄（共修正 {same['hits']} 次）】\n"
                     f"曾出現的問題：{same['symptom']}\n"
                     f"當時的根因：{same['explanation']}\n"
                     f"採取的修法：{'; '.join(same.get('changes', [])[:4])}\n"
                     f"結果仍存在的問題：{same.get('residual_risk','')}")
    # 跨鏡頭的規律
    if PATTERN_LESSONS:
        rows = []
        for sk, L in list(PATTERN_LESSONS.items())[:8]:
            rows.append(f"- 曾發生「{sk}」× {L['times']} 次；"
                        f"根因：{'、'.join(L['causes'][:2])}；"
                        f"有效修法：{'；'.join(L['fixes'][:2])}")
        parts.append("【跨鏡頭已驗證的規律】\n" + "\n".join(rows))
    return "\n\n".join(parts) if parts else "（尚無歷史記錄）"


def lessons_digest() -> str:
    """把累積的規律蒸餾成一段可注入所有後續生成的文字。"""
    if not PATTERN_LESSONS:
        return ""
    return chat([{
        "role": "user",
        "content": PROMPT_HISTORY_SUMMARY.format(
            records=json.dumps(
                [{"symptom": k, **v} for k, v in PATTERN_LESSONS.items()],
                ensure_ascii=False, indent=1)
        ),
    }], temperature=0.2, max_tokens=1200)
LOCK = threading.Lock()


# ─────────────────────────── 工具 ───────────────────────────

def encode_image(img: Image.Image, max_edge: int = IMG_MAX_EDGE) -> str:
    """縮圖並轉成 JPEG base64，控制 token 用量。"""
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_edge:
        s = max_edge / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=IMG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def data_url(b64: str) -> str:
    return f"data:image/jpeg;base64,{b64}"


def chat(messages, temperature=0.4, max_tokens=MAX_TOKENS, retries=2,
         schema=None, no_think=False):
    """呼叫本地 vLLM。

    schema    — 給定時使用結構化輸出（硬約束 JSON schema）
    no_think  — 關閉思考鏈。純觀察任務不需要，且思考會顯著拖慢回應。
    """
    body = {
        "model": VLLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema, "strict": True},
        }
    if no_think:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    last_err = None
    for _ in range(retries + 1):
        try:
            r = requests.post(f"{VLLM_BASE}/v1/chat/completions", json=body, timeout=1800)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                continue
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise HTTPException(status_code=502, detail=f"模型呼叫失敗：{last_err}")


def human_pages(files) -> list:
    """把上傳的多個檔案轉為圖片物件清單（處理 GIF/PNG/BMP → RGB）。"""
    pages = []
    for f in files:
        raw = f.file.read()
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"{f.filename} 不是有效圖片：{e}")
        pages.append({"name": f.filename, "img": img.convert("RGB")})
    return pages


def strip_fence(txt: str) -> str:
    """移除模型偶發的 ```json 包裝（可能只有開頭、結尾被截斷）。"""
    t = txt.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"```\s*$", "", t)
    return t.strip()


def _balance(s: str, opener: str, closer: str) -> str | None:
    """取出第一段平衡的括號區塊（忽略字串內的括號）；未閉合則補齊。"""
    start = s.find(opener)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:] + closer * depth if depth > 0 else None


def parse_json_loose(txt: str):
    """盡力從模型輸出抽出 JSON，容忍截斷與圍欄殘缺。"""
    txt = strip_fence(txt)
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        frag = _balance(txt, opener, closer)
        if not frag:
            continue
        try:
            return json.loads(frag)
        except Exception:  # noqa: BLE001
            # 尾部被截斷：逐段回退並重新補齊括號
            for cut in range(len(frag) - 1, max(len(frag) - 4000, 0), -1):
                head = frag[:cut].rstrip().rstrip(",:")
                tail = frag[cut:]
                need = tail.count(opener) - tail.count(closer) + 1
                try:
                    return json.loads(head + closer * max(need, 0))
                except Exception:  # noqa: BLE001
                    continue
    return None


# ─────────────────────────── Prompt ───────────────────────────

PROMPT_READ = """你是一位專業的漫畫改編者，專精於把漫畫轉成 AI 影像生成的鏡頭腳本。

【核心觀念：這不是「拆分」，是「改編」】
漫畫是靜態的視覺碎片，影片是連續的時間。
**絕對不要一格對應一個鏡頭** —— 漫畫分格是排版結果，不是導演意圖。
你必須先理解劇情，再重新設計鏡頭序列。

【第一步：理解內容】
通讀所有分格，回答自己：
- 這一話在講什麼？情緒如何發展？
- 誰是主角？他在這一話中的目標與阻礙？
- 場景有沒有變化？氣氛如何轉變？

【第二步：設計鏡頭序列】
把劇情拆成 3–8 個「鏡頭段落（beat）」，每個段落是一段連續的視覺表現，
可以由多格漫畫融合而成，也可以由一格拆成多個鏡頭。

判斷原則：
- **連續的靜止畫面**（同一場景、角色在做一件事）→ 合成一個鏡頭，用運鏡帶出變化
- **情緒遞進**（由遠到近、由靜到動）→ 用鏡頭運動表現，不要切鏡
- **視角真正改變**（換場景、換時間、換視角）→ 這裡才切鏡
- **漫畫分格但內容連續** → 不要跟著分格切
- 原本就切成很多格的連續動作 → 可能是運動線索，應該合併成一個動態鏡頭

【每個鏡頭的時間規劃】
- 單一鏡頭建議 3–6 秒。影片鏡頭不會每秒切一次
- 一句對白約 0.35 秒／字（約 5 字／秒），超過 15 字要考慮拆鏡頭或刪減
- 開場建立鏡頭可給 4–6 秒（讓觀眾進入場景）
- 情緒高點可用 2–3 秒短鏡頭（對比效果）
- 結尾鏡頭給 4–8 秒（留餘韻）

【每個鏡頭的組成】
每個鏡頭應該包含：
- 一個明確的視覺焦點
- 一個運鏡方式（static / slow push in / pan / tracking / pull out）
- 一個情緒基調

【硬性規則】
1. 對白框與擬聲詞**不要畫成畫面元素**，改寫成 dialogue / sfx 欄位
2. 漫畫的速度線、集中線、色網點是靜態媒介語言，轉影片時**必須省略**
3. 角色外觀必須在**每個 shot 的 prompt 中逐字重複嵌入**，不得用「同上」「該角色」
   —— 繪圖模型無記憶，這是全片一致性的唯一保證
4. 全片共用同一組風格關鍵詞
5. shot 的 id 用兩位數，從 01 開始

【appearance 欄位格式】
必須是**可直接餵給生圖模型的英文描述**：
"16-year-old male, short black hair with bangs covering right eye, dark brown eyes,
slim build, navy blue high-collar uniform jacket with silver badge, black bandage on right wrist"
不要寫成中文敘述。"""


PROMPT_SUMMARY = """請為這一話的 AI 影像生成，撰寫一份**總結式（全局）提示詞文件`。

用途：這份文件要給使用者作為整片的美術與場景基準，供 Qwen-Image-2.1 生圖時統一風格。

【已知資料】
本話標題：{title}
劇情梗概：{logline}
預估長度：{duration}
畫風關鍵詞：{style}

【角色（外觀必須逐字沿用，不可改寫）】
{characters}

【場景】
{scenes}

【鏡頭序列】
{shots}

【輸出內容】
請產出一份 Markdown，必須包含以下段落：

## 1. 全域美術基準
一段可直接餵給生圖模型的英文提示詞，涵蓋：畫風、線稿特性、上色方式、色板、年代感、媒介質感。
這段要能單獨套用到任何一個鏡頭。

## 2. 角色速查
每個角色一段，逐字沿用上面的 appearance，並補上：
- 建議的出鏡景別
- 表情基線
- 不可出現的干擾（避免特徵被誤加）

## 3. 場景速查
每個場景一段，逐字沿用上面的 description，並補上：
- 建議的光線條件與時間
- 構圖建議
- 應排除的物件

## 4. 色彩與情緒曲線
按鏡頭順序，列出該段的情緒基調與建議色調，
說明情緒如何從頭走到尾（這是導演視角，不是逐格描述）。

## 5. 統一負面提示詞
列出全片共用的 negative prompt，針對生成瑕疵與風格偏移。

【要求】
- 風格與場景段落用英文（要餵給生圖模型）
- 說明與分析用中文
- 不要逐格複述鏡頭清單，要提煉出貫穿全片的規律
- 內容要具體可執行，不要空泛的形容詞"""

SCHEMA_SHOT = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "estimated_duration": {"type": "string"},
        "global_style": {
            "type": "string",
            "description": "英文風格關鍵詞，全片共用，例：Japanese anime cel-shading, clean lineart, vibrant flat colors",
        },
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "appearance_en": {"type": "string"},
                    "personality": {"type": "string"},
                    "ref_prompt": {"type": "string"},
                },
                "required": ["id", "name", "appearance_en", "ref_prompt"],
            },
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description_en": {"type": "string"},
                },
                "required": ["id", "name", "description_en"],
            },
        },
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "page": {"type": "integer"},
                    "duration": {"type": "string"},
                    "shot_size": {"type": "string"},
                    "camera": {"type": "string"},
                    "character_ids": {"type": "array", "items": {"type": "string"}},
                    "scene_id": {"type": "string"},
                    "action": {"type": "string"},
                    "expression": {"type": "string"},
                    "lighting": {"type": "string"},
                    "mood": {"type": "string"},
                    "dialogue": {"type": "string"},
                    "sfx": {"type": "string"},
                    "prompt": {"type": "string"},
                    "video_prompt": {"type": "string"},
                },
                "required": [
                    "id", "page", "duration", "shot_size", "camera",
                    "character_ids", "scene_id", "action", "dialogue",
                    "prompt", "video_prompt",
                ],
            },
        },
    },
    "required": ["title", "global_style", "characters", "scenes", "shots"],
}


PROMPT_FEEDBACK = """你是一位影像品質評估員。請觀察圖片並比對目標提示詞。

【第一階段：純觀察】
只看圖片本身，**不參考提示詞、不下好壞判斷**。依序寫下你在圖上實際看到的：

1. 畫面主體：幾個人／件物？各自的性別年齡、髮型髮色、眼睛、膚色、服裝顏色與款式
2. 姿勢與動作：身體朝向、手臂位置、視線方向
3. 細部：服裝上有無徽章／口袋／領扣等特徵？有無飾品？手腕、頸部有無特殊物？
4. 背景：場景類型、可見的物件清單、光源方向與強弱
5. 畫面文字：有無任何可見文字？內容為何？
6. 畫風：線稿特性、上色方式、色調

**這一階段只寫事實，不寫「好看」「不對」「奇怪」等評價字眼。**

【第二階段：逐項比對】
現在才對照提示詞與角色設定，逐項標記「符合」或「不符合」：

- 角色外觀：髮型、瞳色、體型、服裝款式與顏色是否都與設定一致？
- 標誌性細節：設定中提到的配件（繃帶、徽章、眼鏡等）是否出現在圖上？
- 景別：要求的 close-up / medium / wide 是否符合？
- 姿勢：要求的動作是否呈現？
- 背景：要求的場景元素是否都在？有無出現未提及的干擾物？
- 畫風：是否符合指定的風格關鍵詞？
- 文字：畫面是否乾淨，無殘留文字？

【判定規則】
- 全部符合 → PASS
- 任何一項不符合 → NEEDS_FIX，並在 issues 標明是哪一項

【關於看不到的特徵】
若設定要求的特徵在畫面中無法確認（例如畫面裁切在手腕以上，看不到繃帶），
在 observation 中如實說明「該區域未入鏡」，並在 issues 中列為 MINOR（可接受）而非 BLOCKER。
不要因為看不見就判定生成失敗。

【severity】
- BLOCKER：與提示詞明確衝突，或有明顯生成瑕疵（多餘肢體、不對稱五官、殘留文字）
- MINOR：可用但不理想（構圖偏移、陰影不足、配色略偏、風格有輕微出入）

【重要】
不要為了嚴格而誇大問題。如果圖片符合提示詞，就老實說 PASS。
同樣，也不要為了寬容而放過明顯缺陷。你的觀察與判定必須一致。

【輸出】
observation 寫第一階段的純觀察事實，不含評價。"""

SCHEMA_REVIEW = {
    "type": "object",
    "properties": {
        "observation": {
            "type": "string",
            "description": "純觀察：在圖上實際看到的內容，特別是手指數量、角色外觀細節、有無殘留文字",
        },
        "verdict": {"type": "string", "enum": ["PASS", "NEEDS_FIX"]},
        "score": {"type": "integer"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["BLOCKER", "MINOR"]},
                    "category": {"type": "string"},
                    "observation": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "category", "observation", "fix"],
            },
        },
        "rewritten_prompt": {"type": "string"},
        "negative_prompt": {"type": "string"},
    },
    "required": ["observation", "verdict", "score", "issues",
                 "rewritten_prompt", "negative_prompt"],
}


# ─────────────────────────── 任務 ───────────────────────────

def run_analysis(pid: str, pages: list):
    """背景任務：分析漫畫 → 產出分鏡結構（單階段，結構化輸出）。"""
    try:
        total = len(pages)
        print(f"[{pid}] 開始分析 {total} 頁…", flush=True)

        def prog(done: int):
            with LOCK:
                STORE[pid]["progress"] = f"{done}/{total} 頁"

        # ── 分批送圖，每批最多 6 頁 ──
        # 90k context 下，一次灌 20 頁會爆；分批 + 沿用前一批已定義的角色/場景，
        # 可維持跨頁一致性而不致超出視窗。
        BATCH = int(os.environ.get("BATCH_PAGES", "6"))
        prev_chars: list = []
        prev_scenes: list = []
        all_shots: list = []
        meta: dict = {}

        for start in range(0, total, BATCH):
            chunk = pages[start:start + BATCH]
            # vLLM 的 content 需為扁平 list（text / image_url 交錯），不接受巢狀陣列
            parts = [{"type": "text", "text": (
                f"以下是漫畫第 {start + 1} 頁：\n\n"
                "（請依閱讀順序由左至右、由上至下分析各分格）"
            )}]
            for i, p in enumerate(chunk):
                if i:
                    parts.append({"type": "text", "text": f"\n以下是漫畫第 {start + i + 1} 頁："})
                parts.append({"type": "image_url", "image_url": {"url": data_url(encode_image(p["img"]))}})
            parts.append({"type": "text", "text":
                f"\n以上是第 {start + 1}–{start + len(chunk)} 頁。請為這幾頁產出分鏡資料。"
                f"若已有角色／場景，沿用它們並補充新出現的。"
                f"若為第二批之後，title 欄位填空字串即可。"})

            carry = ""
            if prev_chars or prev_scenes:
                carry = "\n【已定義的角色（務必沿用相同 id 與 appearance_en，不可改寫）】\n"
                carry += json.dumps(prev_chars, ensure_ascii=False)
                carry += "\n\n【已定義的場景（務必沿用相同 id 與 description_en）】\n"
                carry += json.dumps(prev_scenes, ensure_ascii=False)

            head = (
                f"這是漫畫的第 {start + 1} 到第 {start + len(chunk)} 頁。\n"
                f"{carry}\n\n"
                f"請為這幾頁產出分鏡資料。"
                f"若已有角色/場景，沿用它們並補充新出現的。"
                f"若為第二批之後，title 欄位填空字串即可。"
            )

            raw = chat(
                [{"role": "user", "content": [{"type": "text", "text": head}] + parts}],
                temperature=0.3,
                max_tokens=MAX_TOKENS,
                schema=SCHEMA_SHOT,
            )
            data = parse_json_loose(raw)
            if not isinstance(data, dict) or "shots" not in data:
                raise RuntimeError(f"模型未回傳可解析的 JSON（批次 {start // BATCH + 1}）")

            if not meta.get("title") and data.get("title"):
                meta = {
                    "title": data.get("title", ""),
                    "logline": data.get("logline", ""),
                    "estimated_duration": data.get("estimated_duration", ""),
                    "global_style": data.get("global_style", ""),
                }

            # 合併角色／場景（以 id 去重，後者不覆蓋前者）
            seen_c = {c["id"]: c for c in prev_chars}
            for c in data.get("characters", []) or []:
                if c.get("id") and c["id"] not in seen_c:
                    seen_c[c["id"]] = c
            prev_chars = list(seen_c.values())

            seen_s = {s["id"]: s for s in prev_scenes}
            for s in data.get("scenes", []) or []:
                if s.get("id") and s["id"] not in seen_s:
                    seen_s[s["id"]] = s
            prev_scenes = list(seen_s.values())

            for s in data.get("shots", []) or []:
                s["page"] = s.get("page") or (start + 1)
                all_shots.append(s)

            prog(start + len(chunk))
            print(f"[{pid}] 已處理 {start + len(chunk)}/{total} 頁，"
                  f"累計 {len(all_shots)} 顆鏡頭", flush=True)

        # ── 重編鏡號，確保連續；補齊模型漏填的欄位 ──
        # ── 序列重整：把逐格對應重組成真正的鏡頭序列 ──
        before = len(all_shots)
        all_shots = enforce_sequence(all_shots, prev_chars, prev_scenes)
        print(f"[{pid}] 序列重整：{before} 格 → {len(all_shots)} 個鏡頭", flush=True)

        neg = (
            "extra fingers, malformed hands, distorted face, asymmetric eyes, "
            "inconsistent character design, text artifacts, gibberish text, "
            "watermark, low quality, blurry"
        )
        for s in all_shots:
            s.setdefault("negative_prompt", neg)
            if not s.get("video_prompt"):
                s["video_prompt"] = f"{s.get('action','')}; camera: {s.get('camera','static')}"

        result = {
            **(meta or {"title": "", "logline": "", "estimated_duration": "", "global_style": ""}),
            "characters": prev_chars,
            "scenes": prev_scenes,
            "shots": all_shots,
            "negative_prompt": neg,
            "page_count": total,
        }

        # ── 生成總結式全局提示詞文件 ──
        try:
            print(f"[{pid}] 生成全局美術基準文件…", flush=True)
            result["art_bible"] = build_art_bible(result)
            print(f"[{pid}] 美術基準完成（{len(result['art_bible'])} 字）", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{pid}] 美術基準生成失敗（不影響主流程）：{e}", flush=True)
            result["art_bible"] = None

        with LOCK:
            STORE[pid]["result"] = result
            STORE[pid]["status"] = "done"
            STORE[pid]["progress"] = f"{total}/{total} 頁"
            STORE[pid]["done_at"] = int(__import__("time").time())
        persist_project(pid, result)
        print(f"[{pid}] 完成，共 {len(all_shots)} 顆鏡頭、"
              f"{len(prev_chars)} 個角色", flush=True)

    except Exception as e:  # noqa: BLE001
        print(f"[{pid}] 失敗：{e}", flush=True)
        with LOCK:
            STORE[pid]["status"] = "error"
            STORE[pid]["error"] = str(e)


# ─────────────────────────── API ───────────────────────────

@app.post("/api/analyze")
async def api_analyze(files: list[UploadFile] = File(...), title: str = Form("")):
    pages = human_pages(files)
    if not pages:
        raise HTTPException(status_code=400, detail="沒有讀到任何圖片")
    if len(pages) > 40:
        raise HTTPException(status_code=400, detail="單次最多 40 頁，請分批處理")

    pid = uuid.uuid4().hex[:12]
    with LOCK:
        STORE[pid] = {
            "status": "running",
            "result": None,
            "error": None,
            "created": title,
            "progress": f"0/{len(pages)} 頁",
        }

    threading.Thread(target=run_analysis, args=(pid, pages), daemon=True).start()
    return {"project_id": pid, "status": "running", "pages": len(pages)}


@app.get("/api/status/{pid}")
async def api_status(pid: str):
    with LOCK:
        if pid not in STORE:
            raise HTTPException(status_code=404, detail="專案不存在或已過期")
        p = STORE[pid]
    return {k: p.get(k) for k in ("status", "progress", "error")}


@app.get("/api/project/{pid}")
async def api_project(pid: str):
    with LOCK:
        if pid not in STORE:
            raise HTTPException(status_code=404, detail="專案不存在或已過期")
        return STORE[pid]["result"]


@app.post("/api/review")
async def api_review(
    image: UploadFile = File(...),
    target_prompt: str = Form(...),
    characters: str = Form(""),
    shot: str = Form(""),
    extra: str = Form(""),
):
    """上傳生成的分鏡圖 → 視覺審查 → 重寫提示詞。"""
    raw = image.file.read()
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"不是有效圖片：{e}")

    b64 = encode_image(img, REVIEW_MAX_EDGE)

    blocks = [
        f"【目標生圖提示詞】\n{target_prompt}",
    ]
    if characters:
        blocks.append(f"【角色設定（比對一致性的基準，必須逐字符合）】\n{characters}")
    if shot:
        blocks.append(f"【此鏡頭的分鏡資料】\n{shot}")
    if extra:
        blocks.append(f"【使用者補充說明】\n{extra}")

    blocks.append("請審查下方這張圖，輸出 JSON 審查結果。")

    txt = chat(
        [{
            "role": "user",
            "content": [{"type": "text", "text": "\n\n".join(blocks)}]
            + [{"type": "image_url", "image_url": {"url": data_url(b64)}}],
        }],
        temperature=0.2,
        max_tokens=3000,
        schema=SCHEMA_REVIEW,
        no_think=True,
    )
    result = parse_json_loose(txt)
    if not isinstance(result, dict) or "verdict" not in result:
        return {
            "verdict": "NEEDS_FIX",
            "score": 0,
            "observation": "模型未回傳可解析的 JSON，可能輸出過長被截斷",
            "_enforced": False,
            "issues": [{
                "severity": "BLOCKER",
                "category": "輸出格式",
                "observation": "結構化輸出失敗",
                "fix": "請重試，或縮短角色設定描述",
            }],
            "rewritten_prompt": target_prompt,
            "negative_prompt": "",
            "raw": txt[:2000],
        }

    result.setdefault("rewritten_prompt", target_prompt)
    result = _enforce_verdict(result)
    return result


# ─────────────────────────────────────────────────────────────
# 規則層複核（保守版）
#
# 實測紀錄：
#   1. 原本用「專職找錯」框架 → 模型過度挑毛病（六根手指判 PASS 是反例，
#      但正常圖也會被誤判）
#   2. 改成「中性觀察 + 獨立比對」 → 缺陷圖能自己判 NEEDS_FIX ✅，
#      正常圖能自己判 PASS ✅，分數合理
#   3. 規則層用 regex 補救 → 實測發現誤判率反而上升（合成案例來回盪）
#
# 結論：中性框架已解決根因，規則層不再做內容判斷，
#      僅保留「觀察與判定直接矛盾」一種明確情況。
# ─────────────────────────────────────────────────────────────


def _enforce_verdict(res: dict) -> dict:
    """僅處理明顯自相矛盾的情況，不預設判定方向。"""
    res["_enforced"] = False
    res.setdefault("_notes", [])

    obs = str(res.get("observation", ""))
    issues = res.get("issues") or []
    blockers = [i for i in issues
                if str(i.get("severity", "")).upper() == "BLOCKER"]

    # 只有這一種明確矛盾：模型自己列了 BLOCKER，卻判 PASS
    if res.get("verdict") == "PASS" and blockers:
        res["verdict"] = "NEEDS_FIX"
        res["_enforced"] = True
        res["_notes"].append(
            f"模型列出 {len(blockers)} 項 BLOCKER，判定卻為 PASS")
        res.setdefault("issues", []).insert(0, {
            "severity": "MINOR",
            "category": "自動複核",
            "observation": "；".join(res["_notes"]),
            "fix": "判定與問題清單不一致，已由規則層改為 NEEDS_FIX。",
        })

    sc = int(res.get("score") or 0)
    if res["verdict"] == "NEEDS_FIX" and sc > 80:
        res["score"] = 80
    if res["verdict"] == "PASS" and sc < 50:
        res["score"] = 50
    return res


@app.get("/api/projects")
async def api_projects():
    """列出所有已存檔的專案。"""
    with LOCK:
        items = sorted(PROJECT_INDEX.items(),
                       key=lambda kv: -kv[1]["done_at"])
    return {"projects": [{"id": pid, **meta} for pid, meta in items]}


@app.get("/api/export/{pid}")
async def api_export(pid: str, fmt: str = "json"):
    """匯出分鏡結果：json / markdown / csv。"""
    with LOCK:
        if pid not in STORE or not STORE[pid].get("result"):
            raise HTTPException(status_code=404, detail="專案不存在")
        d = STORE[pid]["result"]

    if fmt == "json":
        return JSONResponse(d, headers={
            "Content-Disposition": f'attachment; filename="{pid}.json"'})

    if fmt == "csv":
        import csv
        import io as _io
        buf = _io.StringIO()
        cols = ["id", "page", "duration", "shot_size", "camera", "action",
                "dialogue", "sfx", "prompt", "negative_prompt", "video_prompt"]
        w = csv.writer(buf)
        w.writerow(cols)
        for sh in d.get("shots", []):
            w.writerow([sh.get(c, "") for c in cols])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{pid}.csv"'})

    if fmt == "md":
        L = [f"# {d.get('title','未命名')}", "",
             f"> {d.get('logline','')}", "",
             f"預估長度：{d.get('estimated_duration','?')}　｜　"
             f"鏡頭數：{len(d.get('shots',[]))}", "",
             "## 全域畫風", "", f"`{d.get('global_style','')}`", ""]
        if d.get("art_bible"):
            L += ["## 全域美術基準", "", d["art_bible"], "", "---", ""]
        L += ["## 角色設定檔", ""]
        for c in d.get("characters", []):
            L += [f"### {c.get('id')} {c.get('name','')}", "",
                  f"- 外觀（逐字不可改）：`{c.get('appearance_en','')}`",
                  f"- 性格：{c.get('personality','')}", ""]
        L += ["## 場景", ""]
        for sc in d.get("scenes", []):
            L += [f"- **{sc.get('id')}** {sc.get('name','')}：{sc.get('description_en','')}", ""]
        L += ["## 逐鏡描述", ""]
        for sh in d.get("shots", []):
            L += [f"### {sh.get('id')}　`{sh.get('duration','')}`　"
                  f"{sh.get('shot_size','')} / {sh.get('camera','')}", "",
                  f"**動作**：{sh.get('action','')}"]
            if sh.get("dialogue"):
                L.append(f"**對白**：「{sh['dialogue']}」")
            if sh.get("sfx"):
                L.append(f"**音效**：{sh['sfx']}")
            L += ["", "**生圖提示詞**：", "```", sh.get("prompt", ""), "```", "",
                  "**負面提示詞**：", "```", sh.get("negative_prompt", ""), "```", "",
                  "**動態化**：", "```", sh.get("video_prompt", ""), "```", "", "---", ""]
        return Response(chr(10).join(L), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{pid}.md"'})

    raise HTTPException(status_code=400, detail="fmt 只支援 json / md / csv")


@app.delete("/api/project/{pid}")
async def api_delete_project(pid: str):
    with LOCK:
        STORE.pop(pid, None)
        PROJECT_INDEX.pop(pid, None)
    try:
        p = os.path.join(PROJECTS_DIR, f"{pid}.json")
        if os.path.exists(p):
            os.remove(p)
    except Exception:  # noqa: BLE001
        pass
    return {"deleted": pid}



def enforce_sequence(shots: list, characters: list, scenes: list) -> list:
    """把逐格對應的鏡頭重組成真正的鏡頭序列。

    漫畫分格是排版結果，不是導演意圖。本函式強制套用影片的敘事節奏：
      1. 連續且同場景的鏡頭合併（景別接近時尤其容易是同一件事）
      2. 開頭若為場景建立鏡頭，拉長
      3. 中段連續 close-up 合併，避免 2 秒一切
      4. 給每個序列鏡頭分配合理的運鏡與時長
      5. 若合併後仍過短，延長而非增加切點
    """
    import re as _re
    if not shots:
        return shots

    def size_of(sh):
        t = str(sh.get("shot_size", "")).lower()
        for k, v in (("extreme", 1), ("establishing", 5), ("wide", 4),
                     ("close-up", 1), ("close", 1), ("medium", 3),
                     ("full", 4), ("body", 3), ("object", 3)):
            if k in t:
                return v
        return 3

    def sec_of(sh):
        m = _re.search(r"([\d.]+)", str(sh.get("duration", "")))
        return float(m.group(1)) if m else 3.0

    # ── 1) 合併相鄰鏡頭 ──
    # 合併條件：同場景 且 景別相近 且 合併後總長不超過 8 秒
    merged = []
    for sh in shots:
        if merged:
            prev = merged[-1]
            same_scene = (sh.get("scene_id") == prev.get("scene_id"))
            similar = abs(size_of(sh) - size_of(prev)) <= 1
            combined = sec_of(prev) + sec_of(sh)
            # 對白不能硬塞進同一鏡頭（會塞不下）
            no_dialogue_conflict = not (sh.get("dialogue") and prev.get("dialogue"))
            if same_scene and similar and combined <= 8.0 and no_dialogue_conflict:
                prev["action"] = (
                    f"{prev.get('action','')} / 續：{sh.get('action','')}"
                ).strip(" /")
                prev["duration"] = f"{round(combined,1)}s"
                if sh.get("dialogue"):
                    prev["dialogue"] = sh["dialogue"]
                if sh.get("sfx"):
                    prev["sfx"] = (prev.get("sfx", "") + " " + sh["sfx"]).strip()
                # 合併後的景別取較寬的（保留更大資訊量）
                if size_of(sh) > size_of(prev):
                    prev["shot_size"] = sh["shot_size"]
                prev["_merged"] = prev.get("_merged", 1) + 1
                continue
        merged.append(dict(sh))

    # ── 2) 分配運鏡與時長 ──
    n = len(merged)
    for i, sh in enumerate(merged):
        size = size_of(sh)
        first = (i == 0)
        last = (i == n - 1)

        # 運鏡：避免整片都是 static
        cam = str(sh.get("camera", "")).lower()
        if "static" in cam or not cam or "static" in cam:
            if first and size >= 4:
                sh["camera"] = "establishing shot, slow push in"
            elif size >= 4:
                sh["camera"] = "slow pan right"
            elif last:
                sh["camera"] = "slow pull out"
            else:
                sh["camera"] = "subtle slow push in"

        # 時長：鏡頭序列的節奏
        cur = sec_of(sh)
        if first and size >= 4:
            floor = 4.0          # 開場建立鏡頭給足
        elif last:
            floor = 4.0          # 結尾留餘韻
        elif sh.get("dialogue"):
            # 對白長度決定時長（約 0.35 秒/字）
            need = len(str(sh["dialogue"])) * 0.4 + 1.0
            floor = max(3.0, need)
        else:
            floor = 3.0          # 一般鏡頭不短於 3 秒
        sh["duration"] = f"{round(max(cur, floor),1)}s"

        # 清理合併標記
        sh.pop("_merged", None)

    # ── 3) 重編號 ──
    for i, sh in enumerate(merged, 1):
        sh["id"] = f"SH{i:02d}"

    return merged


def build_art_bible(d: dict) -> str:
    """把分鏡結果提煉成一份全局美術基準文件。"""
    chars = "\n".join(
        f"- {c.get('id')} {c.get('name','')}（{c.get('role','')}）：{c.get('appearance_en','')}"
        for c in d.get("characters", [])) or "（無）"
    scenes = "\n".join(
        f"- {sc.get('id')} {sc.get('name','')}：{sc.get('description_en','')}"
        for sc in d.get("scenes", [])) or "（無）"
    shots = "\n".join(
        f"- {sh.get('id')}（{sh.get('duration','')}｜{sh.get('shot_size','')}｜"
        f"{sh.get('camera','')}｜情緒：{sh.get('mood','')}）"
        f"\n  動作：{sh.get('action','')}"
        + (f"\n  對白：「{sh['dialogue']}」" if sh.get("dialogue") else "")
        for sh in d.get("shots", []))

    prompt = PROMPT_SUMMARY.format(
        title=d.get("title") or "（未定名）",
        logline=d.get("logline") or "（未提供）",
        duration=d.get("estimated_duration") or "（未估算）",
        style=d.get("global_style") or "（未指定）",
        characters=chars, scenes=scenes, shots=shots or "（無）")

    txt = chat([{"role": "user", "content": prompt}],
               temperature=0.35, max_tokens=4000)
    return txt.strip()


# ── 自我修復端點 ──

@app.post("/api/repair")
async def api_repair(
    prompt: str = Form(...),
    symptom: str = Form(...),
    characters: str = Form(""),
    shot_id: str = Form(""),
    image: Optional[UploadFile] = File(None),
):
    """使用者描述問題 → 根因分析 → 提示詞修補 → 寫入記憶。"""
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="缺少提示詞")
    if not symptom.strip():
        raise HTTPException(status_code=400, detail="請描述發現的問題")

    matched = match_causes(symptom)
    rules_text = "\n".join(
        f"- [{r['id']}] 觸發詞：{'、'.join(r['trigger'])}\n"
        f"  根因：{r['cause']}\n"
        f"  修法：{r['fix_prompt']}"
        for r in CAUSE_RULES
    )
    history = memory_context_for(prompt, shot_id)

    parts = [{
        "type": "text",
        "text": (
            f"【已知根因規則庫】\n{rules_text}\n\n"
            f"【使用的提示詞】\n{prompt}\n\n"
            f"【角色設定】\n{characters or '（未提供）'}\n\n"
            f"【使用者報告的問題】\n{symptom}\n\n"
            f"【其他歷史記錄】\n{history}\n\n"
            f"請依系統指示完成根因分析與提示詞修補。"
        ),
    }]
    if image:
        try:
            img = Image.open(io.BytesIO(image.file.read())).convert("RGB")
            parts.append({"type": "text", "text": "\n（附上實際生成的圖，"
                                                 "若有與你判斷衝突之處，以使用者報告為準）"})
            parts.append({"type": "image_url",
                          "image_url": {"url": data_url(encode_image(img))}})
        except Exception:  # noqa: BLE001
            pass

    err = ""
    try:
        raw = chat([{"role": "user", "content": parts}],
                   temperature=0.3, max_tokens=3500, schema=SCHEMA_DIAGNOSE)
        diag = parse_json_loose(raw)
    except Exception as e:  # noqa: BLE001
        diag, err = None, str(e)

    if not isinstance(diag, dict) or "rewritten_prompt" not in diag:
        if not matched:
            raise HTTPException(status_code=502,
                                detail=f"模型診斷失敗且規則庫未命中：{err}")
        r = matched[0]
        diag = {
            "primary_cause": {"category": r["id"], "explanation": r["cause"],
                              "evidence": "由規則庫依症狀關鍵詞命中"},
            "matched_rule": r["id"],
            "rewritten_prompt": prompt,
            "negative_prompt": r["fix_negative"],
            "changes": [r["fix_prompt"]] + ([r["alt"]] if r.get("alt") else []),
            "residual_risk": "需重新生成後由你確認",
            "prevention": r["fix_prompt"],
            "_fallback": True,
        }

    if matched and not diag.get("_fallback"):
        rid = diag.get("matched_rule")
        rule = next((r for r in CAUSE_RULES if r["id"] == rid), None) or matched[0]
        neg = diag.get("negative_prompt", "")
        for token in rule["fix_negative"].split(", "):
            if token.lower() not in neg.lower():
                neg = (neg + ", " + token).strip(", ")
        diag["negative_prompt"] = neg
        if not diag.get("prevention"):
            diag["prevention"] = rule["fix_prompt"]
        diag["_rule_applied"] = rule["id"]

    record_repair(_norm_key(prompt), shot_id, symptom, diag,
                  diag["rewritten_prompt"])
    return diag


@app.get("/api/memory")
async def api_memory():
    lessons = []
    for sk, L in sorted(PATTERN_LESSONS.items(),
                        key=lambda kv: -kv[1]["times"])[:20]:
        lessons.append({"symptom": sk, "times": L["times"],
                        "causes": L["causes"], "fixes": L["fixes"]})
    recent = sorted(REPAIR_MEMORY.values(), key=lambda r: -r["ts"])[:30]
    return {
        "total_repairs": sum(r["hits"] for r in REPAIR_MEMORY.values()),
        "distinct_prompts": len(REPAIR_MEMORY),
        "lessons": lessons, "recent": recent,
        "cause_rules": [{"id": r["id"], "trigger": r["trigger"],
                         "cause": r["cause"]} for r in CAUSE_RULES],
    }


@app.get("/api/lessons")
async def api_lessons():
    return {"digest": lessons_digest(), "has_memory": bool(PATTERN_LESSONS)}


@app.delete("/api/memory")
async def api_clear_memory():
    global REPAIR_MEMORY, PATTERN_LESSONS
    REPAIR_MEMORY, PATTERN_LESSONS = {}, {}
    save_memory()
    return {"cleared": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    load_memory()
    load_projects()
    import uvicorn
    # 綁 0.0.0.0 讓 Tailscale 網段可存取；8081 的 vLLM 不受影響
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8090")), log_level="warning")
