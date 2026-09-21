"""Backend profiles and startup resolution for CodeBuddy regions.

The proxy talks to the same plugin protocol in both regions.  Keeping the
region-specific values here means authentication, URL construction, session
validation, and model discovery can all use one resolved backend object.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import pathlib
from types import MappingProxyType
from typing import Any, Mapping


def normalize_endpoint(endpoint: str) -> str:
    """Return an endpoint without trailing slashes.

    The existing client accepted arbitrary endpoint strings, so this helper
    intentionally only normalizes the separator and does not impose a new
    URL scheme validation policy.
    """

    value = str(endpoint).strip()
    if not value:
        raise ValueError("endpoint 不能为空")
    normalized = value.rstrip("/")
    if not normalized:
        raise ValueError("endpoint 不能为空")
    return normalized


@dataclass(frozen=True, slots=True)
class BackendProfile:
    """Immutable region-specific CodeBuddy configuration."""

    key: str
    display_name: str
    default_endpoint: str
    auth_prefix: str
    default_session_filename: str
    models_resource: str
    chat_path: str
    enterprise_models_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_endpoint", normalize_endpoint(self.default_endpoint))
        if not self.auth_prefix.startswith("/"):
            object.__setattr__(self, "auth_prefix", f"/{self.auth_prefix}")
        if not self.chat_path.startswith("/"):
            object.__setattr__(self, "chat_path", f"/{self.chat_path}")
        if not self.enterprise_models_path.startswith("/"):
            object.__setattr__(self, "enterprise_models_path", f"/{self.enterprise_models_path}")


@dataclass(frozen=True, slots=True)
class ResolvedBackend:
    """A profile after endpoint and session-file precedence is resolved."""

    profile: BackendProfile
    endpoint: str
    session_file: pathlib.Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", normalize_endpoint(self.endpoint))
        object.__setattr__(self, "session_file", pathlib.Path(self.session_file).expanduser())

    @property
    def backend(self) -> BackendProfile:
        """Alias useful to callers that refer to the profile as a backend."""

        return self.profile

    @property
    def key(self) -> str:
        return self.profile.key

    @property
    def display_name(self) -> str:
        return self.profile.display_name

    @property
    def auth_prefix(self) -> str:
        return self.profile.auth_prefix

    @property
    def models_resource(self) -> str:
        return self.profile.models_resource

    @property
    def session_path(self) -> pathlib.Path:
        """Alias for callers that use path terminology."""

        return self.session_file

    @property
    def chat_path(self) -> str:
        return self.profile.chat_path

    @property
    def enterprise_models_path(self) -> str:
        return self.profile.enterprise_models_path


DOMESTIC_PROFILE = BackendProfile(
    key="domestic",
    display_name="国内版 CodeBuddy",
    default_endpoint="https://copilot.tencent.com",
    auth_prefix="/plugin",
    default_session_filename=".codebuddy-session.json",
    models_resource="models_config.domestic.json",
    chat_path="/v2/chat/completions",
    enterprise_models_path="/console/enterprises/{enterpriseId}/config/models",
)

GLOBAL_PROFILE = BackendProfile(
    key="global",
    display_name="国际版 CodeBuddy",
    default_endpoint="https://www.codebuddy.ai",
    auth_prefix="/plugin",
    default_session_filename=".codebuddy-global-session.json",
    models_resource="models_config.global.json",
    chat_path="/v2/chat/completions",
    enterprise_models_path="/console/enterprises/{enterpriseId}/config/models",
)

# Descriptive aliases keep the public module convenient without duplicating
# configuration values.
DOMESTIC_BACKEND = DOMESTIC_PROFILE
GLOBAL_BACKEND = GLOBAL_PROFILE
DOMESTIC = DOMESTIC_PROFILE
GLOBAL = GLOBAL_PROFILE
BACKEND_PROFILES: Mapping[str, BackendProfile] = MappingProxyType(
    {
        DOMESTIC_PROFILE.key: DOMESTIC_PROFILE,
        GLOBAL_PROFILE.key: GLOBAL_PROFILE,
    }
)


def profile_for_key(key: str) -> BackendProfile:
    try:
        return BACKEND_PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"未知 CodeBuddy backend profile: {key}") from exc


def resolve_backend(
    *,
    global_mode: bool = False,
    endpoint: str | None = None,
    session_file: pathlib.Path | str | None = None,
    environment: Mapping[str, str] | None = None,
    profile: BackendProfile | None = None,
) -> ResolvedBackend:
    """Resolve profile, endpoint, and session path with CLI precedence.

    Explicit ``endpoint`` wins first.  ``--global`` selects the global
    profile and therefore intentionally ignores ``CODEBUDDY_ENDPOINT`` when
    no explicit endpoint is supplied.  The environment variable is only a
    domestic-mode fallback.
    """

    selected_profile = profile or (GLOBAL_PROFILE if global_mode else DOMESTIC_PROFILE)
    if endpoint is not None:
        resolved_endpoint = endpoint
    elif profile is not None or global_mode:
        resolved_endpoint = selected_profile.default_endpoint
    else:
        env = os.environ if environment is None else environment
        resolved_endpoint = env.get("CODEBUDDY_ENDPOINT") or selected_profile.default_endpoint

    resolved_session = (
        pathlib.Path(session_file).expanduser()
        if session_file is not None
        else pathlib.Path.home() / selected_profile.default_session_filename
    )
    return ResolvedBackend(
        profile=selected_profile,
        endpoint=resolved_endpoint,
        session_file=resolved_session,
    )


def resolve_backend_from_args(
    args: Any,
    *,
    environment: Mapping[str, str] | None = None,
) -> ResolvedBackend:
    """Resolve a parsed argparse namespace using the shared precedence rules."""

    return resolve_backend(
        global_mode=bool(getattr(args, "global_mode", False)),
        endpoint=getattr(args, "endpoint", None),
        session_file=getattr(args, "session_file", None),
        environment=environment,
    )
