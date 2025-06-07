import requests
import time
import os
from dotenv import load_dotenv
from flask import Flask
from playwright.sync_api import sync_playwright
import urllib.parse
from datetime import date, datetime

# === LOAD .env ===
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")

# === SETTINGS ===
SEASON = 21
GATEWAY = 20
MATCHES_TO_FETCH = 100
MATCHES_TO_ANALYZE = 10
MATCHES_FROM_SITE = 5

app = Flask(__name__)

# === GLOBALS ===
last_posted_date = None

# === FUNCTIONS ===


def load_players(filename):
    with open(filename, "r", encoding="utf-8") as f:
        players = [line.strip() for line in f if line.strip()]
    return players


def normalize_player_id(player_id):
    """Автоматически получает правильный BattleTag (регистр и т.п.)."""
    try:
        search_name = player_id.split("#")[0]  # только ник без #
        url = f"https://website-backend.w3champions.com/api/players/search?search={urllib.parse.quote(search_name)}"
        response = requests.get(url)
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
        url = f"https://website-backend.w3champions.com/api/matches/search?playerId={player_id_encoded}&gateway={GATEWAY}&offset=0&pageSize={MATCHES_TO_FETCH}&season={SEASON}"

        response = requests.get(url)
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

        for team in match['teams']:
            for player in team['players']:
                if player['battleTag'] == player_id:
                    player_team = team
                else:
                    opponent_team = team

        if not player_team:
            continue

        if player_team['won']:
            win_count += 1
        else:
            lose_count += 1

        opponent_player = opponent_team['players'][
            0] if opponent_team and opponent_team['players'] else None
        if opponent_player:
            race_map = {1: 'HU', 2: 'OR', 3: 'UD', 4: 'NE'}
            race = race_map.get(opponent_player['race'], 'UNK')
            result_icon = "❌" if not player_team['won'] else "✅"
            recent_opponents.append(
                f"- {opponent_player['battleTag']} ({race}) {result_icon}")

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
                    if "PlayerName--win" in result_class:
                        result = "✅ Победа"
                    elif "PlayerName--loss" in result_class:
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
        response = requests.post(url, json=payload)
        print(
            f"➡️ Telegram response part {idx+1}/{len(parts)}: {response.status_code}, {response.text}"
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

        full_message = f"🏆 <b>W3Champions Статистика игроков</b>\n📅 Сегодня: {today}\n\n"

        for player in players:
            print(f"🔄 Normalizing {player}...")
            normalized_player_id = normalize_player_id(player)

            print(f"🔄 Fetching stats for {normalized_player_id}...")
            matches_api = get_matches(normalized_player_id)
            win_count, lose_count, winrate, recent_opponents = analyze_matches(
                matches_api, normalized_player_id)

            site_matches = parse_site_matches(normalized_player_id)

            msg = build_player_message(normalized_player_id, win_count,
                                       lose_count, winrate, recent_opponents,
                                       site_matches)

            full_message += msg
            full_message += "—" * 30 + "\n"

            time.sleep(2)

        safe_send_to_telegram(full_message)

        last_posted_date = today
        print("✅ Telegram post complete.")
        return "✅ Bot run success", 200

    except Exception as e:
        print(f"❌ Error in /run: {e}")
        return f"❌ Error in /run: {e}", 500


# === MAIN ===

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
