# ozon_report

## Что это

Набор автономных Python-скриптов для ежедневной и недельной аналитики Ozon. Проект собирает данные из Ozon Seller API и Performance API, обновляет Google Sheets, отправляет сообщения в ботов и пишет часть аналитики в PostgreSQL для дальнейшего использования в Grafana.

## Что решает

Проект закрывает регулярную операционную отчётность по Ozon без единой центральной точки входа. Каждый сценарий запускается отдельным скриптом под cron и отвечает за свой срез: свод, SKU, оперативка, ценовые сегменты, недельная аналитика, потребность и загрузка в БД.

## Основные возможности

- загрузка дневных метрик Ozon в Google Sheets;
- недельные и сравнительные отчёты;
- расчёт ценовых сегментов;
- выгрузка данных в PostgreSQL;
- отправка сообщений в ботов;
- отдельные задачи по оперативной аналитике и потребности.

## Стек

- Python
- requests
- pandas
- gspread
- oauth2client
- openpyxl
- psycopg2-binary
- python-dotenv

## Структура проекта

```text
ozon_report/
├── constants.py
├── ozon_to_sheets.py
├── ozon_14days.py
├── weekly_sum.py
├── week_live.py
├── weekfindyn.py
├── grafana_report.py
├── daily.py
├── dailyhour.py
├── seg.py / segW.py
├── ozon_google_op.py
├── ozon_bot.py
├── ozon_opbot.py
├── ozon_obor.py
├── obor_excel.py
├── google-creds.json
└── .env
```

## Установка зависимостей

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Точки входа

У проекта нет одного `main.py`. Запускать нужно конкретный сценарий:

- `python ozon_to_sheets.py`
- `python grafana_report.py`
- `python weekly_sum.py`
- `python ozon_bot.py`
- `python daily.py`
- `python dailyhour.py`

## Как использовать

1. Подготовить `.env` и `google-creds.json`.
2. Проверить доступ сервисного аккаунта к Google Sheets.
3. Запускать нужный скрипт вручную или через cron.
4. Для задач с PostgreSQL проверить переменные `PG_*`.

## Важные файлы

- `constants.py` — маппинги SKU и строк листов.
- `google-creds.json` — ключ сервисного аккаунта Google.
- `.env` — токены и параметры окружения.
- `requirements.txt` — зависимости проекта.

## Примеры команд

```bash
python ozon_to_sheets.py
python grafana_report.py
python daily.py
python dailyhour.py
python weekly_sum.py
python week_live.py
python seg.py
```

## Миграция finance/transaction → finance/accrual (отключение 3-6 июля 2026)

Методы `/v3/finance/transaction/list` и `/v3/finance/transaction/totals`
устаревают и отключаются Ozon 3-6 июля 2026. Замена — новые методы
`/v1/finance/accrual/{types,by-day,postings}`.

Чтобы не править одну и ту же логику в 7 скриптах, добавлен единый слой
**`finance_accrual.py`**, который повторяет старый интерфейс:

| Функция | Заменяет | Возвращает |
|---|---|---|
| `totals(date)` | `transaction/totals` за день | dict с теми же ключами (`accruals_for_sale`, `sale_commission`, `processing_and_delivery`, ...) и знаками |
| `totals_period(d1, d2)` | `transaction/totals` за период | то же, просуммированное по дням |
| `sales_by_sku(date)` | продажи по SKU из `transaction/list` | `{sku: ₽}` |
| `sales_total(date)` | суммарные продажи за день | `₽` |
| `accrual_types()` | — | `{type_id: name}` (справочник, кэш) |
| `postings(numbers)` | `accrual/postings` | сырые начисления по отправлениям |

Использование:

```python
from finance_accrual import totals, sales_by_sku

t = totals("2026-06-17")        # drop-in под старую логику комиссий
sku_rub = sales_by_sku("2026-06-17")
```

Маппинг `type_id → категория` (`CATEGORY_MAP` в модуле) сверен до копейки
с прежним `totals` на 2026-06-17. Незнакомый `type_id` уходит в
`others_amount` и печатает `[accrual] UNMAPPED type_id=...` — общая сумма
при этом остаётся верной, но тип надо вручную отнести в нужную категорию.

**`probe_accrual.py`** — разведочный скрипт (только читает и печатает):
дёргает три новых метода за дату и сверяет их со старыми, пока те живы.
Перед окончательным переключением прогоните его на нескольких датах и
закройте все `UNMAPPED`:

```bash
python probe_accrual.py 2026-06-15
python finance_accrual.py 2026-06-15   # быстрый self-check totals()
```

Особенности новых методов: суммы приходят строками (`"-42.7"`), пагинация
`by-day` — через курсор `last_id`, `accrual/postings` принимает номера
отправлений (не даты) и по свежим отправлениям может быть пустым — поэтому
рабочая лошадка для регулярных отчётов именно `by-day`.

## Возможные проблемы и примечания

- Архитектура скриптов плоская: общая бизнес-логика распределена по файлам.
- В репозитории лежит `google-creds.json`; для публичной демонстрации лучше заменить его на шаблон.
- Часть задач сильно зависит от структуры Google Sheets и словарей в `constants.py`.
- Некоторые даты и режимы вычисляются автоматически как "вчера"; это удобно для cron, но требует проверки при ручном backfill.

Подробности по переменным окружения и cron смотрите в `CONFIG.md` и `RUNBOOK.md`.
