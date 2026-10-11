#!/usr/bin/env python3
"""
thought_log.py — Logging pemikiran sistem otonom.

Setiap komponen (selector, producer, feedback) menulis jejak pemikirannya
ke file log harian, sehingga user bisa melihat KENAPA setiap keputusan diambil:
- Kenapa topik A dipilih dan topik B ditolak
- Kenapa script lolos/ditolak quality gate
- Kenapa bobot strategi berubah

Format: [timestamp] [komponen] pesan
Log harian: state/thought_YYYY-MM-DD.md (Markdown, enak dibaca)
"""

import os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")


def _log_path():
    os.makedirs(STATE_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(STATE_DIR, f"thought_{today}.md")


def think(komponen, pesan, level="info"):
    """Tulis satu baris pemikiran ke log harian + print ke stdout."""
    ts = datetime.now().strftime("%H:%M:%S")
    icon = {"info": "💭", "keputusan": "✅", "tolak": "❌", "data": "📊",
            "peringatan": "⚠️"}.get(level, "💭")
    baris = f"[{ts}] {icon} **{komponen}**: {pesan}"
    print(baris)
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write(baris + "\n")


def section(judul):
    """Pemisah section di log."""
    ts = datetime.now().strftime("%H:%M:%S")
    baris = f"\n## [{ts}] {judul}\n"
    print(baris.strip())
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write(baris)


def get_today_log():
    """Path log hari ini (untuk dibaca user)."""
    return _log_path()
