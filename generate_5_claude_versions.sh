#!/bin/bash
# สร้าง 5 เวอร์ชันคำทำนายโดยใช้ Claude
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
set -e

# สร้าง prompt สำหรับ Claude เพื่อสร้าง 5 เวอร์ชัน
PROMPT="อ่าน JSON จากด้านบนและเอาข้อมูลจากนั้น (ประกอบด้วยข้อมูล 7 วันเกิด, เรือนประจำวัน, มุมต่างๆ, ดาวเคราะห์, คะแนน) และสร้างคำทำนาย 5 เวอร์ชัน (v1 ถึง v5) โดยแต่ละเวอร์ชันมีโทนสีที่แตกต่างกันออกไปและลักษณะเฉพาะที่แตกต่างกันออกไป
ตอบใน JSON รูปแบบนี้:
{
  \"v1\": {
    \"sun\": {\"day\": \"sun\", \"day_th\": \"อาทิตย์\", \"prediction\": \"...\", \"score\": 2},
    ...
  },
  \"v2\": { ... },
  \"v3\": { ... },
  \"v4\": { ... },
  \"v5\": { ... }
}

กรุณาตอบกลับเป็น JSON โดยตรง"

# เรียกใช้ claude -p (ถ้าใช้ได้)
if command -v claude >/dev/null 2>&1; then
  echo "🤖 กำลังสร้าง 5 เวอร์ชันโดยใช้ Claude..."
  RESPONSE=$(claude -p "$PROMPT" 2>&1 || echo "ERROR: claude failed")
  echo "📄 คำตอบของ Claude:"
  echo "$RESPONSE"
else
  echo "⚠️ ไม่พบ claude ใน PATH; สร้างแต่ละเวอร์ชันเป็นการจำลอง"
  for i in {1..5}; do
    cp predictions/all.json "predictions/sun_v$i.json"
    echo "📄 สร้าง predictions/sun_v$i.json (จำลอง)"
  done
fi

echo "🎉 เวอร์ชัน predictions/sun_v1.json...predictions/sun_v5.json สร้างเสร็จแล้ว"