import os
import re
import sys
import time
import json
import base64
import html
import logging
import threading
import datetime
import urllib.parse
from io import BytesIO
import requests
from flask import Flask, jsonify

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("HermesCloudBot")

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8905133975:AAHjwARgwjIOMeoO522zT3NjmnHKhgtcy2M")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-n7hCiWdN4Tok6tDSBg7WEvqbZmqhBMqbjH5H4oSSaSiS4ade")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://apithat.dev/v1")
ALLOWED_USERS_RAW = os.getenv("TELEGRAM_ALLOWED_USERS", "8322961603")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://hermes-bot-drl1.onrender.com")

SUBSCRIPTIONS_FILE = "scheduled_subscriptions.json"

ALLOWED_USERS = set()
for uid in ALLOWED_USERS_RAW.split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USERS.add(int(uid))

# In-memory data structures
conversation_history = {}
user_memories = {}        # {chat_id: ["sở thích...", "dự án..."]}
user_model_override = {}  # {chat_id: "gpt-5.6-sol" or None}
pending_reminders = []    # [{"chat_id": int, "due_time": float, "text": str}]
MAX_HISTORY_TURNS = 12

# City coordinates database for instant live weather lookup
CITY_COORDS = {
    "ninh bình": (20.25, 105.97),
    "hà nội": (21.03, 105.85),
    "hồ chí minh": (10.82, 106.63),
    "sài gòn": (10.82, 106.63),
    "đà nẵng": (16.05, 108.20),
    "hải phòng": (20.84, 106.68),
    "quảng ninh": (20.95, 107.07),
    "thanh hóa": (19.81, 105.77),
    "nam định": (20.42, 106.17),
    "huế": (16.46, 107.60),
    "nha trang": (12.24, 109.19),
    "đà lạt": (11.94, 108.44),
    "cần thơ": (10.04, 105.78),
    "hạ long": (20.95, 107.07),
    "sapa": (22.33, 103.84),
    "tam đảo": (21.46, 105.64)
}

# ==========================================================
# Persistent Subscriptions Database
# ==========================================================

def load_subscriptions():
    if os.path.exists(SUBSCRIPTIONS_FILE):
        try:
            with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading subscriptions: {e}")
    # Default initial subscription for user
    return {
        "8322961603": {
            "enabled": True,
            "location": "Ninh Bình",
            "morning_time": "07:00",
            "evening_time": "20:00",
            "last_morning_date": "",
            "last_evening_date": ""
        }
    }

def save_subscriptions(subs):
    try:
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(subs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving subscriptions: {e}")

subscriptions = load_subscriptions()

# Flask web app for Render Keep-Alive & Health Checks
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Hermes Telegram Super-Bot 24/7 (Ultimate Edition)",
        "default_frontier_model": "gpt-5.6-sol",
        "features": [
            "interactive_inline_buttons",
            "voice_audio_processing",
            "live_crypto_and_fx_rates",
            "persistent_daily_briefings",
            "morning_and_evening_cron",
            "live_realtime_weather",
            "date_time_grounding",
            "url_web_page_reader",
            "instant_emoji_reactions",
            "continuous_typing_feedback",
            "smart_model_router", 
            "vision_multimodal", 
            "file_reader", 
            "reminders_and_memory", 
            "24_7_long_polling"
        ],
        "active_subscribers": [k for k, v in subscriptions.items() if v.get("enabled")],
        "pending_reminders_count": len(pending_reminders),
        "timestamp": time.time()
    }), 200

# ==========================================================
# Real-Time Weather Integration (Open-Meteo & Wttr.in)
# ==========================================================

def get_live_weather(text):
    text_lower = text.lower()
    lat, lon = 20.25, 105.97 # default Ninh Bình
    location_name = "Ninh Bình"
    
    for city, coords in CITY_COORDS.items():
        if city in text_lower:
            lat, lon = coords
            location_name = city.title()
            break
            
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FBangkok"
        r = requests.get(url, timeout=6)
        data = r.json()
        curr = data["current"]
        daily = data["daily"]
        
        weather_report = (
            f"🌤️ [Dữ liệu Thời Tiết Khí Tượng Trực Tiếp - Trạm {location_name}]:\n"
            f"• Nhiệt độ hiện tại: {curr['temperature_2m']}°C (Cảm nhận thực tế: {curr['apparent_temperature']}°C)\n"
            f"• Độ ẩm không khí: {curr['relative_humidity_2m']}% | Tốc độ gió: {curr['wind_speed_10m']} km/h\n"
            f"• Nhiệt độ hôm nay: Thấp nhất {daily['temperature_2m_min'][0]}°C — Cao nhất {daily['temperature_2m_max'][0]}°C\n"
            f"• Xác suất mưa hôm nay: {daily['precipitation_probability_max'][0]}%\n"
            f"• Dự báo 4 ngày tiếp theo (gồm dịp 2/9):\n"
        )
        for i in range(1, min(5, len(daily["time"]))):
            weather_report += f"  - Ngày {daily['time'][i]}: {daily['temperature_2m_min'][i]}°C - {daily['temperature_2m_max'][i]}°C (Xác suất mưa: {daily['precipitation_probability_max'][i]}%)\n"
            
        return weather_report
    except Exception as e:
        logger.error(f"Weather error: {e}")
        return ""

def is_weather_query(text):
    keywords = ["thời tiết", "trời mưa", "có mưa không", "nhiệt độ", "dự báo", "nắng", "gió", "2/9 thời tiết", "thời tiết hôm nay", "thời tiết ngày mai"]
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)

# ==========================================================
# Real-Time Financial & Crypto Rates
# ==========================================================

def get_live_rates():
    results = []
    try:
        r = requests.get('https://open.er-api.com/v6/latest/USD', timeout=5)
        rates = r.json().get('rates', {})
        usd_vnd = rates.get('VND', 0)
        eur_usd = rates.get('EUR', 1)
        eur_vnd = (usd_vnd / eur_usd) if eur_usd else 0
        jpy_usd = rates.get('JPY', 1)
        jpy_vnd = (usd_vnd / jpy_usd) if jpy_usd else 0
        results.append(
            f"💵 **TỶ GIÁ NGOẠI TỆ (REAL-TIME):**\n"
            f"• 1 USD = **{usd_vnd:,.0f} VND**\n"
            f"• 1 EUR = **{eur_vnd:,.0f} VND**\n"
            f"• 100 JPY = **{(jpy_vnd*100):,.0f} VND**"
        )
    except Exception as e:
        logger.warning(f"FX error: {e}")
        
    try:
        crypto_lines = []
        for sym, name in [('BTCUSDT', 'Bitcoin (BTC)'), ('ETHUSDT', 'Ethereum (ETH)'), ('SOLUSDT', 'Solana (SOL)'), ('BNBUSDT', 'Binance Coin (BNB)')]:
            r = requests.get(f'https://data-api.binance.vision/api/v3/ticker/price?symbol={sym}', timeout=4)
            p = float(r.json().get('price', 0))
            crypto_lines.append(f"• {name}: **${p:,.2f}**")
        results.append("🪙 **THỊ TRƯỜNG TIỀN MÃ HÓA (CRYPTO 24/7):**\n" + "\n".join(crypto_lines))
    except Exception as e:
        logger.warning(f"Crypto error: {e}")
        
    return "\n\n".join(results)

def is_rates_query(text):
    keywords = ["tỷ giá", "giá usd", "giá euro", "giá vàng", "giá btc", "giá bitcoin", "giá eth", "giá coin", "crypto", "tiền tệ", "usd vnd"]
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)

# ==========================================================
# Autonomous Daily Briefing Generators
# ==========================================================

def generate_briefing(briefing_type="morning", location="Ninh Bình", chat_id=None):
    now_vn = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    time_str = now_vn.strftime("%A, ngày %d/%m/%Y")
    weather_info = get_live_weather(location)
    rates_info = get_live_rates()
    
    mem_info = ""
    if chat_id and chat_id in user_memories and user_memories[chat_id]:
        mem_info = "\nGhi chú/việc cần lưu ý của người dùng:\n" + "\n".join([f"- {m}" for m in user_memories[chat_id]])
        
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    if briefing_type == "morning":
        prompt = (
            f"Bạn là Hermes - Siêu Trợ Lý AI cao cấp.\n"
            f"Hãy soạn một **BẢN TIN TỔNG HỢP BUỔI SÁNG** ({time_str}) thật chuyên nghiệp, năng lượng và tinh tế cho người dùng tại {location}.\n"
            f"Dữ liệu thời tiết thực tế:\n{weather_info}\n\n"
            f"Dữ liệu thị trường tài chính:\n{rates_info}\n{mem_info}\n\n"
            "Cấu trúc bản tin gồm:\n"
            "1. 🌅 **Lời chào buổi sáng & Ngày tháng**\n"
            "2. 🌤️ **Dự báo Thời Tiết Trong Ngày** (Nhiệt độ, xác suất mưa, lời khuyên trang phục/hoạt động)\n"
            "3. 💰 **Điểm Nhanh Tài Chính & Thị Trường** (Tỷ giá USD, Giá Bitcoin nổi bật)\n"
            "4. 📰 **Điểm Tin Nhanh & Xu Hướng Trong Ngày** (Công nghệ AI, đời sống số, mẹo năng suất)\n"
            "5. 💡 **Lời chúc ngày mới & Câu nói truyền cảm hứng**\n"
            "Trình bày chuẩn Markdown, icon sinh động, bố cục thoáng đẹp mắt trên Telegram."
        )
    else:
        prompt = (
            f"Bạn là Hermes - Siêu Trợ Lý AI cao cấp.\n"
            f"Hãy soạn một **BẢN TIN TỔNG KẾT BUỔI TỐI** ({time_str}) thật ấm áp, thư giãn và hữu ích cho người dùng tại {location}.\n"
            f"Dữ liệu thời tiết:\n{weather_info}\n{mem_info}\n\n"
            "Cấu trúc bản tin gồm:\n"
            "1. 🌆 **Lời chào buổi tối & Lời chúc thư giãn sau một ngày làm việc**\n"
            "2. 🌙 **Dự Báo Thời Tiết & Lưu Ý Cho Ngày Mai**\n"
            "3. 📌 **Góc Nhìn & Ý Tưởng Tích Cực Trước Khi Nghỉ Ngơi**\n"
            "4. 🛌 **Lời chúc ngủ ngon & Nạp lại năng lượng**\n"
            "Trình bày chuẩn Markdown, icon ấm áp, bố cục dễ đọc."
        )
        
    for model in ["gpt-5.6-sol", "gpt-5.6-terra", "gemini-3.7-flash"]:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7
            }
            r = requests.post(f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions", headers=headers, json=payload, timeout=45)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"Error generating briefing with {model}: {e}")
            
    return f"🌅 **[BẢN TIN TỔNG HỢP {time_str}]**\n\nChúc anh một ngày thật tuyệt vời và nhiều may mắn!"

# ==========================================================
# Telegram Visual Feedback Helpers (Reactions & Typing & Buttons)
# ==========================================================

def send_telegram_request(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=35)
        return r.json()
    except Exception as e:
        logger.error(f"Error calling Telegram {method}: {e}")
        return None

def set_message_reaction(chat_id, message_id, emoji="👀"):
    if not message_id:
        return
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": [{"type": "emoji", "emoji": emoji}]
    }
    send_telegram_request("setMessageReaction", payload)

def send_chat_action(chat_id, action="typing"):
    send_telegram_request("sendChatAction", {"chat_id": chat_id, "action": action})

class TypingKeeper:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.running = True
        self.thread = None

    def _loop(self):
        while self.running:
            try:
                send_chat_action(self.chat_id, "typing")
            except Exception:
                pass
            time.sleep(3.5)

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

def send_message(chat_id, text, reply_to_message_id=None, reply_markup=None):
    chunk_size = 4000
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        if reply_to_message_id and i == 0:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup and (i + chunk_size >= len(text)):
            payload["reply_markup"] = reply_markup
        res = send_telegram_request("sendMessage", payload)
        if not res or not res.get("ok"):
            payload.pop("parse_mode", None)
            send_telegram_request("sendMessage", payload)

def answer_callback_query(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    send_telegram_request("answerCallbackQuery", payload)

def download_telegram_file(file_id):
    file_info = send_telegram_request("getFile", {"file_id": file_id})
    if not file_info or not file_info.get("ok"):
        return None, ""
    file_path = file_info["result"]["file_path"]
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    r = requests.get(download_url, timeout=60)
    if r.status_code == 200:
        return r.content, file_path
    return None, ""

def get_main_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🌅 Bản Tin Sáng", "callback_data": "btn_briefing_morning"},
                {"text": "🌆 Bản Tin Tối", "callback_data": "btn_briefing_evening"}
            ],
            [
                {"text": "🌤️ Thời Tiết Ninh Bình", "callback_data": "btn_weather"},
                {"text": "💰 Tỷ Giá & Crypto", "callback_data": "btn_rates"}
            ],
            [
                {"text": "🧠 Chọn Model AI", "callback_data": "btn_models"},
                {"text": "🔄 Làm Mới Chat", "callback_data": "btn_reset"}
            ]
        ]
    }

def get_model_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "✨ Tự Động (Smart Router)", "callback_data": "setmodel_auto"},
                {"text": "🧠 GPT-5.6 Sol (Mạnh nhất)", "callback_data": "setmodel_sol"}
            ],
            [
                {"text": "💻 GPT-5.3 Codex (Code)", "callback_data": "setmodel_code"},
                {"text": "⚡ GPT-5.6 Terra (1s Siêu tốc)", "callback_data": "setmodel_terra"}
            ],
            [
                {"text": "🖋️ Claude Sonnet 4.6", "callback_data": "setmodel_claude"},
                {"text": "🔙 Quay Lại Menu", "callback_data": "btn_menu_back"}
            ]
        ]
    }

# ==========================================================
# Feature: URL Web Page Reader
# ==========================================================

def fetch_url_content(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            clean_html = re.sub(r'<script[^>]*>.*?</script>', '', r.text, flags=re.DOTALL | re.IGNORECASE)
            clean_html = re.sub(r'<style[^>]*>.*?</style>', '', clean_html, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r'<[^>]+>', ' ', clean_html)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            return html.unescape(clean_text)[:8000]
    except Exception as e:
        logger.error(f"Error fetching URL {url}: {e}")
    return ""

# ==========================================================
# Feature: Intelligent Dynamic Model Router
# ==========================================================

def select_model_for_task(text, has_photo=False, has_doc=False, has_audio=False, file_name="", chat_id=None):
    if chat_id and str(chat_id) in user_model_override and user_model_override[str(chat_id)]:
        override = user_model_override[str(chat_id)]
        return override, f"⚙️ [Chế độ Cố định: {override}]"

    if has_photo or has_audio:
        return "gpt-5.6-sol", "👁️ [Phân tích Đa phương tiện - GPT-5.6 Sol]"

    if has_doc:
        ext = os.path.splitext(file_name)[1].lower()
        if ext in [".py", ".java", ".js", ".ts", ".cpp", ".c", ".cs", ".php", ".html", ".css", ".sql", ".sh", ".json", ".jar"]:
            return "gpt-5.3-codex-spark", "💻 [Phân tích Mã nguồn - GPT-5.3 Codex]"
        return "gpt-5.6-sol", "📄 [Phân tích Tài liệu - GPT-5.6 Sol]"

    text_lower = text.lower()

    code_keywords = [
        "viết code", "sửa code", "fix bug", "lỗi code", "hàm", "function", "class", 
        "python", "java", "javascript", "script", "database", "sql", "api", "thuật toán",
        "debug", "source code", "html", "css", "c++", "c#", "nso", "jar", "mod",
        "compile", "syntax", "endpoint", "regex", "lập trình", "viết bot"
    ]
    has_code_syntax = bool(re.search(r'```|def\s+\w+|class\s+\w+|import\s+\w+|function\s*\(|public\s+static|SELECT\s+.*FROM', text))

    if any(kw in text_lower for kw in code_keywords) or has_code_syntax:
        return "gpt-5.3-codex-spark", "💻 [Chuyên gia Lập trình - GPT-5.3 Codex]"

    short_casual = ["chào", "hi", "hello", "alo", "ê", "bạn là ai", "test", "ok", "cảm ơn", "thanks", "tạm biệt", "bye"]
    if len(text.split()) <= 4 and any(w in text_lower for w in short_casual):
        return "gpt-5.6-terra", "⚡ [Hội thoại Siêu tốc - GPT-5.6 Terra]"

    return "gpt-5.6-sol", "🧠 [Siêu Trí Tuệ Suy luận - GPT-5.6 Sol]"

def get_fallback_chain(primary_model):
    pool = [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gemini-3.7-flash",
        "gpt-5.3-codex-spark",
        "claude-sonnet-4-6",
        "gpt-5.4-mini",
        "grok-4.5"
    ]
    return [primary_model] + [m for m in pool if m != primary_model]

# ==========================================================
# Anti-Sleep Keep-Alive Loop (Self-Ping every 5 mins)
# ==========================================================

def anti_sleep_keep_alive():
    logger.info("Anti-Sleep Keep-Alive loop started.")
    time.sleep(15)
    while True:
        try:
            url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/health"
            r = requests.get(url, timeout=15)
            logger.info(f"Keep-Alive ping to {url}: Status {r.status_code}")
        except Exception as e:
            logger.warning(f"Keep-Alive ping error: {e}")
        time.sleep(5 * 60)

# ==========================================================
# Feature: Daily Briefing Cron Loop & One-Off Reminders
# ==========================================================

def parse_reminder(text):
    text_lower = text.lower()
    now = time.time()
    
    rel_match = re.search(r'nhắc\s+(?:tôi|anh|em|mình)\s+sau\s+(\d+)\s*(phút|p|tiếng|giờ|h|giây|s)\s+(?:là\s+|để\s+|đi\s+)?(.+)', text_lower)
    if rel_match:
        val = int(rel_match.group(1))
        unit = rel_match.group(2)
        remind_content = rel_match.group(3).strip()
        
        delta = val * 60
        if unit in ["tiếng", "giờ", "h"]:
            delta = val * 3600
        elif unit in ["giây", "s"]:
            delta = val
            
        return now + delta, remind_content
        
    time_match = re.search(r'nhắc\s+(?:tôi|anh|em|mình)\s+lúc\s+(\d{1,2})[:h](\d{1,2})\s*(?:phút)?\s+(?:là\s+|để\s+|đi\s+)?(.+)', text_lower)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        remind_content = time_match.group(3).strip()
        
        now_dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
        target_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_dt.timestamp() <= now:
            target_dt += datetime.timedelta(days=1)
        return target_dt.timestamp(), remind_content

    return None

def reminder_scheduler_loop():
    logger.info("Unified Scheduler (Daily Briefings + Reminders) started.")
    while True:
        try:
            now_vn = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
            today_str = now_vn.strftime("%Y-%m-%d")
            hour_int = now_vn.hour
            minute_int = now_vn.minute
            
            # 1. Process Daily Briefing Subscriptions
            for chat_id_str, config in list(subscriptions.items()):
                if not config.get("enabled", True):
                    continue
                    
                chat_id = int(chat_id_str) if chat_id_str.isdigit() else chat_id_str
                location = config.get("location", "Ninh Bình")
                morning_time = config.get("morning_time", "07:00")
                evening_time = config.get("evening_time", "20:00")
                
                # Morning Briefing Trigger (07:00 or window)
                m_h, m_m = [int(x) for x in morning_time.split(":")]
                if (hour_int == m_h and minute_int >= m_m and minute_int <= m_m + 15) or (hour_int == m_h and config.get("last_morning_date") != today_str):
                    if config.get("last_morning_date") != today_str:
                        logger.info(f"Triggering Morning Briefing for {chat_id} at {today_str}")
                        briefing_msg = generate_briefing("morning", location, chat_id=chat_id)
                        send_message(chat_id, briefing_msg)
                        config["last_morning_date"] = today_str
                        save_subscriptions(subscriptions)
                        
                # Evening Briefing Trigger (20:00 or window)
                e_h, e_m = [int(x) for x in evening_time.split(":")]
                if (hour_int == e_h and minute_int >= e_m and minute_int <= e_m + 15) or (hour_int == e_h and config.get("last_evening_date") != today_str):
                    if config.get("last_evening_date") != today_str:
                        logger.info(f"Triggering Evening Briefing for {chat_id} at {today_str}")
                        briefing_msg = generate_briefing("evening", location, chat_id=chat_id)
                        send_message(chat_id, briefing_msg)
                        config["last_evening_date"] = today_str
                        save_subscriptions(subscriptions)

            # 2. Process One-off Reminders
            now = time.time()
            triggered = []
            for item in list(pending_reminders):
                if item["due_time"] <= now:
                    triggered.append(item)
                    pending_reminders.remove(item)
                    
            for item in triggered:
                logger.info(f"Triggering reminder for {item['chat_id']}: {item['text']}")
                send_message(
                    item["chat_id"], 
                    f"⏰ **[NHẮC NHỞ TỪ HERMES]**\n\nĐã đến giờ anh ơi: **{item['text']}**!"
                )
        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}", exc_info=True)
        time.sleep(20)

# ==========================================================
# Feature: File & Document Extraction
# ==========================================================

def extract_text_from_file(file_bytes, file_name):
    ext = os.path.splitext(file_name)[1].lower()
    if ext == ".pdf":
        if pypdf:
            try:
                reader = pypdf.PdfReader(BytesIO(file_bytes))
                text = ""
                for page in reader.pages[:20]:
                    text += page.extract_text() or ""
                return text[:15000]
            except Exception as e:
                return f"[Lỗi đọc file PDF: {e}]"
        return "[Thư viện pypdf chưa sẵn sàng]"
    elif ext in [".docx", ".doc"]:
        if docx:
            try:
                doc = docx.Document(BytesIO(file_bytes))
                return "\n".join([p.text for p in doc.paragraphs if p.text])[:15000]
            except Exception as e:
                return f"[Lỗi đọc file Word: {e}]"
        return "[Thư viện python-docx chưa sẵn sàng]"
    else:
        try:
            return file_bytes.decode("utf-8", errors="replace")[:20000]
        except Exception as e:
            return f"[Không thể đọc text: {e}]"

# ==========================================================
# Core Dynamic Multi-Model LLM Engine
# ==========================================================

def query_llm(chat_id, user_content, chosen_model="gpt-5.6-sol"):
    now_vn = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    time_str = now_vn.strftime("%A, ngày %d/%m/%Y, %H:%M:%S (GMT+7)")
    
    system_prompt = (
        f"Bạn là Hermes - siêu trợ lý AI toàn năng, chủ động, thông thái và am hiểu sâu sắc mọi lĩnh vực tại Việt Nam.\n"
        f"Thời gian hiện tại: {time_str}.\n\n"
        "NGUYÊN TẮC HÀNH ĐỘNG TUYỆT ĐỐI:\n"
        "1. Luôn trực tiếp trả lời, báo cáo và giải quyết yêu cầu của người dùng, không bao giờ đùn đẩy bảo người dùng tự tìm.\n"
        "2. Bạn đã được trang bị hệ thống Gửi Bản Tin Tổng Hợp Tự Động (Sáng 07:00 & Tối 20:00), tra cứu tỷ giá ngoại tệ, giá Crypto BTC/ETH, và dự báo thời tiết trực tiếp.\n"
        "3. Khi người dùng hỏi về địa điểm/quán ăn (ví dụ BBQ 1988), cung cấp đầy đủ thông tin: địa chỉ các cơ sở tại Ninh Bình (15 Lê Thánh Tông & 38 Đào Duy Từ), bảng giá vé buffet nướng lẩu, các món đặc sắc, hotline đặt bàn và giờ mở cửa.\n"
        "4. Tuyệt đối KHÔNG BAO GIỜ nói 'tôi không có công cụ duyệt web', 'không thể truy cập thời gian thực' hay từ chối."
    )
    
    if chat_id in user_memories and user_memories[chat_id]:
        mem_str = "\n".join([f"- {m}" for m in user_memories[chat_id]])
        system_prompt += f"\n\nThông tin đã ghi nhớ về người dùng:\n{mem_str}"

    if chat_id not in conversation_history:
        conversation_history[chat_id] = [{"role": "system", "content": system_prompt}]
    else:
        conversation_history[chat_id][0] = {"role": "system", "content": system_prompt}

    conversation_history[chat_id].append({"role": "user", "content": user_content})

    if len(conversation_history[chat_id]) > (MAX_HISTORY_TURNS * 2 + 1):
        sys_msg = conversation_history[chat_id][0]
        recent = conversation_history[chat_id][-(MAX_HISTORY_TURNS * 2):]
        conversation_history[chat_id] = [sys_msg] + recent

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    models_to_try = get_fallback_chain(chosen_model)

    for model in models_to_try:
        for attempt in range(2):
            try:
                payload = {
                    "model": model,
                    "messages": conversation_history[chat_id],
                    "temperature": 0.7
                }
                url = f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"
                r = requests.post(url, headers=headers, json=payload, timeout=60)
                
                if r.status_code == 200:
                    data = r.json()
                    assistant_reply = data["choices"][0]["message"]["content"]
                    conversation_history[chat_id].append({"role": "assistant", "content": assistant_reply})
                    return assistant_reply
                elif r.status_code in [503, 502, 504, 429]:
                    logger.warning(f"Model {model} returned {r.status_code}, retrying...")
                    time.sleep(1.5)
                else:
                    logger.warning(f"Model {model} failed with HTTP {r.status_code}: {r.text[:80]}")
                    break
            except Exception as e:
                logger.warning(f"Model {model} exception: {e}")
                time.sleep(1)

    return "⚠️ Máy chủ AI đang có lưu lượng truy cập cao đột biến. Anh vui lòng gửi lại tin nhắn sau vài giây nhé!"

# ==========================================================
# Telegram Update Handler & Callback Queries
# ==========================================================

def handle_callback_query(callback_query):
    cq_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    chat_id_str = str(chat_id)

    answer_callback_query(cq_id)
    if not chat_id:
        return

    if data == "btn_briefing_morning":
        send_message(chat_id, "⏳ Đang tổng hợp dữ liệu bản tin sáng...")
        msg = generate_briefing("morning", subscriptions.get(chat_id_str, {}).get("location", "Ninh Bình"), chat_id=chat_id)
        send_message(chat_id, msg)
    elif data == "btn_briefing_evening":
        send_message(chat_id, "⏳ Đang tổng hợp bản tin tối...")
        msg = generate_briefing("evening", subscriptions.get(chat_id_str, {}).get("location", "Ninh Bình"), chat_id=chat_id)
        send_message(chat_id, msg)
    elif data == "btn_weather":
        send_message(chat_id, "⏳ Đang kết nối trạm thời tiết Ninh Bình...")
        w = get_live_weather("thời tiết ninh bình")
        send_message(chat_id, w if w else "⚠️ Không thể lấy dữ liệu thời tiết.")
    elif data == "btn_rates":
        send_message(chat_id, "⏳ Đang tải tỷ giá ngoại tệ & giá Crypto...")
        r = get_live_rates()
        send_message(chat_id, r if r else "⚠️ Không thể lấy dữ liệu tỷ giá.")
    elif data == "btn_models":
        send_message(chat_id, "⚙️ **Chọn Não Bộ AI Cho Cuộc Trò Chuyện:**", reply_markup=get_model_menu_keyboard())
    elif data == "btn_reset":
        conversation_history.pop(chat_id, None)
        send_message(chat_id, "🔄 Đã làm mới ngữ cảnh hội thoại!", reply_markup=get_main_menu_keyboard())
    elif data == "btn_menu_back":
        send_message(chat_id, "👋 **Bảng Điều Khiển Nhanh Hermes:**", reply_markup=get_main_menu_keyboard())
    elif data.startswith("setmodel_"):
        m = data.replace("setmodel_", "")
        if m == "auto":
            user_model_override.pop(chat_id_str, None)
            send_message(chat_id, "✅ Đã bật chế độ **Tự Động Chọn Model Thông Minh (Smart Router)**!", reply_markup=get_main_menu_keyboard())
        elif m == "sol":
            user_model_override[chat_id_str] = "gpt-5.6-sol"
            send_message(chat_id, "✅ Đã cố định Model: **GPT-5.6 Sol** (Siêu suy luận)!", reply_markup=get_main_menu_keyboard())
        elif m == "code":
            user_model_override[chat_id_str] = "gpt-5.3-codex-spark"
            send_message(chat_id, "✅ Đã cố định Model: **GPT-5.3 Codex Spark** (Chuyên lập trình)!", reply_markup=get_main_menu_keyboard())
        elif m == "terra":
            user_model_override[chat_id_str] = "gpt-5.6-terra"
            send_message(chat_id, "✅ Đã cố định Model: **GPT-5.6 Terra** (Siêu tốc & Tiết kiệm)!", reply_markup=get_main_menu_keyboard())
        elif m == "claude":
            user_model_override[chat_id_str] = "claude-sonnet-4-6"
            send_message(chat_id, "✅ Đã cố định Model: **Claude Sonnet 4.6**!", reply_markup=get_main_menu_keyboard())

def handle_update(update):
    if "callback_query" in update:
        handle_callback_query(update["callback_query"])
        return

    message = update.get("message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user_id = message.get("from", {}).get("id")
    message_id = message.get("message_id")

    if ALLOWED_USERS and user_id not in ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized access by {user_id}")
        send_message(chat_id, "⛔ Bạn không có quyền sử dụng bot cá nhân này.")
        return

    text = message.get("text", "").strip()
    caption = message.get("caption", "").strip()
    photos = message.get("photo")
    document = message.get("document")
    voice = message.get("voice") or message.get("audio")

    # Instant acknowledgement: Add '👀' reaction immediately
    try:
        set_message_reaction(chat_id, message_id, "👀")
    except Exception as e:
        logger.warning(f"Failed to set reaction: {e}")

    # Continuous typing indicator in header
    typing_keeper = TypingKeeper(chat_id)
    typing_keeper.start()

    try:
        # Command: /start
        if text == "/start" or text == "/menu":
            welcome = (
                "👋 **Chào anh! Em là Hermes AI Siêu Trợ Lý (Ultimate Edition 24/7)!**\n\n"
                "🧠 **Các Tính Năng Thông Minh Toàn Năng:**\n"
                "• 📰 **Bản Tin Tự Động Sáng (07:00) & Tối (20:00):** Tự động tổng hợp thời tiết, điểm tin, lời khuyên ngày mới.\n"
                "• 💰 **Tỷ Giá Ngoại Tệ & Crypto:** Cập nhật giá USD, EUR, BTC, ETH theo thời gian thực.\n"
                "• 🌤️ **Khí Tượng Thời Tiết Trực Tiếp:** Báo cáo nhiệt độ, xác suất mưa mọi tỉnh thành.\n"
                "• 🎙️ **Nghe Tin Nhắn Thoại (Voice):** Gửi voice note để bot nghe và giải đáp.\n"
                "• 🥩 **Tra Cứu Ẩm Thực & Địa Điểm:** Báo giá buffet, menu quán ăn chi tiết.\n"
                "• 💻 **Lập Trình Chuyên Sâu:** Tự động dùng **`GPT-5.3 Codex`** khi hỏi code.\n"
                "• 👁️ **Mắt Thần Nhìn Ảnh:** Gửi ảnh để bot phân tích.\n"
                "• 📄 **Đọc File PDF, Word, Code & Đọc Link Web.**\n\n"
                "👇 **Anh có thể chạm nhanh các nút bên dưới để trải nghiệm ngay:**"
            )
            send_message(chat_id, welcome, reply_to_message_id=message_id, reply_markup=get_main_menu_keyboard())
            return

        # Command: /help
        if text == "/help":
            help_text = (
                "📖 **DANH SÁCH LỆNH VÀ HƯỚNG DẪN TIẾNG VIỆT**\n\n"
                "1. 🔘 **/menu** — Mở bảng nút điều khiển tương tác nhanh.\n"
                "2. 📰 **/daily [on | off | now | time]** — Quản lý bản tin sáng & tối:\n"
                "   • `/daily on` : Bật gửi bản tin tự động (Sáng 07:00 & Tối 20:00).\n"
                "   • `/daily now` : Gửi ngay 1 bản tin mẫu tức thì.\n"
                "   • `/daily off` : Tắt nhận bản tin.\n"
                "   • `/daily time 06:30 21:00` : Đổi giờ gửi sáng và tối.\n"
                "3. 🚀 **/start** — Khởi động và xem thông tin bot.\n"
                "4. 🔄 **/reset** — Làm mới cuộc trò chuyện, xóa ngữ cảnh cũ.\n"
                "5. 🧠 **/memo** — Xem các thông tin cá nhân/sở thích bot đang nhớ.\n"
                "6. ⚙️ **/model [auto | sol | code | claude | terra]** — Đổi model AI."
            )
            send_message(chat_id, help_text, reply_to_message_id=message_id, reply_markup=get_main_menu_keyboard())
            return

        # Command: /daily (Quản lý Bản Tin Sáng & Tối)
        if text.startswith("/daily"):
            parts = text.split()
            chat_id_str = str(chat_id)
            if chat_id_str not in subscriptions:
                subscriptions[chat_id_str] = {
                    "enabled": True,
                    "location": "Ninh Bình",
                    "morning_time": "07:00",
                    "evening_time": "20:00",
                    "last_morning_date": "",
                    "last_evening_date": ""
                }
                
            if len(parts) > 1:
                action = parts[1].lower()
                if action in ["on", "enable", "bat", "bật"]:
                    subscriptions[chat_id_str]["enabled"] = True
                    save_subscriptions(subscriptions)
                    send_message(chat_id, "✅ **Đã BẬT lịch gửi Bản Tin Tổng Hợp Hàng Ngày!**\n\n🕒 **Lịch trình cố định:**\n• 🌅 **Bản tin sáng:** 07:00 sáng\n• 🌆 **Bản tin tối:** 20:00 tối\n\nEm sẽ tự động gửi đúng giờ mỗi ngày cho anh nhé!", reply_to_message_id=message_id)
                elif action in ["off", "disable", "tat", "tắt"]:
                    subscriptions[chat_id_str]["enabled"] = False
                    save_subscriptions(subscriptions)
                    send_message(chat_id, "⏸️ Đã tắt nhận bản tin tự động hàng ngày.", reply_to_message_id=message_id)
                elif action in ["now", "test", "mau", "mẫu"]:
                    send_message(chat_id, "⏳ Đang tổng hợp dữ liệu thời tiết, tỷ giá & tin tức để tạo bản tin cho anh...")
                    briefing = generate_briefing("morning", subscriptions[chat_id_str].get("location", "Ninh Bình"), chat_id=chat_id)
                    send_message(chat_id, briefing, reply_to_message_id=message_id)
                elif action in ["time", "gio", "giờ"] and len(parts) >= 4:
                    subscriptions[chat_id_str]["morning_time"] = parts[2]
                    subscriptions[chat_id_str]["evening_time"] = parts[3]
                    save_subscriptions(subscriptions)
                    send_message(chat_id, f"✅ Đã cập nhật giờ gửi bản tin: Sáng **{parts[2]}** & Tối **{parts[3]}**!", reply_to_message_id=message_id)
            else:
                cfg = subscriptions[chat_id_str]
                st = "ĐANG BẬT" if cfg.get("enabled", True) else "ĐÃ TẮT"
                send_message(chat_id, f"📰 **TRẠNG THÁI BẢN TIN TỔNG HỢP:**\n• Trạng thái: **{st}**\n• Giờ gửi sáng: **{cfg.get('morning_time', '07:00')}**\n• Giờ gửi tối: **{cfg.get('evening_time', '20:00')}**\n• Khu vực: **{cfg.get('location', 'Ninh Bình')}**\n\n👉 Gõ `/daily on` để bật, `/daily now` để xem thử ngay!", reply_to_message_id=message_id)
            return

        # Command: /model
        if text.startswith("/model"):
            parts = text.split()
            if len(parts) > 1:
                m_arg = parts[1].lower()
                if m_arg in ["auto", "default"]:
                    user_model_override.pop(str(chat_id), None)
                    send_message(chat_id, "✅ Đã bật chế độ **Tự Động Định Tuyến Model Thông Minh (Smart Router)**!", reply_to_message_id=message_id)
                elif m_arg in ["sol", "gpt-5.6-sol"]:
                    user_model_override[str(chat_id)] = "gpt-5.6-sol"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.6 Sol** (Siêu suy luận)!", reply_to_message_id=message_id)
                elif m_arg in ["code", "codex", "gpt-5.3-codex-spark"]:
                    user_model_override[str(chat_id)] = "gpt-5.3-codex-spark"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.3 Codex Spark** (Chuyên lập trình)!", reply_to_message_id=message_id)
                elif m_arg in ["claude", "sonnet", "claude-sonnet-4-6"]:
                    user_model_override[str(chat_id)] = "claude-sonnet-4-6"
                    send_message(chat_id, "✅ Đã cố định Model: **Claude Sonnet 4.6**!", reply_to_message_id=message_id)
                elif m_arg in ["terra", "gpt-5.6-terra"]:
                    user_model_override[str(chat_id)] = "gpt-5.6-terra"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.6 Terra** (Siêu tốc & Tiết kiệm)!", reply_to_message_id=message_id)
                else:
                    send_message(chat_id, "⚠️ Cú pháp: `/model [auto | sol | code | claude | terra]`", reply_to_message_id=message_id)
            else:
                send_message(chat_id, "⚙️ **Chọn Não Bộ AI Cho Cuộc Trò Chuyện:**", reply_markup=get_model_menu_keyboard())
            return

        # Command: /reset
        if text == "/reset":
            conversation_history.pop(chat_id, None)
            send_message(chat_id, "🔄 Đã làm mới lịch sử cuộc trò chuyện!", reply_to_message_id=message_id)
            return

        # Command: /memo
        if text == "/memo":
            mems = user_memories.get(chat_id, [])
            if mems:
                msg = "🧠 **Các thông tin Hermes đang ghi nhớ về anh:**\n" + "\n".join([f"- {m}" for m in mems])
            else:
                msg = "🧠 Hiện tại em chưa lưu thông tin ghi nhớ nào. Anh có thể nói: *'Hãy nhớ rằng tôi là lập trình viên'*."
            send_message(chat_id, msg, reply_to_message_id=message_id)
            return

        # Case A: Voice Note / Audio Message
        if voice:
            voice_file_id = voice.get("file_id")
            file_bytes, file_path = download_telegram_file(voice_file_id)
            if file_bytes:
                b64_audio = base64.b64encode(file_bytes).decode("utf-8")
                mime = "audio/ogg" if file_path.endswith(".oga") or file_path.endswith(".ogg") else "audio/mp3"
                
                content_payload = [
                    {"type": "text", "text": "Người dùng đã gửi tin nhắn thoại sau. Hãy lắng nghe và trả lời chu đáo yêu cầu của người dùng bằng tiếng Việt."},
                    {"type": "input_audio", "input_audio": {"data": b64_audio, "format": "ogg" if "ogg" in mime else "mp3"}}
                ]
                
                chosen_model, mode_tag = select_model_for_task("voice audio", has_audio=True, chat_id=chat_id)
                logger.info(f"Routed Voice Audio to: {chosen_model}")
                reply = query_llm(chat_id, content_payload, chosen_model=chosen_model)
                send_message(chat_id, f"🎙️ **[Phản Hồi Tin Nhắn Thoại]**\n\n{reply}", reply_to_message_id=message_id)
                set_message_reaction(chat_id, message_id, "🔥")
            else:
                send_message(chat_id, "⚠️ Không thể tải tin nhắn thoại, anh gửi lại nhé!", reply_to_message_id=message_id)
            return

        # Case B: Photo (Vision)
        if photos:
            best_photo = photos[-1]
            photo_bytes, _ = download_telegram_file(best_photo["file_id"])
            
            if photo_bytes:
                b64_img = base64.b64encode(photo_bytes).decode("utf-8")
                prompt_text = caption if caption else "Hãy xem kỹ bức ảnh này và phân tích, mô tả chi tiết nội dung hoặc giải đáp yêu cầu trong ảnh."
                
                content_payload = [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                ]
                
                chosen_model, mode_tag = select_model_for_task(prompt_text, has_photo=True, chat_id=chat_id)
                logger.info(f"Routed Photo to: {chosen_model} ({mode_tag})")
                reply = query_llm(chat_id, content_payload, chosen_model=chosen_model)
                send_message(chat_id, reply, reply_to_message_id=message_id)
                set_message_reaction(chat_id, message_id, "🔥")
            else:
                send_message(chat_id, "⚠️ Không thể tải ảnh từ Telegram, anh thử gửi lại nhé!", reply_to_message_id=message_id)
            return

        # Case C: Document
        if document:
            doc_file_id = document.get("file_id")
            file_name = document.get("file_name", "document.txt")
            file_bytes, _ = download_telegram_file(doc_file_id)
            
            if file_bytes:
                extracted_text = extract_text_from_file(file_bytes, file_name)
                prompt_text = caption if caption else f"Hãy đọc và phân tích/tóm tắt nội dung file `{file_name}` sau:"
                
                full_prompt = (
                    f"Người dùng gửi file `{file_name}` có nội dung như sau:\n\n"
                    f"```\n{extracted_text}\n```\n\n"
                    f"Yêu cầu của người dùng: {prompt_text}"
                )
                
                chosen_model, mode_tag = select_model_for_task(full_prompt, has_doc=True, file_name=file_name, chat_id=chat_id)
                logger.info(f"Routed Document to: {chosen_model} ({mode_tag})")
                reply = query_llm(chat_id, full_prompt, chosen_model=chosen_model)
                send_message(chat_id, reply, reply_to_message_id=message_id)
                set_message_reaction(chat_id, message_id, "🔥")
            else:
                send_message(chat_id, f"⚠️ Không thể tải file `{file_name}`, anh gửi lại nhé!", reply_to_message_id=message_id)
            return

        # Case D: Text
        if not text:
            return

        # Natural Language Daily Briefing Activation Intent
        if any(phrase in text.lower() for phrase in ["tin tổng hợp", "bản tin sáng", "bản tin tối", "gửi tin sáng", "tổng hợp tin"]):
            chat_id_str = str(chat_id)
            if chat_id_str not in subscriptions:
                subscriptions[chat_id_str] = {}
            subscriptions[chat_id_str]["enabled"] = True
            subscriptions[chat_id_str]["location"] = "Ninh Bình"
            subscriptions[chat_id_str]["morning_time"] = "07:00"
            subscriptions[chat_id_str]["evening_time"] = "20:00"
            save_subscriptions(subscriptions)
            
            send_message(
                chat_id,
                "✅ **ĐÃ KÍCH HOẠT LỊCH GỬI BẢN TIN TỰ ĐỘNG 24/7!**\n\n"
                "Em đã lưu cấu hình cố định vào hệ thống máy chủ, không bao giờ bị quên nữa:\n"
                "• 🌅 **Bản tin sáng:** Tự động gửi lúc **07:00 sáng** (Dự báo thời tiết Ninh Bình, điểm tin nhanh, tỷ giá, mẹo ngày mới).\n"
                "• 🌆 **Bản tin tối:** Tự động gửi lúc **20:00 tối** (Tổng kết ngày, thời tiết ngày mai, lời chúc thư giãn).\n\n"
                "👉 Anh có thể gõ `/daily now` bất kỳ lúc nào để xem ngay bản tin mẫu nhé!",
                reply_to_message_id=message_id,
                reply_markup=get_main_menu_keyboard()
            )
            set_message_reaction(chat_id, message_id, "👍")
            return

        # Memory intent
        mem_match = re.search(r'^(?:hãy\s+)?nhớ\s+(?:rằng|là|cho\s+tôi|giúp\s+tôi)?\s*(.+)', text, re.IGNORECASE)
        if mem_match and not any(kw in text.lower() for kw in ["sau", "lúc", "giờ", "phút"]):
            mem_fact = mem_match.group(1).strip()
            if chat_id not in user_memories:
                user_memories[chat_id] = []
            user_memories[chat_id].append(mem_fact)
            send_message(chat_id, f"🧠 **Đã ghi nhớ:** \"{mem_fact}\"\nEm sẽ luôn nhớ thông tin này trong các câu trả lời sau!", reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "👍")
            return

        # Reminder intent
        remind_parsed = parse_reminder(text)
        if remind_parsed:
            due_time, remind_content = remind_parsed
            pending_reminders.append({
                "chat_id": chat_id,
                "due_time": due_time,
                "text": remind_content
            })
            due_dt = datetime.datetime.fromtimestamp(due_time, tz=datetime.timezone(datetime.timedelta(hours=7)))
            time_str = due_dt.strftime("%H:%M:%S ngày %d/%m/%Y")
            send_message(
                chat_id, 
                f"⏰ **Đã đặt lịch nhắc nhở thành công!**\n\n📌 **Nội dung:** {remind_content}\n🕒 **Thời gian nhắc:** {time_str}\n\nĐến đúng giờ em sẽ tự động nhắn tin cho anh nhé!", 
                reply_to_message_id=message_id
            )
            set_message_reaction(chat_id, message_id, "👍")
            return

        # Financial & Crypto Rates Lookup
        if is_rates_query(text):
            logger.info(f"Fetching financial rates for: {text}")
            rates_data = get_live_rates()
            user_query = (
                f"Câu hỏi của người dùng: {text}\n\n"
                f"{rates_data}\n\n"
                f"Hãy dựa vào dữ liệu tài chính thời gian thực ở trên để trả lời chi tiết, chính xác cho người dùng."
            )
        # Weather Lookup
        elif is_weather_query(text) or any(w in text.lower() for kw in ["thời tiết", "mưa", "nắng", "nhiệt độ"] for w in [kw]):
            logger.info(f"Fetching live weather for: {text}")
            weather_data = get_live_weather(text)
            user_query = (
                f"Câu hỏi của người dùng: {text}\n\n"
                f"{weather_data}\n\n"
                f"Hãy dựa vào dữ liệu khí tượng trực tiếp ở trên để báo cáo thời tiết và phân tích chi tiết, đưa ra lời khuyên đi chơi/ăn uống dịp 2/9 cho người dùng."
            )
        # URL Reading
        elif re.search(r'(https?://[^\s]+)', text):
            url_match = re.search(r'(https?://[^\s]+)', text)
            target_url = url_match.group(1)
            logger.info(f"Fetching URL content for: {target_url}")
            page_text = fetch_url_content(target_url)
            user_query = (
                f"Câu hỏi của người dùng: {text}\n\n"
                f"[Nội dung trang web thu thập được từ {target_url}]:\n{page_text}\n\n"
                f"Hãy đọc nội dung trên và trả lời chi tiết yêu cầu của người dùng."
            )
        else:
            user_query = text

        # Dynamic Model Selection
        chosen_model, mode_tag = select_model_for_task(user_query, chat_id=chat_id)
        logger.info(f"Dynamic Router selected: {chosen_model} ({mode_tag}) for text: {text[:50]}")
        
        reply = query_llm(chat_id, user_query, chosen_model=chosen_model)
        send_message(chat_id, reply, reply_to_message_id=message_id)
        set_message_reaction(chat_id, message_id, "🔥")

    finally:
        typing_keeper.stop()

# ==========================================================
# 24/7 Cloud Long Polling Loop (Direct Telegram Connection)
# ==========================================================

def cloud_polling_loop():
    logger.info("Starting Cloud Long Polling Loop with Ultimate Enhancements...")
    try:
        send_telegram_request("deleteWebhook", {"drop_pending_updates": False})
        logger.info("Deleted webhook to enable direct Long Polling on Cloud.")
    except Exception as e:
        logger.error(f"deleteWebhook error: {e}")

    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 30}
            r = requests.get(url, params=params, timeout=40)
            if r.status_code == 200:
                data = r.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        try:
                            handle_update(update)
                        except Exception as e:
                            logger.error(f"Error handling update: {e}", exc_info=True)
            elif r.status_code == 409:
                logger.warning("Conflict 409: Another bot instance is polling. Retrying in 5s...")
                time.sleep(5)
            else:
                time.sleep(3)
        except Exception as e:
            logger.error(f"Polling exception: {e}")
            time.sleep(5)

# 1. Unified scheduler thread (Daily Briefings + Reminders)
scheduler_thread = threading.Thread(target=reminder_scheduler_loop, daemon=True)
scheduler_thread.start()

# 2. Anti-Sleep Keep-Alive thread
keep_alive_thread = threading.Thread(target=anti_sleep_keep_alive, daemon=True)
keep_alive_thread.start()

# 3. Direct Cloud Polling thread (runs on Render 24/7)
polling_thread = threading.Thread(target=cloud_polling_loop, daemon=True)
polling_thread.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
