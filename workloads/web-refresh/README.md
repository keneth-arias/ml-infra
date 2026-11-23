# Actualización horaria de AirAware Web en bm

`cronjob.json` instala `airaware-hourly-forecast` en `airaware-web-refresh`.
Usa el contexto **microk8s**, nodo **bm**, CPU y las imágenes locales fijadas
por digest. No ejecuta trabajo en Nebius. Se verificó MicroK8s v1.36.2,
32 CPU y 62 148 720 KiB de memoria asignable antes de la primera ejecución.
La zona horaria de CronJob requiere Kubernetes 1.27 o posterior.
[Contrato oficial de CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

La expresión `0 * * * *`, con `timeZone: America/Lima`, ejecuta cada hora.
La captura no consulta ni publica antes de `2026-11-23T14:00:00Z`: **23 de
noviembre de 2026 a las 09:00 en Lima**. Las ejecuciones anteriores completan
sin trabajo. Desde ese momento continúa las 24 horas del día. `Forbid`
evita solapamientos programados; cada Job tiene 15 minutos de plazo y cero
reintentos automáticos. Un fallo espera al siguiente horario y conserva
el último archivo del sitio. El desfase del reloj del gráfico de SENAMHI
no está confirmado; la zona horaria del programador no modifica sus etiquetas.

El flujo tiene tres contenedores consecutivos:

1. Captura como máximo doce gráficos de hasta tres días mediante Lightpanda.
   Conserva los bytes y recibos en `receipts`, un volumen local con Retain.
2. Ejecuta en CPU la CNN pública de AirAware sobre historias completas de
   24 horas, con pesos verificados y sin acceso a las credenciales Git.
3. Valida y exporta seis horizontes. Clona `qriqs/airaware-web`, reemplaza solo
   `public/datos/actual.json`, crea un commit y hace `git push origin HEAD:main`.
   Vercel despliega el commit mediante la integración Git existente.

No combina instrumentos, imputa datos ni reemplaza observaciones con
predicciones. Si todas las lecturas tienen más de seis horas de retraso,
la captura falla. Una estación incompleta queda con valores `null`. La web
avisa cuando la captura publicada tiene más de dos horas.

La publicación rechaza un archivo anterior al que está en `main`, no usa
`force` y falla ante una escritura concurrente. Conserva 14 días de recibos
tras las publicaciones exitosas. El volumen pide 2 GiB; cada contenedor
solicita 300 mCPU y 256 MiB, con límites de 2 CPU y 2 GiB de RAM. El namespace
aplica Pod Security `restricted`; no se monta un token de Kubernetes.

## Insumos privados y código

Los objetos auxiliares se instalaron en el mismo namespace:

- `web-refresh-code`: `refresh.py` y `stage_external_pm.py` de AirAware
  `83dd496b1c0d64b9da5928dffd682337808c2da8`, `compare_cnn.py` y `run.sh`.
- `web-refresh-cnn`: pesos de solo lectura, SHA-256
  `9ceb6dcd793f661720fd58606992581857993f66109dc3024480e164316b7315`.
- `web-refresh-git`: clave SSH ya autorizada de Keneth y `known_hosts`
  verificado del equipo. Solo el publicador monta este Secret. No se agregó
  una cuenta ni se modificaron permisos del repositorio. Una clave exclusiva
  del repositorio requiere que su propietario la registre; Keneth tiene
  permiso de escritura, sin administración de deploy keys en este repo.

El código debe corresponder a la revisión declarada antes de reemplazar
el ConfigMap. Pesos y secretos quedan fuera de Git. La imagen de captura
requiere `/opt/vendor` en `PYTHONPATH`; el publicador incluye una cuenta
Unix UID 1000 porque OpenSSH exige una entrada en `/etc/passwd`.

## Construir y reinstalar

Desde `ml-infra`, con AirAware al costado:

```sh
kubectl --context microk8s get nodes -o wide
kubectl --context microk8s describe node bm
docker image inspect local/airaware-fresh-peak-collector:synthetic-qualified --format '{{.Id}}'
```

La base local debe tener ID
`sha256:e8e3caa7645d74297af205c45aeb3af865cd24864fa9b2e96e95e7ae07a0ea64`.
Construye `workloads/web-refresh/Dockerfile`, exporta con `docker save`, importa
con `microk8s ctr images import` y registra el digest real con
`microk8s ctr images tag TAG REFERENCIA_CON_DIGEST`. Conserva `/opt/vendor`
al cambiar `PYTHONPATH`.

Para un cluster nuevo, instala primero el Namespace de `cronjob.json`, luego
el código y los insumos privados. No descargues ni publiques pesos en Git.

```sh
kubectl --context microk8s -n airaware-web-refresh create configmap web-refresh-code \
  --from-file=refresh.py=../airaware/tools/web-refresh/refresh.py \
  --from-file=stage_external_pm.py=../airaware/tools/timenet-smoke/stage_external_pm.py \
  --from-file=compare_cnn.py=../airaware/tools/training/compare_cnn.py \
  --from-file=run.sh=workloads/web-refresh/run.sh
```

Crea `web-refresh-cnn` desde el archivo privado `model.pt` verificado y
`web-refresh-git` con archivos `id_ed25519` y `known_hosts`. No imprimas sus
valores. `render.py --publisher-image REFERENCIA --airaware-revision SHA`
regenera el manifiesto con nuevas revisiones. Antes de activarlo:

```sh
kubectl --context microk8s apply --dry-run=server --validate=strict -f workloads/web-refresh/cronjob.json
kubectl --context microk8s diff -f workloads/web-refresh/cronjob.json
kubectl --context microk8s apply -f workloads/web-refresh/cronjob.json
kubectl --context microk8s -n airaware-web-refresh get cronjob,jobs,pvc
```

## Operación y recuperación

```sh
kubectl --context microk8s -n airaware-web-refresh get jobs
kubectl --context microk8s -n airaware-web-refresh logs job/NOMBRE -c capture
kubectl --context microk8s -n airaware-web-refresh logs job/NOMBRE -c forecast
kubectl --context microk8s -n airaware-web-refresh logs job/NOMBRE -c publish
```

Para pausar, usa `kubectl --context microk8s -n airaware-web-refresh patch
cronjob airaware-hourly-forecast --type merge -p '{"spec":{"suspend":true}}'`.
Esto no detiene un Job activo. Conserva el PVC, pesos, código y recibos.
Para recuperar datos erróneos del sitio, revierte únicamente el commit de
datos correspondiente, después de suspender las nuevas publicaciones.
Si `bm` o alguna imagen local no está disponible, el Job queda pendiente:
no se cambia su destino a una nube de pago.

## Verificación de la entrega

La captura inicial validó 7/7 estaciones, 54 historias completas y sus seis
salidas finitas. La última etiqueta publicada fue `2026-11-22T23:00:00`;
San Borja y San Martín de Porres tenían un hueco en ese origen. Las pruebas
de AirAware (576), del exportador (5), de la web (57) y de Chromium (8)
aprobaron, junto con compilaciones, formato, tipos y auditorías sin hallazgos.
La validación existente de manifiestos y el dry run del servidor aprobaron.
La identidad Git se comprobó desde un Pod restringido con `git ls-remote`.
Los recibos de la ejecución local están en
`/home/kymai/.local/state/ml-infra/airaware-web-refresh-20261123/`.

La integración de Vercel devolvió `Deployment was blocked` para el commit
publicado en `main` y para el commit anterior incorporado durante la entrega.
GitHub aceptó la publicación. El panel redirige al inicio de sesión, por lo que
el motivo específico requiere acceso del propietario a Vercel. El CronJob
actualiza Git; este bloqueo externo debe resolverse para que esos commits
actualicen el dominio de producción. No se cambió la identidad del autor
para eludir las reglas de despliegue.
