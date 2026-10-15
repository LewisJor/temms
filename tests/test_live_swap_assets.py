import hashlib
import io
import json

import pytest

from examples.live_swap import assets


@pytest.fixture
def asset_manifest(tmp_path, monkeypatch):
    records = [
        {"name": name, "url": f"https://example.invalid/{name}", "bytes": len(content),
         "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in (("first.bin", b"first"), ("second.bin", b"second"))
    ]
    manifest = {"files": records, "models": []}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(assets, "MANIFEST", path)
    return manifest


@pytest.mark.parametrize("contents", [None, b"wrong", b"longer than first"])
def test_verify_assets_rejects_missing_or_corrupt_without_network(
    tmp_path, monkeypatch, asset_manifest, contents,
):
    monkeypatch.setattr(assets, "urlopen", lambda *a, **k: pytest.fail("unexpected download"))
    if contents is not None:
        (tmp_path / "first.bin").write_bytes(contents)
    (tmp_path / "second.bin").write_bytes(b"second")

    message = r"first.bin.*python examples/live_swap/assets.py --assets"
    with pytest.raises(ValueError, match=message):
        assets.verify_assets(tmp_path)


def test_prepare_skips_verified_and_publishes_download(tmp_path, monkeypatch, asset_manifest):
    (tmp_path / "first.bin").write_bytes(b"first")
    requested = []

    def download(url, timeout):
        requested.append(url)
        return io.BytesIO(b"second")

    monkeypatch.setattr(assets, "urlopen", download)
    assert assets.prepare(tmp_path) == asset_manifest
    assert requested == ["https://example.invalid/second.bin"]
    assert (tmp_path / "first.bin").read_bytes() == b"first"
    assert (tmp_path / "second.bin").read_bytes() == b"second"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("failure", ["interrupted", "corrupt", "oversized"])
def test_failed_prepare_preserves_files_and_retry_recovers_offline(
    tmp_path, monkeypatch, asset_manifest, failure,
):
    (tmp_path / "first.bin").write_bytes(b"first")
    (tmp_path / "second.bin").write_bytes(b"previous contents")
    requested = []

    class FirstDownload(io.BytesIO):
        def read(self, size):
            assert 0 < size <= 1024 * 1024
            if failure == "interrupted" and self.tell():
                raise OSError("connection interrupted")
            return super().read(min(size, 2))

    response = FirstDownload({
        "interrupted": b"second", "corrupt": b"broken", "oversized": b"too many bytes",
    }[failure])

    def download(url, timeout):
        requested.append(url)
        return response

    monkeypatch.setattr(assets, "urlopen", download)
    error = OSError if failure == "interrupted" else ValueError
    with pytest.raises(error):
        assets.prepare(tmp_path)

    missing_url = asset_manifest["files"][1]["url"]
    assert requested == [missing_url]
    assert (tmp_path / "first.bin").read_bytes() == b"first"
    assert (tmp_path / "second.bin").read_bytes() == b"previous contents"
    assert not list(tmp_path.glob("*.part"))

    response = io.BytesIO(b"second")
    assert assets.prepare(tmp_path) == asset_manifest
    assert requested == [missing_url, missing_url]
    assert (tmp_path / "first.bin").read_bytes() == b"first"
    assert (tmp_path / "second.bin").read_bytes() == b"second"
    assert not list(tmp_path.glob("*.part"))

    monkeypatch.setattr(assets, "urlopen", lambda *a, **k: pytest.fail("unexpected download"))
    assert assets.prepare(tmp_path) == asset_manifest
    assert assets.verify_assets(tmp_path) == asset_manifest


@pytest.mark.parametrize("size", [(400, 200), (200, 400)])
def test_preprocess_produces_known_rgb_tensor(tmp_path, size):
    np = pytest.importorskip("numpy")
    image = pytest.importorskip("PIL.Image")
    path = tmp_path / "color.png"
    image.new("RGB", size, (255, 128, 0)).save(path)

    result = assets.preprocess(path)

    assert list(result) == ["data"]
    values = result["data"]
    assert values.shape == (1, 3, 224, 224)
    assert values.dtype == np.float32
    assert values.flags.c_contiguous
    expected = (np.array([1, 128 / 255, 0]) - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
    np.testing.assert_allclose(values, np.broadcast_to(expected[None, :, None, None], values.shape),
                               rtol=1e-6, atol=1e-6)
