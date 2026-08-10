#!/usr/bin/env python3
"""generate_captions_oldstyle.py — สร้างแคปชั่นตามรูปแบบเดิม:
ดวงของชาววัน[เกิด] ประจำวันที่ [D] [เดือนเต็ม] [พ.ศ.]
เนื้อหา (จาก prediction)
#ดวงชาววันX #ดูดวงDเดือนพศ + 3 แท็กจากเนื้อหา
"""
import json, os, re
from datetime import datetime

DAY_TH = {"sun":"อาทิตย์","mon":"จันทร์","tue":"อังคาร","wed":"พุธ","thu":"พฤหัสบดี","fri":"ศุกร์","sat":"เสาร์"}
MONTH_TH = {
    1:"มกราคม",2:"กุมภาพันธ์",3:"มีนาคม",4:"เมษายน",5:"พฤษภาคม",6:"มิถุนายน",
    7:"กรกฎาคม",8:"สิงหาคม",9:"กันยายน",10:"ตุลาคม",11:"พฤศจิกายน",12:"ธันวาคม"
}

def extract_keywords(text):
    # Simple: pick meaningful Thai words (length>2) excluding common words
    stop = set(["ของ","ชาว","วัน","ประจำ","วันที่","ดวง","กับ","และ","หรือ","ไม่","จะ","เป็น","ใน","มี","ให้","ได้","ไป","มา","จาก","เพื่อ","แต่","ถ้า","แล้ว","แต่","อย่าง","นี้","นั้น","ใด","ไหน","ใคร","อะไร","ทั้ง","ทุก","บาง","หลาย","มาก","น้อย","ดี","ชั่ว","ใหญ่","เล็ก","เร็ว","ช้า","สูง","ต่ำ","ไกล","ใกล้","ซ้าย","ขวา","ขึ้น","ลง","เข้า","ออก","บน","ล่าง","ซ้าย","ขวา"])
    words = re.findall(r'[\u0e00-\u0e7f]+', text)
    keywords = [w for w in words if len(w) > 2 and w not in stop]
    # deduplicate preserving order
    seen=set(); uniq=[]
    for w in keywords:
        if w not in seen:
            seen.add(w); uniq.append(w)
    return uniq[:3]

def generate_caption(pred, date_str):
    y, m, d = map(int, date_str.split('-'))
    day_th = DAY_TH[pred["day"]]
    date_th = f"{d} {MONTH_TH[m]} {y+543}"  # พ.ศ.
    # Start with required format
    lines = []
    lines.append(f"ดวงของชาววัน{day_th}")
    lines.append(f"ประจำวันที่ {date_th}")
    lines.append("")  # blank line
    # Prediction text from pred['prediction']
    lines.append(pred["prediction"].strip())
    lines.append("")
    # Hashtags
    day_tag = f"#ดวงชาววัน{day_th}"
    date_tag = f"#ดูดวง{d}{MONTH_TH[m][:3]}{y+543}"  # e.g., #ดูดวง11สิงห์2569
    # Extract 3 keywords from prediction
    kws = extract_keywords(pred["prediction"])
    tags = [day_tag, date_tag] + [f"#{kw}" for kw in kws]
    # Ensure exactly 5 hashtags
    if len(tags) > 5:
        tags = tags[:5]
    elif len(tags) < 5:
        # pad with generic
        while len(tags) < 5:
            tags.append("#ดูดวง")
    lines.append(" ".join(tags))
    return "\n".join(lines)

if __name__ == "__main__":
    import sys, json
    with open("output/2026-08-11/predictions/all.json") as f:
        all_pred = json.load(f)
    date_str = "2026-08-11"
    out_dir = "output/2026-08-11/captions"
    os.makedirs(out_dir, exist_ok=True)
    for day_key in ["sun","mon","tue","wed","thu","fri","sat"]:
        pred = all_pred[day_key]
        caption = generate_caption(pred, date_str)
        out_path = os.path.join(out_dir, f"{day_key}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({
                "day": day_key,
                "day_th": pred["day_th"],
                "caption": caption,
                "hashtags": caption.split()[-5:]  # last 5 tokens are hashtags
            }, f, ensure_ascii=False, indent=2)
        print(f"��✓ {pred['day_th']}: {len(caption)} chars")