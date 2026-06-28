# CONFIG

## Переменные окружения

### Ozon API

- `OZON_CLIENT_ID`
- `OZON_API_KEY`
- `OZON_PERF_CLIENT_ID`
- `OZON_PERF_CLIENT_SECRET`

### Google Sheets

- `GOOGLE_CREDS`
- `GOOGLE_CREDS_PATH`
- `GOOGLE_SHEET_NAME`
- `GOOGLE_SHEET_NAME_DAY`
- `GOOGLE_SHEET_NAME_MANUAL`
- `GOOGLE_SHEET_NAME_OBOR`
- `GOOGLE_SHEET_NAME_OP`
- `GOOGLE_SHEET_WORKSHEET_MANUAL`
- `GOOGLE_SHEET_ID_MANUAL`
- `WORKSHEET_NAME`
- `WEEKFINDYN_WORKSHEET`

### PostgreSQL

- `PG_HOST`
- `PG_PORT`
- `PG_DB`
- `PG_USER`
- `PG_PASSWORD`

### Боты и уведомления

- `VKTEAMS_API`
- `VKTEAMS_BOT_TOKEN`
- `VK_TEAMS_CHAT_ID`

### Управление режимами

- `TARGET_DATE`
- `DAYS_BACK`
- `BACKFILL_DAYS`
- `WEEKFINDYN_BACKFILL_WEEKS`
- `SKIP_SVD_OVH`
- `PAUSE_BETWEEN_SKU_SECONDS`
- `DEBUG_DAILY`
- `USE_NSK_TIME`

## Что ещё используется

- `google-creds.json` присутствует в корне проекта.
- `constants.py` хранит словари SKU, метрики и маппинги строк.

## Что не найдено

- единый `.env.example`;
- отдельный слой конфигурации поверх `.env`;
- централизованная проверка обязательных переменных для всех скриптов.
