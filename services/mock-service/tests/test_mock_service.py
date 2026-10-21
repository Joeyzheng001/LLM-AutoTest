"""HTTP mock 与真实 fixture 执行器之间的契约回归。"""

from __future__ import annotations

import httpx
import pytest
from executor.nodes.auto_execute import _run_fixture_phase

from mock_service.main import app


@pytest.mark.asyncio
async def test_fixture_installs_invokes_and_removes_real_http_stub() -> None:
    transport = httpx.ASGITransport(app=app)
    path = "/__admin/scenarios/fixture-contract"
    shared = {
        "name": "fixture-contract",
        "base_url": "http://mock.local",
        "path": path,
        "required": True,
        "scope": "case",
        "control_kind": "downstream_response",
        "interface_id": "IF-REST-002",
        "scenario_contains": "O32失败",
    }
    fixtures = [
        {
            **shared,
            "phase": "setup",
            "method": "PUT",
            "body": {
                "method": "POST",
                "path": "/o32/fixture-contract",
                "match": {"body": {"serialNo": "CASE-001"}},
                "response": {"body": {"errorCode": "MOCK_FAILURE"}},
            },
        },
        {**shared, "phase": "teardown", "method": "DELETE"},
    ]
    records, blocked = await _run_fixture_phase(
        fixtures,
        "setup",
        "http://target.local",
        transport,
        True,
        {"Authorization": "Bearer target-only"},
        case_id="CASE-001",
    )
    assert blocked is None
    assert records[0]["passed"] is True
    async with httpx.AsyncClient(transport=transport, base_url="http://mock.local") as client:
        response = await client.post("/o32/fixture-contract", json={"serialNo": "CASE-001"})
        assert response.status_code == 200
        assert response.json() == {"errorCode": "MOCK_FAILURE"}
        unmatched = await client.post("/o32/fixture-contract", json={"serialNo": "CASE-002"})
        assert unmatched.status_code == 404
    records, blocked = await _run_fixture_phase(
        fixtures,
        "teardown",
        "http://target.local",
        transport,
        True,
        {},
        case_id="CASE-001",
    )
    assert blocked is None
    assert records[0]["passed"] is True
    async with httpx.AsyncClient(transport=transport, base_url="http://mock.local") as client:
        assert (
            await client.post("/o32/fixture-contract", json={"serialNo": "CASE-001"})
        ).status_code == 404
        assert (await client.delete(path)).status_code == 200


@pytest.mark.asyncio
async def test_match_conflicts_are_explicit_and_priority_is_deterministic() -> None:
    names = ["conflict-first", "conflict-second"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock.local"
    ) as client:
        spec = {"method": "POST", "path": "/o32/conflict", "response": {"body": None}}
        try:
            for name in names:
                assert (
                    await client.put(f"/__admin/scenarios/{name}", json=spec)
                ).status_code == 200
            assert (await client.post("/o32/conflict")).status_code == 409
            spec["priority"] = 10
            await client.put(f"/__admin/scenarios/{names[0]}", json=spec)
            response = await client.post("/o32/conflict")
            assert response.status_code == 200
            assert response.json() is None
        finally:
            for name in names:
                await client.delete(f"/__admin/scenarios/{name}")


@pytest.mark.asyncio
async def test_admin_rejects_invalid_routes_and_transport_headers() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock.local"
    ) as client:
        for spec in [
            {"method": "POST", "path": "/__admin/scenarios", "response": {}},
            {"method": "POST", "path": "/valid?query=wrong-place", "response": {}},
            {"method": "POST", "path": "/valid", "response": {"headers": {"Content-Length": "10"}}},
        ]:
            assert (await client.put("/__admin/scenarios/invalid", json=spec)).status_code == 422
