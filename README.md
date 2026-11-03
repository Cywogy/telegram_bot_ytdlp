# Video Bot — деплой с нуля

Telegram-бот для скачивания видео (YouTube, TikTok, Instagram, Twitter/X, VK, Facebook, Reddit, Vimeo) и отправки их в чат.

Отправьте ссылку в тексте или подписи к медиа. Бот обработает первую поддерживаемую ссылку. Под видео есть кнопка «Открыть оригинал». В группе после отправки удаляется только отдельная ссылка. Сообщения с комментарием и медиа сохраняются. Администратор может включить сохранение всех ссылок командой `/keeplinks on` и вернуть обычный режим через `/keeplinks off`. В личке исходные текстовые сообщения удаляются после успешной отправки.

По умолчанию бот выбирает формат до 720p по короткой стороне. Для раздельных дорожек H.264 и AAC бот использует ffmpeg. Команда `/quality 480` устанавливает личный предел 480p, `/quality auto` возвращает 720p. Если размер заранее неизвестен и ролик не помещается, бот предлагает автору повтор в доступном меньшем разрешении до 480p.

После проверки метаданных бот ищет Telegram file_id по площадке, ID видео и выбранному формату. Одновременные запросы одного кэшируемого формата в одном процессе используют одну загрузку. При необходимости авторизации бот повторяет запрос с настроенными cookies. Такие загрузки, прямые эфиры и контент с указанным ограничением доступа обходят общий кэш.

Для кэша и настроек используется SQLite. По умолчанию файл `video-bot.sqlite3` создаётся в рабочей директории. Для systemd укажите `StateDirectory=video-bot` и `Environment=VIDEO_BOT_DB=/var/lib/video-bot/video-bot.sqlite3` в секции `[Service]`, если у пользователя сервиса нет прав записи в каталог проекта.

## 1. Подготовка сервера

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git ffmpeg
```

## 2. Клонирование репозитория

```bash
git clone https://github.com/Cywogy/telegram_bot_ytdlp.git
cd telegram_bot_ytdlp
```

## 3. Виртуальное окружение и зависимости

Ставим зависимости вручную:

```bash
python3 -m venv venv
source venv/bin/activate

pip install -U python-telegram-bot yt-dlp python-dotenv
```


## 4. Файл окружения

Создайте локальный файл `.env` с токеном бота:

```bash
nano .env
```

```env
BOT_TOKEN=реальный_токен_от_BotFather
YTDLP_COOKIES_FILE=/opt/video-bot/cookies.txt
```

Токен получаете у [@BotFather](https://t.me/BotFather) командой `/newbot`.

## 5. Cookies для Instagram (опционально)

Для загрузок с авторизацией экспортируйте cookies нужного сайта в формате Netscape, сохраните файл на сервере с правами `chmod 600 cookies.txt` и задайте его абсолютный путь в `YTDLP_COOKIES_FILE`. Для публичных видео эту переменную можно пропустить.

## 6. Тестовый запуск

```bash
source venv/bin/activate
python3 video_bot.py
```

Должно появиться `🤖 Бот запущен!` — напишите боту в Telegram `/start` и проверьте на ссылке.

## 7. Запуск как systemd-сервис

Создайте `/etc/systemd/system/video-bot.service`:

```ini
[Unit]
Description=Telegram video download bot
After=network.target

[Service]
Type=simple
User=video-bot
WorkingDirectory=/opt/video-bot
ExecStart=/opt/video-bot/venv/bin/python3 /opt/video-bot/video_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now video-bot
sudo systemctl status video-bot
journalctl -u video-bot -f   # логи в реальном времени
```

## 8. Автоудаление файлов с сервера

Уже реализовано в коде:
- каждое видео скачивается в свою уникальную временную папку;
- папка удаляется сразу после отправки в Telegram (успешно или с ошибкой — `finally`);
- на старте бот подчищает `/tmp/video-bot` от возможного мусора с прошлого запуска.

## 9. Обновление yt-dlp

Instagram/TikTok/YouTube часто меняют защиту — старую версию `yt-dlp` из `apt` использовать не стоит, обновляйте через `pip` регулярно (раз в 1-2 недели):

```bash
cd /opt/video-bot
source venv/bin/activate
pip install -U yt-dlp
sudo systemctl restart video-bot
```

Это можно добавить в cron.

## 10. Ограничения

- Telegram Bot API без локального сервера не позволяет боту отправлять файлы крупнее **~50 МБ** — бот сам предупредит об этом в чате.
- Число одновременных загрузок ограничено (`MAX_CONCURRENT_DOWNLOADS = 3` в коде) — чтобы не перегружать сервер.
