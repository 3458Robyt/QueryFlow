"""Reproducible release archive helpers.

The build back-end puts filesystem timestamps into the intermediate archives.
Normalize those metadata fields before publishing so a local build and the CI
build produce byte-for-byte identical artifacts from the same source tree.
"""

from __future__ import annotations

import gzip
import io
import subprocess
import tarfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo


def normalize_wheel_bytes(payload: bytes) -> bytes:
    """Return a wheel with stable ordering, timestamps, and file metadata."""

    with ZipFile(io.BytesIO(payload), "r") as source:
        entries = [
            (info.filename, source.read(info.filename), info.is_dir())
            for info in source.infolist()
        ]

    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as target:
        for name, content, is_directory in sorted(entries):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.create_system = 3
            info.external_attr = (0o40755 if is_directory else 0o100644) << 16
            info.extra = b""
            info.comment = b""
            target.writestr(info, content)
    return output.getvalue()


def normalize_sdist_bytes(payload: bytes) -> bytes:
    """Return a source distribution with stable tar and gzip metadata."""

    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as source:
        for member in sorted(source.getmembers(), key=lambda item: item.name):
            content: bytes | None = None
            if member.isfile():
                extracted = source.extractfile(member)
                if extracted is None:
                    raise ValueError(
                        f"No se pudo leer el miembro del sdist: {member.name}"
                    )
                content = extracted.read()
            normalized = tarfile.TarInfo(member.name)
            normalized.type = member.type
            normalized.mode = 0o755 if member.isdir() else 0o644
            if member.issym() or member.islnk():
                normalized.mode = 0o777
            normalized.uid = 0
            normalized.gid = 0
            normalized.uname = ""
            normalized.gname = ""
            normalized.mtime = 0
            normalized.linkname = member.linkname
            normalized.pax_headers = {}
            normalized.size = len(content) if content is not None else 0
            members.append((normalized, content))

    tar_payload = io.BytesIO()
    with tarfile.open(
        fileobj=tar_payload, mode="w", format=tarfile.PAX_FORMAT
    ) as target:
        for member, content in members:
            target.addfile(member, io.BytesIO(content) if content is not None else None)

    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        compressed.write(tar_payload.getvalue())
    return output.getvalue()


def build_release(root: Path, out_dir: Path) -> tuple[Path, Path, Path]:
    """Build, normalize, and checksum the versioned release artifacts."""

    root = root.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["uv", "build", "--out-dir", str(out_dir)], cwd=root, check=True)

    from . import __version__

    prefix = f"queryflow_gcp-{__version__}"
    sdist = out_dir / f"{prefix}.tar.gz"
    wheel = out_dir / f"{prefix}-py3-none-any.whl"
    if not sdist.is_file() or not wheel.is_file():
        raise FileNotFoundError("uv build no produjo los dos artefactos esperados")
    sdist.write_bytes(normalize_sdist_bytes(sdist.read_bytes()))
    wheel.write_bytes(normalize_wheel_bytes(wheel.read_bytes()))

    checksum = out_dir / "SHA256SUMS"
    import hashlib

    lines = []
    for artifact in (sdist, wheel):
        lines.append(
            f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}"
        )
    checksum.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sdist, wheel, checksum
