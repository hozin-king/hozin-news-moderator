import json
import os
import re
import sys
import requests
API = 'https://aistudio.hozinking.my.id'
SECRET = os.environ.get('AISTUDIO_CRON_SECRET', '').strip()
UA = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10) HozinNewsModerator/1.0'}
PORN = ['bokep', 'porno', 'porn', 'hentai', 'onlyfans', 'bugil', 'esek-esek', 'mesum', 'cabul', 'video syur', 'foto syur', 'skandal seks', 'asusila']
PORN_WORD = ['xxx', 'jav', 'sex', 'porn']
JUDOL_STRONG = ['togel', 'judol', 'judi online', 'judi slot', 'maxwin', 'mix parlay', 'parlay', 'sbobet', 'maxbet', 'bandar togel', 'bandar judi', 'situs slot', 'link slot', 'daftar slot', 'situs togel', 'agen togel', 'gacor hari ini', 'pola gacor', 'rtp slot', 'slot gacor', 'scatter hitam', 'deposit pulsa']
JUDOL_COMBO = [({'slot'}, {'gacor', 'maxwin', 'scatter', 'pola', 'trik', 'bocoran', 'rtp'}), ({'gacor'}, {'slot', 'togel', 'rtp', 'maxwin'}), ({'deposit'}, {'slot', 'togel', 'judi'}), ({'taruhan'}, {'online', 'bola'})]
PHISHING_LURE = ['segera verifikasi', 'verifikasi akun anda', 'verifikasi akun kamu', 'akun anda akan diblokir', 'akun kamu akan diblokir', 'akun anda terkunci', 'klik tautan berikut', 'klik link berikut', 'masukkan kata sandi', 'masukkan password', 'konfirmasi data pribadi', 'data pribadi anda', 'hadiah gratis klik', 'klaim hadiah', 'selamat anda menang', 'anda terpilih sebagai pemenang']

def _load_kata_kasar():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kata_kasar_id.json')
        with open(p, encoding='utf-8') as f:
            return json.load(f).get('id', [])
    except Exception as e:
        print(f'[WARN] gagal load kata_kasar_id.json: {e}', file=sys.stderr)
        return []
KASAR = _load_kata_kasar()

def _wb(_a):
    return re.compile('\\b' + re.escape(_a) + '\\b', re.IGNORECASE)
PORN_WORD_RE = [_wb(w) for w in PORN_WORD]
KASAR_RE = [_wb(w) for w in KASAR]

def check_item(_b):
    """Return (kategori, istilah) atau (None, None)."""
    low = _b.lower()
    for p in PORN:
        if p in low:
            return ('porn', p)
    for rx in PORN_WORD_RE:
        if rx.search(_b):
            return ('porn', rx.pattern.strip('\\b'))
    for j in JUDOL_STRONG:
        if j in low:
            return ('judol', j)
    words = set(re.findall('[a-z0-9]+', low))
    for need_a, need_b in JUDOL_COMBO:
        if need_a & words and need_b & words:
            return ('judol', '+'.join(sorted(need_a & words)) + '/' + '+'.join(sorted(need_b & words)))
    for ph in PHISHING_LURE:
        if ph in low:
            return ('phishing', ph)
    for rx, w in zip(KASAR_RE, KASAR):
        if rx.search(_b):
            return ('kasar', w)
    return (None, None)

def fetch_recent(_c=100):
    r = requests.get(f'{API}/api/news', params={'limit': _c}, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json().get('news', [])

def moderate(_d, _e):
    r = requests.post(f'{API}/api/ingest/moderate', headers={**UA, 'X-Cron-Secret': SECRET}, json={'ids': _d, 'reason': _e}, timeout=30)
    r.raise_for_status()
    return r.json()

def main():
    if not SECRET:
        print('[ERROR] AISTUDIO_CRON_SECRET tidak di-set.', file=sys.stderr)
        sys.exit(1)
    items = fetch_recent()
    print(f'[INFO] memeriksa {len(items)} berita terbaru')
    flagged = {}
    for n in items:
        nid = n.get('id')
        if not nid:
            continue
        text = ' '.join([str(n.get('title') or ''), str(n.get('summary') or ''), str(n.get('content') or ''), ' '.join(n.get('tags') or [])])
        cat, term = check_item(text)
        if cat:
            flagged[nid] = (cat, term, (n.get('title') or '')[:70])
    if not flagged:
        print('[INFO] bersih — tidak ada konten terlarang.')
        return
    by_cat = {}
    for nid, (cat, term, title) in flagged.items():
        by_cat.setdefault(cat, []).append(nid)
        print(f"[FLAG] #{nid} [{cat}] '{term}' :: {title}")
    for cat, ids in by_cat.items():
        res = moderate(ids, cat)
        print(f'[INFO] dihapus {res.get('deleted', 0)} item kategori {cat}')
if __name__ == '__main__':
    main()
