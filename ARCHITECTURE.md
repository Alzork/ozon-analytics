# ARCHITECTURE

## Общая схема

Проект устроен как набор независимых скриптов вокруг общих справочников из `constants.py`. Общего пакета или единого orchestration-слоя нет.

## Основные уровни

### Источники данных

- Ozon Seller API
- Ozon Performance API
- локальные словари и маппинги в `constants.py`

### Выходы

- Google Sheets
- PostgreSQL
- сообщения в ботов
- Excel-файлы и промежуточные артефакты

### Скрипты по назначению

- базовые выгрузки: `ozon_to_sheets.py`, `ozon_14days.py`;
- недельная аналитика: `weekly_sum.py`, `week_live.py`, `weekfindyn.py`;
- оперативка: `ozon_google_op.py`, `ozon_bot.py`, `ozon_opbot.py`;
- сегменты и артикулы: `seg.py`, `segW.py`, `daily.py`, `dailyhour.py`;
- БД и Grafana: `grafana_report.py`;
- потребность: `ozon_obor.py`, `obor_excel.py`.

## Что стоит сделать вручную позже

- собрать общие утилиты в отдельный модуль;
- добавить `.env.example`;
- описать структуру Google Sheets и PostgreSQL отдельно.
