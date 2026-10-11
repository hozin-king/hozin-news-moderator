# Autonomous News Pipeline — Hozin News Moderator

Sistem berita otonom: pilih berita terbaik → produksi artikel full Bahasa Indonesia → publish → belajar dari performa.

Arsitektur mirror persis dari sistem video otonom (`~/workspace/yt-automations/autonomous/`).

## Komponen

| File | Peran | Padanan video |
|---|---|---|
| `news_selector.py` | Pilih 1 berita terbaik dari API publik | `auto_topic_selector.py` |
| `news_producer.py` | Fetch konten full → auto-translate ID → struktur jurnalistik → quality gate | `video_producer.py` |
| `news_feedback.py` | Evaluasi views → sesuaikan bobot kategori/keyword | `feedback_loop.py` |
| `thought_log.py` | Jejak pemikiran tiap keputusan (copy persis) | `thought_log.py` |
| `run_pipeline.py` | Orkestrator end-to-end | `run_pipeline.py` |
| `weights.json` | Bobot scoring (auto-update oleh feedback) | `weights.json` |

## Cara pakai

```bash
# Simulasi penuh (tanpa publish)
python3 run_pipeline.py --dry-run

# Produksi beneran (butuh AISTUDIO_CRON_SECRET)
export AISTUDIO_CRON_SECRET="..."
python3 run_pipeline.py

# Tiap tahap manual
python3 news_selector.py --out state/berita.json
python3 news_producer.py --berita state/berita.json --out out/artikel
python3 news_feedback.py
```

## Anti AI slop

- `filter_slop()`: tolak clickbait, judul ALL CAPS, konten < 150 kata
- `quality_gate()`: tolak artikel tanpa gambar konten valid, terjemahan rusak
- Gambar: `og:image` → `twitter:image` → img `<article>`; tolak logo/icon/tombol/sosmed; cek dimensi ≥ 300px

## Auto-translate

Producer otomatis deteksi bahasa (via `langdetect` bila ada, fallback heuristik kata fungsi Indonesia). Bukan Bahasa Indonesia → terjemahkan judul + konten via endpoint gratis Google (tanpa API key). `article.json` menyimpan `original_lang` + `original_title` sebagai referensi.

## Thought log

Tiap run menulis `state/thought_YYYY-MM-DD.md`: bahasa asli terdeteksi apa, diterjemahkan atau tidak, kenapa berita dipilih/ditolak, kenapa bobot berubah.

## Catatan

- Secret HANYA via env var `AISTUDIO_CRON_SECRET` — tidak ada hardcode.
- `state/` di-gitignore (tracking internal, thought log, produced.json).
