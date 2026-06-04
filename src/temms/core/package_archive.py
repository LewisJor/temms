"""
Archive support for immutable TEMMS package artifacts.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PACKAGE_ARCHIVE_SUFFIX = ".temms.tar.zst"


def is_package_archive(path: Path) -> bool:
    """Return whether a path is a TEMMS package archive."""
    return path.is_file() and path.name.endswith(PACKAGE_ARCHIVE_SUFFIX)


def default_archive_path(package_dir: Path) -> Path:
    """Return the default archive path for a directory package."""
    name = package_dir.name
    if name.endswith(".temms"):
        name = name.removesuffix(".temms")
    return package_dir.with_name(f"{name}{PACKAGE_ARCHIVE_SUFFIX}")


def create_package_archive(package_dir: Path, output_path: Path | None = None) -> Path:
    """Create a zstd-compressed tar archive from a directory package."""
    if not package_dir.is_dir():
        raise ValueError(f"Package archive source must be a directory: {package_dir}")
    if not (package_dir / "manifest.json").exists():
        raise ValueError(f"Missing manifest.json in package: {package_dir}")

    output_path = output_path or default_archive_path(package_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="temms-archive-") as tmp:
        tar_path = Path(tmp) / f"{package_dir.name}.tar"
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
            for path in _archive_paths(package_dir):
                arcname = _archive_name(package_dir, path)
                info = tar.gettarinfo(str(path), arcname=arcname)
                _normalize_tar_info(info)
                if info.isdir():
                    tar.addfile(info)
                elif info.isfile():
                    with path.open("rb") as file:
                        tar.addfile(info, file)
                else:
                    raise ValueError(
                        "Package archives may contain only regular files or directories: " f"{path}"
                    )
        _compress_zstd(tar_path, output_path)

    return output_path


def sign_package_artifact(package_path: Path, key: str, signer: str = "temms") -> Path:
    """Sign a directory package or replace a signed archive package."""
    from temms.core.signing import sign_package

    if package_path.is_dir():
        return sign_package(package_path, key, signer=signer)

    if not is_package_archive(package_path):
        raise ValueError(f"Unsupported package path: {package_path}")

    with tempfile.TemporaryDirectory(prefix="temms-sign-") as tmp:
        tmp_path = Path(tmp)
        package_dir = extract_package_archive(package_path, tmp_path)
        sign_package(package_dir, key, signer=signer)
        create_package_archive(package_dir, package_path)
    return package_path


def extract_package_archive(archive_path: Path, dest_dir: Path) -> Path:
    """Extract a package archive and return the extracted package directory."""
    if not is_package_archive(archive_path):
        raise ValueError(f"Not a TEMMS package archive: {archive_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="temms-archive-") as tmp:
        tar_path = Path(tmp) / archive_path.name.removesuffix(".zst")
        _decompress_zstd(archive_path, tar_path)
        with tarfile.open(tar_path, "r") as tar:
            members = tar.getmembers()
            _validate_members(members)
            top_level = _single_top_level_dir(members)
            tar.extractall(dest_dir, members=members)

    package_dir = dest_dir / top_level
    if not (package_dir / "manifest.json").exists():
        raise ValueError(f"Archive did not contain a TEMMS package: {archive_path}")
    return package_dir


@contextmanager
def package_directory(path: Path, work_dir: Path | None = None) -> Iterator[Path]:
    """Yield a package directory, extracting archives to a temporary location."""
    if path.is_dir():
        yield path
        return

    if not is_package_archive(path):
        raise ValueError(f"Unsupported package path: {path}")

    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="temms-package-") as tmp:
            yield extract_package_archive(path, Path(tmp))
    else:
        extracted_root = work_dir / path.name.removesuffix(PACKAGE_ARCHIVE_SUFFIX)
        if extracted_root.exists():
            shutil.rmtree(extracted_root)
        extracted_root.mkdir(parents=True, exist_ok=True)
        yield extract_package_archive(path, extracted_root)


def _compress_zstd(source: Path, destination: Path) -> None:
    try:
        import zstandard as zstd

        compressor = zstd.ZstdCompressor(level=10)
        with source.open("rb") as src, destination.open("wb") as dst:
            compressor.copy_stream(src, dst)
        return
    except ImportError:
        pass

    zstd_bin = shutil.which("zstd")
    if not zstd_bin:
        raise RuntimeError("Creating .temms.tar.zst requires zstandard or the zstd CLI")
    subprocess.run(
        [zstd_bin, "-q", "-f", str(source), "-o", str(destination)],
        check=True,
    )


def _decompress_zstd(source: Path, destination: Path) -> None:
    try:
        import zstandard as zstd

        decompressor = zstd.ZstdDecompressor()
        with source.open("rb") as src, destination.open("wb") as dst:
            decompressor.copy_stream(src, dst)
        return
    except ImportError:
        pass

    zstd_bin = shutil.which("zstd")
    if not zstd_bin:
        raise RuntimeError("Reading .temms.tar.zst requires zstandard or the zstd CLI")
    subprocess.run(
        [zstd_bin, "-q", "-d", "-f", str(source), "-o", str(destination)],
        check=True,
    )


def _archive_paths(package_dir: Path) -> list[Path]:
    """Return package paths in deterministic tar order."""
    paths = [package_dir]
    paths.extend(
        sorted(package_dir.rglob("*"), key=lambda path: path.relative_to(package_dir).as_posix())
    )
    return paths


def _archive_name(package_dir: Path, path: Path) -> str:
    """Return the package-relative archive name for a source path."""
    if path == package_dir:
        return package_dir.name
    return f"{package_dir.name}/{path.relative_to(package_dir).as_posix()}"


def _normalize_tar_info(info: tarfile.TarInfo) -> None:
    """Normalize tar metadata so package archives are reproducible."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o111 else 0o644


def _validate_members(members: list[tarfile.TarInfo]) -> None:
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe archive member path: {member.name}")
        if member.issym() or member.islnk():
            raise ValueError(f"Archive links are not allowed: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise ValueError(
                "Archive members must be regular files or directories: " f"{member.name}"
            )


def _single_top_level_dir(members: list[tarfile.TarInfo]) -> str:
    top_levels = {
        Path(member.name).parts[0] for member in members if member.name and Path(member.name).parts
    }
    if len(top_levels) != 1:
        raise ValueError("Package archive must contain exactly one top-level directory")
    top_level = next(iter(top_levels))
    if not top_level.endswith(".temms"):
        raise ValueError("Package archive top-level directory must end with .temms")
    return top_level
