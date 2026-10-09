# Hozin News Moderator

Moderator otomatis untuk berita Hozin AI Studio. Berjalan tiap 15 menit via GitHub Actions.

## Cara kerja

1. Ambil 100 berita terbaru dari API
2. Periksa terhadap daftar kata/frasa terlarang:
   - Pornografi
   - Judi online (judol)
   - Kata kasar (wordlist dari [kata-kasar](https://github.com/joshuamanly/kata-kasar))
   - Umpan phishing
3. Item yang kena flag langsung dihapus via API

## Keamanan

- Script dalam bentuk minified/obfuscated
- Secret API (`AISTUDIO_CRON_SECRET`) disimpan di GitHub Secrets, tidak ada di kode
- Log hasil moderasi tercatat di server dan bisa dipantau dari panel super admin
