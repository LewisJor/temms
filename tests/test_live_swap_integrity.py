from pathlib import Path

import pytest

from temms import InferenceResult, ModelRef

MODELS = (
    ModelRef("mobilenetv2", "sha256:small"),
    ModelRef("resnet18", "sha256:large"),
)
LABELS = [f"class-{index}" for index in range(1000)]


@pytest.fixture
def demo():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import run

    return run


def _scores(demo, marker):
    scores = demo.np.zeros((1, 1000), dtype=demo.np.float32)
    scores[0, 7] = 4
    scores[0, 11] = marker
    return scores


@pytest.fixture
def sample(demo):
    return {
        "image": {"name": "first.jpg", "sha256": "first-image"},
        "expected": {"index": 7, "label": LABELS[7]},
        "feed": {"data": demo.np.array([1], dtype=demo.np.float32)},
        "references": {
            (model.id, model.digest): _scores(demo, 0.25 + index * 0.5)
            for index, model in enumerate(MODELS)
        },
    }


@pytest.mark.parametrize("model", MODELS)
def test_output_matches_the_returned_model_and_preserves_all_scores(demo, sample, model):
    scores = sample["references"][model.id, model.digest].copy()
    checked = demo.check_output(InferenceResult(model, (scores,)), sample, model, LABELS)

    assert checked == {
        "prediction": {"index": 7, "label": LABELS[7]},
        "scores": scores[0].tolist(),
        "reference_match": True,
        "identity_match": True,
        "correct": True,
    }


@pytest.mark.parametrize("substitution", ["other-model", "previous-input"])
def test_same_top_prediction_cannot_hide_wrong_model_or_stale_input(demo, sample, substitution):
    wrong = (sample["references"][MODELS[1].id, MODELS[1].digest].copy()
             if substitution == "other-model" else _scores(demo, 1.25))
    checked = demo.check_output(InferenceResult(MODELS[0], (wrong,)), sample, MODELS[0], LABELS)

    assert checked["prediction"] == {"index": 7, "label": LABELS[7]}
    assert checked["correct"] is True and checked["identity_match"] is True
    assert checked["reference_match"] is False


def test_unknown_serving_digest_cannot_use_another_revision_reference(demo, sample):
    returned = ModelRef(MODELS[0].id, "sha256:unregistered-revision")
    scores = sample["references"][MODELS[0].id, MODELS[0].digest].copy()
    checked = demo.check_output(InferenceResult(returned, (scores,)), sample, MODELS[0], LABELS)

    assert checked["reference_match"] is False
    assert checked["identity_match"] is False


@pytest.mark.parametrize("admitted", [
    MODELS[1],
    ModelRef(MODELS[0].id, "sha256:other-admission"),
])
def test_matching_scores_do_not_excuse_wrong_admitted_identity(demo, sample, admitted):
    scores = sample["references"][MODELS[0].id, MODELS[0].digest].copy()
    checked = demo.check_output(InferenceResult(MODELS[0], (scores,)), sample, admitted, LABELS)

    assert checked["reference_match"] is True
    assert checked["identity_match"] is False


def test_classifier_error_is_separate_from_output_integrity(demo, sample):
    sample["expected"] = {"index": 8, "label": LABELS[8]}
    scores = sample["references"][MODELS[0].id, MODELS[0].digest].copy()
    checked = demo.check_output(InferenceResult(MODELS[0], (scores,)), sample, MODELS[0], LABELS)

    assert checked["correct"] is False
    assert checked["reference_match"] is True and checked["identity_match"] is True


@pytest.mark.parametrize("shape", [(1000,), (1, 1), (1, 999), (2, 1000), (1000, 1)])
def test_output_validation_rejects_shapes_before_numpy_can_broadcast(demo, sample, shape):
    output = (demo.np.zeros(shape, dtype=demo.np.float32),)
    with pytest.raises(ValueError):
        demo.check_output(InferenceResult(MODELS[0], output), sample, MODELS[0], LABELS)


@pytest.mark.parametrize("count", [0, 2])
def test_classifier_requires_exactly_one_output(demo, count):
    with pytest.raises(ValueError):
        demo.classifier_scores(tuple(_scores(demo, 0.25) for _ in range(count)))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_scores_cannot_match_a_reference(demo, sample, value):
    scores = sample["references"][MODELS[0].id, MODELS[0].digest].copy()
    scores[0, 11] = value
    with pytest.raises(ValueError):
        demo.check_output(InferenceResult(MODELS[0], (scores,)), sample, MODELS[0], LABELS)


@pytest.mark.parametrize("index,delta,matches", [
    (12, 0.0000005, True),
    (12, 0.000002, False),
    (7, 0.00002, True),
    (7, 0.0001, False),
])
def test_comparison_uses_declared_absolute_and_relative_tolerance(
    demo, sample, index, delta, matches,
):
    scores = sample["references"][MODELS[0].id, MODELS[0].digest].copy()
    scores[0, index] += delta
    checked = demo.check_output(InferenceResult(MODELS[0], (scores,)), sample, MODELS[0], LABELS)
    assert checked["reference_match"] is matches


@pytest.fixture
def preparation(demo, monkeypatch, tmp_path):
    manifest = {"files": [
        {"name": f"image-{index}.jpg", "sha256": f"image-sha-{index}",
         "expected_index": 7, "expected_synset": "n00000007"}
        for index in (1, 2)
    ]}
    feeds = {
        item["name"]: {"data": demo.np.full((1, 3, 224, 224), index, dtype=demo.np.float32)}
        for index, item in enumerate(manifest["files"], 1)
    }
    state = {"mode": None, "calls": [], "loaded": []}
    buffer = _scores(demo, 0)
    monkeypatch.setattr(demo, "preprocess", lambda path: feeds[path.name])

    class Session:
        def __init__(self, model):
            self.model = model

        def run(self, names, feed):
            image = int(feed["data"][0, 0, 0, 0])
            state["calls"].append((self.model, image))
            marker = 0.25 + self.model * 0.5 + image * 0.1
            if state["mode"] == "input-independent":
                marker = 0.25 + self.model * 0.5
            elif state["mode"] == "model-independent":
                marker = 0.25 + image * 0.1
            elif state["mode"] == "within-tolerance":
                marker = 0.25 + (self.model * 2 + image) * 0.0000001
            buffer[:] = _scores(demo, marker)
            return [buffer]  # Every call reuses the same mutable native-style buffer.

    def factory(path, providers):
        assert tuple(providers) == ("CPUExecutionProvider",)
        model = [ref.id for ref in MODELS].index(Path(path).stem)
        state["loaded"].append(model)
        return Session(model)

    monkeypatch.setattr(demo, "cpu_session", factory)
    return manifest, feeds, state, buffer


def test_preparation_freezes_feeds_and_copies_each_model_input_reference(
    demo, preparation, tmp_path,
):
    manifest, feeds, state, buffer = preparation
    samples = demo.prepare_inputs(tmp_path, manifest, MODELS, LABELS)

    assert set(state["loaded"]) == {0, 1}
    assert sorted(state["calls"]) == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert len(samples) == 2
    buffer.fill(99)
    for image, (sample, item) in enumerate(zip(samples, manifest["files"], strict=True), 1):
        assert sample["image"] == {"name": item["name"], "sha256": item["sha256"]}
        assert sample["expected"] == {"index": 7, "label": LABELS[7]}
        demo.np.testing.assert_array_equal(sample["feed"]["data"], feeds[item["name"]]["data"])
        assert not sample["feed"]["data"].flags.writeable
        with pytest.raises(ValueError):
            sample["feed"]["data"][0, 0, 0, 0] = 99
        assert set(sample["references"]) == {(model.id, model.digest) for model in MODELS}
        for model, ref in enumerate(MODELS):
            scores = sample["references"][ref.id, ref.digest]
            assert not demo.np.shares_memory(scores, buffer) and not scores.flags.writeable
            assert scores[0, 11] == pytest.approx(0.25 + model * 0.5 + image * 0.1)
            with pytest.raises(ValueError):
                scores[0, 11] = 99


@pytest.mark.parametrize("mode", [
    "input-independent", "model-independent", "within-tolerance",
])
def test_preparation_rejects_ambiguous_model_input_references(demo, preparation, tmp_path, mode):
    manifest, _, state, _ = preparation
    state["mode"] = mode
    with pytest.raises(ValueError):
        demo.prepare_inputs(tmp_path, manifest, MODELS, LABELS)
