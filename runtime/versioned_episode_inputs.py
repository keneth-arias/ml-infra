"""Read immutable Nebius S3 objects through short-lived, version-specific HTTPS URLs."""

import json
from pathlib import Path, PurePosixPath
import re
import shutil
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class ObjectSet:
    def __init__(self, records, prefix, base, *, exact_keys=False):
        source = urlsplit(prefix)
        if (
            source.scheme != "https"
            or source.netloc != "storage.eu-south1.nebius.cloud"
            or not re.fullmatch(r"/[a-z0-9.-]+/(?:[a-zA-Z0-9_-]+/)+", source.path)
            or source.query
            or source.fragment
        ):
            raise ValueError(
                "Expected the approved eu-south1 HTTPS bucket prefix ending in /"
            )
        self.records = {}
        for record in records:
            name = record["name"]
            path = PurePosixPath(name)
            allowed = (
                (name == "SHA256SUMS" or name.startswith("weights/"))
                if base
                else name in {"manifest.json", "data.npz", "thresholds.json"}
            )
            if (
                not allowed
                or str(path) != name
                or path.is_absolute()
                or name in self.records
                or any(
                    not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]*", part)
                    for part in path.parts
                )
            ):
                raise ValueError("Invalid or duplicate input object path")
            url = urlsplit(record["url"])
            key = unquote(url.path)
            version = record["version_id"]
            if (
                url.scheme != "https"
                or url.netloc != source.netloc
                or url.fragment
                or not key.startswith(source.path)
                or (exact_keys and key != source.path + name)
                or ".." in PurePosixPath(key).parts
                or not isinstance(version, str)
                or not version
                or version == "null"
                or parse_qs(url.query).get("versionId") != [version]
                or type(record["size"]) is not int
                or record["size"] < 0
            ):
                raise ValueError("Object is outside the approved versioned source")
            self.records[name] = record
        if not self.records:
            raise ValueError("Empty input inventory")

    def copy(self, target, check_manifest=None):
        if (
            sum(r["size"] for r in self.records.values())
            > shutil.disk_usage(target.parent).free
        ):
            raise OSError("Insufficient space for the versioned inputs")
        target.mkdir()
        client = build_opener(NoRedirect())
        if check_manifest and "manifest.json" not in self.records:
            raise ValueError("Missing cohort manifest")
        for name in sorted(self.records, key=lambda n: n != "manifest.json"):
            record = self.records[name]
            output = target / name
            output.parent.mkdir(parents=True, exist_ok=True)
            # Avoid logging signed query strings, including on HTTP failures.
            try:
                with (
                    client.open(record["url"], timeout=30) as src,
                    output.open("xb") as dst,
                ):
                    if (
                        src.headers.get("x-amz-version-id") != record["version_id"]
                        or int(src.headers["Content-Length"]) != record["size"]
                    ):
                        raise ValueError("Object metadata differs")
                    remaining = record["size"]
                    while remaining:
                        block = src.read(min(remaining, 1024 * 1024))
                        if not block:
                            raise OSError("Short object")
                        dst.write(block)
                        remaining -= len(block)
                    if src.read(1):
                        raise ValueError("Oversize object")
            except Exception:
                raise OSError(f"Versioned download failed for {name}") from None
            if check_manifest and name == "manifest.json":
                check_manifest(output)


def sources(inventory, prefix, *, paired=False):
    data = json.loads(Path(inventory).read_text())
    if paired:
        if not prefix.endswith("/"):
            raise ValueError("Expected the approved versioned source directory prefix")
        if set(data) != {"base", "bundle", "inner_validation"}:
            raise ValueError(
                "Expected only base, fit and inner-validation object inventories"
            )
        return (
            ObjectSet(data["base"], prefix + "base/", True, exact_keys=True),
            ObjectSet(data["bundle"], prefix + "trainer/fit/", False, exact_keys=True),
            ObjectSet(
                data["inner_validation"],
                prefix + "trainer/inner_validation/",
                False,
                exact_keys=True,
            ),
        )
    return ObjectSet(data["base"], prefix, True), ObjectSet(
        data["bundle"], prefix, False
    )
