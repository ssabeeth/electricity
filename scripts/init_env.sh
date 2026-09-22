#!/usr/bin/env bash
# Create .env from .env.example with random secrets (only if .env is missing).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
if [[ -f .env ]]; then
  echo ".env exists; leaving it unchanged"
  exit 0
fi
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
cp .env.example .env
sed -i.bak \
  -e "s|^AIRFLOW_ADMIN_PASSWORD=.*|AIRFLOW_ADMIN_PASSWORD=$(rand)|" \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand)|" \
  -e "s|^AIRFLOW_JWT_SECRET=.*|AIRFLOW_JWT_SECRET=$(rand)|" \
  -e "s|^AIRFLOW_API_SECRET_KEY=.*|AIRFLOW_API_SECRET_KEY=$(rand)|" \
  -e "s|^AIRFLOW_UID=.*|AIRFLOW_UID=$(id -u)|" \
  .env
rm -f .env.bak
echo "Created .env (Airflow admin password: see AIRFLOW_ADMIN_PASSWORD in .env)"
