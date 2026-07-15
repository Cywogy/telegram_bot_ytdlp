# Video Bot — деплой с нуля

Telegram-бот для скачивания видео (YouTube, TikTok, Instagram, Twitter/X, VK, Facebook, Reddit, Vimeo) и отправки их в чат.

## 1. Подготовка сервера

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

## 2. Клонирование репозитория

```bash
git clone https://github.com/Cywogy/telegram_bot_ytdlp.git
cd video-bot
```

## 3. Виртуальное окружение и зависимости

Ставим зависимости вручную:

```bash
python3 -m venv venv
source venv/bin/activate

pip install -U python-telegram-bot yt-dlp python-dotenv
```

```bash
python3 -m venv venv
source venv/bin/activate
```

## 4. Файл окружения

Отредактируйте шаблон `.env` и впишите реальный токен:

```bash
nano .env
```

```env
BOT_TOKEN=реальный_токен_от_BotFather
YTDLP_COOKIES_FILE=/opt/video-bot/cookies.txt
```

Токен получаете у [@BotFather](https://t.me/BotFather) командой `/newbot`.

## 5. Cookies для Instagram (опционально)

Файл `cookies.txt` в репозитории по умолчанию — просто заглушка-инструкция, а не настоящие куки. Если нужна поддержка Instagram:

1. Установите расширение «Get cookies.txt LOCALLY» в браузере.
2. Залогиньтесь на instagram.com (лучше не с основного аккаунта).
3. Экспортируйте куки домена instagram.com в реальный `cookies.txt`, замените им файл-заглушку.
4. Путь уже указан в `.env` через `YTDLP_COOKIES_FILE`.

Если Instagram не нужен — просто уберите эту переменную из `.env`.

Куки со временем протухают — если бот снова начнёт ругаться на Instagram, повторите экспорт.

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
- на старте бот подчищает `/tmp/video-bot` от возможного мусора с прошлого запуска;
- раз в 30 минут фоновая задача удаляет папки старше часа — на случай `kill -9` посреди активной загрузки.

Настройки — `ORPHAN_MAX_AGE_MINUTES` и `ORPHAN_CLEANUP_INTERVAL_SECONDS` в начале `video_bot.py`.

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
