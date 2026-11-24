"""Ejecutar y verificar el pronóstico puntual del candidato Qwen congelado."""

import hashlib
import http.client
import json
import ssl
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

MODEL = "Qwen3.5-0.8B + LoRA"
CHECKPOINT = "ca2ee75b0702a7893bc76a196351a45900fd54b68347f724157a532062d5c0cf"
BASE = "1d76cc3cfd4c15f9c749372611e5ca6e0b6f910b1fcf0551ce9157e9bfef29c1"
SCALER = "c8ebd87bce74ea0157e85093e4c9c81d981af34fb62f1cbc2c12ce653b2c3863"
MLX_WEIGHTS = "9a29ea6d5608d1ce9bc47b0bbaaaf316319988fe3091cab30b40148cb105be43"
CHANNELS = ("pm25", "pm10")
HORIZONS = np.arange(1, 7)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_joint_history(history, target):
    with np.load(history, allow_pickle=False) as data:
        if set(data.files) != {"window_id", "history"}:
            raise ValueError("La captura debe contener solo identificadores e historiales")
        ids, values = data["window_id"], data["history"]
    if values.shape != (len(ids), 24) or not np.isfinite(values).all():
        raise ValueError("Se requieren 24 valores finitos de PM2.5 por ventana")
    # El modo first enmascara el canal de PM10 en el ejecutor histórico.
    joint = np.stack((values, np.zeros_like(values)), axis=-1)
    np.savez(target, window_id=ids, history=joint, channels=CHANNELS)
    return ids


def predict(root):
    history = root / "history.npz"
    output = root / "predictions.npz"
    step = Path("/model/step-00015745")
    scaler = Path("/model/scaler-15745.json")
    if (digest(step / "manifest.json") != CHECKPOINT
            or digest(Path("/model/SHA256SUMS")) != BASE or digest(scaler) != SCALER):
        raise ValueError("El volumen no contiene el checkpoint, los pesos y el escalador congelados")
    history_sha256 = digest(history)
    joint = root / "joint-history.npz"
    ids = write_joint_history(history, joint)
    if len(ids):
        subprocess.run(
            [
                sys.executable, "-u", str(Path(__file__).with_name("gpu_worker.py")),
                "-m", "opentslm.forecast_inference",
                "--input-view", "first", "--scaler", str(scaler), "--batch", "16",
                "--weights", "/model/weights", "--checkpoint", str(step),
                "--data", str(joint), "--output", str(output),
            ],
            check=True,
        )
    else:
        np.savez(output, window_id=ids, predicted=np.empty((0, 6, 2)),
                 forecast_hour=HORIZONS, checkpoint_sha256=CHECKPOINT, channels=CHANNELS,
                 view="first", scaler_sha256=SCALER)
    if digest(history) != history_sha256:
        raise ValueError("Las historias cambiaron durante la inferencia")
    (root / "qwen-receipt.json").write_text(json.dumps({
        "history_sha256": history_sha256,
        "output_sha256": digest(output),
    }))


def mlx_request(request):
    credentials = Path("/mlx-credentials")
    context = ssl.create_default_context(cafile=str(credentials / "cert.pem"))
    token = (credentials / "token").read_text().strip()
    connection = http.client.HTTPSConnection("host.docker.internal", 18443, timeout=90, context=context)
    try:
        connection.request("POST", "/v1/forecast", json.dumps(request, allow_nan=False).encode(),
                           {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"El servicio MLX respondió con HTTP {response.status}")
        content = response.read(131073)
        if len(content) > 131072:
            raise ValueError("Respuesta MLX demasiado grande")
        return json.loads(content)
    finally:
        connection.close()


def predict_mlx(root):
    history = root / "history.npz"
    history_sha256 = digest(history)
    output = root / "predictions.npz"
    with np.load(history, allow_pickle=False) as data:
        if set(data.files) != {"window_id", "history"}:
            raise ValueError("La captura debe contener solo identificadores e historiales")
        ids, values = data["window_id"], data["history"]
    if (ids.ndim != 1 or ids.dtype.kind != "U" or len(ids) > 56
            or len(np.unique(ids)) != len(ids) or values.shape != (len(ids), 24)
            or not np.isfinite(values).all()):
        raise ValueError("Se requieren hasta 56 historiales de PM2.5 con identificadores únicos")
    identity = {"checkpoint_sha256": CHECKPOINT, "weights_sha256": MLX_WEIGHTS,
                "history_sha256": history_sha256, "window_ids": ids.tolist()}
    if len(ids):
        response = mlx_request({**identity, "histories": values.tolist()})
        if set(response) != identity.keys() | {"forecast"} or any(
                response[key] != value for key, value in identity.items()):
            raise ValueError("El servicio MLX no verificó la identidad de la solicitud")
        predicted = np.asarray(response["forecast"], dtype="float32")
    else:
        predicted = np.empty((0, 6), dtype="float32")
    if predicted.shape != (len(ids), 6) or not np.isfinite(predicted).all() or digest(history) != history_sha256:
        raise ValueError("La respuesta MLX o el historial no son válidos")
    np.savez(output, window_id=ids, predicted=predicted, forecast_hour=HORIZONS,
             checkpoint_sha256=CHECKPOINT, view="first", scaler_sha256=SCALER, channels=["pm25"])
    (root / "qwen-receipt.json").write_text(json.dumps({
        "history_sha256": history_sha256, "output_sha256": digest(output),
        "backend": "mlx-metal", "weights_sha256": MLX_WEIGHTS,
    }))
    print(f"MLX Metal verificó {len(ids)} ventanas, pesos {MLX_WEIGHTS[:12]}", flush=True)


def export(root):
    snapshot = json.loads((root / "capture.json").read_bytes())
    history_path = root / "history.npz"
    output = root / "predictions.npz"
    receipt = json.loads((root / "qwen-receipt.json").read_bytes())
    hashes = {"history_sha256": digest(history_path), "output_sha256": digest(output)}
    mlx = receipt == {**hashes, "backend": "mlx-metal", "weights_sha256": MLX_WEIGHTS}
    if receipt != hashes and not mlx:
        raise ValueError("Las historias o las predicciones difieren del recibo de inferencia")

    eligible = {}
    for station, record in snapshot["stations"].items():
        for window in record["windows"]:
            values = window["history"]
            if len(values) == 24 and all(value is not None for value in values):
                eligible[station + "/" + window["origin"]] = window
    with np.load(history_path, allow_pickle=False) as data:
        if not np.array_equal(data["window_id"], list(eligible)) or not np.array_equal(
                data["history"], np.asarray([w["history"] for w in eligible.values()],
                                            dtype="float32").reshape(-1, 24)):
            raise ValueError("Las historias no coinciden con la captura")

    with np.load(output, allow_pickle=False) as data:
        predicted = data["predicted"]
        if (data["checkpoint_sha256"].item() != CHECKPOINT
                or data["view"].item() != "first"
                or data["scaler_sha256"].item() != SCALER
                or tuple(data["channels"].tolist()) != (("pm25",) if mlx else CHANNELS)
                or not np.array_equal(data["window_id"], list(eligible))
                or not np.array_equal(data["forecast_hour"], HORIZONS)
                or predicted.shape != ((len(eligible), 6) if mlx else (len(eligible), 6, 2))
                or not np.isfinite(predicted).all()):
            raise ValueError("Predicciones incompatibles con Qwen o sus seis horizontes")
    forecasts = predicted if mlx else predicted[:, :, 0]
    for window, values in zip(eligible.values(), forecasts.tolist(), strict=True):
        window["forecast"] = values
    snapshot["model"] = {
        "name": MODEL, "checkpoint_sha256": CHECKPOINT,
        "computed_at_utc": datetime.now(UTC).isoformat(),
    }
    (root / "actual.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    print(f"Exportados {len(eligible)} pronósticos Qwen de seis horas", flush=True)


if __name__ == "__main__":
    command, directory = sys.argv[1:]
    root = Path(directory)
    if not (root / "skip").exists():
        {"predict": predict, "predict-mlx": predict_mlx, "export": export}[command](root)
