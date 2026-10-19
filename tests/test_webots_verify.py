import copy
import json

import pytest

SMALL = {"id": "mobilenetv2", "digest": "small-digest"}
LARGE = {"id": "resnet18", "digest": "large-digest"}


@pytest.fixture
def audit():
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from examples.webots import verify

    return verify


def _scene(previous, selected, battery, activation):
    active = previous if activation in ("failed", "unchanged") else selected
    common = {"selected": selected, "battery": battery}
    event = common | {"event": "activation", "activation": activation, "active": active}
    event.update({"error": "missing private artifact"} if activation == "failed"
                 else {"decision_selected": selected})
    frames = [common | {
        "event": "frame", "active": active, "activation": activation,
        "outcome": "ok" if selected else "abstained", "serving": active if selected else None,
        "admitted": active, "activation_at_admission": activation,
    } for _ in range(12)]
    return [common | {"event": "stage", "active": previous}, event, *frames]


def test_scene_accepts_old_admission_that_completes_after_publication(audit):
    records = _scene(SMALL, LARGE, 80, "published")
    records[2].update(serving=SMALL, admitted=SMALL, activation_at_admission="pending")
    assert records[2]["active"] == LARGE
    assert audit.verify_scene(records, SMALL, LARGE, "published") == LARGE


@pytest.mark.parametrize("change", [
    "late-activation", "wrong-decision", "impossible-admission", "waiting", "abstained",
    "lost-active", "missing-witness",
])
def test_scene_rejects_forged_activation_and_admission_evidence(audit, change):
    records = _scene(SMALL, LARGE, 80, "published")
    mutations = {
        "late-activation": lambda: records.append(records.pop(1)),
        "wrong-decision": lambda: records[1].update(decision_selected=None),
        "impossible-admission": lambda: records[2].update(activation_at_admission="pending"),
        "waiting": lambda: records[2].update(outcome="waiting", serving=None),
        "abstained": lambda: records[2].update(outcome="abstained", serving=None),
        "lost-active": lambda: records[2].update(active=None),
        "missing-witness": lambda: [record.update(
            serving=SMALL, admitted=SMALL, activation_at_admission="pending",
        ) for record in records[2:]],
    }
    mutations[change]()
    with pytest.raises(ValueError):
        audit.verify_scene(records, SMALL, LARGE, "published")


def test_scene_rejects_infeasible_old_admission_during_downshift(audit):
    records = _scene(LARGE, SMALL, 20, "published")
    records[2].update(serving=LARGE, admitted=LARGE, activation_at_admission="pending")
    with pytest.raises(ValueError, match="infeasible model admitted"):
        audit.verify_scene(records, LARGE, SMALL, "published")


@pytest.mark.parametrize("selected,battery,activation", [
    (LARGE, 80, "failed"), (None, 5, "unchanged"),
])
def test_failed_replacement_and_abstention_preserve_every_active_state(
    audit, selected, battery, activation,
):
    records = _scene(SMALL, selected, battery, activation)
    assert audit.verify_scene(records, SMALL, selected, activation) == SMALL
    records[-1]["active"] = LARGE
    with pytest.raises(ValueError, match="active state drift"):
        audit.verify_scene(records, SMALL, selected, activation)


def test_abstention_may_precede_the_unchanged_activation_event(audit):
    records = _scene(LARGE, None, 5, "unchanged")
    first = records.pop(2)
    first["activation"] = "pending"
    records.insert(1, first)
    assert audit.verify_scene(records, LARGE, None, "unchanged") == LARGE


@pytest.mark.parametrize("change", [None, "duplicate-pair", "wrong-digest", "same-scores"])
def test_reference_grid_requires_exact_distinguishable_model_input_pairs(audit, change):
    manifest = json.loads((audit.ROOT / "examples/live_swap/assets.json").read_text())
    digests = {item["name"]: item["sha256"] for item in manifest["files"]}
    inputs = [{"image": {"name": f"camera-{index}.png", "sha256": str(index)}}
              for index in range(20)]
    references = [{
        "model": {"id": name, "digest": "sha256:" + digests[name + ".onnx"]},
        "image": item["image"],
        "scores": audit.np.eye(1, 1000, model * 20 + index)[0].tolist(),
    } for model, name in enumerate(("mobilenetv2", "resnet18"))
        for index, item in enumerate(inputs)]
    header = {"manifest": manifest, "references": references}
    mutations = {
        "duplicate-pair": lambda: references.__setitem__(1, copy.deepcopy(references[0])),
        "wrong-digest": lambda: references[0]["model"].update(digest="wrong"),
        "same-scores": lambda: references[1].update(scores=references[0]["scores"]),
    }
    if change is None:
        models, lookup = audit.verify_references(header, inputs)
        assert len(models) == 2 and len(lookup) == 40
    else:
        mutations[change]()
        with pytest.raises(ValueError):
            audit.verify_references(header, inputs)
