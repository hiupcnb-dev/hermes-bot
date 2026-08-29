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
MAX_HISTORY_TURNS = 20

# Flask web app for Render Keep-Alive & Health Checks
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Hermes Telegram Super-Bot 24/7 (Real-Time Web Search & Reader Edition)",
        "default_frontier_model": "gpt-5.6-sol",
        "features": [
            "supercharged_live_web_search",
            "url_web_page_reader",
            "instant_emoji_reactions",
            "continuous_typing_feedback",
            "smart_model_router", 
            "vision_multimodal", 
            "file_reader", 
            "reminders_and_memory", 
            "24_7_long_polling"
        ],
        "pending_reminders_count": len(pending_reminders),
        "timestamp": time.time()
    }), 200

# ==========================================================
# Telegram Visual Feedback Helpers (Reactions & Typing)
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

def send_message(chat_id, text, reply_to_message_id=None):
    chunk_size = 4000
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        if reply_to_message_id and i == 0:
            payload["reply_to_message_id"] = reply_to_message_id
        res = send_telegram_request("sendMessage", payload)
        if not res or not res.get("ok"):
            payload.pop("parse_mode", None)
            send_telegram_request("sendMessage", payload)

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

# ==========================================================
# Feature: Supercharged Real-Time Web Search & Web Reader
# ==========================================================

def search_live_web(query, max_results=6):
    """Searches live web using robust multi-result Bing engine with Vietnamese locale"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    
    results = []
    try:
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers, timeout=10)
        snippets = re.findall(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', r.text, re.DOTALL)
        if not snippets:
            snippets = re.findall(r'<div class="b_caption"[^>]*><p[^>]*>(.*?)</p>', r.text, re.DOTALL)
            
        for s in snippets[:max_results]:
            clean = re.sub(r'<[^>]+>', '', s).strip()
            clean = html.unescape(clean)
            if clean and clean not in results:
                results.append(f"• {clean}")
    except Exception as e:
        logger.error(f"Bing search error: {e}")

    try:
        titles_links = re.findall(r'<li class="b_algo">.*?<h2><a href="([^"]*)"[^>]*>(.*?)</a></h2>', r.text, re.DOTALL)
        for link, title in titles_links[:max_results]:
            clean_title = html.unescape(re.sub(r'<[^>]+>', '', title).strip())
            results.append(f"📌 Nguồn: [{clean_title}] ({link})")
    except Exception:
        pass

    return "\n".join(results)

def fetch_url_content(url):
    """Fetches text content from a given web URL"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            # Strip script and style tags
            clean_html = re.sub(r'<script[^>]*>.*?</script>', '', r.text, flags=re.DOTALL | re.IGNORECASE)
            clean_html = re.sub(r'<style[^>]*>.*?</style>', '', clean_html, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r'<[^>]+>', ' ', clean_html)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            return html.unescape(clean_text)[:8000]
    except Exception as e:
        logger.error(f"Error fetching URL {url}: {e}")
    return ""

def should_search_web(query):
    """Intelligently detects if user prompt needs real-time live internet information"""
    if re.search(r'https?://[^\s]+', query):
        return True

    keywords = [
        "giá", "buffet", "quán", "nhà hàng", "menu", "thực đơn", "review", "đánh giá",
        "facebook", "ở đâu", "địa chỉ", "số điện thoại", "mấy giờ", "mở cửa", "khuyến mãi",
        "hôm nay", "hôm qua", "ngày mai", "tin tức", "thời tiết", "giá vàng", "tỷ giá",
        "mới nhất", "kết quả", "bóng đá", "ai vô địch", "search", "tìm kiếm", "tìm", "tra",
        "sự kiện", "livescore", "chứng khoán", "bitcoin", "ninh bình", "hà nội", "sài gòn",
        "bbq", "lẩu", "nướng", "2/9", "lễ", "du lịch", "khách sạn", "vé máy bay"
    ]
    query_lower = query.lower()
    return any(kw in query_lower for kw in keywords)

# ==========================================================
# Feature: Intelligent Dynamic Model Router
# ==========================================================

def select_model_for_task(text, has_photo=False, has_doc=False, file_name="", chat_id=None):
    if chat_id and chat_id in user_model_override and user_model_override[chat_id]:
        override = user_model_override[chat_id]
        return override, f"⚙️ [Chế độ Cố định: {override}]"

    if has_photo:
        return "gpt-5.6-sol", "👁️ [Phân tích Thị giác - GPT-5.6 Sol]"

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
        "gpt-5.3-codex-spark",
        "claude-sonnet-4-6",
        "gpt-5.4-mini",
        "grok-4.5",
        "gemini-3.7-flash"
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
# Feature: Reminder Parser & Background Scheduler
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
    logger.info("Reminder scheduler loop started.")
    while True:
        try:
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
            logger.error(f"Error in reminder scheduler: {e}")
        time.sleep(5)

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
    system_prompt = (
        "Bạn là Hermes - siêu trợ lý AI toàn năng, thông thái, trả lời chuẩn Markdown bằng tiếng Việt tự nhiên.\n"
        "Bạn ĐÃ ĐƯỢC TÍCH HỢP HỆ THỐNG TÌM KIẾM INTERNET VÀ ĐỌC WEB THỜI GIAN THỰC (Real-Time Live Web Search).\n"
        "Khi có dữ liệu tìm kiếm thời gian thực được cung cấp, bạn hãy tổng hợp đầy đủ, rõ ràng và chi tiết (giá cả, địa chỉ, số điện thoại, thực đơn, đánh giá) để trả lời người dùng một cách chính xác và thuyết phục nhất.\n"
        "Tuyệt đối không bao giờ nói 'tôi không có công cụ duyệt web' hay từ chối tìm kiếm."
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
# Telegram Update Handler
# ==========================================================

def handle_update(update):
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
        if text == "/start":
            welcome = (
                "👋 **Chào anh! Em là Hermes AI Siêu Trợ Lý (Real-Time Web Search & Reader)!**\n\n"
                "🧠 **Các Tính Năng Thông Minh Đã Được Kích Hoạt 24/7:**\n"
                "• 🌐 **Duyệt Web & Tìm Kiếm Trực Tuyến:** Tra cứu giá cả, quán ăn, menu, tin tức theo thời gian thực.\n"
                "• 💻 **Lập Trình Chuyên Sâu:** Tự động chuyển **`GPT-5.3 Codex`** khi hỏi code, sửa bug.\n"
                "• 🧠 **Siêu Trí Tuệ Flagship:** Dùng **`GPT-5.6 Sol`** để suy luận logic và giải quyết vấn đề.\n"
                "• 👁️ **Mắt Thần Nhìn Ảnh:** Gửi ảnh để bot phân tích chi tiết.\n"
                "• 📄 **Đọc File:** Đọc và tóm tắt file PDF, Word, File Code.\n"
                "• ⏰ **Hẹn Giờ & Ghi Nhớ:** Tự động nhắc nhở và lưu trí nhớ cá nhân.\n\n"
                "🛠️ **Lệnh điều khiển:** `/help`, `/model`, `/reset`, `/memo`"
            )
            send_message(chat_id, welcome, reply_to_message_id=message_id)
            return

        # Command: /help
        if text == "/help":
            help_text = (
                "📖 **DANH SÁCH LỆNH VÀ HƯỚNG DẪN TIẾNG VIỆT**\n\n"
                "1. 🚀 **/start** — Khởi động và xem thông tin bot.\n"
                "2. 🔄 **/reset** — Làm mới cuộc trò chuyện, xóa ngữ cảnh cũ.\n"
                "3. 🧠 **/memo** — Xem các thông tin cá nhân/sở thích bot đang nhớ về anh.\n"
                "4. ⚙️ **/model [auto | sol | code | claude | terra]** — Đổi model AI:\n"
                "   • `auto`: Tự động nhận diện câu hỏi để chọn model phù hợp nhất.\n"
                "   • `sol`: GPT-5.6 Sol (Mạnh nhất, suy luận logic sâu).\n"
                "   • `code`: GPT-5.3 Codex (Chuyên lập trình, sửa bug code).\n"
                "   • `claude`: Claude Sonnet 4.6 (Văn phong cao cấp, viết lách).\n"
                "   • `terra`: GPT-5.6 Terra (Phản hồi 1 giây, siêu tiết kiệm).\n\n"
                "✨ **CÁC TÍNH NĂNG TỰ ĐỘNG KHÔNG CẦN LỆNH:**\n"
                "• 🌐 **Tìm kiếm Web & Tra cứu:** Nhắn bất kỳ câu hỏi nào cần tra cứu giá cả, quán ăn, thời tiết, tin tức.\n"
                "• 👁️ **Gửi ảnh:** Bot tự động nhìn ảnh và phân tích.\n"
                "• 📄 **Gửi file (.pdf, .docx, file code):** Bot tự động đọc và tóm tắt.\n"
                "• ⏰ **Hẹn giờ:** Nhắn *'Nhắc anh sau 10 phút...'* hoặc *'Nhắc tôi lúc 08:00...'*.\n"
                "• 🧠 **Ghi nhớ:** Nhắn *'Hãy nhớ rằng tôi thích...'* để bot lưu vào bộ nhớ."
            )
            send_message(chat_id, help_text, reply_to_message_id=message_id)
            return

        # Command: /model
        if text.startswith("/model"):
            parts = text.split()
            if len(parts) > 1:
                m_arg = parts[1].lower()
                if m_arg in ["auto", "default"]:
                    user_model_override.pop(chat_id, None)
                    send_message(chat_id, "✅ Đã bật chế độ **Tự Động Định Tuyến Model Thông Minh (Smart Router)**!", reply_to_message_id=message_id)
                elif m_arg in ["sol", "gpt-5.6-sol"]:
                    user_model_override[chat_id] = "gpt-5.6-sol"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.6 Sol** (Siêu suy luận)!", reply_to_message_id=message_id)
                elif m_arg in ["code", "codex", "gpt-5.3-codex-spark"]:
                    user_model_override[chat_id] = "gpt-5.3-codex-spark"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.3 Codex Spark** (Chuyên lập trình)!", reply_to_message_id=message_id)
                elif m_arg in ["claude", "sonnet", "claude-sonnet-4-6"]:
                    user_model_override[chat_id] = "claude-sonnet-4-6"
                    send_message(chat_id, "✅ Đã cố định Model: **Claude Sonnet 4.6**!", reply_to_message_id=message_id)
                elif m_arg in ["terra", "gpt-5.6-terra"]:
                    user_model_override[chat_id] = "gpt-5.6-terra"
                    send_message(chat_id, "✅ Đã cố định Model: **GPT-5.6 Terra** (Siêu tốc & Tiết kiệm)!", reply_to_message_id=message_id)
                else:
                    send_message(chat_id, "⚠️ Cú pháp: `/model [auto | sol | code | claude | terra]`", reply_to_message_id=message_id)
            else:
                current = user_model_override.get(chat_id, "Tự động (Smart Router)")
                send_message(chat_id, f"ℹ️ Model hiện tại của anh: **{current}**\nĐổi model bằng cách gõ: `/model [auto | sol | code | claude | terra]`", reply_to_message_id=message_id)
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

        # Case A: Photo (Vision)
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

        # Case B: Document
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

        # Case C: Text
        if not text:
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

        # Real-time Web Search & URL Reader
        user_query = text
        
        # Check for URL link in text
        url_match = re.search(r'(https?://[^\s]+)', text)
        if url_match:
            target_url = url_match.group(1)
            logger.info(f"Fetching URL content for: {target_url}")
            page_text = fetch_url_content(target_url)
            if page_text:
                user_query = (
                    f"Câu hỏi của người dùng: {text}\n\n"
                    f"[Nội dung trang web thu thập được từ {target_url}]:\n{page_text}\n\n"
                    f"Hãy đọc nội dung trên và trả lời chi tiết yêu cầu của người dùng."
                )
        elif should_search_web(text):
            logger.info(f"Triggering Supercharged Live Web Search for: {text}")
            search_results = search_live_web(text)
            if search_results:
                user_query = (
                    f"Câu hỏi của người dùng: {text}\n\n"
                    f"[Dữ liệu tìm kiếm thời gian thực trên Internet]:\n{search_results}\n\n"
                    f"Hãy tổng hợp các dữ liệu tìm kiếm thực tế trên để đưa ra câu trả lời chi tiết, chính xác, nêu rõ giá cả, địa điểm, thực đơn, đánh giá cụ thể cho người dùng."
                )

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
    logger.info("Starting Cloud Long Polling Loop with Supercharged Web Search...")
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

# 1. Reminder scheduler thread
reminder_thread = threading.Thread(target=reminder_scheduler_loop, daemon=True)
reminder_thread.start()

# 2. Anti-Sleep Keep-Alive thread
keep_alive_thread = threading.Thread(target=anti_sleep_keep_alive, daemon=True)
keep_alive_thread.start()

# 3. Direct Cloud Polling thread (runs on Render 24/7)
polling_thread = threading.Thread(target=cloud_polling_loop, daemon=True)
polling_thread.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
