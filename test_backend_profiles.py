import json
import importlib.resources
import stat
from pathlib import Path

import pytest

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.backend_profile import (
    BackendProfile,
    DOMESTIC_PROFILE,
    GLOBAL_PROFILE,
    resolve_backend,
    resolve_backend_from_args,
)
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError


def test_profile_resolution_precedence_and_help(tmp_path):
    environment = {"CODEBUDDY_ENDPOINT": "https://env.example.test///"}

    domestic = resolve_backend(environment=environment)
    assert domestic.profile is DOMESTIC_PROFILE
    assert domestic.endpoint == "https://env.example.test"
    assert domestic.session_file == Path.home() / ".codebuddy-session.json"

    global_backend = resolve_backend(global_mode=True, environment=environment)
    assert global_backend.profile is GLOBAL_PROFILE
    assert global_backend.endpoint == GLOBAL_PROFILE.default_endpoint
    assert global_backend.session_file == Path.home() / ".codebuddy-global-session.json"

    explicit = resolve_backend(
        global_mode=True,
        endpoint="https://staging-codebuddy.tencent.com///",
        session_file=tmp_path / "staging.json",
        environment=environment,
    )
    assert explicit.profile is GLOBAL_PROFILE
    assert explicit.endpoint == "https://staging-codebuddy.tencent.com"
    assert explicit.session_file == tmp_path / "staging.json"

    args = proxy.parse_args(["--global"])
    assert args.global_mode is True
    assert resolve_backend_from_args(args, environment=environment).profile is GLOBAL_PROFILE
    assert "--global" in proxy.build_parser().format_help()


@pytest.mark.parametrize("profile", [DOMESTIC_PROFILE, GLOBAL_PROFILE])
def test_profile_urls_keep_paths_and_change_only_host(tmp_path, profile):
    client = CodeBuddyClient(
        backend=resolve_backend(profile=profile, session_file=tmp_path / f"{profile.key}.json")
    )

    expected_host = profile.default_endpoint
    assert client.build_url("/v2/plugin/auth/state?platform=VSCode") == (
        f"{expected_host}/v2/plugin/auth/state?platform=VSCode"
    )
    assert client.build_url("/v2/plugin/auth/token?state=s") == (
        f"{expected_host}/v2/plugin/auth/token?state=s"
    )
    assert client.build_url("/v2/plugin/login/account?state=s") == (
        f"{expected_host}/v2/plugin/login/account?state=s"
    )
    assert client.build_url("/v2/plugin/auth/token/refresh") == (
        f"{expected_host}/v2/plugin/auth/token/refresh"
    )
    assert client.build_url(profile.chat_path) == f"{expected_host}/v2/chat/completions"
    assert client.build_url(client.enterprise_models_path("enterprise-1")) == (
        f"{expected_host}/console/enterprises/enterprise-1/config/models"
    )


def test_session_profiles_are_isolated_and_saved_with_metadata(tmp_path):
    legacy_file = tmp_path / "legacy.json"
    legacy_file.write_text(
        json.dumps({"auth": {"accessToken": "domestic-token"}}),
        encoding="utf-8",
    )

    domestic = CodeBuddyClient(endpoint=DOMESTIC_PROFILE.default_endpoint, session_file=legacy_file)
    assert domestic.session["auth"]["accessToken"] == "domestic-token"

    global_backend = resolve_backend(global_mode=True, session_file=legacy_file)
    with pytest.raises(CodeBuddyError, match="只能由 domestic profile") as global_error:
        CodeBuddyClient(backend=global_backend)
    assert "domestic-token" not in str(global_error.value)
    assert json.loads(legacy_file.read_text(encoding="utf-8"))["auth"]["accessToken"] == "domestic-token"

    with pytest.raises(CodeBuddyError, match="默认 endpoint") as custom_error:
        CodeBuddyClient(
            endpoint="https://staging-codebuddy.tencent.com",
            session_file=legacy_file,
        )
    assert "domestic-token" not in str(custom_error.value)

    saved_file = tmp_path / "saved.json"
    client = CodeBuddyClient(
        backend=resolve_backend(profile=GLOBAL_PROFILE, session_file=saved_file)
    )
    client._save_session(
        {
            "auth": {"accessToken": "global-token", "refreshToken": "refresh-token"},
            "account": {"uid": "u1"},
            "machineId": "machine-1",
        }
    )
    saved = json.loads(saved_file.read_text(encoding="utf-8"))
    assert saved["backend"] == "global"
    assert saved["endpoint"] == "https://www.codebuddy.ai"
    assert saved["machineId"] == "machine-1"
    assert stat.S_IMODE(saved_file.stat().st_mode) == 0o600

    mismatch_file = tmp_path / "mismatch.json"
    mismatch_file.write_text(
        json.dumps(
            {
                "backend": "global",
                "endpoint": "https://www.codebuddy.ai",
                "auth": {"accessToken": "must-not-be-used"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CodeBuddyError, match="endpoint 与当前 backend 不匹配") as endpoint_error:
        CodeBuddyClient(
            backend=resolve_backend(
                profile=GLOBAL_PROFILE,
                endpoint="https://staging-codebuddy.tencent.com",
                session_file=mismatch_file,
            )
        )
    assert "must-not-be-used" not in str(endpoint_error.value)
    assert "must-not-be-used" in mismatch_file.read_text(encoding="utf-8")

    with pytest.raises(CodeBuddyError, match="backend 不匹配") as profile_error:
        CodeBuddyClient(
            backend=resolve_backend(
                profile=DOMESTIC_PROFILE,
                session_file=mismatch_file,
            )
        )
    assert "must-not-be-used" not in str(profile_error.value)

    matching_file = tmp_path / "matching.json"
    matching_file.write_text(
        json.dumps(
            {
                "backend": "global",
                "endpoint": GLOBAL_PROFILE.default_endpoint,
                "auth": {"accessToken": "matching-token"},
            }
        ),
        encoding="utf-8",
    )
    matching = CodeBuddyClient(
        backend=resolve_backend(
            profile=GLOBAL_PROFILE,
            session_file=matching_file,
        )
    )
    assert matching.session["auth"]["accessToken"] == "matching-token"


def test_root_model_config_matches_packaged_domestic_resource():
    package_resource = importlib.resources.files("codebuddy_proxy").joinpath(
        DOMESTIC_PROFILE.models_resource
    )
    root_copy = Path(__file__).with_name("models_config.json")

    assert root_copy.read_bytes() == package_resource.read_bytes()


def test_refresh_preserves_session_metadata_and_machine_id(tmp_path, monkeypatch):
    session_file = tmp_path / "refresh.json"
    client = CodeBuddyClient(
        backend=resolve_backend(profile=GLOBAL_PROFILE, session_file=session_file)
    )
    client._save_session(
        {
            "auth": {"refreshToken": "old-refresh"},
            "account": {"uid": "u1"},
            "machineId": "machine-1",
        }
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {"accessToken": "new-access", "refreshToken": "new-refresh"},
    )

    assert client.refresh() is True
    saved = json.loads(session_file.read_text(encoding="utf-8"))
    assert saved["backend"] == "global"
    assert saved["endpoint"] == "https://www.codebuddy.ai"
    assert saved["machineId"] == "machine-1"
    assert saved["account"] == {"uid": "u1"}


def test_domain_header_prefers_token_and_falls_back_to_resolved_authority(tmp_path):
    domestic = CodeBuddyClient(
        endpoint="https://copilot.tencent.com", session_file=tmp_path / "domestic.json"
    )
    assert domestic._enterprise_headers({})["X-Domain"] == "copilot.tencent.com"
    domestic.session = {"auth": {"accessToken": "token"}}
    assert domestic.auth_headers()["X-Domain"] == "copilot.tencent.com"
    assert domestic._enterprise_headers({"domain": "token.example"})["X-Domain"] == "token.example"

    global_client = CodeBuddyClient(
        backend=resolve_backend(profile=GLOBAL_PROFILE, session_file=tmp_path / "global.json")
    )
    global_client.session = {"auth": {"accessToken": "token"}}
    assert global_client.auth_headers()["X-Domain"] == "www.codebuddy.ai"

    port_client = CodeBuddyClient(
        endpoint="https://example.com:8443", session_file=tmp_path / "port.json"
    )
    assert port_client._enterprise_headers({})["X-Domain"] == "example.com:8443"


def test_profile_model_resources_and_codex_shape():
    domestic_ids = {model["id"] for model in proxy.load_models_from_local_config(DOMESTIC_PROFILE)}
    global_ids = {model["id"] for model in proxy.load_models_from_local_config(GLOBAL_PROFILE)}

    assert {"glm-5.2", "deepseek-v4-pro"}.issubset(domestic_ids)
    assert {"claude-4.0", "gpt-5", "gemini-2.5-pro"}.issubset(global_ids)
    assert "glm-5.2" not in global_ids

    codex_model = proxy.model_to_codex_format(
        next(model for model in proxy.load_models_from_local_config(GLOBAL_PROFILE) if model["id"] == "gpt-5")
    )
    assert {"id", "slug", "display_name", "object", "created", "owned_by"}.issubset(codex_model)

    missing = BackendProfile(
        key="missing",
        display_name="Missing",
        default_endpoint="https://missing.example",
        auth_prefix="/plugin",
        default_session_filename=".missing.json",
        models_resource="does-not-exist.json",
        chat_path="/v2/chat/completions",
        enterprise_models_path="/console/enterprises/{enterpriseId}/config/models",
    )
    with pytest.raises(CodeBuddyError, match="does-not-exist.json"):
        proxy.load_models_from_local_config(missing)
