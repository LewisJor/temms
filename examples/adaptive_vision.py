"""A complete TEMMS example with an injected runtime."""

import asyncio

from temms import TEMMS, BestFeasibleSelector, Constraint, ModelRef, Operator
from temms.adapters import InMemoryRuntime


daylight = ModelRef(
    id="daylight",
    digest="sha256:daylight",
    priority=100,
    constraints=(Constraint("light", Operator.EQ, "bright"),),
)
lowlight = ModelRef(id="lowlight", digest="sha256:lowlight", priority=10)


async def main() -> None:
    runtime = InMemoryRuntime[str, str](
        models={"vision": [daylight, lowlight]},
        handlers={
            daylight.digest: lambda frame: f"day:{frame}",
            lowlight.digest: lambda frame: f"low:{frame}",
        },
    )
    temms = TEMMS(runtime=runtime, selector=BestFeasibleSelector())

    result = await temms.reconcile("vision", {"light": "low"})
    inference = await temms.infer("vision", "frame-001")

    print(result.decision.selected_model.id)
    print(inference.model.id, inference.output)


asyncio.run(main())
