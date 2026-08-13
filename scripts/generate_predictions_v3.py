#!/usr/bin/env python3
"""generate_predictions_v3.py — สร้างคำทำนาย 7 วันเกิด จาก astro_v3 JSON ตาม prediction-guide.json

Usage:
  python3 generate_predictions_v3.py --input content/horoscope/astro_v3_2026-08-14.json \
      --output output/predictions/2026-08-14
"""
import argparse
import json
import os
import re
import sys

# ─── ไทย ───
MONTHS_FULL = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
               "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]

DAY_ABBR_TO_TH = {
    "mon": "จันทร์", "tue": "อังคาร", "wed": "พุธ", "thu": "พฤหัสบดี",
    "fri": "ศุกร์", "sat": "เสาร์", "sun": "อาทิตย์",
}

TOPIC_META = {
    "love":          {"label": "ความรัก",       "tag": "ดวงความรัก", "pos": "เรื่องหัวใจมีจังหวะให้เปิดใจและพูดคุยกันได้ดีขึ้น"},
    "work":          {"label": "การงาน",         "tag": "ดวงการงาน",   "pos": "งานมีแนวโน้มเดินหน้าเป็นลำดับ เหมาะกับการจัดลำดับและเคลียร์สิ่งที่ค้าง"},
    "money":         {"label": "การเงิน",         "tag": "ดวงการเงิน",  "pos": "การเงินมีจังหวะให้จัดการได้คล่องขึ้น เหมาะกับการตรวจรายละเอียดและวางแผน"},
    "health":        {"label": "สุขภาพ",          "tag": "ดวงสุขภาพ",   "pos": "ร่างกายมีพลังงานพอใช้ได้ ถ้ารู้สึกเหนื่อยก็ควรพักให้เป็นเวลา"},
    "communication": {"label": "การสื่อสาร",      "tag": "การสื่อสาร",  "pos": "การสื่อสารน่าจะราบรื่น เหมาะกับการคุยเรื่องสำคัญและเคลียร์ความเข้าใจ"},
    "emotion":       {"label": "อารมณ์",          "tag": "อารมณ์วันนี้", "pos": "อารมณ์ค่อนข้างคงที่ ลองหาเวลาอยู่กับสิ่งที่ทำให้ใจชื้น"},
}

TOPIC_CAUTION = {
    "love":          "ความสัมพันธ์อาจมีจุดที่ต้องพูดให้ชัด ลองฟังอีกฝ่ายให้จบก่อนสรุป",
    "work":          "งานมีจุดที่ต้องใช้ความรอบคอบ ควรเผื่อเวลาและตรวจรายละเอียดก่อนส่ง",
    "money":         "เรื่องเงินมีจังหวะให้ระวัง ลองทบทวนรายรับรายจ่ายก่อนตัดสินใจ",
    "health":        "สุขภาพต้องการการดูแลเป็นพิเศษ พักผ่อนให้เพียงพอและสังเกตอาการตัวเอง",
    "communication": "การสื่อสารอาจคลาดเคลื่อนได้ง่าย ลองถามให้ชัดและยืนยันความเข้าใจก่อนสรุป",
    "emotion":       "อารมณ์อาจอ่อนไหวกว่าปกติ ลองจัดการทีละเรื่องและไม่รีบตัดสินใจตอนเหนื่อย",
}

TOPIC_NEUTRAL = {
    "love":          "ความรักเป็นเรื่องที่ค่อย ๆ ดูแลกันได้ตามปกติ",
    "work":          "งานวันนี้เป็นจังหวะเรียบ ๆ เหมาะกับการเก็บรายละเอียดและวางแผนต่อ",
    "money":         "การเงินไม่มีความเร่งด่วน ลองใช้เวลาเช็คความเคลื่อนไหวให้ชัด",
    "health":        "สุขภาพเป็นกลาง ๆ อย่าลืมดูแลตัวเองให้สม่ำเสมอ",
    "communication": "การสื่อสารเป็นไปตามปกติ เปิดใจรับฟังก็เพียงพอ",
    "emotion":       "อารมณ์ทรง ๆ อย่าเพิ่งฝืน ลองให้เวลากับตัวเองบ้าง",
}

BANNED_TAGS = {"โหราศาสตร์", "ดูดวงวันนี้", "ดวงชะตา", "สายมู", "จักรวาล"}
FALLBACK_TAGS = ["พยากรณ์", "เช็คดวง", "ติดเทรนด์ดวง", "มูเตลู", "ดูดวงฟรี"]


def buddhist_date(date_str):
    """'2026-08-14' → (14, 'สิงหาคม', 2569)"""
    y, m, d = date_str.split("-")
    return int(d), MONTHS_FULL[int(m)], int(y) + 543


def heading(day_abbr, date_str):
    d, month_full, year_be = buddhist_date(date_str)
    day_th = DAY_ABBR_TO_TH.get(day_abbr, day_abbr)
    return f"ดวงของชาววัน{day_th} ประจำวันที่ {d} {month_full} พ.ศ. {year_be}"


def score_topics(scores):
    """เรียงหัวข้อจาก scores: (topic, value, label) ตามน้ำหนัก"""
    if not scores:
        return []
    ranked = []
    for key, meta in TOPIC_META.items():
        s = scores.get(key)
        if isinstance(s, dict) and "value" in s:
            ranked.append((key, s["value"], s.get("label", "")))
    ranked.sort(key=lambda x: abs(x[1]), reverse=True)
    return ranked


def build_body(day):
    """สร้างเนื้อหา 3-5 ประโยค ตาม guide"""
    ranked = score_topics(day.get("scores"))
    themes = day.get("themes") or []
    moon_phase = day.get("moon_phase", "")
    lucky = day.get("lucky", "")
    hard = day.get("hard_count", 0)
    soft = day.get("soft_count", 0)
    is_change_day = day.get("is_change_day", False)
    change_msg = day.get("change_msg", "")

    sentences = []

    # 1. ภาพรวมของวัน 1 ประโยค
    strong_topics = [r for r in ranked if r[1] >= 2]
    weak_topics = [r for r in ranked if r[1] <= -2]
    if strong_topics and not weak_topics:
        t = strong_topics[0][0]
        sentences.append(f"วันนี้มีจังหวะที่ดีในเรื่อง{TOPIC_META[t]['label']} เหมาะกับการลงมือทำตามแผน")
    elif weak_topics and not strong_topics:
        t = weak_topics[0][0]
        sentences.append(f"วันนี้เรื่อง{TOPIC_META[t]['label']}อาจต้องใช้ความรอบคอบเป็นพิเศษ")
    elif strong_topics and weak_topics:
        t_good = strong_topics[0][0]
        t_care = weak_topics[0][0]
        sentences.append(
            f"วันนี้มีทั้งจังหวะที่ดีในเรื่อง{TOPIC_META[t_good]['label']} "
            f"และจุดที่ต้องระวังในเรื่อง{TOPIC_META[t_care]['label']}"
        )
    else:
        sentences.append("วันนี้เป็นวันเรียบ ๆ ที่มีคุณค่าในตัวของมันเอง ลองใช้เวลากับสิ่งที่อยู่ตรงหน้า")

    # 2. ประเด็นเด่น 1-2 ด้าน (เฉพาะที่มีน้ำหนัก)
    picked = []
    for topic, val, label in ranked[:3]:
        if abs(val) >= 2:
            picked.append(topic)
        if len(picked) >= 2:
            break
    for topic in picked:
        if val := next((r[1] for r in ranked if r[0] == topic), 0):
            if val >= 2:
                sentences.append(TOPIC_META[topic]["pos"])
            else:
                sentences.append(TOPIC_CAUTION[topic])

    # 2b. themes จากข้อมูลต้นทาง (ถ้ามีและยังไม่ได้พูดถึง)
    for th in themes[:1]:
        cleaned = re.sub(r"\s*—\s*", " ", th).strip()
        if cleaned and not any(cleaned[:8] in s for s in sentences):
            sentences.append(f"ข้อมูลชี้ให้เห็นว่า{cleaned}")

    # 2c. การเปลี่ยนแปลงของวัน (ถ้าเป็น change day)
    if is_change_day and change_msg:
        sentences.append(f"วันนี้เป็นช่วงเปลี่ยนผ่าน: {change_msg}")

    # 3. ข้อควรระวัง (จาก weak topics ที่ยังไม่ได้พูด)
    for topic, val, label in ranked:
        if val <= -2 and topic not in picked:
            sentences.append(TOPIC_CAUTION[topic])
            break

    # 4. คำแนะนำที่ทำได้จริง 1 ประโยค
    if lucky:
        sentences.append(f"สีและทิศที่ช่วยเสริมจังหวะวันนี้คือ{lucky} ลองใช้เป็นตัวช่วยจัดระเบียบวันของคุณ")
    else:
        sentences.append("ค่อย ๆ ทำทีละเรื่อง แล้วเลือกทางที่เหมาะกับสถานการณ์ของคุณ")

    return sentences


def build_hashtags(day_abbr, date_str, scores, body_text):
    d, month_full, year_be = buddhist_date(date_str)
    day_th = DAY_ABBR_TO_TH.get(day_abbr, day_abbr)
    tags = [f"ดวงชาววัน{day_th}", f"ดูดวง{d}{month_full}{year_be}"]

    ranked = score_topics(scores)
    topics = [r[0] for r in ranked[:4] if abs(r[1]) >= 2]
    for t in topics:
        tags.append(TOPIC_META[t]["tag"])
        if len(tags) >= 5:
            break

    # ถ้ายังไม่ครบ 5 ใช้ fallback
    for fb in FALLBACK_TAGS:
        if len(tags) >= 5:
            break
        if fb not in tags:
            tags.append(fb)

    # กรองต้องห้าม + เด็ดซ้ำ
    clean = []
    for t in tags:
        if t in BANNED_TAGS or t in clean:
            continue
        clean.append(t)
    while len(clean) < 5:
        for fb in FALLBACK_TAGS:
            if fb not in clean:
                clean.append(fb)
            if len(clean) >= 5:
                break
    return clean[:5]


def build_caption(day, date_str):
    day_abbr = day.get("day_abbr", "")
    head = heading(day_abbr, date_str)
    body = build_body(day)
    tags = build_hashtags(day_abbr, date_str, day.get("scores"), " ".join(body))
    caption = head + "\n\n" + "\n".join(body) + "\n\n" + " ".join(f"#{t}" for t in tags)
    return caption


def main():
    parser = argparse.ArgumentParser(description="สร้างคำทำนาย 7 วันเกิดจาก astro_v3")
    parser.add_argument("--input", required=True, help="astro_v3 JSON path")
    parser.add_argument("--output", required=True, help="Output directory")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    date_str = data.get("date")
    if not date_str:
        print("✗ ไม่พบ field date ในข้อมูล", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)
    all_preds = {}
    for day in data.get("days", []):
        abbr = day.get("day_abbr", "")
        if not abbr:
            continue
        caption = build_caption(day, date_str)
        pred = {
            "day": abbr,
            "day_th": DAY_ABBR_TO_TH.get(abbr, abbr),
            "date": date_str,
            "ruler_planet": day.get("ruler_planet", ""),
            "ruler_sign": day.get("ruler_sign", ""),
            "ruler_dignity": day.get("ruler_dignity", ""),
            "ruler_retrograde": day.get("ruler_retrograde", False),
            "scores": day.get("scores", {}),
            "hard_count": day.get("hard_count", 0),
            "soft_count": day.get("soft_count", 0),
            "themes": day.get("themes", []),
            "moon_sign": day.get("moon_sign", ""),
            "moon_phase": day.get("moon_phase", ""),
            "lucky": day.get("lucky", ""),
            "caption": caption,
        }
        all_preds[abbr] = pred
        out_path = os.path.join(args.output, f"{abbr}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(pred, f, ensure_ascii=False, indent=2)
        print(f"✓ {pred['day_th']} — {len(caption)} chars")

    all_path = os.path.join(args.output, "all.json")
    with open(all_path, "w", encoding="utf-8") as f:
        json.dump(all_preds, f, ensure_ascii=False, indent=2)
    print(f"✓ all.json ({len(all_preds)} days) → {args.output}")


if __name__ == "__main__":
    main()
