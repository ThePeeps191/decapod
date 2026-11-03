import pytest

from decapod.domain import Level
from decapod.evals.scenarios import SCENARIOS, run_scenario


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenarios_pass_with_scripted_brain(name):
    result = await run_scenario(SCENARIOS[name])
    assert result.passed, f"{name}: {result.details}"
    assert result.metrics.cycles >= 1


async def test_scenario_at_capped_level():
    result = await run_scenario(SCENARIOS["steady_progress"], max_level=Level.L1)
    assert result.passed and result.level is Level.L1
