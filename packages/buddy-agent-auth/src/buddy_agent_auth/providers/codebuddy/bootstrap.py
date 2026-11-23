"""Initialize WorkBuddy account storage from legacy sessions and CLI options."""
from __future__ import annotations
import pathlib
import tempfile
from .backend_profile import DOMESTIC_PROFILE, GLOBAL_PROFILE, resolve_backend
from .client import CodeBuddyClient, CodeBuddyError


def prepare_account_store(store, resolved_backend, *, global_mode=False, endpoint=None,
                          session_file=None, login=False, no_browser=False,
                          environment_endpoint=None, client_class=CodeBuddyClient,
                          on_incompatible=lambda backend: None):
    """Import old sessions once and select the requested or freshly logged-in identity."""
    initial_import = not store.index_file.exists()
    explicit = bool(global_mode or endpoint is not None
                    or session_file is not None
                    or environment_endpoint)
    candidates = [resolved_backend]
    for profile in (DOMESTIC_PROFILE, GLOBAL_PROFILE):
        candidate = resolve_backend(profile=profile, environment={})
        if candidate not in candidates:
            candidates.append(candidate)
    selected_uid = None
    for candidate in candidates:
        # Migration is one-time. Re-reading a legacy file after its account
        # was deleted must not silently associate that identity again.
        if not initial_import and not (session_file is not None
                                       and candidate is resolved_backend):
            continue
        try:
            old = client_class(backend=candidate)
        except CodeBuddyError:
            if session_file is not None and candidate is resolved_backend:
                raise
            on_incompatible(candidate.profile.key)
            continue
        account = old.session.get("account") or {}
        auth = old.session.get("auth") or {}
        if not isinstance(account, dict) or not isinstance(auth, dict):
            continue
        uid = account.get("uid")
        if not uid or not (auth.get("accessToken") or auth.get("refreshToken")):
            continue
        if candidate is resolved_backend:
            selected_uid = str(uid).strip()
        known = any(
            row["backend"] == old.profile.key and row["endpoint"] == old.endpoint
            and str(row["uid"]) == str(uid) for row in store.list_accounts()
        )
        if not known:
            store.import_client(old)
    if login:
        with tempfile.TemporaryDirectory(prefix="login-", dir=store.root) as temporary:
            login_backend = resolve_backend(
                profile=resolved_backend.profile, endpoint=resolved_backend.endpoint,
                session_file=pathlib.Path(temporary) / "session.json", environment={},
            )
            login_client = client_class(backend=login_backend)
            login_client.login(open_browser=not no_browser)
            store.import_client(login_client, activate=True)
    elif explicit:
        matching = [row for row in store.list_accounts()
                    if row["backend"] == resolved_backend.profile.key
                    and row["endpoint"] == resolved_backend.endpoint
                    and (session_file is None or str(row["uid"]) == selected_uid)]
        if matching:
            chosen = next((row for row in matching if row["active"]), matching[0])
            store.set_active(chosen["id"])
        else:
            store.clear_active()
