"""Abrir los PR de datos desde bm con la identidad de GitHub del publicador."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

REPO = "qriqs/airaware-web"
OWNER = "qriqs"


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True, timeout=60).strip()


def open_review(commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("SHA de publicación inválido")
    branch = f"forecast/gemma-{commit}"
    prs = json.loads(gh("api", "-X", "GET", f"repos/{REPO}/pulls", "-f", "state=all",
                        "-f", f"head={OWNER}:{branch}", "-f", "base=main"))
    if len(prs) > 1:
        raise ValueError("La rama tiene más de un PR")
    if prs:
        pr = prs[0]
        if pr["head"]["sha"] != commit:
            raise ValueError("La rama del PR cambió")
        if pr["state"] == "closed":
            return pr["number"]
    else:
        head = gh("api", f"repos/{REPO}/git/ref/heads/{branch}", "--jq", ".object.sha")
        if head != commit:
            raise ValueError("La rama remota difiere del commit exportado")
        diff = json.loads(gh("api", f"repos/{REPO}/compare/main...{commit}"))
        if (diff["ahead_by"] != 1 or
            [(f["filename"], f["status"]) for f in diff["files"]] != [("public/datos/actual.json", "modified")]):
            raise ValueError("El PR horario debe cambiar únicamente los datos")
        pr = json.loads(gh("api", "-X", "POST", f"repos/{REPO}/pulls",
                           "-f", "base=main", "-f", f"head={branch}", "-f",
                           "title=Actualizar el pronóstico horario de Gemma 1B", "-f",
                           "body=Captura de SENAMHI y seis pronósticos puntuales por historia completa. Revisión y fusión a cargo de @qriqs."))
    number = pr["number"]
    if OWNER not in {r["login"] for r in pr["requested_reviewers"]}:
        reviewed = gh("api", "--paginate", f"repos/{REPO}/pulls/{number}/reviews",
                      "--jq", '.[] | select(.user.login == "qriqs") | .commit_id')
        if commit not in reviewed.splitlines():
            gh("api", "-X", "POST", f"repos/{REPO}/pulls/{number}/requested_reviewers",
               "-f", f"reviewers[]={OWNER}")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path)
    args = parser.parse_args()
    failed = False
    for intent in sorted(args.runs.glob("*/pr-head")):
        receipt = intent.with_name("pr-number")
        if receipt.exists():
            continue
        try:
            number = open_review(intent.read_text().strip())
            tmp = receipt.with_suffix(".tmp")
            tmp.write_text(f"{number}\n")
            os.replace(tmp, receipt)
            print(f"{intent.parent.name}: https://github.com/{REPO}/pull/{number}", flush=True)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            # Una rama pendiente no debe impedir la apertura de otras.
            print(f"{intent.parent.name}: {error}", flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
