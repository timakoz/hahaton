#!/usr/bin/env bash
# Перенос обученных моделей и данных приложения между серверами одним архивом.
#
#   ./scripts/state.sh export [файл.tgz]   # упаковать: models/ + data/app.db (правки) + data/projects/
#   ./scripts/state.sh import файл.tgz     # распаковать в этот проект (существующие файлы перезаписываются)
#   ./scripts/state.sh check               # что есть в models/ и сколько места занимает проект
#
# Обучающие данные (data/processed, data/synth), логи и окружение .venv НЕ переносятся:
# приложению они не нужны, а для переобучения пересоздаются (prepare ~20 с, synth ~5 мин).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

case "${1:-}" in
  export)
    OUT="${2:-$ROOT/forestmerge_state_$(date +%Y%m%d_%H%M).tgz}"
    ITEMS=()
    for p in models data/app.db data/projects; do [[ -e "$p" ]] && ITEMS+=("$p"); done
    [[ -d models ]] || { echo "Нет папки models/ — сначала обучите модели"; exit 1; }
    # catboost_info — логи обучения CatBoost, приложению не нужны
    tar czf "$OUT" --exclude='catboost_info' "${ITEMS[@]}"
    echo "Архив: $OUT ($(du -h "$OUT" | cut -f1))"
    echo "Состав: ${ITEMS[*]}"
    command -v sha256sum >/dev/null && sha256sum "$OUT"
    echo "Скопировать на другой сервер:  scp \"$OUT\" user@app-server:/opt/hahaton/"
    ;;
  import)
    IN="${2:?укажите архив: ./scripts/state.sh import файл.tgz}"
    command -v sha256sum >/dev/null && sha256sum "$IN"
    tar xzf "$IN" -C "$ROOT"
    echo "Распаковано в $ROOT:"
    ls -la models/*/ 2>/dev/null | grep -E "\.(cbm|onnx|txt|json)$" || true
    echo "Если приложение уже запущено — перезапустите: ./scripts/app.sh stop && ./scripts/app.sh start"
    ;;
  check)
    for f in models/impute/height.cbm models/impute/diameter.cbm models/impute/age.cbm models/impute/meta.json \
             models/encoder/encoder.onnx models/encoder/meta.json models/ranker/model.txt models/ranker/features.json; do
      if [[ -f "$f" ]]; then echo "ok    $f ($(du -h "$f" | cut -f1))"; else echo "НЕТ   $f"; fi
    done
    echo "--- место ---"
    du -sh "$ROOT" .venv data models logs 2>/dev/null || true
    ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
