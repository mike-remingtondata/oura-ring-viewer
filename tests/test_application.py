"""Tests for the chart-drawer Application entry point."""

import json
from unittest.mock import MagicMock, PropertyMock, patch

from oura_ring_viewer.applications.oura_chart_app import OuraChartApp


def test_on_open_launches_the_chart_for_the_active_patient() -> None:
    app = OuraChartApp(event=MagicMock())
    with patch.object(
        type(app), "context", new_callable=PropertyMock, return_value={"patient": {"id": "abc123"}}
    ):
        effect = app.on_open()

    data = json.loads(effect.payload)["data"]
    assert data["url"] == "/plugin-io/api/oura_ring_viewer/?patient_id=abc123"
    assert data["target"] == "right_chart_pane_large"
    assert data["title"] == "Oura Ring"
