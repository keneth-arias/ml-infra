#!/bin/sh
set -eu
run="/state/runs/$JOB_NAME"
case "$1" in
  predict)
    exec /opt/conda/bin/python -u /config/qwen.py predict "$run"
    ;;
  predict-mlx)
    exec /opt/python/bin/python3.13 -u /config/qwen.py predict-mlx "$run"
    ;;
  publish)
    /opt/python/bin/python3.13 -u /config/qwen.py export "$run"
    test ! -f "$run/skip" || exit 0
    # La ejecución manual inicial solo exporta; la publicación requiere la interfaz instalada.
    test "${PUBLISH:-false}" = true || exit 0
    mkdir -m 700 /tmp/ssh
    cp /credentials/id_ed25519 /tmp/ssh/key
    chmod 600 /tmp/ssh/key
    export GIT_SSH_COMMAND='ssh -i /tmp/ssh/key -o IdentitiesOnly=yes -o UserKnownHostsFile=/credentials/known_hosts -o StrictHostKeyChecking=yes'
    git clone --depth 1 --single-branch --branch main git@github.com:qriqs/neblima-web.git /tmp/web
    cd /tmp/web
    /opt/python/bin/python3.13 -u /config/publish.py "$run"
    # Conservar los recibos recientes y acotar el uso del volumen local.
    find /state/runs -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf -- {} \;
    ;;
  *) exit 2 ;;
esac
