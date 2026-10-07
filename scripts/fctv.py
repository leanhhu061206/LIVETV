#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FCTV33 M3U GENERATOR - Tutti gli eventi live

Logica (ricavata da extractors/fctv33.py di EasyProxy):
  - /api/common/bs  -> token "bs" per endpoint (code 100 = /api/match/live,
                       code 102 = /api/match/detail). Nessuna firma richiesta.
  - /api/match/live -> lista live in protobuf. Richiede URL firmato:
                       BASE/sfver<HASH><BS_TOKEN>/api/match/live
                       dove HASH = md5(JSON({"sportType":..,"language":..,"stream":true}))[:6]
                       e BS_TOKEN = token da /api/common/bs per code=100.
                       (cfr. handleReq / initBs nel JS del sito)
  - /api/match/detail -> dettaglio + lista stream (protobuf, nessuna firma).
  - /api/stream/detail -> HLS firmato IP-bound (rb-session). NON usato qui:
                         l'URL firmato scade ed e' legato all'IP della Action,
                         quindi in playlist scriviamo URL STABILI risolvibili
                         dall'extractor fctv33 di EasyProxy:
                           - pagina evento:  SITE/sport/leagueLink-matchId/teamLink.html
                           - dettaglio stream: SITE/live/detail?matchId=..&streamId=..&sportType=..&siteType=..

Stile / output: come scripts/sports99.py e scripts/streamed.py
  - output in root del repo git (anche se lanciato da scripts/)
  - fallback "NESSUN EVENTO" se vuoto (come streamed.py)
  - solo dipendenza: requests (+ stdlib)

Uso:
  python scripts/fctv.py [-o fctv.m3u] [--no-streams] [--lang 0]
                         [--only-sports football,basketball] [--exclude-sports others]
"""

import argparse
import hashlib
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =============================================================================
# CONFIGURAZIONE
# =============================================================================
OUTPUT_FILE = "fctv.m3u"
TIMEOUT = 20
LANGUAGE = 0  # cLangType: en=0, it=8, de=7, ... (0 = nomi originali, va bene per ITA)

# Finestra live (S1): scarta match finiti da più di N ore; gli orari in
# playlist sono Europe/Rome (il runner GitHub è in UTC).
PAST_CUTOFF_HOURS = 4
# Detail mirato (S2): chiama /api/match/detail solo se il match è nell'hint
# live (payload field 2) oppure dentro questa finestra attorno a now.
DETAIL_PAST_HOURS = 3
DETAIL_FUTURE_HOURS = 1

# Stato match (B: field 4 del protobuf, cfr. enum MS_* nel JS: MS_FTB_LIVE=100,
# MS_BSK_LIVE=200, ..., MS_FINISH=10000). Fallback A: durata stimata per sport.
MS_FINISH = 10000
STATUS_LIVE_MAX = 9999
# durata stimata evento per sportType (fallback A quando manca lo stato)
SPORT_DURATION_H = {
    1: 2.5, 2: 2.5, 3: 4.0, 4: 4.0, 6: 8.0, 7: 4.0, 8: 2.5, 9: 4.0,
    10: 3.0, 11: 3.0, 12: 2.0, 13: 2.5, 14: 3.0, 15: 6.0, 16: 2.5, 90: 3.0,
}

try:
    from zoneinfo import ZoneInfo
    ROME_TZ = ZoneInfo("Europe/Rome")
except Exception:
    ROME_TZ = None

SITE_URL = "https://www.fctv33hd.rest"
DATA_API_BASES = [
    "https://apis-data10.tcdru136ovur.ru",
    "https://apis-data10.tcllu137fien.ru",
    "https://apis-data-defra10.tcllu137fien.ru",
]

EPG_URLS = "https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz,https://github.com/nzo66/TV/raw/refs/heads/main/epg.xml.gz"

# sportType id -> slug (inverso di SPORT_SLUG_MAP in extractors/fctv33.py)
SPORT_SLUG = {
    1: "football",
    2: "basketball",
    3: "tennis",
    4: "baseball",
    6: "cricket",
    7: "motorsport",
    8: "rugby",
    9: "american-football",
    10: "aussie-rules",
    11: "hockey",
    12: "badminton",
    13: "volleyball",
    14: "fighting",
    15: "cycling",
    16: "handball",
    90: "others",
}
SPORT_TYPES = sorted(SPORT_SLUG.keys())

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": f"{SITE_URL}/",
    "Origin": SITE_URL,
    "Accept": "*/*",
}

# Sessioni per-thread (S3): requests.Session non è thread-safe, una per thread.
_thread_local = threading.local()


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": HEADERS["User-Agent"]})
    return s


def get_thread_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = make_session()
        _thread_local.session = s
    return s


def select_sports(only: str = "", exclude: str = ""):
    """Filtra sport per slug o id numerico (S5). Ritorna lista di sportType."""
    def _parse(v: str):
        out = set()
        for tok in (v or "").replace(";", ",").split(","):
            tok = tok.strip().lower()
            if not tok:
                continue
            if tok.isdigit() and int(tok) in SPORT_SLUG:
                out.add(int(tok))
                continue
            for sid, slug in SPORT_SLUG.items():
                if tok == slug:
                    out.add(sid)
                    break
        return out
    sports = set(SPORT_TYPES)
    inc = _parse(only)
    exc = _parse(exclude)
    if inc:
        sports &= inc
    sports -= exc
    return sorted(sports)


# =============================================================================
# PROTOBUF MINIMALE (varint + length-delimited, come in fctv33.py)
# =============================================================================
def read_varint(buf: bytes, offset: int = 0):
    value = 0
    shift = 0
    index = offset
    length = len(buf)
    while index < length:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return value, index


def read_length_delimited(buf: bytes, offset: int = 0):
    length, start = read_varint(buf, offset)
    return buf[start:start + length], start + length


def read_fields(buf: bytes):
    fields = {}
    offset = 0
    length = len(buf)
    while offset < length:
        tag, next_offset = read_varint(buf, offset)
        offset = next_offset
        field = tag >> 3
        wire = tag & 0x7
        if wire == 0:
            val, after = read_varint(buf, offset)
            offset = after
            fields.setdefault(field, []).append(val)
            continue
        if wire == 2:
            chunk, after = read_length_delimited(buf, offset)
            offset = after
            fields.setdefault(field, []).append(chunk)
            continue
        break
    return fields


def _b2s(v) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


# =============================================================================
# PATH / UTILS
# =============================================================================
def find_repo_root():
    start_dirs = [Path.cwd().resolve(), Path(__file__).resolve().parent]
    for start_dir in start_dirs:
        for directory in (start_dir, *start_dir.parents):
            if (directory / ".git").exists():
                return directory
    return Path.cwd().resolve()


def pick_api_base(session: requests.Session) -> str:
    """Ritorna il primo DATA_API_BASE che risponde a /api/common/timesync."""
    for base in DATA_API_BASES:
        try:
            r = session.get(f"{base}/api/common/timesync", headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and b"Success" in r.content[:20]:
                return base
        except Exception:
            continue
    return DATA_API_BASES[0]


def get_bs_token(session: requests.Session, base: str, code: int, sport: int, lang: int) -> str:
    """Token bs per endpoint (cfr. initBs nel JS: /api/common/bs?code=..&sportType=..)."""
    r = session.get(
        f"{base}/api/common/bs",
        headers=HEADERS,
        params={"code": code, "sportType": sport, "language": lang, "stream": True},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    top = read_fields(r.content)
    if not top.get(10):
        raise RuntimeError(f"bs: envelope senza payload (code={code} sport={sport})")
    inner = read_fields(top[10][0])
    if not inner.get(1):
        raise RuntimeError(f"bs: payload senza item (code={code} sport={sport})")
    kv = read_fields(inner[1][0])
    token = _b2s(kv[2][0])
    return token


def sfver_prefix(params_sorted: dict, bs_token: str) -> str:
    """sfver + md5(JSON(params))[:6] + bs_token (cfr. handleReq a3 nel JS)."""
    s = json.dumps(params_sorted, separators=(",", ":"))
    h = hashlib.md5(s.encode("utf-8")).hexdigest()[:6]
    return f"sfver{h}{bs_token}"


# =============================================================================
# PARSING MATCH LIVE (payload field 1 di /api/match/live)
# =============================================================================
def _nested_str(blob: bytes, *path) -> str:
    """Scende nei protobuf annidati e ritorna la prima stringa utile."""
    try:
        cur = blob
        for p in path:
            f = read_fields(cur)
            if p not in f or not isinstance(f[p][0], bytes):
                return ""
            cur = f[p][0]
        return _b2s(cur)
    except Exception:
        return ""


def parse_live_match(buf: bytes) -> dict:
    m = read_fields(buf)
    try:
        match_id = str(m[1][0])
    except Exception:
        return {}
    try:
        sport = int(m[2][0])
    except Exception:
        sport = 1
    try:
        match_date = int(m[3][0])
    except Exception:
        match_date = 0
    # stato (B): field 4. Assente = upcoming; 1..9999 = live in corso
    # (100/101.. calcio, 200.. basket, ...); >=10000 = finito/cancellato.
    try:
        status = int(m[4][0]) if 4 in m else None
    except Exception:
        status = None

    # league: field 10 -> {3: {2: name}, 4: logo}
    league = ""
    league_logo = ""
    try:
        lb = read_fields(m[10][0])
        if 3 in lb and isinstance(lb[3][0], bytes):
            league = _nested_str(lb[3][0], 2)
        if 4 in lb and isinstance(lb[4][0], bytes):
            league_logo = _b2s(lb[4][0]).split("!")[0]
    except Exception:
        pass

    # teams: field 30 -> [0]=match name {2:...}, [1]=home {1..,10:inner}, [2]=away
    match_name = ""
    home = away = ""
    home_logo = ""
    try:
        entries = m.get(30, [])
        if entries:
            try:
                match_name = _nested_str(entries[0], 2)
            except Exception:
                match_name = ""
        for idx, key in ((1, "home"), (2, "away")):
            if len(entries) > idx and isinstance(entries[idx], bytes):
                team_outer = read_fields(entries[idx])
                if 10 in team_outer and isinstance(team_outer[10][0], bytes):
                    team_inner = read_fields(team_outer[10][0])
                    if 3 in team_inner and isinstance(team_inner[3][0], bytes):
                        name = _nested_str(team_inner[3][0], 2)
                        if key == "home":
                            home = name
                        else:
                            away = name
                    if 4 in team_inner and isinstance(team_inner[4][0], bytes):
                        logo = _b2s(team_inner[4][0]).split("!")[0]
                        if key == "home" and logo:
                            home_logo = logo
    except Exception:
        pass

    if not home or not away:
        # fallback: "A vs B" dal nome match
        if " vs " in match_name:
            parts = match_name.split(" vs ", 1)
            home = home or parts[0].strip()
            away = away or parts[1].strip()

    # slug per URL evento stabile: campi 150..165 -> {20: teamLink, 21: leagueLink}
    # (il numero del campo varia per sport: football=150, basket=152, tennis=153, ...)
    team_link = ""
    league_link = ""
    try:
        for _f in range(150, 166):
            if _f in m and isinstance(m[_f][0], bytes):
                try:
                    extra = read_fields(m[_f][0])
                except Exception:
                    continue
                if 20 in extra and 21 in extra:
                    team_link = _b2s(extra[20][0]).strip().lower()
                    league_link = _b2s(extra[21][0]).strip().lower()
                    break
    except Exception:
        pass

    slug = SPORT_SLUG.get(sport, "football")
    if league_link and team_link:
        event_url = f"{SITE_URL}/{slug}/{league_link}-{match_id}/{team_link}.html"
    else:
        event_url = f"{SITE_URL}/live/detail?matchId={match_id}&sportType={sport}"

    logo = home_logo or league_logo

    return {
        "matchId": match_id,
        "sportType": sport,
        "sport": slug,
        "matchDate": match_date,
        "status": status,
        "league": league,
        "home": home,
        "away": away,
        "name": match_name or f"{home} vs {away}".strip(" vs "),
        "logo": logo,
        "event_url": event_url,
    }


def fetch_live_sport(base: str, sport: int, lang: int, session: requests.Session = None):
    """Ritorna (match_parsati, live_ids) per uno sport.

    live_ids = matchId nel payload field 2 (hint dei match con stream/live).
    """
    session = session or get_thread_session()
    params_sorted = {"sportType": sport, "language": lang, "stream": True}
    token = get_bs_token(session, base, 100, sport, lang)
    prefix = sfver_prefix(params_sorted, token)
    r = session.get(
        f"{base}/{prefix}/api/match/live",
        headers=HEADERS,
        params={"sportType": sport, "language": lang, "stream": "true"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    top = read_fields(r.content)
    if not top.get(10):
        return [], set()
    payload = read_fields(top[10][0])
    out = []
    for raw in payload.get(1, []):
        if not isinstance(raw, bytes):
            continue
        info = parse_live_match(raw)
        if info.get("matchId"):
            out.append(info)
    live_ids = set()
    for raw in payload.get(2, []):
        if not isinstance(raw, bytes):
            continue
        try:
            f = read_fields(raw)
            for mid in f.get(50, []):
                live_ids.add(str(mid))
        except Exception:
            continue
    return out, live_ids


def fetch_match_detail(base: str, match_id: str, sport: int, retries: int = 4,
                       session: requests.Session = None):
    """Dettaglio match: ritorna (streams, team_link, league_link).

    Gestisce 429 con backoff+jitter e rotazione round-robin delle basi API
    (S3). Ritorna liste vuote se fallisce.
    """
    session = session or get_thread_session()
    params = {
        "matchId": match_id,
        "sportType": str(sport),
        "digit": "seth",
        "country": "IT",
        "continent": "EU",
    }
    bases = [base] + [b for b in DATA_API_BASES if b != base]
    # round-robin dal primo tentativo (S3): distribuisce il carico, non solo al retry
    try:
        start_idx = abs(hash(match_id)) % len(bases)
    except Exception:
        start_idx = 0
    last_err = None
    for attempt in range(retries):
        b = bases[(start_idx + attempt) % len(bases)]
        try:
            # piccola pacing casuale per non martellare l'API (S3)
            if attempt > 0:
                time.sleep(random.uniform(0.2, 0.8))
            r = session.get(f"{b}/api/match/detail", headers=HEADERS, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = 2 * (attempt + 1) + random.uniform(0, 1)
                print(f"    [~] 429 detail {match_id}, retry tra {wait:.1f}s (tentativo {attempt + 1})")
                time.sleep(wait)
                last_err = "429"
                continue
            r.raise_for_status()
            top = read_fields(r.content)
            if not top.get(10):
                return [], "", ""
            payload = read_fields(top[10][0])
            streams = []
            for raw in payload.get(2, []):
                if not isinstance(raw, bytes):
                    continue
                f = read_fields(raw)
                try:
                    sid = str(f[1][0] if isinstance(f[1][0], int) else _b2s(f[1][0]))
                except Exception:
                    continue
                name = _b2s(f.get(3, [b""])[0]) if 3 in f else ""
                try:
                    site = int(f.get(9, [2001])[0])
                except Exception:
                    site = 2001
                if sid and sid != "0":
                    streams.append({"streamId": sid, "name": name or "Stream", "siteType": site})
            team_link = league_link = ""
            try:
                if 1 in payload and isinstance(payload[1][0], bytes):
                    mf = read_fields(payload[1][0])
                    for _f in range(150, 166):
                        if _f in mf and isinstance(mf[_f][0], bytes):
                            try:
                                extra = read_fields(mf[_f][0])
                            except Exception:
                                continue
                            if 20 in extra and 21 in extra:
                                team_link = _b2s(extra[20][0]).strip().lower()
                                league_link = _b2s(extra[21][0]).strip().lower()
                                break
            except Exception:
                pass
            return streams, team_link, league_link
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1) + random.uniform(0, 1))
                continue
            break
    print(f"    [!] detail {match_id}: {last_err}")
    return [], "", ""


def fetch_match_streams(base: str, match_id: str, sport: int, session: requests.Session = None):
    """Compat: solo lista stream."""
    streams, _, _ = fetch_match_detail(base, match_id, sport, retries=2, session=session)
    return streams


# =============================================================================
# GENERATORE M3U
# =============================================================================
def fmt_time(ms: int) -> str:
    """Orario evento in Europe/Rome (S1: il runner GitHub è in UTC).

    Con data DD/MM se non è oggi (per gli upcoming di domani).
    """
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        rdt = dt.astimezone(ROME_TZ) if ROME_TZ else dt.astimezone()
        now_r = datetime.now(timezone.utc).astimezone(ROME_TZ) if ROME_TZ else datetime.now().astimezone()
        hhmm = rdt.strftime("%H:%M")
        if rdt.date() != now_r.date():
            return rdt.strftime("%d/%m ") + hhmm
        return hhmm
    except Exception:
        return ""


def sanitize(s: str) -> str:
    return (s or "").replace(",", " ").replace('"', "'").strip()


def _extinf(match_id: str, sport_slug: str, display: str, logo: str, stream_id: str = "") -> str:
    """Riga EXTINF (S4): tvg-id significativo + group-title per sport."""
    tid = f"fctv33.{match_id}" + (f".{stream_id}" if stream_id else "")
    group = f"FCTV {sport_slug.upper()}"
    logo_attr = f'tvg-logo="{logo}"' if logo else 'tvg-logo=""'
    return f'#EXTINF:-1 tvg-id="{tid}" group-title="{group}" {logo_attr},{display}'


def generate_m3u(output_file: str = OUTPUT_FILE, with_streams: bool = True, lang: int = LANGUAGE,
                 only_sports: str = "", exclude_sports: str = "") -> str:
    print("=" * 60)
    print("FCTV M3U GENERATOR - Tutti gli eventi live")
    print("=" * 60)
    print()
    t0 = time.time()

    sports = select_sports(only_sports, exclude_sports)
    if not sports:
        print("[!] Nessuno sport selezionato dopo i filtri.")
        return ""

    main_session = make_session()
    base = pick_api_base(main_session)
    print(f"[*] Data API: {base} | lang={lang} | streams={'on' if with_streams else 'off'} | sport: {len(sports)}")
    print()

    # 1. live per sport (parallelo, sessione per thread - S3)
    all_matches = []
    live_hint_ids = set()
    print("[*] Recupero live per sport...")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_live_sport, base, sp, lang): sp for sp in sports}
        for fut in as_completed(futs):
            sp = futs[fut]
            try:
                items, hint_ids = fut.result()
                print(f"    [+] {SPORT_SLUG.get(sp, sp)}: {len(items)} eventi")
                all_matches.extend(items)
                live_hint_ids |= hint_ids
            except Exception as e:
                print(f"    [!] sport={sp}: {e}")

    print(f"[+] Totale eventi live: {len(all_matches)} (hint con stream: {len(live_hint_ids)})")

    # S1: cutoff match finiti da più di PAST_CUTOFF_HOURS ore
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - PAST_CUTOFF_HOURS * 3600 * 1000
    kept, dropped = [], 0
    for mt in all_matches:
        md = mt.get("matchDate", 0) or 0
        if md and md < cutoff_ms:
            dropped += 1
            continue
        kept.append(mt)
    all_matches = kept
    print(f"[*] Dopo cutoff (-{PAST_CUTOFF_HOURS}h): {len(all_matches)} eventi (scartati: {dropped})")
    all_matches.sort(key=lambda x: (x.get("matchDate", 0), x.get("sport", ""), x.get("name", "")))

    # 2. (opzionale) stream per match -> una entry per stream con URL dettaglio stabile
    # S2: detail solo per match nell'hint live o dentro la finestra
    #     [now-DETAIL_PAST_HOURS, now+DETAIL_FUTURE_HOURS]; gli altri usano
    #     subito la pagina evento .html senza chiamate.
    # Ordinamento stile app: 0=live in corso, 1=upcoming, 2=finiti di recente.
    entries = []  # (gruppo, matchDate, sport, name, extinf, url)
    skipped = 0
    seen_urls = set()
    lo = now_ms - DETAIL_PAST_HOURS * 3600 * 1000
    hi = now_ms + DETAIL_FUTURE_HOURS * 3600 * 1000

    def _group(mt) -> int:
        """0=live in corso, 1=upcoming, 2=finito (B stato reale + fallback A)."""
        if mt["matchId"] in live_hint_ids:
            return 0
        st = mt.get("status")
        if st is not None:
            if st >= MS_FINISH:
                return 2
            if st > 0:
                return 0
            return 1  # MS_COMING == 0
        # fallback A: stima fine = kickoff + durata tipica dello sport
        md = mt.get("matchDate", 0) or 0
        if not md:
            return 0
        dur_ms = int(SPORT_DURATION_H.get(mt.get("sportType", 0), 3.0) * 3600 * 1000)
        if md > now_ms:
            return 1
        if now_ms > md + dur_ms:
            return 2
        return 0

    if with_streams and all_matches:
        need_detail, fast = [], []
        for mt in all_matches:
            md = mt.get("matchDate", 0) or 0
            st = mt.get("status")
            status_live = st is not None and 0 < st < MS_FINISH
            if (mt["matchId"] in live_hint_ids) or status_live or (md and lo <= md <= hi) or not md:
                need_detail.append(mt)
            else:
                fast.append(mt)
        print(f"[*] Detail mirato: {len(need_detail)} match (hint/finestra), {len(fast)} diretti .html")

        def _add(group, match_date, sport_slug, match_id, display, logo, url, stream_id=""):
            if url in seen_urls:
                return False
            seen_urls.add(url)
            if group == 2 and "[ENDED]" not in display:
                display = f"[ENDED] {display}"
            entries.append((group,
                            match_date, sport_slug, display,
                            _extinf(match_id, sport_slug, display, logo, stream_id), url))
            return True

        for mt in fast:
            hhmm = fmt_time(mt.get("matchDate", 0))
            league = sanitize(mt.get("league", ""))
            name = sanitize(mt.get("name", ""))
            display = sanitize(f"[{mt['sport'].upper()}] {hhmm} - {name} ({league})".strip())
            url = mt["event_url"]
            if ".html" not in url and "streamId" not in url:
                skipped += 1
                continue
            _add(_group(mt), mt.get("matchDate", 0), mt["sport"], mt["matchId"], display, mt.get("logo", ""), url)

        def _job(mt):
            lst, team_link, league_link = fetch_match_detail(base, mt["matchId"], mt["sportType"], retries=4)
            # arricchisci event_url se il live non aveva gli slug (es. basket/tennis)
            if team_link and league_link and ".html" not in mt.get("event_url", ""):
                mt["event_url"] = (
                    f"{SITE_URL}/{mt['sport']}/{league_link}-{mt['matchId']}/{team_link}.html"
                )
            # piccola pausa anti-burst tra un detail e l'altro (S3)
            time.sleep(random.uniform(0.1, 0.4))
            return mt, lst

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(_job, mt): mt for mt in need_detail}
            for fut in as_completed(futs):
                mt, lst = fut.result()
                g = _group(mt)
                hhmm = fmt_time(mt.get("matchDate", 0))
                league = sanitize(mt.get("league", ""))
                name = sanitize(mt.get("name", ""))
                logo = mt.get("logo", "")
                if not lst:
                    url = mt["event_url"]
                    # scarta URL non risolvibili dall'extractor (detail senza streamId e senza .html)
                    if ".html" not in url and "streamId" not in url:
                        skipped += 1
                        continue
                    display = sanitize(f"[{mt['sport'].upper()}] {hhmm} - {name} ({league})".strip())
                    _add(g, mt.get("matchDate", 0), mt["sport"], mt["matchId"], display, logo, url)
                    continue
                for st in lst:
                    sname = sanitize(st["name"])
                    display = sanitize(f"[{mt['sport'].upper()}] {hhmm} - {name} [{sname}] ({league})".strip())
                    url = (f"{SITE_URL}/live/detail?matchId={mt['matchId']}"
                           f"&streamId={st['streamId']}&sportType={mt['sportType']}&siteType={st['siteType']}")
                    _add(g, mt.get("matchDate", 0), mt["sport"], mt["matchId"], display, logo, url, st["streamId"])
        print(f"[+] Voci playlist (match x stream): {len(entries)} (scartate non risolvibili: {skipped})")
    else:
        for mt in all_matches:
            hhmm = fmt_time(mt.get("matchDate", 0))
            league = sanitize(mt.get("league", ""))
            name = sanitize(mt.get("name", ""))
            display = sanitize(f"[{mt['sport'].upper()}] {hhmm} - {name} ({league})".strip())
            url = mt["event_url"]
            if url in seen_urls:
                continue
            if ".html" not in url and "streamId" not in url:
                skipped += 1
                continue
            seen_urls.add(url)
            g = _group(mt)
            if g == 2 and "[ENDED]" not in display:
                display = f"[ENDED] {display}"
            entries.append((g, mt.get("matchDate", 0), mt["sport"], display,
                            _extinf(mt["matchId"], mt["sport"], display, mt.get("logo", "")), url))

    entries.sort(key=lambda x: (x[0], x[1], x[2], x[3]))

    m3u = [f'#EXTM3U url-tvg="{EPG_URLS}"']
    for _, _, _, _, extinf, url in entries:
        m3u.append(extinf)
        m3u.append(url)

    if len(m3u) == 1:
        print("\n[!] Nessun evento trovato. Aggiungo fallback 'NESSUN EVENTO'.")
        m3u.append('#EXTINF:-1 tvg-id="fctv33.none" tvg-logo="" tvg-name="NESSUN EVENTO" group-title="FCTV",NESSUN EVENTO')
        m3u.append("https://example.com/no_event")

    out_path = Path(output_file)
    if not out_path.is_absolute():
        out_path = find_repo_root() / output_file
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")

    print()
    print(f"[+] Playlist salvata in: {out_path}")
    n_live = sum(1 for e in entries if e[0] == 0)
    n_next = sum(1 for e in entries if e[0] == 1)
    n_past = sum(1 for e in entries if e[0] == 2)
    print(f"[+] Eventi live: {len(all_matches)} | voci: {len(entries)} "
          f"(live: {n_live}, upcoming: {n_next}, finiti: {n_past}) | {time.time() - t0:.1f}s")
    print()
    print("NOTA: gli URL sono pagine/dettagli STABILI (non HLS firmati).")
    print("Aprili tramite EasyProxy extractor 'fctv33' (es. /extractor/video?d=<url>,")
    print("/proxy/manifest.m3u8?url=<url>&host=fctv33): la firma rb-session")
    print("IP-bound viene creata dalla tua IP al momento della visione.")
    print("=" * 60)
    print("COMPLETATO!")
    print("=" * 60)
    return str(out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Genera playlist M3U con tutti gli eventi live FCTV33")
    ap.add_argument("-o", "--output", default=OUTPUT_FILE, help=f"File di output (default: {OUTPUT_FILE})")
    ap.add_argument("--no-streams", action="store_true", help="Una voce per match (salta /api/match/detail)")
    ap.add_argument("--lang", type=int, default=LANGUAGE, help="cLangType numerico (default: 0=en)")
    ap.add_argument("--only-sports", default="", help="Solo questi sport: slug o id separati da virgola (es. football,basketball o 1,2)")
    ap.add_argument("--exclude-sports", default="", help="Escludi questi sport: slug o id separati da virgola")
    args = ap.parse_args()
    generate_m3u(args.output, with_streams=not args.no_streams, lang=args.lang,
                 only_sports=args.only_sports, exclude_sports=args.exclude_sports)
