import io
import tarfile
import unittest
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from queryflow.release_build import normalize_sdist_bytes, normalize_wheel_bytes


def _wheel_bytes(
    timestamp: tuple[int, int, int, int, int, int], reverse: bool
) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        names = ["pkg/__init__.py", "pkg-0.2.0.dist-info/WHEEL"]
        if reverse:
            names.reverse()
        for name in names:
            entry = ZipInfo(name, date_time=timestamp)
            entry.compress_type = ZIP_STORED
            archive.writestr(entry, b"same bytes\n")
    return output.getvalue()


def _sdist_bytes(timestamp: int, reverse: bool) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        names = [
            "queryflow-0.2.0b1/README.md",
            "queryflow-0.2.0b1/queryflow/__init__.py",
        ]
        if reverse:
            names.reverse()
        for name in names:
            entry = tarfile.TarInfo(name)
            entry.size = len(b"same bytes\n")
            entry.mtime = timestamp
            archive.addfile(entry, io.BytesIO(b"same bytes\n"))
    return output.getvalue()


class ReleaseBuildTests(unittest.TestCase):
    def test_wheel_normalization_ignores_zip_order_and_timestamps(self):
        first = normalize_wheel_bytes(
            _wheel_bytes((2026, 8, 10, 17, 0, 0), reverse=False)
        )
        second = normalize_wheel_bytes(
            _wheel_bytes((2026, 8, 10, 17, 30, 0), reverse=True)
        )

        self.assertEqual(first, second)

    def test_sdist_normalization_ignores_tar_order_and_timestamps(self):
        first = normalize_sdist_bytes(_sdist_bytes(1_700_000_000, reverse=False))
        second = normalize_sdist_bytes(_sdist_bytes(1_800_000_000, reverse=True))

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
