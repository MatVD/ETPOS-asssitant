#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

CLI=".venv/bin/etpos-assistant"
if [ ! -x "$CLI" ]; then
  echo "ETPOS Assistant n'est pas installé dans .venv. Exécute d'abord: make install" >&2
  exit 1
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="eval/results"
mkdir -p "$RESULT_DIR"

printf '%s\n' "== Codex =="
"$CLI" codex-status

printf '%s\n' "== Source Support officielle =="
"$CLI" inspect-source etpos-support-fr

printf '%s\n' "== Retrieval benchmark principal =="
"$CLI" eval-retrieval \
  --path eval/benchmark.jsonl \
  --docs-db data/docs.db \
  --limit 5 \
  --min-recall 0.95 \
  --min-mrr 0.80 \
  --min-group-coverage 0.95

printf '%s\n' "== Retrieval acceptance =="
"$CLI" eval-retrieval \
  --path eval/acceptance.jsonl \
  --docs-db data/docs.db \
  --limit 5 \
  --min-recall 0.95 \
  --min-group-coverage 0.95

printf '%s\n' "== Retrieval contrat Support =="
"$CLI" eval-retrieval \
  --path eval/support.jsonl \
  --docs-db data/docs.db \
  --limit 5 \
  --min-recall 0.95 \
  --min-group-coverage 0.95

export ETPOS_PROVIDER=codex

printf '%s\n' "== Réponses Codex benchmark principal =="
"$CLI" eval-answer \
  --path eval/benchmark.jsonl \
  --json-output "$RESULT_DIR/benchmark-$STAMP.json"

printf '%s\n' "== Réponses Codex acceptance =="
"$CLI" eval-answer \
  --path eval/acceptance.jsonl \
  --json-output "$RESULT_DIR/acceptance-$STAMP.json"

printf '%s\n' "== Réponses Codex contrat Support =="
"$CLI" eval-answer \
  --path eval/support.jsonl \
  --json-output "$RESULT_DIR/support-$STAMP.json"

printf '%s\n' "Baseline terminée. Rapports :"
printf '  %s\n' \
  "$RESULT_DIR/benchmark-$STAMP.json" \
  "$RESULT_DIR/acceptance-$STAMP.json" \
  "$RESULT_DIR/support-$STAMP.json"
