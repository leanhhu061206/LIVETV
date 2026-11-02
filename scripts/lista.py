import requests
import os
import re
import time
import unicodedata
import urllib.parse
import concurrent.futures
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

load_dotenv()

# Definisce il percorso della cartella dello script e della cartella principale
script_dir = os.path.dirname(os.path.abspath(__file__))
output_dir = os.path.dirname(script_dir)

# ---------- EasySports-style poster generator ----------
from PIL import Image, ImageDraw, ImageFont, ImageFilter

def _env_int(name, default, minimum):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except Exception:
        return default

POSTER_SIZE = (_env_int("POSTER_WIDTH", 640, 320), _env_int("POSTER_HEIGHT", 360, 180))
_EMOJI_RE = re.compile(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\U000E0000-\U000E007F]')

def _poster_clean(value):
    return re.sub(r'\s+', ' ', _EMOJI_RE.sub(' ', str(value or ''))).strip()


def _poster_slug(value):
    text = _poster_clean(value)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'[^A-Za-z0-9]+', '_', text)
    return re.sub(r'_+', '_', text).strip('_') or 'team'


def poster_relative_path(team1, team2):
    return os.path.join("logos", f"{_poster_slug(team1)}_vs_{_poster_slug(team2)}.png")


_MIGRATED_LOGO_NAMES = False


def migrate_logo_filenames():
    global _MIGRATED_LOGO_NAMES
    if _MIGRATED_LOGO_NAMES:
        return
    logos_dir = os.path.join(output_dir, "logos")
    if not os.path.isdir(logos_dir):
        _MIGRATED_LOGO_NAMES = True
        return
    targets = {}
    for name in os.listdir(logos_dir):
        if not name.lower().endswith(".png") or "_vs_" not in name:
            continue
        team1, team2 = name[:-4].split("_vs_", 1)
        target = os.path.basename(poster_relative_path(team1, team2))
        if target != name:
            targets.setdefault(target, []).append(name)
    for target, sources in targets.items():
        target_path = os.path.join(logos_dir, target)
        candidates = list(sources)
        if os.path.exists(target_path) and target not in candidates:
            candidates.append(target)
        candidates.sort(key=lambda n: os.path.getmtime(os.path.join(logos_dir, n)), reverse=True)
        keep = candidates[0]
        keep_path = os.path.join(logos_dir, keep)
        if os.path.abspath(keep_path) != os.path.abspath(target_path):
            os.replace(keep_path, target_path)
        for extra in candidates[1:]:
            extra_path = os.path.join(logos_dir, extra)
            if os.path.abspath(extra_path) != os.path.abspath(target_path):
                try:
                    os.remove(extra_path)
                except Exception:
                    pass
    _MIGRATED_LOGO_NAMES = True

def _poster_font(size, bold=True, italic=False):
    if bold and italic:
        names = ['DejaVuSans-BoldOblique.ttf', 'arialbi.ttf']
    elif bold:
        names = ['DejaVuSans-Bold.ttf', 'arialbd.ttf']
    elif italic:
        names = ['DejaVuSans-Oblique.ttf', 'ariali.ttf']
    else:
        names = ['DejaVuSans.ttf', 'arial.ttf']
    for folder in ('/usr/share/fonts/truetype/dejavu/', 'C:/Windows/Fonts/', ''):
        for name in names:
            try:
                return ImageFont.truetype(folder + name, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()

def _poster_fit(img, box):
    if img is None:
        return None
    img = img.convert('RGBA')
    img.thumbnail((box, box), getattr(Image, 'Resampling', Image).LANCZOS)
    return img

def _poster_wrap(text, max_chars, max_lines=2):
    words = text.split()
    lines, cur = [], ''
    for word in words:
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= max_chars:
            cur += ' ' + word
        else:
            lines.append(cur)
            cur = word
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and words and len(' '.join(lines)) < len(' '.join(words)):
        lines[-1] = lines[-1].rstrip() + '\u2026'
    return lines[:max_lines]

def _poster_accent(event_name, category):
    text = (str(event_name or '') + ' ' + str(category or '')).lower()
    if re.search(r'basket|nba|euroleague', text): return (224, 138, 60)
    if re.search(r'tennis|atp|wta', text): return (200, 212, 74)
    if re.search(r'hockey|nhl', text): return (90, 182, 232)
    if re.search(r'american football|nfl|ncaaf', text): return (196, 107, 74)
    if re.search(r'baseball|mlb', text): return (212, 106, 106)
    if re.search(r'mma|box|wrestl|fight', text): return (217, 83, 79)
    if re.search(r'motogp|formula|f1|nascar|racing|motorsport', text): return (224, 178, 60)
    if re.search(r'golf', text): return (116, 184, 122)
    if re.search(r'cricket', text): return (111, 168, 214)
    if re.search(r'darts', text): return (211, 107, 168)
    if re.search(r'rugby', text): return (142, 124, 195)
    if re.search(r'volley', text): return (224, 194, 74)
    return (63, 166, 106)

def _poster_league(event_name, category):
    text = _poster_clean(event_name)
    if ':' in text:
        text = text.split(':', 1)[0]
    if '|' in text:
        text = text.split('|')[-1]
    text = text.strip(' -')
    return (text or _poster_clean(category) or 'LIVE EVENT').upper()[:46]

def _poster_initials(name):
    words = [w for w in _poster_clean(name).split() if w]
    return ''.join(w[0] for w in words[:2]).upper() or '?'

def create_event_poster(team1, team2, img1, img2, event_name='', category='', time_str='', output_path=''):
    W, H = POSTER_SIZE
    S = W / 640.0
    accent = _poster_accent(event_name, category)
    gold = (212, 175, 55)

    bg = Image.new('RGB', (W, H), (10, 10, 15))
    draw = ImageDraw.Draw(bg)
    top, bottom = (10, 10, 15), (25, 25, 38)
    for y in range(H):
        t = y / max(1, H - 1)
        draw.line((0, y, W, y), fill=(
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        ))

    bg = bg.convert('RGBA')
    draw = ImageDraw.Draw(bg)

    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.polygon([(0, H), (W, int(H * 0.66)), (W, H)], fill=(*accent, 22))
    od.polygon([(0, H), (W, int(H * 0.79)), (W, H)], fill=(*gold, 16))
    bg = Image.alpha_composite(bg, overlay)
    draw = ImageDraw.Draw(bg)

    league = _poster_league(event_name, category)
    header_font = _poster_font(max(10, int(13 * S)), bold=True)
    draw.text((int(32 * S), int(18 * S)), league, font=header_font, fill=accent, anchor='la')

    box = int(170 * S)
    centers = [int(W * 0.25), int(W * 0.75)]
    logo_y = int(H * 0.43)
    shadow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for cx in centers:
        sd.ellipse((cx - box * 0.45, int(H * 0.68), cx + box * 0.45, int(H * 0.73)), fill=(0, 0, 0, 100))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(3, int(6 * S))))
    bg = Image.alpha_composite(bg, shadow)
    draw = ImageDraw.Draw(bg)

    for img, name, cx in ((img1, team1, centers[0]), (img2, team2, centers[1])):
        logo = _poster_fit(img, box)
        if logo:
            x = cx - logo.width // 2
            y = logo_y - logo.height // 2
            bg.alpha_composite(logo, (x, y))
        else:
            initials = _poster_initials(name)
            font = _poster_font(max(20, int(54 * S)), bold=True)
            draw.text((cx, logo_y), initials, font=font, fill=accent, anchor='mm')

    cx, cy = W // 2, logo_y
    r = int(44 * S)
    draw.polygon([(cx - int(68 * S), cy - int(30 * S)), (cx - int(22 * S), cy + int(40 * S)),
                  (cx - int(34 * S), cy + int(44 * S)), (cx - int(80 * S), cy - int(26 * S))], fill=gold)
    draw.polygon([(cx + int(68 * S), cy + int(30 * S)), (cx + int(22 * S), cy - int(40 * S)),
                  (cx + int(34 * S), cy - int(44 * S)), (cx + int(80 * S), cy + int(26 * S))], fill=accent)
    pts = [(cx, cy - r), (cx + int(r * 0.85), cy - r // 2), (cx + int(r * 0.85), cy + r // 2),
           (cx, cy + r), (cx - int(r * 0.85), cy + r // 2), (cx - int(r * 0.85), cy - r // 2)]
    draw.polygon(pts, fill=(20, 20, 28))
    draw.line(pts + [pts[0]], fill=gold, width=max(2, int(2 * S)))
    vs_font = _poster_font(max(16, int(28 * S)), bold=True, italic=True)
    draw.text((cx, cy + int(1 * S)), 'VS', font=vs_font, fill=gold, anchor='mm')

    name_font = _poster_font(max(11, int(16 * S)), bold=True)
    for name, cx in ((team1, centers[0]), (team2, centers[1])):
        lines = _poster_wrap(_poster_clean(name), 18, 2)
        start_y = int(H * 0.78) if len(lines) == 1 else int(H * 0.75)
        for i, line in enumerate(lines):
            draw.text((cx, start_y + i * int(18 * S)), line, font=name_font, fill=(241, 245, 249), anchor='ma')

    rule_y = int(H * 0.90)
    draw.line((int(64 * S), rule_y, W - int(64 * S), rule_y), fill=(*gold, 90), width=1)

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        bg.convert('RGB').save(output_path, 'PNG', optimize=True)
    return bg

def poster_is_current(path):
    try:
        with Image.open(path) as im:
            return im.size == POSTER_SIZE
    except Exception:
        return False


def cached_poster_url(team1, team2):
    relative = poster_relative_path(team1, team2)
    absolute = os.path.join(output_dir, relative)
    try:
        if not os.path.exists(absolute):
            return None, absolute
        if time.time() - os.path.getmtime(absolute) > 3 * 60 * 60:
            return None, absolute
        if not poster_is_current(absolute):
            return None, absolute
        user = os.getenv("NOMEGITHUB", "").strip()
        repo = os.getenv("NOMEREPO", "").strip()
        if user and repo:
            url = f"https://raw.githubusercontent.com/{user}/{repo}/main/{urllib.parse.quote(relative.replace(os.sep, '/'), safe='/')}"
            return url, absolute
        return absolute, absolute
    except Exception:
        return None, absolute
# ---------- end poster generator ----------

# ---------- EasySports-style multi-source logo resolver ----------

LOGO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
LOGO_TTL = 24 * 3600
LOGO_MISS_TTL = 10 * 60
_LOGO_CACHE = {}
_LOGO_TS = {}
_PREFETCHED = set()
_WIKI_BACKOFF = 0.0
_TSDB_BACKOFF = 0.0

_SPORT_RULES = [
    ('fight', re.compile(r'(\bmma\b|wrestl|\bcombat\b|\bboxing\b|\bufc\b|\baew\b|\bwwe\b|🥊|🤼|🥋)', re.I)),
    ('motor-sports', re.compile(r'(motogp|moto gp|formula|\bf1\b|nascar|superbike|indycar|motorsport|🏍|🏎)', re.I)),
    ('american-football', re.compile(r'(ncaaf|american football|🏈)', re.I)),
    ('ice-hockey', re.compile(r'(ice hockey|hockey|\bnhl\b|\bkhl\b|\bahl\b|\bechl\b|\bohl\b|\bwhl\b|🏒)', re.I)),
    ('basketball', re.compile(r'(basketball|\bnba\b|eurocup|euroleague|\baba\b|\bvtb\b|opap|🏀)', re.I)),
    ('baseball', re.compile(r'(baseball|\bmlb\b|⚾)', re.I)),
    ('tennis', re.compile(r'(tennis|\batp\b|\bwta\b|🎾)', re.I)),
    ('golf', re.compile(r'(golf|⛳)', re.I)),
    ('cricket', re.compile(r'(cricket|\bipl\b|🏏)', re.I)),
    ('darts', re.compile(r'(darts|🎯)', re.I)),
    ('rugby', re.compile(r'(rugby|🏉)', re.I)),
    ('volleyball', re.compile(r'(volleyball|🏐)', re.I)),
    ('other', re.compile(r'(water polo|handball|floorball|rowing|weightlifting|snooker|🏊|🤾)', re.I)),
    ('soccer', re.compile(r'(soccer|football|premiership|\bleague\b|\bmls\b|futsal|⚽)', re.I)),
]

_WIKI_SPORT_RE = {
    'soccer': re.compile(r'football|soccer|\bfc\b|sports club|reserve team', re.I),
    'basketball': re.compile(r'basketball', re.I),
    'tennis': re.compile(r'tennis', re.I),
    'ice-hockey': re.compile(r'ice hockey|hockey', re.I),
    'american-football': re.compile(r'american football|gridiron', re.I),
    'baseball': re.compile(r'baseball', re.I),
    'fight': re.compile(r'mixed martial|boxer|boxing|wrestl|fighter|martial', re.I),
    'motor-sports': re.compile(r'racing|motorsport|formula|driver|motorcycle', re.I),
    'golf': re.compile(r'golf', re.I),
    'cricket': re.compile(r'cricket', re.I),
    'darts': re.compile(r'darts', re.I),
    'rugby': re.compile(r'rugby', re.I),
    'volleyball': re.compile(r'volleyball', re.I),
    'other': re.compile(r'sport|athlet|rower|rowing|weightlift|olympic|champion', re.I),
}

_TSDB_SPORT = {
    'soccer': re.compile(r'soccer', re.I), 'basketball': re.compile(r'basketball', re.I),
    'ice-hockey': re.compile(r'ice hockey|hockey', re.I),
    'american-football': re.compile(r'american football|nfl', re.I),
    'baseball': re.compile(r'baseball', re.I), 'rugby': re.compile(r'rugby', re.I),
    'volleyball': re.compile(r'volleyball', re.I), 'cricket': re.compile(r'cricket', re.I),
}
_TSDB_PLAYER_SPORT = {
    'tennis': re.compile(r'tennis', re.I), 'golf': re.compile(r'golf', re.I),
    'darts': re.compile(r'darts', re.I),
    'fight': re.compile(r'boxing|mma|mixed martial|wrestl', re.I),
    'motor-sports': re.compile(r'racing|motorsport|formula|driver|motorcycle', re.I),
}

_WIKI_API = ('https://en.wikipedia.org/w/api.php?action=query&format=json&prop=pageimages|description'
             '&piprop=thumbnail|name&pithumbsize=1280&pilicense=any&redirects=1&maxlag=5')
_WIKI_SEARCH_API = ('https://en.wikipedia.org/w/api.php?action=query&format=json&generator=search'
                    '&prop=pageimages|description&piprop=thumbnail|name&pithumbsize=1280'
                    '&pilicense=any&maxlag=5&gsrsearch=')
_ESPN_SEARCH = 'https://site.web.api.espn.com/apis/search/v2?region=us&lang=en&limit=10&query='
_ESPN_COLLEGE = {
    'women': 'https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/teams?limit=500',
    'men': 'https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams?limit=500',
}
_ESPN_LEAGUES = {
    'spain la liga': 'esp.1', 'spain segunda division': 'esp.2',
    'england premier league': 'eng.1', 'england championship': 'eng.2',
    'italy serie a': 'ita.1', 'italy serie b': 'ita.2',
    'germany bundesliga': 'ger.1', 'germany 2 bundesliga': 'ger.2',
    'france ligue 1': 'fra.1', 'netherlands eredivisie': 'ned.1',
    'netherlands eerste divisie': 'ned.2', 'russian premier league': 'rus.1',
    'romania liga i': 'rou.1', 'greece super league': 'gre.1',
    'peru primera division': 'per.1', 'israel ligat ha al': 'isr.1',
    'sweden allsvenskan': 'swe.1', 'denmark super liga': 'den.1',
    'norway eliteserien': 'nor.1', 'argentina primera division': 'arg.1',
    'afc champions league elite': 'afc.champions',
    'uefa champions league': 'uefa.champions', 'uefa europa league': 'uefa.europa',
    'turkey super lig': 'tur.1', 'portugal primeira liga': 'por.1',
    'belgium pro league': 'bel.1',
}
_SCORES365_SEARCH = 'https://webws.365scores.com/web/search/?appTypeId=5&langId=1&query='
_SCORES365_IMG = 'https://imagecache.365scores.com/image/upload/f_png,c_limit,q_auto:eco,dpr_1/Competitors/'
_SCORES365_COMPETITIONS = {'saudi arabia division 1': 5502, 'sweden superettan': 123}
_SCORES365_STOP = {'al', 'el', 'the', 'fc', 'cf', 'sc', 'club', 'team', 'de', 'cd', 'ca', 'ac', 'as', 'us', 'ss', 'ssc', 'fk', 'bc', 'kk'}
_ESPN_CACHE = {}
_ESPN_LEAGUE_CACHE = {}
_SCORES365_COMP_CACHE = {}
_TSDB_KEY = os.getenv('TSDB_KEY', '123')
_TSDB_API = f'https://www.thesportsdb.com/api/v1/json/{_TSDB_KEY}/searchteams.php?t='

_RESERVE_ALIAS = {'juventus': 'Juventus Next Gen', 'milan': 'Milan Futuro', 'inter': 'Inter U23'}
_PRIORITY_ALIASES = {
    'atalanta': 'Atalanta BC', 'parma': 'Parma Calcio 1913', 'bologna': 'Bologna FC 1909',
    'genoa': 'Genoa CFC', 'verona': 'Hellas Verona FC', 'lecce': 'US Lecce',
    'napoli': 'SSC Napoli', 'roma': 'AS Roma', 'lazio': 'SS Lazio', 'milan': 'AC Milan',
    'inter': 'Inter Milan', 'fiorentina': 'ACF Fiorentina', 'torino': 'Torino FC',
    'udinese': 'Udinese Calcio', 'cagliari': 'Cagliari Calcio', 'como': 'Como 1907',
    'monza': 'AC Monza', 'empoli': 'Empoli FC', 'sassuolo': 'US Sassuolo Calcio',
    'salernitana': 'US Salernitana 1919', 'juventus': 'Juventus FC',
    'al sadd': 'Al Sadd SC', 'al wasl': 'Al-Wasl F.C.',
    'al quwa al jawiya': 'Al-Quwa Al-Jawiya',
}
_TEAM_EXTRA_OK = {
    'club', 'deportivo', 'deportes', 'fc', 'fk', 'cf', 'sc', 'ac', 'as', 'us', 'cd', 'ca',
    'bc', 'kk', 'hc', 'vc', 'u', 'bt', 'cs', 'kf', 'sk', 'bk', 'if', 'ff', 'ssv', 'vfl',
    'vfb', 'rb', 'afc', 'tsg', 'sp', 'sv', 'fcm', 'cfm',
}


def _safe_print(message):
    try:
        print(message)
    except Exception:
        try:
            print(str(message).encode('ascii', 'replace').decode('ascii'))
        except Exception:
            pass


def _sport_of(category, title):
    text = f"{category or ''} {title or ''}"
    for sport, rx in _SPORT_RULES:
        if rx.search(text):
            return sport
    return 'other'


def _norm_name(value):
    s = unicodedata.normalize('NFD', str(value or ''))
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def _clean_name(value):
    s = _EMOJI_RE.sub(' ', str(value or ''))
    s = re.sub(r'\([^)]*\)', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _league_prefix(title, category):
    text = _clean_name(title)
    if ':' in text:
        text = text.split(':', 1)[0]
    return text.strip(' -') or _clean_name(category)


def _event_sides(title):
    s = _clean_name(str(title or ''))
    s = re.sub(r'\s*\(\d{1,2}:\d{2}\)\s*$', '', s)
    if ':' in s:
        s = s.split(':')[-1].strip()
    parts = re.split(r'\s+(?:vs\.?|v)\s+', s, flags=re.I)
    out = []
    for part in parts:
        part = _clean_name(part.split('/')[0])
        if len(part) >= 3:
            out.append(part)
    return out if len(out) == 2 else []


def _reserve_aliases(name):
    m = re.match(r'^(.*?)\s+(II|2|B|U23|U-23|U21|U-21|Next Gen)$', str(name or ''), re.I)
    if not m:
        return []
    base = m.group(1).strip()
    out = []
    known = _RESERVE_ALIAS.get(_norm_name(base))
    if known:
        out.append(known)
    out.extend([base + ' Next Gen', base + ' U23', base + ' B'])
    return [x for x in dict.fromkeys(out) if _norm_name(x) != _norm_name(name)]


def _wiki_variants(name, sport):
    out = [name]
    has_club = re.search(r'(^|\s)(fc|f\.c\.|fk|cf|sc|ac|as|us|cd|ca|bc|kk|hc|vc|sk|bk)(\s|$)', name, re.I)
    for alias in _reserve_aliases(name):
        if alias not in out:
            out.append(alias)
    if not has_club:
        if sport == 'soccer':
            extras = [name + ' BC', 'FC ' + name, name + ' FC', name + ' Calcio', 'FK ' + name]
        elif sport == 'basketball':
            extras = ['BC ' + name, 'KK ' + name]
        elif sport == 'ice-hockey':
            extras = ['HC ' + name, name + ' HC']
        elif sport == 'volleyball':
            extras = ['VC ' + name]
        else:
            extras = []
        for extra in extras:
            extra = _clean_name(extra)
            if extra and extra not in out:
                out.append(extra)
    return out[:6]


def _title_matches(name, title, sport):
    q = [w for w in _norm_name(name).split() if len(w) >= 2]
    t = [w for w in _norm_name(re.sub(r'\([^)]*\)', ' ', str(title or ''))).split() if len(w) >= 2]
    if not q or not all(w in t for w in q):
        return False
    if sport not in _TSDB_SPORT:
        return True
    qs = set(q)
    return all(w in qs or w in _TEAM_EXTRA_OK for w in t)


def _wiki_accept(desc, sport):
    d = str(desc or '').lower()
    if not d:
        return False
    if re.search(r'disambiguation|city|town|village|municipality|river|company|album|film|song|genus|species', d):
        return False
    return bool((_WIKI_SPORT_RE.get(sport) or _WIKI_SPORT_RE['other']).search(d))


def _fetch_json(url, timeout=6):
    try:
        r = requests.get(url, headers={
            'User-Agent': LOGO_UA,
            'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _cached_logo(name, sport):
    key = (_norm_name(name), sport)
    if key not in _LOGO_CACHE:
        return None
    ttl = LOGO_TTL if _LOGO_CACHE[key] else LOGO_MISS_TTL
    if time.time() - _LOGO_TS.get(key, 0) > ttl:
        return None
    return _LOGO_CACHE[key]


def _store_logo(name, sport, url):
    key = (_norm_name(name), sport)
    _LOGO_CACHE[key] = url or ''
    _LOGO_TS[key] = time.time()


def _espn_team_logo(teams, name):
    q = [w for w in _norm_name(name).split() if len(w) >= 2]
    if not q:
        return ''
    for team in teams:
        tokens = set(_norm_name(team.get('n', '')).split())
        if all(w in tokens for w in q):
            return team.get('logo', '')
        if team.get('a') and _norm_name(team['a']) == _norm_name(name):
            return team.get('logo', '')
    return ''


def _espn_search_team(name):
    q = [w for w in _norm_name(name).split() if len(w) >= 2]
    if not q:
        return ''
    data = _fetch_json(_ESPN_SEARCH + urllib.parse.quote(name), 6)
    if not data:
        return ''
    tries = 0
    for result in data.get('results') or []:
        if result.get('type') != 'team':
            continue
        for item in result.get('contents') or []:
            if item.get('type') != 'team' or 'soccer' not in (item.get('sport') or '').lower():
                continue
            tokens = set(_norm_name(item.get('displayName', '')).split())
            if not all(w in tokens for w in q):
                continue
            m = re.search(r'~t:(\d+)', item.get('uid') or '')
            slug = item.get('defaultLeagueSlug')
            if not m or not slug:
                continue
            tries += 1
            if tries > 2:
                return ''
            detail = _fetch_json(
                'https://site.api.espn.com/apis/site/v2/sports/soccer/' +
                urllib.parse.quote(slug) + '/teams/' + m.group(1), 6
            )
            if detail:
                logos = (detail.get('team') or {}).get('logos') or []
                if logos and logos[0].get('href'):
                    return logos[0]['href']
    return ''


def _espn_league_teams(slug):
    cached = _ESPN_LEAGUE_CACHE.get(slug)
    if cached and time.time() - cached['ts'] < LOGO_TTL:
        return cached['teams']
    data = _fetch_json(
        'https://site.api.espn.com/apis/site/v2/sports/soccer/' + slug + '/teams?limit=500', 8
    )
    teams = []
    if data:
        leagues = ((data.get('sports') or [{}])[0].get('leagues') or [{}])
        for row in (leagues[0].get('teams') or []):
            team = row.get('team') or {}
            logo = ((team.get('logos') or [{}])[0].get('href') or '')
            if team.get('displayName') and logo:
                teams.append({'n': team['displayName'], 'a': team.get('abbreviation', ''), 'logo': logo})
    if teams:
        _ESPN_LEAGUE_CACHE[slug] = {'ts': time.time(), 'teams': teams}
    return teams


def _espn_college_teams(key):
    cached = _ESPN_CACHE.get(key)
    if cached and time.time() - cached['ts'] < LOGO_TTL:
        return cached['teams']
    data = _fetch_json(_ESPN_COLLEGE[key], 8)
    teams = []
    if data:
        leagues = ((data.get('sports') or [{}])[0].get('leagues') or [{}])
        for row in (leagues[0].get('teams') or []):
            team = row.get('team') or {}
            logo = ((team.get('logos') or [{}])[0].get('href') or '')
            if team.get('displayName') and logo:
                teams.append({'n': team['displayName'], 'a': team.get('abbreviation', ''), 'logo': logo})
    if teams:
        _ESPN_CACHE[key] = {'ts': time.time(), 'teams': teams}
    return teams


def _lev(a, b):
    if a == b:
        return 0
    m, n = len(a), len(b)
    d = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        d[i][0] = i
    for j in range(n + 1):
        d[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    return d[m][n]


def _scores365_match(qname, cname):
    q = [w for w in _norm_name(qname).split() if len(w) >= 2 and w not in _SCORES365_STOP]
    t = [w for w in _norm_name(cname).split() if len(w) >= 2 and w not in _SCORES365_STOP]
    if not q or not t:
        return False
    ts = set(t)
    return all(any(w == x or (len(w) >= 4 and len(x) >= 4 and _lev(w, x) <= 1) for x in t) for w in q)


def _scores365_logo(name, sport):
    sid = 3 if sport == 'tennis' else 1
    q = [w for w in _norm_name(name).split() if len(w) >= 2]
    if not q:
        return ''
    data = _fetch_json(_SCORES365_SEARCH + urllib.parse.quote(_norm_name(name)), 6)
    if not data:
        return ''
    candidates = []
    for competitor in data.get('competitors') or []:
        if not competitor or competitor.get('sportId') != sid or not competitor.get('name'):
            continue
        tokens = set(w for w in _norm_name(competitor['name']).split() if len(w) >= 2)
        if not all(w in tokens for w in q):
            continue
        candidates.append((len(tokens) - len(q), len(str(competitor['name'])), competitor.get('id')))
    candidates.sort()
    for _, _, cid in candidates[:3]:
        if cid:
            return _SCORES365_IMG + str(cid)
    return ''


def _scores365_competition_teams(cid):
    cached = _SCORES365_COMP_CACHE.get(cid)
    if cached and time.time() - cached['ts'] < LOGO_TTL:
        return cached['teams']
    data = _fetch_json(
        'https://webws.365scores.com/web/standings/?appTypeId=5&langId=1&competitions=' +
        str(cid) + '&live=false&withSeasons=true', 8
    )
    teams = []
    if data:
        for standing in data.get('standings') or []:
            rows = standing.get('rows') or []
            if not rows and standing.get('competitor'):
                rows = [standing]
            for row in rows:
                competitor = row.get('competitor') or {}
                if competitor.get('id') and competitor.get('name'):
                    teams.append({'n': competitor['name'], 'logo': _SCORES365_IMG + str(competitor['id'])})
    if teams:
        _SCORES365_COMP_CACHE[cid] = {'ts': time.time(), 'teams': teams}
    return teams


def _wiki_search_one(name, sport):
    global _WIKI_BACKOFF
    if time.time() < _WIKI_BACKOFF:
        return ''
    query = urllib.parse.quote('intitle:"' + str(name).replace('"', '') + '"')
    data = _fetch_json(_WIKI_SEARCH_API + query + '&gsrlimit=8', 6)
    if not data:
        return ''
    if data.get('error'):
        if re.search(r'maxlag|ratelimited|too many', json.dumps(data['error']), re.I):
            _WIKI_BACKOFF = time.time() + 120
        return ''
    pages = sorted((data.get('query', {}).get('pages') or {}).values(),
                   key=lambda p: p.get('index', 0))
    for page in pages:
        thumb = page.get('thumbnail') or {}
        src = thumb.get('source')
        if not src:
            continue
        if not _wiki_accept(page.get('description', ''), sport):
            continue
        if not _title_matches(name, page.get('title', ''), sport):
            continue
        return src
    return ''


def _wiki_search_name(name, sport):
    for query_name in [name] + _reserve_aliases(name):
        found = _wiki_search_one(query_name, sport)
        if found:
            return found
    return ''


def _wiki_pages(titles):
    global _WIKI_BACKOFF
    pages, done = {}, set()
    if not titles or time.time() < _WIKI_BACKOFF:
        return pages, done
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        url = _WIKI_API + '&titles=' + urllib.parse.quote('|'.join(batch))
        data = _fetch_json(url, 6)
        if not data:
            continue
        if data.get('error'):
            if re.search(r'maxlag|ratelimited|too many', json.dumps(data['error']), re.I):
                _WIKI_BACKOFF = time.time() + 120
            continue
        by_title = {}
        for page in (data.get('query', {}).get('pages') or {}).values():
            by_title[page.get('title', '')] = page
        alias = {}
        for row in data.get('query', {}).get('normalized') or []:
            alias[row.get('from')] = row.get('to')
        for row in data.get('query', {}).get('redirects') or []:
            alias[row.get('from')] = row.get('to')
        for title in batch:
            done.add(title)
            current = title
            for _ in range(3):
                nxt = alias.get(current)
                if not nxt or nxt == current:
                    break
                current = nxt
            page = by_title.get(current)
            if page and (page.get('thumbnail') or {}).get('source'):
                pages[title] = page
    return pages, done


def _tsdb_team(query, name, sport):
    global _TSDB_BACKOFF
    if time.time() < _TSDB_BACKOFF or sport not in _TSDB_SPORT:
        return ''
    data = _fetch_json(_TSDB_API + urllib.parse.quote(query), 5)
    if not data:
        return ''
    q = [w for w in _norm_name(name).split() if len(w) >= 2]
    for team in data.get('teams') or []:
        if not team or not team.get('strTeam'):
            continue
        if not _TSDB_SPORT[sport].search(team.get('strSport', '')):
            continue
        tokens = set(_norm_name(team['strTeam']).split())
        if q and not all(w in tokens for w in q):
            continue
        return team.get('strBadge') or team.get('strBanner') or team.get('strFanart1') or ''
    return ''


def _tsdb_team_variants(name, sport):
    exact = _tsdb_team(name, name, sport)
    if exact:
        return exact
    prefixes = {'soccer': ['FK ', 'FC '], 'basketball': ['KK ', 'BC '],
                'ice-hockey': ['HC '], 'volleyball': ['VC ']}.get(sport, [])
    for prefix in prefixes:
        found = _tsdb_team(prefix + name, name, sport)
        if found:
            return found
    return ''


def _tsdb_player(name, sport):
    global _TSDB_BACKOFF
    if time.time() < _TSDB_BACKOFF or sport not in _TSDB_PLAYER_SPORT:
        return ''
    data = _fetch_json(
        'https://www.thesportsdb.com/api/v1/json/' + _TSDB_KEY +
        '/searchplayers.php?p=' + urllib.parse.quote(name), 5
    )
    if not data:
        return ''
    q = [w for w in _norm_name(name).split() if len(w) >= 2]
    for player in data.get('player') or []:
        if not player or not player.get('strPlayer'):
            continue
        if not _TSDB_PLAYER_SPORT[sport].search(player.get('strSport', '')):
            continue
        tokens = set(_norm_name(player['strPlayer']).split())
        if q and not all(w in tokens for w in q):
            continue
        img = player.get('strCutout') or player.get('strThumb') or ''
        if img:
            return img
        return _wiki_search_name(player['strPlayer'], sport)
    return ''


def resolve_team_logo(team_name, event_name='', category=''):
    name = _clean_name(team_name)
    if len(name) < 2:
        return ''
    sport = _sport_of(category, event_name)
    cached = _cached_logo(name, sport)
    if cached is not None:
        return cached

    logo = ''
    if sport == 'soccer':
        logo = _espn_search_team(name)
    if not logo and sport in ('soccer', 'tennis'):
        logo = _scores365_logo(name, sport)
    if not logo:
        logo = _wiki_search_name(name, sport)
    if not logo:
        if sport in _TSDB_SPORT:
            logo = _tsdb_team_variants(name, sport)
        else:
            logo = _tsdb_player(name, sport)

    _store_logo(name, sport, logo)
    if logo:
        _safe_print(f"[logos] {name} ({sport}) -> {logo[:100]}")
    return logo


def _fill_scores365(events):
    groups = {}
    for ev in events:
        cid = _SCORES365_COMPETITIONS.get(_norm_name(_league_prefix(ev.get('t', ''), ev.get('c', ''))))
        if cid:
            groups.setdefault(cid, []).append(ev)
    for cid, evs in groups.items():
        teams = _scores365_competition_teams(cid)
        if not teams:
            continue
        for ev in evs:
            sport = _sport_of(ev.get('c', ''), ev.get('t', ''))
            for side in _event_sides(ev.get('t', '')):
                if _cached_logo(side, sport):
                    continue
                candidates = [t for t in teams if _scores365_match(side, t['n'])]
                if candidates:
                    candidates.sort(key=lambda t: len(_norm_name(t['n']).split()))
                    _store_logo(side, sport, candidates[0]['logo'])


def _fill_espn_leagues(events):
    groups = {}
    for ev in events:
        slug = _ESPN_LEAGUES.get(_norm_name(_league_prefix(ev.get('t', ''), ev.get('c', ''))))
        if slug:
            groups.setdefault(slug, []).append(ev)
    for slug, evs in groups.items():
        teams = _espn_league_teams(slug)
        if not teams:
            continue
        for ev in evs:
            sport = _sport_of(ev.get('c', ''), ev.get('t', ''))
            for side in _event_sides(ev.get('t', '')):
                if _cached_logo(side, sport):
                    continue
                logo = _espn_team_logo(teams, side)
                if logo:
                    _store_logo(side, sport, logo)


def _fill_espn_college(events):
    college = [ev for ev in events if 'college basketball' in
               ((ev.get('c') or '') + ' ' + (ev.get('t') or '')).lower()]
    if not college:
        return
    for key in {('women' if 'women' in (ev.get('c', '') + ' ' + ev.get('t', '')).lower() else 'men')
                for ev in college}:
        teams = _espn_college_teams(key)
        if not teams:
            continue
        for ev in college:
            ev_key = 'women' if 'women' in (ev.get('c', '') + ' ' + ev.get('t', '')).lower() else 'men'
            if ev_key != key:
                continue
            sport = _sport_of(ev.get('c', ''), ev.get('t', ''))
            for side in _event_sides(ev.get('t', '')):
                if _cached_logo(side, sport):
                    continue
                logo = _espn_team_logo(teams, side)
                if logo:
                    _store_logo(side, sport, logo)


def _fill_wiki(events):
    names = {}
    for ev in events:
        sport = _sport_of(ev.get('c', ''), ev.get('t', ''))
        for side in _event_sides(ev.get('t', '')):
            if not _cached_logo(side, sport):
                names.setdefault(side, sport)
    if not names:
        return

    titles, title_name = [], {}
    for name in names:
        alias = _PRIORITY_ALIASES.get(_norm_name(name))
        if alias and alias not in title_name:
            title_name[alias] = name
            titles.append(alias)
    for name in names:
        for alias in _reserve_aliases(name):
            if len(titles) >= 400:
                break
            if alias not in title_name:
                title_name[alias] = name
                titles.append(alias)
    for name in names:
        if names[name] != 'soccer':
            continue
        for alias in [name + ' BC', name + ' Calcio']:
            if len(titles) >= 400:
                break
            if alias not in title_name:
                title_name[alias] = name
                titles.append(alias)
    for name in names:
        if len(titles) >= 400:
            break
        if name not in title_name:
            title_name[name] = name
            titles.append(name)
    for name in names:
        for alias in _wiki_variants(name, names[name]):
            if len(titles) >= 400:
                break
            if alias not in title_name:
                title_name[alias] = name
                titles.append(alias)

    pages, done = _wiki_pages(titles)
    for title, name in title_name.items():
        if title not in done:
            continue
        sport = names[name]
        if _cached_logo(name, sport):
            continue
        page = pages.get(title)
        if page and _wiki_accept(page.get('description', ''), sport):
            _store_logo(name, sport, page['thumbnail']['source'])


def prefetch_logos(events):
    fresh = []
    for ev in events:
        title = _clean_name(ev.get('t', ''))
        if not title or title in _PREFETCHED:
            continue
        _PREFETCHED.add(title)
        fresh.append(ev)
    if not fresh:
        return
    _safe_print(f"[logos] prefetch {len(fresh)} eventi...")
    for fill in (_fill_scores365, _fill_espn_leagues, _fill_espn_college, _fill_wiki):
        try:
            fill(fresh)
        except Exception as e:
            _safe_print(f"[!] Prefetch {fill.__name__}: {e}")
    _safe_print(f"[logos] cache dopo prefetch: {sum(1 for v in _LOGO_CACHE.values() if v)} loghi")


def prefetch_logos_for_channels(categorized_channels):
    try:
        migrate_logo_filenames()
    except Exception as e:
        _safe_print(f"[!] Migrazione nomi loghi fallita: {e}")
    events, seen = [], set()
    for category, channels in categorized_channels.items():
        for ch in channels:
            title = re.sub(r'\s*\(\d{1,2}:\d{2}\)\s*$', '', ch.get('event_title', ''))
            if title and title not in seen:
                seen.add(title)
                events.append({'t': title, 'c': category})
    try:
        prefetch_logos(events)
    except Exception as e:
        _safe_print(f"[!] Prefetch loghi fallito: {e}")

# ---------- end multi-source logo resolver ----------

def headers_to_extvlcopt(headers):
    """Converte un dizionario di header in una lista di stringhe #EXTVLCOPT per VLC."""
    vlc_opts = []
    for key, value in headers.items():
        # VLC usa nomi di header in minuscolo
        vlc_opts.append(f'#EXTVLCOPT:http-{key.lower()}={value}')
    return vlc_opts

# Funzione per il secondo script (epg_merger.py)
def epg_merger():
    # Codice del secondo script qui
    # Aggiungi il codice del tuo script "epg_merger.py" in questa funzione.
    # Ad esempio:
    print("Eseguendo l'epg_merger.py...")
    # Il codice che avevi nello script "epg_merger.py" va qui, senza modifiche.
    import requests
    import gzip
    import os
    import xml.etree.ElementTree as ET
    import io

    # URL dei file GZIP o XML da elaborare
    urls_gzip = [
        'https://www.open-epg.com/files/italy1.xml',
        'https://www.open-epg.com/files/italy2.xml',
        'https://www.open-epg.com/files/italy3.xml',
        'https://www.open-epg.com/files/italy4.xml',
        'https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz'
    ]

    # File di output
    output_xml = os.path.join(output_dir, 'epg.xml')

    # URL remoto di it.xml
    url_it = 'https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/PlutoTV/it.xml'

    # Disabilita gli avvisi per le richieste non verificate
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

    # File eventi_dlhd locale
    path_eventi_dlhd = os.path.join(output_dir, 'eventi_dlhd.xml')

    def download_and_parse_xml(url):
        """Scarica un file .xml o .gzip e restituisce l'ElementTree."""
        try:
            # Aggiunto verify=False per ignorare gli errori SSL
            response = requests.get(url, timeout=30, verify=False)
            response.raise_for_status()

            # Prova a decomprimere come GZIP
            try:
                with gzip.open(io.BytesIO(response.content), 'rb') as f_in:
                    xml_content = f_in.read()
            except (gzip.BadGzipFile, OSError):
                # Non Ã¨ un file gzip, usa direttamente il contenuto
                xml_content = response.content

            return ET.ElementTree(ET.fromstring(xml_content))
        except requests.exceptions.RequestException as e:
            print(f"Errore durante il download da {url} (verifica SSL disabilitata): {e}")
        except ET.ParseError as e:
            print(f"Errore nel parsing del file XML da {url}: {e}")
        return None

    # Creare un unico XML vuoto
    root_finale = ET.Element('tv')
    tree_finale = ET.ElementTree(root_finale)

    # Processare ogni URL
    for url in urls_gzip:
        tree = download_and_parse_xml(url)
        if tree is not None:
            root = tree.getroot()
            for element in root:
                root_finale.append(element)

    # Check CANALI_DADDY flag before processing eventi_dlhd.xml
    canali_daddy_flag = os.getenv("CANALI_DADDY", "no").strip().lower()
    if canali_daddy_flag == "si":
        # Aggiungere eventi_dlhd.xml da file locale
        if os.path.exists(path_eventi_dlhd):
            try:
                tree_eventi_dlhd = ET.parse(path_eventi_dlhd)
                root_eventi_dlhd = tree_eventi_dlhd.getroot()
                for programme in root_eventi_dlhd.findall(".//programme"):
                    root_finale.append(programme)
            except ET.ParseError as e:
                print(f"Errore nel parsing del file eventi_dlhd.xml: {e}")
        else:
            print(f"File non trovato: {path_eventi_dlhd}")
    else:
        print("[INFO] Skipping eventi_dlhd.xml in epg_merger as CANALI_DADDY is not 'si'.")

    # Aggiungere it.xml da URL remoto
    tree_it = download_and_parse_xml(url_it)
    if tree_it is not None:
        root_it = tree_it.getroot()
        for programme in root_it.findall(".//programme"):
            root_finale.append(programme)
    else:
        print(f"Impossibile scaricare o analizzare il file it.xml da {url_it}")

    # Funzione per pulire attributi
    def clean_attribute(element, attr_name):
        if attr_name in element.attrib:
            old_value = element.attrib[attr_name]
            new_value = old_value.replace(" ", "").lower()
            element.attrib[attr_name] = new_value

    # Pulire gli ID dei canali
    for channel in root_finale.findall(".//channel"):
        clean_attribute(channel, 'id')

    # Pulire gli attributi 'channel' nei programmi
    for programme in root_finale.findall(".//programme"):
        clean_attribute(programme, 'channel')

    # Salvare il file XML finale
    with open(output_xml, 'wb') as f_out:
        tree_finale.write(f_out, encoding='utf-8', xml_declaration=True)
    print(f"File XML salvato: {output_xml}")

    # Salvare anche il file GZIP
    output_gz = os.path.join(output_dir, 'epg.xml.gz')
    with gzip.open(output_gz, 'wb') as f_gz:
        tree_finale.write(f_gz, encoding='utf-8', xml_declaration=True)
    print(f"File GZIP salvato: {output_gz}")
             
# Funzione per il terzo script (eventi_dlhd_m3u8_generator.py)
def eventi_dlhd_m3u8_generator_world():
    # Codice del terzo script qui
    # Aggiungi il codice del tuo script "eventi_dlhd_m3u8_generator.py" in questa funzione.
    print("Eseguendo l'eventi_dlhd_m3u8_generator.py...")
    # Il codice che avevi nello script "eventi_dlhd_m3u8_generator.py" va qui, senza modifiche.
    import json
    import re
    import requests
    import urllib.parse # Consolidato
    from datetime import datetime, timedelta
    from dateutil import parser
    import os
    from dotenv import load_dotenv
    from PIL import Image, ImageDraw, ImageFont
    import io # Aggiunto per encoding URL
    import time
    
    # Carica le variabili d'ambiente dal file .env
    load_dotenv()

    LINK_DADDY = os.getenv("LINK_DADDY", "").strip() or "https://dlive.sx"
    JSON_FILE = os.path.join(script_dir, "daddyliveSchedule.json")
    OUTPUT_FILE = os.path.join(output_dir, "eventi_dlhd.m3u")
    HEADERS = { 
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36" 
    } 
     
    HTTP_TIMEOUT = 10 
    session = requests.Session() 
    session.headers.update(HEADERS) 
    # Definisci current_time e three_hours_in_seconds per la logica di caching
    current_time = time.time()
    three_hours_in_seconds = 3 * 60 * 60
    
    def clean_category_name(name): 
        # Rimuove tag html come </span> o simili 
        return re.sub(r'<[^>]+>', '', name).strip()
        
    def clean_tvg_id(tvg_id):
        """
        Pulisce il tvg-id rimuovendo caratteri speciali, spazi e convertendo tutto in minuscolo
        """
        # import re # 're' Ã¨ giÃ  importato a livello di funzione
        # Rimuove caratteri speciali comuni mantenendo solo lettere e numeri
        cleaned = re.sub(r'[^a-zA-Z0-9À-ÿ]', '', tvg_id)
        return cleaned.lower()
     
    def search_logo_for_event(event_name, category="", time_str=""): 
        """ 
        Cerca un logo per l'evento specificato utilizzando un motore di ricerca 
        Restituisce l'URL dell'immagine trovata o None se non trovata 
        """ 
        try: 
            # Rimuovi eventuali riferimenti all'orario dal nome dell'evento
            # Cerca pattern come "Team A vs Team B (20:00)" e rimuovi la parte dell'orario
            clean_event_name = re.sub(r'\s*\(\d{1,2}:\d{2}\)\s*$', '', event_name)
            # Se c'ÃÂ¨ un ':', prendi solo la parte dopo
            if ':' in clean_event_name:
                clean_event_name = clean_event_name.split(':', 1)[1].strip()
            
            # Verifica se l'evento contiene "vs" o "-" per identificare le due squadre
            teams = None
            if " vs " in clean_event_name:
                teams = clean_event_name.split(" vs ")
            elif " VS " in clean_event_name:
                teams = clean_event_name.split(" VS ")
            elif " VS. " in clean_event_name:
                teams = clean_event_name.split(" VS. ")
            elif " vs. " in clean_event_name:
                teams = clean_event_name.split(" vs. ")
            
            # Se abbiamo identificato due squadre, cerchiamo i loghi separatamente
            if teams and len(teams) == 2:
                team1 = teams[0].strip()
                team2 = teams[1].strip()

                cached_url, _ = cached_poster_url(team1, team2)
                if cached_url:
                    print(f"[✓] Poster esistente: {cached_url}")
                    return cached_url
                
                print(f"[🔍] Ricerca logo per Team 1: {team1}")
                logo1_url = search_team_logo(team1, event_name, category)
                
                print(f"[🔍] Ricerca logo per Team 2: {team2}")
                logo2_url = search_team_logo(team2, event_name, category)
                
                # Se abbiamo trovato entrambi i loghi, creiamo un'immagine combinata
                if logo1_url and logo2_url:
                    # Scarica i loghi e l'immagine VS
                    try:
                        from os.path import exists, getmtime
                        
                        # Crea la cartella logos se non esiste
                        logos_dir = os.path.join(output_dir, "logos")
                        os.makedirs(logos_dir, exist_ok=True)
                        
                        # Verifica se l'immagine combinata esiste giÃÂ  e non ÃÂ¨ obsoleta
                        relative_logo_path = poster_relative_path(team1, team2)
                        absolute_output_filename = os.path.join(output_dir, relative_logo_path)
                        if exists(absolute_output_filename):
                            file_age = current_time - os.path.getmtime(absolute_output_filename)
                            if file_age <= three_hours_in_seconds and poster_is_current(absolute_output_filename):
                                print(f"[✓] Utilizzo immagine combinata esistente: {absolute_output_filename}")
                                
                                # Carica le variabili d'ambiente per GitHub
                                NOMEREPO = os.getenv("NOMEREPO", "").strip()
                                NOMEGITHUB = os.getenv("NOMEGITHUB", "").strip()
                                
                                # Se le variabili GitHub sono disponibili, restituisci l'URL raw di GitHub
                                if NOMEGITHUB and NOMEREPO:
                                    github_raw_url = f"https://raw.githubusercontent.com/{NOMEGITHUB}/{NOMEREPO}/main/{urllib.parse.quote(relative_logo_path.replace(os.sep, '/'), safe='/')}"
                                    print(f"[✓] URL GitHub generato per logo esistente: {github_raw_url}")
                                    return github_raw_url
                                else:
                                    # Altrimenti restituisci il percorso locale
                                    return absolute_output_filename
                        
                        # Scarica i loghi
                        img1, img2 = None, None
                        
                        if logo1_url:
                            try:
                                # Aggiungi un User-Agent simile a un browser
                                logo_headers = {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                                }
                                response1 = requests.get(logo1_url, headers=logo_headers, timeout=10)
                                response1.raise_for_status() # Controlla errori HTTP
                                if 'image' in response1.headers.get('Content-Type', '').lower():
                                    img1 = Image.open(io.BytesIO(response1.content))
                                    print(f"[✓] Logo1 scaricato con successo da: {logo1_url}")
                                else:
                                    print(f"[!] URL logo1 ({logo1_url}) non è un'immagine (Content-Type: {response1.headers.get('Content-Type')}).")
                                    logo1_url = None # Invalida URL se non è un'immagine
                            except requests.exceptions.RequestException as e_req:
                                print(f"[!] Errore scaricando logo1 ({logo1_url}): {e_req}")
                                logo1_url = None
                            except Exception as e_pil: # Errore specifico da PIL durante Image.open
                                print(f"[!] Errore PIL aprendo logo1 ({logo1_url}): {e_pil}")
                                logo1_url = None
                        
                        if logo2_url:
                            try:
                                # Aggiungi un User-Agent simile a un browser
                                logo_headers = {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                                }
                                response2 = requests.get(logo2_url, headers=logo_headers, timeout=10)
                                response2.raise_for_status() # Controlla errori HTTP
                                if 'image' in response2.headers.get('Content-Type', '').lower():
                                    img2 = Image.open(io.BytesIO(response2.content))
                                    print(f"[✓] Logo2 scaricato con successo da: {logo2_url}")
                                else:
                                    print(f"[!] URL logo2 ({logo2_url}) non è un'immagine (Content-Type: {response2.headers.get('Content-Type')}).")
                                    logo2_url = None # Invalida URL se non è un'immagine
                            except requests.exceptions.RequestException as e_req:
                                print(f"[!] Errore scaricando logo2 ({logo2_url}): {e_req}")
                                logo2_url = None
                            except Exception as e_pil: # Errore specifico da PIL durante Image.open
                                print(f"[!] Errore PIL aprendo logo2 ({logo2_url}): {e_pil}")
                                logo2_url = None
                        # Procedi con la combinazione solo se entrambi i loghi sono stati caricati con successo
                        if not (img1 and img2):
                            print(f"[!] Impossibile caricare entrambi i loghi come immagini valide per la combinazione. Logo1 caricato: {bool(img1)}, Logo2 caricato: {bool(img2)}.")
                            raise ValueError("Uno o entrambi i loghi non sono stati caricati correttamente.") # Questo forzerÃ  l'except sottostante
                        
                        create_event_poster(team1, team2, img1, img2, event_name, category, time_str, absolute_output_filename)
                        
                        print(f"[✓] Immagine combinata creata: {absolute_output_filename}")
                        
                        # Carica le variabili d'ambiente per GitHub
                        NOMEREPO = os.getenv("NOMEREPO", "").strip()
                        NOMEGITHUB = os.getenv("NOMEGITHUB", "").strip()
                        
                        # Se le variabili GitHub sono disponibili, restituisci l'URL raw di GitHub
                        if NOMEGITHUB and NOMEREPO:
                            github_raw_url = f"https://raw.githubusercontent.com/{NOMEGITHUB}/{NOMEREPO}/main/{urllib.parse.quote(relative_logo_path.replace(os.sep, '/'), safe='/')}"
                            print(f"[✓] URL GitHub generato: {github_raw_url}")
                            return github_raw_url
                        else:
                            # Altrimenti restituisci il percorso assoluto
                            return absolute_output_filename
                        
                    except Exception as e:
                        print(f"[!] Errore nella creazione dell'immagine combinata: {e}")
                        # Se fallisce, restituisci solo il primo logo trovato
                        return logo1_url or logo2_url
                
                # Se non abbiamo trovato entrambi i loghi, restituisci quello che abbiamo
                return logo1_url or logo2_url
                    
        except Exception as e: 
            print(f"[!] Errore nella ricerca del logo per '{event_name}': {e}") 
        
        # Se non troviamo nulla, restituiamo None 
        return None

    def search_team_logo(team_name, event_name="", category=""):
        try:
            return resolve_team_logo(team_name, event_name, category)
        except Exception as e:
            _safe_print(f"[!] Resolver loghi fallito per '{team_name}': {e}")
            return None

    def get_stream_from_channel_id(channel_id): 
        # Restituisce direttamente l'URL .php
        embed_url = f"{LINK_DADDY}/watch.php?id={channel_id}" 
        print(f"URL .php per il canale Daddylive {channel_id}.")
        return embed_url
     
    # def clean_category_name(name): # Rimossa definizione duplicata
    #     # Rimuove tag html come </span> o simili
    #     return re.sub(r'<[^>]+>', '', name).strip()
     
    def extract_channels_from_json(path): 
        keywords = {"italy", "rai", "italia", "it", "uk", "tnt", "usa", "tennis channel", "tennis stream", "la"} 
        now = datetime.now()  # ora attuale completa (data+ora) 
     
        with open(path, "r", encoding="utf-8") as f: 
            data = json.load(f) 
     
        categorized_channels = {} 
     
        for date_key, sections in data.items(): 
            date_part = date_key.split(" - ")[0] 
            try: 
                date_obj = parser.parse(date_part, fuzzy=True).date() 
            except Exception as e: 
                print(f"[!] Errore parsing data '{date_part}': {e}") 
                continue 
            
            # Il titolo della pagina puo' restare indietro di un giorno: il blocco
            # valido e' comunque quello corrente, quindi si usa la data piu' recente.
            block_date = max(date_obj, now.date())
     
            for category_raw, event_items in sections.items(): 
                category = clean_category_name(category_raw)
                # Salta la categoria TV Shows
                if category.lower() == "tv shows":
                    continue
                if category not in categorized_channels: 
                    categorized_channels[category] = [] 
     
                for item in event_items: 
                    time_str = item.get("time", "00:00") # Orario originale dal JSON
                    event_title = item.get("event", "Evento") 
     
                    try: 
                        # Parse orario evento originale (dal JSON)
                        original_event_time_obj = datetime.strptime(time_str, "%H:%M").time()

                        # Gli orari prima delle 05:00 (ora UK) appartengono al mattino
                        # successivo: lo "sports day" della pagina parte alle 05:00.
                        event_date_obj = block_date + timedelta(days=1) if original_event_time_obj.hour < 5 else block_date
                        event_datetime = datetime.combine(event_date_obj, original_event_time_obj)

                        # Includi se l'evento e' iniziato da meno di 2 ore (o e' futuro)
                        if now - event_datetime > timedelta(hours=2):
                            continue

                        time_formatted = event_datetime.strftime("%H:%M")
                    except Exception as e_time:
                        print(f"[!] Errore parsing orario '{time_str}' per evento '{event_title}' in data '{date_key}': {e_time}")
                        time_formatted = time_str # Fallback
     
                    for ch in item.get("channels", []): 
                        channel_name = ch.get("channel_name", "") 
                        channel_id = ch.get("channel_id", "") 
     
                        words = set(re.findall(r'\b\w+\b', channel_name.lower())) 
                        if keywords.intersection(words): 
                            tvg_name = f"{event_title} ({time_formatted})" 
                            categorized_channels[category].append({ 
                                "tvg_name": tvg_name, 
                                "channel_name": channel_name, 
                                "channel_id": channel_id,
                                "event_title": event_title  # Aggiungiamo il titolo dell'evento per la ricerca del logo
                            }) 
     
        return categorized_channels 
     
    def generate_m3u_from_schedule(json_file, output_file): 
        categorized_channels = extract_channels_from_json(json_file) 
        prefetch_logos_for_channels(categorized_channels)

        with open(output_file, "w", encoding="utf-8") as f: 
            f.write("#EXTM3U\n") 

            # Controlla se ci sono eventi_dlhd prima di aggiungere il canale DADDYLIVE
            has_events = any(channels for channels in categorized_channels.values())
            
            if has_events:
                # Aggiungi il canale iniziale/informativo solo se ci sono eventi_dlhd
                f.write(f'#EXTINF:-1 tvg-name="DADDYLIVE" group-title="Eventi Live DLHD",DADDYLIVE\n')
                f.write("https://example.com.m3u8\n\n")
            else:
                print("[ℹ️] Nessun evento trovato, canale DADDYLIVE non aggiunto.")

            for category, channels in categorized_channels.items(): 
                if not channels: 
                    continue 
          
                for ch in channels: 
                    tvg_name = ch["tvg_name"] 
                    channel_id = ch["channel_id"] 
                    event_title = ch["event_title"]  # Otteniamo il titolo dell'evento
                    channel_name = ch["channel_name"]
                    
                    # Cerca un logo per questo evento
                    # Rimuovi l'orario dal titolo dell'evento prima di cercare il logo
                    clean_event_title = re.sub(r'\s*\(\d{1,2}:\d{2}\)\s*$', '', event_title)
                    print(f"[🔍] Ricerca logo per: {clean_event_title}") 
                    logo_url = search_logo_for_event(clean_event_title, category=category)
                    logo_attribute = f' tvg-logo="{logo_url}"' if logo_url else ''
     
                    try: 
                        # Cerca lo stream .m3u8 nei siti specificati
                        stream = get_stream_from_channel_id(channel_id)
                                                    
                        if stream: 
                            cleaned_event_id = clean_tvg_id(event_title) # Usa event_title per tvg-id
                            f.write(f'#EXTINF:-1 tvg-id="{cleaned_event_id}" tvg-name="{category} | {tvg_name}"{logo_attribute} group-title="Eventi Live DLHD",{category} | {tvg_name}\n')
                            # Aggiungi EXTHTTP headers per canali daddy (esclusi .php)
                            if "ava.karmakurama.com" in stream and not stream.endswith('.php'):
                                daddy_headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1", "Referrer": "https://ava.karmakurama.com/", "Origin": "https://ava.karmakurama.com"}
                                vlc_opt_lines = headers_to_extvlcopt(daddy_headers)
                                for line in vlc_opt_lines:
                                    f.write(f'{line}\n')
                            f.write(f'{stream}\n\n')
                            print(f"[✓] {tvg_name}" + (f" (logo trovato)" if logo_url else " (nessun logo trovato)")) 
                        else: 
                            print(f"[✗] {tvg_name} - Nessuno stream trovato") 
                    except Exception as e: 
                        print(f"[!] Errore su {tvg_name}: {e}") 
     
    # Esegui la generazione quando la funzione viene chiamata
    generate_m3u_from_schedule(JSON_FILE, OUTPUT_FILE)

# Funzione per il terzo script (eventi_dlhd_m3u8_generator.py)
def eventi_dlhd_m3u8_generator():
    # Codice del terzo script qui
    # Aggiungi il codice del tuo script "eventi_dlhd_m3u8_generator.py" in questa funzione.
    print("Eseguendo l'eventi_dlhd_m3u8_generator.py...")
    # Il codice che avevi nello script "eventi_dlhd_m3u8_generator.py" va qui, senza modifiche.
    import json 
    import re 
    import requests 
    from urllib.parse import quote 
    from datetime import datetime, timedelta 
    from dateutil import parser 
    import urllib.parse
    import os
    from dotenv import load_dotenv
    from PIL import Image, ImageDraw, ImageFont
    import io
    import urllib.parse # Aggiunto per encoding URL
    import time

    # Carica le variabili d'ambiente dal file .env
    load_dotenv()
    LINK_DADDY = os.getenv("LINK_DADDY", "").strip() or "https://dlive.sx"
    JSON_FILE = os.path.join(script_dir, "daddyliveSchedule.json") # Cache in scripts
    OUTPUT_FILE = os.path.join(output_dir, "eventi_dlhd.m3u") # Output in main dir
     
    HEADERS = { 
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36" 
    } 
     
    HTTP_TIMEOUT = 10 
    session = requests.Session() 
    session.headers.update(HEADERS) 
    # Definisci current_time e three_hours_in_seconds per la logica di caching
    current_time = time.time()
    three_hours_in_seconds = 3 * 60 * 60
    
    def clean_category_name(name): 
        # Rimuove tag html come </span> o simili 
        return re.sub(r'<[^>]+>', '', name).strip()
        
    def clean_tvg_id(tvg_id):
        """
        Pulisce il tvg-id rimuovendo caratteri speciali, spazi e convertendo tutto in minuscolo.
        """
        import re
        # Rimuove caratteri speciali comuni mantenendo solo lettere e numeri
        cleaned = re.sub(r'[^a-zA-Z0-9À-ÿ]', '', tvg_id)
        return cleaned.lower()
     
    def search_logo_for_event(event_name, category="", time_str=""): 
        """ 
        Cerca un logo per l'evento specificato utilizzando un motore di ricerca 
        Restituisce l'URL dell'immagine trovata o None se non trovata 
        """ 
        try: 
            # Rimuovi eventuali riferimenti all'orario dal nome dell'evento
            # Cerca pattern come "Team A vs Team B (20:00)" e rimuovi la parte dell'orario
            clean_event_name = re.sub(r'\s*\(\d{1,2}:\d{2}\)\s*$', '', event_name)
            # Se c'è un ':', prendi solo la parte dopo
            if ':' in clean_event_name:
                clean_event_name = clean_event_name.split(':', 1)[1].strip()
            
            # Verifica se l'evento contiene "vs" o "-" per identificare le due squadre
            teams = None
            if " vs " in clean_event_name:
                teams = clean_event_name.split(" vs ")
            elif " VS " in clean_event_name:
                teams = clean_event_name.split(" VS ")
            elif " VS. " in clean_event_name:
                teams = clean_event_name.split(" VS. ")
            elif " vs. " in clean_event_name:
                teams = clean_event_name.split(" vs. ")
            
            # Se abbiamo identificato due squadre, cerchiamo i loghi separatamente
            if teams and len(teams) == 2:
                team1 = teams[0].strip()
                team2 = teams[1].strip()

                cached_url, _ = cached_poster_url(team1, team2)
                if cached_url:
                    print(f"[✓] Poster esistente: {cached_url}")
                    return cached_url
                
                print(f"[🔍] Ricerca logo per Team 1: {team1}")
                logo1_url = search_team_logo(team1, event_name, category)
                
                print(f"[🔍] Ricerca logo per Team 2: {team2}")
                logo2_url = search_team_logo(team2, event_name, category)
                
                # Se abbiamo trovato entrambi i loghi, creiamo un'immagine combinata
                if logo1_url and logo2_url:
                    # Scarica i loghi e l'immagine VS
                    try:
                        from os.path import exists, getmtime
                        
                        # Crea la cartella logos se non esiste
                        logos_dir = os.path.join(output_dir, "logos")
                        os.makedirs(logos_dir, exist_ok=True)
                        
                        # Verifica se l'immagine combinata esiste giÃÂ  e non ÃÂ¨ obsoleta
                        relative_logo_path = poster_relative_path(team1, team2)
                        absolute_output_filename = os.path.join(output_dir, relative_logo_path)
                        if exists(absolute_output_filename):
                            file_age = current_time - os.path.getmtime(absolute_output_filename)
                            if file_age <= three_hours_in_seconds and poster_is_current(absolute_output_filename):
                                print(f"[✓] Utilizzo immagine combinata esistente: {absolute_output_filename}")
                                
                                # Carica le variabili d'ambiente per GitHub
                                NOMEREPO = os.getenv("NOMEREPO", "").strip()
                                NOMEGITHUB = os.getenv("NOMEGITHUB", "").strip()
                                
                                # Se le variabili GitHub sono disponibili, restituisci l'URL raw di GitHub
                                if NOMEGITHUB and NOMEREPO:
                                    github_raw_url = f"https://raw.githubusercontent.com/{NOMEGITHUB}/{NOMEREPO}/main/{urllib.parse.quote(relative_logo_path.replace(os.sep, '/'), safe='/')}"
                                    print(f"[✓] URL GitHub generato per logo esistente: {github_raw_url}")
                                    return github_raw_url
                                else:
                                    # Altrimenti restituisci il percorso locale
                                    return absolute_output_filename
                        
                        # Scarica i loghi
                        img1, img2 = None, None
                        
                        if logo1_url:
                            try:
                                # Aggiungi un User-Agent simile a un browser
                                logo_headers = {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                                }
                                response1 = requests.get(logo1_url, headers=logo_headers, timeout=10)
                                response1.raise_for_status() # Controlla errori HTTP
                                if 'image' in response1.headers.get('Content-Type', '').lower():
                                    img1 = Image.open(io.BytesIO(response1.content))
                                    print(f"[✓] Logo1 scaricato con successo da: {logo1_url}")
                                else:
                                    print(f"[!] URL logo1 ({logo1_url}) non è un'immagine (Content-Type: {response1.headers.get('Content-Type')}).")
                                    logo1_url = None # Invalida URL se non è un'immagine
                            except requests.exceptions.RequestException as e_req:
                                print(f"[!] Errore scaricando logo1 ({logo1_url}): {e_req}")
                                logo1_url = None
                            except Exception as e_pil: # Errore specifico da PIL durante Image.open
                                print(f"[!] Errore PIL aprendo logo1 ({logo1_url}): {e_pil}")
                                logo1_url = None
                        
                        if logo2_url:
                            try:
                                # Aggiungi un User-Agent simile a un browser
                                logo_headers = {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                                }
                                response2 = requests.get(logo2_url, headers=logo_headers, timeout=10)
                                response2.raise_for_status() # Controlla errori HTTP
                                if 'image' in response2.headers.get('Content-Type', '').lower():
                                    img2 = Image.open(io.BytesIO(response2.content))
                                    print(f"[✓] Logo2 scaricato con successo da: {logo2_url}")
                                else:
                                    print(f"[!] URL logo2 ({logo2_url}) non è un'immagine (Content-Type: {response2.headers.get('Content-Type')}).")
                                    logo2_url = None # Invalida URL se non è un'immagine
                            except requests.exceptions.RequestException as e_req:
                                print(f"[!] Errore scaricando logo2 ({logo2_url}): {e_req}")
                                logo2_url = None
                            except Exception as e_pil: # Errore specifico da PIL durante Image.open
                                print(f"[!] Errore PIL aprendo logo2 ({logo2_url}): {e_pil}")
                                logo2_url = None
                        # Procedi con la combinazione solo se entrambi i loghi sono stati caricati con successo
                        if not (img1 and img2):
                            print(f"[!] Impossibile caricare entrambi i loghi come immagini valide per la combinazione. Logo1 caricato: {bool(img1)}, Logo2 caricato: {bool(img2)}.")
                            raise ValueError("Uno o entrambi i loghi non sono stati caricati correttamente.") # Questo forzerÃ  l'except sottostante
                        
                        create_event_poster(team1, team2, img1, img2, event_name, category, time_str, absolute_output_filename)
                        
                        print(f"[✓] Immagine combinata creata: {absolute_output_filename}")
                        
                        # Carica le variabili d'ambiente per GitHub
                        NOMEREPO = os.getenv("NOMEREPO", "").strip()
                        NOMEGITHUB = os.getenv("NOMEGITHUB", "").strip()
                        
                        # Se le variabili GitHub sono disponibili, restituisci l'URL raw di GitHub
                        if NOMEGITHUB and NOMEREPO:
                            github_raw_url = f"https://raw.githubusercontent.com/{NOMEGITHUB}/{NOMEREPO}/main/{urllib.parse.quote(relative_logo_path.replace(os.sep, '/'), safe='/')}"
                            print(f"[✓] URL GitHub generato: {github_raw_url}")
                            return github_raw_url
                        else:
                            # Altrimenti restituisci il percorso assoluto
                            return absolute_output_filename
                        
                    except Exception as e:
                        print(f"[!] Errore nella creazione dell'immagine combinata: {e}")
                        # Se fallisce, restituisci solo il primo logo trovato
                        return logo1_url or logo2_url
                
                # Se non abbiamo trovato entrambi i loghi, restituisci quello che abbiamo
                return logo1_url or logo2_url
                    
        except Exception as e: 
            print(f"[!] Errore nella ricerca del logo per '{event_name}': {e}") 
        
        # Se non troviamo nulla, restituiamo None 
        return None

    def search_team_logo(team_name, event_name="", category=""):
        try:
            return resolve_team_logo(team_name, event_name, category)
        except Exception as e:
            _safe_print(f"[!] Resolver loghi fallito per '{team_name}': {e}")
            return None

    def get_stream_from_channel_id(channel_id): 
        # Restituisce direttamente l'URL .php
        embed_url = f"{LINK_DADDY}/watch.php?id={channel_id}" 
        print(f"URL .php per il canale Daddylive {channel_id}.")
        return embed_url
     
    def clean_category_name(name): 
        # Rimuove tag html come </span> o simili 
        return re.sub(r'<[^>]+>', '', name).strip() 
     
    def extract_channels_from_json(path): 
        keywords = {"italy", "rai", "italia", "it"} 
        now = datetime.now()  # ora attuale completa (data+ora) 
     
        with open(path, "r", encoding="utf-8") as f: 
            data = json.load(f) 
     
        categorized_channels = {} 
     
        for date_key, sections in data.items(): 
            date_part = date_key.split(" - ")[0] 
            try: 
                date_obj = parser.parse(date_part, fuzzy=True).date() 
            except Exception as e: 
                print(f"[!] Errore parsing data '{date_part}': {e}") 
                continue 
     
            # Il titolo della pagina puo' restare indietro di un giorno: il blocco
            # valido e' comunque quello corrente, quindi si usa la data piu' recente.
            block_date = max(date_obj, now.date())
     
            for category_raw, event_items in sections.items(): 
                category = clean_category_name(category_raw)
                # Salta la categoria TV Shows
                if category.lower() == "tv shows":
                    continue
                if category not in categorized_channels: 
                    categorized_channels[category] = [] 
     
                for item in event_items: 
                    time_str = item.get("time", "00:00") 
                    try: 
                        # Parse orario evento
                        time_obj = datetime.strptime(time_str, "%H:%M")

                        # Gli orari prima delle 05:00 (ora UK) appartengono al mattino
                        # successivo: lo "sports day" della pagina parte alle 05:00.
                        event_date_obj = block_date + timedelta(days=1) if time_obj.hour < 5 else block_date
                        event_datetime = datetime.combine(event_date_obj, time_obj.time())

                        # Includi se l'evento e' iniziato da meno di 2 ore (o e' futuro)
                        if now - event_datetime > timedelta(hours=2):
                            continue

                        time_formatted = time_obj.strftime("%H:%M")
                    except Exception: 
                        time_formatted = time_str 
     
                    event_title = item.get("event", "Evento") 
     
                    for ch in item.get("channels", []): 
                        channel_name = ch.get("channel_name", "") 
                        channel_id = ch.get("channel_id", "") 
     
                        words = set(re.findall(r'\b\w+\b', channel_name.lower())) 
                        if keywords.intersection(words): 
                            tvg_name = f"{event_title} ({time_formatted})" 
                            categorized_channels[category].append({ 
                                "tvg_name": tvg_name, 
                                "channel_name": channel_name, 
                                "channel_id": channel_id,
                                "event_title": event_title  # Aggiungiamo il titolo dell'evento per la ricerca del logo
                            }) 
     
        return categorized_channels 
     
    def generate_m3u_from_schedule(json_file, output_file): 
        categorized_channels = extract_channels_from_json(json_file) 
        prefetch_logos_for_channels(categorized_channels)

        with open(output_file, "w", encoding="utf-8") as f: 
            f.write("#EXTM3U\n") 

            # Controlla se ci sono eventi_dlhd prima di aggiungere il canale DADDYLIVE
            has_events = any(channels for channels in categorized_channels.values())
            
            if has_events:
                # Aggiungi il canale iniziale/informativo solo se ci sono eventi_dlhd
                f.write(f'#EXTINF:-1 tvg-name="DADDYLIVE" group-title="Eventi Live DLHD",DADDYLIVE\n')
                f.write("https://example.com.m3u8\n\n")
            else:
                print("[ℹ️] Nessun evento trovato, canale DADDYLIVE non aggiunto.")

            for category, channels in categorized_channels.items(): 
                if not channels: 
                    continue 
          
                for ch in channels: 
                    tvg_name = ch["tvg_name"] 
                    channel_id = ch["channel_id"] 
                    event_title = ch["event_title"]  # Otteniamo il titolo dell'evento
                    channel_name = ch["channel_name"]
                    
                    # Cerca un logo per questo evento
                    # Rimuovi l'orario dal titolo dell'evento prima di cercare il logo
                    clean_event_title = re.sub(r'\s*\(\d{1,2}:\d{2}\)\s*$', '', event_title)
                    print(f"[🔍] Ricerca logo per: {clean_event_title}") 
                    logo_url = search_logo_for_event(clean_event_title, category=category)
                    logo_attribute = f' tvg-logo="{logo_url}"' if logo_url else ''
     
                    try: 
                        # Cerca lo stream .m3u8 nei siti specificati
                        stream = get_stream_from_channel_id(channel_id)

                        if stream: 
                            cleaned_event_id = clean_tvg_id(event_title) # Usa event_title per tvg-id
                            f.write(f'#EXTINF:-1 tvg-id="{cleaned_event_id}" tvg-name="{category} | {tvg_name}"{logo_attribute} group-title="Eventi Live DLHD",{category} | {tvg_name}\n')
                            # Aggiungi EXTHTTP headers per canali daddy (esclusi .php)
                            if "ava.karmakurama.com" in stream and not stream.endswith('.php'):
                                daddy_headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1", "Referrer": "https://ava.karmakurama.com/", "Origin": "https://ava.karmakurama.com"}
                                vlc_opt_lines = headers_to_extvlcopt(daddy_headers)
                                for line in vlc_opt_lines:
                                    f.write(f'{line}\n')
                            f.write(f'{stream}\n\n')
                            print(f"[✓] {tvg_name}" + (f" (logo trovato)" if logo_url else " (nessun logo trovato)")) 
                        else: 
                            print(f"[✗] {tvg_name} - Nessuno stream trovato") 
                    except Exception as e: 
                        print(f"[!] Errore su {tvg_name}: {e}") 
     
    if __name__ == "__main__": 
        generate_m3u_from_schedule(JSON_FILE, OUTPUT_FILE)

# Funzione per il quarto script (schedule_extractor.py)
def schedule_extractor():
    # Codice del quarto script qui
    # Aggiungi il codice del tuo script "schedule_extractor.py" in questa funzione.
    print("Eseguendo lo schedule_extractor.py...")
    # Il codice che avevi nello script "schedule_extractor.py" va qui, senza modifiche.
    from playwright.sync_api import sync_playwright
    import os
    import json
    from datetime import datetime
    import time
    import re
    from bs4 import BeautifulSoup
    from dotenv import load_dotenv
    
    # Carica le variabili d'ambiente dal file .env
    load_dotenv()
    
    LINK_DADDYBYPASS = os.getenv("LINK_DADDYBYPASS", "").strip() or "https://dlive.sx"
    
    def html_to_json(html_content):
        """Converte il contenuto HTML della programmazione in formato JSON."""
        soup = BeautifulSoup(html_content, 'html.parser')
        result = {}
        
        # Cerca il div principale con il nuovo ID
        schedule_div = soup.find('div', id='schedule')
        if not schedule_div:
            # Fallback se l'ID non viene trovato, cerca la classe
            schedule_div = soup.find('div', class_='schedule schedule--compact')
        
        if not schedule_div:
            print("AVVISO: Contenitore 'schedule' non trovato!")
            return result
        
        # La data è unica per tutto lo schedule (Saturday 15th Nov 2025)
        day_title_tag = schedule_div.find('div', class_='schedule__dayTitle')
        if not day_title_tag:
            current_date = "Unknown Date"
        else:
            current_date = day_title_tag.text.strip()
        
        result[current_date] = {}
        
        # Processa tutte le categorie direttamente
        for category_div in schedule_div.find_all('div', class_='schedule__category'):
            cat_header = category_div.find('div', class_='schedule__catHeader')
            if not cat_header:
                continue
            
            # Estrai il nome della categoria dal tag card__meta
            cat_meta = cat_header.find('div', class_='card__meta')
            if not cat_meta:
                continue
            
            current_category = cat_meta.text.strip()
            result[current_date][current_category] = []
            
            category_body = category_div.find('div', class_='schedule__categoryBody')
            if not category_body:
                continue
            
            # Processa tutti gli eventi
            for event_div in category_body.find_all('div', class_='schedule__event'):
                event_header = event_div.find('div', class_='schedule__eventHeader')
                if not event_header:
                    continue
                
                time_span = event_header.find('span', class_='schedule__time')
                event_title_span = event_header.find('span', class_='schedule__eventTitle')
                
                event_data = {
                    'time': time_span.text.strip() if time_span else '',
                    'event': event_title_span.text.strip() if event_title_span else 'Evento Sconosciuto',
                    'channels': []
                }
                
                channels_div = event_div.find('div', class_='schedule__channels')
                if channels_div:
                    for link in channels_div.find_all('a', href=True):
                        href = link.get('href', '')
                        # Estrae l'id da watch.php?id=XXX
                        channel_id_match = re.search(r'id=(\d+)', href)
                        if channel_id_match:
                            channel_id = channel_id_match.group(1)
                            channel_name = link.get('title', link.text.strip())
                            event_data['channels'].append({
                                'channel_name': channel_name,
                                'channel_id': channel_id
                            })
                
                if event_data['channels']:
                    result[current_date][current_category].append(event_data)
        
        return result
    
    def modify_json_file(json_file_path):
        with open(json_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        current_month = datetime.now().strftime("%B")
    
        # Questa logica non è più necessaria con la nuova struttura HTML
        # che fornisce già la data completa.
        # for date in list(data.keys()):
        #     match = re.match(r"(\w+\s\d+)(st|nd|rd|th)\s(\d{4})", date)
        #     if match:
        #         day_part = match.group(1)
        #         suffix = match.group(2)
        #         year_part = match.group(3)
        #         new_date = f"{day_part}{suffix} {current_month} {year_part}"
        #         data[new_date] = data.pop(date)
    
        with open(json_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        
        print(f"File JSON modificato e salvato in {json_file_path}")
        
    def extract_schedule_container():
        from bs4 import BeautifulSoup
        import json
        import os
        
        url = f"{LINK_DADDYBYPASS}/"
        script_dir = os.path.dirname(os.path.abspath(__file__))
        json_output = os.path.join(script_dir, "daddyliveSchedule.json")
        
        print(f"Accesso a {url}...")
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until='networkidle')
                page.wait_for_selector('#schedule', timeout=30000)
                html_content = page.content()
                browser.close()
            
            print("✓ Pagina caricata!")
            
            # Parse HTML
            soup = BeautifulSoup(html_content, 'html.parser')
            schedule_div = soup.find('div', id='schedule')
            
            if not schedule_div:
                print("❌ #schedule non trovato!")
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
                return False
            
            print("✓ Schedule estratto!")
            json_data = html_to_json(str(schedule_div))
            
            with open(json_output, "w", encoding="utf-8") as f:
                json.dump(json_data, f, indent=4)
            
            print(f"✓ Salvato in {json_output}")
            modify_json_file(json_output)
            return True
            
        except Exception as e:
            print(f"❌ ERRORE: {str(e)}")
            return False
    
    if __name__ == "__main__":
        success = extract_schedule_container()
        if not success:
            exit(1)

# Funzione per il sesto script (italy_channels.py)
def italy_channels():
    print("Eseguendo il italy_channels.py...")
    import requests
    import time
    import re
    import xml.etree.ElementTree as ET
    import os
    from dotenv import load_dotenv
    from bs4 import BeautifulSoup

    def getAuthSignature():
        import os, time
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "accept-encoding": "gzip"
        }
        now = int(time.time() * 1000)
        data = {
            "token": "",
            "reason": "app-focus",
            "locale": "de",
            "theme": "dark",
            "metadata": {
                "device": {"type": "phone", "uniqueId": "vypn-test"},
                "os": {"name": "android", "version": "14", "abis": ["arm64-v8a"], "host": "android"},
                "app": {"platform": "android"},
                "version": {"package": "net.vypn.app", "binary": "1.4.1", "js": "1.4.1"}
            },
            "appFocusTime": 0,
            "playerActive": False,
            "playDuration": 0,
            "devMode": False,
            "hasAddon": True,
            "castConnected": False,
            "package": "net.vypn.app",
            "version": "1.4.1",
            "process": "app",
            "firstAppStart": now,
            "lastAppStart": now,
            "ipLocation": None,
            "adblockEnabled": True,
            "migrationApplied": False,
            "migrationTargetInstalled": False,
            "proxy": {
                "supported": ["ss"],
                "engine": "Mu",
                "ssVersion": "2022",
                "enabled": False,
                "autoServer": True,
                "id": ""
            },
            "iap": {"supported": False, "error": ""}
        }
        resp = requests.post("https://www.vypn.net/api/app/ping", json=data, headers=headers, timeout=10)
        return resp.json().get("addonSig") or resp.json().get("mhub")

    def vavoo_groups():
        # Puoi aggiungere altri gruppi per più canali
        return ["Italy"]

    def clean_channel_name(name):
        """Rimuove i suffissi .a, .b, .c dal nome del canale"""
        # Rimuove .a, .b, .c alla fine del nome (con o senza spazi prima)
        cleaned_name = re.sub(r'\s*\.(a|b|c|s|d|e|f|g|h|i|j|k|l|m|n|o|p|q|r|t|u|v|w|x|y|z)\s*$', '', name, flags=re.IGNORECASE)
        return cleaned_name.strip()

    def normalize_channel_name(name):
        name = re.sub(r"\s+", "", name.strip().lower())
        name = re.sub(r"\.it\b", "", name)
        name = re.sub(r"hd|fullhd", "", name)
        return name

    def fetch_logos():
        return {
            "sky uno": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-uno-it.png",
            "rai 1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-1-it.png",
            "rai 2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-2-it.png",
            "rai 3": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-3-it.png",
            "eurosport 1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/spain/eurosport-1-es.png",
            "eurosport 2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/spain/eurosport-2-es.png",
            "italia 1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/italia1-it.png",
            "la 7": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/la7-it.png",
            "la 7 d": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/la7d-it.png",
            "rai sport+": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-sport-it.png",
            "rai sport [live during events only]": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-sport-it.png",
            "rai premium": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-premium-it.png",
            "sky sport golf": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-golf-it.png",
            "sky sport moto gp": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-motogp-it.png",
            "sky sport tennis": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-tennis-it.png",
            "sky sport f1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-f1-it.png",
            "sky sport football": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-football-it.png",
            "sky sport football [live during events only]": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-football-it.png",
            "sky sport uno": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-uno-it.png",
            "sky sport arena": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-arena-it.png",
            "sky cinema collection": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-collection-it.png",
            "sky cinema uno": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-uno-it.png",
            "sky cinema action": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-action-it.png",
            "sky cinema action (backup)": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-action-it.png",
            "sky cinema comedy": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-comedy-it.png",
            "sky cinema uno +24": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-uno-plus24-it.png",
            "sky cinema romance": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-romance-it.png",
            "sky cinema family": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-family-it.png",
            "sky cinema due +24": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-due-plus24-it.png",
            "sky cinema drama": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-drama-it.png",
            "sky cinema suspense": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-suspense-it.png",
            "sky sport 24": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-24-it.png",
            "sky sport 24 [live during events only]": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-24-it.png",
            "sky sport calcio": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-calcio-it.png",
            "sky sport 251": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/77/Sky_Sport_-_Logo_2020.svg/2560px-Sky_Sport_-_Logo_2020.svg.png",
            "sky sport 252": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/77/Sky_Sport_-_Logo_2020.svg/2560px-Sky_Sport_-_Logo_2020.svg.png",
            "sky sport 253": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/77/Sky_Sport_-_Logo_2020.svg/2560px-Sky_Sport_-_Logo_2020.svg.png",
            "sky sport 254": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/77/Sky_Sport_-_Logo_2020.svg/2560px-Sky_Sport_-_Logo_2020.svg.png",
            "sky sport 255": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/77/Sky_Sport_-_Logo_2020.svg/2560px-Sky_Sport_-_Logo_2020.svg.png",
            "sky sport 256": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/77/Sky_Sport_-_Logo_2020.svg/2560px-Sky_Sport_-_Logo_2020.svg.png",
            "sky calcio 1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/germany/sky-select-1-alt-de.png",
            "sky calcio 2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/germany/sky-select-2-alt-de.png",
            "sky calcio 3": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/germany/sky-select-3-alt-de.png",
            "sky calcio 4": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/germany/sky-select-4-alt-de.png",
            "sky calcio 5": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/germany/sky-select-5-alt-de.png",
            "sky calcio 6": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/germany/sky-select-6-alt-de.png",
            "sky calcio 7": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/germany/sky-select-7-alt-de.png",
            "sky serie": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-serie-it.png",
            "crime+investigation": "https://upload.wikimedia.org/wikipedia/commons/4/4d/Crime_%2B_Investigation_Logo_10.2019.svg",
            "20 mediaset": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/20-it.png",
            "mediaset 20": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/20-it.png",
            "27 twenty seven": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/26/Twentyseven_logo.svg/260px-Twentyseven_logo.svg.png",
            "27 twentyseven": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/26/Twentyseven_logo.svg/260px-Twentyseven_logo.svg.png",
            "canale 5": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/canale5-it.png",
            "cine 34 mediaset": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/cine34-it.png",
            "cine 34": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/cine34-it.png",
            "discovery focus": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/focus-it.png",
            "focus": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/focus-it.png",
            "italia 2": "https://upload.wikimedia.org/wikipedia/it/thumb/c/c5/Logo_Italia2.svg/520px-Logo_Italia2.svg.png",
            "mediaset italia 2": "https://upload.wikimedia.org/wikipedia/it/thumb/c/c5/Logo_Italia2.svg/520px-Logo_Italia2.svg.png",
            "mediaset italia": "https://www.italiasera.it/wp-content/uploads/2019/06/Mediaset-640x366.png",
            "mediaset extra": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/mediaset-extra-it.png",
            "mediaset 1": "https://play-lh.googleusercontent.com/2-Cl0plYUCxk8bnbeavm4ZOJ_S4Xuwmql_N3_E4OJyf7XK_YUvdNOWgzn8KD-Bur8w0",
            "mediaset infinity+ 1": "https://play-lh.googleusercontent.com/2-Cl0plYUCxk8bnbeavm4ZOJ_S4Xuwmql_N3_E4OJyf7XK_YUvdNOWgzn8KD-Bur8w0",
            "mediaset infinity+ 2": "https://play-lh.googleusercontent.com/2-Cl0plYUCxk8bnbeavm4ZOJ_S4Xuwmql_N3_E4OJyf7XK_YUvdNOWgzn8KD-Bur8w0",
            "mediaset infinity+ 5": "https://play-lh.googleusercontent.com/2-Cl0plYUCxk8bnbeavm4ZOJ_S4Xuwmql_N3_E4OJyf7XK_YUvdNOWgzn8KD-Bur8w0",
            "mediaset iris": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/iris-it.png",
            "iris": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/iris-it.png",
            "rete 4": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rete4-it.png",
            "sport italia (backup)": "https://play-lh.googleusercontent.com/0IcWROAOpuEcMf2qbOBNQYhrAPUuSmw-zv0f867kUxKSwSTD_chyCDuBP2PScIyWI9k",
            "sport italia": "https://play-lh.googleusercontent.com/0IcWROAOpuEcMf2qbOBNQYhrAPUuSmw-zv0f867kUxKSwSTD_chyCDuBP2PScIyWI9k",
            "sportitalia plus": "https://www.capitaladv.eu/wp-content/uploads/2020/07/LOGO-SPORTITALIA-PLUS-HD_2-1.png",
            "sport italia solo calcio [live during events only]": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/36/SI_Solo_Calcio_logo_%282019%29.svg/1200px-SI_Solo_Calcio_logo_%282019%29.svg.png",
            "sportitalia solocalcio": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/36/SI_Solo_Calcio_logo_%282019%29.svg/1200px-SI_Solo_Calcio_logo_%282019%29.svg.png",
            "dazn 1": "https://upload.wikimedia.org/wikipedia/commons/d/d6/Dazn-logo.png",
            "dazn2": "https://upload.wikimedia.org/wikipedia/commons/d/d6/Dazn-logo.png",
            "dazn": "https://upload.wikimedia.org/wikipedia/commons/d/d6/Dazn-logo.png",
            "dazn zona": "https://upload.wikimedia.org/wikipedia/commons/d/d6/Dazn-logo.png",
            "motortrend": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/39/Motor_Trend_logo.svg/2560px-Motor_Trend_logo.svg.png",
            "sky sport max": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-max-it.png",
            "sky sport nba": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-nba-it.png",
            "sky sport serie a": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-serie-a-it.png",
            "sky sports f1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-f1-it.png",
            "sky sports golf": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2b/Sky_Sport_Golf_Logo_2022.svg/2560px-Sky_Sport_Golf_Logo_2022.svg.png",
            "sky super tennis": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-sport-tennis-it.png",
            "tennis channel": "https://images.tennis.com/image/upload/t_16-9_768/v1620828532/tenniscom-prd/assets/Fallback/Tennis_Fallback_v6_f5tjzv.jpg",
            "super tennis": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/super-tennis-it.png",
            "tv 8": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/tv8-it.png",
            "sky primafila 1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 3": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 4": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 5": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 6": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 7": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 8": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 9": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 10": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 11": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 12": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 13": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 14": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 15": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 16": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 17": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky primafila 18": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-primafila-it.png",
            "sky cinema due": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-cinema-due-it.png",
            "sky atlantic": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-atlantic-it.png",
            "nat geo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/national-geographic-it.png",
            "discovery nove": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/nove-it.png",
            "discovery channel": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/discovery-channel-it.png",
            "real time": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/real-time-it.png",
            "rai 5": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-5-it.png",
            "rai gulp": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-gulp-it.png",
            "rai italia": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Rai_Italia_-_Logo_2017.svg/1024px-Rai_Italia_-_Logo_2017.svg.png",
            "rai movie": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-movie-it.png",
            "rai news 24": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-news-24-it.png",
            "rai scuola": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-scuola-it.png",
            "rai storia": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-storia-it.png",
            "rai yoyo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-yoyo-it.png",
            "rai 4": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/rai-4-it.png",
            "rai 4k": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/bc/Rai_4K_-_Logo_2017.svg/1200px-Rai_4K_-_Logo_2017.svg.png",
            "hgtv": "https://d204lf4nuskf6u.cloudfront.net/italy-images/c2cbeaabb81be73e81c7f4291cf798e3.png?k=2nWZhtOSUQdq2s2ItEDH5%2BQEPdq1khUY8YJSK0%2BNV90dhkyaUQQ82V1zGPD7O5%2BS",
            "top crime": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/top-crime-it.png",
            "cielo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/cielo-it.png",
            "dmax": "https://cdn.cookielaw.org/logos/50417659-aa29-4f7f-b59d-f6e887deed53/a32be519-de41-40f4-abed-d2934ba6751b/9a44af24-5ca6-4098-aa95-594755bd7b2d/dmax_logo.png",
            "food network": "https://upload.wikimedia.org/wikipedia/commons/f/f4/Food_Network_-_Logo_2016.png",
            "giallo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/giallo-it.png",
            "history": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/history-channel-it.png",
            "la 5": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/la5-it.png",
            "sky arte": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-arte-it.png",
            "sky documentaries": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-documentaries-it.png",
            "sky nature": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/sky-nature-it.png",
            "warner tv": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/41/Warner_TV_Italy.svg/1200px-Warner_TV_Italy.svg.png",
            "fox": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/fox-it.png",
            "nat geo wild": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/national-geographic-wild-it.png",
            "animal planet": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/20/2018_Animal_Planet_logo.svg/2560px-2018_Animal_Planet_logo.svg.png",
            "boing": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/boing-it.png",
            "k2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/k2-it.png",
            "discovery k2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/k2-it.png",
            "nick jr": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/nick-jr-it.png",
            "nickelodeon": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/nickelodeon-it.png",
            "premium crime": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/premium-crime-it.png",
            "rakuten action movies": "https://img.utdstc.com/icon/7f6/a4a/7f6a4a47aa35e90d889cb8e71ed9a6930fe5832219371761736e87e880f85a5f:200",
            "rakuten comedy movies": "https://img.utdstc.com/icon/7f6/a4a/7f6a4a47aa35e90d889cb8e71ed9a6930fe5832219371761736e87e880f85a5f:200",
            "rakuten drama": "https://img.utdstc.com/icon/7f6/a4a/7f6a4a47aa35e90d889cb8e71ed9a6930fe5832219371761736e87e880f85a5f:200",
            "rakuten family": "https://img.utdstc.com/icon/7f6/a4a/7f6a4a47aa35e90d889cb8e71ed9a6930fe5832219371761736e87e880f85a5f:200",
            "rakuten top free": "https://img.utdstc.com/icon/7f6/a4a/7f6a4a47aa35e90d889cb8e71ed9a6930fe5832219371761736e87e880f85a5f:200",
            "rakuten tv shows": "https://img.utdstc.com/icon/7f6/a4a/7f6a4a47aa35e90d889cb8e71ed9a6930fe5832219371761736e87e880f85a5f:200",
            "boing plus": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/79/Boing_Plus_logo_2020.svg/1200px-Boing_Plus_logo_2020.svg.png",
            "wwe channel": "https://upload.wikimedia.org/wikipedia/en/8/8c/WWE_Network_logo.jpeg",
            "rsi la 2": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/f4/RSI_La_2_2012.svg/1200px-RSI_La_2_2012.svg.png",
            "rsi la 1": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/be/RSI_La_1_2012.svg/1200px-RSI_La_1_2012.svg.png",
            "cartoon network": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/cartoon-network-it.png",
            "sky tg 24": "https://play-lh.googleusercontent.com/0RJjBW8_r64dWLAbG7kUVrkESbBr9Ukx30pDI83e5_o1obv2MTC7KSpBAIhhXvJAkXE",
            "tg com 24": "https://yt3.hgoogleusercontent.com/ytc/AIdro_kVh4SupZFtHrALXp9dRWD9aahJOUfl8rhSF8VroefSLg=s900-c-k-c0x00ffffff-no-rj",
            "tgcom 24": "https://yt3.hgoogleusercontent.com/ytc/AIdro_kVh4SupZFtHrALXp9dRWD9aahJOUfl8rhSF8VroefSLg=s900-c-k-c0x00ffffff-no-rj",
            "cartoonito": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/cartoonito-it.png",
            "super!": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2d/Super%21_logo_2021.svg/1024px-Super%21_logo_2021.svg.png",
            "deejay tv": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/deejay-tv-it.png",
            "cartoonito (backup)": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/cartoonito-it.png",
            "frisbee": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/italy/frisbee-it.png",
            "catfish": "https://upload.wikimedia.org/wikipedia/commons/4/46/Catfish%2C_the_TV_Show_Logo.PNG", # "tv7 news" era attaccato qui, l'ho rimosso, sembrava un errore di battitura
            "disney+ film": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3e/Disney%2B_logo.svg/2560px-Disney%2B_logo.svg.png",
            "comedy central": "https://yt3.googleusercontent.com/FPzu1EWCI54fIh2j9JEp0NOzwoeugjL4sZTQCdoxoQY1U4QHyKx2L3wPSw27IueuZGchIxtKfv8=s900-c-k-c0x00ffffff-no-rj",
            "arte network": "https://www.arte.tv/sites/corporate/wp-content/themes/arte-entreprise/img/arte_logo.png",
            "aurora arte": "https://www.auroraarte.it/wp-content/uploads/2023/11/AURORA-ARTE-brand.png",
            "telearte": "https://www.teleartetv.it/web/wp-content/uploads/2023/04/logo_TA.jpg",
            "sky sport motogp": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/hd/sky-sport-motogp-hd-it.png",
            "sky sport": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/sky-sport-it.png",
            "rai sport": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/rai-sport-it.png",
            "rtv san marino sport": "https://static.wikia.nocookie.net/internationaltelevision/images/7/79/San_Marino_RTV_Sport_-_logo.png/revision/latest?cb=20221207153729",
            "rtv sport": "https://logowik.com/content/uploads/images/san-marino-rtv-sport-20211731580347.logowik.com.webp",
            "trsport": "https://teleromagna.it/Images/logo-tr-sport.jpg",
            "aci sport tv": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/aci-sport-tv-it.png",
            "euronews": "https://play-lh.googleusercontent.com/Mi8GAQIp3x94VcvbxZNsK-CTNhHy1zmo51pmME5KkkK4WgN4aQhM1FlNgLZUMD4VAXhL",
            "tg norba 24": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/tg-norba-24-it.png",
            "tv7 news": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcStj45lIWvQ0KFzv6jyIP9vOZgPnWQirEl6dw&s",
            "milan tv": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/milan-tv-it.png",
            "rtl 102.5": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/rtl-1025-it.png",
            "la c tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/lac-tv-it.png?raw=true",
            "italian fishing tv": "https://www.upmagazinearezzo.it/atladv/wp-content/uploads/2017/07/atlantide-adv-logo-italian-fishing-tv.jpg",
            "rtv san marino": "https://raw.githubusercontent.com/tv-logo/tv-logos/refs/heads/main/countries/italy/rtv-san-marino-it.png",
            "antenna sud": "https://www.antennasud.com/media/2022/08/cropped-LOGO_ANTENNA_SUD_ROSSO_FORATO.png",
            "senato tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/senato-tv-it.png?raw=true",
            "rete oro": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQkE5wuUMIVAtANMfpSL4T5bIO73owXBhpvEg&s",
            "caccia": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/caccia-it.png?raw=true",
            "111 tv": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSDr-HrHBtGsogIKps_qWVME_l5axKwINoq2Q&s",
            "lazio tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/lazio-style-channel-it.png?raw=true",
            "padre pio tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/padre-pio-tv-it.png?raw=true",
            "inter tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/inter-tv-it.png?raw=true",
            "kiss kiss italia": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/radio-kiss-kiss-italia-it.png?raw=true",
            "12 tv parma": "https://www.12tvparma.it/wp-content/uploads/2021/11/ogg-image.jpg",
            "canale 21 extra": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcROcfjFIqjwxnG9AbEhJ6gwKb6IprmlFnF9aQ&s",
            "videolina": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/videolina-it.png?raw=true",
            "tv 2000": "https://upload.wikimedia.org/wikipedia/it/0/0d/Logo_di_TV2000.png",
            "byoblu": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRaMdUB8WEdsRVi_WZLxoi79pqlRef4s9Zehg&s",
            "kiss kiss napoli": "https://kisskissnapoli.it/wp-content/uploads/2022/03/cropped-logo-kisskiss-napoli.png",
            "kiss kiss": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/radio-kiss-kiss-tv-it.png?raw=true",
            "caccia e pesca": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/caccia-pesca-it.png?raw=true",
            "pesca": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/pesca-it.png?raw=true",
            "canale 7": "https://upload.wikimedia.org/wikipedia/commons/2/24/Canale_7.png",
            "crime+inv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/crime-and-investigation-it.png?raw=true",
            "cafe 24": "https://play-lh.googleusercontent.com/DW0Tvz72-8XZ7rEBVh1jBzwYE1fZhTaowuuxN75Jl8yBtnFkySH1z2T2b7OPlotmHeQ",
            "antenna 2": "https://www.omceo.bg.it/images/loghi/antenna-2.png",
            "avengers grimm channel": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQN8by3YXjCGJQaxT6b-cgZ872BjY_NLIrALA&s",
            "classica": "https://upload.wikimedia.org/wikipedia/commons/4/4e/CLA_HD_Logo-CENT-300ppi_CMYK.jpg",
            "70 80 hits": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcS5WEMBuK9zFW2nr_clM7noNGaUwp5fxRrmJA&s",
            "cusano italia tv": "https://play-lh.googleusercontent.com/c2HegRLQmaQFJXROyFH-phglfaZzQ-vikbZ464ZVJGfW8kX9jQuLACb2TIlydv1apsg",
            "espansione tv": "https://massimoemanuelli.com/wp-content/uploads/2017/10/etv-logo-attuale.png?w=640",
            "tva vicenza": "https://massimoemanuelli.com/wp-content/uploads/2017/10/tva-vi-2.png",
            "m2o": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/34/Radio_m2o_-_Logo_2019.svg/1200px-Radio_m2o_-_Logo_2019.svg.png",
            "televenezia": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ0xhoeVg7nRLPp8GnPkFzLUWvJ5WolvU-iYw&s",
            "a3": "https://yt3.googleusercontent.com/3zkPKViC7G2rHWbBpYzSL6dFM9OMFBqIC6JrT-mM73EQsERHMqx4sPzWpBD8nfEqgf_uSHi124Y=s900-c-k-c0x00ffffff-no-rj",
            "alto adige tv": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQSdt3E_MmezXRKr7QOUEr0leEcErbaNGqbog&s",
            "cremona 1": "https://www.arvedi.it/fileadmin/user_upload/istituzionale/gruppo-arvedi-e-informazione-logo-Cremona1.png",
            "gold tv": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSSjSJx2Wah0-hfWbBn4_C79K5I0600lcD8zw&s",
            "france 24": "https://github.com/tv-logo/tv-logos/blob/main/countries/france/france-24-fr.png?raw=true",
            "iunior tv": "https://upload.wikimedia.org/wikipedia/commons/9/94/Iunior_tv.png",
            "canale 2": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/canale-italia-2-it.png?raw=true",
            "pesca e caccia": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/caccia-pesca-it.png?raw=true",
            "qvc": "https://github.com/tv-logo/tv-logos/blob/main/countries/germany/qvc-de.png?raw=true",
            "tele chiara": "https://upload.wikimedia.org/wikipedia/commons/b/ba/Telechiara-logo.png",
            "bergamo tv": "https://www.opq.it/wp-content/uploads/BergamoTV.png",
            "italia 3": "https://static.wikia.nocookie.net/dreamlogos/images/4/4e/Italia_3_2013.png/revision/latest?cb=20200119124403",
            "primocanale": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/primocanale-it.png?raw=true",
            "rei tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/rei-tv-it.png?raw=true",
            "rete veneta": "https://upload.wikimedia.org/wikipedia/it/d/df/Logo_Rete_Veneta.png",
            "telearena": "https://upload.wikimedia.org/wikipedia/commons/6/60/TeleArena_logo.png",
            "reggio tv": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/60/Reggio_TV_logo.png/640px-Reggio_TV_logo.png",
            "tv2000": "https://upload.wikimedia.org/wikipedia/it/0/0d/Logo_di_TV2000.png",
            "retebiella tv": "https://alpitv.com/wp-content/uploads/2022/01/logo.png",
            "videostar tv": "https://www.videostartv.eu/images/videostar.png",
            "canale 8": "https://upload.wikimedia.org/wikipedia/it/thumb/6/6d/TV8_Logo_2016.svg/1200px-TV8_Logo_2016.svg.png",
            "juwelo italia": "https://upload.wikimedia.org/wikipedia/commons/f/fd/Juwelo_TV.svg",
            "rtc telecalabria": "https://play-lh.googleusercontent.com/7PzluYVAEVOCNzGYGkewkKI3PA0PkCKAc9KUZGfYzAbZnQLnlPAE5iQBMZEUi7ZKwJc",
            "tele mia": "https://upload.wikimedia.org/wikipedia/commons/a/a6/Telemia.png",
            "bloomberg tv 4k": "https://github.com/tv-logo/tv-logos/blob/main/countries/united-states/bloomberg-television-us.png?raw=true",
            "tele abruzzo": "https://www.abruzzi.tv/logo-abruzzi.png",
            "fashiontv": "https://github.com/tv-logo/tv-logos/blob/main/countries/international/fashion-tv-int.png?raw=true",
            "quarta rete": "https://quartarete.tv/wp-content/uploads/2022/06/Logo-Quartarete-ok.png",
            "fashion tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/international/fashion-tv-int.png?raw=true",
            "love fm tv": "https://www.lovefm.it/themes/default/assets/img_c/logo-love-new.png",
            "telerama": "https://upload.wikimedia.org/wikipedia/commons/b/b1/T%C3%A9l%C3%A9rama_logo.png",
            "teletubbies": "https://banner2.cleanpng.com/20180606/qrg/aa9vorpin.webp",
            "primo canale": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/primocanale-it.png?raw=true",
            "lira tv": "https://liratv.es/wp-content/uploads/2021/07/LIRA-TV-1.png",
            "la tr3": "https://www.tvdream.net/img/latr3.png",
            "tele liguria sud": "https://www.teleliguriasud.it/sito/wp-content/uploads/2024/10/LOGO-RETINA.png",
            "la nuova tv": "https://play-lh.googleusercontent.com/Ck_esrelbBPGT2rsTtvuvciOBHA0f5b-VExXvBf-NP9fegvHhEuN9MIx7pgdv1WlW8o",
            "top calcio 24": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRPHW5VLQDGnNMVZszgWZRqnBSjPUTgAcUltQ&s",
            "fm italia": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRtJ5HlBu4jXOrC4iA-giQNzXa9zm42bS-yrA&s",
            "supersix lombardia": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcT_ebhZMV4eYibx6UpVDOt1KOlmhOPYBh0gKw&s",
            "prima tv": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/36/Prima_TV_Logo_2022.svg/800px-Prima_TV_Logo_2022.svg.png",
            "camera dei deputati": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/camera-dei-deputati-it.png?raw=true",
            "tele venezia": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ0xhoeVg7nRLPp8GnPkFzLUWvJ5WolvU-iYw&s",
            "telemolise": "https://m.media-amazon.com/images/I/61yiY3jR+kL.png",
            "esperia tv 18": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/81/ESPERIATV18_verde.png/260px-ESPERIATV18_verde.png",
            "onda novara tv": "https://gag-fondazionedeagostini.s3.amazonaws.com/wp-content/uploads/2023/03/logo-Onda-Novara-TV-Ufficiale-1.png",
            "carina tv": "https://radiocarina.it/wp-content/uploads/2024/01/RadioCarina-Vers.2.png",
            "teleromagna": "https://teleromagna.it/images/teleromagna-logo.png",
            "elive tv brescia": "https://upload.wikimedia.org/wikipedia/commons/e/ec/%C3%88_live_Brescia_logo.png",
            "bellla & monella tv": "https://upload.wikimedia.org/wikipedia/commons/0/0e/Logo_Ufficiale_Radio_Bellla_%26_Monella.png",
            "videotolentino": "https://yt3.googleusercontent.com/ytc/AIdro_kAZM1WRzE6qfQx90xPJ3v1Jz1gaJwn6BbrZcewu6eTcQ=s900-c-k-c0x00ffffff-no-rj",
            "super tv brescia": "https://bresciasat.it/assets/front/img/logo3.png",
            "umbria tv": "https://upload.wikimedia.org/wikipedia/commons/0/09/Umbria_TV_wordmark%2C_ca._2020.png",
            "qvc italia": "https://github.com/tv-logo/tv-logos/blob/main/countries/germany/qvc-de.png?raw=true",
            "rttr": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/rttr-it.png?raw=true",
            "onda tv": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcT9Og_2yYQhg-ersjEG5xZ99bri_Di4l5dlyw&s",
            "rttr tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/rttr-it.png?raw=true",
            "teleboario": "https://i.ytimg.com/vi/vNB5TJBjA3U/sddefault.jpg",
            "video novara": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ1PvoHpnx0hNKtt435CUaJza2e_qsm5B87Cg&s",
            "fano tv": "https://prolocopesarourbino.it/wp-content/uploads/2019/07/FANO-TV.jpg",
            "etv marche": "https://etvmarche.it/wp-content/uploads/2021/05/Logo-Marche-BLU.png",
            "granducato": "https://www.telegranducato.it/wp-content/uploads/img_struttura_sito/logo_granducato_ridotto.png",
            "maria+vision italia": "",
            "star comedy": "https://github.com/tv-logo/tv-logos/blob/main/countries/portugal/star-comedy-pt.png?raw=true",
            "telecolor": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/telecolore-it.png?raw=true",
            "telequattro": "https://telequattro.medianordest.it/wp-content/uploads/2020/10/T4Logo.png",
            "tele tusciasabina 2000": "https://yt3.googleusercontent.com/ytc/AIdro_lp10Brud3JZex6CgE4M9c-XcKFY4MrRhcFe9PUn-N4SD4=s900-c-k-c0x00ffffff-no-rj",
            "stereo 5 tv": "https://www.stereo5.it/2022/wp-content/uploads/2023/02/LOGO-NUOVO-2023-2.png",
            "televideo agrigento": "https://lh3.googleusercontent.com/proxy/UNXKnLrwdDNoio4peXah3Pz81kI5Cv2FzJo82TPzn4seN-JZ3tovuVe45XSRBkIyMOfrrZ3bnWaMsTi80Xj40Q",
            "vco azzurra tv": "https://upload.wikimedia.org/wikipedia/commons/9/91/Logo_VCO_Azzurra_TV.png",
            "company tv": "https://www.trendcomunicazione.com/wp-content/uploads/2018/11/20180416-logo-tv-bokka-300x155.png",
            "tele pavia": "https://www.milanopavia.tv/wp-content/uploads/2020/01/logoMilanoPaviaTV.png",
            "uninettuno": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSYRpKemP5FC0RLOQVhc9kPU71aJW9Tj9DU8g&s",
            "star life": "https://github.com/tv-logo/tv-logos/blob/main/countries/argentina/star-life-ar.png?raw=true",
            "vera tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/vera-tv-it.png?raw=true",
            "arancia tv": "",
            "entella tv": "https://m.media-amazon.com/images/I/81omr2rZ8+L.png",
            "euro tv": "https://upload.wikimedia.org/wikipedia/it/9/93/Eurotv.png",
            "peer tv alto adige": "https://www.cxtv.com.br/img/Tvs/Logo/webp-l/6e7dee025526c334b9280153c418e10e.webp",
            "esperia tv": "https://upload.wikimedia.org/wikipedia/commons/b/b3/Logo_ESPERIAtv.png",
            "tele friuli": "https://www.telefriuli.it/wp-content/uploads/2022/11/logo_telefriuli_positivo.png",
            "rtp": "https://upload.wikimedia.org/wikipedia/commons/7/7c/RTP.png",
            "icaro tv": "https://www.gruppoicaro.it/wp-content/uploads/2020/05/icarotv.png",
            "telea tv": "https://www.tvdream.net/img/telea-tv.png",
            "telemantova": "https://www.telemantova.it/gfx/lib/ath-v1/logos/tmn/plain.svg?20241007",
            "bloomberg tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/united-states/bloomberg-television-us.png?raw=true",
            "super j": "https://e7.pngegg.com/pngimages/439/74/png-clipart-superman-superhero-drawing-super-man-font-superhero-heart.png",
            "uninettuno university tv": "https://www.laureaonlinegiurisprudenza.it/wp-content/uploads/2019/09/Logo-Uninettuno.png",
            "rds social tv": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/73/RDS-Logo.png/260px-RDS-Logo.png",
            "rete tv italia": "https://www.retetvitalia.it/news/wp-content/uploads/2019/07/cropped-RTI-L.png",
            "fm italia tv": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRtJ5HlBu4jXOrC4iA-giQNzXa9zm42bS-yrA&s",
            "telenord": "https://upload.wikimedia.org/wikipedia/it/a/a8/Telenord.png",
            "equ tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/equ-tv-it.png?raw=true",
            "orler tv": "https://www.tvdream.net/img/orlertv.png",
            "rmc 101": "https://upload.wikimedia.org/wikipedia/commons/f/fc/LogoRMC101.png",
            "telebari": "https://www.aaroiemac.it/notizie/wp-content/uploads/2018/11/1524066248-telebari.png",
            "telepace trento": "https://www.tvdream.net/img/telepace-trento.png",
            "trentino tv": "https://www.trentinotv.it/images/resource/logo-trentino.png",
            "tv qui": "https://www.tvdream.net/img/tvqui-modena-cover.jpg",
            "tv 33": "https://d1yjjnpx0p53s8.cloudfront.net/styles/logo-thumbnail/s3/0013/3844/brand.gif?itok=54JkEUiu",
            "trm h24": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/trm-h24-it.png?raw=true",
            "tt teletutto": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSQpzqT0DLXv-md7VU-fTF5BeaEashocwHUdw&s",
            "teletricolore": "https://www.teletricolore.it/wp-content/uploads/2018/02/logo.png",
            "globus television": "https://ennapress.it/wp-content/uploads/2020/10/globus.png",
            "rtr 99 tv": "https://www.tvdream.net/img/rtr99-cover.jpg",
            "tele romagna": "https://teleromagna.it/images/teleromagna-logo.png", 
            "telecitta": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7e/Logo_Telecitt%C3%A0.svg/800px-Logo_Telecitt%C3%A0.svg.png",
            "rds social": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/73/RDS-Logo.png/260px-RDS-Logo.png", 
            "super tv aristanis": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQFBXh88zZx4IvyQKyYd5Hu2yeytO42zNQ4zA&s",
            "tv yes": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/tv-yes-it.png?raw=true",
            "quadrifoglio tv": "https://i.imgur.com/GfzpwKD.png",
            "telemistretta": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQN6reG7R24hdOigLSXg2G5oKcPqKt8cBc0jQ&s",
            "tele sirio": "https://www.telesirio.it/images/logo.png",
            "tvrs": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/tvrs-it.png?raw=true",
            "tele tricolore": "https://www.teletricolore.it/wp-content/uploads/2018/02/logo.png", 
            "telepace": "https://e7.pngegg.com/pngimages/408/890/png-clipart-telepace-high-definition-television-hot-bird-%C4%8Ct1-albero-della-vita-television-text.png",
            "baby tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/spain/baby-tv-es.png?raw=true",
            "mtv hits": "https://github.com/tv-logo/tv-logos/blob/main/countries/serbia/mtv-hits-rs.png?raw=true",
            "radio freccia": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/radio-freccia-it.png?raw=true",
            "bella radio tv": "https://i0.wp.com/bellaradio.it/wp-content/uploads/2020/01/Bella-2020-3.png?fit=3000%2C3000&ssl=1",
            "ol3 radio": "https://pbs.twimg.com/profile_images/570326948497195008/Wf6DPfFP_400x400.jpeg",
            "51 radio tv": "https://tvtvtv.ru/icons/51_tv.png",
            "radionorba tv": "https://github.com/tv-logo/tv-logos/blob/main/countries/italy/radio-norba-tv-it.png?raw=true",
            "euro indie music chart tv": "https://m.media-amazon.com/images/I/61Wa4RqJVJL.png",
            "tele radio sciacca": "https://pbs.twimg.com/profile_images/613988173203423232/rWCQ9j6h_400x400.png",
            "radio capital": "https://static.wikia.nocookie.net/logopedia/images/1/1e/Radio_Capital_-_Logo_2019.svg.png/revision/latest?cb=20190815181629",
            "radio 51": "https://tvtvtv.ru/icons/51_tv.png" 
        }

    CATEGORY_KEYWORDS = {
        "Rai": ["rai"],
        "Mediaset": ["twenty seven", "twentyseven", "mediaset", "italia 1", "italia 2", "canale 5", "la 5", "cine 34", "top crime", "iris", "focus", "rete 4"],
        "Sport": ["inter", "milan", "lazio", "calcio", "tennis", "sport", "sportitalia", "trsport", "sports", "super tennis", "supertennis", "dazn", "eurosport", "sky sport", "rai sport"],
        "Film - Serie TV": ["crime", "primafila", "cinema", "movie", "film", "serie", "hbo", "fox", "rakuten", "atlantic"],
        "News": ["news", "tg", "rai news", "sky tg", "tgcom", "euronews"],
        "Bambini": ["frisbee", "super!", "fresbee", "k2", "cartoon", "boing", "nick", "disney", "baby", "rai yoyo", "cartoonito"],
        "Documentari": ["documentaries", "discovery", "geo", "history", "nat geo", "nature", "arte", "documentary"],
        "Musica": ["deejay", "rds", "hits", "rtl", "mtv", "vh1", "radio", "music", "kiss", "kisskiss", "m2o", "fm"],
        "Altro": ["real time"]
    }

    def classify_channel(name):
        name_lower = name.lower()
        for category, words in CATEGORY_KEYWORDS.items():
            for word in words:
                # Gestione speciale per parole con caratteri speciali
                if any(char in word for char in ['!', '&', '+', '-']):
                    # Per parole con caratteri speciali, usa una ricerca diretta
                    if word in name_lower:
                        return category
                else:
                    # Per parole normali, usa word boundaries
                    pattern = r'\b' + re.escape(word) + r'\b'
                    if re.search(pattern, name_lower):
                        return category
        return "Altro"

    def get_channels():
        signature = getAuthSignature()
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "accept-encoding": "gzip",
            "mediahubmx-signature": signature
        }
        all_channels = []
        for group in vavoo_groups():
            cursor = 0
            while True:
                data = {
                    "language": "de",
                    "region": "AT",
                    "catalogId": "iptv",
                    "id": "iptv",
                    "adult": False,
                    "search": "",
                    "sort": "name",
                    "filter": {"group": group},
                    "cursor": cursor,
                    "clientVersion": "3.0.2"
                }
                resp = requests.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers, timeout=10)
                r = resp.json()
                items = r.get("items", [])
                all_channels.extend(items)
                cursor = r.get("nextCursor")
                if not cursor:
                    break
        return all_channels

    def create_tvg_id_map(epg_file="epg.xml"):
        """Legge un file EPG XML e mappa i nomi dei canali normalizzati ai loro tvg-id."""
        tvg_id_map = {}
        try:
            tree = ET.parse(epg_file)
            root = tree.getroot()
            for channel in root.findall('.//channel'):
                tvg_id = channel.get('id')
                display_name = channel.find('display-name').text
                if tvg_id and display_name:
                    normalized_name = normalize_channel_name(display_name)
                    tvg_id_map[normalized_name] = tvg_id
        except Exception as e:
            print(f"Errore nella lettura di {epg_file}: {e}")
        return tvg_id_map

    def save_as_m3u(channels, filename="italy.m3u"):
        logos = fetch_logos()
        epg_xml_path = os.path.join(output_dir, "epg.xml")
        tvg_id_map = create_tvg_id_map(epg_xml_path)
        channels_by_category = {}

        # MAPPA SEMPLIFICATA PER RENAME
        VAVOO_RENAME_MAP = {
            "DISCOVERY FOCUS": "FOCUS",
            "CINE 34 MEDIASET": "CINE 34", 
            "MEDIASET IRIS": "IRIS",
            "MEDIASET 1": "ITALIA 1",
            "ZONA DAZN": "DAZN",
            "27 TWENTY SEVEN": "27 TWENTYSEVEN"
        }

        # Processa i canali Vavoo (che sono dizionari)
        if channels and isinstance(channels[0], dict):
            for ch in channels:
                original_name = ch.get("name", "SenzaNome")
                name = clean_channel_name(original_name)
                
                # Applica il rename
                display_name = VAVOO_RENAME_MAP.get(name.upper(), name)
                
                # USA IL NOME MODIFICATO PER IL LOOKUP
                name_for_lookup = display_name  # Nome modificato per ricerca logo/tvg-id
                
                url = ch.get("url", "")
                category = classify_channel(display_name)

                if url:
                    if category not in channels_by_category:
                        channels_by_category[category] = []
                    
                    # USA IL NOME MODIFICATO PER RICERCA LOGO E TVG-ID
                    logo = logos.get(name_for_lookup.lower(), "")
                    tvg_id = tvg_id_map.get(normalize_channel_name(name_for_lookup), "")
                    
                    channels_by_category[category].append({
                        "name": display_name,      # Nome modificato per display
                        "url": url,
                        "logo": logo,             # Logo trovato con nome modificato
                        "tvg_id": tvg_id         # TVG-ID trovato con nome modificato
                    })

        # Salva nel file M3U
        with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for category, channel_list in channels_by_category.items():
                channel_list.sort(key=lambda x: x["name"].lower())
                
                # Gestione dei canali duplicati (aggiungi suffisso numerico)
                name_count = {}
                url_by_name = {}
                # Prima passata: conta le occorrenze dei nomi e memorizza gli URL
                for ch in channel_list:
                    name = ch["name"]
                    url = ch["url"]
                    if name not in name_count:
                        name_count[name] = 1
                        url_by_name[name] = [url]
                    else:
                        name_count[name] += 1
                        url_by_name[name].append(url)

                # Seconda passata: rinomina i canali duplicati con URL diversi
                for ch in channel_list:
                    name = ch["name"]
                    url = ch["url"]
                    # Se ci sono più canali con lo stesso nome ma URL diversi
                    if name_count[name] > 1 and len(set(url_by_name[name])) > 1:
                        # Trova l'indice di questo URL nell'elenco degli URL per questo nome
                        idx = url_by_name[name].index(url) + 1
                        # Modifica il nome solo se non è già stato modificato
                        if not name.endswith(f"({idx})"):
                            ch["name"] = f"{name} ({idx})"

                f.write(f"\n# {category.upper()}\n")
                for ch in channel_list:
                    name = ch["name"]
                    url = ch["url"]
                    
                    # Usa logo e tvg_id pre-calcolati
                    logo = ch.get("logo", "")
                    tvg_id = ch.get("tvg_id", "")
                    
                    f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}" group-title="{category}",{name}\n')
                    
                    # Aggiungi EXTHTTP headers per canali daddy (esclusi .php)
                    if "ava.karmakurama.com" in url and not url.endswith('.php'):
                        daddy_headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1", "Referrer": "https://ava.karmakurama.com/", "Origin": "https://ava.karmakurama.com"}
                        vlc_opt_lines = headers_to_extvlcopt(daddy_headers)
                        for line in vlc_opt_lines:
                            f.write(f'{line}\n')
                    
                    f.write(f'{url}\n')

        print(f"Playlist M3U salvata in: {os.path.join(output_dir, filename)}")
        print(f"Totale canali Vavoo: {len(channels)}")
        print(f"Totale canali per categoria:")
        for category, channel_list in channels_by_category.items():
            print(f" {category}: {len(channel_list)} canali")

    if __name__ == "__main__":
        # 1. Canali da sorgenti Vavoo (JSON)
        print("\n--- Fetching canali da sorgenti Vavoo (JSON) ---")
        channels = get_channels()
        print(f"Trovati {len(channels)} canali Vavoo.")

        # 2. Crea la playlist M3U
        print("\n--- Creazione playlist M3U ---")
        # Salva i canali Vavoo
        save_as_m3u(channels, filename="vavoo.m3u")
    
# Funzione per il settimo script (world_channels_generator.py)
def world_channels_generator():
    # Codice del settimo script qui
    # Aggiungi il codice del tuo script "world_channels_generator.py" in questa funzione.
    print("Eseguendo il world_channels_generator.py...")
    # Il codice che avevi nello script "world_channels_generator.py" va qui, senza modifiche.
    import requests
    import time
    import re
    
    def getAuthSignature():
        import os, time
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "accept-encoding": "gzip"
        }
        now = int(time.time() * 1000)
        data = {
            "token": "",
            "reason": "app-focus",
            "locale": "de",
            "theme": "dark",
            "metadata": {
                "device": {"type": "phone", "uniqueId": "vypn-test"},
                "os": {"name": "android", "version": "14", "abis": ["arm64-v8a"], "host": "android"},
                "app": {"platform": "android"},
                "version": {"package": "net.vypn.app", "binary": "1.4.1", "js": "1.4.1"}
            },
            "appFocusTime": 0,
            "playerActive": False,
            "playDuration": 0,
            "devMode": False,
            "hasAddon": True,
            "castConnected": False,
            "package": "net.vypn.app",
            "version": "1.4.1",
            "process": "app",
            "firstAppStart": now,
            "lastAppStart": now,
            "ipLocation": None,
            "adblockEnabled": True,
            "migrationApplied": False,
            "migrationTargetInstalled": False,
            "proxy": {
                "supported": ["ss"],
                "engine": "Mu",
                "ssVersion": "2022",
                "enabled": False,
                "autoServer": True,
                "id": ""
            },
            "iap": {"supported": False, "error": ""}
        }
        resp = requests.post("https://www.vypn.net/api/app/ping", json=data, headers=headers, timeout=10)
        return resp.json().get("addonSig") or resp.json().get("mhub")
    
    def vavoo_groups():
        # Puoi aggiungere altri gruppi per più canali
        return [""]
    
    def clean_channel_name(name):
        """Rimuove i suffissi .a, .b, .c dal nome del canale"""
        # Rimuove .a, .b, .c alla fine del nome (con o senza spazi prima)
        cleaned_name = re.sub(r'\s*\.(a|b|c|s|d|e|f|g|h|i|j|k|l|m|n|o|p|q|r|t|u|v|w|x|y|z)\s*$', '', name, flags=re.IGNORECASE)
        return cleaned_name.strip()
    
    def get_channels():
        signature = getAuthSignature()
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "accept-encoding": "gzip",
            "mediahubmx-signature": signature
        }
        all_channels = []
        for group in vavoo_groups():
            cursor = 0
            while True:
                data = {
                    "language": "de",
                    "region": "AT",
                    "catalogId": "iptv",
                    "id": "iptv",
                    "adult": False,
                    "search": "",
                    "sort": "name",
                    "filter": {"group": group},
                    "cursor": cursor,
                    "clientVersion": "3.0.2"
                }
                resp = requests.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers, timeout=10)
                r = resp.json()
                items = r.get("items", [])
                all_channels.extend(items)
                cursor = r.get("nextCursor")
                if not cursor:
                    break
        return all_channels
    
    def save_as_m3u(channels, filename="world.m3u"):
        # Raggruppa i canali per categoria
        channels_by_category = {}
        
        for ch in channels:
            original_name = ch.get("name", "SenzaNome")
            # Pulisce il nome rimuovendo .a, .b, .c
            name = clean_channel_name(original_name)
            url = ch.get("url", "")
            category = ch.get("group", "Generale")  # Usa il campo "group" come categoria
            
            if url:
                if category not in channels_by_category:
                    channels_by_category[category] = []
                channels_by_category[category].append((name, url))
        
        with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            
            # Scrivi i canali raggruppati per categoria
            for category, channel_list in channels_by_category.items():
                # Aggiungi un commento per la categoria
                f.write(f"\n# {category.upper()}\n")
                
                for name, url in channel_list:
                    f.write(f'#EXTINF:-1 group-title="{category}",{name}\n{url}\n')
        
        print(f"Playlist M3U salvata in: {os.path.join(output_dir, filename)}")
        print(f"Canali organizzati in {len(channels_by_category)} categorie:")
        for category, channel_list in sorted(channels_by_category.items()):
            print(f"  - {category}: {len(channel_list)} canali")
    
    if __name__ == "__main__":
        channels = get_channels()
        print(f"Trovati {len(channels)} canali. Creo la playlist M3U con i link proxy...")
        save_as_m3u(channels) 
    
def sportsonline():
    import requests
    import re
    from bs4 import BeautifulSoup
    import datetime
    
    # URL del file di programmazione
    PROG_URL = "https://sportsonline.sc/prog.txt"
    # Lingua che vogliamo cercare
    TARGET_LANGUAGE = "ITALIAN"
    
    def get_italian_channels(lines):
        """
        Analizza le righe del file di programmazione per trovare i canali in italiano.
        Restituisce una lista di identificativi dei canali (es. ['hd7']).
        """
        italian_channels = []
        for line in lines:
            # Cerca le righe che definiscono la lingua di un canale
            if TARGET_LANGUAGE in line.upper():
                # Estrae l'identificativo del canale (es. "HD7") e lo converte in minuscolo
                channel_id = line.split()[0].lower()
                italian_channels.append(channel_id)
                print(f"[INFO] Trovato canale italiano: {channel_id.upper()}")
        return italian_channels
    
    def main():
        """
        Funzione principale che orchestra il processo.
        """
        # --- Controllo del giorno della settimana ---
        today_weekday = datetime.date.today().weekday() # Lunedì=0, Martedì=1, ..., Domenica=6
        weekdays_english = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
        # Applichiamo sempre il filtro per il giorno corrente
        day_to_filter = weekdays_english[today_weekday]
        print(f"Oggi è {day_to_filter}, verranno cercati solo gli eventi_dlhd di oggi.")
    
        print(f"1. Scarico la programmazione da: {PROG_URL}")
        try:
            response = requests.get(PROG_URL, timeout=10)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[ERRORE FATALE] Impossibile scaricare il file di programmazione: {e}")
            return
    
        lines = response.text.splitlines()
    
        print("\n2. Cerco i canali in lingua italiana...")
        italian_channels = get_italian_channels(lines)
    
        playlist_entries = []
    
        if not italian_channels:
            print("[ATTENZIONE] Nessun canale italiano trovato nella programmazione.")
            print("[INFO] Creo un canale fallback 'NESSUN EVENTO'...")
            playlist_entries.append({
                "name": "NESSUN EVENTO", 
                "url": "https://cph-p2p-msl.akamaized.net/hls/live/2000341/test/master.m3u8",
                "referrer": "https://sportsonline.sc/",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
        else:
            print("\n3. Cerco gli Eventi trasmessi sui canali italiani...")
    
            # --- NUOVA LOGICA DI SCANSIONE PER GIORNO ---
            processing_today_events = (day_to_filter is None) # Se è weekend, processa tutto
    
            for line in lines:
                line_upper = line.upper().strip()
    
                # Controlliamo se la riga è un'intestazione di un giorno della settimana
                if line_upper in weekdays_english:
                    if day_to_filter and line_upper == day_to_filter:
                        # Abbiamo trovato la sezione del giorno corrente, iniziamo a processare
                        processing_today_events = True
                    else:
                        # Abbiamo trovato un altro giorno, smettiamo di processare
                        processing_today_events = False
                    continue
    
                # Processiamo la riga solo se siamo nella sezione del giorno giusto (o se è weekend)
                if not processing_today_events:
                    continue
    
                # Da qui in poi, la logica è la stessa, ma viene eseguita solo sulle righe corrette
                if '|' not in line:
                    continue
    
                parts = line.split('|')
                if len(parts) != 2:
                    continue
    
                event_info = parts[0].strip()
                page_url = parts[1].strip()
    
                is_italian_event = any(f"/{channel}.php" in page_url for channel in italian_channels)
    
                if is_italian_event:
                    print(f"\n[EVENTO] Trovato evento italiano: '{event_info}'")
                    
                    # Riformattiamo il nome dell'evento per mettere l'orario alla fine
                    event_parts = event_info.split(maxsplit=1)
                    if len(event_parts) == 2:
                        time_str_original, name_only = event_parts
                        
                        # --- Aggiungi 1 ora all'orario ---
                        try:
                            # Converte la stringa dell'orario in un oggetto datetime
                            original_time = datetime.datetime.strptime(time_str_original.strip(), '%H:%M')
                            # Aggiunge un'ora
                            new_time = original_time + datetime.timedelta(hours=1)
                            time_str = new_time.strftime('%H:%M')
                        except ValueError:
                            time_str = time_str_original.strip() # Usa l'orario originale se il formato non è valido
                        event_name = f"{name_only.strip()} {time_str}"
                    else:
                        event_name = event_info # Fallback se il formato non è quello previsto
    
                    # Aggiungiamo direttamente l'URL .php alla lista
                    playlist_entries.append({
                        "name": event_name,
                        "url": page_url,
                        "referrer": "https://sportsonline.sc/",
                        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    })
            
            # --- AGGIUNTA: Creazione canale fallback se non ci sono eventi_dlhd ---
            if not playlist_entries:
                print("\n[INFO] Nessun evento italiano con link streaming valido trovato oggi.")
                print("[INFO] Creo un canale fallback 'NESSUN EVENTO'...")
                playlist_entries.append({
                    "name": "NESSUN EVENTO", 
                    "url": "https://cph-p2p-msl.akamaized.net/hls/live/2000341/test/master.m3u8",
                    "referrer": "https://sportsonline.sc/",
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
    
        # 4. Creazione del file M3U
        output_filename = os.path.join(output_dir, "sportsonline.m3u")
        print(f"\n4. Scrivo la playlist nel file: {output_filename}")
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for entry in playlist_entries:
                f.write(f'#EXTINF:-1 group-title="Eventi Live SPORTSONLINE",{entry["name"]}\n')
                f.write(f'{entry["url"]}\n')
    
        print(f"\n[COMPLETATO] Playlist creata con successo! Apri il file '{output_filename}' con un player come VLC.")
    
    if __name__ == "__main__":
        main()

def search_m3u8_in_sites(channel_id, is_tennis=False, session=None):
    """
    Cerca i file .m3u8 nei siti specificati per i canali daddy e tennis
    """
    # Carica la variabile d'ambiente LINK_DADDY
    LINK_DADDY = os.getenv("LINK_DADDY", "").strip() or "https://dlive.sx"
    # Restituisce direttamente l'URL .php come richiesto
    embed_url = f"{LINK_DADDY}/watch.php?id={channel_id}"
    print(f"URL .php per il canale Daddylive {channel_id}: {embed_url}")
    return embed_url

# Funzione principale che esegue tutti gli script
def main():
    # load_daddy_cache()  # RIMOSSO: non più definita né necessaria
    try:
        canali_daddy_flag = os.getenv("CANALI_DADDY", "no").strip().lower()
        if canali_daddy_flag == "si":
            try:
                schedule_success = schedule_extractor()
            except Exception as e:
                print(f"Errore durante l'esecuzione di schedule_extractor: {e}")

        # Leggi le variabili d'ambiente
        eventi_dlhd_en = os.getenv("eventi_dlhd_EN", "no").strip().lower()
        world_flag = os.getenv("WORLD", "si").strip().lower()

        # eventi_dlhd M3U8
        try:
            if canali_daddy_flag == "si":
                if eventi_dlhd_en == "si":
                    eventi_dlhd_m3u8_generator_world()
                else:
                    eventi_dlhd_m3u8_generator()
            else:
                print("[INFO] Generazione eventi_dlhd.m3u8 saltata: CANALI_DADDY non è 'si'.")
        except Exception as e:
            print(f"Errore durante la generazione eventi_dlhd.m3u8: {e}")
            return

        # EPG Merger
        try:
            epg_merger()
        except Exception as e:
            print(f"Errore durante l'esecuzione di epg_merger: {e}")
            return

        # Canali Italia
        try:
            italy_channels()
        except Exception as e:
            print(f"Errore durante l'esecuzione di italy_channels: {e}")
            return

        # Canali World (solo se WORLD=si)
        try:
            if world_flag == "si":
                world_channels_generator()
            else:
                print("[INFO] Generazione world.m3u saltata: WORLD non è 'si'.")
        except Exception as e:
            print(f"Errore durante l'esecuzione di world_channels_generator: {e}")
            
        try:
            sportsonline()
        except Exception as e:
            print(f"Errore durante l'esecuzione di sportsonline: {e}")
            
        print("Tutti gli script sono stati eseguiti correttamente!")
    finally:
        pass 
        
if __name__ == "__main__":
    main()
