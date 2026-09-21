#!/usr/bin/env python3
"""
Kalender Libur Nasional Indonesia (optimized)
=============================================
- index.json + next_check_after (hemat iLovePDF)
- Hash PDF: skip OCR jika sumber tidak berubah (tanpa syarat complete)
- Musim cek Sep–Jan untuk tahun terbaru
- MAX_ILOVEPDF_DOCS_PER_RUN default 6
- Alur hemat iLovePDF: Extract → parse anchor (Imlek/Fitri/Adha/Kenaikan/Wafat) → OCR bila kurang
- Inject libur tanggal tetap (1 Jan, 1 Mei, 1 Jun, 17 Agust, 25 Des) jika hilang
- Jangan timpa JSON complete dengan hasil jelek
- Parser 2 pass + normalisasi nama + normalisasi teks flatten iLovePDF
- Cache cek JDIH 24 jam
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

try:
    from pdf2image import convert_from_path
    import pytesseract
    from PIL import ImageOps, ImageEnhance, ImageFilter
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.alert import Alert
    from selenium.common.exceptions import (
        UnexpectedAlertPresentException,
        NoAlertPresentException,
        ElementNotInteractableException,
    )
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

# ============================================================
# KONFIGURASI
# ============================================================
LIST_URL = "https://jdih.menpan.go.id/dokumen-hukum/jenis?jenis=keputusan%20bersama%20menteri"
PDF_BASE = "https://data-jdih.menpan.go.id/dokumen"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "kalender").strip("/")

ILOVEPDF_PUBLIC_KEY = os.getenv("ILOVEPDF_PUBLIC_KEY", "")
ILOVEPDF_SECRET_KEY = os.getenv("ILOVEPDF_SECRET_KEY", "")

# Setiap PDF = 1 "slot" lokal (bukan mirror billing API).
# Alur hemat: Extract dulu (10 kredit/file); OCR hanya jika hasil parse kurang.
# OCR = 5 kredit/halaman; Extract = 10 kredit/file (lihat iloveapi.com/pricing).
MAX_ILOVEPDF_DOCS_PER_RUN = int(os.getenv("MAX_ILOVEPDF_DOCS_PER_RUN", "6"))
# Hemat CPU multi-thread native (Tesseract/BLAS)
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

MIN_NATIONAL_COMPLETE = 15
MIN_JOINT_COMPLETE = 5
MIN_NATIONAL_SOFT = 12  # lengkap longgar jika tanggal kunci ada
MIN_NATIONAL_REJECT = 8  # di bawah ini jangan timpa complete
# Ambang "Extract cukup → skip OCR": evaluasi hasil parse, bukan jumlah karakter.
# Override via env bila perlu (mis. MIN_NATIONAL_SKIP_OCR=10).
MIN_NATIONAL_SKIP_OCR = int(os.getenv("MIN_NATIONAL_SKIP_OCR", str(MIN_NATIONAL_SOFT)))
MIN_JOINT_SKIP_OCR = int(os.getenv("MIN_JOINT_SKIP_OCR", "4"))

RETRY_DAYS_NOT_FOUND = 14
RETRY_DAYS_INCOMPLETE = 7
SEASON_START_MONTH = 9   # Sep
SEASON_END_MONTH = 1     # Jan (tahun berikutnya)
JDIH_CACHE_HOURS = 24

MONTH_MAP = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
    "september": 9, "oktober": 10, "november": 11, "desember": 12,
}

# Normalisasi nama libur (substring OCR → kanonik)
NAME_NORMALIZE = [
    # Idul Fitri — termasuk typo OCR umum (1/l/I)
    (r"(?:idul|1dul|ldul|iudl|idol)\s*f[il1]tr[il1]|idulfitri|1dulfitri", "Idul Fitri"),
    (r"(?:idul|1dul|ldul)\s*a[dth]ha|iduladha|iduladh[ae]", "Idul Adha"),
    (r"isra\s*m[il1]kraj|isra.?mi.?raj|[il1]sra\s*m[il1]kraj", "Isra Mikraj Nabi Muhammad S.A.W."),
    (r"[il1]?mlek|imlek|kongz[il1]li", "Tahun Baru Imlek"),
    (r"nyep[il1]|tahun\s*baru\s*saka", "Hari Suci Nyepi"),
    (r"wafat\s*(yesus|isa|jesus)", "Wafat Yesus Kristus"),
    (r"kebangk[il1]tan|paskah", "Kebangkitan Yesus Kristus (Paskah)"),
    (r"hari\s*buruh", "Hari Buruh Internasional"),
    (r"wa[il1]sak|waisak", "Hari Raya Waisak"),
    (r"kena[il1]kan\s*(yesus|isa|jesus)", "Kenaikan Yesus Kristus"),
    (r"pancas[il1]la", "Hari Lahir Pancasila"),
    (r"muharr?am|tahun\s*baru\s*[il1]slam", "1 Muharam Tahun Baru Islam"),
    (r"prok[l1]amasi|kemerdekaan", "Proklamasi Kemerdekaan"),
    (r"mau[l1][il1]d|maulid", "Maulid Nabi Muhammad S.A.W."),
    (r"kelah[il1]ran\s*yesus|natal", "Kelahiran Yesus Kristus"),
    (r"tahun\s*baru\s*\d*\s*masehi|tahun\s*baru\s*masehi", "Tahun Baru Masehi"),
]

KEY_DATES_HINTS = [
    r"idul\s*fitri",
    r"proklamasi|17\s*agustus",
    r"kelahiran\s*yesus|natal|25\s*desember",
]

# --- Anchor (tanggal tidak tetap, HARUS dari extract/OCR) ---
# Dipakai untuk menilai apakah ekstraksi sudah "menangkap inti" kalender.
ANCHOR_HOLIDAYS = [
    ("imlek", r"[il1]?mlek|imlek|kongz[il1]li"),
    ("idul_fitri", r"(?:idul|1dul|ldul|iudl)\s*f[il1]tr[il1]|idulfitri|1dulfitri"),
    ("idul_adha", r"(?:idul|1dul|ldul)\s*a[dth]ha|iduladha"),
    ("kenaikan_yesus", r"kena[il1]kan\s*(yesus|isa|jesus)"),
    ("wafat_yesus", r"wafat\s*(yesus|isa|jesus)"),
]
# Minimal berapa anchor yang harus ketemu agar Extract dianggap memadai
MIN_ANCHORS_OK = int(os.getenv("MIN_ANCHORS_OK", "4"))

# --- Tanggal tetap nasional (bisa diisi manual jika OCR/Extract melewatkan) ---
# Format: (bulan, hari, nama kanonik)
FIXED_NATIONAL_HOLIDAYS = [
    (1, 1, "Tahun Baru {year} Masehi"),
    (5, 1, "Hari Buruh Internasional"),
    (6, 1, "Hari Lahir Pancasila"),
    (8, 17, "Proklamasi Kemerdekaan"),
    (12, 25, "Kelahiran Yesus Kristus"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

KNOWN_PDF_URLS: Dict[int, List[str]] = {
    2027: ["https://data-jdih.menpan.go.id/dokumen/2026skb002.pdf"],
    2026: ["https://data-jdih.menpan.go.id/dokumen/2025skbmenpanrb005.pdf"],
    2025: [
        "https://jdih.kemenkoinfra.go.id/cfind/source/files/keputusan-bersama-3-menteri-nomor-1017-2-2-tahun-2024.pdf",
        "https://www.kemenkopmk.go.id/sites/default/files/artikel/2025-08/SKB%20Perubahan%20Libur%20Nasional%20dan%20Cuti%20Bersama%20Tahun%202025.pdf",
    ],
}

# runtime
_ilovepdf_used = 0
_jdih_cache_path = Path(tempfile.gettempdir()) / "kalender_jdih_cache.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def log_result(year: int, action: str, reason: str, credits: int = 0) -> None:
    print(f"RESULT year={year} action={action} reason={reason} credits_est={credits}")


# ============================================================
# GITHUB
# ============================================================
def _gh_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_url(path: str) -> str:
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FOLDER}/{path}"


def github_enabled() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def github_get_json(filename: str) -> Tuple[Optional[Dict], Optional[str]]:
    if not github_enabled():
        return None, None
    try:
        r = requests.get(
            _gh_url(filename), headers=_gh_headers(),
            params={"ref": GITHUB_BRANCH}, timeout=25,
        )
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        body = r.json()
        raw = base64.b64decode(body["content"]).decode("utf-8")
        return json.loads(raw), body.get("sha")
    except Exception as e:
        print(f"[!] Baca GitHub {filename}: {e}")
        return None, None


def github_put_json(filename: str, data: Dict, message: str) -> bool:
    if not github_enabled():
        print(f"[!] GitHub belum diset → skip {filename}")
        return False
    _, sha = github_get_json(filename)
    payload = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(_gh_url(filename), headers=_gh_headers(), json=payload, timeout=45)
        if r.status_code in (200, 201):
            print(f"[✓] GitHub ← {GITHUB_FOLDER}/{filename}")
            return True
        print(f"[!] Upload HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[!] Upload: {e}")
        return False


def empty_index() -> Dict:
    return {"years": {}, "latest_year": None, "next_check_after": None, "updated_at": iso_now()}


def load_index() -> Dict:
    data, _ = github_get_json("index.json")
    if data:
        return data
    p = Path("index.json")
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return empty_index()


def save_index(index: Dict) -> None:
    index["updated_at"] = iso_now()
    ys = [int(y) for y in (index.get("years") or {}).keys()]
    index["latest_year"] = max(ys) if ys else None
    Path("index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    github_put_json("index.json", index, f"Update index.json ({now_utc().strftime('%Y-%m-%d')})")


# ============================================================
# COMPLETE / HASH / SEASON
# ============================================================
def _holiday_names_blob(data: Dict) -> str:
    return " ".join(
        h.get("name", "") for h in (data.get("national_holidays") or [])
    ).lower()


def detect_anchors(data: Dict) -> Dict[str, bool]:
    """Deteksi anchor (libur tanggal tidak tetap) dari hasil parse."""
    blob = _holiday_names_blob(data)
    found: Dict[str, bool] = {}
    for key, pat in ANCHOR_HOLIDAYS:
        found[key] = bool(re.search(pat, blob, re.I))
    return found


def count_anchors(data: Dict) -> int:
    return sum(1 for v in detect_anchors(data).values() if v)


def has_key_holidays(data: Dict) -> bool:
    """Kompatibel lama: minimal 2 hint, ATAU cukup anchor modern."""
    blob = _holiday_names_blob(data)
    hits = sum(1 for pat in KEY_DATES_HINTS if re.search(pat, blob, re.I))
    if hits >= 2:
        return True
    return count_anchors(data) >= min(3, MIN_ANCHORS_OK)


def inject_fixed_holidays(data: Dict) -> Dict:
    """Tambahkan libur nasional tanggal tetap yang hilang dari hasil extract.

    Tanggal tetap (1 Jan, 1 Mei, 1 Jun, 17 Agust, 25 Des) tidak berubah tiap tahun,
    jadi aman diisi manual jika OCR/Extract melewatkannya. Anchor (Imlek, Idul Fitri,
    dll.) TIDAK diisi manual — harus dari dokumen.
    """
    year = int(data.get("year") or 0)
    if not year:
        return data
    existing = {h.get("date") for h in (data.get("national_holidays") or [])}
    added = []
    for month, day, name_tpl in FIXED_NATIONAL_HOLIDAYS:
        iso = f"{year}-{month:02d}-{day:02d}"
        if iso in existing:
            continue
        name = name_tpl.replace("{year}", str(year))
        # hitung hari dalam seminggu (opsional, lokal)
        try:
            from datetime import date as _date
            wd = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][
                _date(year, month, day).weekday()
            ]
        except Exception:
            wd = ""
        added.append({
            "date": iso,
            "day": wd,
            "name": name,
            "type": "national_holiday",
            "source": "fixed_calendar",
        })
        existing.add(iso)
    if added:
        national = list(data.get("national_holidays") or []) + added
        national = sorted(national, key=lambda x: x.get("date") or "")
        # dedupe by date
        seen = set()
        deduped = []
        for h in national:
            d = h.get("date")
            if d and d not in seen:
                seen.add(d)
                deduped.append(h)
        data = dict(data)
        data["national_holidays"] = deduped
        data["total_national"] = len(deduped)
        print(f"[+] Inject fixed holidays: {[a['date'] for a in added]}")
    return data


def is_complete(data: Dict) -> bool:
    n = int(data.get("total_national") or 0)
    j = int(data.get("total_joint_leave") or 0)
    anchors_map = detect_anchors(data)
    anchors = sum(1 for v in anchors_map.values() if v)
    # Idul Fitri hampir selalu 2 hari nasional — minimal 1 tanggal Fitri harus ada
    fitri_dates = sum(
        1 for h in (data.get("national_holidays") or [])
        if re.search(r"idul\s*fitri|1dul\s*fitri", h.get("name") or "", re.I)
    )
    has_fitri = anchors_map.get("idul_fitri") or fitri_dates >= 1
    # Lengkap ketat
    if (
        n >= MIN_NATIONAL_COMPLETE
        and j >= MIN_JOINT_COMPLETE
        and anchors >= MIN_ANCHORS_OK
        and has_fitri
        and fitri_dates >= 1
    ):
        return True
    # Soft
    if (
        n >= MIN_NATIONAL_SOFT
        and j >= MIN_JOINT_COMPLETE
        and anchors >= MIN_ANCHORS_OK
        and has_fitri
    ):
        return True
    if n >= MIN_NATIONAL_COMPLETE and j >= MIN_JOINT_COMPLETE and has_key_holidays(data) and has_fitri:
        return True
    return False


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def in_skb_season(today: Optional[datetime] = None) -> bool:
    """True di Sep–Dec atau Jan (musim rilis SKB tahun berikutnya)."""
    d = (today or now_utc()).date()
    return d.month >= SEASON_START_MONTH or d.month <= SEASON_END_MONTH


def default_next_check(after_success_year: Optional[int] = None) -> str:
    n = now_utc()
    if after_success_year:
        # pantau tahun berikutnya mulai 1 Sep tahun after_success_year
        target = datetime(after_success_year, SEASON_START_MONTH, 1, tzinfo=timezone.utc)
        if target.date() < n.date():
            target = n + timedelta(days=RETRY_DAYS_NOT_FOUND)
        return target.date().isoformat()
    if not in_skb_season(n):
        # lompat ke 1 Sep tahun ini / depan
        y = n.year if n.month < SEASON_START_MONTH else n.year + 1
        return datetime(y, SEASON_START_MONTH, 1, tzinfo=timezone.utc).date().isoformat()
    return (n + timedelta(days=RETRY_DAYS_NOT_FOUND)).date().isoformat()


def should_check_now(index: Dict, force: bool) -> bool:
    if force:
        return True
    nca = index.get("next_check_after")
    if not nca:
        return True
    try:
        s = str(nca)[:10]
        due = datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return True
    return now_utc().date() >= due


def years_to_process(
    index: Dict, year: Optional[int], from_year: Optional[int], force: bool
) -> List[int]:
    cur = now_utc().year
    if year is not None:
        return [year]
    if from_year is not None:
        return list(range(from_year, max(cur + 1, from_year) + 1))

    years_map = index.get("years") or {}
    incomplete = [int(y) for y, m in years_map.items() if not m.get("complete")]
    latest = index.get("latest_year")

    if latest is None:
        targets = [cur, cur + 1]
    else:
        targets = [int(latest) + 1]
        # di luar musim: jangan paksa cek tahun baru kecuali incomplete
        if not in_skb_season() and not force:
            targets = []

    targets = sorted(set(targets + incomplete))
    if not should_check_now(index, force):
        print(f"[*] Belum waktunya (next_check_after={index.get('next_check_after')})")
        print("    Gunakan --year / --from / --force untuk memaksa.")
        return []
    return targets


def needs_scrape(index: Dict, y: int, force: bool) -> bool:
    meta = (index.get("years") or {}).get(str(y))
    if force:
        return True
    if not meta:
        return True
    if not meta.get("complete"):
        return True
    return False


# ============================================================
# JDIH CACHE + PDF RESOLVE
# ============================================================
def load_jdih_cache() -> Dict:
    try:
        if _jdih_cache_path.exists():
            return json.loads(_jdih_cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_jdih_cache(cache: Dict) -> None:
    try:
        _jdih_cache_path.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def jdih_years_from_page() -> Set[int]:
    cache = load_jdih_cache()
    ts = cache.get("ts")
    if ts:
        try:
            age = now_utc() - datetime.fromisoformat(ts)
            if age < timedelta(hours=JDIH_CACHE_HOURS) and cache.get("years"):
                print(f"[*] JDIH cache hit: {cache['years']}")
                return set(cache["years"])
        except Exception:
            pass

    years: Set[int] = set()
    if not HAS_SELENIUM:
        return years

    print("[*] Load daftar JDIH...")
    driver = create_driver()
    try:
        driver.get(LIST_URL)
        time.sleep(3)
        dismiss_alert(driver)
        text = BeautifulSoup(driver.page_source, "html.parser").get_text(" ", strip=True)
        for m in re.finditer(
            r"(?:Hari Libur Nasional dan )?Cuti Bersama\s+Tahun\s+(\d{4})",
            text,
            re.I,
        ):
            years.add(int(m.group(1)))
        for m in re.finditer(
            r"Hari Libur Nasional dan Cuti Bersama\s+Tahun\s+(\d{4})", text, re.I
        ):
            years.add(int(m.group(1)))
        print(f"[+] JDIH years: {sorted(years)}")
        save_jdih_cache({"ts": iso_now(), "years": sorted(years)})
    except Exception as e:
        print(f"[!] JDIH: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return years


def create_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--mute-audio")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--js-flags=--max-old-space-size=256")
    options.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    options.page_load_strategy = "eager"
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    for p in (os.getenv("CHROME_BIN", ""), "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if p and Path(p).exists():
            options.binary_location = p
            break
    dp = None
    for p in (os.getenv("CHROMEDRIVER_PATH", ""), "/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"):
        if p and Path(p).exists():
            dp = p
            break
    service = Service(executable_path=dp) if dp else Service()
    d = webdriver.Chrome(service=service, options=options)
    d.set_page_load_timeout(25)
    return d


def dismiss_alert(driver):
    try:
        Alert(driver).accept()
        time.sleep(0.2)
    except NoAlertPresentException:
        pass
    except Exception:
        pass


def url_looks_like_pdf(url: str) -> bool:
    try:
        r = requests.head(url, headers=HEADERS, timeout=12, allow_redirects=True)
        if r.status_code != 200:
            r = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        ctype = (r.headers.get("content-type") or "").lower()
        clen = int(r.headers.get("content-length") or 0)
        return r.status_code == 200 and ("pdf" in ctype or "octet" in ctype or clen > 5000)
    except Exception:
        return False


def try_direct_pdf(year: int) -> Optional[str]:
    urls = list(KNOWN_PDF_URLS.get(year, []))
    for name in (
        f"{year-1}skb002.pdf",
        f"{year-1}skbmenpanrb002.pdf",
        f"{year-1}skbmenpanrb005.pdf",
        f"{year}skb002.pdf",
    ):
        urls.append(f"{PDF_BASE}/{name}")
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        if url_looks_like_pdf(url):
            print(f"[+] PDF URL: {url}")
            return url
    return None


def extract_pdf_from_unduh(driver) -> Optional[str]:
    for btn in driver.find_elements(
        By.XPATH, "//button[contains(translate(., 'UNDUH', 'unduh'), 'unduh')]"
    ):
        try:
            attrs = driver.execute_script(
                "var e=arguments[0],o={};for(var a of e.attributes)o[a.name]=a.value;return o;",
                btn,
            )
            val = (attrs.get("@click") or attrs.get("x-on:click") or attrs.get("onclick") or "")
            val = val.replace("\\/", "/")
            m = re.search(r"https?://[^\s'\"<>]+?\.pdf", val)
            if m:
                return m.group(0)
        except Exception:
            continue
    for a in driver.find_elements(By.CSS_SELECTOR, "a[href$='.pdf']"):
        href = a.get_attribute("href") or ""
        if href.endswith(".pdf"):
            return href
    return None


def scrape_pdf_url_selenium(target_year: int) -> Optional[str]:
    if not HAS_SELENIUM:
        return None
    print(f"[*] Selenium PDF tahun {target_year}...")
    driver = create_driver()
    try:
        driver.get(LIST_URL)
        time.sleep(3)
        dismiss_alert(driver)
        for i in range(10):
            buttons = driver.find_elements(
                By.XPATH, "//button[contains(normalize-space(.),'Lihat')]"
            )
            if i >= len(buttons):
                break
            try:
                btn = buttons[i]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.3)
                try:
                    btn.click()
                except ElementNotInteractableException:
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                dismiss_alert(driver)
                page = BeautifulSoup(driver.page_source, "html.parser").get_text(" ", strip=True)
                if re.search(rf"Cuti Bersama\s+Tahun\s+{target_year}", page, re.I):
                    pdf = extract_pdf_from_unduh(driver)
                    if pdf:
                        print(f"[+] Selenium PDF: {pdf}")
                        return pdf
                driver.get(LIST_URL)
                time.sleep(1.5)
                dismiss_alert(driver)
            except UnexpectedAlertPresentException:
                dismiss_alert(driver)
            except Exception:
                try:
                    driver.get(LIST_URL)
                    time.sleep(1.5)
                except Exception:
                    pass
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def resolve_pdf_urls(year: int) -> List[str]:
    urls: List[str] = []
    for u in KNOWN_PDF_URLS.get(year, []):
        if u not in urls and url_looks_like_pdf(u):
            urls.append(u)
    u = try_direct_pdf(year)
    if u and u not in urls:
        urls.append(u)
    if not urls:
        u2 = scrape_pdf_url_selenium(year)
        if u2 and u2 not in urls:
            urls.append(u2)
    return urls


# ============================================================
# DOWNLOAD + OCR
# ============================================================
def download_pdf(pdf_url: str) -> Path:
    print(f"[*] Download PDF...")
    resp = requests.get(pdf_url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    name = pdf_url.split("/")[-1].split("?")[0] or "skb.pdf"
    if not name.endswith(".pdf"):
        name += ".pdf"
    tmp = Path(tempfile.gettempdir()) / f"skb_{name}"
    tmp.write_bytes(resp.content)
    print(f"[+] {len(resp.content):,} bytes, sha256={file_sha256(tmp)[:12]}...")
    return tmp



def _read_ilovepdf_text(path: Path) -> str:
    """Baca output Extract iLovePDF yang sering UTF-16 / null-padded."""
    raw = path.read_bytes()
    if not raw:
        return ""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except Exception:
            pass
    if raw.count(b"\x00") > max(10, len(raw) // 4):
        try:
            return raw.decode("utf-16-le", errors="replace").replace("\ufeff", "")
        except Exception:
            return raw.decode("utf-8", errors="replace").replace("\x00", "")
    out = raw.decode("utf-8", errors="replace")
    if "\x00" in out:
        out = out.replace("\x00", "")
    return out


def _ilovepdf_available() -> bool:
    if not ILOVEPDF_PUBLIC_KEY or not ILOVEPDF_SECRET_KEY:
        return False
    if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN:
        return False
    try:
        from ilovepdf import ExtractTask  # noqa: F401
        return True
    except ImportError:
        return False


def _ilovepdf_extract_only(pdf_path: Path) -> Optional[str]:
    """Extract teks dari PDF tanpa OCR. Biaya API: 10 kredit/file."""
    global _ilovepdf_used
    if not _ilovepdf_available():
        return None
    try:
        from ilovepdf import ExtractTask
    except ImportError:
        return None

    out_dir = Path(tempfile.mkdtemp(prefix="ilovepdf_ext_"))
    try:
        print("[*] iLovePDF Extract (tanpa OCR, hemat)...")
        ext = ExtractTask(ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY)
        ext.add_file(str(pdf_path))
        ext.execute()
        ext.set_output_filename("extract.txt")
        ext.download(str(out_dir))
        _ilovepdf_used += 1
        txts = list(out_dir.glob("*.txt"))
        if not txts:
            print("[!] Extract: tidak ada file .txt")
            return None
        content = _read_ilovepdf_text(txts[0])
        rem = ""
        try:
            if hasattr(ext, "remaining_credits") and ext.remaining_credits is not None:
                rem = f", remaining_credits={ext.remaining_credits}"
        except Exception:
            pass
        print(f"[+] Extract {len(content)} chars (pdfs_used≈{_ilovepdf_used}{rem})")
        return content
    except Exception as e:
        print(f"[!] iLovePDF Extract: {e}")
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _ilovepdf_ocr_then_extract(pdf_path: Path) -> Optional[str]:
    """OCR dulu lalu Extract. Biaya API: 5 kredit/halaman + 10 kredit/file."""
    global _ilovepdf_used
    if not _ilovepdf_available():
        if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN:
            print(f"[!] Batas MAX_ILOVEPDF_DOCS_PER_RUN={MAX_ILOVEPDF_DOCS_PER_RUN} tercapai")
        return None
    try:
        from ilovepdf import PdfOcrTask, ExtractTask
    except ImportError:
        print("[*] ilovepdf package belum terpasang")
        return None

    out_dir = Path(tempfile.mkdtemp(prefix="ilovepdf_ocr_"))
    try:
        print("[*] iLovePDF OCR (ind+eng)...")
        task = PdfOcrTask(ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY)
        f = task.add_file(str(pdf_path))
        try:
            f.ocr_languages = ["ind", "eng"]
        except Exception:
            try:
                f.ocr_languages = "ind"
            except Exception:
                pass
        task.execute()
        task.set_output_filename("ocr_result.pdf")
        task.download(str(out_dir))
        ocr_pdf = out_dir / "ocr_result.pdf"
        if not ocr_pdf.exists():
            pdfs = list(out_dir.glob("*.pdf"))
            if not pdfs:
                return None
            ocr_pdf = pdfs[0]

        print("[*] iLovePDF Extract (setelah OCR)...")
        ext = ExtractTask(ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY)
        ext.add_file(str(ocr_pdf))
        ext.execute()
        ext.set_output_filename("extract.txt")
        ext.download(str(out_dir))
        _ilovepdf_used += 1
        txts = list(out_dir.glob("*.txt"))
        if not txts:
            return None
        content = _read_ilovepdf_text(txts[0])
        rem = ""
        try:
            if hasattr(ext, "remaining_credits") and ext.remaining_credits is not None:
                rem = f", remaining_credits={ext.remaining_credits}"
        except Exception:
            pass
        print(f"[+] OCR+Extract {len(content)} chars (pdfs_used≈{_ilovepdf_used}{rem})")
        return content
    except Exception as e:
        print(f"[!] iLovePDF OCR+Extract: {e}")
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def extract_quality_ok(data: Dict) -> bool:
    """Apakah hasil parse cukup baik sehingga OCR tidak diperlukan?

    Prioritas: anchor (Imlek, Idul Fitri, Idul Adha, Kenaikan, Wafat Yesus).
    Tanggal tetap bisa di-inject nanti, jadi tidak wajib dari Extract.
    """
    n = int(data.get("total_national") or 0)
    j = int(data.get("total_joint_leave") or 0)
    anchors = detect_anchors(data)
    n_anchors = sum(1 for v in anchors.values() if v)
    missing = [k for k, v in anchors.items() if not v]

    if is_complete(data):
        print(f"[*] Anchor OK (complete): {n_anchors}/{len(ANCHOR_HOLIDAYS)}")
        return True

    # Anchor adalah kriteria utama — tanggal tidak tetap harus ketemu
    if n_anchors >= MIN_ANCHORS_OK and j >= MIN_JOINT_SKIP_OCR:
        print(f"[*] Anchor memadai ({n_anchors}/{len(ANCHOR_HOLIDAYS)}) → skip OCR")
        return True

    if n_anchors >= MIN_ANCHORS_OK and n >= MIN_NATIONAL_SKIP_OCR and j >= 2:
        print(f"[*] Anchor memadai + nasional cukup → skip OCR")
        return True

    print(
        f"[*] Anchor kurang ({n_anchors}/{len(ANCHOR_HOLIDAYS)}, missing={missing}) "
        f"nasional={n} cuti={j} → perlu OCR"
    )
    return False


def ocr_via_ilovepdf(pdf_path: Path, year: int) -> Optional[str]:
    """Alur hemat berbasis kualitas parse:
    1. Extract saja (banyak SKB sudah punya layer teks).
    2. Parse → jika jumlah libur/cuti sudah memadai → skip OCR.
    3. Jika kurang → OCR + Extract, pilih hasil yang lebih baik.
    """
    if not ILOVEPDF_PUBLIC_KEY or not ILOVEPDF_SECRET_KEY:
        print("[*] iLovePDF key belum diset")
        return None
    if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN:
        print(f"[!] Batas MAX_ILOVEPDF_DOCS_PER_RUN={MAX_ILOVEPDF_DOCS_PER_RUN} tercapai")
        return None

    # Langkah 1: Extract tanpa OCR
    text = _ilovepdf_extract_only(pdf_path)
    if text and text.strip():
        parsed = extract_from_ocr(text, year)
        n, j = parsed["total_national"], parsed["total_joint_leave"]
        print(f"[*] Parse Extract → nasional={n} cuti={j}")
        if extract_quality_ok(parsed):
            print("[*] Hasil Extract memadai → skip OCR (hemat kredit halaman)")
            return text
        print(f"[*] Hasil Extract kurang (butuh ≥{MIN_NATIONAL_SKIP_OCR} nasional "
              f"& ≥{MIN_JOINT_SKIP_OCR} cuti, atau complete) → lanjut OCR")
    else:
        print("[*] Extract gagal/kosong → lanjut OCR")

    # Langkah 2: OCR + Extract
    if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN:
        print("[!] Kuota lokal habis setelah Extract; pakai hasil Extract / Tesseract")
        return text

    ocr_text = _ilovepdf_ocr_then_extract(pdf_path)
    if not ocr_text or not ocr_text.strip():
        return text

    if not text or not text.strip():
        return ocr_text

    # Pilih teks yang hasil parse-nya lebih baik
    p_ext = extract_from_ocr(text, year)
    p_ocr = extract_from_ocr(ocr_text, year)
    score_ext = p_ext["total_national"] + p_ext["total_joint_leave"]
    score_ocr = p_ocr["total_national"] + p_ocr["total_joint_leave"]
    print(f"[*] Bandingkan Extract({score_ext}) vs OCR+Extract({score_ocr})")
    if score_ocr >= score_ext:
        return ocr_text
    return text


def ocr_via_tesseract(pdf_path: Path) -> str:
    """OCR lokal hemat CPU: DPI rendah, 1× PSM, skip halaman tak relevan, 1 thread."""
    if not HAS_TESSERACT:
        return ""
    # Batasi thread native (Tesseract/OpenMP/BLAS) agar tidak saturasi semua core
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")

    dpi = int(os.getenv("TESSERACT_DPI", "180"))  # default 180 (dulu 300)
    max_pages = int(os.getenv("TESSERACT_MAX_PAGES", "4"))
    print(f"[*] Tesseract lokal (dpi={dpi}, max_pages={max_pages})...")

    # JPEG + dpi rendah + 1 thread → jauh lebih hemat RAM/CPU daripada PNG 300dpi
    try:
        images = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            fmt="jpeg",
            thread_count=1,
            first_page=1,
            last_page=max_pages + 2,  # sedikit buffer, nanti difilter
        )
    except Exception as e:
        print(f"[!] pdf2image: {e}")
        try:
            images = convert_from_path(str(pdf_path), dpi=dpi, fmt="jpeg", thread_count=1)
        except Exception as e2:
            print(f"[!] pdf2image fallback: {e2}")
            return ""

    if not images:
        return ""

    # Prioritas: halaman tengah/akhir (tabel libur biasanya bukan cover)
    # Ambil hingga max_pages halaman paling relevan
    n = len(images)
    if n <= max_pages:
        page_idxs = list(range(n))
    else:
        # skip cover (0), ambil sisa dari belakang / tengah
        page_idxs = list(range(1, n))[-max_pages:]

    parts = []
    cfg = "--psm 6 --oem 1"  # LSTM only, 1 pass (bukan oem 3 + 2× psm)
    for i in page_idxs:
        try:
            g = images[i].convert("L")
            # autocontrast ringan saja; tanpa SHARPEN & Contrast 1.5 (boros)
            g = ImageOps.autocontrast(g, cutoff=2)
            # perkecil lebar max 1600px jika terlalu besar
            w, h = g.size
            if w > 1600:
                ratio = 1600 / w
                g = g.resize((1600, int(h * ratio)))
            t = pytesseract.image_to_string(g, lang="ind+eng", config=cfg)
        except Exception as e:
            print(f"    Halaman {i+1}: error {e}")
            t = ""
        # skip halaman hampir kosong (cover/tanda tangan)
        if len(t.strip()) < 80:
            print(f"    Halaman {i+1}: skip ({len(t.strip())} chars)")
            continue
        parts.append(f"=== PAGE {i+1} ===\n{t}")
        print(f"    Halaman {i+1}: {len(t)} karakter")
        # early exit jika sudah ketemu section libur + cuti
        blob = "\n".join(parts).lower()
        if "hari libur" in blob and "cuti bersama" in blob and len(blob) > 1500:
            print("    [*] Section libur+cuti lengkap → stop halaman berikutnya")
            break

    # bebaskan memori gambar
    del images
    return "\n".join(parts)


def ocr_pdf_text(pdf_path: Path, year: int) -> str:
    t = ocr_via_ilovepdf(pdf_path, year)
    if t:
        return t
    return ocr_via_tesseract(pdf_path)


# ============================================================
# PARSER + NORMALISASI
# ============================================================

# Peta typo OCR umum (huruf/angka sering tertukar)
_OCR_WORD_FIXES = [
    (r"\b1dul\b", "Idul"),
    (r"\bldul\b", "Idul"),
    (r"\bIudl\b", "Idul"),
    (r"\bIdol\b", "Idul"),
    (r"\bF[il1]tr[il1]\b", "Fitri"),
    (r"\bWa[il1]sak\b", "Waisak"),
    (r"\bNyep[il1]\b", "Nyepi"),
    (r"\bIm[l1]ek\b", "Imlek"),
    (r"\b[l1]mlek\b", "Imlek"),
    (r"\b[l1]sra\b", "Isra"),
    (r"\bM[il1]kraj\b", "Mikraj"),
    (r"\bProk[l1]amasi\b", "Proklamasi"),
    (r"\bKemerdekaa[nm]\b", "Kemerdekaan"),
    (r"\bPancas[il1]la\b", "Pancasila"),
    (r"\bMau[l1][il1]d\b", "Maulid"),
    (r"\bKena[il1]kan\b", "Kenaikan"),
    (r"\bKebangk[il1]tan\b", "Kebangkitan"),
    (r"\blanuari\b", "Januari"),
    (r"\bFcbruari\b", "Februari"),
    (r"\bFebruarr?[il1]\b", "Februari"),
    (r"\bAgus[tl]us\b", "Agustus"),
    (r"\bDesemb[ce]r\b", "Desember"),
    (r"\b0ktober\b", "Oktober"),
    (r"\b0ktobcr\b", "Oktober"),
    (r"\bNopember\b", "November"),
    (r"\bH[il1]jriah\b", "Hijriah"),
    (r"\bKongz[il1]li\b", "Kongzili"),
]


def fix_ocr_text(text: str) -> str:
    """Perbaiki typo OCR tingkat kata sebelum parsing."""
    if not text:
        return text
    t = text
    for pat, repl in _OCR_WORD_FIXES:
        t = re.sub(pat, repl, t, flags=re.I)
    # Angka OCR: O/o di posisi digit → 0, l/I di posisi digit → 1 (hati-hati)
    # Hanya di pola "N Bulan" / rentang tanggal
    def _fix_day_token(m: re.Match) -> str:
        raw = m.group(1)
        fixed = raw.replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1").replace("|", "1")
        if fixed.isdigit() and 1 <= int(fixed) <= 31:
            return fixed + m.group(2)
        return m.group(0)
    month_pat = (
        r"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)"
    )
    t = re.sub(rf"\b([0-9OolI|]{{1,2}})(\s+{month_pat})", _fix_day_token, t, flags=re.I)
    return t


def safe_iso_date(year: int, month: int, day: int) -> Optional[str]:
    """Validasi tanggal kalender; tolak 31 Feb, day=0, dll."""
    try:
        from datetime import date as _date
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        _date(year, month, day)  # raises on invalid
        return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        return None


def normalize_holiday_name(name: str) -> str:
    n = fix_ocr_text(name or "")
    n = re.sub(r"\s+", " ", n).strip(" .|=-")
    # Buang sampah OCR umum di akhir/tengah
    n = re.sub(
        r"\b(?:Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)\s+dan\b.*$",
        "", n, flags=re.I,
    )
    n = re.sub(r"\bdan\s*$", "", n, flags=re.I)
    n = re.sub(r"\s{2,}", " ", n).strip(" .|=-")
    low = n.lower()
    for pat, canon in NAME_NORMALIZE:
        if re.search(pat, low, re.I):
            tail = ""
            if canon == "Hari Suci Nyepi":
                ym = re.search(r"(Tahun\s+Baru\s+Saka\s+\d{3,4}|\d{3,4}\s*Saka)", n, re.I)
                if ym:
                    tail = f" ({ym.group(0).strip()})"
                return (canon + tail).strip()
            ym = re.search(
                r"(\d{4}\s*(?:Hijriah|Kongzili|BE|Saka)?|\d{3,4}\s*Hijriah|\d{3,4}\s*Kongzili|\d{3,4}\s*BE).*$",
                n, re.I,
            )
            if ym and canon not in ("Tahun Baru Masehi",):
                tail = " " + ym.group(0).strip()
                # hentikan di kata sampah
                tail = re.split(r"\s+(?:Maret|dan|Senin|Selasa)\b", tail, maxsplit=1, flags=re.I)[0]
            if canon == "Tahun Baru Masehi":
                ym2 = re.search(r"20\d{2}", n)
                return f"Tahun Baru {ym2.group(0)} Masehi" if ym2 else canon
            return (canon + tail).strip()
    # Bersihkan sisa tanpa match kanonik
    n = re.sub(r"\b(?:Maret|April|Mei)\s+dan\b.*", "", n, flags=re.I).strip(" .|=-")
    return n


def parse_dates_from_chunk(chunk: str, year: int) -> List[str]:
    chunk = fix_ocr_text(chunk or "")
    month_pat = (
        r"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember|"
        r"lanuari|Fcbruari|Agus[tl]us|Desemb[ce]r|0ktober|Nopember)"
    )
    # alias bulan OCR → kanonik
    _mo_alias = {
        "lanuari": "januari", "fcbruari": "februari", "agustlus": "agustus",
        "agustus": "agustus", "desembcr": "desember", "desember": "desember",
        "0ktober": "oktober", "nopember": "november",
    }
    results: List[str] = []
    # Range beda bulan: 10 Maret - 11 Maret / 21 Maret – 22 Maret
    for m in re.finditer(
        rf"(\d{{1,2}})\s*{month_pat}\s*[-–]\s*(\d{{1,2}})\s*{month_pat}", chunk, re.I
    ):
        d1, mo1, d2, mo2 = int(m.group(1)), m.group(2).lower(), int(m.group(3)), m.group(4).lower()
        if MONTH_MAP.get(mo1):
            results.append(f"{year}-{MONTH_MAP[mo1]:02d}-{d1:02d}")
        if MONTH_MAP.get(mo2):
            results.append(f"{year}-{MONTH_MAP[mo2]:02d}-{d2:02d}")
    if results:
        return results
    # Range sama bulan: 21-22 Maret / 21 – 22 Maret
    m = re.search(
        rf"(\d{{1,2}})\s*[-–/]\s*(\d{{1,2}})\s+{month_pat}", chunk, re.I
    )
    if m:
        d1, d2, mo = int(m.group(1)), int(m.group(2)), MONTH_MAP.get(m.group(3).lower())
        if mo:
            lo, hi = min(d1, d2), max(d1, d2)
            # batasi span wajar (libur jarang > 5 hari beruntun)
            if 0 < hi - lo <= 5:
                return [f"{year}-{mo:02d}-{d:02d}" for d in range(lo, hi + 1)]
            return [f"{year}-{mo:02d}-{d1:02d}", f"{year}-{mo:02d}-{d2:02d}"]
    # "21 dan 22 Maret" / "20, 23, dan 24 Maret"
    m = re.search(
        rf"(\d{{1,2}}(?:\s*,\s*\d{{1,2}})*(?:\s*,?\s*dan\s*\d{{1,2}})+)\s+{month_pat}",
        chunk, re.I,
    )
    if m:
        days = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
        mo = MONTH_MAP.get(m.group(2).lower())
        if mo and days:
            return [f"{year}-{mo:02d}-{d:02d}" for d in days]
    m = re.search(
        rf"(\d{{1,2}}\s*,\s*\d{{1,2}}(?:\s*,\s*\d{{1,2}})*(?:\s*,?\s*dan\s*\d{{1,2}})?).{{0,50}}?{month_pat}",
        chunk, re.I,
    )
    if m:
        days = re.findall(r"\d{1,2}", m.group(1))
        mo = MONTH_MAP.get(m.group(2).lower())
        if mo and days:
            return [f"{year}-{mo:02d}-{int(d):02d}" for d in days]
    # Single date
    m = re.search(rf"(\d{{1,2}})\s+{month_pat}", chunk, re.I)
    if m:
        mo = MONTH_MAP.get(m.group(2).lower())
        if mo:
            iso = safe_iso_date(year, mo, int(m.group(1)))
            return [iso] if iso else []
    return []


def _filter_valid_dates(dates: List[str], year: int) -> List[str]:
    """Buang tanggal invalid / di luar tahun target."""
    out = []
    for iso in dates:
        try:
            y, m, d = [int(x) for x in iso.split("-")]
            if y != year:
                continue
            ok = safe_iso_date(y, m, d)
            if ok:
                out.append(ok)
        except Exception:
            continue
    return out


def weekday_name(iso: str) -> str:
    """Hitung nama hari dari tanggal ISO (lebih andal daripada OCR)."""
    try:
        from datetime import date as _date
        y, m, d = [int(x) for x in iso.split("-")]
        return ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][
            _date(y, m, d).weekday()
        ]
    except Exception:
        return ""


def _normalize_ocr_text(text: str) -> str:
    """Normalisasi teks hasil ExtractTask / Tesseract agar struktur baris
    lebih mudah diparse. iLovePDF Extract sering meratakan newline."""
    t = fix_ocr_text(text or "")
    t = t.replace("|", " ").replace("]", " ").replace("[", " ")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # Sisipkan newline sebelum header section
    t = re.sub(
        r"(?<!\n)\s*(A\.\s*HARI\s+LIBUR\s+NASIONAL)",
        r"\n\1", t, flags=re.I,
    )
    t = re.sub(
        r"(?<!\n)\s*(B\.\s*CUTI\s+BERSAMA)",
        r"\n\1", t, flags=re.I,
    )
    # Sisipkan newline sebelum pola tanggal (hari + angka + bulan)
    month_pat = (
        r"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)"
    )
    t = re.sub(
        rf"(?<!\n)\s+(\d{{1,2}}\s+{month_pat})",
        r"\n\1", t, flags=re.I,
    )
    t = re.sub(
        rf"(?<!\n)\s+((?:Senin|Selasa|Rabu|Kamis|Jumat|Jum'?at|Sabtu|Minggu)\s+\d{{1,2}}\s+{month_pat})",
        r"\n\1", t, flags=re.I,
    )
    # Rapatkan spasi berlebih tapi pertahankan newline
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()



def _ensure_idul_fitri_pair(
    national: List[Dict], joint: List[Dict], text: str, year: int
) -> Tuple[List[Dict], List[Dict]]:
    """Idul Fitri nasional hampir selalu 2 hari beruntun. Jika hanya 1 ketemu,
    cari pasangan di teks atau hari tetangga yang masuk akal."""
    fitri_pat = re.compile(r"idul\s*fitri|idulfitri|1dul\s*fitri|ldul\s*fitri", re.I)

    def is_fitri(h: Dict) -> bool:
        return bool(fitri_pat.search(h.get("name") or ""))

    nat_fitri = [h for h in national if is_fitri(h)]
    if len(nat_fitri) >= 2:
        return national, joint
    if len(nat_fitri) == 0:
        # Coba temukan di teks: "21 dan 22 Maret" dekat Idul Fitri
        m = re.search(
            rf"(idul\s*fitri|1dul\s*fitri).{{0,80}}?(\d{{1,2}})\s*(?:dan|,|[-–])\s*(\d{{1,2}})\s*"
            rf"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)",
            text, re.I | re.DOTALL,
        )
        if not m:
            m = re.search(
                rf"(\d{{1,2}})\s*(?:dan|,|[-–])\s*(\d{{1,2}})\s*"
                rf"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)"
                rf".{{0,60}}?(idul\s*fitri|1dul\s*fitri)",
                text, re.I | re.DOTALL,
            )
        if m:
            groups = m.groups()
            # extract day1, day2, month from whichever pattern matched
            nums = [int(x) for x in groups if x and x.isdigit()]
            mon = None
            for g in groups:
                if g and g.lower() in MONTH_MAP:
                    mon = MONTH_MAP[g.lower()]
                    break
            if mon and len(nums) >= 2:
                for d in nums[:2]:
                    iso = f"{year}-{mon:02d}-{d:02d}"
                    if not any(h.get("date") == iso for h in national):
                        national.append({
                            "date": iso,
                            "day": weekday_name(iso),
                            "name": "Idul Fitri",
                            "type": "national_holiday",
                        })
        return national, joint

    # Tepat 1 tanggal Fitri nasional → tambah tetangga ±1 hari jika disebut di teks
    only = nat_fitri[0]
    y, m, d = [int(x) for x in only["date"].split("-")]
    from datetime import date as _date, timedelta
    base = _date(y, m, d)
    candidates = [base - timedelta(days=1), base + timedelta(days=1)]
    # Cari angka hari tetangga di teks dekat kata Fitri
    blob = text.lower()
    for cand in candidates:
        day_num = cand.day
        # angka hari muncul di teks
        if not re.search(rf"\b{day_num}\b", blob):
            continue
        iso = cand.isoformat()
        # jangan duplikat / jangan ambil yang sudah cuti saja tanpa national
        if any(h.get("date") == iso and is_fitri(h) for h in national):
            continue
        # jika sudah di joint sebagai fitri, promote? biasanya 2 hari nasional dulu
        national.append({
            "date": iso,
            "day": weekday_name(iso),
            "name": only.get("name") or "Idul Fitri",
            "type": "national_holiday",
        })
        print(f"[+] Lengkapi pasangan Idul Fitri nasional: {iso}")
        break
    return national, joint


def extract_from_ocr(text: str, year: int) -> Dict:
    text_norm = _normalize_ocr_text(text)
    section_a = section_b = ""
    m_a = re.search(
        r"A\.\s*HARI\s+LIBUR\s+NASIONAL.*?(?=B\.\s*CUTI\s+BERSAMA|$)",
        text_norm, re.DOTALL | re.I,
    )
    if m_a:
        section_a = m_a.group(0)
    m_b = re.search(
        r"B\.\s*CUTI\s+BERSAMA.*?(?=MENTERI\s+AGAMA|PLT\.|MENTERI\s+KETENAGAKERJAAN|$)",
        text_norm, re.DOTALL | re.I,
    )
    if m_b:
        section_b = m_b.group(0)

    # Fallback header tanpa "A." / "B." (kadang OCR hilang nomor)
    if not section_a:
        m_a2 = re.search(
            r"HARI\s+LIBUR\s+NASIONAL.*?(?=CUTI\s+BERSAMA|$)",
            text_norm, re.DOTALL | re.I,
        )
        if m_a2:
            section_a = m_a2.group(0)
    if not section_b:
        m_b2 = re.search(
            r"CUTI\s+BERSAMA.*?(?=MENTERI\s+AGAMA|PLT\.|MENTERI\s+KETENAGAKERJAAN|$)",
            text_norm, re.DOTALL | re.I,
        )
        if m_b2:
            section_b = m_b2.group(0)

    month_pat = (
        r"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)"
    )
    KEYWORDS = [
        # Urutan: spesifik dulu (Nyepi/Saka, Imlek, Fitri) sebelum "Tahun Baru" generik
        "Nyepi", "Tahun Baru Saka", "Imlek", "Kongzili",
        "Idul Fitri", "Idulfitri", "1dul Fitri", "ldul Fitri",
        "Idul Adha", "Iduladha",
        "Isra Mikraj", "Wafat Yesus", "Wafat Isa", "Kebangkitan", "Paskah",
        "Hari Buruh", "Waisak", "Kenaikan Yesus", "Kenaikan Isa", "Pancasila",
        "Muharam", "Muharram", "Proklamasi", "Maulid", "Kelahiran Yesus", "Natal",
        "Tahun Baru",  # terakhir agar tidak menelan Nyepi/Imlek
    ]

    def rows_from_section(section: str, tipe: str) -> List[Dict]:
        if not section:
            return []
        # Jika teks sangat sedikit baris (flattened), pecah berdasarkan pola tanggal
        raw_lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
        if len(raw_lines) <= 3 and len(section) > 200:
            # Pecah pada boundary tanggal / nama hari
            chunks = re.split(
                rf"(?=(?:\d{{1,2}}\s+{month_pat})|(?:Senin|Selasa|Rabu|Kamis|Jumat|Jum'?at|Sabtu|Minggu)\s+\d)",
                section, flags=re.I,
            )
            raw_lines = [c.strip() for c in chunks if c and c.strip()]

        merged: List[str] = []
        buf = ""
        for ln in raw_lines:
            cand = f"{buf} {ln}".strip() if buf else ln
            has_month = bool(re.search(month_pat, cand, re.I))
            has_name = any(re.search(kw, cand, re.I) for kw in KEYWORDS)
            if len(ln) < 12 and not has_name:
                buf = cand
                continue
            if has_month and not has_name:
                buf = cand
                continue
            if has_name and not has_month and buf:
                merged.append(f"{buf} {ln}".strip())
                buf = ""
                continue
            if buf:
                merged.append(buf)
                buf = ""
            merged.append(ln)
        if buf:
            merged.append(buf)
        ms = re.compile(rf"^{month_pat}", re.I)
        merged2: List[str] = []
        for ln in merged:
            if merged2 and ms.search(ln):
                merged2[-1] += " " + ln
            else:
                merged2.append(ln)

        out = []
        for ln in merged2:
            if not re.search(month_pat, ln, re.I):
                continue
            name = None
            for kw in KEYWORDS:
                if re.search(kw, ln, re.I):
                    mkw = re.search(rf"({re.escape(kw)}.*)$", ln, re.I)
                    name = (mkw.group(1) if mkw else kw)
                    name = re.split(
                        r"\s+(?:April|Mei|Juni|Juli|Agustus|Senin|Selasa|Rabu|Kamis|Jumat|Jum/?at|Sabtu|Minggu)\b",
                        name, maxsplit=1, flags=re.I,
                    )[0].strip(" .|=-")
                    break
            if not name:
                mday = re.search(
                    r"(Senin|Selasa|Rabu|Kamis|Jumat|Jum'?at|Sabtu|Minggu)\s+(.+)$",
                    ln, re.I,
                )
                if mday:
                    name = mday.group(2).strip(" .|")
                else:
                    continue
            dates = _filter_valid_dates(parse_dates_from_chunk(ln, year), year)
            name = normalize_holiday_name(name)
            for iso in dates:
                out.append({
                    "date": iso,
                    "day": weekday_name(iso),
                    "name": name,
                    "type": tipe,
                })
        return out

    def dedupe(items: List[Dict]) -> List[Dict]:
        seen = set()
        out = []
        for it in sorted(items, key=lambda x: x["date"]):
            if it["date"] not in seen:
                seen.add(it["date"])
                out.append(it)
        return out

    # Pass 1: section A/B
    national = rows_from_section(section_a or "", "national_holiday")
    joint = rows_from_section(section_b or "", "joint_leave")

    # Pass 2: global scan seluruh teks (tangkap baris yang lolos section)
    global_rows = rows_from_section(text_norm, "national_holiday")
    known_dates = {r["date"] for r in national + joint}
    for r in global_rows:
        if r["date"] in known_dates:
            continue
        # heuristik: jika di dekat "CUTI BERSAMA" di teks → joint
        pos = text_norm.lower().find(r["name"][:20].lower()) if r.get("name") else -1
        cuti_pos = text_norm.lower().find("cuti bersama")
        if cuti_pos >= 0 and pos > cuti_pos:
            r = dict(r, type="joint_leave")
            joint.append(r)
        else:
            national.append(r)
        known_dates.add(r["date"])

    national = dedupe(national)
    joint = dedupe([x for x in joint if x["type"] == "joint_leave" or x["date"] not in {n["date"] for n in national}])
    joint = dedupe([{**x, "type": "joint_leave"} for x in joint])

    # --- Post-process: lengkapi pasangan Idul Fitri (hampir selalu 2 hari beruntun) ---
    national, joint = _ensure_idul_fitri_pair(national, joint, text_norm, year)

    # Rapikan nama + hari
    for lst in (national, joint):
        for h in lst:
            h["name"] = normalize_holiday_name(h.get("name") or "")
            if h.get("date"):
                h["day"] = weekday_name(h["date"])

    national = dedupe(national)
    joint = dedupe(joint)

    return {
        "year": year,
        "source": "Keputusan Bersama Menteri Agama, Ketenagakerjaan, dan PANRB",
        "scraped_at": iso_now(),
        "national_holidays": national,
        "joint_leave": joint,
        "total_national": len(national),
        "total_joint_leave": len(joint),
    }


# ============================================================
# PROSES TAHUN
# ============================================================
def register_year(index: Dict, data: Dict, source_pdf: str = "", source_sha: str = "") -> None:
    y = str(data["year"])
    index.setdefault("years", {})[y] = {
        "file": f"holidays-{y}.json",
        "total_national": data.get("total_national", 0),
        "total_joint_leave": data.get("total_joint_leave", 0),
        "source_pdf": source_pdf,
        "source_sha256": source_sha,
        "scraped_at": data.get("scraped_at") or iso_now(),
        "complete": is_complete(data),
    }


def process_year(year: int, index: Dict, force: bool) -> Optional[Dict]:
    global _ilovepdf_used
    print("-" * 50)
    print(f"  Tahun {year}")
    print("-" * 50)

    meta = (index.get("years") or {}).get(str(year)) or {}

    if not needs_scrape(index, year, force):
        log_result(year, "skip", "complete")
        return None

    # Ketersediaan di JDIH (cache) — skip OCR jika belum ada & bukan known URL
    has_known = bool(KNOWN_PDF_URLS.get(year))
    if not force and not has_known:
        jyears = jdih_years_from_page()
        if jyears and year not in jyears and try_direct_pdf(year) is None:
            index["next_check_after"] = (
                now_utc() + timedelta(days=RETRY_DAYS_NOT_FOUND)
            ).date().isoformat()
            log_result(year, "skip", "not_found_on_jdih")
            return None

    urls = resolve_pdf_urls(year)
    if not urls:
        index["next_check_after"] = (
            now_utc() + timedelta(days=RETRY_DAYS_NOT_FOUND)
        ).date().isoformat()
        log_result(year, "skip", "no_pdf_url")
        return None

    best: Optional[Dict] = None
    best_score = -1
    best_url = ""
    best_sha = ""
    credits_before = _ilovepdf_used

    for url in urls:
        # skip jika hash sama dengan index (sumber tidak berubah)
        try:
            pdf_path = download_pdf(url)
        except Exception as e:
            print(f"[!] Download gagal: {e}")
            continue
        try:
            sha = file_sha256(pdf_path)
            # Skip OCR jika PDF sumber sama (hash identik), meskipun belum
            # complete. Ini mencegah membakar kuota iLovePDF berulang kali
            # pada file yang tidak berubah. Gunakan --force untuk memaksa ulang.
            if not force and meta.get("source_sha256") == sha:
                print("[*] PDF tidak berubah (hash sama) → skip OCR")
                log_result(year, "skip", "same_hash")
                return None

            if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN and ILOVEPDF_PUBLIC_KEY:
                # masih bisa tesseract
                print("[*] Kuota iLovePDF run ini habis, pakai Tesseract bila ada")

            ocr_text = ocr_pdf_text(pdf_path, year)
            if not ocr_text.strip():
                print("[!] OCR kosong")
                continue
            cand = extract_from_ocr(ocr_text, year)
            # Isi tanggal tetap yang hilang (1 Jan, 1 Mei, 1 Jun, 17 Agust, 25 Des)
            cand = inject_fixed_holidays(cand)
            anchors = detect_anchors(cand)
            n_anc = sum(1 for v in anchors.values() if v)
            score = cand["total_national"] + cand["total_joint_leave"]
            # Bonus kecil jika anchor lengkap agar hasil ber-anchor menang
            score_rank = score + n_anc * 0.1
            print(
                f"    → nasional={cand['total_national']} cuti={cand['total_joint_leave']} "
                f"anchors={n_anc}/{len(ANCHOR_HOLIDAYS)}"
            )
            if score_rank > best_score:
                best_score = score_rank
                best_score = score
                best = cand
                best_url = url
                best_sha = sha
        finally:
            try:
                pdf_path.unlink()
            except Exception:
                pass

    credits = _ilovepdf_used - credits_before

    if not best:
        index["next_check_after"] = (
            now_utc() + timedelta(days=RETRY_DAYS_NOT_FOUND)
        ).date().isoformat()
        log_result(year, "fail", "ocr_empty", credits)
        return None

    # Jangan timpa data complete dengan hasil jelek
    if (
        meta.get("complete")
        and not force
        and int(best.get("total_national") or 0) < MIN_NATIONAL_REJECT
    ):
        print("[!] Hasil baru terlalu sedikit; pertahankan JSON complete yang ada")
        log_result(year, "skip", "reject_poor_overwrite", credits)
        return None

    out_name = f"holidays-{year}.json"
    Path(out_name).write_text(
        json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[✓] {out_name} complete={is_complete(best)} "
        f"n={best['total_national']} j={best['total_joint_leave']}"
    )
    github_put_json(
        out_name, best,
        f"Update holidays-{year}.json ({now_utc().strftime('%Y-%m-%d')})",
    )
    register_year(index, best, best_url, best_sha)

    if is_complete(best):
        index["next_check_after"] = default_next_check(year)
        log_result(year, "ocr", "ok_complete", credits)
    else:
        index["next_check_after"] = (
            now_utc() + timedelta(days=RETRY_DAYS_INCOMPLETE)
        ).date().isoformat()
        log_result(year, "ocr", "ok_incomplete", credits)

    return best


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Scraper libur nasional (optimized)")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--from", dest="from_year", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  Kalender Libur Nasional Indonesia")
    if args.year:
        mode = f"year={args.year}"
    elif args.from_year:
        mode = f"from={args.from_year}"
    else:
        mode = "auto"
    if args.force:
        mode += " force"
    print(f"  Mode: {mode}")
    print(f"  Season: {'yes' if in_skb_season() else 'no'} | max_ilovepdf/run={MAX_ILOVEPDF_DOCS_PER_RUN}")
    print("=" * 60)

    index = load_index()
    print(
        f"[*] Index latest={index.get('latest_year')} "
        f"next_check={index.get('next_check_after')} "
        f"years={list((index.get('years') or {}).keys())}"
    )

    targets = years_to_process(index, args.year, args.from_year, args.force)
    if not targets:
        print("[*] Tidak ada target. Selesai.")
        return

    print(f"[*] Target: {targets}")
    for y in targets:
        if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN and not args.force:
            # force masih boleh lanjut via tesseract
            if ILOVEPDF_PUBLIC_KEY:
                print(f"[*] Kuota iLovePDF run tercapai; sisa tahun memakai Tesseract saja")
        process_year(y, index, args.force)

    save_index(index)
    print(f"\nSelesai. iLovePDF docs used ≈ {_ilovepdf_used}")


if __name__ == "__main__":
    main()
