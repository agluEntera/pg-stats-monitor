#!/bin/bash
set -e

echo "=== pg-stats-monitor setup (Elasticsearch + Kibana) ==="

# Определяем доступную команду docker compose / docker-compose
if docker compose version &>/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null 2>&1; then
    DOCKER_COMPOSE="docker-compose"
else
    echo "[!] Не найден ни 'docker compose' (v2), ни 'docker-compose' (v1). Установите Docker."
    exit 1
fi

echo "[i] Используется: $DOCKER_COMPOSE ($(${DOCKER_COMPOSE} version --short 2>/dev/null || ${DOCKER_COMPOSE} --version))"

# 1. Проверить .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[!] Создан .env — заполните параметры подключения и запустите снова."
    exit 1
fi

set -a; source .env; set +a

# 2. Поднять collector
echo "[*] Сборка и запуск collector..."
${DOCKER_COMPOSE} up -d --build
echo "[+] Collector запущен (cron каждый понедельник 09:00)."

# 3. Применить index template + создать Kibana dashboard
echo "[*] Настройка Kibana..."
pip install --quiet requests
python3 kibana/setup_kibana.py

echo ""
echo "=== Готово! ==="
echo "Kibana Dashboard: ${KIBANA_HOST}/app/dashboards"
echo ""
echo "Тестовый запуск сборщика (не ждать понедельника):"
echo "  ${DOCKER_COMPOSE} exec collector python collect.py"
