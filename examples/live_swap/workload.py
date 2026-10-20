"""Explicitly prepare the predeclared Oxford-IIIT Pet comparison workload."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

if __package__:
    from . import assets
else:
    import assets

MANIFEST = Path(__file__).with_name("workload.json")
VALIDATION_MANIFEST = Path(__file__).with_name("validation.json")
BUNDLED_INPUTS = Path(__file__).with_name("inputs")


def _manifest(name: str = "small") -> dict:
    path = {"small": MANIFEST, "validation": VALIDATION_MANIFEST}[name]
    return json.loads(path.read_text(encoding="utf-8"))


def verify_workload(directory: Path, name: str = "small") -> dict:
    """Verify pinned image bytes offline, retaining predeclared ground truth."""
    return assets.verify_assets(directory, _manifest(name))


def _page(manifest: dict, offset: int) -> tuple[dict, list]:
    query = urlencode({
        "dataset": manifest["dataset"], "config": manifest["config"],
        "split": manifest["split"], "offset": offset, "length": 100,
    })
    with urlopen(f"https://datasets-server.huggingface.co/rows?{query}", timeout=30) as response:
        if response.headers.get("x-revision") != manifest["revision"]:
            raise ValueError("Dataset revision changed; refusing to replace the pinned workload")
        page = json.load(response)
    names = next(feature["type"]["names"] for feature in page["features"]
                 if feature["name"] == "label")
    return {item["row_idx"]: item["row"] for item in page["rows"]}, names


def _resolve(record: dict, rows: dict, names: list) -> str:
    row = rows.get(record["source_row"], {})
    label = row.get("label")
    if (row.get("image_id") != record["source_image_id"]
            or label != record["source_label"]
            or not isinstance(label, int) or not 0 <= label < len(names)
            or names[label] != record["source_label_name"]):
        raise ValueError(f"Source annotation changed for {record['name']}; refusing download")
    return row["image"]["src"]


def prepare_workload(directory: Path, name: str = "small") -> dict:
    """Copy bundled inputs or resolve checked validation URLs, then verify bytes."""
    manifest = _manifest(name)
    print(f"Frozen selection: {manifest['selection_rule']}", flush=True)
    resolved = {**manifest, "files": [dict(record) for record in manifest["files"]]}
    pages = {}
    for record in resolved["files"]:
        if assets._matches(directory / record["name"], record):
            continue
        if name == "small":
            source = BUNDLED_INPUTS / record["name"]
            if not assets._matches(source, record):
                raise ValueError(
                    f"Missing or corrupt bundled workload image: {source}. "
                    "Restore examples/live_swap/inputs from this checkout."
                )
            record["url"] = source.resolve().as_uri()
            continue
        offset = record["source_row"] // 100 * 100
        if offset not in pages:
            pages[offset] = _page(manifest, offset)
        record["url"] = _resolve(record, *pages[offset])
    assets.prepare(directory, resolved)
    return verify_workload(directory, name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=assets.DEFAULT_ASSETS)
    parser.add_argument("--workload", choices=("small", "validation"), default="small")
    arguments = parser.parse_args()
    try:
        manifest = prepare_workload(arguments.assets, arguments.workload)
    except (OSError, ValueError, KeyError, StopIteration) as error:
        parser.exit(1, f"Workload preparation failed: {error}\n")
    print(f"Verified {len(manifest['files'])} workload images in {arguments.assets}")


if __name__ == "__main__":
    main()
