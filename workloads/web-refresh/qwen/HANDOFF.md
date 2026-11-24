# Neblima OrbStack and MLX handover

The live hourly publisher runs on `m4` in OrbStack. It reads SENAMHI at
minute 15 in `America/Lima` to account for stations that populate an hour
after the boundary. The `bm` MicroK8s Qwen publisher is suspended and
kept as the rollback path. The Website repository is `qriqs/neblima-web`.
It receives only the generated `public/datos/actual.json` data commit.

## What is deployed

| Item | Verified identity |
| --- | --- |
| OrbStack CronJob | `orbstack / airaware-web-refresh / airaware-hourly-forecast` |
| Schedule | `15 * * * *`, `America/Lima`, `Forbid` |
| macOS service | `lat.orbitly.neblima-mlx` in the logged-in user's launchd session |
| Bridge | TLS on `127.0.0.1:18443` from macOS and `host.docker.internal:18443` from the Pod |
| Model | Qwen3.5-0.8B LoRA, Seed 27, step 15,745, MLX FP32 on Metal |
| Checkpoint | `ca2ee75b0702a7893bc76a196351a45900fd54b68347f724157a532062d5c0cf` |
| MLX weights | `9a29ea6d5608d1ce9bc47b0bbaaaf316319988fe3091cab30b40148cb105be43` |
| Local Linux arm64 image config | `sha256:9dc7c125a7b76b3e1ec5188a34d34e25420395f699dcdb6634955e2c5c3b9d18` |
| Live publisher ConfigMap at handover | `web-refresh-qwen-publish-79c205468a8d` |
| Capture source | Neblima `83dd496b1c0d64b9da5928dffd682337808c2da8`, packaged as `web-refresh-capture-83dd496b` |

The Pod captures PM2.5, submits complete 24-hour histories to Metal, and
publishes six-hour point forecasts. Upstream blanks remain missing. The
publisher revalidates the captured timestamp, observation hour, and
checkpoint after a non-fast-forward push and retries only against a new
head. A capture that was already superseded exits without a new commit.
The current publisher scripts are byte-for-byte copies of the mounted
ConfigMap. The renderer assigns new immutable ConfigMap names on future
builds.

## Evidence

- The OrbStack cutover on November 24, 2026 at 01:00 UTC produced 36
  forecasts for seven stations. A same-history comparison with bm on
  40 captured histories had maximum absolute drift of 0.0000411 µg/m³.
  [Replay](evidence/replay.json) · [Cutover publication](evidence/cutover-site.json)
- Moving to minute 15 advanced five stations from 19:00 to 20:00 Lima
  time and produced 40 forecasts. Santa Anita's 20:00 PM2.5 cell was
  blank in the official station table. [SENAMHI publication](evidence/senamhi-site.json) · [Official station map](https://www.senamhi.gob.pe/mapas/mapa-ca-monitoreo/map_data_ca.php)
- The latest handover check confirmed the OrbStack and MicroK8s schedule
  states, the running LaunchAgent, the completed publisher Job, and the
  data served on the website. [Handover readback](evidence/live-handover.json) · [Website data](https://neblima.orbitly.lat/datos/actual.json)

## Operate and restore

- Always name `orbstack` or `microk8s` explicitly. See the
  [OrbStack runbook](README.orbstack.md) for image, LaunchAgent, TLS, and
  receipt setup. The [bm runbook](README.md) covers the suspended fallback.
- Check the latest hourly Job log and compare its published commit with
  the public payload. Alert on a stale observation hour, repeated failed
  captures, or missing forecast windows. The Mac uses the verified FP32
  bundle from the [portable export](../../qwen-portable/README.md).
- Roll back by suspending the OrbStack CronJob and draining every active
  publisher Job. Then resume bm and verify the next data commit and live
  hash. Schedule a separate maintenance check for login and restart
  after a complete macOS reboot.
