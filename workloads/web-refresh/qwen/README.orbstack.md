# Actualización horaria de Neblima Web en m4

El [registro de operación](HANDOFF.md) contiene las huellas verificadas
del despliegue activo y la evidencia de la primera publicación.

El CronJob `airaware-web-refresh/airaware-hourly-forecast` del contexto
`orbstack` realiza la captura, solicita el pronóstico y publica
`public/datos/actual.json`. Se ejecuta en el minuto 15 de cada hora en
`America/Lima` para dar tiempo a que SENAMHI publique las nuevas
lecturas de sus estaciones. Cada captura conserva la hora original de
las observaciones y los valores faltantes de PM2.5.
El servicio `lat.orbitly.neblima-mlx` corre como LaunchAgent de macOS
y usa Metal con el bundle FP32 de Qwen3.5-0.8B, semilla 27, paso 15 745.
Cada solicitud carga el modelo una vez y procesa grupos de hasta 16
ventanas. El modelo y la clave privada TLS se quedan en macOS.

## Despliegue y verificación

- Construye [Dockerfile.orbstack](Dockerfile.orbstack) en el motor local
  de OrbStack para Linux arm64. Añade al resultado una etiqueta
  `cfg-<sha256 del ID de configuración>`. Verifica el ID real antes
  de desplegar y el `imageID` de los contenedores después del primer Job.
- Instala [mlx_host.py](mlx_host.py) con MLX 0.32.4 en el Python arm64
  de macOS y el bundle con las huellas que exige el servicio. El proceso
  escucha en `127.0.0.1:18443`. El certificado debe incluir
  `host.docker.internal`. Guarda el certificado y el token en
  `web-refresh-mlx`, que solo monta el contenedor `forecast`.
  El LaunchAgent necesita una sesión del usuario de macOS. Comprueba
  también que OrbStack arranque tras volver a iniciar sesión.
- Usa el ConfigMap inmutable de captura `web-refresh-capture-83dd496b`.
  Su revisión de Neblima es `83dd496b1c0d64b9da5928dffd682337808c2da8`.
  Contiene `tools/web-refresh/refresh.py` y
  `tools/timenet-smoke/stage_external_pm.py` con los nombres de archivo
  `refresh.py` y `stage_external_pm.py`. Genera un CronJob suspendido
  con `render.py --target orbstack`. Valida con
  `kubectl --context orbstack apply --dry-run=server` y ejecuta un Job
  manual con publicación desactivada. Solo monta `web-refresh-git`
  en el publicador de producción. La opción `--publish` también
  genera la plantilla suspendida. [Revisión de captura de Neblima](https://github.com/keneth-arias/Neblima/tree/83dd496b1c0d64b9da5928dffd682337808c2da8/tools/web-refresh)
- Comprueba los tres contenedores con `kubectl --context orbstack -n
  airaware-web-refresh logs job/NOMBRE --all-containers --prefix`.
  El PVC `receipts` conserva el recibo, `actual.json` y
  `published-commit.txt`. Compara la huella del archivo publicado con
  el archivo que entrega la web.

### Instalación del LaunchAgent

En m4, usa un Python arm64 con MLX 0.32.4 y una copia verificada del bundle
`mlx-fp32`. La [receta y el manifiesto de exportación](../../qwen-portable/README.md)
identifican ese bundle. Para una instalación nueva, prepara el directorio
de servicio y genera el certificado y el token como archivos privados con
[tls.cnf](tls.cnf). Usa los archivos de la instalación activa para verificar
o reiniciar un despliegue existente.

```sh
set -eu
umask 077
root="$HOME/.local/share/neblima-mlx/hourly"
mkdir -p "$root/bin" "$root/tls" "$root/logs" "$HOME/Library/LaunchAgents"
test ! -e "$root/tls/token"
test ! -e "$root/tls/key.pem"
test ! -e "$root/tls/cert.pem"
cp mlx_host.py "$root/bin/mlx_host.py"
/usr/bin/openssl rand -hex 32 > "$root/tls/token"
/usr/bin/openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
  -keyout "$root/tls/key.pem" -out "$root/tls/cert.pem" -config tls.cnf
```

Define `model` como el directorio absoluto de `mlx-fp32` y `python`
como el ejecutable del entorno arm64. En una primera instalación,
genera y registra la plantilla desde esta carpeta.

```sh
set -eu
umask 077
plist="$HOME/Library/LaunchAgents/lat.orbitly.neblima-mlx.plist"
test ! -e "$plist"
"$python" render_launchagent.py --root "$root" --bundle "$model" --python "$python" > "$plist"
/usr/bin/plutil -lint "$plist"
/bin/launchctl bootstrap "gui/$(id -u)" "$plist"
/bin/launchctl print "gui/$(id -u)/lat.orbitly.neblima-mlx"
```

Para el Secret de inferencia, entrega solo `cert.pem` y `token` al
contexto `orbstack`. El publicador usa el Secret independiente
`web-refresh-git`.

```sh
kubectl --context orbstack -n airaware-web-refresh create secret generic web-refresh-mlx \
  --from-file=cert.pem="$root/tls/cert.pem" --from-file=token="$root/tls/token"
```

## Cambio de publicador y recuperación

Suspende el CronJob del contexto que publica y espera que sus Jobs
terminen antes de reanudar el otro. `Forbid` solo coordina los Jobs
del mismo CronJob. MicroK8s mantiene su plantilla con CUDA y el PVC del
modelo para recuperar el servicio si falla m4. Verifica el contexto,
el commit publicado y la frescura del archivo en la web tras cada cambio.
