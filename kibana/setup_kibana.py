"""
Одноразовая настройка Kibana:
  - создаёт Data View для индекса pg-query-stats
  - создаёт 4 визуализации (line, bar, table, metric)
  - создаёт Dashboard с этими панелями

Запуск:
    pip install requests python-dotenv
    python setup_kibana.py
или с .env:
    export $(grep -v '^#' ../.env | xargs) && python setup_kibana.py
"""
import os
import sys
import json
import uuid
import logging
import requests
from requests.auth import HTTPBasicAuth

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

KIBANA_HOST = os.environ.get("KIBANA_HOST", "http://localhost:5601").rstrip("/")
KIBANA_USER = os.environ.get("KIBANA_USER", "")
KIBANA_PASS = os.environ.get("KIBANA_PASSWORD", "")
ES_INDEX    = os.environ.get("ES_INDEX", "pg-query-stats")

session = requests.Session()
session.headers.update({"kbn-xsrf": "true", "Content-Type": "application/json"})
session.verify = False
if KIBANA_USER:
    session.auth = HTTPBasicAuth(KIBANA_USER, KIBANA_PASS)


def api(method, path, **kwargs):
    url = "{0}{1}".format(KIBANA_HOST, path)
    resp = session.request(method, url, **kwargs)
    if not resp.ok:
        log.error("HTTP %s %s → %s: %s", method, path, resp.status_code, resp.text[:300])
        resp.raise_for_status()
    return resp.json()


# ─── 1. Index Template ────────────────────────────────────────────────────────

def apply_index_template():
    template_path = os.path.join(os.path.dirname(__file__), "../elasticsearch/index-template.json")
    with open(template_path) as f:
        template = json.load(f)

    es_host = os.environ.get("ES_HOST", "http://localhost:9200").rstrip("/")
    es_auth = (os.environ.get("ES_USER", ""), os.environ.get("ES_PASSWORD", ""))

    resp = requests.put(
        "{0}/_index_template/pg-query-stats-template".format(es_host),
        json=template,
        auth=HTTPBasicAuth(*es_auth) if es_auth[0] else None,
        verify=False,
    )
    if resp.ok:
        log.info("Index template применён.")
    else:
        log.warning("Index template: %s — %s", resp.status_code, resp.text[:200])


# ─── 2. Data View ─────────────────────────────────────────────────────────────

def create_data_view():
    """Создаёт Data View, возвращает его id."""
    # Проверяем, есть ли уже
    existing = api("GET", "/api/data_views")
    for dv in existing.get("data_view", []):
        if dv.get("title") == ES_INDEX:
            log.info("Data View уже существует: %s", dv["id"])
            return dv["id"]

    resp = api("POST", "/api/data_views/data_view", json={
        "data_view": {
            "title":         ES_INDEX,
            "name":          "PG Query Stats",
            "timeFieldName": "@timestamp",
        }
    })
    dv_id = resp["data_view"]["id"]
    log.info("Data View создан: %s", dv_id)
    return dv_id


# ─── 3. Визуализации ──────────────────────────────────────────────────────────

def _search_source(dv_id):
    return json.dumps({
        "index": dv_id,
        "query": {"query_string": {"query": "*", "analyze_wildcard": True}},
        "filter": [],
    })


def create_line_chart(dv_id):
    """Mean Exec Time — тренд по неделям (top 10 запросов)."""
    vis_id = str(uuid.uuid4())
    vis_state = {
        "title": "Mean Exec Time — тренд по неделям",
        "type":  "line",
        "params": {
            "type": "line",
            "grid": {"categoryLines": False},
            "categoryAxes": [{
                "id": "CategoryAxis-1", "type": "category", "position": "bottom",
                "show": True, "scale": {"type": "linear"},
                "labels": {"show": True, "truncate": 100}, "title": {}
            }],
            "valueAxes": [{
                "id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value",
                "position": "left", "show": True,
                "scale": {"type": "linear", "mode": "normal"},
                "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
                "title": {"text": "Mean Exec Time (сек)"}
            }],
            "seriesParams": [{
                "show": True, "type": "line", "mode": "normal",
                "data": {"label": "Mean Exec Time", "id": "1"},
                "valueAxis": "ValueAxis-1",
                "drawLinesBetweenPoints": True, "lineWidth": 2,
                "interpolate": "linear", "showCircles": True
            }],
            "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "times": [], "addTimeMarker": False,
        },
        "aggs": [
            {
                "id": "1", "enabled": True, "type": "avg", "schema": "metric",
                "params": {"field": "mean_exec_time", "customLabel": "Mean (сек)"}
            },
            {
                "id": "2", "enabled": True, "type": "date_histogram", "schema": "segment",
                "params": {
                    "field": "@timestamp", "useNormalizedEsInterval": True,
                    "interval": "1w", "drop_partials": False, "min_doc_count": 1,
                    "extended_bounds": {}, "customLabel": "Неделя"
                }
            },
            {
                "id": "3", "enabled": True, "type": "terms", "schema": "group",
                "params": {
                    "field": "query_short.keyword", "orderBy": "1",
                    "order": "desc", "size": 10, "otherBucket": False,
                    "customLabel": "Запрос"
                }
            },
        ],
    }
    api("POST", "/api/saved_objects/visualization/{0}".format(vis_id), json={
        "attributes": {
            "title":    vis_state["title"],
            "visState": json.dumps(vis_state),
            "uiStateJSON": "{}",
            "description": "",
            "kibanaSavedObjectMeta": {"searchSourceJSON": _search_source(dv_id)},
        }
    })
    log.info("Визуализация создана: %s", vis_state["title"])
    return vis_id


def create_bar_chart(dv_id):
    """Топ запросов по mean_exec_time (горизонтальный bar)."""
    vis_id = str(uuid.uuid4())
    vis_state = {
        "title": "Топ запросов — Mean Exec Time",
        "type":  "horizontal_bar",
        "params": {
            "type": "horizontal_bar",
            "grid": {"categoryLines": False},
            "categoryAxes": [{
                "id": "CategoryAxis-1", "type": "category", "position": "left",
                "show": True, "scale": {"type": "linear"},
                "labels": {"show": True, "truncate": 200}, "title": {}
            }],
            "valueAxes": [{
                "id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value",
                "position": "bottom", "show": True,
                "scale": {"type": "linear", "mode": "normal"},
                "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
                "title": {"text": "Mean Exec Time (сек)"}
            }],
            "seriesParams": [{
                "show": True, "type": "histogram", "mode": "normal",
                "data": {"label": "Mean Exec Time", "id": "1"},
                "valueAxis": "ValueAxis-1",
            }],
            "addTooltip": True, "addLegend": False, "legendPosition": "right",
            "times": [], "addTimeMarker": False,
        },
        "aggs": [
            {
                "id": "1", "enabled": True, "type": "avg", "schema": "metric",
                "params": {"field": "mean_exec_time", "customLabel": "Mean Exec Time (сек)"}
            },
            {
                "id": "2", "enabled": True, "type": "terms", "schema": "segment",
                "params": {
                    "field": "query_short.keyword", "orderBy": "1",
                    "order": "desc", "size": 15, "otherBucket": False,
                    "customLabel": "Запрос"
                }
            },
        ],
    }
    api("POST", "/api/saved_objects/visualization/{0}".format(vis_id), json={
        "attributes": {
            "title":    vis_state["title"],
            "visState": json.dumps(vis_state),
            "uiStateJSON": "{}",
            "description": "",
            "kibanaSavedObjectMeta": {"searchSourceJSON": _search_source(dv_id)},
        }
    })
    log.info("Визуализация создана: %s", vis_state["title"])
    return vis_id


def create_data_table(dv_id):
    """Таблица: все запросы с метриками (по выбранному периоду)."""
    vis_id = str(uuid.uuid4())
    vis_state = {
        "title": "Детализация запросов",
        "type":  "table",
        "params": {
            "perPage": 25,
            "showPartialRows": False,
            "showMetricsAtAllLevels": False,
            "sort": {"columnIndex": None, "direction": None},
            "showTotal": False,
            "totalFunc": "sum",
        },
        "aggs": [
            {
                "id": "1", "enabled": True, "type": "avg", "schema": "metric",
                "params": {"field": "mean_exec_time", "customLabel": "Mean (сек)"}
            },
            {
                "id": "2", "enabled": True, "type": "max", "schema": "metric",
                "params": {"field": "max_exec_time", "customLabel": "Max (сек)"}
            },
            {
                "id": "3", "enabled": True, "type": "sum", "schema": "metric",
                "params": {"field": "total_exec_time", "customLabel": "Total (сек)"}
            },
            {
                "id": "4", "enabled": True, "type": "sum", "schema": "metric",
                "params": {"field": "calls", "customLabel": "Вызовов"}
            },
            {
                "id": "5", "enabled": True, "type": "terms", "schema": "bucket",
                "params": {
                    "field": "query_short.keyword", "orderBy": "1",
                    "order": "desc", "size": 50, "otherBucket": False,
                    "customLabel": "Запрос"
                }
            },
        ],
    }
    api("POST", "/api/saved_objects/visualization/{0}".format(vis_id), json={
        "attributes": {
            "title":    vis_state["title"],
            "visState": json.dumps(vis_state),
            "uiStateJSON": "{}",
            "description": "",
            "kibanaSavedObjectMeta": {"searchSourceJSON": _search_source(dv_id)},
        }
    })
    log.info("Визуализация создана: %s", vis_state["title"])
    return vis_id


def create_metric(dv_id):
    """Метрика: количество уникальных запросов."""
    vis_id = str(uuid.uuid4())
    vis_state = {
        "title": "Уникальных запросов",
        "type":  "metric",
        "params": {
            "addTooltip": True,
            "addLegend": False,
            "type": "metric",
            "metric": {
                "percentageMode": False,
                "useRanges": False,
                "colorSchema": "Green to Red",
                "metricColorMode": "None",
                "colorsRange": [{"from": 0, "to": 10000}],
                "labels": {"show": True},
                "invertColors": False,
                "style": {
                    "bgFill": "#000", "bgColor": False,
                    "labelColor": False, "subText": "",
                    "fontSize": 60,
                }
            }
        },
        "aggs": [
            {
                "id": "1", "enabled": True,
                "type": "cardinality", "schema": "metric",
                "params": {"field": "query_hash", "customLabel": "Уникальных запросов"}
            }
        ],
    }
    api("POST", "/api/saved_objects/visualization/{0}".format(vis_id), json={
        "attributes": {
            "title":    vis_state["title"],
            "visState": json.dumps(vis_state),
            "uiStateJSON": "{}",
            "description": "",
            "kibanaSavedObjectMeta": {"searchSourceJSON": _search_source(dv_id)},
        }
    })
    log.info("Визуализация создана: %s", vis_state["title"])
    return vis_id


# ─── 4. Dashboard ─────────────────────────────────────────────────────────────

def create_dashboard(line_id, bar_id, table_id, metric_id):
    dash_id = str(uuid.uuid4())

    panels = [
        {
            "panelIndex": "1",
            "gridData": {"x": 0, "y": 0, "w": 8, "h": 4, "i": "1"},
            "version": "8.0.0",
            "type": "visualization",
            "id": metric_id,
        },
        {
            "panelIndex": "2",
            "gridData": {"x": 0, "y": 4, "w": 48, "h": 16, "i": "2"},
            "version": "8.0.0",
            "type": "visualization",
            "id": line_id,
        },
        {
            "panelIndex": "3",
            "gridData": {"x": 0, "y": 20, "w": 24, "h": 16, "i": "3"},
            "version": "8.0.0",
            "type": "visualization",
            "id": bar_id,
        },
        {
            "panelIndex": "4",
            "gridData": {"x": 24, "y": 20, "w": 24, "h": 16, "i": "4"},
            "version": "8.0.0",
            "type": "visualization",
            "id": table_id,
        },
    ]

    api("POST", "/api/saved_objects/dashboard/{0}".format(dash_id), json={
        "attributes": {
            "title":           "PostgreSQL Query Stats",
            "description":     "Еженедельная статистика из pg_stat_statements",
            "panelsJSON":      json.dumps(panels),
            "optionsJSON":     json.dumps({"darkTheme": False, "hidePanelTitles": False, "useMargins": True}),
            "timeRestore":     False,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"language": "kuery", "query": ""},
                    "filter": [],
                })
            },
        }
    })
    log.info("Dashboard создан: id=%s", dash_id)
    log.info("Откройте Kibana → Dashboards → 'PostgreSQL Query Stats'")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Настройка Kibana для pg-query-stats ===")
    log.info("Kibana: %s", KIBANA_HOST)
    log.info("Индекс: %s", ES_INDEX)

    try:
        api("GET", "/api/status")
        log.info("Kibana доступна.")
    except Exception as e:
        log.error("Не удалось подключиться к Kibana: %s", e)
        sys.exit(1)

    apply_index_template()
    dv_id     = create_data_view()
    line_id   = create_line_chart(dv_id)
    bar_id    = create_bar_chart(dv_id)
    table_id  = create_data_table(dv_id)
    metric_id = create_metric(dv_id)
    create_dashboard(line_id, bar_id, table_id, metric_id)

    log.info("=== Готово! ===")


if __name__ == "__main__":
    main()
