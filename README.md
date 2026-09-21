# Kalender Libur Nasional Indonesia

Scraper SKB 3 Menteri → `holidays-{tahun}.json` + `index.json` (hemat iLovePDF).

## Algoritma anchor + tanggal tetap

### Anchor (tanggal **tidak tetap** — wajib dari Extract/OCR)
| Key | Pola nama |
|---|---|
| Imlek | imlek / kongzili |
| Idul Fitri | idul fitri |
| Idul Adha | idul adha |
| Kenaikan Yesus | kenaikan yesus/isa |
| Wafat Yesus | wafat yesus/isa |

Minimal **4 dari 5** anchor ketemu → Extract dianggap memadai (skip OCR).

### Tanggal tetap (bisa di-inject otomatis jika hilang)
- 1 Januari — Tahun Baru Masehi
- 1 Mei — Hari Buruh Internasional
- 1 Juni — Hari Lahir Pancasila
- 17 Agustus — Proklamasi Kemerdekaan
- 25 Desember — Kelahiran Yesus Kristus

## Alur iLovePDF

```
Extract (10 kredit/file)
    ↓ parse
    ↓ deteksi anchor
≥4 anchor + cuti memadai  → skip OCR
anchor kurang             → OCR + Extract → pilih skor terbaik
    ↓
inject tanggal tetap yang hilang
    ↓
simpan JSON + index
```

## Environment

```env
GITHUB_TOKEN=...
GITHUB_REPO=owner/repo
GITHUB_FOLDER=kalender
ILOVEPDF_PUBLIC_KEY=...
ILOVEPDF_SECRET_KEY=...

MAX_ILOVEPDF_DOCS_PER_RUN=6
MIN_ANCHORS_OK=4
MIN_NATIONAL_SKIP_OCR=12
MIN_JOINT_SKIP_OCR=4
```

## Perintah

```bash
python main.py
python main.py --year 2027 --force
```
