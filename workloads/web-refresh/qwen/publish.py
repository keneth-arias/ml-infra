"""Publish an exported Qwen snapshot from the workload's disposable Git clone."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TARGET = Path("public/datos/actual.json")
MAX_ATTEMPTS = 3


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def publish_snapshot(repo: Path, source: Path, model: str, checkpoint: str) -> str | None:
    candidate = source.read_bytes()
    new = json.loads(candidate)
    target = repo / TARGET
    base = git(repo, "rev-parse", "HEAD")
    for attempt in range(MAX_ATTEMPTS):
        if not target.exists():
            raise SystemExit("Falta instalar el consumidor de datos en main")
        old = json.loads(target.read_bytes())
        for snapshot in (old, new):
            identity = snapshot["model"]
            if snapshot["schema_version"] != 1 or (
                identity["name"],
                identity["checkpoint_sha256"],
            ) != (model, checkpoint):
                raise SystemExit(
                    "La web y la captura deben usar el mismo candidato congelado de Qwen"
                )
        if not any(
            window["forecast"] is not None
            for station in new["stations"].values()
            for window in station["windows"]
        ):
            raise SystemExit("La captura no tiene historias completas para pronosticar")
        if datetime.fromisoformat(new["generated_at_utc"]) <= datetime.fromisoformat(
            old["generated_at_utc"]
        ):
            print("La web ya tiene una captura igual o más reciente")
            return None
        if new["timeline"][-1] < old["timeline"][-1]:
            raise SystemExit("La última hora observada retrocedió")

        target.write_bytes(candidate)
        git(repo, "add", "--", str(TARGET))
        git(
            repo,
            "-c",
            "user.name=k.arias",
            "-c",
            "user.email=keneth.arias@outlook.com",
            "commit",
            "-m",
            "chore(datos): actualiza SENAMHI y el pronóstico de seis horas",
            "--",
            str(TARGET),
        )
        commit = git(repo, "rev-parse", "HEAD")
        try:
            git(repo, "push", "origin", "HEAD:refs/heads/main")
        except subprocess.CalledProcessError:
            git(repo, "fetch", "--depth=1", "--no-tags", "--refmap=", "origin", "refs/heads/main")
            updated_base = git(repo, "rev-parse", "FETCH_HEAD")
            if updated_base == commit:
                return commit
            if updated_base == base or attempt + 1 == MAX_ATTEMPTS:
                raise
            print("main cambió durante la publicación. Volviendo a validar la captura.", flush=True)
            git(repo, "checkout", "--detach", updated_base)
            base = updated_base
        else:
            return commit


if __name__ == "__main__":
    from qwen import CHECKPOINT, MODEL

    run = Path(sys.argv[1])
    published = publish_snapshot(Path.cwd(), run / "actual.json", MODEL, CHECKPOINT)
    if published:
        print(f"Captura publicada: {published}")
        (run / "published-commit.txt").write_text(f"{published}\n")
