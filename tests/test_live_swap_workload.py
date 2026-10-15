import hashlib
import io
import json

import pytest

from examples.live_swap import assets, workload


@pytest.fixture
def workload_manifest(tmp_path, monkeypatch):
    manifest = {
        "dataset": "test/pets", "revision": "pinned", "config": "default", "split": "test",
        "selection_rule": "two predetermined test rows",
        "prepare_script": "examples/live_swap/workload.py",
        "files": [
            {"name": f"pet-{name}.jpg", "source_row": row, "source_image_id": name,
             "source_label": label, "source_label_name": breed, "expected_synset": synset,
             "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, row, label, breed, synset, content in (
                ("beagle_1", 3, 0, "beagle", "n02088364", b"first"),
                ("boxer_2", 5, 1, "boxer", "n02108089", b"second"),
            )
        ],
    }
    path = tmp_path / "workload.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(workload, "MANIFEST", path)
    return manifest


def _metadata(manifest):
    return {
        "features": [{"name": "label", "type": {"names": ["beagle", "boxer"]}}],
        "rows": [{"row_idx": record["source_row"], "row": {
            "label": record["source_label"], "image_id": record["source_image_id"],
            "image": {"src": f"https://example.invalid/{record['name']}?signature=fresh"},
        }} for record in manifest["files"]],
    }


def test_workload_resolves_checked_rows_and_preserves_manifest(
    tmp_path, monkeypatch, workload_manifest,
):
    metadata_requests, image_requests = [], []

    def metadata(url, timeout):
        metadata_requests.append(url)
        response = io.BytesIO(json.dumps(_metadata(workload_manifest)).encode())
        response.headers = {"x-revision": "pinned"}
        return response

    def download(url, timeout):
        image_requests.append(url)
        return io.BytesIO(b"first" if "beagle" in url else b"second")

    monkeypatch.setattr(workload, "urlopen", metadata)
    monkeypatch.setattr(assets, "urlopen", download)
    assert workload.prepare_workload(tmp_path) == workload_manifest
    assert len(metadata_requests) == 1
    assert "offset=0&length=100" in metadata_requests[0]
    assert len(image_requests) == 2
    assert all("signature=fresh" in url for url in image_requests)
    assert json.loads(workload.MANIFEST.read_text()) == workload_manifest
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("drift", ["revision", "image_id", "label", "label_name", "row"])
def test_workload_rejects_source_drift_before_any_image_download(
    tmp_path, monkeypatch, workload_manifest, drift,
):
    page = _metadata(workload_manifest)
    headers = {"x-revision": "pinned"}
    if drift == "revision":
        headers["x-revision"] = "new-revision"
    elif drift == "label_name":
        page["features"][0]["type"]["names"][1] = "another-breed"
    elif drift == "row":
        page["rows"][1]["row_idx"] = 99
    else:
        page["rows"][1]["row"][drift] = "changed" if drift == "image_id" else 0
    response = io.BytesIO(json.dumps(page).encode())
    response.headers = headers
    monkeypatch.setattr(workload, "urlopen", lambda *a, **k: response)
    monkeypatch.setattr(assets, "urlopen", lambda *a, **k: pytest.fail("unexpected download"))

    with pytest.raises(ValueError, match="changed"):
        workload.prepare_workload(tmp_path)
    assert not list(tmp_path.glob("pet-*.jpg"))


def test_prepared_workload_verifies_and_prepares_without_network(
    tmp_path, monkeypatch, workload_manifest,
):
    (tmp_path / "pet-beagle_1.jpg").write_bytes(b"first")
    (tmp_path / "pet-boxer_2.jpg").write_bytes(b"second")
    for module in (assets, workload):
        monkeypatch.setattr(module, "urlopen", lambda *a, **k: pytest.fail("unexpected network"))

    assert workload.verify_workload(tmp_path) == workload_manifest
    assert workload.prepare_workload(tmp_path) == workload_manifest


def test_workload_missing_or_corrupt_error_gives_workload_command(
    tmp_path, monkeypatch, workload_manifest,
):
    (tmp_path / "pet-beagle_1.jpg").write_bytes(b"wrong")
    for module in (assets, workload):
        monkeypatch.setattr(module, "urlopen", lambda *a, **k: pytest.fail("unexpected network"))

    with pytest.raises(ValueError, match=r"pet-beagle_1.*pet-boxer_2.*workload.py --assets"):
        workload.verify_workload(tmp_path)
