import asyncio
from types import SimpleNamespace

import httpx

from codebuddy_proxy import workbuddy_catalog as catalog


def fixture():
    return {"code": 0, "data": {"agents": [{"name": "other", "models": ["unused"]}, {"name": "cli", "models": ["b", "a", "b", "missing", "disabled"]}],
            "models": [{"id": "a", "maxInputTokens": 128000, "maxOutputTokens": 8000},
                       {"id": "b", "maxInputTokens": 200000, "maxOutputTokens": 10000, "supportsImages": True,
                        "disabledMultimodal": True, "reasoning": {"supportedEfforts": ["low", "high"]}},
                       {"id": "disabled", "disabled": True, "maxInputTokens": 1, "maxOutputTokens": 1}]}}


def client(region, uid, token="token"):
    return SimpleNamespace(profile=SimpleNamespace(key=region), endpoint="https://" + region + ".example",
                           session={"auth": {"accessToken": token}, "account": {"uid": uid}})


def test_parse_respects_cli_order_and_disabled():
    models = catalog.parse_catalog(fixture())
    assert [m["id"] for m in models] == ["b", "a"]
    assert models[0]["supportsImages"] is False
    try:
        catalog.parse_catalog({"models": [], "agents": []})
    except ValueError:
        pass
    else:
        assert False, "missing cli whitelist must fail"


def test_request_protocol_for_regions(monkeypatch):
    real_client = httpx.AsyncClient
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=fixture())
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs))
    async def run():
        assert len(await catalog.fetch_catalog("domestic", "https://cn.example", "cn-token")) == 2
        assert len(await catalog.fetch_catalog("global", "https://global.example", "global-token")) == 2
        assert requests[0].url.path == "/console/enterprises/personal/models"
        assert requests[1].url.path == "/v3/config"
        assert requests[0].headers["authorization"] == "Bearer cn-token"
        assert requests[1].headers["user-agent"] == "WorkBuddyAI/5.5.2"
        assert requests[1].headers["x-product"] == "SaaS"
    asyncio.run(run())


def test_cache_isolates_account_region_and_falls_back(tmp_path, monkeypatch):
    calls = []
    async def fetch(region, endpoint, token):
        calls.append((region, endpoint, token))
        await asyncio.sleep(0.01)
        return catalog.parse_catalog(fixture())
    monkeypatch.setattr(catalog, "fetch_catalog", fetch)
    store = catalog.WorkBuddyCatalog(tmp_path)
    cn_a, cn_b, global_a = client("domestic", "a"), client("domestic", "b"), client("global", "a")
    fallback = [{"id": "static"}]
    async def run():
        first, second = await asyncio.gather(store.get(cn_a, fallback), store.get(cn_a, fallback))
        assert {first[1]["source"], second[1]["source"]} == {"live", "cached"}
        assert len(calls) == 1
        await store.get(cn_b, fallback)
        await store.get(global_a, fallback)
        assert len(calls) == 3
        restarted = catalog.WorkBuddyCatalog(tmp_path)
        assert (await restarted.get(cn_a, fallback))[1]["source"] == "cached"
        async def failure(*args):
            raise httpx.ConnectError("sensitive error")
        monkeypatch.setattr(catalog, "fetch_catalog", failure)
        data, info = await restarted.get(cn_a, fallback, refresh=True)
        assert info["source"] == "cached" and info["stale"] is True
        assert [m["id"] for m in data] == ["b", "a"]
        data, info = await restarted.get(client("global", "other"), fallback)
        assert data == fallback and info["source"] == "builtin"
        assert "sensitive" not in str(info)
    asyncio.run(run())


def test_actual_models_endpoint_converts_catalog(tmp_path, monkeypatch):
    import codebuddy_proxy.__main__ as proxy
    from codebuddy_proxy.backend_profile import DOMESTIC_PROFILE, resolve_backend
    from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient
    current = CodeBuddyClient(backend=resolve_backend(profile=DOMESTIC_PROFILE, session_file=tmp_path / "session"))
    current.session = {"auth": {"accessToken": "test"}, "account": {"uid": "a"}}
    state = proxy.ProxyState(current, None, None, model_catalog=catalog.WorkBuddyCatalog(tmp_path / "cache"))
    monkeypatch.setattr(proxy, "proxy_state", state)
    async def fetch(*args):
        return catalog.parse_catalog(fixture())
    monkeypatch.setattr(catalog, "fetch_catalog", fetch)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://local") as api:
            response = await api.get("/v1/models")
            assert response.status_code == 200
            body = response.json()
            assert body["catalog"]["source"] == "live"
            assert [m["id"] for m in body["models"]] == ["b", "a"]
            assert body["models"][0]["input_modalities"] == ["text"]
            assert [x["effort"] for x in body["models"][0]["supported_reasoning_levels"]] == ["low", "high"]
            assert (await api.get("/v1/models")).json()["catalog"]["source"] == "cached"
            assert (await api.get("/v1/models?refresh=true")).json()["catalog"]["source"] == "live"
    asyncio.run(run())


def test_static_mode_never_requests_upstream(tmp_path, monkeypatch):
    async def fail(*args):
        raise AssertionError("static mode must not request the upstream")
    monkeypatch.setattr(catalog, "fetch_catalog", fail)
    store = catalog.WorkBuddyCatalog(tmp_path, static=True)
    data, info = asyncio.run(store.get(client("domestic", "a"), [{"id": "fallback"}], refresh=True))
    assert data == [{"id": "fallback"}]
    assert info["source"] == "builtin"
