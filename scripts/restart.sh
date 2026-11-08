#!/bin/bash
# Перезапуск сервисов после правок кода. Образ собирается отдельно:
#   docker compose build worker web bot
set -e
cd "$(dirname "$0")/.."
docker compose up -d --force-recreate worker web
docker compose --profile bot up -d --force-recreate bot
sleep 3
docker ps --format '{{.Names}}\t{{.Status}}' | grep favourites
