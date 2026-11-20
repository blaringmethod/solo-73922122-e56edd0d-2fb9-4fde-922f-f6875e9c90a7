#!/usr/bin/env bash
# Convenience launcher for local (non-Docker) development.
# Defaults to a file-based SQLite database (same shape as docker-compose);
# point DATABASE_URL at PostgreSQL for the production dialect.
set -euo pipefail

export DATABASE_URL="${DATABASE_URL:-sqlite:////tmp/research.db}"
export PYTHONPATH="$(cd "$(dirname "$0")" && pwd):${PYTHONPATH:-}"

python -m app.db_init
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
