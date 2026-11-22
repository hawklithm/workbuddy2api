import asyncio
import json
import sqlite3
import threading
import time

import httpx
from fastapi import FastAPI

from codebuddy_proxy import trae_models as models
from codebuddy_proxy.trae_gateway import router


def sample():
    return {"function_configs": [{"function": "chat_v3", "config_info_list": [
        {"config_name": "glm", "display_config": {"display_name": "GLM"},
         "context_window_tokens": {"dev": 200000, "max": 1000000},
         "reasoning_effort_config": {"options": ["low", "high"], "default_level": "high"}},
        {"config_name": "glm"}, {"config_name": "hidden", "is_invisible_to_user": True},
        {"config_name": "custom_model_1"}, {"config_name": "summary"},
    ]}, {"function": "solo_agent", "config_info_list": [{"config_name": "wrong-group"}]}]}


def test_catalog_filters_and_context():
    result = models.parse_catalog(sample())
    assert [item["id"] for item in result] == ["glm"]
    assert result[0]["context_length"] == 200000
    assert result[0]["context_length_max"] == 900000
    assert result[0]["reasoning_efforts"] == ["low", "high"]
    assert "multimodal" not in result[0]
    assert models.catalog_headers("Cloud-IDE-JWT value", "device", "machine")["x-ide-token"] == "value"


def test_fetch_catalog_uses_expected_request(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=sample())
    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs))
    async def run():
        _, entries = await models.fetch_catalog("jwt", "device", "machine")
        assert entries[0]["id"] == "glm"
        assert requests[0].url == models.CATALOG_URL
        assert json.loads(requests[0].content) == {"functions": ["chat_v3"]}
        assert requests[0].headers["x-ide-token"] == "jwt"
        assert "authorization" not in requests[0].headers
    asyncio.run(run())


def test_client_cache_uses_exact_uid_and_chat_group(tmp_path):
    path = tmp_path / "state.vscdb"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        for uid in ("12", "123"):
            data = {"chat_v3": [{"name": "model-" + uid}, {"name": "custom", "provider": "third-party"}],
                    "solo_agent": [{"name": "other"}]}
            db.execute("INSERT INTO ItemTable VALUES (?, ?)", (uid + "_AI.agent.model.model_list_map", json.dumps(data)))
    assert [m["id"] for m in models.client_cache("12", path)] == ["model-12"]
    assert models.client_cache("1", path) == []


def test_cache_uid_and_corruption(tmp_path):
    path = tmp_path / "models-cache.json"
    path.write_text(json.dumps({"uid": "a", "fetched_at": time.time(), "response": sample()}), encoding="utf-8")
    assert models.read_cache(path, "a")[1][0]["id"] == "glm"
    assert models.read_cache(path, "b") is None
    path.write_text("{", encoding="utf-8")
    assert models.read_cache(path, "a") is None


def test_endpoint_cache_refresh_and_account_isolation(tmp_path, monkeypatch):
    class Accounts:
        lock = threading.RLock()
        def _records(self):
            return [{"id": "a", "uid": "user-a"}, {"id": "b", "uid": "user-b"}]
        def _path(self, account_id):
            return tmp_path / account_id
    accounts = Accounts()
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    app = FastAPI()
    app.include_router(router)
    app.state.trae_get_account_manager = lambda: type("Manager", (), {"trae_accounts": accounts})()
    monkeypatch.setattr(models, "load_account", lambda accounts, account_id: ("user-" + account_id, "jwt", "device", "machine"))
    calls = []
    async def fetch(token, device, machine):
        calls.append(token)
        await asyncio.sleep(0.01)
        return sample(), models.parse_catalog(sample())
    monkeypatch.setattr(models, "fetch_catalog", fetch)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            assert (await client.get("/trae/v1/models")).status_code == 400
            headers = {"X-Trae-Account-Id": "a"}
            one, two = await asyncio.gather(client.get("/trae/v1/models", headers=headers), client.get("/trae/v1/models", headers=headers))
            assert one.json()["data"][0]["id"] == "glm"
            assert {one.json()["source"], two.json()["source"]} == {"upstream", "upstream_cache"}
            assert len(calls) == 1
            assert (await client.get("/trae/v1/models", headers={"X-Trae-Account-Id": "b"})).json()["source"] == "upstream"
            assert len(calls) == 2
            assert (await client.get("/trae/v1/models?refresh=true", headers=headers)).json()["source"] == "upstream"
            assert len(calls) == 3
            async def failed(*args):
                raise httpx.ConnectError("secret details")
            monkeypatch.setattr(models, "fetch_catalog", failed)
            monkeypatch.setattr(models, "client_cache", lambda uid: [])
            result = (await client.get("/trae/v1/models?refresh=true", headers=headers)).json()
            assert result["source"] == "upstream_cache" and result["stale"] is True
            assert "secret details" not in json.dumps(result)
    asyncio.run(run())


def test_fallback_when_no_cache(tmp_path, monkeypatch):
    class Accounts:
        lock = threading.RLock()
        def _records(self):
            return [{"id": "a", "uid": "user-a"}]
        def _path(self, account_id):
            return tmp_path
    app = FastAPI()
    app.include_router(router)
    app.state.trae_get_account_manager = lambda: type("Manager", (), {"trae_accounts": Accounts()})()
    monkeypatch.setattr(models, "load_account", lambda *args: (_ for _ in ()).throw(RuntimeError("credentials")))
    monkeypatch.setattr(models, "client_cache", lambda uid: [])
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.get("/trae/v1/models")
            assert response.status_code == 200
            assert response.json()["source"] == "static_fallback"
            assert response.json()["stale"] is True
    asyncio.run(run())
