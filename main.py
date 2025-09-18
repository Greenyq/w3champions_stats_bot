import os
import re
import json
import time
import html
import urllib.parse
import requests
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask
from playwright.sync_api import sync_playwright

# === LOAD .env ===
load_dotenv()

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")

# Discord (webhook)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_WEBHOOK_USERNAME = os.getenv("DISCORD_WEBHOOK_USERNAME", "WC3 Stats")
DISCORD_DISABLE = os.getenv("DISCORD_DISABLE",
                            "0")  # "1" чтобы временно отключить постинг

# === SETTINGS ===
SEASON = int(os.getenv("SEASON", 22))
GATEWAY = int(os.getenv("GATEWAY", 20))
MATCHES_TO_FETCH = int(os.getenv("MATCHES_TO_FETCH", 100))
MATCHES_TO_ANALYZE = int(os.getenv("MATCHES_TO_ANALYZE", 10))
MATCHES_FROM_SITE = int(os.getenv("MATCHES_FROM_SITE", 5))

app = Flask(__name__)

# === GLOBALS ===
last_posted_date = None


# === UTIL ===
def html_to_discord_md(s: str) -> str:
    """Грубая конвертация HTML -> markdown для Discord."""
    if not s:
        return s
    s = s.replace("<b>", "**").replace("</b>", "**")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)  # убрать прочие теги
    s = html.unescape(s)
    return s.strip()


def make_player_embed(title: str,
                      description: str,
                      url: str | None = None,
                      color: int = 0xF1C40F):
    """Сборка одного embed. Discord лимит ~4096 символов на description."""
    if description and len(description) > 4000:
        description = description[:3995] + "…"
    embed = {
        "title": title or "Update",
        "description": description or "\u200b",
        "timestamp": datetime.utcnow().isoformat(),
        "color": color,
        "footer": {
            "text": "W3Champions AutoFeed"
        },
    }
    if url:
        embed["url"] = url
    return embed


def send_discord_embeds(embeds, username=None):
    """Отправка порцией (до 10 embed за один POST) с экспоненциальным бэкоффом и распознаванием Cloudflare."""
    if DISCORD_DISABLE == "1":
        print("🔕 Discord disabled via DISCORD_DISABLE=1")
        return 204, "Discord disabled"
    if not DISCORD_WEBHOOK_URL:
        return 400, "DISCORD_WEBHOOK_URL is not set"
    if not embeds:
        return 204, "No embeds to send"

    payload = {
        "username": username or DISCORD_WEBHOOK_USERNAME,
        "embeds": embeds[:10],  # максимум 10 embed в одном запросе
    }
    headers = {"Content-Type": "application/json"}

    backoff = 1.0
    for attempt in range(5):  # до 5 попыток
        r = requests.post(DISCORD_WEBHOOK_URL,
                          headers=headers,
                          data=json.dumps(payload),
                          timeout=20)
        if r.status_code in (200, 204):
            return r.status_code, "OK"

        body = (r.text or "")[:600].lower()
        if r.status_code == 429:
            # уважать retry_after от Discord
            try:
                retry_after = float(r.json().get("retry_after", backoff))
            except Exception:
                retry_after = backoff
            sleep_for = retry_after + 0.2 * attempt
            print(f"⏳ Discord 429 rate limited. Sleep {sleep_for:.2f}s")
            time.sleep(sleep_for)
            continue

        if r.status_code in (403, 503) and ("cloudflare" in body
                                            or "access denied" in body):
            # Cloudflare бан исходящего IP — отступаемся и пробуем с бэкоффом
            print("⚠️ Cloudflare block detected for discord.com. Backing off…")

        # экспоненциальный бэкофф + лёгкий джиттер
        sleep_for = backoff + 0.2 * attempt
        print(
            f"⏳ Discord error {r.status_code}. Sleep {sleep_for:.2f}s and retry…"
        )
        time.sleep(sleep_for)
        backoff = min(backoff * 2, 8.0)

    return r.status_code, (r.text or "")[:600]


# === DATA FUNCTIONS ===
def load_players(filename):
    with open(filename, "r", encoding="utf-8") as f:
        players = [line.strip() for line in f if line.strip()]
    return players


def normalize_player_id(player_id):
    """Автоматически получает правильный BattleTag (регистр и т.п.)."""
    try:
        search_name = player_id.split("#")[0]  # только ник без #
        url = f"https://website-backend.w3champions.com/api/players/search?search={urllib.parse.quote(search_name)}"
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()

        players = data.get("players", [])
        for player in players:
            if player.get("battleTag",
                          "").endswith("#" + player_id.split("#")[1]):
                correct_battleTag = player.get("battleTag")
                print(f"✅ Normalized {player_id} -> {correct_battleTag}")
                return correct_battleTag

        print(f"⚠️ Could not normalize {player_id}, using as is.")
        return player_id
    except Exception as e:
        print(f"⚠️ Error normalizing {player_id}: {e}")
        return player_id


def get_matches(player_id):
    try:
        player_id_encoded = player_id.replace("#", "%23")
        url = (
            f"https://website-backend.w3champions.com/api/matches/search"
            f"?playerId={player_id_encoded}&gateway={GATEWAY}&offset=0&pageSize={MATCHES_TO_FETCH}&season={SEASON}"
        )
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        return data.get('matches', [])
    except Exception as e:
        print(f"⚠️ API error for {player_id}: {e}")
        return []


def analyze_matches(matches, player_id):
    win_count = 0
    lose_count = 0
    recent_opponents = []

    for match in matches[:MATCHES_TO_ANALYZE]:
        player_team = None
        opponent_team = None

        for team in match.get('teams', []):
            for player in team.get('players', []):
                if player.get('battleTag') == player_id:
                    player_team = team
                else:
                    opponent_team = team

        if not player_team:
            continue

        if player_team.get('won'):
            win_count += 1
        else:
            lose_count += 1

        opponent_player = opponent_team['players'][
            0] if opponent_team and opponent_team.get('players') else None
        if opponent_player:
            race_map = {1: 'HU', 2: 'OR', 3: 'UD', 4: 'NE'}
            race = race_map.get(opponent_player.get('race'), 'UNK')
            result_icon = "❌" if not player_team.get('won') else "✅"
            recent_opponents.append(
                f"- {opponent_player.get('battleTag')} ({race}) {result_icon}")

    total = win_count + lose_count
    winrate = (win_count / total) * 100 if total > 0 else 0.0
    return win_count, lose_count, winrate, recent_opponents


def parse_site_matches(player_id):
    BASE_URL = "https://www.w3champions.com/player"
    player_id_encoded = urllib.parse.quote(player_id)
    url = f"{BASE_URL}/{player_id_encoded}/matches"

    print(f"🌐 Fetching site matches: {url}")
    matches = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url)
            page.wait_for_selector("table.MuiTable-root tbody tr",
                                   timeout=15000)
            rows = page.query_selector_all("table.MuiTable-root tbody tr")

            for row in rows[:MATCHES_FROM_SITE]:
                cols = row.query_selector_all("td")
                if len(cols) < 6:
                    continue

                map_name = cols[0].inner_text().strip()
                matchup = cols[2].inner_text().strip()

                result_span = cols[3].query_selector("span")
                if result_span:
                    result_class = result_span.get_attribute("class")
                    if "PlayerName--win" in (result_class or ""):
                        result = "✅ Победа"
                    elif "PlayerName--loss" in (result_class or ""):
                        result = "❌ Поражение"
                    else:
                        result = "?"
                else:
                    result = "?"

                duration = cols[4].inner_text().strip()
                date_str = cols[5].inner_text().strip()

                match_info = f"- {date_str} — {map_name} — {matchup} — {result} ({duration})"
                matches.append(match_info)

            browser.close()
    except Exception as e:
        print(f"⚠️ Site parse error for {player_id}: {e}")
        return []

    return matches


def build_player_message(player_id, win_count, lose_count, winrate,
                         recent_opponents, site_matches):
    msg = f"📊 <b>Статистика {player_id} (Season {SEASON})</b>\n"
    msg += f"✅ Побед: {win_count}\n"
    msg += f"❌ Поражений: {lose_count}\n"
    msg += f"🏆 Winrate: {winrate:.1f}%\n\n"

    msg += "<b>Последние оппоненты:</b>\n"
    if recent_opponents:
        msg += "\n".join(recent_opponents[:3]) + "\n\n"
    else:
        msg += "Нет данных\n\n"

    msg += "<b>Последние 5 матчей:</b>\n"
    if site_matches:
        msg += "\n".join(site_matches) + "\n"
    else:
        msg += "Нет данных\n"

    msg += "\n"
    return msg


def safe_send_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    MAX_LENGTH = 4000  # немного меньше 4096, с запасом
    parts = [text[i:i + MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
    for idx, part in enumerate(parts):
        payload = {
            "chat_id": TELEGRAM_CHANNEL,
            "text": part,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=20)
        print(
            f"➡️ Telegram response part {idx+1}/{len(parts)}: {response.status_code}, {response.text[:200]}"
        )
        time.sleep(1)


# === Flask routes ===
@app.route('/')
def home():
    return "W3Champions Bot is running."


@app.route('/run')
def run():
    global last_posted_date
    today = date.today()
    print(f"=== BOT STARTED AT {datetime.now()} ===")

    if last_posted_date == today:
        print("⏱ Already sent today.")
        return '⏱ Already sent today', 200

    try:
        players = load_players("players.txt")

        # Заголовок для Telegram
        full_message = f"🏆 <b>W3Champions Статистика игроков</b>\n📅 Сегодня: {today}\n\n"

        # Копим embeds (потом отправим партиями по 10)
        all_embeds = []

        for player in players:
            print(f"🔄 Normalizing {player}...")
            normalized_player_id = normalize_player_id(player)

            print(f"🔄 Fetching stats for {normalized_player_id}...")
            matches_api = get_matches(normalized_player_id)
            win_count, lose_count, winrate, recent_opponents = analyze_matches(
                matches_api, normalized_player_id)

            site_matches = parse_site_matches(normalized_player_id)

            # Текст для Telegram
            msg = build_player_message(normalized_player_id, win_count,
                                       lose_count, winrate, recent_opponents,
                                       site_matches)
            full_message += msg + "—" * 30 + "\n"

            # Embed для Discord
            title = f"Статистика {normalized_player_id} (Season {SEASON})"
            desc = html_to_discord_md(msg)
            profile_url = f"https://www.w3champions.com/player/{urllib.parse.quote(normalized_player_id)}"
            all_embeds.append(make_player_embed(title, desc, url=profile_url))

            # Небольшая пауза, чтобы не долбить внешние API слишком часто
            time.sleep(0.3)

        # Отправляем в Discord партиями по 10 embed
        for i in range(0, len(all_embeds), 10):
            chunk = all_embeds[i:i + 10]
            dc_status, dc_resp = send_discord_embeds(chunk)
            print(f"Discord batch {i//10 + 1}: {dc_status} {dc_resp}")
            time.sleep(1.0)

        # Отправка в Telegram одним батчем
        if TELEGRAM_TOKEN and TELEGRAM_CHANNEL:
            safe_send_to_telegram(full_message)
        else:
            print("ℹ️ Telegram disabled or not configured.")

        last_posted_date = today
        print("✅ Posted to Telegram and Discord.")
        return "✅ Bot run success", 200

    except Exception as e:
        print(f"❌ Error in /run: {e}")
        return f"❌ Error in /run: {e}", 500


# === MAIN ===
if __name__ == "__main__":
    # Для Render: PORT приходит из env
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
