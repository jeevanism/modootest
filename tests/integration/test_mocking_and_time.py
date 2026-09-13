"""Integration tests for HTTP mocking and time control in real Odoo 19 environment."""
import datetime
import pytest
import requests
from odoo import fields

from modootest.developer import freeze_time, mock_http


def test_freeze_time_in_odoo_env(odoo_env, freeze_time):
    """Verify freeze_time controls Odoo ORM fields.Datetime.now() and fields.Date.today()."""
    target_dt_str = "2026-07-20 10:15:30"
    target_date = datetime.date(2026, 7, 20)

    with freeze_time(target_dt_str):
        now_dt = fields.Datetime.now()
        today_d = fields.Date.today()

        assert now_dt.year == 2026
        assert now_dt.month == 7
        assert now_dt.day == 20
        assert now_dt.hour == 10
        assert now_dt.minute == 15
        assert now_dt.second == 30

        assert today_d == target_date

        # Partner record created inside frozen window
        partner = odoo_env["res.partner"].create({"name": "Time Traveler Partner"})
        assert partner.id > 0
        assert partner.name == "Time Traveler Partner"


def test_mock_http_in_odoo_transaction(odoo_env, mock_http):
    """Verify mock_http intercepts outbound HTTP calls made during Odoo business logic."""
    mock_http.post(
        "https://carrier.api.example.com/rates",
        json_data={"rate": 15.50, "currency": "USD", "status": "ok"},
        status_code=200,
    )

    # Simulate an external service call that an Odoo module would execute
    res = requests.post(
        "https://carrier.api.example.com/rates",
        json={"weight": 2.5, "destination": "US"},
        headers={"X-Odoo-Test": "1"},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["rate"] == 15.50
    assert data["currency"] == "USD"

    # Verify interception
    mock_http.assert_called("https://carrier.api.example.com/rates", method="POST", count=1)
    call = mock_http.calls[0]
    assert call.json() == {"weight": 2.5, "destination": "US"}
    assert call.headers.get("X-Odoo-Test") == "1"
