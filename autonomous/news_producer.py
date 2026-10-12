#!/usr/bin/env python3
"""
news_producer.py — Produksi artikel berita full dari berita terpilih.

Pipeline:
    berita.json (news_selector) -> fetch konten full dari source_url
              -> deteksi bahasa -> auto-translate ke Bahasa Indonesia
              -> susun struktur jurnalistik (judul, lead, isi, penutup)
              -> quality_gate() -> article.json

ANTI AI SLOP: quality_gate() menolak bila:
- konten < 80 kata (ambang dilonggarkan 12 Okt 2026 — sebelumnya 150,
  terlalu agresif sampai pipeline hampir tidak pernah menghasilkan artikel)
- tidak ada gambar konten yang valid (bukan logo/icon/tombol)
- hasil terjemahan kosong/rusak

Contoh:
    python3 news_producer.py --berita state/berita_terpilih.json --out out/artikel
    python3 news_producer.py --berita state/berita_terpilih.json --out out/artikel --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date

import requests
from bs4 import BeautifulSoup

from thought_log import think, section

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")

UA = ("Mozilla/5.0 (Linux; Android 10; POCO X6 Pro) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

MIN_WORDS = 80
MIN_IMG_DIM = 300

# Pola URL yang PASTI bukan gambar konten (logo, icon, tombol, sosmed).
BAD_IMG_PATTERNS = [
    "logo", "icon", "sprite", "button", "btn-", "badge", "avatar",
    "placeholder", "blank", "pixel", "spacer", "loading", "spinner",
    "facebook.com", "twitter.com", "instagram.com", "youtube.com",
    "whatsapp", "telegram", "share-", "social-", "follow-",
    "ads", "banner", "promo", ".svg",
]

# Kata fungsi khas Bahasa Indonesia — untuk deteksi bahasa sederhana.
ID_MARKERS = {
    "yang", "dan", "dari", "untuk", "dengan", "adalah", "ini", "itu",
    "tidak", "akan", "juga", "pada", "dalam", "sebagai", "oleh",
    "telah", "sudah", "dapat", "bisa", "karena", "namun", "tetapi",
    "lebih", "sangat", "berita", "menurut", "diluncurkan", "resmi",
}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def word_count(text):
    return len(re.findall(r"\S+", text or ""))


# ============ DETEKSI BAHASA ============

def detect_lang(text):
    """Deteksi bahasa teks. Return 'id' atau 'non-id'.

    Prioritas: pakai langdetect bila terinstal; fallback ke heuristik
    kata fungsi Bahasa Indonesia (tanpa dependensi baru).
    """
    sample = (text or "")[:2000].strip()
    if not sample:
        return "unknown"

    try:
        from langdetect import detect  # noqa
        lang = detect(sample)
        return "id" if lang == "id" else lang
    except ImportError:
        pass
    except Exception:
        pass

    # Heuristik: hitung kata fungsi khas Indonesia.
    words = re.findall(r"[a-z]+", sample.lower())
    if not words:
        return "unknown"
    hits = sum(1 for w in words if w in ID_MARKERS)
    return "id" if hits / len(words) >= 0.04 else "non-id"


# ============ TRANSLATE GRATIS ============

def translate(text, dest="id"):
    """Terjemahkan ke Bahasa Indonesia via endpoint gratis Google (tanpa API key).

    Chunking per kalimat agar tidak melebihi batas panjang request.
    Return teks terjemahan; string kosong bila gagal total.
    """
    if not text or not text.strip():
        return ""
    chunks, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if len(cur) + len(sent) > 1800:
            if cur.strip():
                chunks.append(cur.strip())
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur.strip():
        chunks.append(cur.strip())

    out = []
    for ch in chunks:
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": dest,
                        "dt": "t", "q": ch},
                headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            data = r.json()
            seg = "".join(s[0] for s in data[0] if s and s[0])
            out.append(seg if seg.strip() else ch)
            time.sleep(0.4)
        except Exception as e:
            print(f"[WARN] translate gagal satu chunk, pakai asli: {e}",
                  file=sys.stderr)
            out.append(ch)
    return " ".join(out).strip()


# ============ EKSTRAKSI ARTIKEL + GAMBAR ============

def is_bad_image_url(url):
    u = (url or "").lower()
    if not u or u.startswith("data:"):
        return True, "data URI / kosong"
    for pat in BAD_IMG_PATTERNS:
        if pat in u:
            return True, f"pola '{pat}'"
    return False, ""


def parse_image_dims(data):
    """Baca dimensi JPEG/PNG/WebP dari header tanpa decode penuh."""
    try:
        if len(data) > 30 and data[0] == 0xFF and data[1] == 0xD8:
            i = 2
            while i < len(data) - 8:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return w, h
                ln = int.from_bytes(data[i + 2:i + 4], "big")
                i += 2 + ln
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return w, h
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) > 30:
            if data[12:16] == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
            if data[12:16] == b"VP8L":
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
                return w, h
    except Exception:
        pass
    return None


def check_image_size(url):
    """Return True bila gambar >= MIN_IMG_DIM di kedua sisi (atau tak bisa dicek)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15, stream=True)
        r.raise_for_status()
        head = r.raw.read(32768)
        r.close()
        dims = parse_image_dims(head)
        if dims:
            w, h = dims
            return (w >= MIN_IMG_DIM and h >= MIN_IMG_DIM), f"{w}x{h}"
        return True, "dimensi tak terbaca (diterima)"
    except Exception as e:
        return False, f"gagal cek: {e}"


def extract_image(soup, page_url):
    """Ambil URL gambar konten: og:image -> twitter:image -> img pertama di <article>."""
    cands = []
    for prop in ("og:image", "twitter:image"):
        tag = soup.find("meta", attrs={"property": prop}) or \
              soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            cands.append(tag["content"].strip())
    article = soup.find("article")
    scope = article if article else soup
    for img in scope.find_all("img"):
        src = img.get("src") or ""
        # srcset: ambil kandidat terbesar
        srcset = img.get("srcset") or ""
        if srcset:
            parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
            best, best_w = "", 0
            for p in parts:
                if len(p) == 2 and p[1].endswith("w"):
                    try:
                        w = int(p[1][:-1])
                    except ValueError:
                        continue
                    if w > best_w:
                        best, best_w = p[0], w
            if best:
                src = best
        if src:
            cands.append(src.strip())

    for raw in cands:
        url = urllib.parse.urljoin(page_url, raw)
        bad, reason = is_bad_image_url(url)
        if bad:
            continue
        ok, info = check_image_size(url)
        if ok:
            return url
    return None


def _clean_text(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t


def _jsonld_article_body(soup):
    """Strategi 1: ambil articleBody dari JSON-LD (sering paling bersih)."""
    best = ""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        cands = data if isinstance(data, list) else [data]
        # dukung juga @graph
        expanded = []
        for c in cands:
            if isinstance(c, dict):
                expanded.append(c)
                g = c.get("@graph")
                if isinstance(g, list):
                    expanded.extend([x for x in g if isinstance(x, dict)])
        for it in expanded:
            body = it.get("articleBody")
            if isinstance(body, str):
                body = _clean_text(body)
                if len(body) > len(best):
                    best = body
    return best


def _paras_from(scope):
    """Kumpulkan paragraf <p> yang substansial dari sebuah scope."""
    paras = []
    for p in scope.find_all("p"):
        t = _clean_text(p.get_text(" ", strip=True))
        if len(t) > 40:
            paras.append(t)
    return paras


def _largest_div_text(soup):
    """Strategi 3: teks dari <div> dengan teks terbanyak (fallback)."""
    best, best_len = "", 0
    for div in soup.find_all("div"):
        # lewati div navigasi/iklan/komentar yang jelas
        cls = " ".join(div.get("class") or []).lower()
        did = (div.get("id") or "").lower()
        if any(k in cls or k in did for k in
               ("nav", "menu", "sidebar", "footer", "header", "comment",
                "related", "widget", "ads", "share", "social", "popup")):
            continue
        t = _clean_text(div.get_text(" ", strip=True))
        # hanya teks langsung yang substansial, hindari duplikasi <p>
        if len(t) > best_len and len(t.split()) >= 50:
            best, best_len = t, len(t)
    return best


def extract_article(url):
    """Ambil (konten full, image_url) dari URL asli.

    Beberapa strategi berurutan (12 Okt 2026 — sebelumnya hanya <p> dengan
    satu header sederhana, sehingga hampir selalu 0 kata dan selector
    menolak semua kandidat):
      1. JSON-LD articleBody
      2. <article> → <p>
      3. seluruh halaman → <p>
      4. <div> dengan teks terbanyak
    """
    headers = {
        "User-Agent": UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/webp,*/*;q=0.8"),
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    try:
        r = requests.get(url, headers=headers, timeout=25,
                         allow_redirects=True)
        r.raise_for_status()
        if len(r.content) > 3_000_000:
            return "", None
    except Exception as e:
        print(f"[WARN] gagal buka artikel: {e}", file=sys.stderr)
        return "", None
    try:
        soup = BeautifulSoup(r.content, "html.parser")
        image_url = extract_image(soup, r.url)
        for tag in soup(["script", "style", "nav", "header", "footer",
                         "aside", "form", "button", "iframe", "noscript"]):
            tag.decompose()

        text = _jsonld_article_body(soup)
        if word_count(text) < 60:
            article = soup.find("article")
            paras = _paras_from(article if article else soup)
            text = "\n\n".join(paras)
        if word_count(text) < 60:
            div_text = _largest_div_text(soup)
            if word_count(div_text) > word_count(text):
                text = div_text
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:18000], image_url
    except Exception as e:
        print(f"[WARN] gagal ekstrak artikel: {e}", file=sys.stderr)
        return "", None


# ============ STRUKTUR JURNALISTIK ============

def build_article(title_id, content_id, source_name):
    """Susun struktur jurnalistik: judul, lead, isi, penutup."""
    paras = [p.strip() for p in content_id.split("\n\n") if p.strip()]
    lead = paras[0] if paras else ""
    body = "\n\n".join(paras[1:]) if len(paras) > 1 else ""
    closing = (f"Berita ini disusun dari laporan {source_name}."
               if source_name else "")
    return {
        "judul": title_id.strip(),
        "lead": lead,
        "isi": body,
        "penutup": closing,
    }


def quality_gate(struct, image_url, translated_ok):
    """Tolak artikel yang tidak layak publish. Return (lolos, [catatan])."""
    catatan = []
    full = " ".join([struct["judul"], struct["lead"], struct["isi"]])

    # 1. Konten minimal 150 kata.
    n = word_count(full)
    if n < MIN_WORDS:
        catatan.append(f"DITOLAK: konten hanya {n} kata (< {MIN_WORDS})")

    # 2. Wajib ada gambar konten yang valid.
    if not image_url:
        catatan.append("DITOLAK: tidak ada gambar konten yang valid")

    # 3. Terjemahan tidak boleh kosong/rusak.
    if not translated_ok:
        catatan.append("DITOLAK: hasil terjemahan kosong/rusak")

    # 4. Judul tidak boleh kosong.
    if not struct["judul"].strip():
        catatan.append("DITOLAK: judul kosong")

    lolos = not catatan
    if lolos:
        catatan.append(f"LOLOS: {n} kata, gambar valid, terjemahan OK")
    return lolos, catatan


# ============ MAIN ============

def main():
    ap = argparse.ArgumentParser(
        description="Produksi artikel berita full + auto-translate ke Bahasa Indonesia.")
    ap.add_argument("--berita", required=True,
                    help="Path JSON berita terpilih dari news_selector.py")
    ap.add_argument("--out", default="", help="Direktori output artikel")
    ap.add_argument("--dry-run", action="store_true",
                    help="Hanya fetch + deteksi bahasa, tanpa publish file")
    ap.add_argument("--skip-gate", action="store_true",
                    help="Lewati quality gate (tidak disarankan)")
    args = ap.parse_args()

    item = load_json(args.berita)
    title_raw = (item.get("title") or "").strip()
    source_url = (item.get("source_url") or "").strip()
    source_name = (item.get("source_name") or "").strip()
    category = (item.get("category") or "").strip()

    section("Produksi Artikel")
    think("producer", f"Memproses: {title_raw[:70]}", "data")

    # ── 1. Fetch konten full dari URL asli ──
    content_raw, image_url = ("", None)
    if source_url:
        think("producer", f"Fetch konten full dari {source_url[:60]}...")
        content_raw, image_url = extract_article(source_url)
        think("producer", f"Konten terambil: {word_count(content_raw)} kata", "data")
    # Fallback ke konten yang sudah ada di item selector.
    if word_count(content_raw) < MIN_WORDS and item.get("content"):
        content_raw = item["content"]
        think("producer", "Pakai konten fallback dari item selector", "peringatan")
    if not image_url and item.get("image_url"):
        image_url = item["image_url"]
        think("producer", "Pakai image_url fallback dari item selector")
    think("producer", f"Gambar: {image_url[:60] + '...' if image_url else 'TIDAK ADA'}",
          "keputusan" if image_url else "tolak")

    # ── 2. Deteksi bahasa ──
    sample = f"{title_raw}\n{content_raw}"
    lang = detect_lang(sample)
    think("producer", f"Bahasa asli terdeteksi: {lang}",
          "data" if lang != "unknown" else "peringatan")

    # ── 3. Auto-translate ke Bahasa Indonesia bila perlu ──
    original_title = title_raw
    original_lang = lang
    translated_ok = True
    if lang == "id":
        title_id, content_id = title_raw, content_raw
        think("producer", "Sudah Bahasa Indonesia — tidak perlu diterjemahkan",
              "keputusan")
    else:
        think("producer", f"Menerjemahkan judul + konten ke Bahasa Indonesia...")
        title_id = translate(title_raw)
        content_id = translate(content_raw)
        if not title_id.strip() or word_count(content_id) < 10:
            translated_ok = False
            think("producer", "Hasil terjemahan kosong/rusak!", "tolak")
            # fallback: pakai teks asli agar pipeline tidak buta
            title_id = title_id.strip() or title_raw
            content_id = content_id if word_count(content_id) >= 10 else content_raw
        else:
            think("producer",
                  f"Terjemahan selesai: {word_count(content_id)} kata", "keputusan")

    # ── 4. Struktur jurnalistik ──
    struct = build_article(title_id, content_id, source_name)
    think("producer",
          f"Struktur: judul + lead ({word_count(struct['lead'])} kata) + "
          f"isi ({word_count(struct['isi'])} kata)")

    # ── 5. Quality gate ──
    lolos, catatan = quality_gate(struct, image_url, translated_ok)
    section("Quality Gate Artikel")
    for c in catatan:
        lvl = "tolak" if c.startswith("DITOLAK") else (
            "keputusan" if c.startswith("LOLOS") else "info")
        think("producer", c, lvl)
    if not lolos and not args.skip_gate:
        print("Artikel DITOLAK quality gate.", file=sys.stderr)
        sys.exit(4)

    # ── 6. Output article.json ──
    full_text = "\n\n".join(
        p for p in [struct["judul"], struct["lead"], struct["isi"],
                    struct["penutup"]] if p)
    article = {
        "title": struct["judul"],
        "original_title": original_title,
        "original_lang": original_lang,
        "translated": original_lang != "id",
        "lead": struct["lead"],
        "content": full_text,
        "summary": struct["lead"][:500],
        "image_url": image_url,
        "source_name": source_name,
        "source_url": source_url,
        "category": category,
        "tags": item.get("tags", []),
        "published_at": item.get("published_at"),
        "skor_prediksi": item.get("skor"),
        "alasan_selector": item.get("alasan", []),
        "weights_version": item.get("weights_version", 1),
        "diproduksi_pada": date.today().isoformat(),
        "news_id": item.get("news_id"),
        "views_aktual": None,   # diisi news_feedback
        "feedback_done": False,
    }

    if args.dry_run:
        print("[dry-run] artikel tidak ditulis.")
        print(json.dumps({k: (str(v)[:200] if isinstance(v, str) else v)
                          for k, v in article.items()},
                         indent=2, ensure_ascii=False))
        return

    out_dir = args.out or os.path.join(STATE_DIR, date.today().isoformat())
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "article.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(article, f, indent=2, ensure_ascii=False)
    think("producer", f"article.json ditulis: {out_path}", "keputusan")
    print(f"Selesai: {out_path}")


if __name__ == "__main__":
    main()
