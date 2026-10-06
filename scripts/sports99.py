#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import json
import requests
import urllib.parse as myParse
from typing import Optional, Dict, List
from datetime import datetime, timedelta

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
            logo_attr = f'tvg-logo="{image}"' if image else 'tvg-logo=""'
            
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
            logo_attr = f'tvg-logo="{image}"' if image else 'tvg-logo=""'
            
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
