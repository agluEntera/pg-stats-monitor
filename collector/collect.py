"""
Еженедельный сборщик статистики pg_stat_statements → Elasticsearch.
Поддерживает несколько БД на одном сервере (PG_DATABASES через запятую).
Source в ES: SOURCE_LABEL_dbname (или просто SOURCE_LABEL если БД одна).
"""
import os
import sys
import logging
import psycopg2
from datetime import date, timedelta, timezone, datetime
from elasticsearch import Elasticsearch, helpers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# PostgreSQL
PG_HOST      = os.environ["PG_HOST"]
PG_PORT      = int(os.environ.get("PG_PORT", 5432))
PG_USER      = os.environ["PG_USER"]
PG_PASSWORD  = os.environ["PG_PASSWORD"]
QUERY_FILTER = os.environ.get("QUERY_FILTER", "%insert%")
RESET_STATS  = os.environ.get("RESET_STATS", "true").lower() == "true"

# Список БД: поддерживаем и старый PG_DB, и новый PG_DATABASES
_raw = os.environ.get("PG_DATABASES") or os.environ.get("PG_DB", "")
PG_DATABASES = [db.strip() for db in _raw.split(",") if db.strip()]

if not PG_DATABASES:
    log.error("Укажите PG_DATABASES (или PG_DB) в .env")
    sys.exit(1)

# Elasticsearch
ES_HOST      = os.environ.get("ES_HOST", "http://localhost:9200")
ES_USER      = os.environ.get("ES_USER", "")
ES_PASSWORD  = os.environ.get("ES_PASSWORD", "")
ES_INDEX     = os.environ.get("ES_INDEX", "pg-query-stats")

SOURCE_LABEL = os.environ["SOURCE_LABEL"]


def get_week_start() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def make_source(db_name: str) -> str:
    """
    Одна БД  → SOURCE_LABEL           (обратная совместимость)
    Несколько → SOURCE_LABEL_dbname   (например: DB_APP-ID_entera_app)
    """
    if len(PG_DATABASES) == 1:
        return SOURCE_LABEL
    return f"{SOURCE_LABEL}_{db_name}"


def make_es_client() -> Elasticsearch:
    kwargs = dict(
        hosts=[ES_HOST],
        verify_certs=False,
        ssl_show_warn=False,
        request_timeout=30,
    )
    if ES_USER:
        kwargs["basic_auth"] = (ES_USER, ES_PASSWORD)
    return Elasticsearch(**kwargs)


def fetch_stats(cur, query_filter: str) -> list:
    cur.execute("""
        SELECT
            MD5(query)                  AS query_hash,
            query,
            calls,
            total_exec_time / 1000.0    AS total_exec_time,
            mean_exec_time  / 1000.0    AS mean_exec_time,
            max_exec_time   / 1000.0    AS max_exec_time,
            rows
        FROM pg_stat_statements
        WHERE query ILIKE %s
          AND calls > 0
        ORDER BY mean_exec_time DESC
    """, (query_filter,))
    return cur.fetchall()


def build_documents(rows: list, week_start: date, source: str) -> list:
    ts = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    return [{
        "_index": ES_INDEX,
        "_id": f"{source}_{week_start}_{qh}",
        "_source": {
            "@timestamp":      ts,
            "week_start":      week_start.isoformat(),
            "source":          source,
            "source_label":    SOURCE_LABEL,   # метка сервера без имени БД
            "db_name":         db_name,         # имя БД отдельным полем
            "query_hash":      qh,
            "query":           q,
            "query_short":     q[:120],
            "calls":           calls,
            "total_exec_time": round(tot, 6),
            "mean_exec_time":  round(mean, 6),
            "max_exec_time":   round(maxt, 6),
            "rows":            rows_count,
        }
    } for qh, q, calls, tot, mean, maxt, rows_count in rows]


def collect_db(es: Elasticsearch, db_name: str, week_start: date):
    """Снимает статистику с одной БД."""
    source = make_source(db_name)
    log.info("── БД: %s | source: %s", db_name, source)

    dsn = f"host={PG_HOST} port={PG_PORT} dbname={db_name} user={PG_USER} password={PG_PASSWORD}"
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # Проверяем дубликат
        resp = es.count(
            index=ES_INDEX,
            query={"bool": {"must": [
                {"term": {"week_start": week_start.isoformat()}},
                {"term": {"source":     source}},
            ]}},
            ignore_unavailable=True,
        )
        if resp["count"] > 0:
            log.warning("  Снапшот уже есть (%d документов) — пропускаем.", resp["count"])
            return

        rows = fetch_stats(cur, QUERY_FILTER)
        log.info("  Получено строк: %d", len(rows))

        if not rows:
            log.warning("  Нет данных по фильтру '%s'.", QUERY_FILTER)
            return

        docs = build_documents(rows, week_start, source)
        success, errors = helpers.bulk(es, docs, raise_on_error=False)
        log.info("  Проиндексировано: %d, ошибок: %d", success, len(errors))
        if errors:
            log.error("  Ошибки bulk: %s", errors[:3])

        if RESET_STATS:
            cur.execute("SELECT pg_stat_statements_reset()")
            log.info("  pg_stat_statements сброшен.")
        else:
            log.info("  Сброс пропущен (RESET_STATS=false).")

        conn.commit()

    except Exception as exc:
        conn.rollback()
        log.error("  Ошибка для БД %s: %s", db_name, exc)
        raise
    finally:
        cur.close()
        conn.close()


def collect():
    week_start = get_week_start()
    log.info("Старт сбора. Неделя: %s | сервер: %s | БД: %s | reset: %s",
             week_start, SOURCE_LABEL, PG_DATABASES, RESET_STATS)

    es = make_es_client()
    errors = []

    for db_name in PG_DATABASES:
        try:
            collect_db(es, db_name, week_start)
        except Exception as exc:
            errors.append((db_name, exc))

    if errors:
        for db_name, exc in errors:
            log.error("Ошибка для БД %s: %s", db_name, exc)
        sys.exit(1)

    log.info("Готово. Обработано БД: %d", len(PG_DATABASES))


if __name__ == "__main__":
    collect()
