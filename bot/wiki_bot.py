import os
import re
import sys
from pathlib import Path
from threading import Thread
import telebot
from openai import OpenAI
from flask import Flask

# Load environment variables from ollama.env in parent folder
env_path = Path(__file__).parent.parent / "ollama.env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ[key.strip()] = val.strip()

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemini-3-flash-preview")

# Normalize base_url
if not OLLAMA_BASE_URL.endswith("/v1") and "v1" not in OLLAMA_BASE_URL:
    if not OLLAMA_BASE_URL.endswith("/"):
        OLLAMA_BASE_URL += "/"
    OLLAMA_BASE_URL += "v1"

# Initialize services
if not TELEGRAM_BOT_TOKEN:
    print("WARNING: TELEGRAM_BOT_TOKEN is not set in ollama.env!")
if not OLLAMA_API_KEY:
    print("WARNING: OLLAMA_API_KEY is not set in ollama.env!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

openai_client = OpenAI(
    api_key=OLLAMA_API_KEY,
    base_url=OLLAMA_BASE_URL
) if OLLAMA_API_KEY else None

WIKI_PATH = Path(__file__).parent.parent / "wiki-llm"
RAW_PATH = WIKI_PATH / "raw"

class WikiSearcher:
    def __init__(self, raw_path: Path):
        self.raw_path = raw_path
        
    def search(self, query: str, top_k=3):
        words = [w.lower() for w in re.findall(r'\w+', query) if len(w) > 2]
        if not words:
            return []
            
        results = []
        for root, _, files in os.walk(self.raw_path):
            for file in files:
                if file.endswith(".txt"):
                    file_path = Path(root) / file
                    try:
                        content = file_path.read_text(encoding="utf-8")
                    except:
                        try:
                            content = file_path.read_text(encoding="latin-1")
                        except:
                            continue
                            
                    score = 0
                    content_lower = content.lower()
                    
                    for word in words:
                        score += content_lower.count(word)
                        
                    if score > 0:
                        lines = content.splitlines()
                        title = file_path.stem
                        url = ""
                        date = ""
                        
                        for line in lines[:10]:
                            if line.startswith("Заголовок:"):
                                title = line[len("Заголовок:"):].strip()
                            elif line.startswith("URL:"):
                                url = line[len("URL:"):].strip()
                            elif line.startswith("Дата:"):
                                date = line[len("Дата:"):].strip()
                                
                        text_content = ""
                        if "Текст:" in content:
                            text_content = content.split("Текст:", 1)[1].strip()
                        else:
                            text_content = content
                            
                        results.append({
                            "title": title,
                            "url": url,
                            "date": date,
                            "content": text_content,
                            "score": score
                        })
                        
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

searcher = WikiSearcher(RAW_PATH)

def clean_telegram_html(text: str) -> str:
    # Convert HTML lists which Telegram doesn't support to plain text lists
    text = text.replace("<ul>", "").replace("</ul>", "")
    text = text.replace("<ol>", "").replace("</ol>", "")
    text = text.replace("<li>", "• ").replace("</li>", "\n")
    
    # Strip any other unsupported HTML tags if LLM accidentally outputs them
    allowed_tags = ["b", "i", "code", "a", "pre", "u", "s", "tg-spoiler"]
    
    # Remove tag attributes except for 'href' inside 'a'
    def sanitize_tag(match):
        tag_content = match.group(1).strip()
        is_closing = tag_content.startswith("/")
        tag_name = tag_content.lstrip("/").split()[0].lower()
        
        if tag_name not in allowed_tags:
            return ""
            
        if is_closing:
            return f'</{tag_name}>'
            
        if tag_name == "a" and "href=" in tag_content:
            href_match = re.search(r'href=["\'](https?://[^"\']+)["\']', tag_content)
            if href_match:
                return f'<a href="{href_match.group(1)}">'
            return ""
            
        return f'<{tag_name}>'
        
    text = re.sub(r'<([^>]+)>', sanitize_tag, text)
    return text

def ask_llm(query: str, context: str):
    if not openai_client:
        return "Ошибка: Не настроен доступ к LLM (отсутствует OLLAMA_API_KEY)."
        
    system_prompt = (
        "Вы — опытный и авторитетный эксперт по охране труда и промышленной безопасности.\n"
        "Отвечайте на вопросы пользователя четко, грамотно и структурировано.\n"
        "Для ответа используйте ИСКЛЮЧИТЕЛЬНО предоставленный ниже контекст из статей.\n"
        "Если в контексте нет информации по вопросу, честно ответьте, что информации в базе данных нет, "
        "но постарайтесь дать общий совет эксперта.\n"
        "В конце вашего ответа ОБЯЗАТЕЛЬНО приведите ссылки на использованные статьи в блоке 'Источники:'.\n"
        "Используйте HTML-теги для форматирования в Telegram (<b>жирный</b>, <i>курсив</i>, <code>код</code>, <a href='ссылка'>анкор</a>).\n"
        "КРИТИЧЕСКИ ВАЖНО: Не используйте HTML-теги списков (<ul>, <ol>, <li>). Для списков пишите дефисы (-) или маркеры (•) текстом.\n\n"
        f"Контекст:\n{context}"
    )
    
    try:
        response = openai_client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query}
            ],
            temperature=0.3
        )
        raw_content = response.choices[0].message.content
        return clean_telegram_html(raw_content)
    except Exception as e:
        return f"Ошибка при обращении к ИИ: {e}"

if bot:
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(
            message,
            "👋 Привет! Я бот-ассистент по Охране Труда и Промышленной Безопасности.\n\n"
            "Задайте мне любой вопрос, и я найду ответ в нашей базе знаний Wiki-LLM.\n"
            "Пример вопроса: <code>Как правильно проводить СОУТ?</code>",
            parse_mode="HTML"
        )

    @bot.message_handler(func=lambda message: True)
    def handle_message(message):
        user_query = message.text
        bot.send_chat_action(message.chat.id, 'typing')
        
        relevant_docs = searcher.search(user_query, top_k=3)
        
        if not relevant_docs:
            context = "Контекст пуст. Статей по теме не найдено."
        else:
            context_blocks = []
            for doc in relevant_docs:
                block = f"--- СТАТЬЯ: {doc['title']} (Ссылка: {doc['url']}) ---\n{doc['content']}\n"
                context_blocks.append(block)
            context = "\n".join(context_blocks)
            
        answer = ask_llm(user_query, context)
        bot.reply_to(message, answer, parse_mode="HTML")

# Flask Web Server for Render compatibility
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    # Start web server in a background thread
    Thread(target=run_web, daemon=True).start()
    
    if not TELEGRAM_BOT_TOKEN or not OLLAMA_API_KEY:
        print("Please configure TELEGRAM_BOT_TOKEN and OLLAMA_API_KEY in ollama.env first.")
        sys.exit(1)
        
    print("Starting Telegram Bot...")
    bot.infinity_polling()
