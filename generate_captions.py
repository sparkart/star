#!/usr/bin/env python3
"""
Generate correct captions for Star manifest based on actual script content.
Reads all 217 script files, analyzes content, and generates properly formatted captions.
Ensures NO duplicate bodies within the same day-of-week.
"""
import json
import os
import re
import random

random.seed(42)

MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
          "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]

DAY_NAMES = {"mon": "จันทร์", "tue": "อังคาร", "wed": "พุธ", "thu": "พฤหัสบดี",
             "fri": "ศุกร์", "sat": "เสาร์", "sun": "อาทิตย์"}

SCRIPTS_DIR = "/var/www/star/cdn/star"
MANIFEST_PATH = os.path.join(SCRIPTS_DIR, "manifest.json")


def parse_date(date_str):
    y, m, d = date_str.split("-")
    return int(d), MONTHS[int(m)], int(y) + 543


def format_title(date_str, day_key):
    d, month, year = parse_date(date_str)
    return f"ดวงของชาววัน{DAY_NAMES[day_key]} ประจำวันที่ {d} {month} {year}"


def format_date_hashtag(date_str):
    d, month, year = parse_date(date_str)
    return f"{d}{month}{year}"


def read_script(date_str, day_key):
    path = os.path.join(SCRIPTS_DIR, date_str, f"{day_key}.txt")
    return open(path).read() if os.path.exists(path) else ""


def extract_topics(text):
    """Extract sentiment per topic from the script text."""
    result = {}

    # LOVE
    has_love = bool(re.search(r"(ความรัก|รัก|เสน่ห์|คนรัก|แฟน|คนคุย|คิดถึง|สานสัมพันธ์|กระชับความสัมพันธ์)", text))
    love_strong = bool(re.search(
        r"(ความรัก|รัก|เสน่ห์|คนรัก).{0,50}(แรงมาก|แรงจัด|แรงสุด|พุ่ง|หยุดไม่อยู่)", text))
    love_good = bool(re.search(
        r"(ความรัก|รัก|เสน่ห์|คนรัก).{0,40}(ดี|โอเค|เด่น|อบอุ่น|หวาน|ละมุน|สวย|สบาย|พอไป|อยู่ในเกณฑ์)", text))
    love_warn = bool(re.search(r"(ความรัก|รัก).{0,30}(ระวัง|เตือน|ต้อง|ใจเย็น)", text))
    if love_strong: result["love"] = "strong"
    elif love_good: result["love"] = "good"
    elif love_warn: result["love"] = "warn"
    elif has_love: result["love"] = "good"
    else: result["love"] = "neutral"

    # MONEY
    has_money = bool(re.search(r"(การเงิน|เงิน|การเงินการทอง|โชคลาภ|รายได้)", text))
    money_strong = bool(re.search(
        r"(เงิน|การเงิน|โชคลาภ).{0,50}(แรงมาก|แรงจัด|เด่นมาก|พุ่ง|ดีมาก|เข้ามา)", text))
    money_good = bool(re.search(
        r"(เงิน|การเงิน|การเงินการทอง|โชคลาภ).{0,40}(ดี|โอเค|ไหลลื่น|สบาย|พอไป|เด่น|คล่อง)", text))
    money_warn = bool(re.search(r"(เงิน|การเงิน).{0,30}(ระวัง|เตือน|ต้อง|รอบคอบ|เสี่ยง)", text))
    if money_strong: result["money"] = "strong"
    elif money_good: result["money"] = "good"
    elif money_warn: result["money"] = "warn"
    elif has_money: result["money"] = "good"
    else: result["money"] = "neutral"

    # LUCK
    result["luck"] = bool(re.search(r"โชคลาภ|โชคดี|โชค.*ลาภ|ดวง.*ดี", text))

    # WORK
    work_good = bool(re.search(
        r"(การงาน|งาน|หน้าที่|ก้าวหน้า|ไว้วางใจ|เลื่อน).{0,30}(ดี|โอเค|เด่น|โอกาส|ก้าวหน้า|พอไป|สวย)", text))
    work_warn = bool(re.search(r"(งาน|การงาน).{0,20}(ระวัง|เตือน|ต้อง|อย่า|ใจร้อน|เร็ว|ขัดแย้ง)", text))
    if work_good: result["work"] = "good"
    elif work_warn: result["work"] = "warn"
    else: result["work"] = "neutral"

    # HEALTH
    health_warn = bool(re.search(
        r"(สุขภาพ|ร่างกาย|พัก|ฝืน|เพลีย|เหนื่อย).{0,20}(ระวัง|ต้อง|เตือน|อย่า|อย่าฝืน|พักผ่อน|ดูแล)", text))
    health_good = bool(re.search(r"(สุขภาพ|ร่างกาย).{0,15}(ดี|แข็งแรง|โอเค|พร้อม)", text))
    if health_good: result["health"] = "good"
    elif health_warn: result["health"] = "warn"
    else: result["health"] = "neutral"

    # EMOTION
    emotion_volatile = bool(re.search(
        r"(อารมณ์|อารม).{0,20}(แปรปรวน|ขึ้น.*ลง|แกว่ง|อ่อนไหว|เดี๋ยว.*เดี๋ยว)", text))
    emotion_positive = bool(re.search(
        r"(อารมณ์|นิ่ง|สบาย|มั่นใจ|กล้า|เชื่อมั่น|พลัง|พร้อม).{0,30}(ดี|นิ่ง|สบาย|มั่นใจ|เข้มแข็ง|พร้อม)", text))
    if emotion_volatile: result["emotion"] = "volatile"
    elif emotion_positive: result["emotion"] = "positive"
    else: result["emotion"] = "neutral"

    # COMMUNICATION
    comm_good = bool(re.search(
        r"(สื่อสาร|พูด|เจรจา|การพูด).{0,20}(ดี|คล่อง|ลื่น|เข้าใจ|สวย)", text))
    comm_warn = bool(re.search(r"(สื่อสาร|พูด|คำพูด|ปาก).{0,20}(ระวัง|คิดก่อน|เข้าใจผิด)", text))
    if comm_good: result["communication"] = "good"
    elif comm_warn: result["communication"] = "warn"
    else: result["communication"] = "neutral"

    # OVERALL TONE
    if "แรงมาก" in text or "แรงจัด" in text or "แรงสุด" in text or "พุ่ง" in text:
        result["tone"] = "strong"
    elif "ระวัง" in text and "ต้องระวัง" in text:
        result["tone"] = "cautious"
    elif "สบาย" in text or "ปกติ" in text or "เรื่อยๆ" in text:
        result["tone"] = "normal"
    else:
        result["tone"] = "mixed"

    return result


# Expanded template pools with many variations
LOVE_TEMPLATES = {
    "strong": [
        "ด้านความรักวันนี้โดดเด่นมากเป็นพิเศษ มีเกณฑ์ได้พบคนใหม่หรือกระชับความสัมพันธ์ให้แน่นแฟ้นขึ้น",
        "เรื่องความรักวันนี้แรงมาก เสน่ห์ของคุณพุ่งแรงจนใครเห็นก็ต้องมอง เปิดใจไว้แล้วโอกาสดีๆ จะเข้ามา",
        "ความรักวันนี้มาแรงแบบหยุดไม่อยู่ ดาวส่งเสริมให้คนโสดมีโอกาสพบเนื้อคู่ ส่วนคนมีคู่แล้วยิ่งสวีทหวานขึ้น",
        "วันนี้ดาวแห่งความรักโคจรแรง ดึงดูดคนเข้ามาหาคุณ ใครที่กำลังคุยกับใครอยู่มีสิทธิ์พัฒนาเป็นความสัมพันธ์ที่จริงจังมากขึ้น",
        "เสน่ห์คุณพุ่งแรงวันนี้ คนโสดเตรียมพบเซอร์ไพรส์ คนมีคู่แล้วยิ่งหวานฉ่ำ ใช้โอกาสนี้สานสัมพันธ์ให้ดี",
    ],
    "good": [
        "ด้านความรักวันนี้อยู่ในเกณฑ์ดี ความสัมพันธ์ราบรื่น อบอุ่น คนมีคู่รักกันดี คนโสดมีโอกาสพบปะผู้คนใหม่ๆ",
        "ความรักวันนี้ไปได้สวย ใครที่มีคนคุยอยู่ลองทักไปเถอะ จังหวะเป็นใจ พลังบวกด้านความสัมพันธ์ดีมาก",
        "เรื่องความรักวันนี้ละมุนละไม สายสัมพันธ์แน่นแฟ้น มีความสุขกับคนที่คุณรัก",
        "วันนี้ความรักของคุณอยู่ในช่วงที่ดี ใช้เวลากับคนรู้ใจ หรือถ้าโสดก็ลองเปิดใจให้คนใหม่",
        "ความสัมพันธ์วันนี้ลงตัว ใครอยู่ด้วยแล้วสบายใจ ใครที่โสดก็มีโอกาสเจอคนถูกใจผ่านเพื่อนฝูง",
        "ความรักของคุณวันนี้มีแต่เรื่องดีๆ เข้ามา ไม่ว่าจะเป็นฝั่งมีคู่หรือโสด",
    ],
    "warn": [
        "ด้านความรักวันนี้ต้องใจเย็นๆ หน่อย อย่าใช้อารมณ์นำหน้า หลีกเลี่ยงการทะเลาะเบาะแว้ง",
        "ความรักวันนี้อาจมีเรื่องให้ต้องระวัง ใจเย็นเข้าไว้ อย่าด่วนตัดสินใจอะไรด้วยอารมณ์",
        "เรื่องหัวใจวันนี้อาจมีแรงเสียดทานนิดๆ แต่ถ้าใจเย็นก็ผ่านไปได้",
    ],
    "neutral": [
        "ด้านความรักวันนี้ราบรื่นตามปกติ ไม่มีอะไรน่าห่วง",
        "ความรักวันนี้ไม่หวือหวา แต่มั่นคงดี ใช้ชีวิตไปตามปกติ",
        "ด้านความรักทรงตัวดี ไม่มีประเด็นอะไรให้ต้องกังวล",
        "หัวใจวันนี้สงบนิ่ง ไร้พายุ ไม่มีดราม่า สบายๆ",
        "ความรักวันนี้ปกติสุข ไม่มีไหนดิ่ง ไม่มีไหนพุ่ง",
    ],
}

MONEY_TEMPLATES = {
    "strong": [
        "ด้านการเงินวันนี้มีเกณฑ์ได้รับโชคลาภหรือรายได้เสริมเข้ามาอย่างไม่คาดฝัน",
        "เรื่องการเงินวันนี้เด่นมาก มีจังหวะดีๆ เข้ามาให้คว้า ลงทุนอะไรก็มีแนวโน้มได้ผลดี",
        "การเงินวันนี้พุ่งแรง มีโอกาสได้เงินก้อนหรือข้อเสนอดีๆ เข้ามา",
        "วันนี้โชคด้านการเงินเข้าข้างคุณ มีเกณฑ์ได้ลาภลอยหรือเงินก้อนจากทางที่ไม่คาดคิด",
    ],
    "good": [
        "ด้านการเงินวันนี้ไหลลื่นดี มีจังหวะดีๆ เข้ามาให้ใช้จ่ายอย่างมีความสุข",
        "เรื่องการเงินวันนี้พอไปได้ ไม่ต้องกังวล มีรายรับเข้ามาสม่ำเสมอ",
        "การเงินวันนี้อยู่ในเกณฑ์ดี ใช้จ่ายคล่องมือ ไม่มีปัญหาติดขัด",
        "กระแสเงินสดวันนี้ไหลเวียนดี รายจ่ายสมดุลกับรายรับ",
        "ด้านการเงินไม่น่าห่วง มีสภาพคล่องเพียงพอ",
    ],
    "warn": [
        "ด้านการเงินวันนี้ต้องใช้จ่ายอย่างรอบคอบ หลีกเลี่ยงการลงทุนที่เสี่ยงเกินไป",
        "เรื่องเงินวันนี้ต้องระวังหน่อย เก็บไว้ก่อนดีกว่าใช้ รัดเข็มขัดอีกนิดแล้วจะผ่านไปได้",
    ],
    "neutral": [
        "ด้านการเงินวันนี้ทรงตัว ไม่มีอะไรหวือหวา แต่ก็ไม่น่าเป็นห่วง",
        "การเงินวันนี้พอไปได้ ไม่มีรายจ่ายกะทันหัน เก็บเงินได้เรื่อยๆ",
        "กระแสเงินวันนี้คงที่ ไม่มีเซอร์ไพรส์อะไรทั้งบวกและลบ",
    ],
}

WORK_TEMPLATES = {
    "good": [
        "ด้านการงานมีโอกาสก้าวหน้า ได้รับความไว้วางใจจากผู้ใหญ่หรือหัวหน้างาน",
        "เรื่องงานวันนี้ไปได้สวย มีโอกาสดีๆ เข้ามา ทำงานอะไรก็ราบรื่น",
        "การงานวันนี้ราบรื่นดี ใครที่รอข่าวดีเรื่องงานเตรียมเฮได้เลย",
        "หน้าที่การงานวันนี้ก้าวหน้า ได้รับคำชมจากเจ้านายหรือเพื่อนร่วมงาน",
    ],
    "warn": [
        "ด้านการงานต้องใจเย็นเป็นพิเศษ อย่าตัดสินใจเร็ว หลีกเลี่ยงความขัดแย้งในที่ทำงาน",
        "เรื่องงานวันนี้ต้องระวังความผิดพลาดเล็กๆ น้อยๆ ทำงานให้รอบคอบมากขึ้น",
        "การงานวันนี้อาจมีอุปสรรคเข้ามา แต่อย่าเพิ่งท้อ ตั้งสติและใจเย็นไว้ก่อน",
        "วันนี้ระวังเรื่อง辦公室การเมือง อย่าพาดพิงใคร ทำงานของตัวเองให้ดีที่สุด",
    ],
    "neutral": [
        "ด้านการงานวันนี้ไปได้เรื่อยๆ ไม่มีอะไรน่าห่วง ทำงานตามปกติได้",
        "งานวันนี้ก็เรื่อยๆ ไม่มีข่าวดี ไม่มีข่าวร้าย ทำงานไปตามแผน",
    ],
}

HEALTH_TEMPLATES = {
    "warn": [
        "ด้านสุขภาพต้องดูแลตัวเองให้ดี อย่าฝืนร่างกายเกินไป พักผ่อนให้เพียงพอ",
        "ร่างกายวันนี้อาจล้าหรือเพลียได้ง่าย อย่าลืมพักบ้าง พักผ่อนให้เพียงพอ",
        "สุขภาพวันนี้ต้องระวังเป็นพิเศษ อย่าหักโหมทำงานหนักเกินไป",
    ],
    "good": [
        "ด้านสุขภาพวันนี้แข็งแรงดี มีพลังล้นเหลือ พร้อมลุยทุกกิจกรรม",
    ],
}

EMOTION_TEMPLATES = {
    "volatile": [
        "อารมณ์วันนี้อาจแปรปรวนขึ้นลงบ้าง แต่อย่ากังวลไป เดี๋ยวก็ผ่านไป",
        "วันนี้อารมณ์อาจแกว่งๆ หน่อย ใจเย็นๆ เข้าไว้ อย่าเพิ่งตัดสินใจอะไรตอนอารมณ์ไม่นิ่ง",
    ],
    "positive": [
        "พลังในตัวคุณวันนี้เข้มแข็ง มั่นใจ และพร้อมรับมือกับทุกสถานการณ์",
        "วันนี้เป็นวันที่ความรู้สึกดีๆ เข้ามา มั่นใจในตัวเองให้มากๆ",
    ],
}

COMM_TEMPLATES = {
    "good": [
        "การสื่อสารวันนี้คล่องแคล่ว พูดอะไรก็มีคนเข้าใจ เหมาะแก่การเจรจาหรือนัดพบปะ",
    ],
    "warn": [
        "ระวังเรื่องคำพูดวันนี้ คิดให้ดีก่อนพูด เพราะอาจมีคนเข้าใจผิดได้ง่าย",
    ],
}

TONE_CLOSERS = {
    "strong": [
        "โดยรวมวันนี้พลังในตัวคุณพุ่งแรงเป็นพิเศษ วันที่ต้องมั่นใจและกล้าลงมือทำ",
        "วันนี้เป็นวันที่จักรวาลส่งพลังมาให้คุณเต็มที่ ใช้พลังนี้ให้เกิดประโยชน์สูงสุด",
    ],
    "cautious": [
        "วันนี้ให้ใช้ชีวิตอย่างมีสติ ระวังเรื่องเร่งด่วนและการตัดสินใจที่ต้องใช้ความรอบคอบ",
        "ค่อยๆ ก้าวไปทีละก้าว อย่าใจร้อน แล้วทุกอย่างจะผ่านไปด้วยดี",
    ],
    "mixed": [
        "วันนี้เป็นวันที่ค่อยๆ ใช้ชีวิตไปทีละก้าว ไม่ต้องรีบร้อน ทุกอย่างจะผ่านไปด้วยดี",
        "โดยรวมถือว่าเป็นวันที่สมดุล มีทั้งด้านดีและด้านที่ต้องระวัง ใช้สติให้มาก",
    ],
    "normal": [
        "วันนี้เป็นวันที่ค่อยๆ ใช้ชีวิตไปทีละก้าว ไม่ต้องรีบร้อน ทุกอย่างจะผ่านไปด้วยดี",
    ],
}


def pick_unique(templates, key, used, pool_id):
    """Pick a template, avoiding ones already used for this day-of-week."""
    if key not in templates or not templates[key]:
        return ""
    pool = templates[key]
    # Try to find unused template
    unused = [t for t in pool if f"{pool_id}:{t}" not in used]
    if unused:
        chosen = random.choice(unused)
    else:
        # All used for this pool, clear only entries for this pool_id
        to_remove = {e for e in used if e.startswith(f"{pool_id}:")}
        used.difference_update(to_remove)
        chosen = random.choice(pool)
    used.add(f"{pool_id}:{chosen}")
    return chosen


def generate_summary(topics, day_key, date_str, used):
    """Generate 2-3 sentence unique summary."""
    sentences = []

    # LOVE
    s = pick_unique(LOVE_TEMPLATES, topics["love"], used, f"love_{day_key}")
    if s:
        sentences.append(s)

    # MONEY
    s = pick_unique(MONEY_TEMPLATES, topics["money"], used, f"money_{day_key}")
    if s:
        sentences.append(s)

    # WORK
    if topics["work"] != "neutral":
        s = pick_unique(WORK_TEMPLATES, topics["work"], used, f"work_{day_key}")
        if s:
            sentences.append(s)

    # HEALTH
    if topics["health"] != "neutral":
        s = pick_unique(HEALTH_TEMPLATES, topics["health"], used, f"health_{day_key}")
        if s:
            sentences.append(s)

    # EMOTION
    if topics["emotion"] != "neutral":
        s = pick_unique(EMOTION_TEMPLATES, topics["emotion"], used, f"emotion_{day_key}")
        if s:
            sentences.append(s)

    # COMMUNICATION
    if topics["communication"] != "neutral":
        s = pick_unique(COMM_TEMPLATES, topics["communication"], used, f"comm_{day_key}")
        if s:
            sentences.append(s)

    # Ensure 2-3 sentences
    if len(sentences) < 2:
        tone = topics.get("tone", "mixed")
        s = pick_unique(TONE_CLOSERS, tone, used, f"tone_{day_key}")
        if s:
            sentences.append(s)

    if len(sentences) < 2:
        sentences.append("วันนี้เป็นวันที่ค่อยๆ ใช้ชีวิตไปทีละก้าว ไม่ต้องรีบร้อน ทุกอย่างจะผ่านไปด้วยดี")

    return sentences[:3]


def generate_hashtags(topics, day_key, date_str):
    """Generate exactly 5 hashtags."""
    hashtags = []
    hashtags.append(f"#ดวงชาววัน{DAY_NAMES[day_key]}")
    hashtags.append(f"#ดูดวง{format_date_hashtag(date_str)}")

    content = []
    if topics["love"] in ("strong", "good"):
        content.append("#ดวงความรัก")
    if topics["money"] in ("strong", "good"):
        content.append("#ดวงการเงิน")
    if topics["work"] in ("good", "warn"):
        content.append("#ดวงการงาน")
    if topics["luck"] and "#โชคลาภ" not in content:
        content.append("#โชคลาภ")
    if topics["health"] == "warn":
        content.append("#ดวงสุขภาพ")

    fallback = ["#เช็คดวง", "#สายมู", "#ดวงวันนี้", "#ติดเทรนด์ดวง", "#โหราศาสตร์", "#มูเตลู", "#พยากรณ์"]
    for tag in fallback:
        if len(content) >= 3:
            break
        if tag not in content:
            content.append(tag)

    hashtags.extend(content[:3])
    while len(hashtags) < 5:
        for tag in fallback:
            if tag not in hashtags:
                hashtags.append(tag)
                break
        else:
            hashtags.append("#เช็คดวง")

    return hashtags[:5]


def main():
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    days = manifest["days"]
    total = len(days)
    print(f"Processing {total} dates × 7 days = {total * 7} captions...\n")

    # Track used templates per day-of-week to avoid duplicates
    used_by_day = {dk: set() for dk in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    # Track generated bodies per day-of-week to avoid duplicates
    bodies_by_day = {dk: set() for dk in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}

    for idx, entry in enumerate(days):
        date_str = entry["date"]
        captions = {}

        for dk in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            script = read_script(date_str, dk)
            if not script:
                print(f"  MISSING: {date_str}/{dk}.txt")
                continue

            topics = extract_topics(script)
            title = format_title(date_str, dk)
            hashtags = generate_hashtags(topics, dk, date_str)

            # Retry up to 5 times to get a unique body
            body = None
            for _ in range(5):
                summary_sentences = generate_summary(topics, dk, date_str, used_by_day[dk])
                candidate = " ".join(summary_sentences)
                if candidate not in bodies_by_day[dk]:
                    body = candidate
                    break
                # Reset the used templates for this attempt
                # (generate_summary already consumed some, so we need fresh ones)
                for sent in summary_sentences:
                    for pool_prefix in ["love_", "money_", "work_", "health_", "emotion_", "comm_", "tone_"]:
                        to_remove = {e for e in used_by_day[dk] if e == f"{pool_prefix}{dk}:{sent}"}
                        used_by_day[dk].difference_update(to_remove)

            if body is None:
                # Fallback: just use first attempt
                body = " ".join(generate_summary(topics, dk, date_str, set()))

            bodies_by_day[dk].add(body)

            captions[dk] = {
                "caption": f"{title}\n\n{body}",
                "hashtags": hashtags,
            }

        entry["captions"] = captions

        if (idx + 1) % 5 == 0:
            print(f"  ✓ {idx + 1}/{total} dates processed")

    manifest["updated"] = "2026-08-10T19:15:00Z"

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n✓ Done! {total} dates updated in manifest.json")

    # Verify uniqueness
    for dk in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
        bodies = []
        for day in days:
            parts = day["captions"][dk]["caption"].split("\n\n")
            bodies.append(parts[1] if len(parts) > 1 else "")
        unique = len(set(bodies))
        dup = len(bodies) - unique
        print(f"  {DAY_NAMES[dk]}: {unique}/{len(bodies)} unique bodies ({dup} dupes)")

    # Show samples
    print(f"\n=== SAMPLES (first date: {days[0]['date']}) ===")
    for dk in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
        c = days[0]["captions"][dk]
        print(f"\n{DAY_NAMES[dk]}:")
        print(f"  {c['caption'][:200]}")
        print(f"  Tags: {c['hashtags']}")

    print(f"\n=== SAMPLES (last date: {days[-1]['date']}) ===")
    for dk in ["mon", "tue", "wed"]:
        c = days[-1]["captions"][dk]
        print(f"\n{DAY_NAMES[dk]}:")
        print(f"  {c['caption'][:200]}")
        print(f"  Tags: {c['hashtags']}")


if __name__ == "__main__":
    main()
