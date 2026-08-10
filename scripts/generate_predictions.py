#!/usr/bin/env python3
"""generate_predictions.py — สร้างคำทำนาย 7 วันเกิด จาก final.json"""
import json, os, argparse, sys
from math import floor

ZODIAC_TH = ["เมษ","พฤษภ","เมถุน","กรกฎ","สิงห์","กันย์","ตุลย์","พิจิก","ธนู","มังกร","กุมภ์","มีน"]
HOUSE_TH = {1:"ตนุ",2:"กดุมภะ",3:"สหัชชะ",4:"พันธุ",5:"ปุตตะ",6:"อริ",
            7:"ปัตนิ",8:"มรณะ",9:"ศุภะ",10:"กัมมะ",11:"ลาภะ",12:"วินาศ"}
DAY_NAMES = {"sun":"อาทิตย์","mon":"จันทร์","tue":"อังคาร","wed":"พุธ","thu":"พฤหัสบดี","fri":"ศุกร์","sat":"เสาร์"}
DAY_RULERS = {"sun":"sun","mon":"moon","tue":"mars","wed":"mercury","thu":"jupiter","fri":"venus","sat":"saturn"}
PLANET_TH = {"sun":"อาทิตย์","moon":"จันทร์","mercury":"พุธ","venus":"ศุกร์","mars":"อังคาร","jupiter":"พฤหัสบดี","saturn":"เสาร์","uranus":"ยูเรนัส","neptune":"เนปจูน","pluto":"พลูโต","rahu":"ราหู","ketu":"เกตุ"}

ASPECT_MEANINGS = {
    "conjunction": "รวมพลัง",
    "opposition": "ปะทะ",
    "trine": "หนุนนำ",
    "square": "กดดัน",
    "sextile": "เกื้อหนุน",
    "semi_sextile": "ประสาน",
}

# ─── PREDICTION LOGIC ───
def analyze_day(data, day_key):
    """วิเคราะห์คำทำนายสำหรับวันเกิดหนึ่ง"""
    ruler_key = DAY_RULERS[day_key]
    planets = data["planets"]
    dignities = data.get("dignities", {})
    
    if ruler_key not in planets:
        return {"error": f"Ruler {ruler_key} not found"}
    
    ruler = planets[ruler_key]
    
    # Find aspects to ruler
    aspects = []
    for pkey, pdata in planets.items():
        if pkey == ruler_key: continue
        diff = abs(ruler["longitude"] - pdata["longitude"])
        if diff > 180: diff = 360 - diff
        
        aspect_type = None
        if diff <= 8: aspect_type = ("conjunction", 4)
        elif abs(diff - 60) <= 6: aspect_type = ("sextile", 1)
        elif abs(diff - 90) <= 6: aspect_type = ("square", 3)
        elif abs(diff - 120) <= 6: aspect_type = ("trine", 2)
        elif abs(diff - 180) <= 8: aspect_type = ("opposition", 4)
        elif abs(diff - 30) <= 3: aspect_type = ("semi_sextile", 1)
        
        if aspect_type:
            aspects.append({
                "planet": pkey,
                "planet_th": PLANET_TH.get(pkey, pkey),
                "aspect": aspect_type[0],
                "meaning": ASPECT_MEANINGS.get(aspect_type[0], ""),
                "weight": aspect_type[1],
                "orb": round(diff, 2),
                "planet_sign": pdata["sign"],
                "planet_house": pdata.get("house_name", ""),
            })
    
    # Sort by weight
    aspects.sort(key=lambda x: x["weight"], reverse=True)
    
    # Dignities
    ruler_dignity = dignities.get(ruler_key, ["Peregrine"])
    
    # Score
    score = 0
    for a in aspects:
        if a["aspect"] in ("trine", "sextile"): score += a["weight"]
        elif a["aspect"] in ("square", "opposition"): score -= a["weight"]
    
    # Generate prediction text
    prediction = generate_prediction_text(day_key, ruler, aspects, ruler_dignity, score)
    
    return {
        "day": day_key,
        "day_th": DAY_NAMES[day_key],
        "ruler": ruler_key,
        "ruler_th": PLANET_TH.get(ruler_key, ruler_key),
        "ruler_sign": ruler["sign"],
        "ruler_house": ruler.get("house_name", ""),
        "ruler_dignity": ruler_dignity,
        "ruler_retrograde": ruler["retrograde"],
        "aspects": aspects,
        "score": score,
        "prediction": prediction,
    }

def generate_prediction_text(day_key, ruler, aspects, dignities, score):
    """สร้างข้อความคำทำนาย"""
    parts = []
    day_th = DAY_NAMES[day_key]
    
    # Intro
    parts.append(f"ดวงของชาววัน{day_th}")
    
    # Ruler status
    retro = " (กำลังโคจรถอยหลัง)" if ruler["retrograde"] else ""
    parts.append(f"ดาวประจำวันคือ{PLANET_TH.get(DAY_RULERS[day_key],'')} อยู่ในเรือน{ruler.get('house_name','')} ราศี{ruler['sign']}{retro}")
    
    # Dignities
    if "เกษตร" in str(dignities):
        parts.append("อยู่ในตำแหน่งเกษตร มีความแข็งแกร่ง")
    elif "นิจ" in str(dignities):
        parts.append("อยู่ในตำแหน่งนิจ อาจรู้สึกอ่อนล้า")
    
    # Top aspects
    strong = [a for a in aspects[:3]]
    for a in strong:
        meaning = a["meaning"]
        pth = a["planet_th"]
        if a["aspect"] in ("trine", "sextile"):
            parts.append(f"ได้รับ{meaning}จาก{pth}")
        elif a["aspect"] in ("square", "opposition"):
            parts.append(f"ระวัง{meaning}จาก{pth}")
        elif a["aspect"] == "conjunction":
            parts.append(f"{meaning}กับ{pth}")
    
    # Summary
    if score >= 3:
        parts.append("โดยรวมเป็นวันที่ดี มีโอกาสและพลังงานบวก")
    elif score <= -3:
        parts.append("ควรใช้ความระมัดระวังในการตัดสินใจ")
    else:
        parts.append("เป็นวันที่ต้องใช้ความสมดุล มีทั้งดีและท้าทาย")
    
    return " ".join(parts)

# ─── MAIN ───
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="final.json path")
    parser.add_argument("--output", required=True, help="Output directory")
    args = parser.parse_args()

    with open(args.input) as f:
        data = json.load(f)

    os.makedirs(args.output, exist_ok=True)
    
    all_predictions = {}
    for day_key in ["sun","mon","tue","wed","thu","fri","sat"]:
        pred = analyze_day(data, day_key)
        all_predictions[day_key] = pred
        
        out_path = os.path.join(args.output, f"{day_key}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(pred, f, ensure_ascii=False, indent=2)
        print(f"✓ {pred['day_th']}: score={pred['score']:+d} | {len(pred['aspects'])} aspects")

    # Also save all-in-one
    all_path = os.path.join(args.output, "all.json")
    with open(all_path, 'w', encoding='utf-8') as f:
        json.dump(all_predictions, f, ensure_ascii=False, indent=2)
    print(f"✓ all.json ({len(all_predictions)} days)")
