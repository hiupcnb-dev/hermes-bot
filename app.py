import os
import sys
import time
import json
import logging
import threading
import requests
from flask import Flask, jsonify

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

ALLOWED_USERS = set()
for uid in ALLOWED_USERS_RAW.split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USERS.add(int(uid))

FALLBACK_MODELS = ["gemini-3.7-flash", "gemini-3.6-flash", "gpt-5.4-mini", "grok-4.5"]

# In-memory conversation history: {chat_id: [{"role": "...", "content": "..."}]}
conversation_history = {}
MAX_HISTORY_TURNS = 20

# Flask web app for Render health checks
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Hermes Telegram Bot (Cloud Edition)",
        "model": MODEL_NAME,
        "allowed_users": list(ALLOWED_USERS),
        "timestamp": time.time()
    }), 200

def send_telegram_request(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=30)
        return r.json()
    except Exception as e:
        logger.error(f"Error calling Telegram {method}: {e}")
        return None

def send_chat_action(chat_id, action="typing"):
    send_telegram_request("sendChatAction", {"chat_id": chat_id, "action": action})

def send_message(chat_id, text):
    # Telegram message limit is 4096 characters
    chunk_size = 4000
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        # Try sending with Markdown, fallback to plain text if syntax error
        res = send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown"
        })
        if not res or not res.get("ok"):
            send_telegram_request("sendMessage", {
                "chat_id": chat_id,
                "text": chunk
            })

def query_llm(chat_id, user_prompt):
    # Retrieve or initialize history
    if chat_id not in conversation_history:
        conversation_history[chat_id] = [
            {"role": "system", "content": "Bạn là Hermes - trợ lý AI thông minh, thân thiện, trả lời nhanh gọn, chính xác bằng tiếng Việt chuẩn markdown."}
        ]
    
    # Append user turn
    conversation_history[chat_id].append({"role": "user", "content": user_prompt})
    
    # Trim history if exceeding limit
    if len(conversation_history[chat_id]) > (MAX_HISTORY_TURNS * 2 + 1):
        system_msg = conversation_history[chat_id][0]
        recent_msgs = conversation_history[chat_id][-(MAX_HISTORY_TURNS * 2):]
        conversation_history[chat_id] = [system_msg] + recent_msgs

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    # Try main model first, then fallback models
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
                logger.warning(f"Model {model} failed with {last_error}, trying next model...")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Model {model} exception {last_error}, trying next model...")

    return f"⚠️ Rất tiếc, các nhà cung cấp mô hình đều gặp lỗi tạm thời: {last_error}"

def handle_update(update):
    message = update.get("message")
    if not message:
        return
    
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    from_user = message.get("from", {})
    user_id = from_user.get("id")
    text = message.get("text", "").strip()

    # Security check: only allowed users
    if ALLOWED_USERS and user_id not in ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized access attempt by user_id {user_id} (chat_id: {chat_id})")
        send_message(chat_id, "⛔ Bạn không có quyền sử dụng bot cá nhân này.")
        return

    if not text:
        return

    logger.info(f"Received message from {user_id}: {text}")

    if text == "/start":
        send_message(chat_id, "👋 Chào anh! Em là **Hermes AI Bot (Cloud Edition 24/7)** đã kết nối sẵn sàng với gói API của anh. Anh có thể hỏi em bất kỳ điều gì ngay cả khi tắt máy tính!")
        return
    elif text == "/reset":
        conversation_history.pop(chat_id, None)
        send_message(chat_id, "🔄 Đã làm mới ngữ cảnh trò chuyện!")
        return

    # Show typing status
    send_chat_action(chat_id, "typing")
    
    # Process with LLM
    response_text = query_llm(chat_id, text)
    
    # Reply back to Telegram
    send_message(chat_id, response_text)

def telegram_polling_loop():
    logger.info("Starting Telegram Polling Loop...")
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
                            logger.error(f"Error handling update: {e}")
            elif r.status_code == 409:
                logger.warning("Conflict: Another bot instance is polling. Waiting 10s...")
                time.sleep(10)
            else:
                logger.warning(f"getUpdates returned HTTP {r.status_code}")
                time.sleep(3)
        except Exception as e:
            logger.error(f"Polling loop error: {e}")
            time.sleep(5)

# Start Telegram polling thread automatically when loaded by Gunicorn or Python
bot_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
