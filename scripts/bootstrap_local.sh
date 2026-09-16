#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Python 3.12 est requis." >&2
  exit 1
fi

[ -f .env ] || cp .env.example .env
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/etpos-assistant init-db

echo "Initialisation terminée. Crée maintenant un utilisateur avec :"
echo "  .venv/bin/etpos-assistant create-user"
