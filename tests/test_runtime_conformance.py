import pytest

from temms import InferenceResult, ModelRef, ModelUnavailableError
from temms.adapters import InMemoryRuntime, OnnxModel, OnnxRuntime


@pytest.fixture(params=["memory", "onnx"])
def runtime_factory(request, tmp_path):
    def create(models):
        resources = {"memory_mb": 256}
        if request.param == "memory":
            handlers = {
                ref.digest: lambda value, digest=ref.digest: (digest, value["x"])
                for refs in models.values() for ref in refs
            }
            return InMemoryRuntime(models=models, handlers=handlers, resources=resources)

        class Session:
            def __init__(self, digest):
                self.digest = digest

            def run(self, output_names, input_feed):
                return [self.digest, input_feed["x"]]

        configs, sessions = {}, {}
        for slot, refs in models.items():
            configs[slot] = []
            for index, ref in enumerate(refs):
                path = tmp_path / f"{slot}-{index}.onnx"
                path.write_bytes(b"injected-session fixture")
                configs[slot].append(OnnxModel(ref, path))
                sessions[str(path)] = Session(ref.digest)
        return OnnxRuntime(
            models=configs, resources=resources,
            session_factory=lambda path, providers: sessions[path],
        )

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [
    pytest.param(ModelRef("model", "sha256:new"), id="same-id-new-digest"),
    pytest.param(ModelRef("renamed", "sha256:original"), id="new-id-same-digest"),
])
async def test_activation_identity_uses_both_id_and_digest(runtime_factory, second) -> None:
    first = ModelRef("model", "sha256:original")
    runtime = runtime_factory({"vision": [first, second]})
    assert tuple(await runtime.models("vision")) == (first, second)

    initial = await runtime.activate("vision", first)
    assert initial.changed and initial.active_model == first
    assert await runtime.infer("vision", {"x": "before"}) == InferenceResult(
        first, (first.digest, "before")
    )

    replacement = await runtime.activate("vision", second)
    assert replacement.requested_model == second
    assert replacement.changed and replacement.active_model == second
    assert (await runtime.state("vision")).active_model == second
    assert await runtime.infer("vision", {"x": "after"}) == InferenceResult(
        second, (second.digest, "after")
    )


@pytest.mark.asyncio
async def test_activation_uses_inventory_metadata_for_same_identity(runtime_factory) -> None:
    inventory = ModelRef("model", "sha256:model", {"origin": "runtime"})
    requested = ModelRef("model", "sha256:model", {"origin": "caller"})
    runtime = runtime_factory({"vision": [inventory]})

    initial = await runtime.activate("vision", requested)
    assert initial.requested_model == requested
    assert initial.active_model == inventory
    assert initial.changed

    repeated = await runtime.activate("vision", ModelRef(inventory.id, inventory.digest))
    assert repeated.active_model == inventory
    assert not repeated.changed
    assert (await runtime.state("vision")).active_model == inventory
    assert await runtime.infer("vision", {"x": "request"}) == InferenceResult(
        inventory, (inventory.digest, "request")
    )


@pytest.mark.asyncio
async def test_wrong_slot_activation_preserves_both_serving_models(runtime_factory) -> None:
    vision = ModelRef("model", "sha256:vision")
    other = ModelRef("model", "sha256:other")
    runtime = runtime_factory({"vision": [vision], "other": [other]})
    await runtime.activate("vision", vision)
    await runtime.activate("other", other)
    before = {slot: await runtime.state(slot) for slot in ("vision", "other")}

    with pytest.raises(ModelUnavailableError, match="sha256:other.*slot 'vision'"):
        await runtime.activate("vision", other)

    for slot, ref in (("vision", vision), ("other", other)):
        assert await runtime.state(slot) == before[slot]
        assert await runtime.infer(slot, {"x": slot}) == InferenceResult(ref, (ref.digest, slot))
