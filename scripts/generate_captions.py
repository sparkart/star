#!/usr/bin/env python3
"""generate_captions.py — สร้างแคปชั่นและแฮชแท็กจากคำทำนาย 7 วันเกิด"""
import json, os, argparse

DAY_TH = {"sun":"อาทิตย์","mon":"จันทร์","tue":"อังคาร","wed":"พุธ","thu":"พฤหัสบดี","fri":"ศุกร์","sat":"เสาร์"}
MONTH_TH = {1:"มกราคม",2:"กุมภาพันธ์",3:"มีนาคม",4:"เมษายน",5:"พฤษภาคม",6:"มิถุนายน",
            7:"กรกฎาคม",8:"สิงหาคม",9:"กันยายน",10:"ตุลาคม",11:"พฤศจิกายน",12:"ธันวาคม"}

def thai_date(date_str):
    y, m, d = map(int, date_str.split('-'))
    return f"{d} {MONTH_TH[m]} {y + 543}"  # พ.ศ.

def generate_caption(pred, date_str):
    """สร้างแคปชั่น"""
    day_th = pred["day_th"]
    date_th = thai_date(date_str)
    
    lines = []
    lines.append(f"✨ ดวงของชาววัน{day_th}")
    lines.append(f"📅 ประจำวันที่ {date_th}")
    lines.append("")
    lines.append(f"🔮 {pred['prediction']}")
    lines.append("")
    
    # Aspects detail
    if pred["aspects"]:
        lines.append("⚡ มุมเด่น:")
        for a in pred["aspects"][:3]:
            lines.append(f"  • {a['meaning']}กับ{a['planet_th']} ({a['planet_sign']})")
    
    lines.append("")
    lines.append(f"⭐ ดาวประจำวัน: {pred['ruler_th']} ในเรือน{pred['ruler_house']} ราศี{pred['ruler_sign']}")
    if pred["ruler_retrograde"]:
        lines.append("⚠️ ดาวประจำวันกำลังถอยหลัง!")
    
    return "\n".join(lines)

def generate_hashtags(pred, date_str):
    """สร้าง 5 แฮชแท็ก"""
    y, m, d = map(int, date_str.split('-'))
    day_th = DAY_TH.get(pred["day"], pred["day"])
    tags = [
        f"#ดวงชาววัน{day_th}",
        f"#ดูดวง{d}{MONTH_TH[m][:3]}{y+543}",
    ]
    
    # 3 from content keywords
    keywords = []
    for a in pred["aspects"]:
        if a["meaning"] not in keywords:
            keywords.append(a["meaning"])
    for a in pred["aspects"]:
        if a["planet_th"] not in keywords:
            keywords.append(a["planet_th"])
    
    for kw in keywords[:3]:
        tags.append(f"#{kw}{pred['ruler_sign']}")
    
    return tags[:5]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Predictions directory or all.json")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    # Load predictions
    if os.path.isdir(args.input):
        all_path = os.path.join(args.input, "all.json")
    else:
        all_path = args.input
    
    with open(all_path) as f:
        preds = json.load(f)

    os.makedirs(args.output, exist_ok=True)
    
    manifest = {"date": args.date, "captions": {}}
    
    for day_key in ["sun","mon","tue","wed","thu","fri","sat"]:
        pred = preds[day_key]
        caption = generate_caption(pred, args.date)
        hashtags = generate_hashtags(pred, args.date)
        
        result = {
            "day": day_key,
            "day_th": pred["day_th"],
            "caption": caption,
            "hashtags": hashtags,
        }
        
        out_path = os.path.join(args.output, f"{day_key}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        manifest["captions"][day_key] = {
            "file": f"{day_key}.json",
            "hashtags": hashtags,
            "preview": caption[:100] + "...",
        }
        
        print(f"✓ {pred['day_th']}: {len(caption)} chars, {len(hashtags)} hashtags")
    
    # Save manifest
    manifest_path = os.path.join(args.output, "manifest.json")
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"✓ manifest.json")
