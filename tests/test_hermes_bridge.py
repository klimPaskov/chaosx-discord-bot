from pathlib import Path

import pytest
import yaml

from chaosx_bot.hermes_bridge import _temporary_reasoning_effort


@pytest.mark.asyncio
async def test_temporary_reasoning_effort_restores_config(tmp_path: Path):
    config = tmp_path / "config.yaml"
    original = {"agent": {"reasoning_effort": "medium"}, "model": {"default": "gpt-5.6-luna"}}
    config.write_text(yaml.safe_dump(original), encoding="utf-8")

    async with _temporary_reasoning_effort(config, "xhigh"):
        active = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert active["agent"]["reasoning_effort"] == "xhigh"

    restored = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert restored == original
