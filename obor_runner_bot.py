"""
Бот-кнопка для ручного запуска ozon_obor.py в VK Teams.

Висит постоянно (через systemd), слушает события long-polling'ом.
- Команда /start или /obor -> присылает в чат кнопку "Запустить обор".
- Нажатие кнопки -> запускает ozon_obor.py в фоне, пишет статус и "Готово".

Запускать может любой в чате (без белого списка).
"""

import os
import json
import time
import threading
import subprocess

import requests
from dotenv import load_dotenv

load_dotenv()

# === Настройки ===
BOT_TOKEN = os.getenv("POTREB_BOT_TOKEN")              # токен нового бота (последний в .env)
API_BASE = os.getenv("VKTEAMS_API", "https://myteam.mail.ru/bot/v1")

# чем и что запускаем (по умолчанию — как в cron на VPS)
WORK_DIR = os.getenv("OBOR_WORK_DIR", "/opt/ozon-analytics")
PYTHON_BIN = os.getenv("OBOR_PYTHON", "/opt/ozon-analytics/venv/bin/python")
OBOR_SCRIPT = os.getenv("OBOR_SCRIPT", "ozon_obor.py")

CALLBACK_RUN = "run_obor"            # идентификатор нажатия кнопки
BUTTON_TEXT = "🔄 Запустить обновление таблицы потребности"

# защита от двух одновременных запусков
_run_lock = threading.Lock()


# === VK Teams Bot API ===
def api_get(method, **params):
    """GET-запрос к Bot API с токеном."""
    params["token"] = BOT_TOKEN
    url = f"{API_BASE}/{method}"
    try:
        r = requests.get(url, params=params, timeout=70)
        return r.json()
    except Exception as e:
        print(f"[API] {method} error: {e}")
        return {}


def send_text(chat_id, text, keyboard=None):
    params = {"chatId": chat_id, "text": text}
    if keyboard is not None:
        params["inlineKeyboardMarkup"] = json.dumps(keyboard, ensure_ascii=False)
    return api_get("messages/sendText", **params)


def edit_text(chat_id, msg_id, text, keyboard=None):
    params = {"chatId": chat_id, "msgId": msg_id, "text": text}
    if keyboard is not None:
        params["inlineKeyboardMarkup"] = json.dumps(keyboard, ensure_ascii=False)
    return api_get("messages/editText", **params)


def answer_callback(query_id, text="", show_alert=False):
    return api_get(
        "messages/answerCallbackQuery",
        queryId=query_id,
        text=text,
        showAlert="true" if show_alert else "false",
    )


def run_button():
    """Клавиатура с одной кнопкой запуска."""
    return [[{"text": BUTTON_TEXT, "callbackData": CALLBACK_RUN, "style": "primary"}]]


# === Запуск отчёта ===
def run_obor_job(chat_id, msg_id):
    """Запускает ozon_obor.py и обновляет сообщение по итогу. Вызывается в отдельном потоке."""
    if not _run_lock.acquire(blocking=False):
        edit_text(chat_id, msg_id, "⚠️ Обновление таблицы уже выполняется, подождите завершения.")
        return

    started = time.time()
    try:
        edit_text(chat_id, msg_id, "⏳ Запускаю обновление таблицы… (займёт некоторое время, я пришлю сообщение о готовности)")
        proc = subprocess.run(
            [PYTHON_BIN, OBOR_SCRIPT],
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            timeout=60 * 120,  # 2 часа максимум (таблица тяжёлая, прогон бывает ~1.5 ч)
        )
        elapsed = int(time.time() - started)
        if proc.returncode == 0:
            edit_text(chat_id, msg_id, f"✅ Готово (за {elapsed} с).")
            print(f"[RUN] ok in {elapsed}s")
        else:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            edit_text(chat_id, msg_id, f"❌ Ошибка (код {proc.returncode}).\n{tail}")
            print(f"[RUN] fail rc={proc.returncode}: {tail}")
    except subprocess.TimeoutExpired:
        edit_text(chat_id, msg_id, "❌ Превышено время ожидания (2 ч).")
        print("[RUN] timeout")
    except Exception as e:
        edit_text(chat_id, msg_id, f"❌ Не удалось запустить: {e}")
        print(f"[RUN] exception: {e}")
    finally:
        _run_lock.release()


# === Обработка событий ===
def handle_event(ev):
    etype = ev.get("type")
    payload = ev.get("payload", {})

    if etype == "newMessage":
        text = (payload.get("text") or "").strip().lower()
        chat_id = payload.get("chat", {}).get("chatId")
        if not chat_id:
            return
        if text in ("/start", "/obor", "обор"):
            send_text(chat_id, "Нажмите кнопку, чтобы запустить обновление таблицы потребности 👇", keyboard=run_button())

    elif etype == "callbackQuery":
        data = payload.get("callbackData")
        query_id = payload.get("queryId")
        msg = payload.get("message", {})
        chat_id = msg.get("chat", {}).get("chatId")
        msg_id = msg.get("msgId")
        if data == CALLBACK_RUN and chat_id and msg_id:
            answer_callback(query_id, text="Запускаю…")
            threading.Thread(
                target=run_obor_job, args=(chat_id, msg_id), daemon=True
            ).start()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("POTREB_BOT_TOKEN не задан в .env")

    print(f"[BOT] старт, API={API_BASE}, скрипт={PYTHON_BIN} {OBOR_SCRIPT} (cwd={WORK_DIR})")
    last_event_id = 0
    while True:
        resp = api_get("events/get", lastEventId=last_event_id, pollTime=25)
        if not resp.get("ok"):
            time.sleep(3)
            continue
        for ev in resp.get("events", []):
            last_event_id = ev.get("eventId", last_event_id)
            try:
                handle_event(ev)
            except Exception as e:
                print(f"[EVENT] handle error: {e}")


if __name__ == "__main__":
    main()
