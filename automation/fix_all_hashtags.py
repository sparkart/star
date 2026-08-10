#!/usr/bin/env python3
"""
Fix ALL hashtags across 31 days of star horoscope captions.
- Fix date format: #ดูดวงDสค → #ดูดวงDสิงหาคม2569, #ดูดวงDกย → #ดูดวงDกันยายน2569
- Fix day-of-week: must be #ดวงชาววันX format consistently
- Content hashtags: derived from actual caption text
"""
import json
import re
import os
import subprocess
import sys

# Day name mapping
DAY_TH = {
    'mon': 'จันทร์', 'tue': 'อังคาร', 'wed': 'พุธ',
    'thu': 'พฤหัสบดี', 'fri': 'ศุกร์', 'sat': 'เสาร์', 'sun': 'อาทิตย์'
}

# Content keyword patterns → hashtag
CONTENT_TAGS = [
    (r'(ความรัก|รัก|แฟน|คนรัก|คู่|เนื้อคู่)', '#ดวงความรัก'),
    (r'(เงิน|การเงิน|รายได้|รายรับ|ลงทุน|หุ้น)', '#ดวงการเงิน'),
    (r'(งาน|การงาน|อาชีพ|ตำแหน่ง|โปรโมท|เลื่อน)', '#ดวงการงาน'),
    (r'(โชค|โชคลาภ|ลาภ|ดวงดี|เฮง)', '#โชคลาภ'),
    (r'(สุขภาพ|ป่วย|เจ็บ|โรค|หมอ|ร่างกาย|แข็งแรง)', '#ดวงสุขภาพ'),
    (r'(อารมณ์|รู้สึก|เหงา|เศร้า|ดีใจ|สุข|เครียด)', '#อารมณ์วันนี้'),
    (r'(ครอบครัว|พ่อ|แม่|ญาติ|พี่|น้อง|บ้าน)', '#ครอบครัว'),
    (r'(เดินทาง|ท่องเที่ยว|ย้าย|ต่างประเทศ|ต่างแดน)', '#ดวงเดินทาง'),
    (r'(เพื่อน|มิตร|สังคม|คนรอบข้าง)', '#เพื่อน'),
    (r'(สื่อสาร|พูด|คุย|เจรจา|สนทนา)', '#การสื่อสาร'),
    (r'(พลัง|มั่นใจ|กล้า|เข้มแข็ง|สู้)', '#พลังบวก'),
]

# Extract up to 3 content hashtags from caption text
def extract_content_hashtags(text):
    tags = []
    used_categories = set()
    for pattern, tag in CONTENT_TAGS:
        if re.search(pattern, text, re.IGNORECASE):
            # Extract category for dedup
            cat = tag.replace('#', '')
            if cat not in used_categories:
                tags.append(tag)
                used_categories.add(cat)
        if len(tags) >= 3:
            break
    return tags

def fix_caption_set(date_str, day_key, entry):
    """Fix one caption set (one day-of-week for one date)."""
    caption = entry.get('caption', '')
    year_str = '2569'
    
    # Parse date
    parts = date_str.split('-')
    day_num = int(parts[2])
    month_num = int(parts[1])
    
    if month_num == 8:
        month_full = 'สิงหาคม'
    elif month_num == 9:
        month_full = 'กันยายน'
    else:
        month_full = parts[1]
    
    # Build date hashtag: #ดูดวงDเดือนเต็มพศ
    date_tag = f'#ดูดวง{day_num}{month_full}{year_str}'
    
    # Build day-of-week hashtag: #ดวงชาววันX
    day_tag = f'#ดวงชาววัน{DAY_TH[day_key]}'
    
    # Extract content hashtags from actual caption text
    content_tags = extract_content_hashtags(caption)
    
    # Assemble final 5 hashtags
    hashtags = [day_tag, date_tag]
    hashtags.extend(content_tags)
    
    # Pad to exactly 5 if we don't have enough content tags
    # Fallback to common relevant tags based on text
    fallbacks = []
    if 'ความรัก' in caption and '#ดวงความรัก' not in hashtags:
        fallbacks.append('#ดวงความรัก')
    if ('เงิน' in caption or 'การเงิน' in caption) and '#ดวงการเงิน' not in hashtags:
        fallbacks.append('#ดวงการเงิน')
    if ('งาน' in caption or 'การงาน' in caption) and '#ดวงการงาน' not in hashtags:
        fallbacks.append('#ดวงการงาน')
    if 'โชค' in caption and '#โชคลาภ' not in hashtags:
        fallbacks.append('#โชคลาภ')
    if ('สุขภาพ' in caption or 'ป่วย' in caption) and '#ดวงสุขภาพ' not in hashtags:
        fallbacks.append('#ดวงสุขภาพ')
    
    hashtags.extend(fallbacks)
    
    # Still need more? Add generic but acceptable ones
    if len(hashtags) < 5 and '#พยากรณ์' not in hashtags:
        hashtags.append('#พยากรณ์')
    if len(hashtags) < 5 and '#เช็คดวง' not in hashtags:
        hashtags.append('#เช็คดวง')
    
    # Ensure exactly 5
    hashtags = hashtags[:5]
    while len(hashtags) < 5:
        # Generic fallbacks - avoid the banned ones (#โหราศาสตร์, #ดูดวงวันนี้, #ดวงชะตา, #สายมู, etc.)
        candidates = ['#พยากรณ์', '#เช็คดวง', '#ติดเทรนด์ดวง', '#มูเตลู', '#ดูดวงฟรี']
        for c in candidates:
            if c not in hashtags:
                hashtags.append(c)
                break
        else:
            break
    
    entry['hashtags'] = hashtags[:5]
    return entry

# Load manifest
print("Loading manifest...")
manifest = json.load(open('/tmp/manifest.json'))

total_fixed = 0
for day_entry in manifest['days']:
    date = day_entry['date']
    captions = day_entry.get('captions', {})
    for day_key in DAY_TH:
        if day_key in captions:
            captions[day_key] = fix_caption_set(date, day_key, captions[day_key])
            total_fixed += 1

print(f"Fixed {total_fixed} caption sets across {len(manifest['days'])} days")

# Update manifest metadata
manifest['updated'] = '2026-08-10T18:00:00Z'
manifest['version'] = '2.0'
manifest['comment'] = 'v2: Correct hashtag format with full month names + content-based tags'

# Save locally
output_path = '/tmp/manifest_v2.json'
with open(output_path, 'w', encoding='utf-8') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print(f"Saved to {output_path}")

# Print some samples to verify
print("\n=== SAMPLE VERIFICATION ===")
for day_entry in manifest['days'][:3]:
    date = day_entry['date']
    for dk in ['mon', 'wed', 'sun']:
        if dk in day_entry.get('captions', {}):
            cap = day_entry['captions'][dk]
            print(f"\n{date} {dk}:")
            for h in cap['hashtags']:
                print(f"  {h}")

# Sep samples
for day_entry in manifest['days']:
    if day_entry['date'].startswith('2026-09'):
        date = day_entry['date']
        for dk in ['mon', 'fri']:
            if dk in day_entry.get('captions', {}):
                cap = day_entry['captions'][dk]
                print(f"\n{date} {dk}:")
                for h in cap['hashtags']:
                    print(f"  {h}")
        break

print("\n=== VERIFICATION SUMMARY ===")
# Check for old format violations
issues = []
for day_entry in manifest['days']:
    date = day_entry['date']
    for dk in ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']:
        if dk in day_entry.get('captions', {}):
            hts = day_entry['captions'][dk]['hashtags']
            # Check date format - should have full month
            date_h = hts[1] if len(hts) > 1 else ''
            if 'สค' in date_h or 'กย' in date_h:
                issues.append(f"  BAD DATE: {date} {dk}: {date_h}")
            # Check day format - should be #ดวงชาววันX
            day_h = hts[0] if len(hts) > 0 else ''
            if not day_h.startswith('#ดวงชาววัน'):
                issues.append(f"  BAD DAY: {date} {dk}: {day_h}")
            # Check no banned tags
            banned = ['#โหราศาสตร์', '#ดูดวงวันนี้', '#ดวงชะตา', '#สายมู', '#จักรวาล']
            for b in banned:
                if b in hts:
                    issues.append(f"  BANNED TAG: {date} {dk}: {b}")

if issues:
    print("ISSUES FOUND:")
    for i in issues[:20]:
        print(i)
    if len(issues) > 20:
        print(f"  ... and {len(issues)-20} more")
else:
    print("All checks passed! No issues found.")

print("\nDone. Ready to upload.")
