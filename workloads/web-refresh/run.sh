#!/bin/sh
set -eu
run="/state/runs/$JOB_NAME"
case "$1" in
  capture)
    exec /opt/python/bin/python3.13 /config/refresh.py capture "$run"
    ;;
  predict)
    exec /opt/conda/bin/python /config/refresh.py predict "$run"
    ;;
  correct)
    export PYTHONPATH="/release/tools/timenet-smoke${PYTHONPATH:+:$PYTHONPATH}"
    exec /opt/python/bin/python3.13 /config/refresh.py correct "$run"
    ;;
  publish)
    /opt/python/bin/python3.13 /config/refresh.py export "$run"
    test ! -f "$run/skip" || exit 0
    # La ejecución manual inicial solo exporta; la publicación requiere la interfaz instalada.
    test "${PUBLISH:-true}" = true || exit 0
    mkdir -m 700 /tmp/ssh
    cp /credentials/id_ed25519 /tmp/ssh/key
    chmod 600 /tmp/ssh/key
    export GIT_SSH_COMMAND='ssh -i /tmp/ssh/key -o IdentitiesOnly=yes -o UserKnownHostsFile=/credentials/known_hosts -o StrictHostKeyChecking=yes'
    git clone --depth 1 --single-branch --branch main git@github.com:qriqs/neblima-web.git /tmp/web
    cd /tmp/web
    /opt/python/bin/python3.13 - "$run/actual.json" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

target = Path("public/datos/actual.json")
if not target.exists():
    raise SystemExit("Falta instalar el consumidor de datos en main; no publicar todavía")
old = json.loads(target.read_bytes())
new = json.loads(Path(sys.argv[1]).read_bytes())
if old.get("schema_version") != 2 or any(
    old.get("model", {}).get(key) != new["model"][key]
    for key in ("release_sha256", "checkpoint_sha256", "correction_sha256")
):
    raise SystemExit("La web aún no usa esta versión de Gemma")
if datetime.fromisoformat(new["generated_at_utc"]) <= datetime.fromisoformat(old["generated_at_utc"]):
    raise SystemExit("La captura es anterior a la publicada; no reemplazarla")
target.write_bytes(Path(sys.argv[1]).read_bytes())
PY
    git add -- public/datos/actual.json
    git diff --cached --quiet && exit 0
    git -c user.name='k.arias' -c user.email='keneth.arias@outlook.com' \
      commit -m 'chore(datos): actualiza SENAMHI y el pronóstico de seis horas'
    # Sin force ni reintentos que puedan sobrescribir una publicación concurrente.
    commit=$(git rev-parse HEAD)
    branch="forecast/gemma-$commit"
    # Registrar primero la intención para poder retomar tras una interrupción.
    printf '%s\n' "$commit" > "$run/pr-head.tmp"
    mv "$run/pr-head.tmp" "$run/pr-head"
    git push origin "HEAD:refs/heads/$branch"
    # Conservar los recibos recientes y acotar el uso del volumen local.
    find /state/runs -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec sh -c '
      for dir do
        if test ! -f "$dir/pr-head" || test -f "$dir/pr-number"; then rm -rf -- "$dir"; fi
      done' sh {} +
    ;;
  *) exit 2 ;;
esac
