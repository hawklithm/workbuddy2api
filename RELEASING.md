# Development releases to PyPI

The three independently versioned projects are released in this order:

1. `buddy-agent-auth==0.1.0.dev1` from `hawklithm/workbuddy2api`;
2. `buddy-agent-checkin==0.1.1.dev2` from `hawklithm/dailycheckin`;
3. `workbuddy2api==2.0.8.dev1` from `hawklithm/workbuddy2api`.

Each push of the corresponding tag below builds and validates a wheel and sdist,
publishes to PyPI using OIDC, and installs from PyPI in a clean CI job. Wait for
publication and verification of each package before pushing the next tag.

| PyPI project | GitHub owner | repository | workflow filename | environment | tag |
| --- | --- | --- | --- | --- | --- |
| buddy-agent-auth | hawklithm | workbuddy2api | publish-auth-dev.yml | pypi | buddy-agent-auth-v0.1.0.dev1 |
| buddy-agent-checkin | hawklithm | dailycheckin | publish-dev.yml | pypi | buddy-agent-checkin-v0.1.1.dev2 |
| workbuddy2api | hawklithm | workbuddy2api | publish.yml | pypi | workbuddy2api-v2.0.8.dev1 |

The PyPI owner must create pending trusted publishers for the two new projects,
and verify that the existing workbuddy2api trusted publisher covers `publish.yml`
and environment `pypi`. Configure these in the PyPI account; do not put PyPI
API tokens in the repository or chat. GitHub `pypi` environment restrictions,
if configured, may require an authorized reviewer to approve the publish job.
The existing workbuddy2api release/manual workflow remains available.

After the first two packages exist on PyPI, normal resolution uses package
metadata, not local wheel paths. To test the full released dependency graph in
an isolated environment, using a Python 3.10+ interpreter:

```powershell
uv venv .venv-pypi-verify
uv pip install --python .venv-pypi-verify/Scripts/python.exe --pre --index-url https://pypi.org/simple "workbuddy2api==2.0.8.dev1"
uv pip check --python .venv-pypi-verify/Scripts/python.exe
.venv-pypi-verify/Scripts/python.exe -c "import importlib.metadata as m; import buddy_agent_auth.providers.trae.login, daily_checkin.trae_scheduler, codebuddy_proxy.__main__; print({n:m.version(n) for n in ('buddy-agent-auth','buddy-agent-checkin','workbuddy2api')})"
```

For future dev releases, increment each changed package's version and its
consumers' pre-release lower bounds, then tag the corresponding source commit.
PyPI version files are immutable; never reuse a version to represent new code.
The local wheel preparation script uses temporary UV_FIND_LINKS/UV_PRERELEASE
settings rather than editing pyproject.toml, and may refresh uv lock files.
