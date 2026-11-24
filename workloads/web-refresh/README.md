# Pronóstico horario con Gemma 3 1B en bm

La publicación actual de Neblima usa Qwen con MLX en OrbStack. Consulta el
[runbook de OrbStack](qwen/README.orbstack.md) y el
[registro del cambio de publicador](qwen/HANDOFF.md) antes de modificar el
CronJob que publica a la web.

El CronJob de MicroK8s usa la hora de Lima para la programación y conserva la hora publicada por SENAMHI para cada origen. Produce seis valores puntuales en µg/m³. El benchmark de OpenAQ mantiene su reloj UTC. La autorización de la demostración pública cubre pronósticos puntuales. Los intervalos y las alertas tienen una evaluación separada.

## Flujo

1. La captura Lightpanda verifica la identidad de cada estación y selecciona historias completas de 24 horas. Las lecturas faltantes quedan como nulas.
2. El contenedor de inferencia pide una GPU y 12 GiB en bm. Usa la imagen local del entrenador, los pesos de Gemma, el checkpoint del paso 4500 y un NPZ que solo contiene identificadores e historias. El almacenamiento del modelo es de solo lectura.
3. La imagen de puntuación aplica en CPU la corrección congelada de la semilla 27. Verifica los hashes de la historia, el checkpoint y la corrección.
4. El publicador verifica las seis salidas por historia y la identidad de la versión en la web. Solo este contenedor recibe la credencial de Git. Un error o una captura anterior conserva la última publicación. Los recibos permanecen en el PVC local.

## Instalación y cambio de versión

Verifica el contexto local, el nodo y sus recursos antes de ejecutar. El manifiesto usa el namespace restringido existente, los PVC locales con Retain y el Secret de publicación ya instalado. Obtén el código de AirAware de la revisión que figura en el manifiesto. Crea el ConfigMap de esta versión en el namespace:

```sh
microk8s kubectl --context microk8s -n airaware-web-refresh create configmap web-refresh-gemma-code \
  --from-file=refresh.py=../airaware/tools/web-refresh/refresh.py \
  --from-file=stage_external_pm.py=../airaware/tools/timenet-smoke/stage_external_pm.py \
  --from-file=run.sh=workloads/web-refresh/run.sh
```

Prepara el PVC privado `gemma-1b-release` (6 GiB). Copia el directorio `weights/` y su archivo vecino `SHA256SUMS` del paquete Gemma autorizado. Verifica el hash del archivo de sumas (`ea4cab79e8e31a5db12eb91dd04875a3f35874170f9066eecb964f2cc1935853`) y ejecuta `sha256sum --strict --check SHA256SUMS` desde la raíz. Copia el directorio completo `step-00004500/` cuyo manifiesto empieza por `f1da0c41`. En `calibration/` conserva el contenido verificado de `cpu_code_subpath` del despliegue privado, con `correction.json`, `digests.json` y `tools/timenet-smoke/`. Verifica el manifiesto de código y cada archivo contra los digests del despliegue. Las imágenes de los cuatro contenedores deben existir en containerd de bm bajo las referencias exactas del manifiesto. Para la imagen del publicador se conserva el procedimiento del Dockerfile en esta carpeta.

El CronJob se ejecuta al inicio de cada hora en Lima (`0 * * * *`). Durante la transición, mantén `PUBLISH=false` en el contenedor `publish`: cada ejecución captura las lecturas de SENAMHI, calcula los seis horizontes y guarda `actual.json` en el PVC. Comprueba que qriqs fusionó el PR del sitio, que el dominio público sirve el JSON de Gemma y que `main` contiene la versión compatible antes de quitar esa variable. A partir de entonces cada captura nueva puede abrir su propio PR para revisión.

Para detener nuevas publicaciones, suspende el CronJob. Para volver a la CNN, restaura conjuntamente el consumidor, los datos y el manifiesto de su versión. Conserva ambos PVC y el historial de pronósticos. La publicación en Git y el despliegue de Vercel se comprueban por separado.

## Revisión de cada captura por qriqs

El Pod solo sube la rama `forecast/gemma-<SHA completo>` y registra el commit en el archivo local `pr-head`. El temporizador de usuario de bm ejecuta `open-review.py RUTA_DEL_PVC/runs` con la sesión existente de GitHub CLI. Verifica la rama remota y comprueba que el único cambio sea la captura. Abre el PR hacia `main`, solicita la revisión de `qriqs` y guarda su número en el recibo local. Un reintento recupera el PR existente. El aprobador decide si la captura sigue vigente antes de fusionarla y puede cerrar una anterior. El publicador no aprueba, fusiona ni sube commits a `main`.

El servicio se instala en la cuenta de bm que ya tiene permiso de crear PR con GitHub CLI. Copia el script en `~/.local/lib/airaware-web-review/open-review.py` y las dos unidades en `~/.config/systemd/user/`. Configura `AIRAWARE_RUNS` en `~/.config/airaware-web-review.env` con la ruta real a `runs` del PVC `receipts`. Activa el temporizador con `systemctl --user daemon-reload` y `systemctl --user enable --now airaware-web-review.timer`. Los tokens de la cuenta no se montan en Kubernetes.
