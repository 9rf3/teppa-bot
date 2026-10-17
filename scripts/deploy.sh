#!/usr/bin/env bash
# Обновление бота на VPS одной командой: git pull + пересборка контейнера.
# Запускать оттуда, где лежит репозиторий (например /opt/teppa-bot):
#   ./scripts/deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "→ git pull"
git pull --ff-only

echo "→ docker compose up -d --build"
docker compose up -d --build

echo
docker compose ps
echo
echo "→ последние логи:"
docker compose logs --tail=20 bot
