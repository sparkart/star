#!/usr/bin/env python3
"""pull_ephem.py — ดึงข้อมูลดาราศาสตร์ครบทุกมิติจาก Swiss Ephemeris
Usage: python3 pull_ephem.py --date YYYY-MM-DD --lat 13.75 --lng 100.5 --time 08:00 --tz 7 --run N --output path.json
"""
import argparse, json, os, sys
from datetime import datetime, timezone, timedelta
from math import floor

import swisseph as swe

# ─── CONFIG ───
swe.set_ephe_path('/home/ubuntu/.local/share/swisseph/ephe')

ZODIAC_TH = ["เมษ","พฤษภ","เมถุน","กรกฎ","สิงห์","กันย์","ตุลย์","พิจิก","ธนู","มังกร","กุมภ์","มีน"]
HOUSE_TH = {1:"ตนุ",2:"กดุมภะ",3:"สหัชชะ",4:"พันธุ",5:"ปุตตะ",6:"อริ",
            7:"ปัตนิ",8:"มรณะ",9:"ศุภะ",10:"กัมมะ",11:"ลาภะ",12:"วินาศ"}

PLANETS = {
    swe.SUN:       ("sun",       "อาทิตย์", "☀️"),
    swe.MOON:      ("moon",      "จันทร์",  "🌙"),
    swe.MERCURY:   ("mercury",   "พุธ",     "☿"),
    swe.VENUS:     ("venus",     "ศุกร์",   "♀️"),
    swe.MARS:      ("mars",      "อังคาร",  "♂️"),
    swe.JUPITER:   ("jupiter",   "พฤหัส",   "♃"),
    swe.SATURN:    ("saturn",    "เสาร์",    "♄"),
    swe.URANUS:    ("uranus",    "ยูเรนัส",  "⛢"),
    swe.NEPTUNE:   ("neptune",   "เนปจูน",   "♆"),
    swe.PLUTO:     ("pluto",     "พลูโต",    "♇"),
    swe.TRUE_NODE: ("rahu",      "ราหู",     "☊"),
}

ASTEROIDS = [
    (swe.CHIRON,  "chiron",  "Chiron",  "⚷"),
    (swe.CERES,   "ceres",   "Ceres",   "⚳"),
    (swe.PALLAS,  "pallas",  "Pallas",  "⚴"),
    (swe.JUNO,    "juno",    "Juno",    "⚵"),
    (swe.VESTA,   "vesta",   "Vesta",   "⚶"),
]

FIXED_STARS = [
    "Regulus","Spica","Aldebaran","Antares","Fomalhaut",
    "Sirius","Vega","Capella","Rigel","Betelgeuse",
    "Procyon","Altair","Deneb","Polaris","Algol",
    "Arcturus","Canopus","Castor","Pollux","Achernar",
]

STAR_THAI = {
    "Regulus":"หัวใจสิงห์","Spica":"รวงข้าว","Aldebaran":"ตาวัว",
    "Antares":"คู่ปรับ Mars","Fomalhaut":"ปากปลา","Sirius":"สุนัขใหญ่",
    "Vega":"พิณ","Capella":"แพะ","Rigel":"เท้านายพราน","Betelgeuse":"ไหล่นายพราน",
    "Procyon":"สุนัขเล็ก","Altair":"อินทรี","Deneb":"หงส์","Polaris":"ดาวเหนือ",
    "Algol":"หัวปีศาจ","Arcturus":"ผู้พิทักษ์หมี","Canopus":"เต่า",
    "Castor":"ฝาแฝด 1","Pollux":"ฝาแฝด 2","Achernar":"ปลายแม่น้ำ",
}

STAR_MAGS = {
    "Regulus":1.4,"Spica":1.0,"Aldebaran":0.9,"Antares":1.0,"Fomalhaut":1.2,
    "Sirius":-1.5,"Vega":0.0,"Capella":0.1,"Rigel":0.1,"Betelgeuse":0.5,
    "Procyon":0.4,"Altair":0.8,"Deneb":1.3,"Polaris":2.0,"Algol":2.1,
    "Arcturus":-0.1,"Canopus":-0.7,"Castor":1.6,"Pollux":1.2,"Achernar":0.5,
}

DIGNITIES = {
    'sun':     {'domicile': 'สิงห์',   'exalt': 'เมษ',    'fall': 'ตุลย์',   'detriment': 'กุมภ์'},
    'moon':    {'domicile': 'กรกฎ',   'exalt': 'พฤษภ',   'fall': 'พิจิก',   'detriment': 'มังกร'},
    'mercury': {'domicile': 'เมถุน/กันย์', 'exalt': 'กันย์', 'fall': 'มีน', 'detriment': 'ธนู/มีน'},
    'venus':   {'domicile': 'ตุลย์/พฤษภ', 'exalt': 'มีน', 'fall': 'กันย์', 'detriment': 'เมษ/พิจิก'},
    'mars':    {'domicile': 'เมษ/พิจิก',  'exalt': 'มังกร','fall': 'กรกฎ', 'detriment': 'ตุลย์/พฤษภ'},
    'jupiter': {'domicile': 'ธนู/มีน',    'exalt': 'กรกฎ', 'fall': 'มังกร','detriment': 'เมถุน/กันย์'},
    'saturn':  {'domicile': 'มังกร/กุมภ์','exalt': 'ตุลย์', 'fall': 'เมษ', 'detriment': 'กรกฎ/สิงห์'},
}

HOUSE_SYSTEMS = [
    (b'P', "placidus"),
    (b'W', "whole_sign"),
    (b'K', "koch"),
    (b'E', "equal"),
    (b'C', "campanus"),
    (b'R', "regiomontanus"),
]

DAY_RULERS = ['sun','venus','mercury','moon','saturn','jupiter','mars']
NIGHT_RULERS = ['jupiter','mars','sun','venus','mercury','moon','saturn']

# ─── HELPERS ───
def dms(dd):
    d=int(dd); m=int((dd-d)*60); s=(dd-d-m/60)*3600
    return f"{d}°{m:02d}'{s:02.0f}\""

def deg2sign(lon):
    lon = lon % 360
    s = floor(lon / 30)
    d = round(lon - s * 30, 6)
    return ZODIAC_TH[s], d

def jd2ict(jd):
    frac = jd - floor(jd)
    h = (frac - 0.5) * 24 + 7  # noon→midnight + ICT
    h = h % 24
    m = (h - int(h)) * 60
    return f"{int(h):02d}:{int(m):02d}"

def get_house(degree, cusps):
    """หาว่าดาวอยู่เรือนไหน (Placidus)"""
    for i in range(1, 13):
        cusp_start = cusps[i] % 360
        cusp_end = (cusps[i+1] if i < 12 else cusps[0]) % 360
        deg = degree % 360
        if cusp_start <= cusp_end:
            if cusp_start <= deg < cusp_end:
                return i
        else:  # wrap around 360
            if deg >= cusp_start or deg < cusp_end:
                return i
    return 1

# ─── MAIN ───
def pull_all(date_str, lat, lng, hour, tz_offset, run_num):
    """ดึงข้อมูลทั้งหมด"""
    y, m, d = map(int, date_str.split('-'))
    dt = datetime(y, m, d, hour, 0, 0, tzinfo=timezone(timedelta(hours=tz_offset)))
    jd = swe.julday(dt.year, dt.month, dt.day, dt.hour + dt.minute/60.0)
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    geopos = [lng, lat, 0]

    result = {
        "meta": {
            "date": date_str,
            "time": f"{hour:02d}:00 ICT",
            "lat": lat, "lng": lng,
            "jd": jd,
            "run": run_num,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        },
        "planets": {},
        "asteroids": {},
        "houses": {},
        "sidereal": {"ayanamsa": None, "planets": {}},
        "arabic_parts": {},
        "fixed_stars": {},
        "dignities": {},
        "planetary_hours": {},
        "sabian": {},
    }

    # ── 1. PLANETS ──
    for pid, (key, th_name, icon) in PLANETS.items():
        r = swe.calc_ut(jd, pid, flags)
        lon = r[0][0]; lat_ecl = r[0][1]; dist = r[0][2]; spd = r[0][3]
        sign_th, deg_in = deg2sign(lon)
        result["planets"][key] = {
            "name_th": th_name, "icon": icon,
            "longitude": round(lon, 6),
            "latitude_ecl": round(lat_ecl, 6),
            "distance_au": round(dist, 6),
            "speed_deg_day": round(spd, 6),
            "sign": sign_th, "degree": round(deg_in, 6),
            "retrograde": spd < 0,
            "dms": dms(lon),
        }

    # Ketu = Rahu + 180
    rahu_lon = result["planets"]["rahu"]["longitude"]
    rahu_spd = result["planets"]["rahu"]["speed_deg_day"]
    ketu_lon = (rahu_lon + 180) % 360
    sign_th, deg_in = deg2sign(ketu_lon)
    result["planets"]["ketu"] = {
        "name_th": "เกตุ", "icon": "☋",
        "longitude": round(ketu_lon, 6),
        "latitude_ecl": 0, "distance_au": result["planets"]["rahu"]["distance_au"],
        "speed_deg_day": round(rahu_spd, 6),
        "sign": sign_th, "degree": round(deg_in, 6),
        "retrograde": rahu_spd < 0,
        "dms": dms(ketu_lon),
    }

    # ── 2. HOUSES (Placidus for main, others stored) ──
    placidus = swe.houses(jd, lat, lng, b'P')
    p_cusps = placidus[0]  # [0]=AC, [1]=H1, ..., [11]=H11
    
    # Build cusps array: [0]=AC, [1]=H1, ..., [12]=H12(=AC)
    h_cusps = [p_cusps[0] % 360]  # AC
    for i in range(1, 12):
        h_cusps.append(p_cusps[i] % 360)
    h_cusps.append(h_cusps[0])  # H12 = AC
    
    ascmc = placidus[1]
    asc = ascmc[0]
    mc = ascmc[1]
    
    sign_ac, deg_ac = deg2sign(asc)
    sign_mc, deg_mc = deg2sign(mc)
    
    result["ascendant"] = {"sign": sign_ac, "degree": round(deg_ac, 6), "longitude": round(asc, 6)}
    result["mc"] = {"sign": sign_mc, "degree": round(deg_mc, 6), "longitude": round(mc, 6)}
    
    # Assign houses to planets
    for pkey, pdata in result["planets"].items():
        h = get_house(pdata["longitude"], h_cusps)
        pdata["house"] = h
        pdata["house_name"] = HOUSE_TH[h]

    # Store all house systems
    for sys_code, sys_name in HOUSE_SYSTEMS:
        try:
            hs = swe.houses(jd, lat, lng, sys_code)
            cs = [round(c % 360, 6) for c in hs[0]]
            result["houses"][sys_name] = {
                "cusps": cs,
                "asc": round(hs[1][0], 6),
                "mc": round(hs[1][1] if len(hs[1]) > 1 else 0, 6),
            }
        except:
            result["houses"][sys_name] = None

    # ── 3. ASTEROIDS + Lilith ──
    for aid, key, th_name, icon in ASTEROIDS:
        try:
            r = swe.calc_ut(jd, aid, flags)
            lon = r[0][0]; spd = r[0][3]
            sign_th, deg_in = deg2sign(lon)
            h = get_house(lon, h_cusps)
            result["asteroids"][key] = {
                "name_th": th_name, "icon": icon,
                "longitude": round(lon, 6),
                "speed_deg_day": round(spd, 6),
                "sign": sign_th, "degree": round(deg_in, 6),
                "retrograde": spd < 0,
                "house": h, "house_name": HOUSE_TH[h],
                "dms": dms(lon),
            }
        except:
            result["asteroids"][key] = None

    # Lilith (mean apogee)
    try:
        r = swe.calc_ut(jd, swe.MEAN_APOG, flags)
        lon = r[0][0]; sign_th, deg_in = deg2sign(lon)
        result["asteroids"]["lilith"] = {
            "name_th": "ลิลิธ", "icon": "⚸",
            "longitude": round(lon, 6), "sign": sign_th, "degree": round(deg_in, 6),
            "dms": dms(lon),
        }
    except:
        result["asteroids"]["lilith"] = None

    # ── 4. SIDEREAL (Ayanamsa Lahiri) ──
    ayan = swe.get_ayanamsa_ut(jd)
    result["sidereal"]["ayanamsa"] = round(ayan, 6)
    result["sidereal"]["ayanamsa_dms"] = dms(ayan)
    for pkey, pdata in result["planets"].items():
        sid_lon = (pdata["longitude"] - ayan) % 360
        sign_th, deg_in = deg2sign(sid_lon)
        result["sidereal"]["planets"][pkey] = {
            "longitude": round(sid_lon, 6),
            "sign": sign_th, "degree": round(deg_in, 6),
            "dms": dms(sid_lon),
        }

    # ── 5. ARABIC PARTS ──
    def plon(key):
        return result["planets"][key]["longitude"]
    
    pof = (asc + plon("moon") - plon("sun")) % 360
    pos = (asc + plon("sun") - plon("moon")) % 360
    
    parts = {
        "fortune": ("🍀 โชคลาภ", pof),
        "spirit": ("✨ จิตวิญญาณ", pos),
        "love": ("💕 ความรัก", (asc + plon("venus") - plon("mars")) % 360),
        "death": ("⚔️ วาระสุดท้าย", (asc + plon("saturn") - plon("mars")) % 360),
        "marriage": ("🤝 คู่ครอง", (asc + plon("saturn") - plon("venus")) % 360),
        "passion": ("🔥 กิเลส", (asc + plon("mars") - plon("saturn")) % 360),
        "knowledge": ("📚 ความรู้", (asc + plon("mercury") - plon("jupiter")) % 360),
        "victory": ("👑 ชัยชนะ", (asc + plon("sun") - plon("saturn")) % 360),
    }
    for key, (th_name, val) in parts.items():
        sign_th, deg_in = deg2sign(val)
        result["arabic_parts"][key] = {
            "name_th": th_name,
            "longitude": round(val, 6),
            "sign": sign_th, "degree": round(deg_in, 6),
            "dms": dms(val),
        }

    # ── 6. FIXED STARS ──
    for star in FIXED_STARS:
        try:
            r = swe.fixstar_ut(star, jd, swe.FLG_SWIEPH)
            lon = r[0][0]; sign_th, deg_in = deg2sign(lon)
            result["fixed_stars"][star.lower()] = {
                "name_th": STAR_THAI.get(star, star),
                "longitude": round(lon, 6),
                "sign": sign_th, "degree": round(deg_in, 6),
                "magnitude": STAR_MAGS.get(star),
                "dms": dms(lon),
            }
        except:
            result["fixed_stars"][star.lower()] = None

    # ── 7. ESSENTIAL DIGNITIES ──
    for pkey in ['sun','moon','mercury','venus','mars','jupiter','saturn']:
        if pkey not in DIGNITIES: continue
        pdata = result["planets"][pkey]
        ds = DIGNITIES[pkey]
        dignities_list = []
        sign = pdata["sign"]
        if sign in ds['domicile']: dignities_list.append("เกษตร (Domicile)")
        if sign in ds['exalt']: dignities_list.append("ประมุข (Exaltation)")
        if sign in ds['fall']: dignities_list.append("นิจ (Fall)")
        if sign in ds['detriment']: dignities_list.append("ประทุษ (Detriment)")
        if not dignities_list: dignities_list.append("Peregrine")
        result["dignities"][pkey] = dignities_list

    # ── 8. PLANETARY HOURS ──
    # Find sunrise for the target date
    jd_start = swe.julday(y, m, d - 1, 0.0)  # day before, 00:00 UT
    try:
        sr = swe.rise_trans(jd_start, swe.SUN, swe.CALC_RISE, geopos, 1013.25, 15, swe.FLG_SWIEPH)[1][0]
        ss = swe.rise_trans(sr, swe.SUN, swe.CALC_SET, geopos, 1013.25, 15, swe.FLG_SWIEPH)[1][0]
        
        day_len = (ss - sr) * 24
        night_len = 24 - day_len
        dh = day_len / 12
        nh = night_len / 12
        
        result["planetary_hours"] = {
            "sunrise_ict": jd2ict(sr),
            "sunset_ict": jd2ict(ss),
            "day_length_h": round(day_len, 2),
            "night_length_h": round(night_len, 2),
            "day_hour_min": round(dh * 60, 0),
            "night_hour_min": round(nh * 60, 0),
            "day_hours": [],
            "night_hours": [],
        }
        
        for i in range(12):
            s = sr + i * dh / 24
            e = sr + (i + 1) * dh / 24
            result["planetary_hours"]["day_hours"].append({
                "hour": i + 1,
                "ruler": DAY_RULERS[i % 7],
                "start_ict": jd2ict(s),
                "end_ict": jd2ict(e),
            })
        
        for i in range(12):
            s = ss + i * nh / 24
            e = ss + (i + 1) * nh / 24
            result["planetary_hours"]["night_hours"].append({
                "hour": i + 1,
                "ruler": NIGHT_RULERS[i % 7],
                "start_ict": jd2ict(s),
                "end_ict": jd2ict(e),
            })
        
        # Current hour
        sr_ict_h = (sr - floor(sr)) * 24
        sr_ict_h = (sr_ict_h - 12 + 7) % 24
        now_h = hour
        elapsed = now_h - sr_ict_h
        if elapsed < 0:
            ss_ict_h = (ss - floor(ss)) * 24
            ss_ict_h = (ss_ict_h - 12 + 7) % 24
            elapsed = now_h + 24 - ss_ict_h
            pidx = int(elapsed / nh) if nh > 0 else 0
            result["planetary_hours"]["current"] = {
                "period": "night",
                "hour": pidx + 1,
                "ruler": NIGHT_RULERS[pidx % 7],
            }
        elif elapsed < day_len:
            pidx = int(elapsed / dh)
            result["planetary_hours"]["current"] = {
                "period": "day",
                "hour": pidx + 1,
                "ruler": DAY_RULERS[pidx % 7],
            }
        else:
            pidx = int((elapsed - day_len) / nh) if nh > 0 else 0
            result["planetary_hours"]["current"] = {
                "period": "night",
                "hour": pidx + 1,
                "ruler": NIGHT_RULERS[pidx % 7],
            }
    except Exception as e:
        result["planetary_hours"] = {"error": str(e)}

    # ── 9. SABIAN SYMBOLS ──
    # Key points only (planets + ascendant)
    SABIAN = {}  # abbreviated — full 360 in separate file
    key_points = {
        "sun": result["planets"]["sun"]["longitude"],
        "moon": result["planets"]["moon"]["longitude"],
        "mars": result["planets"]["mars"]["longitude"],
        "venus": result["planets"]["venus"]["longitude"],
        "mercury": result["planets"]["mercury"]["longitude"],
        "jupiter": result["planets"]["jupiter"]["longitude"],
        "saturn": result["planets"]["saturn"]["longitude"],
        "ascendant": asc,
    }
    for name, lon in key_points.items():
        sign_idx = floor(lon / 30)
        deg = int(lon - sign_idx * 30)
        result["sabian"][name] = {
            "sign": ZODIAC_TH[sign_idx],
            "degree": deg,
            "key": f"{sign_idx}_{deg}",
        }

    return result

# ─── CLI ───
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pull full ephemeris data")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--lat", type=float, default=13.75)
    parser.add_argument("--lng", type=float, default=100.5)
    parser.add_argument("--time", type=int, default=8, help="Hour (ICT)")
    parser.add_argument("--tz", type=int, default=7, help="UTC offset")
    parser.add_argument("--run", type=int, required=True, help="Run number (1-3)")
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    data = pull_all(args.date, args.lat, args.lng, args.time, args.tz, args.run)
    
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    size = os.path.getsize(args.output)
    print(f"✓ Run {args.run}: {args.output} ({size:,} bytes)")
    print(f"  Planets: {len(data['planets'])} | Asteroids: {len(data['asteroids'])} | Stars: {len(data['fixed_stars'])}")
    print(f"  House systems: {len(data['houses'])} | Arabic parts: {len(data['arabic_parts'])}")
