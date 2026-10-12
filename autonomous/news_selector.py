#!/usr/bin/env python3
"""
news_selector.py — Pemilihan berita terbaik untuk diproduksi ulang.

Membaca kandidat dari API berita publik (/api/news/public?limit=N),
lalu memilih SATU berita terbaik berdasarkan skor:
freshness (berita baru > lama) + bobot kategori + kelengkapan
(ada gambar asli + konten full) + anti-duplikat.

ANTI AI SLOP: filter_slop() menolak:
- judul clickbait (ALL CAPS, kata WOW/HEBOH/VIRAL, seru berlebih)
- konten < 60 kata (dilonggarkan 12 Okt 2026 — sebelumnya 150, terlalu
  agresif sampai pipeline hampir tidak pernah menghasilkan artikel)
- judul mirip >80% dengan yang sudah diproduksi

Contoh:
    python3 news_selector.py --out berita.json
    python3 news_selector.py --out berita.json --top 5 --dry-run
"""

import argparse
import difflib
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime

from thought_log import think, section

# Reuse ekstraktor isi artikel milik producer — bila API hanya memberi
# judul+ringkasan (content kosong), selector mencoba mengambil isi asli
# dari source_url sebelum menolak kandidat. Best-effort: gagal fetch =
# kandidat tetap ditolak seperti biasa.
try:
    from news_producer import extract_article
except Exception:  # pragma: no cover - producer tidak tersedia
    extract_article = None

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)  # root repo hozin-news-moderator
WEIGHTS_FILE = os.path.join(BASE, "weights.json")
STATE_DIR = os.path.join(BASE, "state")
PRODUCED_FILE = os.path.join(STATE_DIR, "produced.json")

PUBLIC_API = "https://aistudio.hozinking.my.id/api/news/public"
MIN_WORDS = 60

# Kata clickbait (sama seperti scrape_news_v2.py).
CLICKBAIT_WORDS = [
    "wow", "heboh", "viral", "gempar", "gila", "ngakak", "bikin melongo",
    "auto kaya", "dijamin", "100% work", "tanpa modal", "cuan",
]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def word_count(text):
    return len(re.findall(r"\S+", text or ""))


def fetch_candidates(limit=50):
    """Ambil berita terbaru dari API publik."""
    url = f"{PUBLIC_API}?limit={limit}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) hozin-news-autonomous/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"Gagal ambil kandidat dari API: {e}", file=sys.stderr)
        sys.exit(1)
    return data.get("news", [])


def produced_keys():
    """(source_url, title) yang sudah diproduksi sistem ini.

    Cek 3 lapis biar tidak duplikat:
    1. state/produced.json (lokal, antar run di mesin yang sama)
    2. database/latest.json + database/news_*.json (repo, persisten antar run GitHub Actions)
    3. API publik (fallback bila file lokal tidak ada)
    """
    urls, titles = set(), []

    def _collect(items):
        for p in items:
            u = (p.get("source_url") or "").strip()
            t = (p.get("title") or "").strip()
            if u:
                urls.add(u)
                # juga tanpa query param (kadang URL beda param tapi artikel sama)
                urls.add(u.split("?")[0])
            if t:
                titles.append(t)

    # Lapis 1: state lokal
    _collect(load_json(PRODUCED_FILE, []))

    # Lapis 2: database JSON di repo (penting untuk GitHub Actions — fresh env tiap run)
    db_dir = os.path.join(REPO, "database")
    if os.path.isdir(db_dir):
        for fn in os.listdir(db_dir):
            if fn == "latest.json" or (fn.startswith("news_") and fn.endswith(".json")):
                try:
                    data = load_json(os.path.join(db_dir, fn), [])
                    _collect(data if isinstance(data, list) else [data])
                except Exception:
                    pass

    # Lapis 3: API publik (fallback)
    if not urls:
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://aistudio.hozinking.my.id/api/news/public?limit=100",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
                _collect(data.get("news", []))
        except Exception:
            pass

    return urls, titles


def title_similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# Batas fetch isi artikel per run — cukup sampai ketemu kandidat layak,
# jangan hajar semua source bila API-nya memang lagi kosong semua.
# (12 Okt 2026: 8 → 12, karena fetch lama sering gagal dan butuh lebih banyak percobaan)
MAX_SOURCE_FETCH = 12


def enrich_from_source(item, cache, budget):
    """Coba ambil isi artikel dari source_url bila content API tipis.

    Return (ok: bool, catatan: str). Hasil di-cache per URL dalam satu run.
    budget = [sisa] (list 1-elemen agar bisa dikurangi dari pemanggil).
    """
    if extract_article is None:
        return False, "ekstraktor producer tidak tersedia"
    url = (item.get("source_url") or "").strip()
    if not url:
        return False, "tanpa source_url"
    if url in cache:
        body, img = cache[url]
    else:
        if budget[0] <= 0:
            return False, "budget fetch habis"
        budget[0] -= 1
        think("selector", f"content tipis — coba ambil isi dari {url[:70]}…", "data")
        try:
            body, img = extract_article(url)
        except Exception as e:
            body, img = "", None
            think("selector", f"fetch gagal: {e}", "tolak")
        body = body or ""
        cache[url] = (body, img)
    if word_count(body) < MIN_WORDS:
        return False, f"isi source juga tipis ({word_count(body)} kata)"
    item["content"] = body
    if img and not item.get("image_url"):
        item["image_url"] = img
    return True, f"isi diambil dari source ({word_count(body)} kata)"


def is_clickbait(title):
    """Return (True, alasan) bila judul clickbait. Mirror scrape_news_v2.py."""
    t = (title or "").strip()
    low = t.lower()
    letters = [c for c in t if c.isalpha()]
    if len(letters) > 15 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:
        return True, "judul ALL CAPS"
    for w in CLICKBAIT_WORDS:
        if w in low:
            return True, f"kata clickbait '{w}'"
    if t.count("!") >= 2 or t.count("?") >= 2:
        return True, "tanda seru/tanya berlebih"
    if re.search(r"\b\d+\s+(fakta|alasan|cara|trik|rahasia)\b.*\b(bikin|buat)\b", low):
        return True, "pola listicle clickbait"
    return False, ""


def filter_slop(item):
    """Tolak berita berkualitas rendah. Return (lolos: bool, alasan: str)."""
    title = item.get("title") or ""
    content = item.get("content") or ""

    # 1. Clickbait.
    bad, reason = is_clickbait(title)
    if bad:
        return False, f"clickbait ({reason})"

    # 2. Konten tipis <60 kata — anti AI slop (ambang longgar).
    if word_count(content) < MIN_WORDS:
        return False, f"konten tipis ({word_count(content)} kata)"

    # 3. Judul terlalu pendek = tidak informatif.
    if len(title.split()) < 4:
        return False, "judul terlalu pendek"

    return True, "lolos"


def freshness_factor(published_at, half_life_hours):
    """Berita lama nilainya meluruh (paruh waktu dalam jam)."""
    if not published_at:
        return 0.5
    try:
        if isinstance(published_at, (int, float)):
            dt = datetime.fromtimestamp(published_at)
        else:
            dt = datetime.fromisoformat(str(published_at)[:19])
    except (ValueError, TypeError):
        return 0.5
    age_h = max(0, (datetime.now() - dt).total_seconds() / 3600)
    return 0.5 ** (age_h / max(1, half_life_hours))


def score_candidate(item, weights, produced_urls, produced_titles):
    """Hitung skor + kumpulkan alasan. Return (skor, alasan_list)."""
    url = (item.get("source_url") or "").strip()
    title = (item.get("title") or "").strip()

    # Anti-duplikat: URL sama atau judul mirip >80%.
    if url and url in produced_urls:
        return 0.0, ["sudah pernah diproduksi (URL sama) — dilewati"]
    for t in produced_titles:
        if title_similar(title, t) > 0.8:
            return 0.0, [f"sudah pernah diproduksi (judul mirip {title_similar(title, t):.0%}) — dilewati"]

    lolos, alasan_slop = filter_slop(item)
    if not lolos:
        return 0.0, [f"ditolak filter anti-slop: {alasan_slop}"]
    alasan = [f"lolos anti-slop: {alasan_slop}"]

    cat = (item.get("category") or "").lower()
    cat_w = weights.get("category", {}).get(cat, weights.get("category_default", 1.0))
    skor = cat_w
    alasan.append(f"kategori '{cat or '?'}' bobot {cat_w}")

    # Kelengkapan: gambar asli + konten full.
    if item.get("image_url"):
        skor += weights.get("completeness_image_bonus", 0.3)
        alasan.append("punya gambar asli (+bonus)")
    else:
        alasan.append("tanpa gambar (tanpa bonus)")
    if word_count(item.get("content")) >= 300:
        skor += weights.get("completeness_text_bonus", 0.2)
        alasan.append(f"konten full {word_count(item.get('content'))} kata (+bonus)")

    # Keyword boost dari feedback loop.
    teks = f"{title} {item.get('summary','')}".lower()
    for kw, b in weights.get("keyword_boost", {}).items():
        if kw.lower() in teks:
            skor *= b
            alasan.append(f"keyword '{kw}' x{b}")

    # Freshness (paruh waktu dalam jam — berita basi cepat).
    f = freshness_factor(item.get("published_at"), weights.get("freshness_half_life_hours", 12))
    skor *= (0.5 + 0.5 * f)
    alasan.append(f"freshness {f:.2f}")

    return skor, alasan


def main():
    ap = argparse.ArgumentParser(
        description="Pilih 1 berita terbaik untuk diproduksi ulang.")
    ap.add_argument("--out", default="", help="Path output JSON berita terpilih")
    ap.add_argument("--top", type=int, default=5, help="Tampilkan N kandidat teratas")
    ap.add_argument("--dry-run", action="store_true",
                    help="Hanya tampilkan ranking, tidak tulis output")
    ap.add_argument("--limit", type=int, default=50,
                    help="Jumlah kandidat diambil dari API")
    ap.add_argument("--min-score", type=float, default=None,
                    help="Ambang skor minimum (default dari weights.json)")
    args = ap.parse_args()

    weights = load_json(WEIGHTS_FILE, {})
    if not weights:
        print("weights.json tidak ditemukan/kosong", file=sys.stderr)
        sys.exit(2)

    cands = fetch_candidates(args.limit)
    if not cands:
        print("Tidak ada kandidat dari API berita.", file=sys.stderr)
        sys.exit(1)

    produced_urls, produced_titles = produced_keys()
    min_score = args.min_score if args.min_score is not None else weights.get("min_score", 1.0)

    ranked = []
    section("Pemilihan Berita")
    think("selector", f"Mulai menilai {len(cands)} kandidat (ambang skor: {min_score})", "data")
    fetch_cache = {}
    fetch_budget = [MAX_SOURCE_FETCH]
    for c in cands:
        skor, alasan = score_candidate(c, weights, produced_urls, produced_titles)
        # Kandidat yang gugur HANYA karena konten tipis + punya source_url:
        # coba ambil isi asli sebelum benar-benar ditolak.
        if skor == 0.0 and any("konten tipis" in a for a in alasan):
            ok, note = enrich_from_source(c, fetch_cache, fetch_budget)
            think("selector", f"fallback source: {note}", "keputusan" if ok else "tolak")
            if ok:
                skor, alasan = score_candidate(c, weights, produced_urls, produced_titles)
                alasan.append(f"[fallback] {note}")
        ranked.append((skor, c, alasan))
    ranked.sort(key=lambda x: x[0], reverse=True)

    print(f"Kandidat: {len(cands)} | ambang skor: {min_score}\n")
    for i, (skor, c, alasan) in enumerate(ranked[:args.top], 1):
        status = "TERPILIH" if i == 1 and skor >= min_score else ("di bawah ambang" if skor < min_score else "")
        lvl = "keputusan" if i == 1 and skor >= min_score else ("tolak" if skor < min_score else "info")
        think("selector", f"#{i} skor {skor:.2f} — {(c.get('title') or '')[:60]} {'→ ' + status if status else ''}", lvl)
        for a in alasan:
            think("selector", f"  └ {a}")
        print(f"#{i} [{skor:.2f}] {status}")
        print(f"    judul: {(c.get('title') or '')[:90]}")
        for a in alasan:
            print(f"    - {a}")
        print()

    best_skor, best, best_alasan = ranked[0]
    if best_skor < min_score:
        print(f"Tidak ada kandidat di atas ambang ({min_score}). Batal.", file=sys.stderr)
        think("selector", "Tidak ada kandidat layak — pipeline dibatalkan", "peringatan")
        sys.exit(3)

    result = {
        "news_id": best.get("id"),
        "title": best.get("title"),
        "summary": best.get("summary"),
        "content": best.get("content"),
        "image_url": best.get("image_url"),
        "source_name": best.get("source_name"),
        "source_url": best.get("source_url"),
        "category": best.get("category"),
        "published_at": best.get("published_at"),
        "skor": round(best_skor, 3),
        "alasan": best_alasan,
        "dipilih_pada": date.today().isoformat(),
        "weights_version": weights.get("version", 1),
    }

    if args.dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])
        return

    out = args.out or os.path.join(STATE_DIR, "berita_terpilih.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    think("selector", f"Berita terpilih disimpan: {out}", "keputusan")
    print(f"Berita terpilih disimpan: {out}")


if __name__ == "__main__":
    main()
