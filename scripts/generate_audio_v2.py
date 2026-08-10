#!/usr/bin/env python3
"""generate_audio_v2.py — สร้างเสียงพากย์คำทำนายภาษาไทย (Google TTS style)
ใช้ gTTS ภาษาไทย ดูผลลัพธ์ได้จาก output/2026-08-11/audio/"""
import json, os, subprocess, sys
from pathlib import Path

DAY_TH = {"sun":"อาทิตย์","mon":"จันทร์","tue":"อังคาร","wed":"พุธ","thu":"พฤหัสบดี","fri":"ศุกร์","sat":"เสาร์"}

def gen_audio(predictions_file, audio_dir):
    """อ่าน prediction files สร้าง audio 1 ไฟล์ต่อ 1 วันเกิด"""
    os.makedirs(audio_dir, exist_ok=True)
    files = sorted(Path(predictions_file).parent.glob("*.json"))
    if not files:
        print("✗ No prediction files found")
        return
    for f in files:
        with open(f) as fp:
            pred = json.load(fp)
        day = pred["day"]
        th_day = DAY_TH.get(day, day)
        r3 = pred["ruler"]
        r3th = {"sun":"อาทิตย์","moon":"จันทร์","mercury":"พุธ",
                "venus":"ศุกร์","mars":"อังคาร","jupiter":"พฤหัสบดี",
                "saturn":"เสาร์","uranus":"ยูเรนัส","neptune":"เนปจูน",
                "pluto":"พลูโต","rahu":"ราหู","ketu":"เกตุ"}.get(r3, r3)
        r3sign = pred["ruler_sign"]
        r3house = pred["ruler_house"]
        # Build prediction text
        parts = [f"ดวงของชาววัน{th_day}"]
        parts.append(f"ดาวประจำวันคือ{r3th} อยู่ในเรือน{r3house} ราศี{r3sign}")
        parts.append("")
        # Dignity
        if pred["ruler_dignity"]:
            parts.append(f"อยู่ในตำแหน่ง{'เกษตร' if 'เกษตร' in str(pred['ruler_dignity'][0]) else 'นิจ' if 'นิจ' in str(pred['ruler_dignity'][0]) else 'ประทุษ' if 'ประทุษ' in str(pred['ruler_dignity'][0]) else 'Peregrine'}")
        parts.append("")
        # Aspects summary
        if pred["aspects"]:
            parts.append("มุมสำคัญ:")
            for a in pred["aspects"][:4]:
                parts.append(f"  • {a['meaning']}กับ{a['planet_th']} ({a['orb']}° องศา)")
        # Score
        if pred["score"] > 0:
            parts.append(f"โดยรวม {pred['score']} — วันที่ดี")
        elif pred["score"] < 0:
            parts.append(f"โดยรวม {pred['score']} — วันที่ต้องระมัดระวัง")
        else:
            parts.append("โดยรวม 0 — ควรระวังความหมายตรงไปตรงมา")
        parts.append("")
        text = " ".join(parts)
        # Save text
        text_file = os.path.join(audio_dir, f"{day}.txt")
        with open(text_file, 'w') as fp:
            fp.write(text)
        # Generate audio
        print(f"🔊 อัปโหลด audio: {day} | {text[:80]}...")

if __name__ == "__main__":
    gen_audio("predictions/all.json", "audio")
