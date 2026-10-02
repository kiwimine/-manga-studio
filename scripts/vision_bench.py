#!/usr/bin/env python3
"""
視覺能力基準測試
對任意 vLLM 端點跑同一組測試，用於換模型前後對比。

用法:
    python3 vision_bench.py http://127.0.0.1:8081 qwen3.8-27b-abliterated-mtp
    python3 vision_bench.py http://127.0.0.1:8082 qwen3vl
"""
import base64
import io
import json
import sys
import urllib.request

from PIL import Image, ImageDraw

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "qwen3.8-27b-abliterated-mtp"


def ask(prompt, img_bytes=None, max_tokens=200, timeout=300):
    content = [{"type": "text", "text": prompt}]
    if img_bytes:
        b64 = base64.b64encode(img_bytes).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    body = {"model": MODEL, "messages": [{"role": "user", "content": content}],
            "temperature": 0.0, "max_tokens": max_tokens}
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=timeout))
        return r["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"ERR: {e}"


# ── 測試圖 ──────────────────────────────────────────────
def t_read_text():
    """T1 讀文字：模型把 CIRCLE 讀成什麼"""
    img = Image.new("RGB", (400, 150), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 60), "CIRCLE:", fill="black")
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue(), "T1 讀文字", "圖上寫了什麼字？只回答文字內容，不要解釋。", "CIRCLE"


def t_fingers():
    """T2 數手指：明顯畫六根手指"""
    img = Image.new("RGB", (500, 500), (250, 240, 230))
    d = ImageDraw.Draw(img)
    d.ellipse((180, 120, 340, 300), fill=(240, 200, 170), outline=(60, 40, 30), width=3)
    d.ellipse((225, 200, 240, 215), fill=(20, 20, 20))
    d.ellipse((280, 200, 295, 215), fill=(20, 20, 20))
    d.rectangle((150, 300, 370, 420), fill=(40, 60, 120))
    for i in range(6):                      # 六根手指
        d.line((230 + i * 14, 420, 230 + i * 14, 465), fill=(240, 200, 170), width=9)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue(), "T2 數手指", "畫面下方的手有幾根手指？只回答數字。", "5（正確答案）"


def t_missing():
    """T3 細節比對：提示詞要求徽章，圖上沒有"""
    img = Image.new("RGB", (400, 500), (245, 240, 235))
    d = ImageDraw.Draw(img)
    d.ellipse((140, 80, 260, 220), fill=(240, 200, 170), outline=(60, 40, 30), width=3)
    d.rectangle((110, 220, 290, 460), fill=(30, 50, 110))   # 制服但無徽章
    d.rectangle((20, 20, 90, 45), fill=(200, 30, 30))
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue(), "T3 細節比對", "這個人的制服上有沒有任何徽章或標誌？只回答「有」或「沒有」。", "沒有"


def t_sfx():
    """T4 漫畫元素：數分格"""
    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    for r in range(2):
        for c in range(3):
            x0, y0 = 20 + c * 190, 20 + r * 190
            d.rectangle((x0, y0, x0 + 175, y0 + 175),
                        fill=(240 - r * 20, 235, 230), outline=(30, 30, 30), width=3)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue(), "T4 漫畫分格", "這張圖有幾個方格（分隔框）？只回答數字。", "6"


def t_colors():
    """T5 基本辨識：對照組（已知能過）"""
    img = Image.new("RGB", (400, 200), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 180, 180), fill=(255, 0, 0))
    d.ellipse((205, 60, 265, 120), fill=(0, 0, 255))
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue(), "T5 基本辨識", "圖中有哪些顏色的形狀？簡短回答。", "紅色方形、藍色圓形"


TESTS = [t_read_text, t_fingers, t_missing, t_sfx, t_colors]

print(f"端點：{BASE}")
print(f"模型：{MODEL}")
print("=" * 60)
results = []
for fn in TESTS:
    img, name, prompt, expect = fn()
    ans = ask(prompt, img)
    print(f"\n【{name}】")
    print(f"  問題  : {prompt}")
    print(f"  期望  : {expect}")
    print(f"  回答  : {ans[:300]}")
    results.append((name, expect, ans))

print("\n" + "=" * 60)
print("結果摘要（請人工判定，模型自評不可靠）")
for n, e, a in results:
    print(f"  {n}: 期望={e} | 實際={a[:80]}")
