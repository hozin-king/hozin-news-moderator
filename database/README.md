# Database Berita — API Gratis

File JSON di direktori ini adalah **API berita gratis** dari sistem otonom
Hozin News. Diperbarui otomatis setiap run pipeline
(`autonomous/run_pipeline.py`), di-commit oleh GitHub Actions
(`.github/workflows/news-pipeline.yml`).

## Cara pakai

Ambil langsung via `raw.githubusercontent.com` (gratis, tanpa API key):

```
https://raw.githubusercontent.com/hozin-king/hozin-news-moderator/main/database/latest.json
```

File arsip per jam (format `news_YYYY-MM-DD-HH.json`, waktu UTC):

```
https://raw.githubusercontent.com/hozin-king/hozin-news-moderator/main/database/news_2026-10-11-21.json
```

Contoh pakai `curl` + `jq`:

```bash
curl -s https://raw.githubusercontent.com/hozin-king/hozin-news-moderator/main/database/latest.json \
  | jq '.[0] | {title, category, image_url}'
```

## Format data

Array of object, tiap artikel:

| Field | Keterangan |
|---|---|
| `id` | ID unik (hash 12 hex dari source_url) |
| `title` | Judul Bahasa Indonesia |
| `summary` | Ringkasan 1-2 kalimat |
| `content` | Konten full Bahasa Indonesia |
| `image_url` | URL gambar konten asli (bukan logo/icon) |
| `source_name` | Nama sumber asli |
| `source_url` | URL artikel sumber |
| `category` | Kategori (`hp`, `chipset`, `ai`, `teknologi`, `gpu`, `cpu`, `benchmark`, `game`, `internet`) |
| `published_at` | Waktu publish sumber (epoch / ISO string) |
| `original_lang` | Bahasa asli sebelum diterjemahkan (`en`, `id`, …) |
| `scraped_at` | Waktu pipeline memproses (ISO 8601 UTC) |

## Catatan

- `latest.json` selalu di-overwrite tiap run → URL stabil, cocok untuk polling.
- File `news_YYYY-MM-DD-HH.json` adalah arsip — tidak pernah diubah setelah ditulis.
- Semua konten sudah Bahasa Indonesia (auto-translate bila sumber berbahasa asing).
- Atribusi: mohon cantumkan sumber asli (`source_name` + `source_url`) bila memakai ulang.
