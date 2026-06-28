# finance_report

Финансовый дашборд по Ozon: продажи, начисления, комиссии, логистика и услуги
по дням — в целом по магазину и по каждому артикулу. Данные пишутся в
PostgreSQL (`public.ozon_finance`), дашборд строится в Grafana поверх неё.

В отличие от основной аналитики (по заказам), здесь — **финансовые начисления**
из Ozon `finance/accrual/by-day` (через корневой модуль `finance_accrual`).

## Скрипты

- `db.py` — подключение к PG, DDL таблицы `public.ozon_finance`, идемпотентная
  запись (`replace_date`: delete по дате+source → insert).
- `collect.py` — сбор начислений за день в строки (overall + по SKU).
- `run.py` — оркестратор: за вчера / за дату / `--backfill N`.

## Запуск

```bash
# за вчера (REPORT_TZ, по умолчанию Asia/Novosibirsk)
python finance_report/run.py

# за конкретную дату
python finance_report/run.py 2026-06-17

# первичная загрузка истории (последние 90 дней)
python finance_report/run.py --backfill 90

# проверка агрегации без записи в БД
python finance_report/collect.py 2026-06-17
```

⚠️ PG доступна только **с VPS** (порт 5432 наружу закрыт). Запускать там же,
где остальные cron-скрипты. Переменные `PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD`
и `OZON_*` берутся из корневого `.env`.

## Таблица `public.ozon_finance`

Длинный формат (как `ozon_metrics`). Одна строка = одно начисление за день:

| поле | смысл |
|---|---|
| `metric_date` | день начислений |
| `scope` | `overall` (свод) или `sku` (по артикулу) |
| `sku_id` | Ozon SKU (NULL для overall) |
| `offer_id` | наш артикул (NULL для overall / если SKU не маппится) |
| `category` | укрупнённая категория (7 шт., см. ниже) |
| `accrual_type` | имя `type_id` (`Logistic`, `PayPerClick`…) или `sales`/`sale_commission` |
| `type_id` | id типа начисления (NULL для продаж/комиссии) |
| `value` | сумма; знак как в API: **продажи +, расходы −** |
| `source` | `ozon_finance` |

**7 категорий:** `sales`, `sale_commission`, `processing_and_delivery`,
`services_amount`, `refunds_and_cancellations`, `compensation_amount`,
`others_amount`. Маппинг `type_id → category` — в `finance_accrual.CATEGORY_MAP`.

Идея: **храним детально (по type_id, ~115 типов), показываем укрупнённо (7
категорий)**. Дашборд по умолчанию агрегирует по `category`; `accrual_type`/
`type_id` нужны только для drill-down «из чего состоит категория».

## Запросы Grafana (примеры)

```sql
-- Свод по категориям за период (стэк по дням)
SELECT metric_date AS time, category, SUM(value) AS v
FROM public.ozon_finance
WHERE scope='overall' AND $__timeFilter(metric_date)
GROUP BY 1,2 ORDER BY 1;

-- Чистая маржа начислений по дням (продажи + все расходы)
SELECT metric_date AS time, SUM(value) AS net
FROM public.ozon_finance
WHERE scope='overall' AND $__timeFilter(metric_date)
GROUP BY 1 ORDER BY 1;

-- Drill-down: состав категории по типам
SELECT accrual_type, SUM(value) AS v
FROM public.ozon_finance
WHERE scope='overall' AND category='services_amount'
  AND $__timeFilter(metric_date)
GROUP BY 1 ORDER BY 2;

-- По артикулу (переменная $offer)
SELECT metric_date AS time, category, SUM(value) AS v
FROM public.ozon_finance
WHERE scope='sku' AND offer_id = '$offer' AND $__timeFilter(metric_date)
GROUP BY 1,2 ORDER BY 1;
```

## Важно: overall ≠ сумма по SKU

Часть начислений Ozon — на уровне продавца, **без привязки к товару**
(эквайринг, реклама-нонитем, подписки Premium и т.п.). Они попадают только в
`scope='overall'`. Поэтому свод по `overall` авторитетен для итогов, а `sku`-срез
— для разбора по артикулам; их суммы намеренно не совпадают.

Сверено: `overall` по категориям совпадает с `finance_accrual.totals()` (и со
старым `transaction/totals`) до копейки.
