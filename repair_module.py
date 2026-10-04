# ─────────────────────────────────────────────────────────────
# 自我修復引擎：根因分析 + 提示詞修補 + 記憶累積
# ─────────────────────────────────────────────────────────────

# 根因分類庫：把「症狀」對應到「提示詞該怎麼改」
# 這是規則層，用來約束模型的自由發揮，並在模型失敗時仍能運作。

CAUSE_RULES = [
    {
        "id": "R-SEED-DRIFT",
        "trigger": ["每格都不一樣", "每格長得都不一樣", "風格不統一", "不同鏡頭畫風不同", "整體風格不一致", "畫風跟其他格不一致", "風格跑了", "格與格之間風格"],
        "cause": "生成時未固定 seed，且未在每則提示詞中重申同一組風格關鍵詞。"
                 "生圖模型每次取樣獨立，同一角色在不同次呼叫會落到不同的風格區間。",
        "fix_prompt": "Reuse the SAME seed across all shots of this character. "
                      "Repeat the identical style tag verbatim at the end of every prompt.",
        "fix_negative": "varying art styles, inconsistent line weight, mixed rendering techniques",
        "note": "角色外觀不在此層負責，seed 固定只影響風格層級。",
    },
    {
        "id": "R-CHAR-DRIFT",
        "trigger": ["角色長得不像", "每格長得都不一樣", "每格是不同的人", "角色走樣", "臉變了", "髮色不對", "服裝不一致", "換個人", "角色不像同一人", "角色每次都變", "臉不像", "服裝變了", "換了衣服", "不是同一個人", "角色外觀不對", "角色漂移", "身份對不上"],
        "cause": "本系統採用角色位（cast slot）架構：外觀由後期參考圖（reference）負責，"
                 "分鏡層刻意不描述任何外貌。因此角色走樣**不是提示詞的問題**，"
                 "而是參考圖未正確對應到 character_ids 所指定的角色位。",
        "fix_prompt": "Character appearance is owned by the reference images and must NOT be "
                      "described here. Make the cast reference explicit and unambiguous in the "
                      "action text instead: name the slot at the start of the action "
                      "('女1 turns toward the window'), so the right reference image is bound "
                      "to the right slot. Never add hair/eye/clothing/height descriptions.",
        "fix_negative": "identity drift, inconsistent character reference, "
                        "mismatched reference binding, extra characters in frame",
        "note": "本架構下 R-CHAR-DRIFT 的正確處置是檢查 character_ids 與參考圖的對應，"
                "而不是在提示詞中補外觀描述。若要改變角色外觀，請更換參考圖。",
    },
    {
        "id": "R-HAND-DEFECT",
        "trigger": ["手指", "手畫錯", "手畸形", "多一隻手", "六根指", "手部不對", "手變形", "手很怪", "手變成六根手指", "手指不對", "手部畫錯", "六根手指"],
        "cause": "提示詞未對手部給出具體的正向描述。生圖模型對手部的預設生成品質低，"
                 "未指定時容易產生多餘指節或融合。若手部非敘事重點，"
                 "最佳解不是硬修，而是讓構圖避開手部。",
        "fix_prompt": "Hands clearly visible and anatomically correct, exactly five fingers per hand, "
                      "natural finger spacing, defined knuckles.",
        "fix_negative": "extra fingers, fused fingers, missing fingers, six fingers, "
                        "malformed hands, mutated hands",
        "alt": "若手部非必要，可改為 medium shot 讓手部離開畫面中央，"
               "或加入 hands in pockets / hands behind back 降低生成難度。",
    },
    {
        "id": "R-COMPOSITION",
        "trigger": ["構圖不對", "主體不對", "太小", "太大", "出框", "被裁切", "沒拍到", "景別不對", "位置不對", "主體太小", "構圖偏", "角色太小", "拍不到", "主體跑掉"],
        "cause": "提示詞中的景別詞過於抽象（只寫 close-up 沒寫畫面佔比），"
                 "或缺少主體在畫面中的位置指示。生圖模型會依文本統計分布決定構圖。",
        "fix_prompt": "Explicitly state the subject's position in frame and the framing ratio, "
                      "e.g. 'the character occupies the central 60% of the frame', "
                      "'head and shoulders visible', 'full body in frame with margins'.",
        "fix_negative": "cropped head, subject cut off, off-center composition, "
                        "excessive empty space, extreme close-up",
    },
    {
        "id": "R-POSE",
        "trigger": ["姿勢怪", "動作不自然", "動作畫不出來", "動作不對", "姿勢不對", "扭曲", "動作很怪", "姿勢怪怪的"],
        "cause": "提示詞一次描述了多個連續動作。生圖模型是靜態取樣器，"
                 "無法呈現動作序列，會把多動作混雜成一個不合理的姿勢。",
        "fix_prompt": "Describe exactly ONE static pose. Use a single verb in present tense. "
                      "Avoid chained actions ('walking and turning and reaching').",
        "fix_negative": "unnatural pose, contorted body, twisted torso, ambiguous limb placement",
    },
    {
        "id": "R-TEXT-ARTIFACT",
        "trigger": ["有字", "亂碼", "文字", "浮水印", "奇怪的字", "殘留文字", "殘留字", "多了字", "出現文字", "有浮水印"],
        "cause": "提示詞含可被渲染成文字的字串（例如具體人名、地名），"
                 "或負面提示詞中出現 text / watermark 等詞，"
                 "生圖模型有時會把它們畫出來。",
        "fix_prompt": "Remove all text-like tokens from the prompt. Use cast slot labels "
                      "(女1, 男1, 路人) rather than proper names — slot labels are structural, "
                      "not renderable text. Do not include any word that should "
                      "appear as visible text.",
        "fix_negative": "text, letters, watermark, signature, caption, subtitle, speech bubble",
    },
    {
        "id": "R-LIGHTING",
        "trigger": ["光線", "太暗", "太亮", "曝光", "光源", "陰影不對", "光線太平", "沒有立體感"],
        "cause": "提示詞的光線描述缺乏方向與強度指定。生圖模型會套用平均值照明，"
                 "無法從「自然的午後光線」這類抽象詞推導出可用的光位。",
        "fix_prompt": "State light direction, quality and time explicitly: "
                      "'single warm key light from upper-left at 45 degrees, "
                      "cool ambient fill, soft rim light separating subject from background'.",
        "fix_negative": "flat lighting, harsh mixed light sources, blown highlights, "
                        "crushed shadows, muddy contrast",
    },
    {
        "id": "R-STYLE-MISS",
        "trigger": ["畫風不對", "風格跑了", "不是這個風格", "太寫實", "不夠動漫", "畫風跟其他格不一致", "風格不統一", "太寫實了"],
        "cause": "風格關鍵詞數量不足或過於通用。生圖模型依風格詞的統計權重取樣，"
                 "只給一個泛用詞會落在整體平均風格上。",
        "fix_prompt": "Expand the style tag to 5-8 specific, non-generic descriptors covering "
                      "lineart quality, shading type, color palette, era and medium "
                      "(e.g. 'clean thin lineart, hard-edged cel shading, limited 4-color palette, "
                      "1990s hand-painted anime background, flat matte texture').",
        "fix_negative": "photorealistic, 3d render, oil painting, watercolor, cel-shaded 3d",
    },
    {
        "id": "R-BACKGROUND",
        "trigger": ["背景不對", "場景錯", "背景亂", "多東西", "背景太亂", "背景多出", "多出不該有", "背景亂加東西", "憑空多出"],
        "cause": "提示詞未給背景約束。生圖模型會依訓練分布填入「該場景常見的物件」，"
                 "在你不想要的元素上特別活躍。",
        "fix_prompt": "Add an explicit background clause: 'background: <specific>, "
                      "no additional objects, uncluttered, clean negative space'.",
        "fix_negative": "cluttered background, random objects, crowd, extra furniture, "
                        "distracting details",
    },
]


def match_causes(symptom: str) -> list:
    """依使用者描述的症狀，比對根因規則。"""
    s = symptom.lower()
    hits = []
    for rule in CAUSE_RULES:
        score = sum(1 for t in rule["trigger"] if t.lower() in s)
        if score:
            hits.append((score, rule))
    hits.sort(key=lambda x: -x[0])
    return [r for _, r in hits]


PROMPT_DIAGNOSE = """你是一位資深的 AI 生圖流程調校工程師。

使用者生成分鏡圖後，發現了問題。你要分析**為什麼會造成這個問題**，並修補提示詞。

【已知根因規則庫】以下是由經驗歸納的規則，可能命中也可能不命中：
{rules}

【使用的提示詞】
{prompt}

【角色位（cast slots）】
{characters}
注意：本系統的角色**不描述外觀**。角色位只是位置編號（女1／男1／路人），
外觀由後期參考圖負責。

【使用者報告的問題】
{symptom}

【其他歷史記錄（同一角色／同一鏡頭）】
{history}

【任務】分三步：

**第一步：判定根因。**
先檢查規則庫是否有命中。若有，直接採用並補充說明。
若無，你自己判斷——常見根因包括但不限於：
- 提示詞描述過於抽象，缺乏可視化細節
- 缺少必要的正向描述（模型不描寫未提及的東西）
- 描述了多個動作／多個元素，超出單張圖的承載
- 使用了否定語（「不要模糊」）而非正向語（「清晰銳利」）——多數模型對否定詞效果差
- 關鍵特徵寫在提示詞後段，被前面的長描述稀釋
- 同一提示詞承擔了多重意圖（既要角色一致又要特定構圖）

**第二步：重寫提示詞。**
遵守以下規則：
1. **禁止新增任何外貌描述**——髮色、瞳色、膚色、身高、體型、服裝、配件一律不可寫入。
   本架構的外觀由參考圖負責，寫進提示詞只會與參考圖衝突。
2. 角色一律以角色位指涉（女1／男1／路人），且在動作描述開頭明確點名，確保參考圖對應正確
3. 把否定語改寫為正向描述
4. 每則提示詞只承擔一個主要意圖
5. 關鍵特徵放在**提示詞開頭**（模型對開頭注意力較強）
6. 只改動與本次問題相關的部分，其他部分保持原樣
7. 保持英文
8. 若根因是構圖難以避免（如手部），可提供替代構圖而非硬修

**第三步：預測殘餘風險。**
說明這次修改後，哪些既有問題可能被修復、哪些仍可能存在。

【輸出格式】只輸出 JSON，不要任何解釋文字：
{{
  "primary_cause": {{
    "category": "根因分類",
    "explanation": "為什麼這個問題會發生。要講清楚機制，不要只講現象",
    "evidence": "提示詞中支持此判斷的具體片段"
  }},
  "matched_rule": "命中的規則 id，無則填 null",
  "rewritten_prompt": "修補後的完整提示詞",
  "negative_prompt": "對應的負面提示詞",
  "changes": ["逐條說明改了什麼、為什麼這樣改能解決問題"],
  "residual_risk": "修補後仍可能存在的問題",
  "prevention": "未來如何預防此類問題再次發生（一句話）"
}}
"""

SCHEMA_DIAGNOSE = {
    "type": "object",
    "properties": {
        "primary_cause": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "explanation": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["category", "explanation", "evidence"],
        },
        "matched_rule": {"type": "string"},
        "rewritten_prompt": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "string"}},
        "residual_risk": {"type": "string"},
        "prevention": {"type": "string"},
    },
    "required": ["primary_cause", "matched_rule", "rewritten_prompt",
                 "negative_prompt", "changes", "residual_risk", "prevention"],
}


PROMPT_HISTORY_SUMMARY = """以下是一個 AI 生圖專案的歷史修正記錄。請歸納出**必須遵守的規律**。

【記錄】
{records}

【任務】歸納出 3-6 條可重複套用的規律，每條要具體可執行。
特別注意：哪些提示詞寫法會造成問題、哪些寫法確實有效。
不要複述個別案例，要提煉成通則。

輸出條列式，每條一行開頭寫「- 」。

若記錄不足，請說「記錄不足，暫無可歸納規律」。"""
