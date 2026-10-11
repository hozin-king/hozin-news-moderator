#!/usr/bin/env python3
"""
news_feedback.py — Analitik yang mengubah strategi, bukan sekadar laporan.

Membaca:
  - state/produced.json : daftar artikel yang diproduksi sistem otonom
  - /api/news/public    : views aktual tiap berita (match by source_url)

Lalu:
  - bandingkan views aktual vs skor prediksi selector
  - sesuaikan bobot di weights.json (kategori & keyword), disimpan dengan version+1
  - tulis laporan penyesuaian ke state/news_feedback_YYYY-MM-DD.json

Contoh:
    python3 news_feedback.py
    python3 news_feedback.py --dry-run
"""

import argparse
import copy
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime

from thought_log import think, section

BASE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = os.path.join(BASE, "weights.json")
STATE_DIR = os.path.join(BASE, "state")
PRODUCED_FILE = os.path.join(STATE_DIR, "produced.json")

PUBLIC_API = "https://aistudio.hozinking.my.id/api/news/public"

# Artikel dianggap "matang" untuk dinilai setelah publish N hari.
MIN_AGE_DAYS = 3


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_views():
    """views per source_url dari API publik (ambil beberapa halaman)."""
    views = {}
    for offset in (0, 100, 200):
        url = f"{PUBLIC_API}?limit=100&offset={offset}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 10) hozin-news-autonomous/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[WARN] gagal ambil views (offset {offset}): {e}", file=sys.stderr)
            break
        items = data.get("news", [])
        if not items:
            break
        for n in items:
            u = (n.get("source_url") or "").strip()
            if u:
                views[u] = {"views": n.get("views", 0) or 0,
                            "published_at": n.get("published_at")}
    return views


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def main():
    ap = argparse.ArgumentParser(
        description="Evaluasi performa artikel otonom & sesuaikan weights.json.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Hitung penyesuaian tanpa menyimpan")
    args = ap.parse_args()

    weights = load_json(WEIGHTS_FILE, None)
    if not weights:
        print("weights.json tidak ditemukan", file=sys.stderr)
        sys.exit(2)
    produced = load_json(PRODUCED_FILE, [])
    if not produced:
        print("Belum ada artikel di state/produced.json — tidak ada yang dievaluasi.")
        return

    stats = load_views()

    lr = weights.get("learning_rate", 0.2)
    lo, hi = weights.get("min_weight", 0.3), weights.get("max_weight", 3.0)
    new_weights = copy.deepcopy(weights)
    adjustments = []
    evaluated = 0

    for p in produced:
        url = (p.get("source_url") or "").strip()
        if not url or p.get("feedback_done"):
            continue
        st = stats.get(url)
        if not st:
            continue
        views = st.get("views", 0) or 0

        # Hanya nilai artikel yang datanya cukup matang.
        pub = st.get("published_at")
        try:
            if isinstance(pub, (int, float)):
                dt = datetime.fromtimestamp(pub)
            else:
                dt = datetime.fromisoformat(str(pub)[:19])
            age = (datetime.now() - dt).days
        except (ValueError, TypeError):
            age = 999
        if age < MIN_AGE_DAYS:
            continue

        evaluated += 1
        pred = p.get("skor_prediksi") or 1.0
        # Rasio performa vs ekspektasi: views dinormalisasi ke skala skor
        # (skor ~1-3; 100 views ~= ekspektasi dasar untuk berita).
        actual_norm = views / 100.0
        ratio = actual_norm / max(0.1, pred)
        ratio = clamp(ratio, 0.25, 4.0)

        cat = (p.get("category") or "").lower()
        title_text = f"{p.get('title', '')}".lower()

        # Sesuaikan bobot kategori.
        if cat:
            old_w = new_weights.get("category", {}).get(
                cat, weights.get("category_default", 1.0))
            new_w = clamp(old_w * (1 + lr * (ratio - 1)), lo, hi)
            new_weights.setdefault("category", {})[cat] = round(new_w, 3)
            adjustments.append(
                f"kategori '{cat}': {old_w:.2f} -> {new_w:.2f} "
                f"(artikel {views} views, rasio {ratio:.2f}x)")

        # Sesuaikan keyword yang muncul di judul.
        for kw in list(new_weights.get("keyword_boost", {}).keys()):
            if kw.lower() in title_text:
                old_k = new_weights["keyword_boost"][kw]
                new_k = clamp(old_k * (1 + lr * (ratio - 1) * 0.5), lo, hi)
                new_weights["keyword_boost"][kw] = round(new_k, 3)
                adjustments.append(
                    f"keyword '{kw}': {old_k:.2f} -> {new_k:.2f} (rasio {ratio:.2f}x)")

        # Kata baru berkorelasi performa bagus -> masukkan sebagai keyword.
        if ratio >= 2.0:
            for kata in set(re.findall(r"[a-z]{4,}", title_text)):
                if kata not in new_weights.get("keyword_boost", {}) and len(kata) > 4:
                    if kata not in {"yang", "dari", "untuk", "dengan", "berita",
                                    "resmi", "akan", "ini", "baru"}:
                        new_weights.setdefault("keyword_boost", {})[kata] = \
                            round(1.0 + lr, 3)
                        adjustments.append(
                            f"keyword baru '{kata}': 1.00 -> {1.0 + lr:.2f} "
                            f"(temuan dari artikel {views} views)")
                        break  # satu keyword baru per artikel, hindari noise

        p["views_aktual"] = views
        p["rasio_vs_prediksi"] = round(ratio, 2)
        p["feedback_done"] = True
        p["feedback_pada"] = date.today().isoformat()

    report = {
        "tanggal": date.today().isoformat(),
        "artikel_dievaluasi": evaluated,
        "weights_version_lama": weights.get("version", 1),
        "weights_version_baru": weights.get("version", 1) + (1 if evaluated else 0),
        "penyesuaian": adjustments,
    }

    if args.dry_run:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    if evaluated:
        new_weights["version"] = weights.get("version", 1) + 1
        new_weights["updated_at"] = date.today().isoformat()
        with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(new_weights, f, indent=2, ensure_ascii=False)
        with open(PRODUCED_FILE, "w", encoding="utf-8") as f:
            json.dump(produced, f, indent=2, ensure_ascii=False)

    rep_path = os.path.join(STATE_DIR,
                            f"news_feedback_{date.today().isoformat()}.json")
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    section("Feedback Loop — Evaluasi & Penyesuaian Strategi Berita")
    think("feedback", f"Mengevaluasi {evaluated} artikel yang sudah >3 hari tayang",
          "data")
    print(f"Dievaluasi: {evaluated} artikel | penyesuaian: {len(adjustments)}")
    for a in adjustments:
        think("feedback", f"Bobot diubah: {a}", "keputusan")
        print(" -", a)
    if evaluated:
        think("feedback",
              f"Strategi diperbarui: weights.json v{new_weights['version']}",
              "keputusan")
        print(f"weights.json -> v{new_weights['version']}")
    else:
        think("feedback",
              "Belum ada artikel yang cukup umur untuk dinilai — strategi tidak berubah")
    print(f"Laporan: {rep_path}")


if __name__ == "__main__":
    main()
