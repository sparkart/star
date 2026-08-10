#!/usr/bin/env python3
"""generate_audio.py — สร้างเสียงพากย์ภาษาไทยด้วย gTTS"""
import json, os, argparse
from gtts import gTTS

DAY_TH = {"sun":"อาทิตย์","mon":"จันทร์","tue":"อังคาร","wed":"พุธ","thu":"พฤหัสบดี","fri":"ศุกร์","sat":"เสาร์"}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Captions directory")
    parser.add_argument("--output", required=True, help="Output directory")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)
    
    for day_key in ["sun","mon","tue","wed","thu","fri","sat"]:
        caption_path = os.path.join(args.input, f"{day_key}.json")
        if not os.path.exists(caption_path):
            print(f"✗ {day_key}: file not found")
            continue
        
        with open(caption_path) as f:
            data = json.load(f)
        
        # Extract caption text (remove hashtags line)
        caption = data["caption"]
        
        # Generate audio
        tts = gTTS(text=caption, lang='th', slow=False)
        out_path = os.path.join(args.output, f"{day_key}.mp3")
        tts.save(out_path)
        
        size = os.path.getsize(out_path)
        print(f"✓ {DAY_TH[day_key]}: {out_path} ({size:,} bytes)")

    print("✓ All 7 audio files generated")
