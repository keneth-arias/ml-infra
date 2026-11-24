# Recuperación del pronóstico Qwen en bm

El CronJob `airaware-hourly-forecast` está suspendido en el contexto
`microk8s`, nodo `bm` y namespace restringido `airaware-web-refresh`.
El [publicador activo](README.orbstack.md) corre en OrbStack. La plantilla
de recuperación de bm usa imágenes locales
fijadas por digest y una GPU para Qwen3.5-0.8B con LoRA. El checkpoint de
semilla 27 y paso 15 745 corresponde a la variante conjunta PM2.5/PM10
con solo historial de PM2.5 usada en los archivos de OpenAQ y SENAMHI.

Cada hora en `America/Lima`, el Job captura hasta doce gráficos de SENAMHI
mediante Lightpanda. Guarda los originales y recibos en el PVC local
`receipts`. Qwen usa solo historias completas de 24 valores, con un
instrumento por historia. La inferencia usa el modo `first` del ejecutor
histórico, con el canal PM10 enmascarado y el escalador original. El
exportador coteja el recibo, las historias y
los seis horizontes antes de agregar las predicciones al archivo capturado.
Conserva las lecturas observadas y deja los pronósticos de historias
incompletas en `null`. El dato horario contiene los seis pronósticos
puntuales de PM2.5. Las gráficas muestran la cobertura de v3 como
resultado retrospectivo de los archivos.

El publicador valida el modelo, el checkpoint y el orden cronológico antes
de cambiar únicamente `public/datos/actual.json` en `qriqs/neblima-web`. Si
todas las observaciones llevan más de seis horas de retraso o faltan todas
las historias completas, se conserva el archivo público. La publicación usa
un push ordinario a `main` y GitHub Actions despliega a Vercel. Solo ese
contenedor lee el Secret existente `web-refresh-git`. Se conservan 14 días de
recibos tras las publicaciones exitosas.

## Preparación y actualización

Confirma que el nodo bm esté Ready y disponga de GPU asignable. Arranca con
el PVC local `qwen-08b-release` de 3 GiB, clase `airaware-hostpath-retain`.
Tras enlazarlo a bm, copia los pesos autorizados y su archivo `SHA256SUMS`
en la raíz, con el directorio `step-00015745/` y `scaler-15745.json`
al lado de `weights/`. El Job monta el volumen de solo lectura.
Verifica estas tres huellas:

| Archivo | SHA-256 |
| --- | --- |
| `SHA256SUMS` | `1d76cc3cfd4c15f9c749372611e5ca6e0b6f910b1fcf0551ce9157e9bfef29c1` |
| `step-00015745/manifest.json` | `ca2ee75b0702a7893bc76a196351a45900fd54b68347f724157a532062d5c0cf` |
| `scaler-15745.json` | `c8ebd87bce74ea0157e85093e4c9c81d981af34fb62f1cbc2c12ce653b2c3863` |

Instala el ConfigMap inmutable `web-refresh-capture-83dd496b` con
`tools/web-refresh/refresh.py` y `tools/timenet-smoke/stage_external_pm.py`
de AirAware `83dd496b1c0d64b9da5928dffd682337808c2da8`. Usa las imágenes
y los digests del manifiesto. El ConfigMap incorpora `gpu_worker.py` de
la revisión `a714eacdf4ed7de21498549bec904e27a2174dbe` para conservar
el límite del asignador CUDA del ensayo original. El publicador conserva el UID 1000 y el
cliente SSH de este directorio. Pesos, capturas originales y secretos se
guardan fuera de Git.

Primero genera la variante sin credenciales de publicación:

```sh
k() { kubectl --context microk8s -n airaware-web-refresh "$@"; }
k get nodes -o wide
k describe node bm
python3 workloads/web-refresh/render.py \
  --publisher-image docker.io/local/airaware-web-publisher@sha256:84257a1221804f8351be35e687fa1a90d9106ebeea8b4191ffe7ea99ca65ce33 \
  --airaware-revision 83dd496b1c0d64b9da5928dffd682337808c2da8 \
  --capture-config web-refresh-capture-83dd496b --suspend > qwen-preview.json
```

Instala el ConfigMap generado y crea un Job de prueba a partir de
`spec.jobTemplate.spec` del CronJob. Verifica el archivo `actual.json`
en el recibo y que el lector de `main` acepte ese modelo y checkpoint.
Genera la variante de publicación con los mismos argumentos y `--publish`.
Comprueba el dry run del servidor antes
de actualizar el CronJob. `Forbid` controla el solapamiento de las
ejecuciones programadas. Espera que no quede otro publicador activo al
cambiar la plantilla.

## Operación

Consulta los logs de los contenedores `capture`, `forecast` y `publish`
con `k logs job/NOMBRE -c CONTENEDOR`. Para pausar el CronJob, aplica
`{"spec":{"suspend":true}}` con un parche merge. Una pausa deja terminar
los Jobs activos. Para restaurar una versión previa, pausa el publicador,
restaura su plantilla guardada y revierte el commit de datos. El sitio
acepta los checkpoints de recuperación verificados. Si bm no tiene GPU disponible, el
Job permanece pendiente hasta su plazo normal.
