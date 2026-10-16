#!/usr/bin/env python3
"""merge_getter.py — Tarik artikel mentah dari hozin-news-getter ke database.

Jalan SEBELUM pipeline utama di news-pipeline.yml:
  1. Download articles.jsonl dari repo hozin-news-getter (raw.githubusercontent)
  2. Filter pakai moderate.check_item (tolak porn/judol/phishing)
  3. Dedup lawan database/latest.json via source_url
  4. Prepend yang baru (terbaru di atas), cap 1000 — sesuai konvensi repo ini

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

GETTER_URL = ("https://raw.githubusercontent.com/hozin-king/"
              "hozin-news-getter/main/data/articles.jsonl")
LATEST = os.path.join(BASE, "database", "latest.json")
UA = {"User-Agent": "Mozilla/5.0 HozinNewsModerator/1.0"}
MAX_ARTICLES = 1000


def fetch_lines(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace").splitlines()


def to_moderator_format(g):
    """Convert getter article → format database/latest.json."""
    link = g.get("link", "")
    return {
        "id": hashlib.sha256(link.encode()).hexdigest()[:16],
        "title": g.get("title", ""),
        "summary": g.get("description", "")[:500],
        "content": "",
        "category": "rss",
        "image_url": "",
        "original_lang": "id",
        "published_at": g.get("pubDate", ""),
        "scraped_at": g.get("fetched_at", ""),
        "source_name": g.get("source", ""),
        "source_url": link,
    }


def main():
    try:
        lines = fetch_lines(GETTER_URL)
    except Exception as e:
        print(f"[merge_getter] gagal download: {e} — skip, pipeline lanjut")
        return 0

    with open(LATEST, encoding="utf-8") as f:
        latest = json.load(f)
    existing_urls = {a.get("source_url") for a in latest}

    added, rejected = 0, 0
    new_items = []
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
        # Moderasi: tolak porn/judol/phishing
        cat, _ = check_item(f"{g.get('title', '')} {g.get('description', '')}")
        if cat:
            rejected += 1
            continue
        new_items.append(to_moderator_format(g))
        existing_urls.add(g["link"])
        added += 1

    if new_items:
        latest = new_items + latest
        latest = latest[:MAX_ARTICLES]
        with open(LATEST, "w", encoding="utf-8") as f:
            json.dump(latest, f, ensure_ascii=False, indent=1)

    print(f"[merge_getter] {len(lines)} baris getter → {added} baru, "
          f"{rejected} ditolak moderasi, total latest.json: {len(latest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
