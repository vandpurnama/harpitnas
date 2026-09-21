# 📅 Kalender Libur Nasional & Cuti Bersama Indonesia (Auto-Scraper)

Scraper otomatis yang mengambil **Surat Keputusan Bersama (SKB) 3 Menteri** tentang hari libur nasional & cuti bersama dari situs JDIH Kementerian PANRB, lalu mengubahnya menjadi data terstruktur (`holidays-{tahun}.json`) yang siap dipakai aplikasi lain (misalnya kalender interaktif).

Dijalankan otomatis via **GitHub Actions** — tidak butuh server yang nyala 24 jam.

---

## ✨ Fitur

- Ambil PDF SKB 3 Menteri secara otomatis (URL langsung jika sudah dikenal, atau scraping situs JDIH via Selenium/headless Chromium jika belum)
- Ekstraksi teks: **iLovePDF API** (opsional, lebih akurat) dengan fallback otomatis ke **Tesseract OCR** lokal (Bahasa Indonesia)
- Deteksi *anchor* hari libur dengan tanggal tidak tetap (Imlek, Idul Fitri, Idul Adha, Wafat & Kenaikan Yesus) + injeksi otomatis tanggal libur tetap (1 Jan, 1 Mei, 1 Jun, 17 Agu, 25 Des)
- Anti pemborosan kuota: skip otomatis jika PDF sumber belum berubah (cek hash), atau jika belum masuk musim rilis SKB (Sep–Jan)
- Hasil di-*commit* otomatis kembali ke repo GitHub via API — tidak perlu database eksternal

## 📦 Format Output

**`index.json`** — status ringkas semua tahun yang sudah diproses:

```json
{
  "years": {
    "2026": {
      "file": "holidays-2026.json",
      "total_national": 17,
      "total_joint_leave": 8,
      "source_pdf": "https://data-jdih.menpan.go.id/dokumen/...",
      "complete": true
    }
  },
  "latest_year": 2027,
  "next_check_after": "2027-09-01"
}
```

**`holidays-{tahun}.json`** — detail hari libur per tahun:

```json
{
  "year": 2026,
  "source": "Keputusan Bersama Menteri Agama, Ketenagakerjaan, dan PANRB",
  "national_holidays": [
    { "date": "2026-01-01", "day": "Kamis", "name": "Tahun Baru 2026 Masehi", "type": "national_holiday" }
  ],
  "joint_leave": [ ... ],
  "total_national": 17,
  "total_joint_leave": 8
}
```

## 🚀 Cara Pakai — GitHub Actions (direkomendasikan)

1. **Push repo ini ke GitHub** apa adanya (`main.py`, `Dockerfile`, `requirements.txt`, dst di root repo)
2. Buat folder `.github/workflows/` lalu taruh file workflow (`scrape-kalender.yml`) — jadwal cron harian, plus tombol *Run workflow* manual
3. *(Opsional)* Kalau mau pakai iLovePDF: **Settings → Secrets and variables → Actions**, tambahkan `ILOVEPDF_PUBLIC_KEY` dan `ILOVEPDF_SECRET_KEY`. Kalau dikosongkan, otomatis fallback ke Tesseract — tidak error.
4. Pastikan **Settings → Actions → General → Workflow permissions** diset **"Read and write permissions"** (atau cukup andalkan `permissions: contents: write` di file workflow)
5. Commit & push, lalu coba jalankan manual dulu lewat tab **Actions** sebelum mengandalkan jadwal otomatis
6. Hasil (`holidays-*.json`, `index.json`) akan muncul otomatis di folder `kalender/` tiap kali ada pembaruan

Chromium, ChromeDriver, dan Tesseract (+ paket bahasa Indonesia) sudah otomatis terpasang lewat `Dockerfile` — tidak perlu setting tambahan apa pun di runner.

## 🖥️ Cara Pakai — Manual / Lokal

```bash
pip install -r requirements.txt
python main.py                    # mode auto (ikut jadwal next_check_after)
python main.py --year 2027 --force   # paksa scrape tahun tertentu
python main.py --from 2025           # proses dari tahun 2025 sampai sekarang
```

## 🐳 Cara Pakai — Docker / Railway

Repo ini juga tetap kompatibel dengan Docker biasa maupun Railway (`railway.toml` sudah disertakan) bila suatu saat dibutuhkan hosting yang selalu nyala.

```bash
docker build -t kalender-scraper .
docker run --rm -e GITHUB_TOKEN=... -e GITHUB_REPO=owner/repo kalender-scraper
```

## ⚙️ Environment Variables

| Variabel | Wajib? | Keterangan |
|---|---|---|
| `GITHUB_TOKEN` | ✅ (untuk push hasil) | Token dengan akses tulis ke repo. Di GitHub Actions cukup pakai `secrets.GITHUB_TOKEN` bawaan |
| `GITHUB_REPO` | ✅ | Format `owner/repo` tujuan hasil di-push |
| `GITHUB_BRANCH` | opsional | Default `main` |
| `GITHUB_FOLDER` | opsional | Default `kalender` — subfolder tempat file JSON disimpan |
| `ILOVEPDF_PUBLIC_KEY` / `ILOVEPDF_SECRET_KEY` | opsional | Kalau kosong, otomatis pakai Tesseract OCR lokal |
| `MAX_ILOVEPDF_DOCS_PER_RUN` | opsional | Default `6` |
| `MIN_ANCHORS_OK` | opsional | Default `4` |

## 📁 Struktur Repo

```
.
├── main.py              # Script utama scraper
├── Dockerfile            # Image berisi Chromium + Tesseract (ind+eng) + Poppler
├── requirements.txt
├── railway.toml           # (opsional) deploy ke Railway
├── index.json             # Auto-generated, jangan diedit manual
├── holidays-{tahun}.json  # Auto-generated, jangan diedit manual
└── .github/workflows/
    └── scrape-kalender.yml
```

## 🗓️ Algoritma Anchor & Tanggal Tetap

**Anchor** (tanggal tidak tetap — wajib dari hasil Extract/OCR): Imlek, Idul Fitri, Idul Adha, Kenaikan Yesus, Wafat Yesus. Minimal 4 dari 5 anchor ketemu → hasil dianggap memadai.

**Tanggal tetap** (di-injeksi otomatis jika hilang): 1 Januari, 1 Mei, 1 Juni, 17 Agustus, 25 Desember.

## ⚠️ Catatan

- Sumber data: situs resmi [JDIH Kementerian PANRB](https://jdih.menpan.go.id). Script ini tidak berafiliasi dengan pemerintah — gunakan hasilnya sebagai referensi, bukan sumber hukum resmi.
- Karena menggunakan ocr ada kemungkinan bahwa akan ada tanggal atau kata yang masih kurang tepat dan salah silahkan validasi ulang untuk penggunaan komersial. 
- File `index.json` dan `holidays-*.json` di-*generate* otomatis oleh workflow — hindari edit manual karena akan tertimpa di run berikutnya.

