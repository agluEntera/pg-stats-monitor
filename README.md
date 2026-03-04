# Мониторинг медленных запросов PostgreSQL → Elasticsearch + Kibana

Система еженедельно снимает статистику медленных запросов из `pg_stat_statements`, сохраняет её в Elasticsearch и визуализирует в Kibana. Позволяет отслеживать деградацию запросов от недели к неделе на нескольких БД одновременно.

---

## Архитектура

```
ВМ-1 (DB_APP-ID)              ВМ-2 (DB_ANALYTICS)
┌──────────────────────┐       ┌──────────────┐
│  PostgreSQL          │       │  PostgreSQL  │
│  ├─ entera_app       │       │  └─ anl_db   │
│  └─ entera_id        │       │              │
│  collector           │       │  collector   │
│  SOURCE=DB_APP-ID    │       │  SOURCE=anl  │
└──────────┬───────────┘       └──────┬───────┘
           └───────────────────────────┘
                        │  bulk index
             ┌──────────▼──────────┐
             │   Elasticsearch     │  индекс: pg-query-stats
             │   Kibana            │  дашборд: PostgreSQL Query Stats
             └─────────────────────┘
```

**Source в Elasticsearch:**

| Конфиг `PG_DATABASES`       | `source` в документе                               |
|-----------------------------|----------------------------------------------------|
| `entera_app` (одна БД)      | `DB_APP-ID`                                        |
| `entera_app,entera_id`      | `DB_APP-ID_entera_app`, `DB_APP-ID_entera_id`      |

**Поля для фильтрации в Kibana (KQL):**

```
source_label: "DB_APP-ID"           ← все БД этого сервера
db_name: "entera_id"                ← конкретная БД на всех серверах
source: "DB_APP-ID_entera_app"      ← точный источник
```

---

## Требования

| Компонент                       | Версия                                        |
|---------------------------------|-----------------------------------------------|
| Docker + Docker Compose         | v1 (`docker-compose`) или v2 (`docker compose`) |
| PostgreSQL                      | 10+ с расширением `pg_stat_statements`        |
| Elasticsearch                   | 8.x                                           |
| Kibana                          | 8.x                                           |
| Python (для setup_kibana.py)    | 3.8+                                          |

**Включить расширение в PostgreSQL:**

```sql
-- Выполнить под суперпользователем
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

В `postgresql.conf` (требует перезапуска PostgreSQL):

```
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = all
```

---

## Структура проекта

```
pg-stats-monitor/
├── .env.example
├── .env                                 # не коммитить!
├── docker-compose.yml
├── setup.sh
├── collector/
│   ├── collect.py                       # сборщик: PG → ES (поддержка нескольких БД)
│   ├── crontab                          # расписание: пн 09:00
│   ├── requirements.txt
│   └── Dockerfile
├── elasticsearch/
│   └── index-template.json
└── kibana/
    └── setup_kibana.py
```

---

## Установка

### Шаг 1 — Клонировать репозиторий

```shell
git clone https://github.com/your-username/pg-stats-monitor.git
cd pg-stats-monitor
```

### Шаг 2 — Настроить .env

```shell
cp .env.example .env
nano .env
```

**Параметры `.env`:**

```shell
# Идентификатор сервера/приложения
# Если БД несколько — source в ES будет: SOURCE_LABEL_dbname
SOURCE_LABEL=DB_APP-ID

# PostgreSQL
PG_HOST=host.docker.internal
PG_PORT=5432
# Одна или несколько БД через запятую
PG_DATABASES=entera_app,entera_id
PG_USER=myuser
PG_PASSWORD=mypassword

# Фильтр запросов (SQL ILIKE)
QUERY_FILTER=%insert%

# Сбрасывать pg_stat_statements после сбора? (true/false)
# false — если pg_stat_statements используют другие инструменты (pgBadger, pg_activity и т.д.)
RESET_STATS=true

# Elasticsearch
ES_HOST=http://your-elastic-host:9200
ES_USER=elastic
ES_PASSWORD=changeme
ES_INDEX=pg-query-stats

# Kibana (только для setup_kibana.py, выполняется один раз)
KIBANA_HOST=http://your-kibana-host:5601
KIBANA_USER=elastic
KIBANA_PASSWORD=changeme
```

### Шаг 3 — Применить Index Template (один раз на кластер)

```shell
export $(grep -v '^#' .env | xargs)
curl -X PUT "${ES_HOST}/_index_template/pg-query-stats-template" \
  -u "${ES_USER}:${ES_PASSWORD}" \
  -H "Content-Type: application/json" \
  -d @elasticsearch/index-template.json
```

### Шаг 4 — Настроить Kibana (один раз)

```shell
pip install requests
export $(grep -v '^#' .env | xargs)
python3 kibana/setup_kibana.py
```

### Шаг 5 — Запустить

```shell
./setup.sh
```

`setup.sh` автоматически выбирает между `docker compose` (v2) и `docker-compose` (v1).

### Шаг 6 — Тестовый запуск (не ждать понедельника)

```shell
docker compose exec collector python collect.py
# или
docker-compose exec collector python collect.py
```

Ожидаемый вывод для двух БД:

```
[INFO] Старт сбора. Неделя: 2026-03-03 | сервер: DB_APP-ID | БД: ['entera_app', 'entera_id']
[INFO] ── БД: entera_app | source: DB_APP-ID_entera_app
[INFO]   Получено строк: 34
[INFO]   Проиндексировано: 34, ошибок: 0
[INFO]   pg_stat_statements сброшен.
[INFO] ── БД: entera_id | source: DB_APP-ID_entera_id
[INFO]   Получено строк: 12
[INFO]   Проиндексировано: 12, ошибок: 0
[INFO]   pg_stat_statements сброшен.
[INFO] Готово. Обработано БД: 2
```

---

## Поддержка нескольких БД на одном сервере

Коллектор итерирует по всем БД из `PG_DATABASES` в одном контейнере.

**Примеры конфигурации:**

| Ситуация                       | Конфиг                                              |
|--------------------------------|-----------------------------------------------------|
| Одна БД                        | `PG_DATABASES=mydb`                                 |
| Две БД на одном сервере        | `PG_DATABASES=entera_app,entera_id`                 |
| Отдельный сервер аналитики     | Второй `.env` с `SOURCE_LABEL=analytics`            |

**Структура документа в Elasticsearch:**

```json
{
  "@timestamp":   "2026-03-03T00:00:00Z",
  "week_start":   "2026-03-03",
  "source":       "DB_APP-ID_entera_app",
  "source_label": "DB_APP-ID",
  "db_name":      "entera_app",
  "query_hash":   "a1b2c3d4...",
  "query_short":  "INSERT INTO orders (user_id, amount) VALUES ($1, $2)",
  "calls":        142857,
  "mean_exec_time": 0.003421,
  "max_exec_time":  1.24,
  "total_exec_time": 488.9,
  "rows":         142857
}
```

**ID документа:** `DB_APP-ID_entera_app_2026-03-03_a1b2c3d4` — коллизий нет ни между БД, ни между серверами.

---

## Флаг RESET_STATS

| `RESET_STATS`           | Поведение                                                                         |
|-------------------------|-----------------------------------------------------------------------------------|
| `true` (по умолчанию)  | Статистика точная за неделю, сбрасывается каждый понедельник                      |
| `false`                 | Накапливается нарастающим итогом. Использовать если `pg_stat_statements` читают другие инструменты |

---

## Совместимость docker-compose v1 и v2

`setup.sh` автоматически определяет доступную версию:

```
[i] Используется: docker compose (v2.24.5)   ← новый плагин
[i] Используется: docker-compose (1.29.2)    ← старый бинарь
[!] Не найден ни один вариант — ошибка
```

---

## Kibana — работа с дашбордом

**Kibana → Dashboards → "PostgreSQL Query Stats"**

| Панель         | Описание                                                          |
|----------------|-------------------------------------------------------------------|
| **Metric**     | Количество уникальных запросов за период                          |
| **Line chart** | Mean Exec Time (сек) — тренд по неделям для топ-10 запросов       |
| **Bar chart**  | Топ-15 самых медленных запросов за выбранный период               |
| **Data table** | Все запросы: mean / max / total время, количество вызовов         |

**Фильтрация через KQL:**

```
source_label: "DB_APP-ID"                              ← весь сервер
source_label: "DB_APP-ID" and db_name: "entera_id"    ← одна БД на сервере
db_name: "entera_app"                                  ← эта БД на всех серверах
```

**Сценарий анализа:**

1. Выбрать time range `now-8w`
2. На line chart найти запрос с растущим трендом
3. Кликнуть на метку → фильтр применится ко всем панелям
4. Сравнить `mean_exec_time` по неделям в data table

---

## Устранение неполадок

### Нет данных в Kibana

```shell
curl -u elastic:password http://es-host:9200/pg-query-stats/_count
curl -u elastic:password "http://es-host:9200/pg-query-stats/_search?size=2" | python3 -m json.tool
```

### Collector не видит PostgreSQL

Убедитесь в `docker-compose.yml`: `extra_hosts: ["host.docker.internal:host-gateway"]`
И в `.env`: `PG_HOST=host.docker.internal`

### pg_stat_statements пустой

```sql
SELECT * FROM pg_stat_statements LIMIT 5;
-- Если ошибка — добавить в postgresql.conf и перезапустить:
-- shared_preload_libraries = 'pg_stat_statements'
-- pg_stat_statements.track = all
```

### Ошибка подключения к одной из БД

Коллектор продолжает работу для остальных БД и завершается с ненулевым кодом, если хотя бы одна упала. Проверить логи:

```shell
docker compose logs collector
```

---

## Добавление нового сервера

1. Клонировать репозиторий на новую ВМ
2. Задать в `.env` уникальный `SOURCE_LABEL` и нужные `PG_DATABASES`
3. Запустить `./setup.sh`
4. Index Template и Kibana Dashboard создавать повторно **не нужно**
