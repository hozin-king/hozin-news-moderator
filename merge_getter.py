#!/usr/bin/env python3
"""merge_getter.py — Tarik artikel mentah dari hozin-news-getter ke database.

Jalan SEBELUM pipeline utama di news-pipeline.yml:
  1. Download articles.jsonl dari repo hozin-news-getter (raw.githubusercontent)
  2. Download juga data hasil upgrade getter (22 Okt 2026):
       - data/latest.json  → daftar kategori hasil klasifikasi per link
       - data/full.json    → isi artikel FULL per link (content_status=full)
       - categories.json   → label tampilan per slug kategori
  3. Filter pakai moderate.check_item (tolak porn/judol/phishing/kasar) —
     teks yang dicek kini termasuk isi full bila ada.
  4. Dedup lawan database/latest.json via source_url
  5. Prepend yang baru (terbaru di atas), cap 1000 — sesuai konvensi repo ini
  6. Backfill: item lama di latest.json yang URL-nya dikenal getter dan
     isinya masih kosong / kategorinya masih "rss" diisi dari data getter
     (kategori + isi full), supaya arsip ikut naik kelas tanpa menunggu
     artikel baru.

Getter update tiap 10 menit, pipeline moderator tiap jam → latest.json
selalu keisi artikel RSS segar yang udah dimoderasi.
"""
import hashlib
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from moderate import check_item

GETTER_BASE = "https://raw.githubusercontent.com/hozin-king/hozin-news-getter/main"
GETTER_URL = GETTER_BASE + "/data/articles.jsonl"
GETTER_LATEST_URL = GETTER_BASE + "/data/latest.json"
GETTER_FULL_URL = GETTER_BASE + "/data/full.json"
GETTER_CATEGORIES_URL = GETTER_BASE + "/categories.json"
LATEST = os.path.join(BASE, "database", "latest.json")
UA = {"User-Agent": "Mozilla/5.0 HozinNewsModerator/1.0"}
MAX_ARTICLES = 1000


def fetch_lines(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace").splitlines()


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def load_getter_enrichment():
    """Bangun peta link → {categories, content} + peta slug → label.

    Sumber: data/latest.json getter (kategori per artikel) dan
    data/full.json getter (isi full per artikel, hanya content_status=full).
    Kegagalan download TIDAK menghentikan merge — peta kosong berarti
    perilaku lama (tanpa kategori/isi) untuk run ini.
    """
    by_link = {}
    labels = {}
    try:
        cats = fetch_json(GETTER_CATEGORIES_URL)
        for slug, spec in (cats.get("categories") or {}).items():
            if isinstance(spec, dict) and spec.get("label"):
                labels[slug] = spec["label"]
    except Exception as e:
        print(f"[merge_getter] categories.json getter gagal: {e} — label pakai slug")
    for url in (GETTER_LATEST_URL, GETTER_FULL_URL):
        try:
            data = fetch_json(url)
        except Exception as e:
            print(f"[merge_getter] {url.rsplit('/', 1)[-1]} getter gagal: {e} — lanjut tanpa file itu")
            continue
        for a in data.get("articles") or []:
            link = a.get("link")
            if not link:
                continue
            entry = by_link.setdefault(link, {"categories": [], "content": ""})
            for slug in a.get("categories") or []:
                if slug not in entry["categories"]:
                    entry["categories"].append(slug)
            if (a.get("content_status") == "full"
                    and a.get("content")
                    and len(a["content"]) > len(entry["content"])):
                entry["content"] = a["content"]
            # Video tertanam dari getter (aditif) — diteruskan apa adanya.
            if a.get("video_url"):
                entry["video_type"] = a.get("video_type", "")
                entry["video_id"] = a.get("video_id", "")
                entry["video_url"] = a["video_url"]
            # Artikel terjemahan getter: judul + ringkasan Bahasa
            # Indonesia-nya ikut dibawa (judul asli tetap di getter
            # sebagai original_title).
            if a.get("translated"):
                entry["translated"] = True
                entry["source_lang"] = a.get("source_lang", "en")
                if a.get("title"):
                    entry["title"] = a["title"]
                if a.get("description"):
                    entry["summary"] = a["description"]
    return by_link, labels


def to_moderator_format(g, enrich=None, labels=None):
    """Convert getter article → format database/latest.json."""
    link = g.get("link", "")
    enrich = enrich or {}
    labels = labels or {}
    categories = enrich.get("categories") or []
    # Artikel terjemahan: pakai judul + ringkasan Bahasa Indonesia dari
    # getter; artikel biasa tetap dari baris articles.jsonl mentah.
    translated = bool(enrich.get("translated"))
    title = enrich.get("title") if translated and enrich.get("title") \
        else g.get("title", "")
    summary = enrich.get("summary") if translated and enrich.get("summary") \
        else g.get("description", "")
    item = {
        "id": hashlib.sha256(link.encode()).hexdigest()[:16],
        "title": title,
        "summary": summary[:500],
        "content": enrich.get("content", ""),
        # Kategori utama = kategori pertama hasil klasifikasi getter;
        # "rss" dipertahankan untuk artikel tanpa klasifikasi.
        "category": categories[0] if categories else "rss",
        "categories": categories,
        "category_labels": [labels.get(s, s) for s in categories],
        "image_url": "",
        "original_lang": enrich.get("source_lang", "en") if translated
        else "id",
        "published_at": g.get("pubDate", ""),
        "scraped_at": g.get("fetched_at", ""),
        "source_name": g.get("source", ""),
        "source_url": link,
    }
    # Video tertanam (aditif, hanya bila getter menemukannya).
    if enrich.get("video_url"):
        item["video_type"] = enrich.get("video_type", "")
        item["video_id"] = enrich.get("video_id", "")
        item["video_url"] = enrich["video_url"]
    return item


def backfill_existing(latest, by_link, labels):
    """Isi kategori + isi full untuk item lama yang masih kosong.

    Hanya menyentuh item yang source_url-nya ada di data getter, dan
    hanya mengisi field yang masih kosong / masih default — tidak pernah
    menimpa isi atau kategori yang sudah terisi dari sumber lain.
    """
    changed = 0
    for a in latest:
        info = by_link.get(a.get("source_url"))
        if not info:
            continue
        touched = False
        if info.get("categories") and not a.get("categories"):
            a["categories"] = info["categories"]
            a["category_labels"] = [labels.get(s, s) for s in info["categories"]]
            touched = True
        if info.get("categories") and a.get("category") in ("", "rss", None):
            a["category"] = info["categories"][0]
            touched = True
        if info.get("content") and not a.get("content"):
            a["content"] = info["content"]
            touched = True
        if info.get("video_url") and not a.get("video_url"):
            a["video_type"] = info.get("video_type", "")
            a["video_id"] = info.get("video_id", "")
            a["video_url"] = info["video_url"]
            touched = True
        if touched:
            changed += 1
    return changed


def main():
    try:
        lines = fetch_lines(GETTER_URL)
    except Exception as e:
        print(f"[merge_getter] gagal download: {e} — skip, pipeline lanjut")
        return 0

    by_link, labels = load_getter_enrichment()
    print(f"[merge_getter] enrichment getter: {len(by_link)} link berkategori/berisi, "
          f"{len(labels)} label kategori")

    with open(LATEST, encoding="utf-8") as f:
        latest = json.load(f)
    existing_urls = {a.get("source_url") for a in latest}

    added, rejected = 0, 0
    enriched_items, plain_items = [], []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            g = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not g.get("link") or g["link"] in existing_urls:
            continue
        info = by_link.get(g["link"]) or {}
        # Moderasi: tolak porn/judol/phishing/kasar — isi full ikut dicek.
        cat, _ = check_item(
            f"{g.get('title', '')} {g.get('description', '')} {info.get('content', '')}")
        if cat:
            rejected += 1
            continue
        item = to_moderator_format(g, info, labels)
        # Artikel hasil enrichment getter (berkategori / berisi full)
        # diprioritaskan: articles.jsonl terurut per sumber dan artikel
        # enrich berada di ekor file — tanpa prioritas ini mereka selalu
        # terpotong cap MAX_ARTICLES dan tidak pernah tampil.
        if info.get("categories") or info.get("content"):
            enriched_items.append(item)
        else:
            plain_items.append(item)
        existing_urls.add(g["link"])
        added += 1
    new_items = enriched_items + plain_items

    if new_items:
        latest = new_items + latest
        latest = latest[:MAX_ARTICLES]

    filled = backfill_existing(latest, by_link, labels)

    if new_items or filled:
        with open(LATEST, "w", encoding="utf-8") as f:
            json.dump(latest, f, ensure_ascii=False, indent=1)

    print(f"[merge_getter] {len(lines)} baris getter → {added} baru, "
          f"{rejected} ditolak moderasi, {filled} item lama ter-backfill, "
          f"total latest.json: {len(latest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
