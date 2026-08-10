#!/usr/bin/env python3
"""generate_captions_friendly.py — แคปชั่นเรื่งสบายๆ เหมาะกับ Gen Z + Gen Y"""
import json, re, os

DAY_TH = {"sun":"อาทิตย์","mon":"จันทร์","tue":"อังคาร","wed":"พุธ","thu":"พฤหัสบดี","fri":"ศุกร์","sat":"เสาร์"}
MONTH_TH = {1:"มกราคม",2:"กุมภาพันธ์",3:"มีนาคม",4:"เมษายน",5:"พฤษภาคม",6:"มิถุนายน",7:"กรกฎาคม",8:"สิงหาคม",9:"กันยายน",10:"ตุลาคม",11:"พฤศจิกายน",12:"ธันวาคม"}
FRIENDLY_FILLERS = ["omggg","lol","omygod","om","soo","very","so","super","extra","like","u know","honestly","tbh","fr fr","no cap","real talk"]

def friendly_prediction(pred, y, m, d):
    """สร้างข้อความทำนายแบบ friendly"""
    day = pred["day_th"]
    date = f"{d} {MONTH_TH[m]} {y+543}"
    ruler = pred["ruler_th"]
    sign = pred["ruler_sign"]
    house = pred["ruler_house"]
    sc = pred["score"]
    
    # Build friendly text
    lines = []
    lines.append(f"❤️ ดวงของชาววัน{day}")
    lines.append(f" 📅 ประจำวันที่ {date}")
    lines.append("")
    
    # Ruler intro
    if pred["ruler_retrograde"]:
        lines.append(f"วันนี้ ดวงปา {ruler} มันถอยหลังแล้ว ใจๆ เตรียมรับไว้นะ!")
    else:
        lines.append(f"วันนี้ {ruler} กำลังมาวิเคราะห์ชีวิตเราให้เต็มที่เลย 🫡")
    
    # Dignity
    if "เกษตร" in str(pred["ruler_dignity"]):
        lines.append(f"อยู่ในตำแหน่งเกษตร = มีโอกาสสูง แต่อย่าเสียใจนะ!")
    elif "นิจ" in str(pred["ruler_dignity"]):
        lines.append(f"อยู่ในติจ = วันนี้อาจเหนื่อยหน่อย ต้องคอทำนี่ๆ ด้วย")
    
    # Aspects (just the good ones)
    if pred["aspects"]:
        lines.append("")
        lines.append("มุมดีๆ:")
        for a in pred["aspects"][:3]:
            meaning = a["meaning"]
            planet = a["planet_th"]
            if meaning in ["หนุนนำ", "เกื้อหนุน"]:
                lines.append(f"  💫 {planet} มาเป็น {meaning}เต็มที่ อย่าพลาด!")
            elif meaning in ["ปะทะ", "กดดัน"]:
                lines.append(f"  ⚠️ ระวัง {planet} มากับ {meaning} อย่าเกินมา")
            else:
                lines.append(f"  • {planet} มีมุม {meaning}")

    # Score summary
    if sc > 0:
        lines.append("")
        lines.append(f"🔮 สรุป: {day} นี้มาก่อนที่มา มี energy ดีๆ  flowing ✨")
    elif sc < 0:
        lines.append("")
        lines.append(f"🔮 สรุป: {day} นี้อาจมีอะไรบางอย่างซับซ้อน แต่ไม่ต้องกังวล")
    else:
        lines.append("")
        lines.append(f"🔮 สรุป: {day} มี energy ครบ ระดับ normal 😎")
    
    return "\n".join(lines)

def make_hashtags(day, date, content):
    day_tag = f"#ดวงชาววัน{day}"
    date_tag = f"#ดูดวง{date[0]}{MONTH_TH[1][:3]}2569"  # e.g., #ดูดวง11สิง2569
    # Pick 3 keywords from content
    words = [w.strip("[:;,]") for w in content.split() if len(w) > 2 and w not in ["วัน", "วันนี้", "ไหม", "ละ", "แล้ว", "กับ", "แต่", "ใน", "จาก"]]
    tags = [day_tag, date_tag] + [f"#{w}" for w in words[:3]]
    return tags[:5]  # exactly 5

if __name__ == "__main__":
    with open("/var/www/star/output/2026-08-11/predictions/all.json") as f:
        all_pred = json.load(f)
    
    date_str = "2026-08-11"
    y, m, d = 2026, 8, 11
    date_text = f"{d} {MONTH_TH[m]} {y+543}"
    
    os.makedirs("output/2026-08-11/captions", exist_ok=True)
    
    for key in ["sun","mon","tue","wed","thu","fri","sat"]:
        pred = all_pred[key]
        caption = friendly_prediction(pred, y, m, d)
        hashtags = make_hashtags(pred["day_th"], f"{d}{MONTH_TH[m][:3]}", caption)
        
        result = {
            "day": key,
            "day_th": pred["day_th"],
            "caption": caption,
            "hashtags": hashtags
        }
        
        out_path = f"output/2026-08-11/captions/{key}.json"
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        print(f"✓ {pred['day_th']}")
        print(f"  Caption: {caption[:80]}...")
        print(f"  Tags: {' '.join(hashtags)}")
        print()
    
    print("✅ Done!")