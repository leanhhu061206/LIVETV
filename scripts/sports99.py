#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import requests
import urllib.parse as myParse
from typing import Optional, Dict, List
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import lista as poster_generator
except Exception as e:
    print(f"[!] Generatore poster non disponibile: {e}")
    poster_generator = None

# =============================================================================
# CONFIGURAZIONE
# =============================================================================
OUTPUT_FILE = "sports99.m3u"
TIMEOUT = 120
USER = "cdnlivetv"
PLAN = "free"

EPG_URLS = "https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz,https://github.com/leanhhu061206/LIVETV/raw/refs/heads/main/epg.xml.gz"

# =============================================================================
# SPORTS99 CLIENT
# =============================================================================
class StreamSportsClient:
    
    def __init__(self, user: str = "streamsports99", plan: str = "vip"):
        self.user = user
        self.plan = plan
        self.base_api = "https://api.cdnlivetv.ru/api/v1"
        self.player_referer = "https://streamsports99.su/"
        self.player_referer = "https://streamsports99.su/"


    def fetch_channels_sports(self) -> Optional[list]:
        url = f"{self.base_api}/events/sports/?user={self.user}&plan={self.plan}"
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()

            flattened_channels = []

            if "cdn-live-tv" in data:
                for sport_category, events in data["cdn-live-tv"].items():
                    if not isinstance(events, list):
                        continue

                    for event in events:
                        tournament = event.get("tournament", "")
                        home_team = event.get("homeTeam", "")
                        away_team = event.get("awayTeam", "")
                        match_info = f"{tournament} - {home_team} vs {away_team}"

                        for channel in event.get("channels", []):
                            flattened_channels.append({
                                "name": f"{match_info} - {channel['channel_name']}",
                                "channel_name": channel["channel_name"],
                                "code": channel["channel_code"],
                                "url": channel["url"],
                                "image": channel.get("image", ""),
                                "match_info": match_info,
                                "home_team": home_team,
                                "away_team": away_team,
                                "tournament": tournament,
                                "home_team_img": event.get("homeTeamIMG", ""),
                                "away_team_img": event.get("awayTeamIMG", ""),
                                "sport_category": sport_category,
                                "status": event.get("status", "unknown"),
                                "start": event.get("start", ""),
                            })

            return flattened_channels
        except Exception as e:
            print(f"Errore nel parsing: {e}")
            return None

    def get_sports(self) -> Optional[list]:
        print("[*] Recupero eventi sportivi...")
        return self.fetch_channels_sports()


# =============================================================================
# GENERATORE M3U
# =============================================================================
def generate_m3u(output_file: str = OUTPUT_FILE) -> str:
    print("=" * 60)
    print("SPORTS99 M3U GENERATOR - Solo eventi sportivi ITA")
    print("=" * 60)
    print()
    
    print(f"[*] User: {USER}, Plan: {PLAN}")
    print()
    
    client = StreamSportsClient(user=USER, plan=PLAN)
    
    poster_cache = {}

    def download_logo_image(url: str):
        if not url or poster_generator is None:
            return None
        try:
            response = requests.get(url, headers={"User-Agent": poster_generator.LOGO_UA}, timeout=10)
            if response.status_code == 200 and "image" in (response.headers.get("Content-Type") or "").lower():
                from io import BytesIO
                from PIL import Image
                image = Image.open(BytesIO(response.content))
                image.load()
                return image
        except Exception:
            pass
        return None

    def get_poster(c: dict) -> str:
        if poster_generator is None:
            return ""
        home = str(c.get("home_team", "")).strip()
        away = str(c.get("away_team", "")).strip()
        if not home or not away:
            return ""
        key = (home, away, c.get("tournament", ""))
        if key in poster_cache:
            return poster_cache[key]
        event_name = f"{c.get('tournament', '')} : {home} vs {away}"
        sport = c.get("sport_category", "Sport")
        url = ""
        try:
            cached, absolute = poster_generator.cached_poster_url(home, away)
            if cached and cached.startswith(("http://", "https://")):
                url = cached
            else:
                logo1 = c.get("home_team_img") or poster_generator.resolve_team_logo(home, event_name, sport)
                logo2 = c.get("away_team_img") or poster_generator.resolve_team_logo(away, event_name, sport)
                img1 = download_logo_image(logo1)
                img2 = download_logo_image(logo2)
                if img1 is None and logo1:
                    img1 = download_logo_image(poster_generator.resolve_team_logo(home, event_name, sport))
                if img2 is None and logo2:
                    img2 = download_logo_image(poster_generator.resolve_team_logo(away, event_name, sport))
                poster_generator.create_event_poster(home, away, img1, img2, event_name, sport, "", absolute)
                user = os.getenv("NOMEGITHUB", "").strip()
                repo = os.getenv("NOMEREPO", "").strip()
                if user and repo:
                    relative = poster_generator.poster_relative_path(home, away)
                    url = f"https://raw.githubusercontent.com/{user}/{repo}/main/{myParse.quote(relative.replace(os.sep, '/'), safe='/')}"
        except Exception as e:
            print(f"[!] Poster fallito per {home} vs {away}: {e}")
            url = ""
        if url and not url.startswith(("http://", "https://")):
            url = ""
        poster_cache[key] = url
        return url

    m3u_content = f'#EXTM3U url-tvg="{EPG_URLS}"\n'
    
    total_channels = 0
    total_channels_ita = 0
    total_channels_eng = 0
    
    sports_channels = client.get_sports()
    if sports_channels:
        print(f"[+] Trovati {len(sports_channels)} eventi sportivi totali")
        
        code_stats = {}
        for c in sports_channels:
            code = str(c.get("code", "")).strip().lower()
            code_stats[code] = code_stats.get(code, 0) + 1
        print(f"[*] Statistiche codici canali: {code_stats}")
        
        # Canali ITA (filtrati per codice it e orario)
        for c in sports_channels:
            match_info = c.get("match_info", "Unknown Match")
            channel_name = c.get("channel_name", "Unknown Channel")
            player_url = c.get("url", "")
            image = c.get("image", "")
            start_time = c.get("start", "")
            sport_cat = c.get("sport_category", "Sport")
            status = c.get("status", "")
            channel_code = str(c.get("code", "")).strip().lower()
            
            if channel_code == "it":
                print(f"    [*] Canale IT trovato: {channel_name} | stato={status} | url_presente={bool(player_url)} | start={start_time}")
            
            if not player_url or status == "offline":
                continue
            
            if channel_code != "it":
                continue
            
            # Filtra eventi passati (iniziati più di 2 ore fa)
            if start_time:
                try:
                    event_time = None
                    for fmt in ["%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%H:%M"]:
                        try:
                            if fmt == "%H:%M":
                                today = datetime.now().strftime("%Y-%m-%d")
                                event_time = datetime.strptime(f"{today} {start_time}", "%Y-%m-%d %H:%M")
                            else:
                                event_time = datetime.strptime(start_time, fmt)
                            break
                        except:
                            continue
                    
                    if event_time:
                        cutoff_time = datetime.now() - timedelta(hours=2)
                        if event_time < cutoff_time:
                            continue
                except:
                    pass
            
            # Usa l'URL direttamente
            stream_url = player_url
            
            if not stream_url:
                print(f"    [!] Nessun URL trovato per {channel_name}")
                continue
            
            # Costruisci il nome con categoria sport
            display_name = f"[{sport_cat}] {start_time} - {match_info} ({channel_name})"
            display_name = display_name.replace(",", " ").replace('"', "'")
            
            tvg_id = channel_name.replace(" ", ".") if channel_name else ""
            poster_url = get_poster(c)
            logo_url = poster_url or image
            logo_attr = f'tvg-logo="{logo_url}"' if logo_url else 'tvg-logo=""'
            
            m3u_content += f'#EXTINF:-1 tvg-id="{tvg_id}" group-title="Eventi Live Sports99" {logo_attr},{display_name}\n'
            m3u_content += f'{stream_url}\n'
            total_channels += 1
            total_channels_ita += 1
            print(f"    [+] Aggiunto ITA: {channel_name}")

        # Canali ENG (nessun filtro su paese o orario, solo online)
        for c in sports_channels:
            match_info = c.get("match_info", "Unknown Match")
            channel_name = c.get("channel_name", "Unknown Channel")
            player_url = c.get("url", "")
            image = c.get("image", "")
            start_time = c.get("start", "")
            sport_cat = c.get("sport_category", "Sport")
            status = c.get("status", "")
            
            if not player_url or status == "offline":
                continue
            
            stream_url = player_url
            
            if not stream_url:
                print(f"    [!] Nessun URL trovato per {channel_name} (ENG)")
                continue
            
            display_name = f"[{sport_cat}] {start_time} - {match_info} ({channel_name})"
            display_name = display_name.replace(",", " ").replace('"', "'")
            
            tvg_id = channel_name.replace(" ", ".") if channel_name else ""
            poster_url = get_poster(c)
            logo_url = poster_url or image
            logo_attr = f'tvg-logo="{logo_url}"' if logo_url else 'tvg-logo=""'
            
            m3u_content += f'#EXTINF:-1 tvg-id="{tvg_id}" group-title="Eventi Live SPORTS99 ENG" {logo_attr},{display_name}\n'
            m3u_content += f'{stream_url}\n'
            total_channels += 1
            total_channels_eng += 1
            print(f"    [+] Aggiunto ENG: {channel_name}")
    else:
        print("[!] Nessun evento sportivo trovato")
    
    if total_channels_ita == 0:
        print("[!] Nessun canale italiano trovato!")
    
    if total_channels == 0:
        print("[!] Nessun canale trovato!")
        return ""
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(m3u_content)
    
    print()
    print(f"[+] Playlist salvata in: {output_file}")
    print(f"[+] Canali italiani aggiunti: {total_channels_ita}")
    print(f"[+] Canali ENG aggiunti: {total_channels_eng}")
    print("=" * 60)
    print("COMPLETATO!")
    print("=" * 60)
    
    return m3u_content


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Genera una playlist M3U da Sports99 (solo eventi sportivi ITA)')
    parser.add_argument('-o', '--output', default=OUTPUT_FILE, help=f'File di output (default: {OUTPUT_FILE})')
    parser.add_argument('-u', '--user', default=USER, help=f'Username API (default: {USER})')
    parser.add_argument('-p', '--plan', default=PLAN, help=f'Piano API (default: {PLAN})')
    
    args = parser.parse_args()
    
    USER = args.user
    PLAN = args.plan
    generate_m3u(args.output)
