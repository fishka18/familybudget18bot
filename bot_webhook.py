"""
Версия для хостинга с webhook (когда бот «засыпает» без сообщений).

Логика и обработчики берутся из bot.py — этот файл только принимает
сообщения от Telegram по https и передаёт их боту.

Дополнительные настройки в .env (или в панели хостинга):
    WEBHOOK_URL=https://имя-вашего-приложения.хостинг.ру
    WEBHOOK_SECRET=любая_длинная_строка_без_пробелов
    PORT=8080          # обычно хостинг подставляет сам
"""

import os

from flask import Flask, request, abort
import telebot

from bot import bot, log  # импорт регистрирует все обработчики из bot.py

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "telegram").strip()
PORT = int(os.getenv("PORT", "8080"))

app = Flask(__name__)


@app.get("/")
def health():
    """Хостинг дёргает этот адрес, чтобы проверить, что приложение живо."""
    return "bot is alive", 200


@app.post(f"/{WEBHOOK_SECRET}")
def receive_update():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data(as_text=True))
    bot.process_new_updates([update])
    return "", 200


def register_webhook():
    """Говорит Telegram, куда присылать сообщения."""
    if not WEBHOOK_URL:
        log.warning(
            "WEBHOOK_URL пуст — приложение запущено, но Telegram пока не знает "
            "его адреса. Впишите адрес приложения в .env и перезапустите."
        )
        return
    address = f"{WEBHOOK_URL}/{WEBHOOK_SECRET}"
    try:
        bot.remove_webhook()
        bot.set_webhook(url=address)
        log.info("Webhook установлен: %s", address)
    except Exception as exc:  # noqa: BLE001
        log.error("Не удалось установить webhook: %s", exc)


register_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
