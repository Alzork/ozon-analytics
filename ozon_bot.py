import os
import requests
import gspread
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

# === Настройки окружения ===
load_dotenv()

GOOGLE_CREDS = os.getenv("GOOGLE_CREDS")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME_OP")
VK_TOKEN = os.getenv("VKTEAMS_BOT_TOKEN")
VK_API = os.getenv("VKTEAMS_API")
VK_CHAT_ID = os.getenv("VK_TEAMS_CHAT_ID")
if not VK_CHAT_ID:
    VK_CHAT_ID = os.getenv("VK_CHAT_ID", "")

gc = gspread.service_account(filename=GOOGLE_CREDS)
sheet = gc.open(SHEET_NAME).sheet1
MSK = timezone(timedelta(hours=3))


# === Вспомогательные функции ===
def format_delta(value):
    """Возвращает цветной эмодзи и процент со знаком"""
    if value > 0:
        return f"+{abs(value):.1f}%"
    elif value < 0:
        return f"-{abs(value):.1f}%"
    else:
        return f"0.0%"


def calc_dynamics(now, past):
    """Считает процент изменения"""
    try:
        if past == 0:
            return 0
        return (now - past) / past * 100
    except Exception:
        return 0

def calc_pp_delta(now, past):
    """Считает изменение в процентных пунктах (для долей/процентов)."""
    try:
        return now - past
    except Exception:
        return 0


def get_metrics_by_date(date_str):
    """Берет значения метрик по конкретной дате"""
    header = sheet.row_values(1)
    if date_str not in header:
        return None
    col = header.index(date_str) + 1
    data = sheet.col_values(col)[1:7]  # строки 2–7
    cleaned = []
    for x in data:
        if not x.strip():
            cleaned.append(0)
            continue
        val = x.replace(" ", "").replace(",", ".")
        try:
            cleaned.append(float(val))
        except ValueError:
            cleaned.append(0)
    return cleaned


def get_metric_value(metrics, index):
    """Безопасно возвращает значение метрики по индексу."""
    if not metrics or index >= len(metrics):
        return None
    return metrics[index] if metrics[index] else None


def send_to_vk(text):
    """Отправляет сообщение в VK Teams (через form-data, как n8n)"""
    url = f"{VK_API}/messages/sendText"
    data = {
        "token": VK_TOKEN,
        "chatId": VK_CHAT_ID,
        "text": text
    }
    r = requests.post(url, data=data)
    print(f"[VK] Статус отправки: {r.status_code}")
    print(f"[VK] Ответ: {r.text}")


# === Основная логика ===
def make_report():
    now_msk = datetime.now(MSK)
    today = (now_msk - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (now_msk - timedelta(days=2)).strftime("%Y-%m-%d")
    week_ago = (now_msk - timedelta(days=8)).strftime("%Y-%m-%d")
    month_ago = (now_msk - timedelta(days=31)).strftime("%Y-%m-%d")

    metrics_today = get_metrics_by_date(today)
    metrics_yesterday = get_metrics_by_date(yesterday)
    metrics_week = get_metrics_by_date(week_ago)
    metrics_month = get_metrics_by_date(month_ago)

    if not metrics_today:
        print("⚠️ Нет данных за вчера!")
        return

    if not metrics_yesterday:
        print(f"⚠️ Нет данных для сравнения со вчерашним днем: {yesterday}")
    if not metrics_week:
        print(f"⚠️ Нет данных для сравнения с неделей: {week_ago}")
    if not metrics_month:
        print(f"⚠️ Нет данных для сравнения с месяцем: {month_ago}")

    titles = ["Заказы", "Продажи", "Расходы", "Доля МП %", "Прибыль", "СВД (ч)"]
    emojis = ["📦", "💰", "📉", "💹", "💵", "🚗"]
    units = [" ₽", " ₽", " ₽", " %", " ₽", " ч"]

    lines = [f"📊 Оперативная аналитика Ozon — {today}"]

    for i, title in enumerate(titles):
        val_now = metrics_today[i]
        yesterday_val = get_metric_value(metrics_yesterday, i)
        week_val = get_metric_value(metrics_week, i)
        month_val = get_metric_value(metrics_month, i)

        if title == "Доля МП %":
            d0 = calc_pp_delta(val_now, yesterday_val) if yesterday_val is not None else None
            d1 = calc_pp_delta(val_now, week_val) if week_val is not None else None
            d2 = calc_pp_delta(val_now, month_val) if month_val is not None else None
            yesterday_text = (f"{d0:+.1f} п.п." if d0 is not None else "нет данных")
            week_text = (f"{d1:+.1f} п.п." if d1 is not None else "нет данных")
            month_text = (f"{d2:+.1f} п.п." if d2 is not None else "нет данных")
        else:
            d0 = calc_dynamics(val_now, yesterday_val) if yesterday_val is not None else None
            d1 = calc_dynamics(val_now, week_val) if week_val is not None else None
            d2 = calc_dynamics(val_now, month_val) if month_val is not None else None
            yesterday_text = format_delta(d0) if d0 is not None else "нет данных"
            week_text = format_delta(d1) if d1 is not None else "нет данных"
            month_text = format_delta(d2) if d2 is not None else "нет данных"

        lines.append(
            f"{emojis[i]} {title}: {val_now:,.2f}{units[i]}  "
            f"(д/вчд: {yesterday_text} · д/нед: {week_text} · д/мес: {month_text})\n"
        )

    message = "\n".join(lines)
    print(message)
    send_to_vk(message)


if __name__ == "__main__":
    make_report()
