# RUNBOOK

## Основные сценарии cron

По предоставленному описанию на сервере используются такие команды:

```cron
45 9 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python ozon_to_sheets.py > /opt/ozon-analytics/cron.log 2>&1
30 12 * * 1 cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python weekly_sum.py > /opt/ozon-analytics/cron_weeklysum.log 2>&1
0 13 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python week_live.py > /opt/ozon-analytics/week_comp.log 2>&1
0 12 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python grafana_report.py > /opt/ozon-analytics/grafana.log 2>&1
0 20 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python grafana_report.py > /opt/ozon-analytics/grafana2.log 2>&1
30 10 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python daily.py > /opt/ozon-analytics/day.log 2>&1
5 8-23 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python dailyhour.py > /opt/ozon-analytics/hour.log 2>&1
30 8 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python seg.py > /opt/ozon-analytics/cron_seg.log 2>&1
45 8 * * 1 cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python segW.py > /opt/ozon-analytics/cron_segW.log 2>&1
30 10 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python ozon_google_op.py > /opt/ozon-analytics/cron_ozongoogleop.log 2>&1
35 10 * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python ozon_bot.py > /opt/ozon-analytics/cron_ozonbot.log 2>&1
5 * * * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python ozon_opbot.py > /opt/ozon-analytics/cronop.log 2>&1
0 6 * * 1,3 cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python ozon_obor.py > /opt/ozon-analytics/cron_ozon_obor.log 2>&1
0 9 1 * * cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python obor_excel.py > /opt/ozon-analytics/cron_obordel.log 2>&1
40 10 * * 1 cd /opt/ozon-analytics && /opt/ozon-analytics/venv/bin/python weekfindyn.py > /opt/ozon-analytics/cronweekdyn.log 2>&1
```

## Ручной запуск

```bash
source venv/bin/activate
python ozon_to_sheets.py
python grafana_report.py
python weekly_sum.py
python daily.py
python dailyhour.py
```

## Быстрый чек-лист при сбое

1. Проверить доступность Ozon API и токенов.
2. Проверить доступ сервисного аккаунта к Google Sheets.
3. Проверить, что `constants.py` соответствует актуальным SKU и листам.
4. Для `grafana_report.py` проверить подключение к PostgreSQL.
