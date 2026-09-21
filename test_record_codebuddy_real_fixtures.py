import json
import sys

import pytest

import record_codebuddy_real_fixtures as recorder
from codebuddy_proxy.backend_profile import DOMESTIC_PROFILE, GLOBAL_PROFILE


@pytest.mark.parametrize(
    ("profile_args", "profile"),
    [([], DOMESTIC_PROFILE), (["--global"], GLOBAL_PROFILE)],
)
def test_recorder_separates_fixture_paths_from_api_paths(
    tmp_path, monkeypatch, profile_args, profile
):
    output_dir = tmp_path / profile.key / "fixtures"
    requests = []

    class FakeClient:
        def __init__(self, *, backend):
            self.profile = backend.profile
            self.authenticated = False

        def ensure_authenticated(self, *, open_browser):
            self.authenticated = True

    def fake_direct_request(client, method, path, body=None, *, accept="application/json"):
        requests.append((method, path, body, accept))
        if path == "/v3/config":
            return 200, {"Content-Type": "application/json"}, b'{"models": []}', "application/json"
        assert path == profile.chat_path
        return 200, {"Content-Type": "text/event-stream"}, b"data: [DONE]\n\n", "text/event-stream"

    def unexpected_network(*args, **kwargs):
        raise AssertionError("fixture recorder test must not access the network")

    monkeypatch.setattr(recorder, "CodeBuddyClient", FakeClient)
    monkeypatch.setattr(recorder, "direct_request", fake_direct_request)
    monkeypatch.setattr(recorder.urllib.request, "urlopen", unexpected_network)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "record_codebuddy_real_fixtures.py",
            *profile_args,
            "--output-dir",
            str(output_dir),
            "--no-browser",
        ],
    )

    assert recorder.main() == 0

    assert requests[0][0:2] == ("GET", "/v3/config")
    assert requests[1][0:2] == ("POST", profile.chat_path)
    assert output_dir.joinpath("models.v3-config.json").is_file()
    chat_fixture = output_dir.joinpath("chat-hi.sse.json")
    assert chat_fixture.is_file()
    assert {path.name for path in output_dir.iterdir()} == {
        "models.v3-config.json",
        "chat-hi.sse.json",
    }
    assert not output_dir.joinpath(profile.chat_path.lstrip("/")).exists()

    models_fixture = json.loads(
        output_dir.joinpath("models.v3-config.json").read_text(encoding="utf-8")
    )
    chat_fixture_data = json.loads(chat_fixture.read_text(encoding="utf-8"))
    assert models_fixture["request"]["path"] == "/v3/config?repos="
    assert chat_fixture_data["request"]["path"] == profile.chat_path
