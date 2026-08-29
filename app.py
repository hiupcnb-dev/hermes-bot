import os
import re
import sys
import time
import json
import base64
import logging
import threading
import datetime
from io import BytesIO
import requests
from flask import Flask, request, jsonify

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
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.7-flash")
ALLOWED_USERS_RAW = os.getenv("TELEGRAM_ALLOWED_USERS", "8322961603")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://hermes-bot-drl1.onrender.com")

ALLOWED_USERS = set()
for uid in ALLOWED_USERS_RAW.split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USERS.add(int(uid))

FALLBACK_MODELS = ["gemini-3.7-flash", "gemini-3.6-flash", "gpt-5.4-mini", "grok-4.5"]

# In-memory data structures
conversation_history = {}
user_memories = {}  # {chat_id: ["sở thích...", "dự án..."]}
pending_reminders = []  # [{"chat_id": int, "due_time": float, "text": str}]
MAX_HISTORY_TURNS = 20

# Flask web app for Render Webhook & Health Checks
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Hermes Telegram Super-Bot 24/7 (Webhook + Anti-Sleep Edition)",
        "model": MODEL_NAME,
        "features": ["vision_multimodal", "file_reader", "live_web_search", "reminders_and_memory", "webhook_push"],
        "pending_reminders_count": len(pending_reminders),
        "timestamp": time.time()
    }), 200

# ==========================================================
# Feature: Telegram Webhook (Instant Push & Zero Sleep)
# ==========================================================

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """Telegram pushes updates here via HTTPS Webhook"""
    update = request.get_json(force=True, silent=True)
    if update:
        # Process in separate background worker thread so Telegram gets instant 200 OK
        threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return jsonify({"ok": True}), 200

@app.route("/setup-webhook")
def setup_webhook_route():
    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
    res = send_telegram_request("setWebhook", {
        "url": webhook_url,
        "drop_pending_updates": False
    })
    return jsonify({
        "webhook_url": webhook_url,
        "telegram_response": res
    }), 200

def auto_setup_webhook():
    """Register Webhook on startup after 5 seconds"""
    time.sleep(5)
    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
    logger.info(f"Setting Telegram Webhook to {webhook_url}...")
    res = send_telegram_request("setWebhook", {
        "url": webhook_url,
        "drop_pending_updates": False
    })
    logger.info(f"Webhook setup result: {res}")

# ==========================================================
# Anti-Sleep Keep-Alive Loop (Self-Ping every 9 mins)
# ==========================================================

def anti_sleep_keep_alive():
    """Pings self every 9 minutes so Render Free Tier NEVER sleeps"""
    logger.info("Anti-Sleep Keep-Alive loop started.")
    time.sleep(30)
    while True:
        try:
            url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/health"
            r = requests.get(url, timeout=15)
            logger.info(f"Keep-Alive ping to {url}: Status {r.status_code}")
        except Exception as e:
            logger.warning(f"Keep-Alive ping error: {e}")
        time.sleep(9 * 60) # Ping every 9 minutes (Render timeout is 15 mins)

# ==========================================================
# Telegram API Helpers
# ==========================================================

def send_telegram_request(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=35)
        return r.json()
    except Exception as e:
        logger.error(f"Error calling Telegram {method}: {e}")
        return None

def send_chat_action(chat_id, action="typing"):
    send_telegram_request("sendChatAction", {"chat_id": chat_id, "action": action})

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
    """Download a file from Telegram by file_id and return (bytes, file_name)"""
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
# Feature 2: Real-time Live Web Search
# ==========================================================

def search_web_ddg(query, max_results=4):
    """Perform real-time web search via DuckDuckGo HTML scraper"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        url = "https://html.duckduckgo.com/html/"
        data = {"q": query, "b": ""}
        r = requests.post(url, data=data, headers=headers, timeout=10)
        if r.status_code != 200:
            return ""
        
        results = []
        snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', r.text, re.DOTALL)
        titles = re.findall(r'<a class="result__url[^>]*>(.*?)</a>', r.text, re.DOTALL)
        
        for i, snippet in enumerate(snippets[:max_results]):
            clean_snippet = re.sub(r'<[^>]+>', '', snippet).strip()
            clean_title = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ""
            if clean_snippet:
                results.append(f"- {clean_snippet} (Nguồn: {clean_title})")
        
        return "\n".join(results)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return ""

def should_search_web(query):
    keywords = [
        "hôm nay", "thời tiết", "tin tức", "giá vàng", "tỷ giá", "mới nhất", 
        "kết quả", "bóng đá", "ai vô địch", "search", "tìm kiếm", "hôm qua", 
        "ngày mai", "sự kiện", "livescore", "chứng khoán", "bitcoin"
    ]
    query_lower = query.lower()
    return any(kw in query_lower for kw in keywords)

# ==========================================================
# Feature 4: Reminder Parser & Background Scheduler
# ==========================================================

def parse_reminder(text):
    text_lower = text.lower()
    now = time.time()
    
    # 1. 'sau X phút / tiếng / giây'
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
        
    # 2. 'lúc HH:MM'
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
# Feature 5: File & Document Extraction
# ==========================================================

def extract_text_from_file(file_bytes, file_name):
    """Extract text from PDF, Docx, or plain text code files"""
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
# Core LLM Engine
# ==========================================================

def query_llm(chat_id, user_content, is_multimodal=False):
    system_prompt = (
        "Bạn là Hermes - trợ lý AI toàn năng, thông minh, hỗ trợ tận tâm bằng tiếng Việt chuẩn Markdown.\n"
        "Bạn có khả năng: phân tích hình ảnh, đọc tài liệu/code, tìm kiếm thông tin mới nhất và đặt lịch nhắc nhở."
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

    models_to_try = [MODEL_NAME] + [m for m in FALLBACK_MODELS if m != MODEL_NAME]
    last_error = ""

    for model in models_to_try:
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
            else:
                last_error = f"HTTP {r.status_code}: {r.text}"
                logger.warning(f"Model {model} failed: {last_error}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Model {model} error: {last_error}")

    return f"⚠️ Lỗi kết nối mô hình: {last_error}"

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

    # Command: /start
    if text == "/start":
        welcome = (
            "👋 **Chào anh! Em là Hermes AI Siêu Trợ Lý (Cloud 24/7 - Webhook Edition)!**\n\n"
            "✨ **Em đã được kích hoạt chạy 24/7 vĩnh viễn không bao giờ ngủ:**\n"
            "1. 👁️ **Mắt thần nhìn ảnh:** Gửi ảnh đề bài, ảnh lỗi màn hình, sản phẩm để em phân tích.\n"
            "2. 📄 **Đọc file tài liệu:** Gửi file PDF, Word, file code (.py, .java, .txt...) để em đọc và tóm tắt.\n"
            "3. 🌐 **Tìm kiếm Web trực tiếp:** Tự động tra cứu tin tức, giá cả, thời tiết theo thời gian thực.\n"
            "4. ⏰ **Hẹn giờ & Ghi nhớ:** Gõ *'Nhắc tôi sau 15 phút họp'* hoặc *'Hãy nhớ tôi thích cà phê đen'*."
        )
        send_message(chat_id, welcome, reply_to_message_id=message_id)
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
        send_chat_action(chat_id, "typing")
        best_photo = photos[-1]
        photo_bytes, _ = download_telegram_file(best_photo["file_id"])
        
        if photo_bytes:
            b64_img = base64.b64encode(photo_bytes).decode("utf-8")
            prompt_text = caption if caption else "Hãy xem kỹ bức ảnh này và phân tích, mô tả chi tiết nội dung hoặc giải đáp yêu cầu trong ảnh."
            
            content_payload = [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
            ]
            
            reply = query_llm(chat_id, content_payload, is_multimodal=True)
            send_message(chat_id, reply, reply_to_message_id=message_id)
        else:
            send_message(chat_id, "⚠️ Không thể tải ảnh từ Telegram, anh thử gửi lại nhé!", reply_to_message_id=message_id)
        return

    # Case B: Document
    if document:
        send_chat_action(chat_id, "typing")
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
            
            reply = query_llm(chat_id, full_prompt)
            send_message(chat_id, reply, reply_to_message_id=message_id)
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
        return

    # Web search
    send_chat_action(chat_id, "typing")
    user_query = text
    if should_search_web(text):
        logger.info(f"Triggering Web Search for: {text}")
        search_results = search_web_ddg(text)
        if search_results:
            user_query = (
                f"Câu hỏi của người dùng: {text}\n\n"
                f"[Thông tin tìm kiếm thời gian thực trên Internet]:\n{search_results}\n\n"
                f"Hãy tổng hợp thông tin trên một cách ngắn gọn, chính xác để trả lời người dùng."
            )

    reply = query_llm(chat_id, user_query)
    send_message(chat_id, reply, reply_to_message_id=message_id)

# ==========================================================
# Background Threads (Scheduler + Keep-Alive + Webhook Setup)
# ==========================================================

# 1. Reminder scheduler thread
reminder_thread = threading.Thread(target=reminder_scheduler_loop, daemon=True)
reminder_thread.start()

# 2. Anti-Sleep Keep-Alive thread
keep_alive_thread = threading.Thread(target=anti_sleep_keep_alive, daemon=True)
keep_alive_thread.start()

# 3. Webhook auto-setup thread
webhook_setup_thread = threading.Thread(target=auto_setup_webhook, daemon=True)
webhook_setup_thread.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
