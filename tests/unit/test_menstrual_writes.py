"""Menstrual writes preserve the other fields in Garmin's daily snapshot API."""

import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from garmin_mcp import womens_health


@pytest.mark.asyncio
async def test_sex_drive_write_preserves_day_and_verifies_saved_value():
    client = Mock()
    client.get_menstrual_data_for_date.side_effect = [
        {"dayLog": {
            "symptoms": ["CRAMPS"], "moods": ["HAPPY"],
            "flow": "LIGHT", "discharge": [], "sexDrive": "AVERAGE",
            "sexualActivity": None, "notes": "Existing note", "ovulationDay": False,
        }},
        {"dayLog": {"sexDrive": "HIGH"}},
    ]
    womens_health.configure(client)
    app = womens_health.register_tools(FastMCP("menstrual write test"))

    result = await app.call_tool(
        "set_menstrual_sex_drive", {"date": "2026-11-20", "sex_drive": "high"}
    )

    assert json.loads(result[0][0].text) == {
        "date": "2026-11-20", "sexDrive": "HIGH"
    }
    client.update_menstrual_daily_log.assert_called_once_with(
        "2026-11-20", symptoms=["CRAMPS"], moods=["HAPPY"],
        flow="LIGHT", discharge=[], sex_drive="HIGH",
        sexual_activity=None, notes="Existing note", ovulation_day=False,
    )


@pytest.mark.asyncio
async def test_invalid_sex_drive_value_does_not_write():
    client = Mock()
    womens_health.configure(client)
    app = womens_health.register_tools(FastMCP("menstrual validation test"))

    result = await app.call_tool(
        "set_menstrual_sex_drive", {"date": "2026-11-20", "sex_drive": "very high"}
    )

    assert "Unsupported Sex Drive value" in result[0][0].text
    client.update_menstrual_daily_log.assert_not_called()
