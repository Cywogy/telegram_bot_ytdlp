import os
import re
import uuid
import shutil
import asyncio
import logging
from urllib.parse import urlparse
from dotenv import load_dotenv

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
log = logging.getLogger("video_bot")

# --- Токен берём из переменной окружения, а не хардкодим в коде ---
load_dotenv()
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Установите переменную окружения, например:\n"
        "  export BOT_TOKEN='ваш_токен_от_BotFather'\n"
        "ВАЖНО: если раньше токен был захардкожен и где-то засветился (например, в чате/репозитории), "
        "его нужно отозвать через @BotFather (/revoke) и выпустить новый."
    )

BASE_DOWNLOAD_DIR = "/tmp/video-bot"
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

# Bot API без локального сервера не даёт загружать файлы крупнее ~50 МБ
MAX_TELEGRAM_UPLOAD = 50 * 1024 * 1024

# Файл с куками (формат Netscape/cookies.txt) для сайтов, требующих авторизацию —
COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE")

# Через сколько минут "осиротевшие" временные папки (после краша/рестарта) удаляются
ORPHAN_MAX_AGE_MINUTES = 60
ORPHAN_CLEANUP_INTERVAL_SECONDS = 30 * 60

SUPPORTED_DOMAINS = {
    'youtube.com', 'youtu.be', 'tiktok.com', 'instagram.com',
    'twitter.com', 'x.com', 'facebook.com', 'fb.watch',
    'reddit.com', 'vimeo.com', 'vk.com',
}

# Ограничиваем число одновременных загрузок, чтобы не положить сервер
MAX_CONCURRENT_DOWNLOADS = 3
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Ищем первую ссылку в произвольном тексте сообщения
URL_RE = re.compile(r'https?://\S+')


def is_supported(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    host = host.removeprefix("www.").removeprefix("m.")
    return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)


def _blocking_download(url: str, work_dir: str) -> dict:
    """Синхронная функция yt-dlp — запускается в отдельном потоке."""
    ydl_opts = {
        'format': 'best[height<=720][filesize<50M]/best[height<=720]/worst',
        'outtmpl': f'{work_dir}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,          # не тянуть весь плейлист по ссылке на видео
        'restrictfilenames': True,   # безопасные имена файлов
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'socket_timeout': 30,
        # прерывает закачку заранее, если размер известен до начала скачивания
        # (для HLS/DASH без Content-Length не сработает — финальная проверка ниже подстрахует)
        'max_filesize': MAX_TELEGRAM_UPLOAD,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'extractor_args': {
            'youtube': {'player_client': ['android']},
        },
    }

    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts['cookiefile'] = COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info and 'entries' in info and info['entries']:
            # на случай, если всё же прилетела "коллекция" из одного элемента
            info = info['entries'][0]
        filename = ydl.prepare_filename(info)

        if not os.path.exists(filename):
            base = os.path.splitext(filename)[0]
            for ext in ['.mp4', '.webm', '.mkv', '.mov']:
                if os.path.exists(base + ext):
                    filename = base + ext
                    break

        return {"filename": filename, "title": info.get("title", "Видео")}


async def _keep_chat_action_alive(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """send_chat_action живёт в Telegram около 5 секунд — повторяем её,
    пока идёт скачивание (в т.ч. пока запрос стоит в очереди на семафоре)."""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def download_and_send(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # уникальная папка на каждую загрузку — исключает конфликты имён при параллельных запросах
    work_dir = os.path.join(BASE_DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)

    keepalive_task = asyncio.create_task(_keep_chat_action_alive(context, chat_id))

    try:
        async with download_semaphore:
            result = await asyncio.to_thread(_blocking_download, url, work_dir)

        filename = result["filename"]
        title = result["title"]

        if not os.path.exists(filename):
            await update.message.reply_text("❌ Файл не найден после скачивания")
            return

        file_size = os.path.getsize(filename)

        if file_size > MAX_TELEGRAM_UPLOAD:
            await update.message.reply_text(
                "⚠️ Файл больше 50 МБ — Telegram Bot API не позволяет боту его отправить "
                "(нужен локальный Bot API сервер). Попробуйте более низкое качество или другую ссылку."
            )
            return

        with open(filename, 'rb') as f:
            await context.bot.send_video(
                chat_id=chat_id,
                video=f,
                supports_streaming=True,
                write_timeout=120,
                read_timeout=120,
            )

        log.info(f"Видео '{title}' отправлено в чат {chat_id}, удаляю с сервера: {filename}")

        # Удаляем исходное сообщение со ссылкой, как и просили
        try:
            await update.message.delete()
        except TelegramError as e:
            # боту может не хватать прав на удаление (например, в группе он не админ)
            log.warning(f"Не удалось удалить сообщение: {e}")

    except yt_dlp.utils.DownloadError as e:
        log.error(f"Download error: {e}")
        err_text = str(e).lower()
        if "instagram" in url.lower() and (
            "login" in err_text or "rate-limit" in err_text or "restricted" in err_text
        ):
            await update.message.reply_text(
                "❌ Instagram требует авторизацию для этого контента "
                "(это нормально — так у большинства постов/reels сейчас).\n\n"
                "Нужно один раз подключить файл с куками (YTDLP_COOKIES_FILE) — "
                "см. инструкцию в README/комментариях к боту."
            )
        elif "file is larger than max-filesize" in err_text:
            await update.message.reply_text(
                "⚠️ Видео больше 50 МБ — Telegram Bot API не позволяет боту его отправить. "
                "Попробуйте более низкое качество или другую ссылку."
            )
        else:
            await update.message.reply_text(
                "❌ Ошибка скачивания.\n"
                "Возможно, видео недоступно, приватное или заблокировано.\n\n"
                "Попробуйте:\n• Другую ссылку\n• Проверить доступность видео в браузере"
            )
    except TelegramError as e:
        log.error(f"Telegram error: {e}")
        await update.message.reply_text(f"❌ Не получилось отправить видео: {e}")
    except Exception as e:
        log.exception("Unexpected error")
        await update.message.reply_text(f"❌ Ошибка: {e}")
    finally:
        keepalive_task.cancel()
        shutil.rmtree(work_dir, ignore_errors=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = URL_RE.search(text)
    is_group = update.effective_chat.type in ("group", "supergroup")

    if not match:
        if not is_group:
            await update.message.reply_text("📎 Отправьте ссылку на видео")
        return

    url = match.group(0).rstrip('.,!?)»"\'')  # отсечь пунктуацию на конце фразы

    if not is_supported(url):
        if not is_group:
            await update.message.reply_text(
                "⚠️ Сайт не поддерживается.\n"
                "✅ YouTube, TikTok, Instagram, Twitter/X, VK, Facebook, Reddit, Vimeo"
            )
        return

    await download_and_send(url, update, context)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Отправьте ссылку на видео!\n\n"
        "✅ YouTube, TikTok, Instagram, Twitter/X, VK и другие"
    )


async def orphan_cleanup_loop():
    """Подчищает временные папки, которые могли остаться после краша/kill -9
    во время активной загрузки (обычный finally в download_and_send их не задевает)."""
    import time
    while True:
        await asyncio.sleep(ORPHAN_CLEANUP_INTERVAL_SECONDS)
        try:
            now = time.time()
            for name in os.listdir(BASE_DOWNLOAD_DIR):
                path = os.path.join(BASE_DOWNLOAD_DIR, name)
                age_minutes = (now - os.path.getmtime(path)) / 60
                if age_minutes > ORPHAN_MAX_AGE_MINUTES:
                    shutil.rmtree(path, ignore_errors=True)
                    log.info(f"Удалена осиротевшая папка: {path}")
        except Exception:
            log.exception("Ошибка при фоновой очистке")


async def on_startup(app: Application):
    # чистим временную папку от файлов, оставшихся после предыдущего запуска/краша
    shutil.rmtree(BASE_DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)
    asyncio.create_task(orphan_cleanup_loop())


def main():
    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_startup)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Бот запущен!")
    application.run_polling()


if __name__ == "__main__":
    main()