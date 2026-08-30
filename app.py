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
import urllib.request
import xml.etree.ElementTree as ET
import io
import contextlib
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
MEMORIES_FILE = "user_memories.json"
SKILLS_FILE = "skills_registry.json"
TODOS_FILE = "user_todos.json"

ALLOWED_USERS = set()
for uid in ALLOWED_USERS_RAW.split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USERS.add(int(uid))

# In-memory data structures
START_TIME = time.time()
conversation_history = {}
user_model_override = {}  # {chat_id: "gpt-5.6-sol" or None}
user_personas = {}        # {chat_id: "assistant" | "coder" | "humorous" | "teacher" | "executive"}
pending_reminders = []    # [{"chat_id": int, "due_time": float, "text": str}]
active_quizzes = {}       # {chat_id: {"answer": "A", "explanation": "..."}}
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
# Persistent Subscriptions, Memories, Skills & Todos
# ==========================================================

def load_subscriptions():
    if os.path.exists(SUBSCRIPTIONS_FILE):
        try:
            with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading subscriptions: {e}")
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

def load_memories():
    if os.path.exists(MEMORIES_FILE):
        try:
            with open(MEMORIES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading memories: {e}")
    return {
        "8322961603": [
            "Tên người dùng: Hiếu",
            "Nơi ở hiện tại: Ninh Bình",
            "Lĩnh vực: Lập trình viên, nghiên cứu AI, Python, Java NSO Modding",
            "Sở thích ẩm thực: Thích ăn buffet nướng lẩu BBQ 1988 Ninh Bình"
        ]
    }

def save_memories(mems):
    try:
        with open(MEMORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(mems, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving memories: {e}")

user_memories = load_memories()

DEFAULT_SKILLS = {
    "nso_modding": {
        "name": "Ninja School Online Modding",
        "description": "Kỹ thuật dịch ngược, chỉnh sửa mã nguồn file JAR và nạp IP server riêng cho NSO Java ME",
        "instructions": "Khi người dùng hỏi về NSO, JAR, mod Auto50, Up Yên, đổi IP Tailscale/LAN, hãy hướng dẫn cụ thể với bytecode, Recaf, manifest.",
        "created_by": "system"
    },
    "code_reviewer": {
        "name": "Chuyên Gia Đánh Giá & Tối Ưu Code",
        "description": "Kiểm tra bug, bảo mật, tối ưu hiệu năng và viết Clean Code",
        "instructions": "Phân tích kỹ lưỡng các lỗi tiềm ẩn, memory leak, race condition và cung cấp code refactor hoàn chỉnh.",
        "created_by": "system"
    },
    "copywriting_pro": {
        "name": "Bậc Thầy Sáng Tạo Nội Dung & Viral Marketing",
        "description": "Soạn thảo bài viết Facebook, kịch bản TikTok, bài PR sản phẩm triệu view",
        "instructions": "Viết nội dung theo công thức AIDA hoặc PAS, giật tít thu hút, CTA mạnh mẽ và tối ưu tương tác.",
        "created_by": "system"
    },
    "english_coach": {
        "name": "Gia Sư Tiếng Anh Bản Ngữ",
        "description": "Luyện giao tiếp, sửa lỗi phát âm/ngữ pháp và dịch thuật nâng cao",
        "instructions": "Giải thích chi tiết các thành ngữ, từ vựng tự nhiên của người bản xứ và gợi ý các mẫu câu thực tế.",
        "created_by": "system"
    }
}

def load_skills():
    if os.path.exists(SKILLS_FILE):
        try:
            with open(SKILLS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading skills: {e}")
    return DEFAULT_SKILLS

def save_skills(skills_dict):
    try:
        with open(SKILLS_FILE, "w", encoding="utf-8") as f:
            json.dump(skills_dict, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving skills: {e}")

skills_registry = load_skills()

def match_relevant_skills(user_text):
    matched = []
    text_lower = user_text.lower()
    for k, v in skills_registry.items():
        name = v.get("name", "").lower()
        desc = v.get("description", "").lower()
        if any(w in text_lower for w in name.split() if len(w) > 2) or any(w in text_lower for w in desc.split() if len(w) > 3):
            matched.append(v)
    return matched

def load_todos():
    if os.path.exists(TODOS_FILE):
        try:
            with open(TODOS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading todos: {e}")
    return {}

def save_todos(todos):
    try:
        with open(TODOS_FILE, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving todos: {e}")

user_todos = load_todos()

# Flask web app for Render Keep-Alive & Health Checks
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    uptime_sec = int(time.time() - START_TIME)
    return jsonify({
        "status": "healthy",
        "service": "Hermes Telegram Super-Bot 24/7 (Ultimate Edition)",
        "default_frontier_model": "gpt-5.6-sol",
        "features": [
            "live_news_rss_stream",
            "interactive_todo_manager",
            "ai_quiz_and_trivia_games",
            "fast_crypto_shortcuts",
            "python_code_sandbox_runner",
            "ai_persona_switcher",
            "lunar_calendar_and_fengshui",
            "dynamic_self_created_skills",
            "persistent_long_term_memory",
            "ai_image_generation_flux",
            "text_to_speech_voice",
            "qr_code_generator",
            "web_screenshot_capture",
            "interactive_inline_buttons",
            "voice_audio_processing",
            "live_crypto_and_fx_rates",
            "persistent_daily_briefings",
            "live_realtime_weather",
            "24_7_long_polling"
        ],
        "registered_skills_count": len(skills_registry),
        "active_subscribers": [k for k, v in subscriptions.items() if v.get("enabled")],
        "uptime_seconds": uptime_sec,
        "timestamp": time.time()
    }), 200

# ==========================================================
# Feature: Live News RSS Feeds
# ==========================================================

def get_live_news(topic="thoisu"):
    rss_urls = {
        "thoisu": ("https://vnexpress.net/rss/thoi-su.rss", "Thời Sự"),
        "congnghe": ("https://vnexpress.net/rss/so-hoa.rss", "Công Nghệ & AI"),
        "kinhdoanh": ("https://vnexpress.net/rss/kinh-doanh.rss", "Kinh Doanh & Tài Chính"),
        "thethao": ("https://vnexpress.net/rss/the-thao.rss", "Thể Thao"),
        "thegioi": ("https://vnexpress.net/rss/the-gioi.rss", "Thế Giới")
    }
    url, topic_name = rss_urls.get(topic, rss_urls["thoisu"])
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            xml_data = response.read()
        
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")
        
        news_list = []
        for item in items[:5]:
            title = item.find("title").text if item.find("title") is not None else ""
            link = item.find("link").text if item.find("link") is not None else ""
            desc = item.find("description").text if item.find("description") is not None else ""
            desc_clean = desc.split("</br>")[-1] if "</br>" in desc else desc
            desc_clean = re.sub(r'<[^>]+>', '', desc_clean).replace("]]>", "").replace("<![CDATA[", "").strip()
            
            news_list.append(f"📰 **{title}**\n_{desc_clean}_\n🔗 [Đọc bài viết]({link})")
            
        return f"🔥 **ĐIỂM TIN NÓNG TRỰC TIẾP [{topic_name.upper()}]:**\n\n" + "\n\n".join(news_list)
    except Exception as e:
        logger.error(f"Error fetching news: {e}")
        return f"⚠️ Lỗi lấy tin tức: {e}"

# ==========================================================
# Feature: Python Sandbox Runner & Utilities
# ==========================================================

def run_python_sandbox(code_str):
    code_clean = code_str.strip()
    if code_clean.startswith("```python"):
        code_clean = code_clean[9:]
    elif code_clean.startswith("```"):
        code_clean = code_clean[3:]
    if code_clean.endswith("```"):
        code_clean = code_clean[:-3]
    code_clean = code_clean.strip()
    
    t0 = time.time()
    f = io.StringIO()
    try:
        with contextlib.redirect_stdout(f):
            exec(code_clean, {"__builtins__": __builtins__})
        elapsed = (time.time() - t0) * 1000
        output = f.getvalue()
        if not output:
            output = "[Thực thi hoàn tất - Không có output stdout]"
        return f"🐍 **KẾT QUẢ CHẠY CODE PYTHON ({elapsed:.1f}ms):**\n\n```\n{output[:3500]}\n```"
    except Exception as e:
        return f"❌ **LỖI THỰC THI PYTHON:**\n```\n{str(e)[:1000]}\n```"

CAN = ["Giáp", "Ất", "Bính", "Đinh", "Mậu", "Kỷ", "Canh", "Tân", "Nhâm", "Quý"]
CHI = ["Tý", "Sửu", "Dần", "Mão", "Thìn", "Tỵ", "Ngọ", "Mùi", "Thân", "Dậu", "Tuất", "Hợi"]

def get_lunar_calendar_info():
    now_vn = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    year = now_vn.year
    can_year = CAN[(year - 4) % 10]
    chi_year = CHI[(year - 4) % 12]
    
    return (
        f"📅 **LỊCH VẠN NIÊN & CAN CHI VIỆT NAM**\n\n"
        f"• ☀️ **Dương lịch:** {now_vn.strftime('%A, ngày %d/%m/%Y (%H:%M:%S)')}\n"
        f"• 🌙 **Năm Âm lịch:** Năm **{can_year} {chi_year}**\n"
        f"• ⏰ **Khung giờ Hoàng đạo trong ngày:**\n"
        f"  - Giờ Tý (23h - 01h) | Giờ Sửu (01h - 03h)\n"
        f"  - Giờ Thìn (07h - 09h) | Giờ Tỵ (09h - 11h)\n"
        f"  - Giờ Mùi (13h - 15h) | Giờ Tuất (19h - 21h)\n"
        f"• 🌿 **Lời khuyên ngày mới:** Thích hợp triển khai ý tưởng mới, tối ưu hóa công việc, học hỏi kỹ năng và giao dịch thuận lợi."
    )

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

def get_single_crypto(sym="BTCUSDT", name="Bitcoin"):
    try:
        r = requests.get(f'https://data-api.binance.vision/api/v3/ticker/24hr?symbol={sym}', timeout=5)
        data = r.json()
        price = float(data.get('lastPrice', 0))
        change = float(data.get('priceChangePercent', 0))
        high = float(data.get('highPrice', 0))
        low = float(data.get('lowPrice', 0))
        icon = "🟢" if change >= 0 else "🔴"
        return (
            f"🪙 **THÔNG TIN GIÁ {name.upper()} (REAL-TIME):**\n\n"
            f"• Giá hiện tại: **${price:,.2f}**\n"
            f"• Biến động 24h: {icon} **{change:+.2f}%**\n"
            f"• Giá cao nhất 24h: **${high:,.2f}**\n"
            f"• Giá thấp nhất 24h: **${low:,.2f}**"
        )
    except Exception as e:
        return f"⚠️ Lỗi tra cứu giá {name}: {e}"

def is_rates_query(text):
    keywords = ["tỷ giá", "giá usd", "giá euro", "giá vàng", "giá btc", "giá bitcoin", "giá eth", "giá coin", "crypto", "tiền tệ", "usd vnd"]
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)

# ==========================================================
# Feature: Generative AI (Images, Voice, QR, Screenshots)
# ==========================================================

def enhance_prompt_for_image(user_prompt):
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    sys_p = (
        "You are an expert AI prompt engineer for Flux.1 and Midjourney.\n"
        "Convert the user's Vietnamese or simple description into an ultra-detailed, vivid English image generation prompt.\n"
        "Include details like lighting, art style, atmosphere, composition, 8k resolution. Output ONLY the English prompt string."
    )
    payload = {
        "model": "gpt-5.6-terra",
        "messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": user_prompt}],
        "temperature": 0.7
    }
    try:
        r = requests.post(f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions", headers=headers, json=payload, timeout=15)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"Prompt enhancement error: {e}")
    return user_prompt

def generate_ai_image(prompt_text):
    enhanced = enhance_prompt_for_image(prompt_text)
    encoded = urllib.parse.quote(enhanced)
    seed = int(time.time() * 1000) % 999999
    image_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=flux&nologo=true&seed={seed}"
    try:
        r = requests.get(image_url, timeout=30)
        if r.status_code == 200 and len(r.content) > 5000:
            return r.content, enhanced
    except Exception as e:
        logger.error(f"Image generation error: {e}")
    return None, enhanced

def generate_tts_audio(text):
    clean = re.sub(r'[*_`#~]', '', text)[:300]
    encoded = urllib.parse.quote(clean)
    tts_url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl=vi&client=tw-ob&q={encoded}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(tts_url, headers=headers, timeout=10)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except Exception as e:
        logger.error(f"TTS error: {e}")
    return None

def generate_qr_code(data_text):
    encoded = urllib.parse.quote(data_text)
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=600x600&data={encoded}"
    try:
        r = requests.get(qr_url, timeout=10)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        logger.error(f"QR error: {e}")
    return None

def capture_web_screenshot(url):
    if not url.startswith("http"):
        url = "https://" + url
    screen_url = f"https://image.thum.io/get/width/1200/crop/800/noanimate/{url}"
    try:
        r = requests.get(screen_url, timeout=20)
        if r.status_code == 200 and len(r.content) > 5000:
            return r.content
    except Exception as e:
        logger.error(f"Screenshot error: {e}")
    return None

# ==========================================================
# Autonomous Daily Briefing Generators
# ==========================================================

def generate_briefing(briefing_type="morning", location="Ninh Bình", chat_id=None):
    now_vn = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    time_str = now_vn.strftime("%A, ngày %d/%m/%Y")
    weather_info = get_live_weather(location)
    rates_info = get_live_rates()
    
    mem_info = ""
    chat_id_str = str(chat_id)
    if chat_id_str in user_memories and user_memories[chat_id_str]:
        mem_info = "\nThông tin trí nhớ cá nhân của người dùng:\n" + "\n".join([f"- {m}" for m in user_memories[chat_id_str]])
        
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
# Telegram Visual Feedback & Media Helpers
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
    def __init__(self, chat_id, action="typing"):
        self.chat_id = chat_id
        self.action = action
        self.running = True
        self.thread = None

    def _loop(self):
        while self.running:
            try:
                send_chat_action(self.chat_id, self.action)
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

def send_photo(chat_id, photo_bytes, caption=None, reply_to_message_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("image.jpg", photo_bytes, "image/jpeg")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "Markdown"
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    try:
        r = requests.post(url, data=data, files=files, timeout=40)
        return r.json()
    except Exception as e:
        logger.error(f"send_photo error: {e}")
        return None

def send_voice(chat_id, voice_bytes, caption=None, reply_to_message_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
    files = {"voice": ("voice.mp3", voice_bytes, "audio/mp3")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    try:
        r = requests.post(url, data=data, files=files, timeout=40)
        return r.json()
    except Exception as e:
        logger.error(f"send_voice error: {e}")
        return None

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
                {"text": "📰 Tin Tức Nóng", "callback_data": "btn_news_menu"},
                {"text": "📝 Việc Cần Làm", "callback_data": "btn_todos"}
            ],
            [
                {"text": "🎨 Tạo Ảnh AI", "callback_data": "btn_help_draw"},
                {"text": "📱 Tạo Mã QR", "callback_data": "btn_help_qr"}
            ],
            [
                {"text": "🐍 Chạy Python", "callback_data": "btn_help_run"},
                {"text": "🎮 Đố Vui AI", "callback_data": "btn_quiz"}
            ],
            [
                {"text": "🧠 Kỹ Năng & Bộ Nhớ", "callback_data": "btn_skills_memos"},
                {"text": "📅 Âm Lịch Hôm Nay", "callback_data": "btn_lunar"}
            ],
            [
                {"text": "🌤️ Thời Tiết", "callback_data": "btn_weather"},
                {"text": "💰 Tỷ Giá & Crypto", "callback_data": "btn_rates"}
            ],
            [
                {"text": "🎭 Đổi Tính Cách", "callback_data": "btn_personas"},
                {"text": "⚙️ Đổi Model AI", "callback_data": "btn_models"}
            ],
            [
                {"text": "📊 Thống Kê Bot", "callback_data": "btn_stats"},
                {"text": "🔄 Làm Mới Chat", "callback_data": "btn_reset"}
            ]
        ]
    }

def get_news_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "💻 Công Nghệ & AI", "callback_data": "news_congnghe"},
                {"text": "💼 Kinh Doanh", "callback_data": "news_kinhdoanh"}
            ],
            [
                {"text": "🌐 Thời Sự Trong Nước", "callback_data": "news_thoisu"},
                {"text": "⚽ Thể Thao", "callback_data": "news_thethao"}
            ],
            [
                {"text": "🌍 Thế Giới", "callback_data": "news_thegioi"},
                {"text": "🔙 Quay Lại Menu", "callback_data": "btn_menu_back"}
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

def get_persona_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🌟 Trợ Lý Tận Tâm (Mặc định)", "callback_data": "setpersona_assistant"},
                {"text": "💻 Kỹ Sư Senior (Chuyên Code)", "callback_data": "setpersona_coder"}
            ],
            [
                {"text": "😄 Hài Hước, Bạn Thân", "callback_data": "setpersona_humorous"},
                {"text": "👨‍🏫 Gia Sư Tận Tình", "callback_data": "setpersona_teacher"}
            ],
            [
                {"text": "👔 Giám Đốc Điều Hành (Súc tích)", "callback_data": "setpersona_executive"},
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
# Core Dynamic Multi-Model LLM Engine with Personas, Skills & Memory
# ==========================================================

PERSONA_PROMPTS = {
    "assistant": "Phong cách của bạn là: Siêu trợ lý AI tận tâm, thông minh, chu đáo, lễ phép và giải quyết mọi việc đến nơi đến chốn.",
    "coder": "Phong cách của bạn là: Kỹ sư phần mềm Senior. Trả lời tập trung vào giải pháp kỹ thuật, code tối ưu, sạch sẽ, bảo mật, giải thích ngắn gọn đúng trọng tâm.",
    "humorous": "Phong cách của bạn là: Người bạn thân hóm hỉnh, dí dỏm, vui tính, thỉnh thoảng dùng câu nói hài hước và tạo không khí vui vẻ thoải mái.",
    "teacher": "Phong cách của bạn là: Gia sư / Thầy giáo kiên nhẫn, giải thích bản chất vấn đề từ gốc rễ, kèm ví dụ minh họa trực quan sinh động.",
    "executive": "Phong cách của bạn là: Giám đốc điều hành cấp cao. Trả lời cực kỳ súc tích, gạch đầu dòng rõ ràng, tập trung vào hiệu quả và kết quả."
}

def query_llm(chat_id, user_content, chosen_model="gpt-5.6-sol", matched_skills=None):
    now_vn = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    time_str = now_vn.strftime("%A, ngày %d/%m/%Y, %H:%M:%S (GMT+7)")
    
    chat_id_str = str(chat_id)
    persona_key = user_personas.get(chat_id_str, "assistant")
    persona_desc = PERSONA_PROMPTS.get(persona_key, PERSONA_PROMPTS["assistant"])

    system_prompt = (
        f"Bạn là Hermes - siêu trợ lý AI toàn năng, chủ động, thông thái và am hiểu sâu sắc mọi lĩnh vực tại Việt Nam.\n"
        f"Thời gian hiện tại: {time_str}.\n"
        f"{persona_desc}\n\n"
        "NGUYÊN TẮC HÀNH ĐỘNG TUYỆT ĐỐI:\n"
        "1. Luôn trực tiếp trả lời, báo cáo và giải quyết yêu cầu của người dùng, không bao giờ đùn đẩy bảo người dùng tự tìm.\n"
        "2. Bạn sở hữu Hệ Thống Kỹ Năng Tự Học (Skills Engine), Bộ Nhớ Dài Hạn (Persistent Memory), và Quản lý Nhiệm Vụ (To-Do List).\n"
        "3. Bạn có đủ các công cụ: Điểm Báo Tin Tức (`/news`), Chạy Code Python (`/run`), Tạo Ảnh AI Flux (`/draw`), Đọc Giọng Nói TTS (`/tts`), Tạo Mã QR (`/qr`), Chụp Web (`/capture`), Bản Tin Tự Động (`/daily`), Tỷ Giá & Crypto, Thời Tiết, Lịch Âm Dương (`/amlich`), Đố Vui AI (`/quiz`).\n"
        "4. Khi người dùng hỏi về địa điểm/quán ăn (ví dụ BBQ 1988), cung cấp đầy đủ thông tin: địa chỉ các cơ sở tại Ninh Bình (15 Lê Thánh Tông & 38 Đào Duy Từ), bảng giá vé buffet nướng lẩu, các món đặc sắc, hotline đặt bàn và giờ mở cửa.\n"
        "5. Tuyệt đối KHÔNG BAO GIỜ nói 'tôi không có công cụ duyệt web', 'không thể tạo ảnh' hay từ chối."
    )
    
    if chat_id_str in user_memories and user_memories[chat_id_str]:
        mem_str = "\n".join([f"- {m}" for m in user_memories[chat_id_str]])
        system_prompt += f"\n\n🧠 THÔNG TIN ĐÃ GHI NHỚ VĨNH VIỄN VỀ NGƯỜI DÙNG:\n{mem_str}"

    if matched_skills:
        skill_str = "\n\n".join([f"✨ KỸ NĂNG CHUYÊN SÂU [{s['name']}]:\n- Mô tả: {s['description']}\n- Hướng dẫn thực thi: {s['instructions']}" for s in matched_skills])
        system_prompt += f"\n\n⚡ KÍCH HOẠT CÁC KỸ NĂNG CHUYÊN GIA PHÙ HỢP:\n{skill_str}"

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
    elif data == "btn_news_menu":
        send_message(chat_id, "📰 **CHỌN CHUYÊN MỤC TIN TỨC NÓNG TRỰC TIẾP:**", reply_markup=get_news_menu_keyboard())
    elif data.startswith("news_"):
        topic = data.replace("news_", "")
        send_message(chat_id, "⏳ Đang cập nhật luồng tin tức...")
        news_content = get_live_news(topic)
        send_message(chat_id, news_content, reply_markup=get_news_menu_keyboard())
    elif data == "btn_todos":
        todos = user_todos.get(chat_id_str, [])
        if todos:
            lines = [f"{i+1}. {'✅ ~~' + t['task'] + '~~' if t['done'] else '⬜ **' + t['task'] + '**'}" for i, t in enumerate(todos)]
            msg = "📝 **DANH SÁCH CÔNG VIỆC CỦA ANH:**\n\n" + "\n".join(lines) + "\n\n👉 Thêm việc: `/todo add [nội dung]`\n👉 Hoàn thành: `/todo done [số thứ tự]`"
        else:
            msg = "📝 Danh sách việc cần làm hiện đang trống!\n👉 Gõ `/todo add [nội dung]` để thêm công việc mới."
        send_message(chat_id, msg)
    elif data == "btn_quiz":
        send_message(chat_id, "⏳ Hermes đang soạn câu hỏi đố vui...")
        quiz_prompt = (
            "Hãy tạo 1 câu hỏi đố vui kiến thức thú vị bằng tiếng Việt (chủ đề công nghệ, khoa học, đố mẹo hoặc đời sống).\n"
            "Format trả về chính xác:\n"
            "Câu hỏi: [Nội dung]\n"
            "A. [Đáp án A]\n"
            "B. [Đáp án B]\n"
            "C. [Đáp án C]\n"
            "D. [Đáp án D]\n"
            "Đáp án đúng: [A/B/C/D]\n"
            "Giải thích: [Ngắn gọn 1 câu]"
        )
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        try:
            r = requests.post(f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions", headers=headers, json={"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": quiz_prompt}], "temperature": 0.8}, timeout=15)
            q_text = r.json()["choices"][0]["message"]["content"]
            ans_match = re.search(r'Đáp án đúng:\s*([ABCD])', q_text, re.IGNORECASE)
            ans = ans_match.group(1).upper() if ans_match else "A"
            active_quizzes[chat_id_str] = {"answer": ans, "full_text": q_text}
            
            # Create inline answer buttons
            q_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🅰️ A", "callback_data": "quiz_ans_A"},
                        {"text": "🅱️ B", "callback_data": "quiz_ans_B"}
                    ],
                    [
                        {"text": "🅲 C", "callback_data": "quiz_ans_C"},
                        {"text": "🅳 D", "callback_data": "quiz_ans_D"}
                    ]
                ]
            }
            send_message(chat_id, f"🎮 **ĐỐ VUI TRÍ TUỆ CÙNG HERMES:**\n\n{q_text.split('Đáp án đúng')[0].strip()}", reply_markup=q_keyboard)
        except Exception as e:
            send_message(chat_id, "⚠️ Lỗi tạo câu đố, anh bấm lại nhé!")
    elif data.startswith("quiz_ans_"):
        user_choice = data.replace("quiz_ans_", "")
        quiz_data = active_quizzes.get(chat_id_str)
        if quiz_data:
            correct_ans = quiz_data.get("answer", "A")
            full_txt = quiz_data.get("full_text", "")
            if user_choice == correct_ans:
                send_message(chat_id, f"🎉 **CHÍNH XÁC!** Anh trả lời rất xuất sắc!\n\n💡 {full_txt[full_txt.find('Giải thích:'):] if 'Giải thích:' in full_txt else ''}")
            else:
                send_message(chat_id, f"❌ **CHƯA ĐÚNG RỒI!**\nĐáp án chính xác là **{correct_ans}**!\n\n💡 {full_txt[full_txt.find('Giải thích:'):] if 'Giải thích:' in full_txt else ''}")
            active_quizzes.pop(chat_id_str, None)
        else:
            send_message(chat_id, "⚠️ Câu đố này đã kết thúc, anh bấm **[🎮 Đố Vui AI]** để chơi câu mới nhé!")
    elif data == "btn_weather":
        send_message(chat_id, "⏳ Đang kết nối trạm thời tiết Ninh Bình...")
        w = get_live_weather("thời tiết ninh bình")
        send_message(chat_id, w if w else "⚠️ Không thể lấy dữ liệu thời tiết.")
    elif data == "btn_rates":
        send_message(chat_id, "⏳ Đang tải tỷ giá ngoại tệ & giá Crypto...")
        r = get_live_rates()
        send_message(chat_id, r if r else "⚠️ Không thể lấy dữ liệu tỷ giá.")
    elif data == "btn_lunar":
        send_message(chat_id, get_lunar_calendar_info())
    elif data == "btn_help_run":
        send_message(chat_id, "🐍 **HƯỚNG DẪN CHẠY CODE PYTHON SANDBOX:**\n\nAnh gõ:\n👉 `/run [đoạn code python]`\n*Ví dụ:*\n`/run print(sum([x**2 for x in range(10)]))`\n\nBot sẽ biên dịch và trả về kết quả in ra màn hình ngay tức thì!")
    elif data == "btn_help_draw":
        send_message(chat_id, "🎨 **HƯỚNG DẪN TẠO ẢNH AI FLUX.1:**\n\nAnh gõ:\n👉 `/draw [mô tả ảnh]` hoặc nhắn *'Vẽ cho anh chú rồng bay qua mây vàng'*\n\nBot sẽ tự động vẽ và gửi ảnh 1024x1024 chất lượng cao cho anh!")
    elif data == "btn_help_qr":
        send_message(chat_id, "📱 **HƯỚNG DẪN TẠO MÃ QR TỨC THÌ:**\n\nAnh gõ:\n👉 `/qr [link/wifi/stk]`\n\nBot sẽ gửi ảnh mã QR sắc nét ngay lập tức!")
    elif data == "btn_skills_memos":
        skills_text = "\n".join([f"• **{v['name']}**: _{v['description']}_" for k, v in skills_registry.items()])
        mems = user_memories.get(chat_id_str, [])
        mems_text = "\n".join([f"- {m}" for m in mems]) if mems else "Chưa có thông tin ghi nhớ nào."
        msg = (
            f"🧠 **HỆ THỐNG KỸ NĂNG & BỘ NHỚ VĨNH VIỄN**\n\n"
            f"⚡ **Danh sách Kỹ năng AI ({len(skills_registry)} Skills):**\n{skills_text}\n\n"
            f"📝 **Dữ liệu Trí nhớ Hermes đã lưu về anh:**\n{mems_text}\n\n"
            f"💡 **Cách thêm:**\n"
            f"• Thêm kỹ năng mới: `/skill add [tên] | [mô tả] | [hướng dẫn]`\n"
            f"• Ghi nhớ mới: Gõ *'Hãy nhớ rằng tôi thích...'* hoặc `/memo add [nội dung]`"
        )
        send_message(chat_id, msg)
    elif data == "btn_personas":
        send_message(chat_id, "🎭 **Chọn Tính Cách & Phong Cách Nói Chuyện Của Hermes:**", reply_markup=get_persona_menu_keyboard())
    elif data == "btn_stats":
        uptime_m = int((time.time() - START_TIME) / 60)
        current_m = user_model_override.get(chat_id_str, "Tự động (Smart Router)")
        current_p = user_personas.get(chat_id_str, "Trợ Lý Tận Tâm (Mặc định)")
        msg = (
            f"📊 **BÁO CÁO THỐNG KÊ HỆ THỐNG BOT 24/7:**\n\n"
            f"• ⏱️ **Thời gian hoạt động liên tục (Uptime):** {uptime_m} phút\n"
            f"• 🧠 **Model AI hiện tại:** `{current_m}`\n"
            f"• 🎭 **Tính cách hiện tại:** `{current_p}`\n"
            f"• 💾 **Ký ức đã lưu trong bộ nhớ:** `{len(user_memories.get(chat_id_str, []))}` mục\n"
            f"• ⚡ **Số kỹ năng (Skills) đã đăng ký:** `{len(skills_registry)}` kỹ năng\n"
            f"• 🌐 **Trạng thái máy chủ:** `100% Hoạt động & Không bao giờ ngủ (Anti-Sleep Active)`"
        )
        send_message(chat_id, msg)
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
    elif data.startswith("setpersona_"):
        p = data.replace("setpersona_", "")
        user_personas[chat_id_str] = p
        p_names = {
            "assistant": "🌟 Trợ Lý Tận Tâm (Mặc định)",
            "coder": "💻 Kỹ Sư Senior (Chuyên Code)",
            "humorous": "😄 Hài Hước, Bạn Thân",
            "teacher": "👨‍🏫 Gia Sư Tận Tình",
            "executive": "👔 Giám Đốc Điều Hành"
        }
        send_message(chat_id, f"✅ Đã đổi phong cách nói chuyện sang: **{p_names.get(p, p)}**!", reply_markup=get_main_menu_keyboard())

def handle_update(update):
    if "callback_query" in update:
        handle_callback_query(update["callback_query"])
        return

    message = update.get("message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_id_str = str(chat_id)
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
        # Command: /start or /menu
        if text == "/start" or text == "/menu":
            welcome = (
                "👋 **Chào anh! Em là Hermes AI Siêu Trợ Lý (Ultimate Edition 24/7)!**\n\n"
                "🚀 **Bộ Công Cụ & Siêu Năng Lực Toàn Diện:**\n"
                "• 📰 **Điểm Báo & Tin Tức Nóng (`/news`):** Đọc tin tức công nghệ, tài chính trực tiếp.\n"
                "• 📝 **Quản Lý Công Việc (`/todo`):** Ghi nhớ và theo dõi to-do list hàng ngày.\n"
                "• 🎮 **Đố Vui AI Trí Tuệ (`/quiz`):** Thử thách trí tuệ với các câu đố thú vị.\n"
                "• 🐍 **Chạy Code Python (`/run`):** Thực thi code Python và lấy kết quả trong mili-giây.\n"
                "• 🪙 **Tra Cứu Crypto Siêu Tốc:** Gõ `/btc`, `/eth`, `/sol`, `/bnb`.\n"
                "• 🎭 **Đổi Tính Cách AI (`/persona`):** Trợ lý, Kỹ sư Senior, Hài hước, Gia sư, Giám đốc.\n"
                "• 📅 **Lịch Âm Dương & Can Chi (`/amlich`):** Tra cứu ngày Âm lịch Bính Ngọ & Giờ hoàng đạo.\n"
                "• ⚡ **Tự Học & Tự Tạo Skill (`/skill`):** Dạy bot kỹ năng chuyên môn mới theo ý anh.\n"
                "• 💾 **Bộ Nhớ Vĩnh Viễn (`/memo`):** Lưu trữ sở thích, dự án không bao giờ quên.\n"
                "• 🎨 **Tạo Ảnh AI Nghệ Thuật (`/draw [mô tả]`):** Model Flux.1 8K sắc nét.\n"
                "• 🔊 **Đọc Giọng Nói TTS (`/tts [văn bản]`):** Chuyển lời văn thành voice note.\n"
                "• 📱 **Tạo Mã QR Tức Thì (`/qr [link/nội dung]`):** Tạo mã QR độ nét cao.\n"
                "• 📰 **Bản Tin Sáng (07:00) & Tối (20:00):** Tự động gửi đúng giờ mỗi ngày.\n\n"
                "👇 **Anh có thể chạm nhanh các nút bên dưới để trải nghiệm ngay:**"
            )
            send_message(chat_id, welcome, reply_to_message_id=message_id, reply_markup=get_main_menu_keyboard())
            return

        # Command: /help
        if text == "/help":
            help_text = (
                "📖 **DANH SÁCH LỆNH VÀ CÔNG CỤ TỰ ĐỘNG**\n\n"
                "1. 📰 **/news** — Xem điểm tin tức nóng trực tiếp.\n"
                "2. 📝 **/todo [add | done | clear]** — Quản lý công việc cần làm.\n"
                "3. 🎮 **/quiz** — Chơi đố vui trí tuệ với AI.\n"
                "4. 🪙 **/btc, /eth, /sol, /bnb** — Xem giá Crypto thời gian thực.\n"
                "5. 🐍 **/run [code]** — Thực thi code Python trực tiếp.\n"
                "6. 🎭 **/persona [tên]** — Đổi tính cách & phong cách AI.\n"
                "7. 📅 **/amlich** — Xem lịch Âm Dương, Giờ hoàng đạo.\n"
                "8. ⚡ **/skill** — Dạy kỹ năng chuyên gia mới cho bot.\n"
                "9. 🧠 **/memo** — Quản lý bộ nhớ vĩnh viễn.\n"
                "10. 🎨 **/draw [mô tả]** — Tạo ảnh AI Flux.1 sắc nét.\n"
                "11. 🔊 **/tts [văn bản]** — Chuyển văn bản thành giọng nói.\n"
                "12. 📱 **/qr [link]** — Tạo ảnh mã QR tức thì.\n"
                "13. 🖼️ **/capture [link]** — Chụp ảnh màn hình trang web.\n"
                "14. 📰 **/daily [on | off | now]** — Quản lý bản tin sáng & tối.\n"
                "15. 📊 **/stats** — Xem thống kê hệ thống bot.\n"
                "16. 🔄 **/reset** — Làm mới cuộc trò chuyện."
            )
            send_message(chat_id, help_text, reply_to_message_id=message_id, reply_markup=get_main_menu_keyboard())
            return

        # Fast Crypto Shortcuts
        if text.lower() in ["/btc", "btc", "bitcoin"]:
            send_message(chat_id, get_single_crypto("BTCUSDT", "Bitcoin"), reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "🔥")
            return
        if text.lower() in ["/eth", "eth", "ethereum"]:
            send_message(chat_id, get_single_crypto("ETHUSDT", "Ethereum"), reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "🔥")
            return
        if text.lower() in ["/sol", "sol", "solana"]:
            send_message(chat_id, get_single_crypto("SOLUSDT", "Solana"), reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "🔥")
            return
        if text.lower() in ["/bnb", "bnb"]:
            send_message(chat_id, get_single_crypto("BNBUSDT", "Binance Coin"), reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "🔥")
            return

        # Command: /news (Tin tức)
        if text.startswith("/news") or text.startswith("/tin"):
            parts = text.split()
            topic = parts[1].lower() if len(parts) > 1 else "thoisu"
            send_message(chat_id, get_live_news(topic), reply_to_message_id=message_id, reply_markup=get_news_menu_keyboard())
            set_message_reaction(chat_id, message_id, "🔥")
            return

        # Command: /todo (Quản lý công việc)
        if text.startswith("/todo"):
            parts = text.split(maxsplit=2)
            if chat_id_str not in user_todos:
                user_todos[chat_id_str] = []
                
            if len(parts) > 1:
                sub = parts[1].lower()
                if sub in ["add", "them", "thêm"] and len(parts) > 2:
                    task_text = parts[2].strip()
                    user_todos[chat_id_str].append({"task": task_text, "done": False, "created_at": time.time()})
                    save_todos(user_todos)
                    send_message(chat_id, f"📝 **Đã thêm vào danh sách:** \"{task_text}\"", reply_to_message_id=message_id)
                    return
                elif sub in ["done", "xong"] and len(parts) > 2:
                    idx_str = parts[2].strip()
                    if idx_str.isdigit():
                        idx = int(idx_str) - 1
                        if 0 <= idx < len(user_todos[chat_id_str]):
                            user_todos[chat_id_str][idx]["done"] = True
                            save_todos(user_todos)
                            send_message(chat_id, f"✅ **Đã hoàn thành:** ~~{user_todos[chat_id_str][idx]['task']}~~", reply_to_message_id=message_id)
                            return
                elif sub in ["clear", "xoa", "xóa"]:
                    user_todos[chat_id_str] = [t for t in user_todos[chat_id_str] if not t["done"]]
                    save_todos(user_todos)
                    send_message(chat_id, "🧹 Đã dọn dẹp các công việc đã hoàn thành!", reply_to_message_id=message_id)
                    return

            todos = user_todos.get(chat_id_str, [])
            if todos:
                lines = [f"{i+1}. {'✅ ~~' + t['task'] + '~~' if t['done'] else '⬜ **' + t['task'] + '**'}" for i, t in enumerate(todos)]
                msg = "📝 **DANH SÁCH CÔNG VIỆC CỦA ANH:**\n\n" + "\n".join(lines) + "\n\n👉 Thêm việc: `/todo add [nội dung]`\n👉 Đánh dấu xong: `/todo done [số]`\n👉 Dọn việc xong: `/todo clear`"
            else:
                msg = "📝 Danh sách việc cần làm hiện đang trống!\n👉 Gõ `/todo add [nội dung]` để thêm công việc mới."
            send_message(chat_id, msg, reply_to_message_id=message_id)
            return

        # Command: /run (Python Sandbox Code Execution)
        run_match = re.search(r'^/run\s+(.+)', text, re.DOTALL)
        if run_match:
            code_snippet = run_match.group(1).strip()
            res_output = run_python_sandbox(code_snippet)
            send_message(chat_id, res_output, reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "🔥")
            return

        # Command: /quiz (Đố vui AI)
        if text in ["/quiz", "đố vui", "chơi game", "câu đố"]:
            handle_callback_query({"id": "0", "data": "btn_quiz", "message": {"chat": {"id": chat_id}}})
            return

        # Command: /amlich or /licham (Lịch vạn niên & Can Chi)
        if text in ["/amlich", "/licham", "âm lịch", "lịch âm", "hôm nay ngày mấy âm"]:
            send_message(chat_id, get_lunar_calendar_info(), reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "👍")
            return

        # Command: /persona (Đổi tính cách AI)
        if text.startswith("/persona"):
            parts = text.split()
            if len(parts) > 1:
                p_arg = parts[1].lower()
                if p_arg in ["assistant", "coder", "humorous", "teacher", "executive"]:
                    user_personas[chat_id_str] = p_arg
                    send_message(chat_id, f"✅ Đã kích hoạt tính cách: **{p_arg.upper()}**!", reply_to_message_id=message_id)
                else:
                    send_message(chat_id, "⚠️ Chọn 1 trong các tính cách: `assistant`, `coder`, `humorous`, `teacher`, `executive`", reply_to_message_id=message_id)
            else:
                send_message(chat_id, "🎭 **Chọn Tính Cách & Phong Cách Nói Chuyện:**", reply_markup=get_persona_menu_keyboard())
            return

        # Command: /stats (Thống kê Bot)
        if text == "/stats":
            uptime_m = int((time.time() - START_TIME) / 60)
            current_m = user_model_override.get(chat_id_str, "Tự động (Smart Router)")
            current_p = user_personas.get(chat_id_str, "Trợ Lý Tận Tâm (Mặc định)")
            msg = (
                f"📊 **BÁO CÁO THỐNG KÊ HỆ THỐNG BOT 24/7:**\n\n"
                f"• ⏱️ **Thời gian hoạt động liên tục (Uptime):** {uptime_m} phút\n"
                f"• 🧠 **Model AI hiện tại:** `{current_m}`\n"
                f"• 🎭 **Tính cách hiện tại:** `{current_p}`\n"
                f"• 💾 **Ký ức đã lưu trong bộ nhớ:** `{len(user_memories.get(chat_id_str, []))}` mục\n"
                f"• ⚡ **Số kỹ năng (Skills) đã đăng ký:** `{len(skills_registry)}` kỹ năng\n"
                f"• 📝 **Số công việc đang quản lý:** `{len(user_todos.get(chat_id_str, []))}` mục\n"
                f"• 🌐 **Trạng thái máy chủ:** `100% Hoạt động & Không bao giờ ngủ (Anti-Sleep Active)`"
            )
            send_message(chat_id, msg, reply_to_message_id=message_id)
            return

        # Command: /skills or /skill (Quản lý Kỹ năng Tự Tạo)
        if text.startswith("/skill") or text.startswith("/skills"):
            parts = text.split(maxsplit=2)
            if len(parts) > 1:
                sub_cmd = parts[1].lower()
                if sub_cmd in ["add", "tao", "tạo", "them", "thêm"] and len(parts) > 2:
                    content_str = parts[2]
                    chunks = [c.strip() for c in content_str.split("|")]
                    if len(chunks) >= 3:
                        s_name, s_desc, s_inst = chunks[0], chunks[1], chunks[2]
                        s_id = re.sub(r'\W+', '_', s_name.lower()).strip('_')
                        skills_registry[s_id] = {
                            "name": s_name,
                            "description": s_desc,
                            "instructions": s_inst,
                            "created_by": "user"
                        }
                        save_skills(skills_registry)
                        send_message(
                            chat_id, 
                            f"✅ **ĐÃ TẠO VÀ LƯU KỸ NĂNG MỚI THÀNH CÔNG!**\n\n"
                            f"📌 **Tên Skill:** `{s_name}`\n"
                            f"📝 **Mô tả:** {s_desc}\n"
                            f"⚡ **Hướng dẫn:** {s_inst}\n\n"
                            f"Từ bây giờ, bất cứ khi nào anh hỏi về chủ đề này, Hermes sẽ tự động kích hoạt kỹ năng chuyên gia này!", 
                            reply_to_message_id=message_id
                        )
                        return
                    else:
                        send_message(chat_id, "⚠️ Cú pháp tạo skill: `/skill add [Tên] | [Mô tả] | [Hướng dẫn chi tiết]`\nVí dụ:\n`/skill add Soi Kèo | Chuyên gia bóng đá | Phân tích phong độ, thống kê và nhận định tỷ lệ kèo khách quan`", reply_to_message_id=message_id)
                        return
                elif sub_cmd in ["del", "xoa", "xóa"] and len(parts) > 2:
                    target = parts[2].strip().lower()
                    found = False
                    for k in list(skills_registry.keys()):
                        if target in k or target in skills_registry[k]["name"].lower():
                            del skills_registry[k]
                            found = True
                            save_skills(skills_registry)
                            send_message(chat_id, f"🗑️ Đã xóa kỹ năng `{target}` thành công!", reply_to_message_id=message_id)
                            break
                    if not found:
                        send_message(chat_id, f"⚠️ Không tìm thấy kỹ năng `{target}`.", reply_to_message_id=message_id)
                    return
            
            # List all skills
            skills_list = "\n\n".join([f"✨ **{v['name']}** (`{k}`):\n- Mô tả: _{v['description']}_\n- Tác giả: `{v.get('created_by', 'system')}`" for k, v in skills_registry.items()])
            msg = (
                f"⚡ **DANH SÁCH TOÀN BỘ KỸ NĂNG HIỆN CÓ ({len(skills_registry)} Skills):**\n\n"
                f"{skills_list}\n\n"
                f"👉 **Tạo thêm kỹ năng mới:**\n"
                f"`/skill add [Tên] | [Mô tả] | [Hướng dẫn chi tiết]`"
            )
            send_message(chat_id, msg, reply_to_message_id=message_id)
            return

        # Command: /memo (Quản lý Bộ Nhớ Vĩnh Viễn)
        if text.startswith("/memo"):
            parts = text.split(maxsplit=2)
            if len(parts) > 1:
                sub_cmd = parts[1].lower()
                if sub_cmd in ["add", "them", "thêm"] and len(parts) > 2:
                    fact = parts[2].strip()
                    if chat_id_str not in user_memories:
                        user_memories[chat_id_str] = []
                    user_memories[chat_id_str].append(fact)
                    save_memories(user_memories)
                    send_message(chat_id, f"🧠 **Đã lưu vào bộ nhớ vĩnh viễn:** \"{fact}\"", reply_to_message_id=message_id)
                    return
                elif sub_cmd in ["clear", "xoa", "xóa", "reset"]:
                    user_memories[chat_id_str] = []
                    save_memories(user_memories)
                    send_message(chat_id, "🗑️ Đã xóa toàn bộ dữ liệu bộ nhớ của anh.", reply_to_message_id=message_id)
                    return

            mems = user_memories.get(chat_id_str, [])
            if mems:
                msg = "🧠 **CÁC THÔNG TIN HERMES ĐANG GHI NHỚ VĨNH VIỄN VỀ ANH:**\n\n" + "\n".join([f"• {m}" for m in mems]) + "\n\n👉 Thêm thông tin mới: `/memo add [nội dung]` hoặc gõ *'Hãy nhớ rằng...'*."
            else:
                msg = "🧠 Hiện tại em chưa lưu thông tin nào. Anh có thể nói: *'Hãy nhớ rằng tôi là lập trình viên'* hoặc `/memo add [nội dung]`."
            send_message(chat_id, msg, reply_to_message_id=message_id)
            return

        # Command: /draw or /image (Tạo ảnh AI Flux)
        draw_match = re.search(r'^(?:/draw|/image|vẽ\s+ảnh|tạo\s+ảnh|vẽ\s+hình|hãy\s+vẽ)\s*(.+)', text, re.IGNORECASE)
        if draw_match:
            prompt_input = draw_match.group(1).strip()
            send_message(chat_id, f"🎨 Đang phác thảo và vẽ bức ảnh: *\"{prompt_input}\"* bằng Model Flux.1...")
            img_bytes, enhanced_p = generate_ai_image(prompt_input)
            if img_bytes:
                caption = f"✨ **Tác phẩm tạo bởi Hermes AI (Flux.1)**\n\n📌 **Prompt:** _{prompt_input}_\n🎨 **Chi tiết:** `{enhanced_p[:200]}...`"
                send_photo(chat_id, img_bytes, caption=caption, reply_to_message_id=message_id)
                set_message_reaction(chat_id, message_id, "🔥")
            else:
                send_message(chat_id, "⚠️ Không thể kết nối máy chủ vẽ ảnh lúc này, anh thử lại sau vài giây nhé!", reply_to_message_id=message_id)
            return

        # Command: /tts or /speak (Text-to-Speech Voice Generator)
        tts_match = re.search(r'^(?:/tts|/speak|đọc\s+cho\s+tôi|phát\s+âm|nói\s+câu)\s*(.+)', text, re.IGNORECASE)
        if tts_match:
            tts_text = tts_match.group(1).strip()
            voice_bytes = generate_tts_audio(tts_text)
            if voice_bytes:
                send_voice(chat_id, voice_bytes, caption=f"🗣️ {tts_text}", reply_to_message_id=message_id)
                set_message_reaction(chat_id, message_id, "🔥")
            else:
                send_message(chat_id, "⚠️ Lỗi chuyển đổi giọng nói, anh thử lại nhé!", reply_to_message_id=message_id)
            return

        # Command: /qr (Tạo mã QR)
        qr_match = re.search(r'^(?:/qr|tạo\s+mã\s+qr|tạo\s+qr)\s*(.+)', text, re.IGNORECASE)
        if qr_match:
            qr_data = qr_match.group(1).strip()
            qr_bytes = generate_qr_code(qr_data)
            if qr_bytes:
                send_photo(chat_id, qr_bytes, caption=f"📱 **Mã QR Đã Tạo Cho:** `{qr_data}`", reply_to_message_id=message_id)
                set_message_reaction(chat_id, message_id, "🔥")
            else:
                send_message(chat_id, "⚠️ Lỗi tạo mã QR, anh thử lại nhé!", reply_to_message_id=message_id)
            return

        # Command: /capture or /screenshot (Chụp ảnh web)
        cap_match = re.search(r'^(?:/capture|/screenshot|chụp\s+web|chụp\s+trang)\s*(.+)', text, re.IGNORECASE)
        if cap_match:
            target_web = cap_match.group(1).strip()
            send_message(chat_id, f"📸 Đang chụp ảnh toàn màn hình trang web: `{target_web}`...")
            shot_bytes = capture_web_screenshot(target_web)
            if shot_bytes:
                send_photo(chat_id, shot_bytes, caption=f"📸 **Ảnh Chụp Màn Hình Web:** `{target_web}`", reply_to_message_id=message_id)
                set_message_reaction(chat_id, message_id, "🔥")
            else:
                send_message(chat_id, "⚠️ Không thể chụp ảnh trang web này.", reply_to_message_id=message_id)
            return

        # Command: /daily (Quản lý Bản Tin Sáng & Tối)
        if text.startswith("/daily"):
            parts = text.split()
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
                    user_model_override.pop(chat_id_str, None)
                    send_message(chat_id, "✅ Đã bật chế độ **Tự Động Định Tuyến Model Thông Minh (Smart Router)**!", reply_to_message_id=message_id)
                elif m_arg in ["sol", "gpt-5.6-sol"]:
                    user_model_override[chat_id_str] = "gpt-5.6-sol"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.6 Sol** (Siêu suy luận)!", reply_to_message_id=message_id)
                elif m_arg in ["code", "codex", "gpt-5.3-codex-spark"]:
                    user_model_override[chat_id_str] = "gpt-5.3-codex-spark"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.3 Codex Spark** (Chuyên lập trình)!", reply_to_message_id=message_id)
                elif m_arg in ["claude", "sonnet", "claude-sonnet-4-6"]:
                    user_model_override[chat_id_str] = "claude-sonnet-4-6"
                    send_message(chat_id, "✅ Đã cố định Model: **Claude Sonnet 4.6**!", reply_to_message_id=message_id)
                elif m_arg in ["terra", "gpt-5.6-terra"]:
                    user_model_override[chat_id_str] = "gpt-5.6-terra"
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

        # Natural Language Skill Creation Intent: "Tạo skill mới: ..."
        skill_intent = re.search(r'^(?:hãy\s+)?(?:tạo|học)\s+(?:kỹ\s+năng|skill)\s+(?:mới)?[:\s]*(.+)', text, re.IGNORECASE)
        if skill_intent:
            raw_s = skill_intent.group(1).strip()
            chunks = [c.strip() for c in raw_s.split("|")]
            if len(chunks) >= 3:
                s_name, s_desc, s_inst = chunks[0], chunks[1], chunks[2]
            else:
                s_name = raw_s[:30]
                s_desc = raw_s
                s_inst = f"Khi người dùng yêu cầu, hãy áp dụng kỹ năng {raw_s} để hỗ trợ tốt nhất."
            s_id = re.sub(r'\W+', '_', s_name.lower()).strip('_')
            skills_registry[s_id] = {
                "name": s_name,
                "description": s_desc,
                "instructions": s_inst,
                "created_by": "user"
            }
            save_skills(skills_registry)
            send_message(chat_id, f"⚡ **Đã học thành công Kỹ Năng Mới:** `{s_name}`!\nEm sẽ tự động áp dụng kỹ năng này khi anh cần!", reply_to_message_id=message_id)
            set_message_reaction(chat_id, message_id, "🔥")
            return

        # Memory intent
        mem_match = re.search(r'^(?:hãy\s+)?nhớ\s+(?:rằng|là|cho\s+tôi|giúp\s+tôi)?\s*(.+)', text, re.IGNORECASE)
        if mem_match and not any(kw in text.lower() for kw in ["sau", "lúc", "giờ", "phút"]):
            mem_fact = mem_match.group(1).strip()
            if chat_id_str not in user_memories:
                user_memories[chat_id_str] = []
            user_memories[chat_id_str].append(mem_fact)
            save_memories(user_memories)
            send_message(chat_id, f"🧠 **Đã lưu vào bộ nhớ vĩnh viễn:** \"{mem_fact}\"\nEm sẽ luôn nhớ thông tin này trong mọi cuộc trò chuyện sau!", reply_to_message_id=message_id)
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

        # Match relevant skills
        matched_skills = match_relevant_skills(text)
        if matched_skills:
            logger.info(f"Matched {len(matched_skills)} skills: {[s['name'] for s in matched_skills]}")

        # Dynamic Model Selection
        chosen_model, mode_tag = select_model_for_task(user_query, chat_id=chat_id)
        logger.info(f"Dynamic Router selected: {chosen_model} ({mode_tag}) for text: {text[:50]}")
        
        reply = query_llm(chat_id, user_query, chosen_model=chosen_model, matched_skills=matched_skills)
        send_message(chat_id, reply, reply_to_message_id=message_id)
        set_message_reaction(chat_id, message_id, "🔥")

    finally:
        typing_keeper.stop()

# ==========================================================
# 24/7 Cloud Long Polling Loop (Direct Telegram Connection)
# ==========================================================

def cloud_polling_loop():
    logger.info("Starting Cloud Long Polling Loop with Ultimate Suite...")
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
