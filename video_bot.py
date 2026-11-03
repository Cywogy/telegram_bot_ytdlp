import os
import random
import shutil
import asyncio
import logging
import tempfile
from weakref import WeakValueDictionary
from contextlib import asynccontextmanager
from formats import candidates
from bot_store import Store
from video_links import SITE_NAMES, URL_RE, first_supported_link
from dotenv import load_dotenv

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
log = logging.getLogger("video_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Токен берём из переменной окружения, а не хардкодим в коде ---
load_dotenv()
TOKEN = os.environ.get("BOT_TOKEN")
BASE_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "video-bot")

# Bot API без локального сервера не даёт загружать файлы крупнее ~50 МБ
MAX_TELEGRAM_UPLOAD = 50 * 1024 * 1024

# Файл с куками (формат Netscape/cookies.txt) для сайтов, требующих авторизацию —
COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE")

# Ограничиваем число одновременных загрузок, чтобы не положить сервер
MAX_CONCURRENT_DOWNLOADS = 3
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Слабые ссылки освобождают блокировку после последнего ожидающего запроса.
locks = WeakValueDictionary()


@asynccontextmanager
async def share_format(key):
    if key is None:
        yield
    else:
        lock = locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield


def inspect_video(url, options):
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("_type") in ("playlist", "multi_video"):
        raise ValueError("Требуется ссылка на отдельное видео")
    return info


def fetch_format(url, options, fmt):
    with yt_dlp.YoutubeDL(dict(options, format=fmt["format_id"])) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if os.path.isfile(path):
        return path
    return None


async def _keep_chat_action_alive(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """send_chat_action живёт в Telegram около 5 секунд — повторяем её,
    пока идёт скачивание (в т.ч. пока запрос стоит в очереди на семафоре)."""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            await asyncio.sleep(4)
    except TelegramError as error:
        log.debug("Индикатор загрузки недоступен: %s", error)


async def deliver(url, message, context, ceiling, work_dir):
    store = context.bot_data["store"]
    options = dict(quiet=True, no_warnings=True, noplaylist=True, playlistend=1,
                   socket_timeout=30,
                   max_filesize=MAX_TELEGRAM_UPLOAD,
                   outtmpl=os.path.join(work_dir, "%(id)s.%(format_id)s.%(ext)s"),
                   restrictfilenames=True)
    try:
        info = await asyncio.to_thread(inspect_video, url, options)
    except yt_dlp.utils.DownloadError:
        if not COOKIES_FILE or not os.path.isfile(COOKIES_FILE):
            raise
        # У каждого загрузчика своя копия изменяемого cookie jar.
        options["cookiefile"] = shutil.copyfile(COOKIES_FILE, os.path.join(work_dir, "cookies.txt"))
        info = await asyncio.to_thread(inspect_video, url, options)

    formats = candidates(info, ceiling, MAX_TELEGRAM_UPLOAD)
    if not formats:
        await message.reply_text(f"Нет готового видео со звуком до {ceiling}p в пределах 50 МБ.")
        return False
    fmt = formats[0]
    source = f"{info['extractor_key']}:{info['id']}"
    key = None
    if ("cookiefile" not in options and info.get("availability") in (None, "public")
            and not info.get("is_live")):
        key = (context.bot.id, source, fmt["format_id"])
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть оригинал", url=url)]])
    async with share_format(key):
        cached = store.file_id(key) if key else None
        if cached:
            try:
                await message.reply_video(cached, do_quote=False, reply_markup=keyboard)
                return True
            except BadRequest as error:
                if "file identifier" not in str(error).lower() and "file_id" not in str(error).lower():
                    raise
                store.forget(key)
        path = await asyncio.to_thread(fetch_format, url, options, fmt)
        if path is None or os.path.getsize(path) > MAX_TELEGRAM_UPLOAD:
            lower = [f["height"] for f in formats if 0 < (f.get("height") or 0)
                     < (fmt.get("height") or 0) and f["height"] <= 480]
            retry = None
            if lower and message.from_user:
                height = max(lower)
                retry = InlineKeyboardMarkup([[InlineKeyboardButton(
                    f"Попробовать {height}p", callback_data=f"retry:{message.from_user.id}:{height}"
                )]])
            await message.reply_text("Видео не удалось скачать в пределах 50 МБ.",
                                     reply_to_message_id=message.message_id, reply_markup=retry)
            return False
        with open(path, "rb") as video:
            sent = await message.reply_video(video, do_quote=False, supports_streaming=True, reply_markup=keyboard,
                                            write_timeout=120, read_timeout=120)
        if key and sent.video:
            store.save(key, sent.video.file_id)
        return True


async def download_and_send(url, message, context, ceiling):
    keepalive = asyncio.create_task(_keep_chat_action_alive(context, message.chat_id))
    try:
        async with download_semaphore:
            with tempfile.TemporaryDirectory(dir=BASE_DOWNLOAD_DIR) as work_dir:
                sent = await deliver(url, message, context, ceiling, work_dir)
        if sent and message.text:
            group = message.chat.type in ("group", "supergroup")
            keep = context.bot_data["store"].preference(context.bot.id, message.chat_id, "keep", 0)
            if not group or (not keep and message.text.strip() == url):
                try:
                    await message.delete()
                except TelegramError as error:
                    log.warning("Не удалось удалить сообщение: %s", error)
    except (yt_dlp.utils.DownloadError, ValueError):
        await message.reply_text("Не удалось скачать видео. Проверьте доступность ссылки.")
    except Exception:
        log.exception("Ошибка обработки видео")
        await message.reply_text("Не удалось отправить видео. Попробуйте позже.")
    finally:
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)


async def retry_download(update, context):
    query = update.callback_query
    _, owner, height = query.data.split(":")
    if query.from_user.id != int(owner):
        await query.answer("Повтор доступен автору сообщения.")
        return
    original = getattr(query.message, "reply_to_message", None)
    url = first_supported_link(original.text or original.caption or "") if original else None
    if not url:
        await query.answer("Пришлите исходную ссылку ещё раз.")
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        return
    await download_and_send(url, original, context, int(height))


async def quality(update, context):
    user = update.effective_user.id
    store = context.bot_data["store"]
    choices = {"auto": 720, "480": 480}
    if len(context.args) == 1 and context.args[0] in choices:
        store.set_preference(context.bot.id, user, "quality", choices[context.args[0]])
    current = store.preference(context.bot.id, user, "quality", 720)
    await update.message.reply_text(f"Ваш предел — {current}p. Выбор: /quality auto или /quality 480.")


async def keep_links(update, context):
    message = update.message
    if message.chat.type not in ("group", "supergroup"):
        await message.reply_text("Эта настройка доступна в группе.")
        return
    store = context.bot_data["store"]
    if context.args in (["on"], ["off"]):
        member = await context.bot.get_chat_member(message.chat_id, update.effective_user.id)
        if member.status not in ("administrator", "creator"):
            await message.reply_text("Изменить настройку может администратор.")
            return
        store.set_preference(context.bot.id, message.chat_id, "keep", int(context.args == ["on"]))
    keep = store.preference(context.bot.id, message.chat_id, "keep", 0)
    mode = "все исходные сообщения" if keep else "сообщения с комментарием и медиа"
    await message.reply_text(f"Сохраняются {mode}. Настройка: /keeplinks on или /keeplinks off.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    text = message.text or message.caption or ""
    url = first_supported_link(text)
    if url:
        owner = update.effective_user.id if update.effective_user else 0
        ceiling = context.bot_data["store"].preference(context.bot.id, owner, "quality", 720)
        await download_and_send(url, message, context, ceiling)
    elif update.effective_chat.type == "private":
        if URL_RE.search(text):
            await message.reply_text(f"⚠️ Сайт не поддерживается. Доступны {SITE_NAMES}.")
        elif message.text:
            await message.reply_text("📎 Отправьте ссылку на видео")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = random.choice((
        "🏓 Понг! Отбил пакет крышкой от сервера.",
        "🤖 Жив! Притворялся калькулятором, пока ты не написал.",
        "📼 На связи! Перемотал кассету и жду ссылку.",
    ))
    if update.callback_query:
        await update.callback_query.answer(reply)
    else:
        await update.effective_message.reply_text(reply)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📎 Пришлите ссылку в тексте или подписи к медиа. "
        "Если ссылок несколько, я скачаю первое поддерживаемое видео.\n\n"
        f"Доступны {SITE_NAMES}. Лимит файла — 50 МБ.\n/quality — качество, /keeplinks — исходные сообщения группы."
        "\n/ping — проверить, что бот на связи.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏓 Проверить бота", callback_data="ping")
        ]]),
    )


async def on_startup(app: Application):
    # чистим временную папку от файлов, оставшихся после предыдущего запуска/краша
    shutil.rmtree(BASE_DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)
    app.bot_data["store"] = Store(os.environ.get("VIDEO_BOT_DB", "video-bot.sqlite3"))


def main():
    if not TOKEN:
        raise RuntimeError("Задайте BOT_TOKEN в окружении или .env")
    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(8)
        .post_init(on_startup)
        .build()
    )
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CallbackQueryHandler(ping, pattern=r"^ping$"))
    application.add_handler(CommandHandler("quality", quality))
    application.add_handler(CommandHandler("keeplinks", keep_links))
    application.add_handler(CallbackQueryHandler(retry_download, pattern=r"^retry:\d+:\d+$"))
    application.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
    print("🤖 Бот запущен!")
    application.run_polling()


if __name__ == "__main__":
    main()
