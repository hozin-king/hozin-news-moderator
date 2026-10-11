#!/usr/bin/env python3
"""
run_pipeline.py — Orkestrator pipeline berita otonom.

Menjalankan komponen terpisah secara berkesinambungan:
    selector -> producer -> [database JSON] -> publish (API ingest) -> feedback

Setiap run juga menulis file database JSON untuk API berita gratis:
    database/news_YYYY-MM-DD-HH.json  (arsip per jam)
    database/latest.json              (hasil run terbaru, URL stabil)

Setiap tahap menerima output tahap sebelumnya dan meneruskannya.
Thought log mencatat alur pemikiran di setiap tahap.

Contoh:
    python3 run_pipeline.py --dry-run              # simulasi penuh tanpa publish
    python3 run_pipeline.py                        # produksi + publish beneran
    python3 run_pipeline.py --skip-publish         # produksi tanpa publish ke API
    python3 run_pipeline.py --output-dir /tmp/db   # database JSON ke direktori lain
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")
PRODUCED_FILE = os.path.join(STATE_DIR, "produced.json")

sys.path.insert(0, BASE)
from thought_log import think, section

INGEST_API = "https://aistudio.hozinking.my.id/api/ingest/news"
# Secret dibaca dari env var — JANGAN hardcode.
CRON_SECRET = os.environ.get("AISTUDIO_CRON_SECRET", "")


def run(cmd, **kw):
    print("  $", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.stdout:
        print(r.stdout)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"Gagal (exit {r.returncode}): {' '.join(cmd[:3])}")
    return r


def publish(article_path):
    """Publish artikel ke API ingest. Return True bila sukses."""
    with open(article_path, encoding="utf-8") as f:
        art = json.load(f)
    payload = {
        "title": art["title"],
        "source_url": art["source_url"],
        "category": art["category"],
        "summary": art.get("summary", "")[:2000],
        "content": art["content"][:20000],
        "tags": art.get("tags", []),
        "image_url": art.get("image_url"),
        "source_name": art.get("source_name"),
        "published_at": art.get("published_at"),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        INGEST_API, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Cron-Secret": CRON_SECRET,
                 "User-Agent": "hozin-news-autonomous/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
        ok = bool(resp.get("ok", True))
        think("pipeline", f"Publish: {resp}", "keputusan" if ok else "tolak")
        return ok
    except Exception as e:
        think("pipeline", f"Publish gagal: {e}", "tolak")
        return False


def record_produced(article_path):
    """Catat artikel ke state/produced.json untuk feedback loop."""
    try:
        with open(PRODUCED_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        items = []
    with open(article_path, encoding="utf-8") as f:
        art = json.load(f)
    items.append({
        "title": art["title"],
        "source_url": art["source_url"],
        "category": art["category"],
        "skor_prediksi": art.get("skor_prediksi"),
        "diproduksi_pada": art.get("diproduksi_pada"),
        "feedback_done": False,
    })
    with open(PRODUCED_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def to_database_entry(article_path, now_utc):
    """Konversi article.json ke format entri API gratis."""
    with open(article_path, encoding="utf-8") as f:
        art = json.load(f)
    uid = hashlib.md5(
        f"{art.get('source_url', '')}|{art.get('published_at', '')}".encode()
    ).hexdigest()[:12]
    return {
        "id": uid,
        "title": art.get("title", ""),
        "summary": (art.get("summary") or art.get("lead") or "")[:500],
        "content": art.get("content", ""),
        "image_url": art.get("image_url"),
        "source_name": art.get("source_name"),
        "source_url": art.get("source_url"),
        "category": art.get("category"),
        "published_at": art.get("published_at"),
        "original_lang": art.get("original_lang", "id"),
        "scraped_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_database_files(article_path, output_dir, now_utc):
    """Tulis database/news_YYYY-MM-DD-HH.json + latest.json (overwrite).

    File-file ini yang di-commit oleh GitHub Actions workflow
    (.github/workflows/news-pipeline.yml) sebagai API gratis.
    """
    os.makedirs(output_dir, exist_ok=True)
    entry = to_database_entry(article_path, now_utc)
    payload = [entry]  # satu artikel per run

    hourly = os.path.join(
        output_dir, f"news_{now_utc.strftime('%Y-%m-%d-%H')}.json")
    with open(hourly, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    latest = os.path.join(output_dir, "latest.json")
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    think("pipeline", f"Database ditulis: {hourly} + latest.json", "keputusan")
    return hourly, latest


def main():
    ap = argparse.ArgumentParser(description="Pipeline berita otonom end-to-end.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Simulasi tanpa publish")
    ap.add_argument("--skip-publish", action="store_true",
                    help="Produksi tanpa publish ke API")
    ap.add_argument("--berita-json",
                    help="Pakai berita JSON yang sudah ada (skip selector)")
    ap.add_argument("--output-dir", default="",
                    help="Direktori output JSON database (default: ../database "
                         "relatif dari autonomous/)")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    today = date.today().isoformat()
    now_utc = datetime.now(timezone.utc)
    out_dir = os.path.join(STATE_DIR, today)
    os.makedirs(out_dir, exist_ok=True)
    db_dir = args.output_dir or os.path.join(BASE, "..", "database")

    section("Pipeline Berita Otonom — " + today)
    think("pipeline", "Memulai pipeline: selector -> producer -> publish -> feedback",
          "data")

    # ── Tahap 1: Selector ──
    if args.berita_json:
        berita_json = args.berita_json
        think("pipeline", f"Pakai berita yang sudah ada: {berita_json}")
    else:
        think("pipeline", "Tahap 1/4: memilih berita...")
        berita_json = os.path.join(out_dir, "berita_terpilih.json")
        run([sys.executable, os.path.join(BASE, "news_selector.py"),
             "--out", berita_json])
        think("pipeline", f"Berita tersimpan: {berita_json}", "keputusan")

    with open(berita_json, encoding="utf-8") as f:
        berita = json.load(f)
    think("pipeline",
          f"Berita: {(berita.get('title') or '?')[:70]} (skor {berita.get('skor', '?')})")

    # ── Tahap 2: Producer ──
    think("pipeline", "Tahap 2/4: produksi artikel...")
    art_dir = os.path.join(out_dir, "artikel")
    os.makedirs(art_dir, exist_ok=True)
    cmd = [sys.executable, os.path.join(BASE, "news_producer.py"),
           "--berita", berita_json, "--out", art_dir]
    if args.dry_run:
        cmd.append("--dry-run")
    run(cmd)
    art_path = os.path.join(art_dir, "article.json")
    if args.dry_run or not os.path.exists(art_path):
        think("pipeline", "Dry-run selesai — tahap publish & feedback dilewati")
        return
    think("pipeline", f"Artikel selesai: {art_path}", "keputusan")

    # ── Tahap 2b: Tulis file database JSON untuk API gratis ──
    # (jalan walau publish ke API ingest dilewati/gagal — file ini yang
    #  di-commit oleh GitHub Actions workflow news-pipeline.yml)
    write_database_files(art_path, db_dir, now_utc)

    # ── Tahap 3: Publish ──
    if not args.skip_publish:
        think("pipeline", "Tahap 3/4: publish ke API ingest...", "data")
        if not CRON_SECRET:
            think("pipeline",
                  "AISTUDIO_CRON_SECRET tidak di-set — publish DIBATALKAN",
                  "peringatan")
            print("ERROR: set env AISTUDIO_CRON_SECRET dulu.", file=sys.stderr)
            sys.exit(5)
        if publish(art_path):
            record_produced(art_path)
            think("pipeline", "Artikel tercatat di produced.json", "keputusan")
        else:
            think("pipeline", "Publish gagal — feedback dilewati", "tolak")
            sys.exit(6)
    else:
        think("pipeline", "Publish dilewati (--skip-publish)")

    # ── Tahap 4: Feedback ──
    think("pipeline", "Tahap 4/4: feedback loop...")
    run([sys.executable, os.path.join(BASE, "news_feedback.py")])

    think("pipeline", "Pipeline selesai 🎉", "keputusan")
    print(f"\nLog pemikiran: {os.path.join(STATE_DIR, f'thought_{today}.md')}")


if __name__ == "__main__":
    main()
