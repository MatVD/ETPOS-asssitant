#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_SHA="${1:?Usage: remote-deploy.sh <git-sha>}"
APP_DIR="${ETPOS_APP_DIR:-/opt/etpos-assistant}"
SERVICE_NAME="${ETPOS_SERVICE_NAME:-etpos-assistant.service}"
HEALTH_URL="${ETPOS_HEALTH_URL:-http://127.0.0.1:8787/health/ready}"
HEALTH_ATTEMPTS="${ETPOS_HEALTH_ATTEMPTS:-30}"
HEALTH_DELAY_SECONDS="${ETPOS_HEALTH_DELAY_SECONDS:-2}"

cd "$APP_DIR"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Refus du déploiement: le worktree de production contient des modifications suivies." >&2
    exit 1
fi

PREVIOUS_SHA="$(git rev-parse HEAD)"
CHECKED_OUT=0

wait_for_health() {
    local attempt
    for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
        if curl --fail --silent --show-error "$HEALTH_URL" >/dev/null; then
            return 0
        fi
        sleep "$HEALTH_DELAY_SECONDS"
    done
    return 1
}

rollback() {
    local rc=$?
    trap - ERR

    if [[ "$CHECKED_OUT" -eq 1 ]]; then
        echo "Déploiement en échec. Rollback vers $PREVIOUS_SHA" >&2
        git checkout --detach "$PREVIOUS_SHA"
        .venv/bin/python -m pip install --disable-pip-version-check -e .
        sudo -n systemctl restart "$SERVICE_NAME"
        if ! wait_for_health; then
            echo "ALERTE: le rollback a été appliqué mais le health check reste en échec." >&2
        fi
    fi

    exit "$rc"
}
trap rollback ERR

echo "Déploiement $TARGET_SHA (actuel: $PREVIOUS_SHA)"
git fetch --prune origin main
git cat-file -e "${TARGET_SHA}^{commit}"
git checkout --detach "$TARGET_SHA"
CHECKED_OUT=1

.venv/bin/python -m pip install --disable-pip-version-check -e .
.venv/bin/etpos-assistant init-db
.venv/bin/etpos-assistant eval-retrieval --path eval/questions.example.jsonl --limit 5

sudo -n systemctl restart "$SERVICE_NAME"
sudo -n systemctl is-active --quiet "$SERVICE_NAME"
wait_for_health

trap - ERR
echo "Déploiement validé: $TARGET_SHA"
