import os
import pandas as pd
from pathlib import Path
from datetime import datetime
import re
import gspread
import locale
from locale import setlocale, LC_TIME
import shutil
import requests
import time



# ==== НАСТРОЙКИ ====
INPUT_FILE = "ozon_need.xlsx"      # входной файл
OUTPUT_DIR = Path("deliveries")    # папка для доставок
CLEANUP_AFTER_SEND = True          # удалять локальные файлы после отправки
GSHEET_ID = os.getenv("GSHEET_ID", "")
GSHEET_WORKSHEET_NAME = "Расчет"  # None = первый лист
SERVICE_ACCOUNT_FILE = "google-creds.json"

VK_TEAMS_BOT_TOKEN = "001.0661830398.3856836559:1011871218"
VK_TEAMS_CHAT_ID = os.getenv("VK_TEAMS_CHAT_ID", "")
VK_TEAMS_API_BASE = "https://myteam.mail.ru/bot/v1"
SEND_RETRIES = 3
SEND_RETRY_SLEEP_SEC = 3
SEND_BETWEEN_FILES_SLEEP_SEC = 1

DISPLAY_ARTICLE_MAP = {
    "1049": "1049 ИР",
    "882": "882 П ИР",
    "1053": "1053 ИР",
    "648": "648/1 ИР",
    "649": "649/1 ИР",
    "664": "664 ИР",
    "1081": "1081 ИР",
    "677": "677 ИР",
    "1118": "1118 ИР",
    "864": "864 ИР",
    "865": "865 ИР",
    "912": "912/1 ИР",
    "913": "913/1 ИР",
    "948": "948",
    "947": "947",
    "946": "946",
    "1126": "1126 ИР",
    "1121": "1121 ИР",
    "970": "970/1 ИР",
    "738": "738/2 ИР",
    "1056": "1056",
    "1057": "1057",
    "1058": "1058",
    "1061": "1061",
    "1060": "1060",
    "1059": "1059",
    "1087": "1087 ИР",
    "1086": "1086 ИР",
    "1088": "1088 ИР",
    "1093": "1093 ИР",
    "1068": "1068 ИР",
    "1094": "1094 ИР",
    "1095": "1095 ИР",
    "1099": "1099 ИР",
    "1104": "1104",
    "1108": "1108",
    "1105": "1105",
    "1114": "1114",
    "1115": "1115",
    "1124": "1124 ИР",
    "1092": "1092 ИР",
    "1129": "1129 ИР",
    "1127": "1127 ИР",
    "1128": "1128 ИР",
    "1132": "1132 ИР",
    "1130": "1130 ИР",
    "1131": "1131 ИР",
    "1133": "1133 ИР",
    "1102": "1102",
    "1101": "1101",
    "1103": "1103",
    "654": "654 ИР",
    "1122": "1122 ИР",
    "1100": "1100",
    "1106": "1106",
    "1109": "1109",
    "1107": "1107",
    "1110": "1110",
    "1111": "1111",
    "1112": "1112",
    "1116": "1116",
    "1119": "1119",
    "1134": "1134 ИР",
    "660": "660 ИР",
    "863": "863 ИР",
    "884": "884 П ИР",
    "1123": "1123 ИР",
    "325": "325 П ИР",
    "595": "595/П ИР",
    "407": "407/3 ИР",
    "401": "401 ИР",
    "1148": "1148 ИР",
    "1149": "1149 ИР"
}

try:
    setlocale(LC_TIME, "ru_RU.UTF-8")
except locale.Error:
    try:
        setlocale(LC_TIME, "ru_RU.utf8")
    except locale.Error:
        setlocale(LC_TIME, "C")

NOW = datetime.now()
MONTH_NAME = NOW.strftime("%B_%Y").capitalize()



# ==== ВСПОМОГАТЕЛЬНОЕ ====
def safe_filename(text: str) -> str:
    """
    Делает строку безопасной для имени файла
    """
    text = text.strip()
    text = re.sub(r"[^\wа-яА-ЯёЁ\- ]+", "", text)
    text = text.replace(" ", "_")
    return text


def normalize_article_key(value) -> str:
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\.0+$", "", text)
    match = re.search(r"\d+", text)
    return match.group(0) if match else text


def format_article_for_export(value) -> str:
    key = normalize_article_key(value)
    if not key:
        return str(value).strip()
    return DISPLAY_ARTICLE_MAP.get(key, key)


def send_to_vk_teams(file_path: Path, plant: str, cluster: str):
    """
    Отправляет файл в общий чат VK Teams
    (legacy API messages/sendFile — рабочий вариант)
    """
    caption = (
        f"🚚 Площадка: {plant}\n"
        f"📍 Кластер: {cluster}\n"
        f"🗓️ {MONTH_NAME.replace('_', ' ')}"
    )

    url = f"{VK_TEAMS_API_BASE}/messages/sendFile"

    last_error = None

    for attempt in range(1, SEND_RETRIES + 1):
        try:
            with open(file_path, "rb") as f:
                files = {
                    "file": (
                        file_path.name,
                        f,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                }
                data = {
                    "token": VK_TEAMS_BOT_TOKEN,
                    "chatId": VK_TEAMS_CHAT_ID,
                    "caption": caption,
                }

                resp = requests.post(
                    url,
                    files=files,
                    data=data,
                    timeout=60
                )

            print(
                f"📨 VK Teams response | file={file_path.name} | "
                f"attempt={attempt}/{SEND_RETRIES} | status={resp.status_code} | "
                f"text={resp.text[:500]}"
            )

            if resp.status_code == 200:
                return

            last_error = RuntimeError(
                "VK Teams sendFile error\n"
                f"status={resp.status_code}\n"
                f"text={resp.text}\n"
                f"headers={resp.headers}"
            )
        except Exception as e:
            last_error = e
            print(
                f"⚠️ Ошибка отправки | file={file_path.name} | "
                f"attempt={attempt}/{SEND_RETRIES} | error={e}"
            )

        if attempt < SEND_RETRIES:
            time.sleep(SEND_RETRY_SLEEP_SEC)

    raise RuntimeError(
        f"Не удалось отправить файл {file_path.name} после {SEND_RETRIES} попыток: {last_error}"
    )


def export_gsheet_to_excel(output_path: Path):
    """
    Загружает Google Sheet и сохраняет его как Excel-файл
    (совместимо с gspread 6.x)
    """
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(GSHEET_ID)

    if GSHEET_WORKSHEET_NAME:
        ws = sh.worksheet(GSHEET_WORKSHEET_NAME)
    else:
        ws = sh.get_worksheet(0)

    # Берём все значения напрямую
    values = ws.get_all_values()

    if not values:
        raise RuntimeError("Google Sheet пустой")

    headers = values[0]
    rows = values[1:]

    df = pd.DataFrame(rows, columns=headers)

    # (numeric column conversion removed; normalization now only in main())

    df.to_excel(output_path, index=False)


# ==== ОСНОВНАЯ ЛОГИКА ====
def main():
    # Если папка доставок уже есть — очищаем её полностью
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    OUTPUT_DIR.mkdir(exist_ok=True)

    input_path = Path(INPUT_FILE)

    # Входной Excel всегда перезаписываем
    if input_path.exists():
        input_path.unlink()

    print("⬇️  Загружаю Google Sheet и сохраняю Excel...")
    export_gsheet_to_excel(input_path)

    df = pd.read_excel(INPUT_FILE)

    # Переименуем столбцы в удобные имена (по заголовку, не по позиции)
    df = df.rename(columns={
        df.columns[0]: "art",
        df.columns[5]: "cluster",
        df.columns[6]: "plant",
    })
    df = df.rename(columns={
        "Потреб без округ, пал":  "pallets",
        "Потреб без окрг, шт":    "units",
        "Потреб без окрг, тонн":  "tons",
        "Потреб без окрг, маш":   "trucks",
    })

    # Приводим числовые колонки к числам (учёт запятой, пробелов и nbsp)
    for col in ["pallets", "units", "tons", "trucks"]:
        df[col] = (
            df[col]
            .astype(str)
            .str.replace("\xa0", "", regex=False)  # неразрывный пробел
            .str.replace(" ", "", regex=False)     # обычный пробел
            .str.replace(",", ".", regex=False)    # десятичная запятая
        )
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["art"] = df["art"].apply(format_article_for_export)

    # Оставляем только строки с положительной потребностью
    df = df[df["pallets"] > 0]

    # Группировка: Площадка → Кластер
    grouped = df.groupby(["plant", "cluster"])

    sent_files = []
    failed_files = []

    try:
        for (plant, cluster), group in grouped:
            result = group[[
                "art",
                "pallets",
                "units",
                "tons",
                "trucks"
            ]].copy()

            # Итоги
            total_row = pd.DataFrame([{
                "art": "ИТОГО",
                "pallets": result["pallets"].sum(),
                "units": result["units"].sum(),
                "tons": result["tons"].sum(),
                "trucks": result["trucks"].sum(),
            }])

            result = pd.concat([result, total_row], ignore_index=True)

            file_name = (
                f"{safe_filename(plant)}__"
                f"{safe_filename(cluster)}__"
                f"{MONTH_NAME}.xlsx"
            )

            output_path = OUTPUT_DIR / file_name

            result.to_excel(
                output_path,
                index=False,
                sheet_name="Доставка"
            )

            print(f"✔ Создан файл: {output_path}")

            try:
                send_to_vk_teams(output_path, plant, cluster)
                sent_files.append(output_path)
                print(f"✅ Отправлен файл: {output_path}")
            except Exception as e:
                failed_files.append((output_path, str(e)))
                print(f"❌ Не удалось отправить файл: {output_path} | error={e}")

            time.sleep(SEND_BETWEEN_FILES_SLEEP_SEC)
    finally:
        print(
            f"📊 Итог отправки | created={len(sent_files) + len(failed_files)} | "
            f"sent_ok={len(sent_files)} | failed={len(failed_files)}"
        )
        for output_path, error_text in failed_files:
            print(f"❌ FAILED FILE | file={output_path} | error={error_text}")

        if CLEANUP_AFTER_SEND:
            for output_path in sent_files:
                if output_path.exists():
                    output_path.unlink()
                    print(f"🧹 Удален локальный файл: {output_path}")

        if CLEANUP_AFTER_SEND and input_path.exists():
            input_path.unlink()
            print(f"🧹 Удален временный файл: {input_path}")
        if CLEANUP_AFTER_SEND and OUTPUT_DIR.exists():
            # Папка может остаться пустой после удаления всех файлов
            try:
                OUTPUT_DIR.rmdir()
                print(f"🧹 Удалена пустая папка: {OUTPUT_DIR}")
            except OSError:
                pass


if __name__ == "__main__":
    main()
