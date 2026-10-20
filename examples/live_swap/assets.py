"""Explicitly prepare pinned demo assets; runtime verification never downloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import tempfile
from pathlib import Path
from urllib.request import urlopen

MANIFEST = Path(__file__).with_name("assets.json")
DEFAULT_ASSETS = Path(".demo-assets/live-swap")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _matches(path: Path, record: dict) -> bool:
    try:
        if path.stat().st_size != record["bytes"]:
            return False
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        return digest == record["sha256"]
    except OSError:
        return False


def verify_assets(directory: Path, manifest: dict | None = None) -> dict:
    """Check every pinned size and digest without contacting the network."""
    manifest = _manifest() if manifest is None else manifest
    invalid = [
        record["name"] for record in manifest["files"]
        if not _matches(directory / record["name"], record)
    ]
    if invalid:
        script = manifest.get("prepare_script", "examples/live_swap/assets.py")
        command = f"python {script} --assets {shlex.quote(str(directory))}"
        raise ValueError(f"Missing or corrupt assets: {', '.join(invalid)}. Run: {command}")
    return manifest


def prepare(directory: Path, manifest: dict | None = None) -> dict:
    """Download missing/corrupt files, publishing only complete verified assets."""
    manifest = _manifest() if manifest is None else manifest
    directory.mkdir(parents=True, exist_ok=True)
    for record in manifest["files"]:
        destination = directory / record["name"]
        if _matches(destination, record):
            print(f"Verified {record['name']}", flush=True)
            continue
        print(f"Downloading {record['name']} ({record['bytes']:,} bytes)", flush=True)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory, prefix=f".{record['name']}.", suffix=".part", delete=False,
            ) as output:
                staged = Path(output.name)
                received = 0
                with urlopen(record["url"], timeout=30) as response:
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if received > record["bytes"]:
                            raise ValueError(
                                f"{record['name']} exceeds its pinned size; retry prepare"
                            )
                        output.write(chunk)
            if not _matches(staged, record):
                raise ValueError(f"{record['name']} download failed integrity check; retry prepare")
            staged.replace(destination)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
    return verify_assets(directory, manifest)


def preprocess(path: Path) -> dict:
    """Produce the shared ImageNet feed using the pinned ONNX Zoo preprocessing."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    ratio = 256 / min(width, height)
    size = (round(width * ratio), 256) if width > height else (256, round(height * ratio))
    image = image.resize(size, Image.Resampling.BILINEAR)
    left, top = image.width // 2 - 112, image.height // 2 - 112
    image = image.crop((left, top, left + 224, top + 224))
    pixels = np.asarray(image, dtype=np.float32) / 255
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    normalized = (pixels - mean) / std
    return {"data": np.ascontiguousarray(normalized.transpose(2, 0, 1)[None])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    arguments = parser.parse_args()
    try:
        prepare(arguments.assets)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Preparation failed: {error}\n")
    print(f"All demo assets verified in {arguments.assets}")


if __name__ == "__main__":
    main()
