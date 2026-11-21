"""Decode exact Nebius Compute API disk sizes for storage guards."""

import re


GIB = 1024**3
SIZE_UNITS = {"size_bytes": 1, "size_kibibytes": 1024, "size_mebibytes": 1024**2, "size_gibibytes": GIB}


def positive_int64(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Expected a positive integer Nebius disk size")
    return value


def disk_size_gib(disk):
    spec = disk["spec"]
    fields = [field for field in SIZE_UNITS if field in spec]
    if len(fields) != 1:
        raise ValueError("Expected exactly one Nebius disk spec size unit")
    field, = fields
    size_bytes = positive_int64(spec[field]) * SIZE_UNITS[field]
    size_gib, remainder = divmod(size_bytes, GIB)
    if remainder:
        raise ValueError("Nebius disk size must be an exact whole GiB")
    status = disk.get("status", {})
    if "size_bytes" in status and positive_int64(status["size_bytes"]) != size_bytes:
        raise ValueError("Nebius disk spec size differs from its observed size")
    return size_gib
