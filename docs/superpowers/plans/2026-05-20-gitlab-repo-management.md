# v1.3.0 — GitLab repo management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GitLab as a first-class repo source in the manager bot's create-project / create-team wizards, mirroring the existing GitHub flow under a shared `RepoProvider` Protocol.

**Architecture:** New `repo_provider.py` defines the Protocol + `RepoInfo` (moved from `github_client.py`, re-exported for compat). New `gitlab_client.py` mirrors `github_client.py` line-for-line (`glab` CLI primary, `httpx + GITLAB_TOKEN` fallback, `GIT_CONFIG_*` Bearer header for clone). Manager wizard gains one new state (provider picker) before the existing repo-source step, used by both create-project and create-team flows. Two global config fields (`gitlab_pat`, `gitlab_host`) and four team-mode block-list additions round it out.

**Tech Stack:** Python 3.10+, dataclasses, `typing.Protocol`, asyncio, `httpx` (already in `[create]` extra), pytest with `asyncio_mode=auto`, python-telegram-bot v20+, `glab` CLI (external; user-installed).

**Reference design:** [`docs/superpowers/specs/2026-05-20-gitlab-repo-management-design.md`](../specs/2026-05-20-gitlab-repo-management-design.md)

**Baseline before starting:** Whatever `pytest -q` reports on `dev` HEAD when the feature branch is cut. Record it in Task 1.

---

## Task ordering rationale

1. **Task 1** — branch + baseline. Setup.
2. **Tasks 2–3** — extract `RepoProvider` Protocol and move `RepoInfo`. Pure refactor with no behavior change; isolates the abstraction surface.
3. **Tasks 4–5** — config schema + CLI flags. No runtime behavior yet; just data plumbing.
4. **Tasks 6–13** — build `GitLabClient` incrementally, TDD-style. Each task is one method / one path through the dual-backend probe.
5. **Tasks 14–15** — security: env-scrub regression test + team-mode block-list additions. Independent of the wizard, so can land before the manager work.
6. **Tasks 16–20** — manager wizard wiring: helper, new state, project flow, team flow, setup wizard fields.
7. **Tasks 21–22** — end-to-end manager tests for both flows.
8. **Tasks 23–24** — docs + version bump + final smoke.

---

## Task 1: Branch + baseline pin

**Files:**
- None modified

- [ ] **Step 1: Cut the feature branch**

```bash
git checkout dev
git pull --ff-only
git checkout -b feat/v1.3.0-gitlab-repo-management
```

- [ ] **Step 2: Capture baseline test count**

Run: `pytest -q 2>&1 | tail -3`
Expected: `N passed[, M skipped][, K warnings] in X.XXs`

Record the exact numbers (passed / skipped / warnings) in a temporary scratch note. Every subsequent task's verification step compares against this baseline.

- [ ] **Step 3: Confirm `glab` is on PATH for live-mode test stubs** (optional check, no failure if absent)

Run: `command -v glab && glab --version || echo "glab not installed — tests will exercise httpx path only"`
Expected: Either a version string OR the "not installed" message. **Either is fine.** The test suite does not require `glab` to be installed; it mocks `_glab_available` and `_run_glab` directly.

No commit (setup only).

---

## Task 2: `repo_provider.py` — Protocol + `RepoInfo`

**Files:**
- Create: `src/link_project_to_chat/repo_provider.py`
- Test: `tests/test_repo_provider.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_provider.py`:

```python
"""RepoProvider Protocol + RepoInfo dataclass."""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.repo_provider import RepoInfo, RepoProvider


def test_repoinfo_fields():
    info = RepoInfo(
        name="my-app",
        full_name="acme/my-app",
        html_url="https://example.com/acme/my-app",
        clone_url="https://example.com/acme/my-app.git",
        description="An app",
        private=True,
    )
    assert info.name == "my-app"
    assert info.full_name == "acme/my-app"
    assert info.private is True


def test_github_client_satisfies_protocol():
    """Existing GitHubClient must structurally implement RepoProvider."""
    from link_project_to_chat.github_client import GitHubClient

    # Structural check: every method on the Protocol exists on the class.
    for name in ("list_repos", "validate_repo_url", "clone_repo", "close"):
        assert callable(getattr(GitHubClient, name)), f"GitHubClient.{name} missing"


def test_protocol_has_expected_methods():
    """Pin the Protocol surface so a future rename is caught by tests."""
    for name in ("list_repos", "validate_repo_url", "clone_repo", "close"):
        assert hasattr(RepoProvider, name), f"RepoProvider.{name} missing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_repo_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'link_project_to_chat.repo_provider'`

- [ ] **Step 3: Create `src/link_project_to_chat/repo_provider.py`**

```python
"""Provider-neutral repo abstractions.

`RepoInfo` carries the minimal fields the manager wizard needs to display,
validate, and clone a repo from any forge. `RepoProvider` is a structural
Protocol both `GitHubClient` and `GitLabClient` satisfy — the wizard only
talks to this surface so per-provider branching is confined to a single
factory helper in manager/bot.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    clone_url: str
    description: str
    private: bool


class RepoProvider(Protocol):
    async def list_repos(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]: ...
    async def validate_repo_url(self, url: str) -> RepoInfo | None: ...
    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None: ...
    async def close(self) -> None: ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repo_provider.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/repo_provider.py tests/test_repo_provider.py
git commit -m "feat(repo_provider): add Protocol + RepoInfo dataclass"
```

---

## Task 3: `github_client.py` — re-export `RepoInfo` from `repo_provider`

**Files:**
- Modify: `src/link_project_to_chat/github_client.py:22-29` (replace local `RepoInfo` with re-export)

- [ ] **Step 1: Verify the existing tests pass before changing anything**

Run: `pytest tests/test_github_client.py -v 2>&1 | tail -5`
Expected: All tests PASS (this is the green baseline we preserve).

- [ ] **Step 2: Modify `src/link_project_to_chat/github_client.py`**

Find the existing `RepoInfo` dataclass (lines 22-29):

```python
@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    clone_url: str
    description: str
    private: bool
```

Replace with a re-export:

```python
# RepoInfo moved to repo_provider.py for cross-provider sharing.
# Re-exported here so legacy imports (`from .github_client import RepoInfo`)
# keep working. New code should prefer `from .repo_provider import RepoInfo`.
from .repo_provider import RepoInfo  # noqa: F401
```

Also remove the now-unused `from dataclasses import dataclass` line if `dataclass` is no longer referenced anywhere else in the file. (Grep first: `pytest -p no:cacheprovider --collect-only` won't tell you; just `grep -n "dataclass" src/link_project_to_chat/github_client.py`. If only the import line and the deleted decorator referenced it, drop the import.)

- [ ] **Step 3: Run the full GitHub test file to verify no regression**

Run: `pytest tests/test_github_client.py -v 2>&1 | tail -5`
Expected: All tests PASS (same count as Step 1).

- [ ] **Step 4: Run the broader test suite to catch any consumer-side breakage**

Run: `pytest -q 2>&1 | tail -3`
Expected: Baseline count from Task 1 + 3 new tests from Task 2, **no failures**.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/github_client.py
git commit -m "refactor(github_client): re-export RepoInfo from repo_provider"
```

---

## Task 4: Config — `gitlab_pat` and `gitlab_host` fields with round-trip

**Files:**
- Modify: `src/link_project_to_chat/config.py:485` (add fields next to `github_pat`)
- Modify: `src/link_project_to_chat/config.py` (loader near `:1144`, saver near `:1407`)
- Test: `tests/test_config.py` (add tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (append at file end):

```python
def test_gitlab_pat_round_trips(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"gitlab_pat": "glpat-secret"}))
    config = load_config(cfg_file)
    assert config.gitlab_pat == "glpat-secret"
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["gitlab_pat"] == "glpat-secret"


def test_gitlab_host_defaults_to_gitlab_com(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({}))
    config = load_config(cfg_file)
    assert config.gitlab_host == "gitlab.com"


def test_gitlab_host_default_is_omitted_from_saved_json(tmp_path: Path):
    """Keep config.json minimal for the common case."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({}))
    config = load_config(cfg_file)
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "gitlab_host" not in raw


def test_gitlab_host_custom_is_persisted(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"gitlab_host": "gitlab.example.com"}))
    config = load_config(cfg_file)
    assert config.gitlab_host == "gitlab.example.com"
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["gitlab_host"] == "gitlab.example.com"


def test_empty_gitlab_pat_is_omitted_from_saved_json(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({}))
    config = load_config(cfg_file)
    assert config.gitlab_pat == ""
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "gitlab_pat" not in raw
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_gitlab_pat_round_trips tests/test_config.py::test_gitlab_host_defaults_to_gitlab_com -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'gitlab_pat'`

- [ ] **Step 3: Add fields to `Config` dataclass**

Edit `src/link_project_to_chat/config.py`, around line 485. Find:

```python
@dataclass
class Config:
    github_pat: str = ""
```

Insert the two new fields right after `github_pat`:

```python
@dataclass
class Config:
    github_pat: str = ""
    gitlab_pat: str = ""
    gitlab_host: str = "gitlab.com"
```

- [ ] **Step 4: Add loader entries**

Find the existing loader line `config.github_pat = raw.get("github_pat", "")` (around `:1144`). Add two lines after it:

```python
config.github_pat = raw.get("github_pat", "")
config.gitlab_pat = raw.get("gitlab_pat", "")
config.gitlab_host = raw.get("gitlab_host", "gitlab.com")
```

- [ ] **Step 5: Add saver entries (omit defaults)**

Find the existing saver block (around `:1407`):

```python
if config.github_pat:
    raw["github_pat"] = config.github_pat
else:
    raw.pop("github_pat", None)
```

Add immediately after:

```python
if config.gitlab_pat:
    raw["gitlab_pat"] = config.gitlab_pat
else:
    raw.pop("gitlab_pat", None)
if config.gitlab_host and config.gitlab_host != "gitlab.com":
    raw["gitlab_host"] = config.gitlab_host
else:
    raw.pop("gitlab_host", None)
```

- [ ] **Step 6: Run all 5 new tests to verify they pass**

Run: `pytest tests/test_config.py -k "gitlab" -v`
Expected: 5 PASS.

- [ ] **Step 7: Run the full test suite to confirm no regression**

Run: `pytest -q 2>&1 | tail -3`
Expected: Baseline + 8 new tests (3 from Task 2 + 5 from this task), no failures.

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): add gitlab_pat and gitlab_host fields"
```

---

## Task 5: Config — `gitlab_host` loader normalization

**Files:**
- Modify: `src/link_project_to_chat/config.py` (loader)
- Test: `tests/test_config.py` (add tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_gitlab_host_strips_scheme(tmp_path: Path, caplog):
    """A user who manually edits config.json may include a scheme. Strip it."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"gitlab_host": "https://gitlab.example.com"}))
    with caplog.at_level("WARNING"):
        config = load_config(cfg_file)
    assert config.gitlab_host == "gitlab.example.com"
    assert any("gitlab_host" in r.message for r in caplog.records)


def test_gitlab_host_strips_trailing_slash(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"gitlab_host": "gitlab.example.com/"}))
    config = load_config(cfg_file)
    assert config.gitlab_host == "gitlab.example.com"


def test_gitlab_host_strips_path(tmp_path: Path, caplog):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"gitlab_host": "gitlab.example.com/some/path"}))
    with caplog.at_level("WARNING"):
        config = load_config(cfg_file)
    assert config.gitlab_host == "gitlab.example.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_gitlab_host_strips_scheme -v`
Expected: FAIL — config.gitlab_host == "https://gitlab.example.com" (no normalization yet).

- [ ] **Step 3: Add a normalization helper near the top of `config.py` (after existing helpers)**

Place this near `parse_user_bool` (around line 60), so other loaders/CLI handlers can reuse it:

```python
def _normalize_gitlab_host(raw: str) -> tuple[str, bool]:
    """Strip scheme, path, and trailing slash from a user-supplied host.

    Returns ``(normalized, was_cleaned)`` — ``was_cleaned`` is True iff
    the input contained any of those (caller logs a warning then).
    """
    if not isinstance(raw, str):
        return ("gitlab.com", False)
    cleaned = raw.strip()
    original = cleaned
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
    if "/" in cleaned:
        cleaned = cleaned.split("/", 1)[0]
    cleaned = cleaned.rstrip("/")
    if not cleaned:
        return ("gitlab.com", original != "")
    return (cleaned, cleaned != original)
```

- [ ] **Step 4: Use the helper in the loader**

Replace the line from Task 4:

```python
config.gitlab_host = raw.get("gitlab_host", "gitlab.com")
```

with:

```python
gitlab_host_raw = raw.get("gitlab_host", "gitlab.com")
normalized, was_cleaned = _normalize_gitlab_host(gitlab_host_raw)
if was_cleaned:
    logger.warning(
        "gitlab_host %r had scheme/path/slash; normalized to %r",
        gitlab_host_raw, normalized,
    )
config.gitlab_host = normalized
```

- [ ] **Step 5: Run the 3 new tests to verify they pass**

Run: `pytest tests/test_config.py -k "gitlab_host_strips" -v`
Expected: 3 PASS.

- [ ] **Step 6: Run the full test suite**

Run: `pytest -q 2>&1 | tail -3`
Expected: Baseline + previous + 3 new tests, no failures.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): normalize gitlab_host on load (strip scheme/path)"
```

---

## Task 6: CLI — `configure --gitlab-pat` and `--gitlab-host` flags

**Files:**
- Modify: `src/link_project_to_chat/cli.py` (configure subcommand)
- Test: `tests/test_cli.py` (add tests; create file if absent — check first with `ls tests/test_cli.py`)

- [ ] **Step 1: Locate the existing `--github-pat` flag definition**

Run: `grep -n "github-pat\|github_pat" src/link_project_to_chat/cli.py`
Expected: lines showing where `--github-pat` is added to the `configure` subparser, and where it's applied to the loaded config.

- [ ] **Step 2: Write the failing test**

If `tests/test_cli.py` exists, append. If not, create with this header:

```python
"""CLI configure subcommand — gitlab flags."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.cli import main as cli_main


def test_configure_sets_gitlab_pat(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}))
    monkeypatch.setattr("sys.argv", [
        "lptc", "configure", "--config", str(cfg), "--gitlab-pat", "glpat-secret",
    ])
    cli_main()
    raw = json.loads(cfg.read_text())
    assert raw["gitlab_pat"] == "glpat-secret"


def test_configure_sets_gitlab_host(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}))
    monkeypatch.setattr("sys.argv", [
        "lptc", "configure", "--config", str(cfg), "--gitlab-host", "gitlab.example.com",
    ])
    cli_main()
    raw = json.loads(cfg.read_text())
    assert raw["gitlab_host"] == "gitlab.example.com"


def test_configure_rejects_gitlab_host_with_scheme(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}))
    monkeypatch.setattr("sys.argv", [
        "lptc", "configure", "--config", str(cfg),
        "--gitlab-host", "https://gitlab.example.com",
    ])
    with pytest.raises(SystemExit):
        cli_main()
    err = capsys.readouterr().err.lower()
    assert "scheme" in err or "host" in err
```

> **Note:** if the existing `configure` subcommand uses a different invocation convention (e.g. `lptc configure --config <path>` vs. `lptc configure <path>`), match it. Inspect another `test_configure_*` test in the file (or grep `argparse` in cli.py) before writing the test, and adjust the `sys.argv` lists above.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_cli.py -k "gitlab" -v`
Expected: FAIL with `error: unrecognized arguments: --gitlab-pat`.

- [ ] **Step 4: Add the flags to the `configure` subparser**

Find the `configure` argparse setup in `cli.py`. Next to the existing `--github-pat` flag, add:

```python
configure_parser.add_argument(
    "--gitlab-pat",
    help="GitLab personal access token (stored in config.json with 0o600 perms).",
)
configure_parser.add_argument(
    "--gitlab-host",
    help="GitLab host (default: gitlab.com). Bare hostname only — no scheme, no path.",
)
```

- [ ] **Step 5: Wire the flags into the configure handler**

In the `configure` handler (the function called when `args.command == "configure"`), find the block that applies `--github-pat` to the loaded config. Add the gitlab handling right after it:

```python
if args.gitlab_pat is not None:
    config.gitlab_pat = args.gitlab_pat

if args.gitlab_host is not None:
    if "://" in args.gitlab_host or "/" in args.gitlab_host.rstrip("/"):
        parser.error(
            "--gitlab-host must be a bare hostname (e.g. gitlab.example.com); "
            "no scheme, no path."
        )
    config.gitlab_host = args.gitlab_host.rstrip("/")
```

(`parser.error` exits with status 2 and writes to stderr — matches the existing convention.)

- [ ] **Step 6: Run the 3 new tests to verify they pass**

Run: `pytest tests/test_cli.py -k "gitlab" -v`
Expected: 3 PASS.

- [ ] **Step 7: Run the full test suite**

Run: `pytest -q 2>&1 | tail -3`
Expected: Baseline + 14 new tests cumulatively, no failures.

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/cli.py tests/test_cli.py
git commit -m "feat(cli): add configure --gitlab-pat / --gitlab-host flags"
```

---

## Task 7: `gitlab_client.py` skeleton — `_glab_available`, `_run_glab`, `_redact_secrets`

**Files:**
- Create: `src/link_project_to_chat/gitlab_client.py`
- Test: `tests/test_gitlab_client.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_gitlab_client.py`:

```python
"""GitLab client — module-level helpers."""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_glab_available_when_not_on_path():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value=None):
        assert gitlab_client._glab_available() is False


def test_glab_available_when_not_authenticated():
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=1)
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is False


def test_glab_available_when_ready():
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=0)
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is True


def test_redact_secrets_redacts_raw_pat():
    from link_project_to_chat.gitlab_client import _redact_secrets
    out = _redact_secrets("error: glpat-abc123", "glpat-abc123", host="gitlab.com")
    assert "glpat-abc123" not in out
    assert "[REDACTED]" in out


def test_redact_secrets_redacts_base64_form():
    """Defense-in-depth: redact base64(x-access-token:PAT) even though GitLab uses Bearer."""
    import base64
    from link_project_to_chat.gitlab_client import _redact_secrets
    encoded = base64.b64encode(b"x-access-token:glpat-abc").decode()
    out = _redact_secrets(f"leak: {encoded}", "glpat-abc", host="gitlab.com")
    assert encoded not in out


def test_redact_secrets_redacts_credential_url_default_host():
    from link_project_to_chat.gitlab_client import _redact_secrets
    out = _redact_secrets(
        "fatal: https://glpat-x@gitlab.com/g/p.git", "glpat-x", host="gitlab.com"
    )
    assert "glpat-x" not in out
    assert "[REDACTED]@gitlab.com" in out


def test_redact_secrets_redacts_credential_url_self_hosted():
    from link_project_to_chat.gitlab_client import _redact_secrets
    out = _redact_secrets(
        "fatal: https://glpat-x@gitlab.example.com/g/p.git",
        "glpat-x",
        host="gitlab.example.com",
    )
    assert "glpat-x" not in out
    assert "[REDACTED]@gitlab.example.com" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gitlab_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'link_project_to_chat.gitlab_client'`.

- [ ] **Step 3: Create `src/link_project_to_chat/gitlab_client.py`**

```python
"""GitLab client — mirrors github_client.py.

Prefers the `glab` CLI when installed + authenticated; falls back to
`httpx + GITLAB_TOKEN`. Used by the manager wizard's create-project /
create-team flows when the operator picks GitLab in the provider picker.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from .repo_provider import RepoInfo

logger = logging.getLogger(__name__)


def _glab_available() -> bool:
    """Check if glab CLI is installed and authenticated."""
    glab_path = shutil.which("glab")
    if glab_path is None:
        return False
    try:
        proc = subprocess.run(
            [glab_path, "auth", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


async def _run_glab(*args: str) -> tuple[int, str, str]:
    """Run a glab CLI command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "glab", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def _redact_secrets(text: str, *secrets: str, host: str) -> str:
    """Strip raw PATs, their base64 forms, and credential-URL forms from text."""
    redacted = text
    for secret in secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, "[REDACTED]")
        encoded = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
        redacted = redacted.replace(encoded, "[REDACTED]")
    cred_url_re = re.compile(rf"https://[^/@\s]+@{re.escape(host)}")
    redacted = cred_url_re.sub(f"https://[REDACTED]@{host}", redacted)
    return redacted
```

- [ ] **Step 4: Run the 7 new tests to verify they pass**

Run: `pytest tests/test_gitlab_client.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/gitlab_client.py tests/test_gitlab_client.py
git commit -m "feat(gitlab_client): module skeleton — _glab_available + _redact_secrets"
```

---

## Task 8: `GitLabClient.__init__` + `close`

**Files:**
- Modify: `src/link_project_to_chat/gitlab_client.py`
- Test: `tests/test_gitlab_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gitlab_client.py`:

```python
def test_init_prefers_api_mode_when_pat_set():
    """With a PAT and httpx available, prefer the API path even if glab is auth'd."""
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=True):
        client = gitlab_client.GitLabClient(pat="glpat-test")
    assert client._use_glab is False
    assert client._client is not None


def test_init_uses_glab_when_no_pat_and_glab_available():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=True):
        client = gitlab_client.GitLabClient(pat="")
    assert client._use_glab is True
    assert client._client is None


def test_init_raises_when_no_pat_and_no_glab():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=False):
        with pytest.raises(ValueError, match="GitLab PAT required"):
            gitlab_client.GitLabClient(pat="")


def test_init_raises_when_no_httpx_and_no_glab(monkeypatch):
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "httpx", None)
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=False):
        with pytest.raises(ImportError, match="Neither glab CLI nor httpx"):
            gitlab_client.GitLabClient(pat="glpat-test")


def test_init_custom_host_used_for_api_base_url():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=False):
        client = gitlab_client.GitLabClient(pat="glpat-test", host="gitlab.example.com")
    assert str(client._client.base_url) == "https://gitlab.example.com/api/v4"


async def test_close_is_idempotent_when_glab_mode():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=True):
        client = gitlab_client.GitLabClient(pat="")
    await client.close()  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitlab_client.py::test_init_prefers_api_mode_when_pat_set -v`
Expected: FAIL with `AttributeError: module 'link_project_to_chat.gitlab_client' has no attribute 'GitLabClient'`.

- [ ] **Step 3: Add the `GitLabClient` class to `gitlab_client.py`**

Append after the module-level helpers:

```python
class GitLabClient:
    """GitLab client that uses glab CLI if available, falls back to PAT + httpx."""

    def __init__(self, pat: str = "", host: str = "gitlab.com"):
        self._pat = pat
        self._host = host
        prefer_api = bool(pat) and httpx is not None
        self._use_glab = _glab_available() and not prefer_api
        self._client = None
        if not self._use_glab:
            if httpx is None:
                raise ImportError(
                    "Neither glab CLI nor httpx available. "
                    "Install glab (https://gitlab.com/gitlab-org/cli) or run: "
                    "pip install link-project-to-chat[create]"
                )
            if not pat:
                raise ValueError("GitLab PAT required when glab CLI is not available.")
            self._client = httpx.AsyncClient(
                base_url=f"https://{host}/api/v4",
                headers={"PRIVATE-TOKEN": pat},
                timeout=30.0,
            )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
```

- [ ] **Step 4: Run all gitlab-client tests**

Run: `pytest tests/test_gitlab_client.py -v`
Expected: 13 PASS (7 from Task 7 + 6 from this task).

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/gitlab_client.py tests/test_gitlab_client.py
git commit -m "feat(gitlab_client): GitLabClient __init__ + close"
```

---

## Task 9: `GitLabClient.list_repos` — httpx path

**Files:**
- Modify: `src/link_project_to_chat/gitlab_client.py`
- Test: `tests/test_gitlab_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gitlab_client.py`:

```python
@pytest.fixture
def gl_api_client(monkeypatch):
    """Force httpx (API) mode."""
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    return gitlab_client.GitLabClient(pat="glpat-test123")


def _mock_resp(status_code, json_data, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.headers = headers or {}
    return resp


async def test_list_repos_httpx_maps_fields(gl_api_client):
    projects = [
        {
            "path": "my-app",
            "path_with_namespace": "acme/my-app",
            "web_url": "https://gitlab.com/acme/my-app",
            "http_url_to_repo": "https://gitlab.com/acme/my-app.git",
            "description": "An app",
            "visibility": "private",
        },
        {
            "path": "lib",
            "path_with_namespace": "acme/team/lib",
            "web_url": "https://gitlab.com/acme/team/lib",
            "http_url_to_repo": "https://gitlab.com/acme/team/lib.git",
            "description": None,
            "visibility": "internal",
        },
        {
            "path": "open",
            "path_with_namespace": "acme/open",
            "web_url": "https://gitlab.com/acme/open",
            "http_url_to_repo": "https://gitlab.com/acme/open.git",
            "description": "",
            "visibility": "public",
        },
    ]
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, projects, {"link": ""}))
        repos, has_next = await gl_api_client.list_repos(page=1, per_page=5)
    assert [r.full_name for r in repos] == ["acme/my-app", "acme/team/lib", "acme/open"]
    assert [r.name for r in repos] == ["my-app", "lib", "open"]
    assert [r.private for r in repos] == [True, True, False]  # internal counts as non-public
    assert repos[1].description == ""  # None coerced to ""
    assert has_next is False


async def test_list_repos_httpx_detects_next_page(gl_api_client):
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(
            200, [],
            {"link": '<https://gitlab.com/api/v4/projects?page=2>; rel="next"'},
        ))
        _, has_next = await gl_api_client.list_repos(page=1, per_page=5)
    assert has_next is True


async def test_list_repos_httpx_auth_failure(gl_api_client):
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(401, {"message": "Unauthorized"}))
        with pytest.raises(Exception, match="GitLab API error 401"):
            await gl_api_client.list_repos()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitlab_client.py -k "list_repos_httpx" -v`
Expected: FAIL with `AttributeError: 'GitLabClient' object has no attribute 'list_repos'`.

- [ ] **Step 3: Add `list_repos` + `_list_repos_api` to `gitlab_client.py`**

Inside the `GitLabClient` class, before `close`:

```python
    async def list_repos(self, page: int = 1, per_page: int = 5) -> tuple[list[RepoInfo], bool]:
        if self._use_glab:
            return await self._list_repos_glab(page, per_page)
        return await self._list_repos_api(page, per_page)

    async def _list_repos_api(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        resp = await self._client.get(
            "/projects",
            params={
                "membership": "true",
                "order_by": "updated_at",
                "page": page,
                "per_page": per_page,
            },
        )
        if resp.status_code != 200:
            raise Exception(f"GitLab API error {resp.status_code}: {resp.json().get('message', '')}")
        repos = [_repo_info_from_project(p) for p in resp.json()]
        has_next = 'rel="next"' in resp.headers.get("link", "")
        return repos, has_next
```

Add the field-mapping helper at module level (after `_redact_secrets`):

```python
def _repo_info_from_project(p: dict) -> RepoInfo:
    """Map a GitLab `/projects` payload entry to RepoInfo."""
    return RepoInfo(
        name=p["path"],
        full_name=p["path_with_namespace"],
        html_url=p["web_url"],
        clone_url=p["http_url_to_repo"],
        description=p.get("description") or "",
        private=p.get("visibility", "private") != "public",
    )
```

- [ ] **Step 4: Run the 3 new tests**

Run: `pytest tests/test_gitlab_client.py -k "list_repos_httpx" -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/gitlab_client.py tests/test_gitlab_client.py
git commit -m "feat(gitlab_client): list_repos httpx path + RepoInfo mapping"
```

---

## Task 10: `GitLabClient.list_repos` — glab CLI path

**Files:**
- Modify: `src/link_project_to_chat/gitlab_client.py`
- Test: `tests/test_gitlab_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gitlab_client.py`:

```python
@pytest.fixture
def gl_glab_client(monkeypatch):
    """Force glab CLI mode (no PAT)."""
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: True)
    return gitlab_client.GitLabClient(pat="")


async def test_list_repos_glab_parses_link_header_and_body(gl_glab_client):
    body = json.dumps([
        {"path": "p1", "path_with_namespace": "u/p1", "web_url": "https://gitlab.com/u/p1",
         "http_url_to_repo": "https://gitlab.com/u/p1.git", "description": "", "visibility": "private"},
    ])
    headers = (
        'HTTP/2.0 200 OK\r\n'
        'Link: <https://gitlab.com/api/v4/projects?page=2>; rel="next"\r\n'
    )
    stdout = headers + "\r\n" + body
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(0, stdout, ""))) as mock_run:
        repos, has_next = await gl_glab_client.list_repos(page=1, per_page=5)
    assert [r.full_name for r in repos] == ["u/p1"]
    assert has_next is True
    # Argv check: must request --include for headers.
    args = mock_run.await_args.args
    assert args[0] == "api"
    assert "--include" in args
    assert any("projects" in a and "membership=true" in a for a in args)


async def test_list_repos_glab_no_next_page(gl_glab_client):
    body = json.dumps([])
    stdout = "HTTP/2.0 200 OK\r\n\r\n" + body
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(0, stdout, ""))):
        repos, has_next = await gl_glab_client.list_repos(page=1, per_page=5)
    assert repos == []
    assert has_next is False


async def test_list_repos_glab_failure_raises(gl_glab_client):
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(1, "", "auth error"))):
        with pytest.raises(Exception, match="glab api .* failed"):
            await gl_glab_client.list_repos()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitlab_client.py -k "list_repos_glab" -v`
Expected: FAIL with `AttributeError: 'GitLabClient' object has no attribute '_list_repos_glab'`.

- [ ] **Step 3: Add `_list_repos_glab` to the class**

```python
    async def _list_repos_glab(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        # `glab api --include` emits HTTP headers + body the same shape as `gh api --include`.
        code, stdout, stderr = await _run_glab(
            "api", "--include",
            f"projects?membership=true&order_by=updated_at&page={page}&per_page={per_page}",
        )
        if code != 0:
            raise Exception(f"glab api projects failed: {stderr}")
        sep = "\r\n\r\n" if "\r\n\r\n" in stdout else "\n\n"
        headers_part, _, body_part = stdout.partition(sep)
        if not body_part:
            raise Exception("glab api returned no body")
        has_next = 'rel="next"' in headers_part
        repos = [_repo_info_from_project(p) for p in json.loads(body_part)]
        return repos, has_next
```

- [ ] **Step 4: Run the 3 new tests**

Run: `pytest tests/test_gitlab_client.py -k "list_repos_glab" -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/gitlab_client.py tests/test_gitlab_client.py
git commit -m "feat(gitlab_client): list_repos glab CLI path"
```

---

## Task 11: `GitLabClient.validate_repo_url` — both paths + subgroup support

**Files:**
- Modify: `src/link_project_to_chat/gitlab_client.py`
- Test: `tests/test_gitlab_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gitlab_client.py`:

```python
async def test_validate_repo_url_gitlab_com(gl_api_client):
    project = {
        "path": "myproj",
        "path_with_namespace": "owner/myproj",
        "web_url": "https://gitlab.com/owner/myproj",
        "http_url_to_repo": "https://gitlab.com/owner/myproj.git",
        "description": "ok",
        "visibility": "private",
    }
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, project))
        info = await gl_api_client.validate_repo_url("https://gitlab.com/owner/myproj")
    assert info is not None
    assert info.full_name == "owner/myproj"
    # URL-encoded namespaced path in the lookup
    call = mock_client.get.await_args
    assert "projects/owner%2Fmyproj" in call.args[0]


async def test_validate_repo_url_subgroup(gl_api_client):
    project = {
        "path": "leaf",
        "path_with_namespace": "group/sub1/sub2/leaf",
        "web_url": "https://gitlab.com/group/sub1/sub2/leaf",
        "http_url_to_repo": "https://gitlab.com/group/sub1/sub2/leaf.git",
        "description": "",
        "visibility": "internal",
    }
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, project))
        info = await gl_api_client.validate_repo_url(
            "https://gitlab.com/group/sub1/sub2/leaf"
        )
    assert info is not None
    assert info.full_name == "group/sub1/sub2/leaf"
    call = mock_client.get.await_args
    assert "projects/group%2Fsub1%2Fsub2%2Fleaf" in call.args[0]


async def test_validate_repo_url_self_hosted(monkeypatch):
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    client = gitlab_client.GitLabClient(pat="glpat-x", host="gitlab.example.com")
    project = {
        "path": "p",
        "path_with_namespace": "g/p",
        "web_url": "https://gitlab.example.com/g/p",
        "http_url_to_repo": "https://gitlab.example.com/g/p.git",
        "description": "",
        "visibility": "private",
    }
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, project))
        info = await client.validate_repo_url("https://gitlab.example.com/g/p")
    assert info is not None
    assert info.html_url == "https://gitlab.example.com/g/p"


async def test_validate_repo_url_rejects_github(gl_api_client):
    info = await gl_api_client.validate_repo_url("https://github.com/owner/repo")
    assert info is None


async def test_validate_repo_url_rejects_wrong_host(monkeypatch):
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    client = gitlab_client.GitLabClient(pat="glpat-x", host="gitlab.example.com")
    # gitlab.com URL given to a self-hosted client → reject
    info = await client.validate_repo_url("https://gitlab.com/owner/repo")
    assert info is None


async def test_validate_repo_url_rejects_bare_path(gl_api_client):
    info = await gl_api_client.validate_repo_url("not-a-url")
    assert info is None


async def test_validate_repo_url_not_found(gl_api_client):
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(404, {"message": "Not Found"}))
        info = await gl_api_client.validate_repo_url("https://gitlab.com/x/y")
    assert info is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitlab_client.py -k "validate_repo_url" -v`
Expected: FAIL with `AttributeError: 'GitLabClient' object has no attribute 'validate_repo_url'`.

- [ ] **Step 3: Add the regex factory + `validate_repo_url` to the class**

Add this near the top of `gitlab_client.py` (after `_redact_secrets`):

```python
def _gitlab_url_re(host: str) -> "re.Pattern[str]":
    """Compile a per-host URL regex. Supports subgroups via the multi-segment capture."""
    return re.compile(rf"https?://{re.escape(host)}/((?:[^/\s]+/)+?[^/\s]+?)(?:\.git)?/?$")
```

Add to the class:

```python
    async def validate_repo_url(self, url: str) -> RepoInfo | None:
        match = _gitlab_url_re(self._host).match(url.strip())
        if not match:
            return None
        full_path = match.group(1)
        if self._use_glab:
            return await self._validate_glab(full_path)
        return await self._validate_api(full_path)

    async def _validate_api(self, full_path: str) -> RepoInfo | None:
        from urllib.parse import quote
        encoded = quote(full_path, safe="")
        resp = await self._client.get(f"/projects/{encoded}")
        if resp.status_code != 200:
            return None
        return _repo_info_from_project(resp.json())

    async def _validate_glab(self, full_path: str) -> RepoInfo | None:
        from urllib.parse import quote
        encoded = quote(full_path, safe="")
        code, stdout, stderr = await _run_glab("api", f"projects/{encoded}")
        if code != 0:
            return None
        return _repo_info_from_project(json.loads(stdout))
```

- [ ] **Step 4: Run the 7 new tests**

Run: `pytest tests/test_gitlab_client.py -k "validate_repo_url" -v`
Expected: 7 PASS.

- [ ] **Step 5: Run the whole gitlab-client test file**

Run: `pytest tests/test_gitlab_client.py -v 2>&1 | tail -5`
Expected: 23 PASS (cumulative).

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/gitlab_client.py tests/test_gitlab_client.py
git commit -m "feat(gitlab_client): validate_repo_url with subgroup + self-host support"
```

---

## Task 12: `GitLabClient.clone_repo` — git+`GIT_CONFIG_*` Bearer header path

**Files:**
- Modify: `src/link_project_to_chat/gitlab_client.py`
- Test: `tests/test_gitlab_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gitlab_client.py`:

```python
class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes = b"", stdout: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, self._stderr


async def test_clone_repo_api_mode_injects_bearer_via_git_config(
    gl_api_client, tmp_path: Path,
):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.com/u/p",
        clone_url="https://gitlab.com/u/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(0)),
    ) as mock_exec:
        await gl_api_client.clone_repo(repo, tmp_path / "p")

    args = mock_exec.await_args.args
    kwargs = mock_exec.await_args.kwargs
    assert args[:3] == ("git", "clone", "https://gitlab.com/u/p.git")
    assert all("glpat-test123" not in str(a) for a in args)
    env = kwargs["env"]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "AUTHORIZATION: Bearer glpat-test123"


async def test_clone_repo_api_mode_redacts_pat_in_errors(gl_api_client, tmp_path: Path):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.com/u/p",
        clone_url="https://gitlab.com/u/p.git",
        description="", private=True,
    )
    stderr = b"fatal: https://glpat-test123@gitlab.com/u/p.git access denied"
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(1, stderr=stderr)),
    ):
        with pytest.raises(Exception, match="git clone failed") as exc:
            await gl_api_client.clone_repo(repo, tmp_path / "p")
    assert "glpat-test123" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


async def test_clone_repo_api_mode_uses_self_hosted_host_in_git_config(
    monkeypatch, tmp_path: Path,
):
    from link_project_to_chat import gitlab_client
    from link_project_to_chat.repo_provider import RepoInfo

    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    client = gitlab_client.GitLabClient(pat="glpat-x", host="gitlab.example.com")
    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.example.com/u/p",
        clone_url="https://gitlab.example.com/u/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(0)),
    ) as mock_exec:
        await client.clone_repo(repo, tmp_path / "p")

    env = mock_exec.await_args.kwargs["env"]
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.example.com/.extraHeader"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitlab_client.py -k "clone_repo_api_mode" -v`
Expected: FAIL with `AttributeError: 'GitLabClient' object has no attribute 'clone_repo'`.

- [ ] **Step 3: Add `_git_auth_env` helper + `clone_repo` (api branch)**

Add at module level (after `_repo_info_from_project`):

```python
def _git_auth_env(pat: str, host: str) -> dict[str, str]:
    """Inject a one-shot GitLab auth header via env, not argv.

    Uses ``Authorization: Bearer {pat}`` (GitLab convention). The GitHub
    equivalent uses ``basic`` with a base64'd ``x-access-token:{pat}`` pair —
    these MUST differ.
    """
    env = os.environ.copy()
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = f"http.https://{host}/.extraHeader"
    env[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: Bearer {pat}"
    return env
```

Add to the `GitLabClient` class (before `close`):

```python
    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self._use_glab:
            await self._clone_glab(repo, dest)
            return
        env = _git_auth_env(self._pat, self._host) if self._pat else None
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", repo.clone_url, str(dest),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(
                "git clone failed: "
                + _redact_secrets(stderr.decode().strip(), self._pat, host=self._host)
            )
```

(`_clone_glab` will be added in the next task — for now, the api path is what the tests cover.)

- [ ] **Step 4: Run the 3 new tests**

Run: `pytest tests/test_gitlab_client.py -k "clone_repo_api_mode" -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/gitlab_client.py tests/test_gitlab_client.py
git commit -m "feat(gitlab_client): clone_repo httpx path with GIT_CONFIG Bearer header"
```

---

## Task 13: `GitLabClient.clone_repo` — `glab repo clone` path

**Files:**
- Modify: `src/link_project_to_chat/gitlab_client.py`
- Test: `tests/test_gitlab_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gitlab_client.py`:

```python
async def test_clone_repo_glab_mode_uses_full_name(gl_glab_client, tmp_path: Path):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="group/sub/p",
        html_url="https://gitlab.com/group/sub/p",
        clone_url="https://gitlab.com/group/sub/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(0)),
    ) as mock_exec:
        await gl_glab_client.clone_repo(repo, tmp_path / "p")

    args = mock_exec.await_args.args
    assert args[0] == "glab"
    assert args[1:3] == ("repo", "clone")
    assert args[3] == "group/sub/p"
    assert args[4] == str(tmp_path / "p")


async def test_clone_repo_glab_mode_failure_raises(gl_glab_client, tmp_path: Path):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.com/u/p",
        clone_url="https://gitlab.com/u/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(1, stderr=b"glab: permission denied")),
    ):
        with pytest.raises(Exception, match="glab repo clone failed"):
            await gl_glab_client.clone_repo(repo, tmp_path / "p")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitlab_client.py -k "clone_repo_glab_mode" -v`
Expected: FAIL — `_clone_glab` doesn't exist yet.

- [ ] **Step 3: Implement `_clone_glab`**

Add to the `GitLabClient` class (just before the existing `clone_repo`):

```python
    async def _clone_glab(self, repo: RepoInfo, dest: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "glab", "repo", "clone", repo.full_name, str(dest),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(
                "glab repo clone failed: "
                + _redact_secrets(stderr.decode().strip(), self._pat, host=self._host)
            )
```

- [ ] **Step 4: Run the 2 new tests**

Run: `pytest tests/test_gitlab_client.py -k "clone_repo_glab_mode" -v`
Expected: 2 PASS.

- [ ] **Step 5: Run the full gitlab-client suite**

Run: `pytest tests/test_gitlab_client.py -v 2>&1 | tail -5`
Expected: 28 PASS cumulative (7+6+3+3+7+3+2).

- [ ] **Step 6: Run the broader suite**

Run: `pytest -q 2>&1 | tail -3`
Expected: baseline + Task 2 (3) + Task 4 (5) + Task 5 (3) + Task 6 (3) + Task 7-13 (28) = baseline + 45 new tests. No failures.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/gitlab_client.py tests/test_gitlab_client.py
git commit -m "feat(gitlab_client): clone_repo via glab CLI"
```

---

## Task 14: Security — `GITLAB_TOKEN` env-scrub regression test

**Files:**
- Modify: `tests/test_security.py:158` neighborhood (add gitlab block)
- Modify: `tests/backends/test_base_backend.py:34` neighborhood (add gitlab block)
- No production code change expected — the generic `*_TOKEN` pattern in `BaseBackend._prepare_env` already covers `GITLAB_TOKEN`.

- [ ] **Step 1: Read the existing `GITHUB_TOKEN` block in `tests/test_security.py`**

Run: `grep -n "GITHUB_TOKEN" tests/test_security.py`
Expected: one or two test functions referencing `GITHUB_TOKEN`. Note their structure (typically a monkeypatch + assert-not-in-env pair).

- [ ] **Step 2: Add a `GITLAB_TOKEN` test next to it**

Open `tests/test_security.py` and find the existing block setting `GITHUB_TOKEN`. Add immediately below (mirroring its style):

```python
async def test_gitlab_token_not_passed_to_agent_subprocess(monkeypatch):
    """GITLAB_TOKEN must be scrubbed from the subprocess env like GITHUB_TOKEN."""
    # NOTE: This relies on the generic `*_TOKEN` pattern in BaseBackend._prepare_env.
    # If the env-scrub regresses for the gitlab case specifically, this test catches it.
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-" + "B" * 36)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")  # control sample
    from link_project_to_chat.backends.base import BaseBackend

    env = BaseBackend._prepare_env({})
    assert "GITLAB_TOKEN" not in env, "GITLAB_TOKEN leaked into agent env"
    assert "AWS_ACCESS_KEY_ID" not in env, "AWS_ACCESS_KEY_ID leaked (regression check)"
```

> **Note:** if the existing test uses a different invocation (e.g. `prepare_env` instance method, or a different module path), match the existing GitHub test's exact pattern. Inspect the GitHub test before adding this one.

- [ ] **Step 3: Add the parallel test to `tests/backends/test_base_backend.py:34` neighborhood**

Find the existing `monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")` block. Add an analogous gitlab block immediately after:

```python
def test_prepare_env_strips_gitlab_token(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glab-secret")
    from link_project_to_chat.backends.base import BaseBackend

    env = BaseBackend._prepare_env({})
    assert "GITLAB_TOKEN" not in env
```

- [ ] **Step 4: Run the new tests — they should PASS without any source change**

Run: `pytest tests/test_security.py -k "gitlab_token" tests/backends/test_base_backend.py -k "gitlab_token" -v`
Expected: 2 PASS (existing `*_TOKEN` regex already strips `GITLAB_TOKEN`).

> **If these tests fail:** the env-scrub does NOT currently catch `GITLAB_TOKEN`. In that case, inspect [`src/link_project_to_chat/backends/base.py`](../../src/link_project_to_chat/backends/base.py) and either widen the existing regex or add `GITLAB_TOKEN` to the explicit strip-list. Then update this task's plan and re-run.

- [ ] **Step 5: Commit**

```bash
git add tests/test_security.py tests/backends/test_base_backend.py
git commit -m "test(security): pin GITLAB_TOKEN env-scrub regression"
```

---

## Task 15: Team-mode block list — `glab mr create`, `glab mr merge`, `glab release create`, `glab ci run`

**Files:**
- Modify: `src/link_project_to_chat/backends/claude.py:104-109` (extend `_TEAM_DISALLOWED_TOOLS`)
- Modify: `tests/backends/test_claude_backend.py` (extend existing assertions)

- [ ] **Step 1: Write / extend the failing test**

Open `tests/backends/test_claude_backend.py`. Find the existing assertions at `:52` and `:68` (`assert "Bash(gh pr create:*)" in blocked`). Right next to each, add the gitlab assertions:

```python
    assert "Bash(gh pr create:*)" in blocked
    assert "Bash(glab mr create:*)" in blocked   # NEW
```

Also extend whichever test covers `push` / `release` / `network` (search for `gh pr merge`, `gh release create`, `gh workflow run`). For each, add the gitlab sibling. The full diff (one assertion per existing gh-asserting test):

```python
    assert "Bash(gh pr merge:*)" in blocked
    assert "Bash(glab mr merge:*)" in blocked   # NEW

    assert "Bash(gh release create:*)" in blocked
    assert "Bash(glab release create:*)" in blocked   # NEW

    assert "Bash(gh workflow run:*)" in blocked
    assert "Bash(glab ci run:*)" in blocked   # NEW
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/backends/test_claude_backend.py -v 2>&1 | grep -E "FAIL|PASS" | head -10`
Expected: tests referencing the new `glab ...` strings FAIL.

- [ ] **Step 3: Extend `_TEAM_DISALLOWED_TOOLS` in `src/link_project_to_chat/backends/claude.py:104-109`**

Replace the existing dict:

```python
_TEAM_DISALLOWED_TOOLS: dict[str, tuple[str, ...]] = {
    "push": ("Bash(git push:*)", "Bash(git push)", "Bash(gh pr merge:*)"),
    "pr_create": ("Bash(gh pr create:*)",),
    "release": ("Bash(gh release create:*)",),
    "network": ("Bash(curl:*)", "Bash(wget:*)", "Bash(gh workflow run:*)"),
}
```

with:

```python
_TEAM_DISALLOWED_TOOLS: dict[str, tuple[str, ...]] = {
    "push": (
        "Bash(git push:*)", "Bash(git push)",
        "Bash(gh pr merge:*)", "Bash(glab mr merge:*)",
    ),
    "pr_create": (
        "Bash(gh pr create:*)",
        "Bash(glab mr create:*)",
    ),
    "release": (
        "Bash(gh release create:*)",
        "Bash(glab release create:*)",
    ),
    "network": (
        "Bash(curl:*)", "Bash(wget:*)",
        "Bash(gh workflow run:*)",
        "Bash(glab ci run:*)",
    ),
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/backends/test_claude_backend.py -v 2>&1 | tail -5`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/backends/claude.py tests/backends/test_claude_backend.py
git commit -m "feat(safety): block glab mr/release/ci in team mode"
```

---

## Task 16: Manager — `_build_repo_provider` helper + `STATE_CREATE_PROVIDER_PICK`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py` (add helper + state constant)
- Test: `tests/test_manager_create_gitlab.py` (new — start with the helper test only)

- [ ] **Step 1: Locate the existing `STATE_CREATE_*` constants and pick the next free int**

Run: `grep -n "STATE_CREATE" src/link_project_to_chat/manager/bot.py | head -20`
Expected: a series of `STATE_CREATE_NAME = N`, `STATE_CREATE_REPO_SOURCE = N+1`, ... Note the highest int currently used.

- [ ] **Step 2: Write the failing test**

Create `tests/test_manager_create_gitlab.py`:

```python
"""Manager — GitLab provider picker + wizard wiring."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_build_repo_provider_returns_github_client_by_default(tmp_path):
    from link_project_to_chat.config import Config
    from link_project_to_chat.github_client import GitHubClient
    from link_project_to_chat.manager.bot import _build_repo_provider

    config = Config(github_pat="ghp_x")
    ctx = MagicMock()
    ctx.user_data = {"create": {"provider": "github"}}

    provider = _build_repo_provider(ctx, config)
    assert isinstance(provider, GitHubClient)


def test_build_repo_provider_returns_gitlab_client_when_picked(monkeypatch, tmp_path):
    from link_project_to_chat.config import Config
    from link_project_to_chat.gitlab_client import GitLabClient
    from link_project_to_chat.manager.bot import _build_repo_provider
    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client._glab_available", lambda: False
    )

    config = Config(gitlab_pat="glpat-x", gitlab_host="gitlab.example.com")
    ctx = MagicMock()
    ctx.user_data = {"create": {"provider": "gitlab"}}

    provider = _build_repo_provider(ctx, config)
    assert isinstance(provider, GitLabClient)
    assert provider._host == "gitlab.example.com"


def test_provider_pick_state_constant_is_unique():
    from link_project_to_chat.manager import bot as manager_bot
    state_attrs = [
        a for a in dir(manager_bot)
        if a.startswith("STATE_CREATE_") and isinstance(getattr(manager_bot, a), int)
    ]
    state_values = [getattr(manager_bot, a) for a in state_attrs]
    assert "STATE_CREATE_PROVIDER_PICK" in state_attrs
    assert len(state_values) == len(set(state_values)), "duplicate state ints"
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_manager_create_gitlab.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_repo_provider'`.

- [ ] **Step 4: Add the state constant + helper to `manager/bot.py`**

In the constants block (near the other `STATE_CREATE_*` definitions), add (using the next free int from Step 1 — e.g. if highest is `STATE_CREATE_PERSONA_DEV = 21`, this becomes `22`):

```python
STATE_CREATE_PROVIDER_PICK = 22   # ← REPLACE 22 with the next free int from Step 1
```

> **Important:** ALSO add the state to the team-create flow's state constant block if the team flow uses a separate set (some codebases do, some don't). Inspect with `grep -n "STATE_CREATE_TEAM" src/link_project_to_chat/manager/bot.py`. If team has its own constants, add `STATE_CREATE_TEAM_PROVIDER_PICK = N+1`.

Add the helper as a module-level function (right above the `ManagerBot` class, so it's importable):

```python
def _build_repo_provider(ctx, config) -> "RepoProvider":
    """Construct a RepoProvider from the wizard's stored provider choice.

    Called at every site that previously instantiated GitHubClient directly.
    Reads ``ctx.user_data["create"]["provider"]`` — set by the new
    STATE_CREATE_PROVIDER_PICK callback.
    """
    provider = ctx.user_data.get("create", {}).get("provider", "github")
    if provider == "gitlab":
        from ..gitlab_client import GitLabClient
        return GitLabClient(pat=config.gitlab_pat, host=config.gitlab_host)
    from ..github_client import GitHubClient
    return GitHubClient(pat=config.github_pat)
```

Add the type-only import at the top of the file (under TYPE_CHECKING if needed):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..repo_provider import RepoProvider
```

- [ ] **Step 5: Run the 3 new tests**

Run: `pytest tests/test_manager_create_gitlab.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Run the full suite (no manager-bot regressions)**

Run: `pytest -q 2>&1 | tail -3`
Expected: baseline + cumulative new + 3, no failures.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_gitlab.py
git commit -m "feat(manager): _build_repo_provider helper + STATE_CREATE_PROVIDER_PICK"
```

---

## Task 17: Manager — wire provider picker into create-project flow

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py` (project-create conversation handler)
- Test: `tests/test_manager_create_gitlab.py`

- [ ] **Step 1: Locate the project-create flow's state map and the transition from `STATE_CREATE_NAME` to `STATE_CREATE_REPO_SOURCE`**

Run: `grep -n "STATE_CREATE_NAME\|STATE_CREATE_REPO_SOURCE" src/link_project_to_chat/manager/bot.py`
Expected: shows the handler callbacks and where each state transitions to the next.

- [ ] **Step 2: Write the failing wizard test**

Append to `tests/test_manager_create_gitlab.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def project_create_ctx(tmp_path):
    """A minimal ctx user_data for the project-create flow, after name capture."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    ctx = MagicMock()
    ctx.user_data = {"create": {"name": "myproj", "config_path": str(cfg)}}
    return ctx


async def test_project_create_shows_provider_picker_after_name(project_create_ctx):
    """The wizard transitions from name capture to STATE_CREATE_PROVIDER_PICK."""
    from link_project_to_chat.manager import bot as manager_bot

    # Build a stand-in ManagerBot with a fake transport.
    # (Use whatever helper the existing test_manager_create_*.py tests use to
    # construct a ManagerBot under test — typically a FakeTelegramTransport.
    # Inspect tests/test_manager_create_team.py for the pattern.)
    bot = _make_manager_bot_for_test()

    update = _make_text_update("myproj", user_id=42)
    next_state = await bot._create_name_callback(update, project_create_ctx)
    assert next_state == manager_bot.STATE_CREATE_PROVIDER_PICK


async def test_project_create_provider_pick_github_transitions_to_repo_source(
    project_create_ctx,
):
    from link_project_to_chat.manager import bot as manager_bot

    bot = _make_manager_bot_for_test()
    update = _make_button_update("provider:github")
    next_state = await bot._create_provider_pick_callback(update, project_create_ctx)
    assert next_state == manager_bot.STATE_CREATE_REPO_SOURCE
    assert project_create_ctx.user_data["create"]["provider"] == "github"


async def test_project_create_provider_pick_gitlab_transitions_to_repo_source(
    project_create_ctx,
):
    from link_project_to_chat.manager import bot as manager_bot

    bot = _make_manager_bot_for_test()
    update = _make_button_update("provider:gitlab")
    next_state = await bot._create_provider_pick_callback(update, project_create_ctx)
    assert next_state == manager_bot.STATE_CREATE_REPO_SOURCE
    assert project_create_ctx.user_data["create"]["provider"] == "gitlab"


async def test_project_create_repo_list_uses_gitlab_when_picked(
    project_create_ctx, monkeypatch,
):
    """After picking GitLab, the browse-repos list calls GitLabClient.list_repos."""
    from link_project_to_chat.repo_provider import RepoInfo

    project_create_ctx.user_data["create"]["provider"] = "gitlab"

    fake_list_repos = AsyncMock(return_value=(
        [RepoInfo(name="p", full_name="u/p", html_url="", clone_url="", description="", private=False)],
        False,
    ))

    bot = _make_manager_bot_for_test()
    with patch("link_project_to_chat.gitlab_client.GitLabClient") as MockClient:
        MockClient.return_value.list_repos = fake_list_repos
        MockClient.return_value.close = AsyncMock()
        await bot._create_repo_list_callback(
            _make_message_ref(),
            project_create_ctx,
            page=1,
        )

    fake_list_repos.assert_awaited_once()
```

> **Important:** The helpers `_make_manager_bot_for_test`, `_make_text_update`, `_make_button_update`, `_make_message_ref` should be **defined at the top of this test file** as small inline helpers — match the conventions in `tests/test_manager_create_team.py`. Inspect that file first; copy the test-double construction pattern there (typically a `FakeTransport` + a small `ManagerBot.__init__` wrapper).

- [ ] **Step 3: Run the 4 new tests**

Run: `pytest tests/test_manager_create_gitlab.py -k "project_create" -v`
Expected: FAIL — the callbacks don't exist or don't transition correctly.

- [ ] **Step 4: Add `_create_provider_pick_callback` to `ManagerBot`**

In `manager/bot.py`, find the existing `_create_repo_source_callback` (the state-handler immediately before `STATE_CREATE_REPO_SOURCE`). Define the new callback immediately above it:

```python
async def _create_provider_pick_callback(self, update, ctx) -> int:
    """STATE_CREATE_PROVIDER_PICK handler: store provider choice and advance."""
    incoming = self._incoming_from_update(update)
    # Reuse the existing button-payload extraction; payload is "provider:github" or "provider:gitlab".
    payload = self._extract_button_payload(update)
    if payload == "provider:cancel":
        return await self._cancel_create(update, ctx)
    if payload not in ("provider:github", "provider:gitlab"):
        # Invalid payload — re-prompt.
        await self._transport.send_text(
            self._chat_from(incoming), "Pick GitHub or GitLab to continue."
        )
        return STATE_CREATE_PROVIDER_PICK
    ctx.user_data.setdefault("create", {})["provider"] = payload.split(":", 1)[1]
    return await self._show_repo_source(update, ctx)
```

> **Convention check:** the exact name of `_extract_button_payload` and `_show_repo_source` depends on the existing codebase. Run `grep -n "callback_data\|button_payload\|_show_repo_source" src/link_project_to_chat/manager/bot.py` to find the actual helper names, and substitute accordingly.

- [ ] **Step 5: Replace the `STATE_CREATE_NAME → STATE_CREATE_REPO_SOURCE` transition**

Find the end of `_create_name_callback` (where it returns `STATE_CREATE_REPO_SOURCE`). Change the return to:

```python
    return await self._show_provider_pick(update, ctx)
```

Then add a new helper next to `_show_repo_source`:

```python
async def _show_provider_pick(self, update, ctx) -> int:
    """Render the provider picker screen and return STATE_CREATE_PROVIDER_PICK."""
    chat = self._chat_from(self._incoming_from_update(update))
    keyboard = [
        [{"text": "GitHub", "callback_data": "provider:github"}],
        [{"text": "GitLab", "callback_data": "provider:gitlab"}],
        [{"text": "Cancel", "callback_data": "provider:cancel"}],
    ]
    await self._transport.send_text(
        chat, "Pick a repo provider:", buttons=keyboard,
    )
    return STATE_CREATE_PROVIDER_PICK
```

> **Convention check:** match the existing keyboard-construction pattern in `manager/bot.py` (it may use `InlineKeyboardButton`/`InlineKeyboardMarkup` ferried through the transport, or a transport-portable list/dict). Look at how `_show_repo_source` builds its buttons and mirror that.

- [ ] **Step 6: Add `STATE_CREATE_PROVIDER_PICK` to the project-create `ConversationHandler` state map**

Find the project-create `ConversationHandler(states={...})`. Add the new state mapping:

```python
STATE_CREATE_PROVIDER_PICK: [
    CallbackQueryHandler(self._create_provider_pick_callback),
],
```

- [ ] **Step 7: Replace all GitHubClient instantiation sites with `_build_repo_provider`**

Find each of the 3 project-flow sites (`_create_repo_list_callback`, the project URL-paste handler, `_execute_clone`). Replace:

```python
gh = GitHubClient(pat=config.github_pat)
```

with:

```python
provider = _build_repo_provider(ctx, config)
```

…and replace subsequent `gh.list_repos(...)` / `gh.validate_repo_url(...)` / `gh.clone_repo(...)` / `gh.close()` with `provider.list_repos(...)` / etc. The Protocol guarantees the method shapes match.

- [ ] **Step 8: Run the 4 new tests**

Run: `pytest tests/test_manager_create_gitlab.py -k "project_create" -v`
Expected: 4 PASS.

- [ ] **Step 9: Run the broader manager tests**

Run: `pytest tests/test_manager_*.py -v 2>&1 | tail -10`
Expected: All PASS — the GitHub flow still works because `_build_repo_provider` defaults to `github` when no provider is set, AND any existing test that doesn't set `ctx.user_data["create"]["provider"]` continues to get a `GitHubClient`.

- [ ] **Step 10: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_gitlab.py
git commit -m "feat(manager): provider picker in create-project flow"
```

---

## Task 18: Manager — wire provider picker into create-team flow

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py` (team-create conversation handler)
- Test: `tests/test_manager_create_gitlab.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_manager_create_gitlab.py`:

```python
@pytest.fixture
def team_create_ctx(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    ctx = MagicMock()
    ctx.user_data = {"create": {"name": "myteam", "config_path": str(cfg)}}
    return ctx


async def test_team_create_shows_provider_picker_after_name(team_create_ctx):
    from link_project_to_chat.manager import bot as manager_bot

    bot = _make_manager_bot_for_test()
    update = _make_text_update("myteam", user_id=42)
    next_state = await bot._create_team_name_callback(update, team_create_ctx)
    assert next_state == manager_bot.STATE_CREATE_PROVIDER_PICK


async def test_team_create_clone_uses_gitlab_when_picked(team_create_ctx, monkeypatch):
    from link_project_to_chat.repo_provider import RepoInfo

    team_create_ctx.user_data["create"]["provider"] = "gitlab"
    team_create_ctx.user_data["create"]["repo"] = {
        "name": "p", "full_name": "u/p",
        "html_url": "https://gitlab.com/u/p",
        "clone_url": "https://gitlab.com/u/p.git",
        "description": "", "private": True,
    }

    bot = _make_manager_bot_for_test()
    fake_clone = AsyncMock()
    with patch("link_project_to_chat.gitlab_client.GitLabClient") as MockClient:
        MockClient.return_value.clone_repo = fake_clone
        MockClient.return_value.close = AsyncMock()
        # … invoke whatever helper is the team-create clone step …
        # The exact entry-point depends on the existing manager code. Inspect
        # tests/test_manager_create_team.py for the canonical invocation.

    fake_clone.assert_awaited_once()
```

> **Note:** the second test's exact invocation depends on the existing test_manager_create_team.py shape. Mirror its existing GitHub-clone test, with `provider="gitlab"` set.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_manager_create_gitlab.py -k "team_create" -v`
Expected: FAIL.

- [ ] **Step 3: Add the provider picker to the team-create flow**

In `manager/bot.py`, find `_create_team_name_callback`. Replace its return:

```python
    return STATE_CREATE_REPO_SOURCE
```

with:

```python
    return await self._show_provider_pick(update, ctx)
```

Add `STATE_CREATE_PROVIDER_PICK` to the team-create `ConversationHandler` state map (same as Task 17 Step 6 — both flows can share the state int because conversations are scoped per-handler, OR add a parallel `STATE_CREATE_TEAM_PROVIDER_PICK` constant if the existing code uses separate constants for the team flow).

- [ ] **Step 4: Replace the team-create flow's GitHubClient instantiation sites**

Find each of the 2 team-flow sites (the team URL-paste handler and the team-create clone block at ≈`:2411`). Replace `GitHubClient(...)` with `_build_repo_provider(ctx, config)` (mirror Task 17 Step 7).

- [ ] **Step 5: Run the 2 new tests**

Run: `pytest tests/test_manager_create_gitlab.py -k "team_create" -v`
Expected: 2 PASS.

- [ ] **Step 6: Run broader manager tests**

Run: `pytest tests/test_manager_*.py -v 2>&1 | tail -10`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_gitlab.py
git commit -m "feat(manager): provider picker in create-team flow"
```

---

## Task 19: Manager — `_setup` wizard fields for `gitlab_pat` and `gitlab_host`

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py` (setup wizard)
- Test: `tests/test_manager_create_gitlab.py`

- [ ] **Step 1: Locate the existing `github_pat` setup-wizard field**

Run: `grep -n "github_pat\|setup_field" src/link_project_to_chat/manager/bot.py | head -15`
Expected: locations where `github_pat` is added to the editable-fields list of the setup wizard.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_manager_create_gitlab.py`:

```python
async def test_setup_wizard_persists_gitlab_pat(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    bot = _make_manager_bot_for_test(config_path=cfg)

    ctx = MagicMock()
    ctx.user_data = {"pending_edit": {"field": "gitlab_pat", "config_path": str(cfg)}}
    update = _make_text_update("glpat-newvalue", user_id=42)
    await bot._handle_setup_input(update, ctx)

    import json
    raw = json.loads(cfg.read_text())
    assert raw["gitlab_pat"] == "glpat-newvalue"


async def test_setup_wizard_persists_gitlab_host(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    bot = _make_manager_bot_for_test(config_path=cfg)

    ctx = MagicMock()
    ctx.user_data = {"pending_edit": {"field": "gitlab_host", "config_path": str(cfg)}}
    update = _make_text_update("gitlab.example.com", user_id=42)
    await bot._handle_setup_input(update, ctx)

    import json
    raw = json.loads(cfg.read_text())
    assert raw["gitlab_host"] == "gitlab.example.com"


async def test_setup_wizard_rejects_gitlab_host_with_scheme(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    bot = _make_manager_bot_for_test(config_path=cfg)

    ctx = MagicMock()
    ctx.user_data = {"pending_edit": {"field": "gitlab_host", "config_path": str(cfg)}}
    update = _make_text_update("https://gitlab.example.com", user_id=42)
    await bot._handle_setup_input(update, ctx)

    # Should NOT persist and should send a rejection message.
    import json
    raw = json.loads(cfg.read_text())
    assert "gitlab_host" not in raw
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_manager_create_gitlab.py -k "setup_wizard" -v`
Expected: FAIL — fields not in the editable list.

- [ ] **Step 4: Add the two fields to the setup-wizard's field list**

Find the list of setup-wizard editable fields (look for a structure like `_SETUP_FIELDS = [...]` or an inline list inside a `_show_setup_menu` callback). Add `gitlab_pat` and `gitlab_host` entries with the same shape as `github_pat` but with appropriate labels (e.g., `"GitLab PAT (masked)"`, `"GitLab host"`).

In the `_handle_setup_input` function (or wherever pending-edit dispatch happens), add a branch for `gitlab_host`:

```python
if field == "gitlab_host":
    raw_value = value.strip()
    if "://" in raw_value or "/" in raw_value.rstrip("/"):
        await self._transport.send_text(
            chat,
            "Invalid host. Use a bare hostname like gitlab.example.com — no scheme, no path.",
        )
        return
    value = raw_value.rstrip("/")
```

- [ ] **Step 5: Run the 3 new tests**

Run: `pytest tests/test_manager_create_gitlab.py -k "setup_wizard" -v`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_gitlab.py
git commit -m "feat(manager): setup wizard gitlab_pat and gitlab_host fields"
```

---

## Task 20: End-to-end manager test — full create-project flow with GitLab

**Files:**
- Modify: `tests/test_manager_create_gitlab.py`

- [ ] **Step 1: Write the E2E test**

Append to `tests/test_manager_create_gitlab.py`:

```python
async def test_e2e_create_project_with_gitlab(tmp_path, monkeypatch):
    """Full project-create flow: name → provider:gitlab → browse → pick → clone → done."""
    from link_project_to_chat.repo_provider import RepoInfo

    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"gitlab_pat": "glpat-x", "gitlab_host": "gitlab.com"}'
    )
    bot = _make_manager_bot_for_test(config_path=cfg)

    # Stage the GitLab client mock.
    fake_repos = [RepoInfo(
        name="my-app", full_name="acme/my-app",
        html_url="https://gitlab.com/acme/my-app",
        clone_url="https://gitlab.com/acme/my-app.git",
        description="An app", private=True,
    )]
    fake_client = MagicMock()
    fake_client.list_repos = AsyncMock(return_value=(fake_repos, False))
    fake_client.clone_repo = AsyncMock()
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client.GitLabClient",
        lambda **kw: fake_client,
    )

    ctx = MagicMock()
    ctx.user_data = {}

    # Step 1: name
    await bot._create_name_callback(_make_text_update("myproj"), ctx)

    # Step 2: provider pick
    next_state = await bot._create_provider_pick_callback(
        _make_button_update("provider:gitlab"), ctx,
    )
    from link_project_to_chat.manager import bot as manager_bot
    assert next_state == manager_bot.STATE_CREATE_REPO_SOURCE
    assert ctx.user_data["create"]["provider"] == "gitlab"

    # Step 3: browse
    await bot._create_repo_list_callback(_make_message_ref(), ctx, page=1)
    fake_client.list_repos.assert_awaited()

    # Step 4: pick the repo
    ctx.user_data["create"]["repo"] = fake_repos[0].__dict__

    # Step 5: clone
    await bot._execute_clone(_make_chat_ref(), ctx)
    fake_client.clone_repo.assert_awaited_once()
    # The dest path should be repos/<name>/ next to config.json.
    call_dest = fake_client.clone_repo.await_args.args[1]
    assert "repos" in str(call_dest)
    assert "myproj" in str(call_dest)


async def test_e2e_create_project_with_github_still_works(tmp_path, monkeypatch):
    """Regression: picking GitHub in the picker still uses GitHubClient."""
    from link_project_to_chat.repo_provider import RepoInfo

    cfg = tmp_path / "config.json"
    cfg.write_text('{"github_pat": "ghp-x"}')
    bot = _make_manager_bot_for_test(config_path=cfg)

    fake_client = MagicMock()
    fake_client.list_repos = AsyncMock(return_value=(
        [RepoInfo(name="r", full_name="u/r", html_url="", clone_url="", description="", private=False)],
        False,
    ))
    fake_client.clone_repo = AsyncMock()
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.github_client.GitHubClient",
        lambda **kw: fake_client,
    )

    ctx = MagicMock()
    ctx.user_data = {}
    await bot._create_name_callback(_make_text_update("rg"), ctx)
    await bot._create_provider_pick_callback(_make_button_update("provider:github"), ctx)
    assert ctx.user_data["create"]["provider"] == "github"

    await bot._create_repo_list_callback(_make_message_ref(), ctx, page=1)
    fake_client.list_repos.assert_awaited()
```

- [ ] **Step 2: Run the 2 new tests**

Run: `pytest tests/test_manager_create_gitlab.py -k "e2e_create_project" -v`
Expected: 2 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_manager_create_gitlab.py
git commit -m "test(manager): E2E create-project with GitLab + GitHub regression"
```

---

## Task 21: End-to-end manager test — full create-team flow with GitLab

**Files:**
- Modify: `tests/test_manager_create_gitlab.py`

- [ ] **Step 1: Write the E2E team test**

Append to `tests/test_manager_create_gitlab.py`:

```python
async def test_e2e_create_team_with_gitlab(tmp_path, monkeypatch):
    """Full team-create flow up to the clone step, with GitLab picked.

    Note: this test stops at the clone step. The subsequent botfather/supergroup
    creation is covered by the existing test_manager_create_team.py tests and
    is provider-agnostic.
    """
    from link_project_to_chat.repo_provider import RepoInfo

    cfg = tmp_path / "config.json"
    cfg.write_text('{"gitlab_pat": "glpat-x", "gitlab_host": "gitlab.com"}')
    bot = _make_manager_bot_for_test(config_path=cfg)

    fake_client = MagicMock()
    fake_client.clone_repo = AsyncMock()
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client.GitLabClient",
        lambda **kw: fake_client,
    )

    ctx = MagicMock()
    ctx.user_data = {"create": {
        "name": "myteam",
        "config_path": str(cfg),
        "provider": "gitlab",
        "repo": {
            "name": "p", "full_name": "u/p",
            "html_url": "https://gitlab.com/u/p",
            "clone_url": "https://gitlab.com/u/p.git",
            "description": "", "private": True,
        },
        # Whatever other user_data the team-create clone block reads; mirror
        # the existing test_manager_create_team.py fixture shape.
    }}

    # Invoke the team-create clone block. The function name depends on the
    # existing manager code — likely something like `_execute_team_create_clone`
    # or the relevant branch inside `_run_team_create`. Inspect the existing
    # test for the canonical call.

    fake_client.clone_repo.assert_awaited_once()
```

> The exact invocation may need a quick code-read pass before this test will pass. The intent is unambiguous (mock `GitLabClient`, set `provider=gitlab`, invoke the team-clone step, assert `clone_repo` called).

- [ ] **Step 2: Run the new test**

Run: `pytest tests/test_manager_create_gitlab.py -k "e2e_create_team" -v`
Expected: 1 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_manager_create_gitlab.py
git commit -m "test(manager): E2E create-team with GitLab"
```

---

## Task 22: Docs — README, CHANGELOG, CLAUDE.md, AGENTS.md, TODO.md

**Files:**
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`
- Modify: `docs/TODO.md`

- [ ] **Step 1: README — add `glab` to the optional prereqs and document `gitlab_host`**

Find the "Quick start" / prereqs section in `README.md`. Next to the existing `gh auth login` mention, add:

```markdown
- **`gh` CLI** (optional, for GitHub repo browse/clone in the manager wizard) — `gh auth login`
- **`glab` CLI** (optional, for GitLab repo browse/clone) — `glab auth login`. For a self-hosted GitLab instance, also set `gitlab_host` in your config: `link-project-to-chat configure --gitlab-host gitlab.example.com` (bare hostname, no scheme).
```

If a "Config fields" or "configure CLI" section exists, list `gitlab_pat` and `gitlab_host` there too.

- [ ] **Step 2: CHANGELOG — add v1.3.0 entry**

Insert at the top of `docs/CHANGELOG.md`:

```markdown
## v1.3.0 — 2026-05-XX

### Added
- **GitLab repo source in the manager wizard.** Pick GitHub or GitLab when creating a project or team; the existing browse + paste-URL + clone flow works identically against either. Self-hosted GitLab supported via `Config.gitlab_host` (default `gitlab.com`). `glab` CLI is used when installed + authenticated; otherwise an `httpx + GITLAB_TOKEN` fallback applies.
- `Config.gitlab_pat` and `Config.gitlab_host` fields. CLI flags `configure --gitlab-pat` and `configure --gitlab-host`. Manager `/setup` wizard gains GitLab fields.
- `repo_provider.py` Protocol for cross-provider abstraction. Both `GitHubClient` and `GitLabClient` satisfy it.

### Security
- Team-mode block list extended: `glab mr create`, `glab mr merge`, `glab release create`, `glab ci run`.
- `GITLAB_TOKEN` env-scrub regression test pinned.
- PAT redaction in `gitlab_client._redact_secrets` covers raw, base64, and credential-URL forms (on both gitlab.com and the configured self-hosted host).
```

- [ ] **Step 3: CLAUDE.md and AGENTS.md — brief mention**

Find the "Key modules" section in `CLAUDE.md`. Add a one-line entry next to the existing `github_client` mention (or in the same paragraph):

```markdown
- **gitlab_client.py** — Mirrors `github_client.py` for GitLab repos. Uses `glab` CLI if available, falls back to `httpx + GITLAB_TOKEN`. Both clients satisfy the `RepoProvider` Protocol in `repo_provider.py`.
```

Repeat the same line in `AGENTS.md` (the two are kept in sync per the file headers).

- [ ] **Step 4: docs/TODO.md — log v1.3.0 status**

Append a new section to `docs/TODO.md` (after the existing v1.0.0 / v1.1.0 / v1.2.0 sections):

```markdown
### 1.6 GitLab repo management (v1.3.0)

Branch: `feat/v1.3.0-gitlab-repo-management`.

Design doc: [2026-05-20-gitlab-repo-management-design.md](superpowers/specs/2026-05-20-gitlab-repo-management-design.md)
Plan: [2026-05-20-gitlab-repo-management.md](superpowers/plans/2026-05-20-gitlab-repo-management.md) (22 tasks).

Status: 🟡 in progress

Scope: `RepoProvider` Protocol, `GitLabClient`, `gitlab_pat` / `gitlab_host` config, manager wizard provider picker for create-project + create-team, glab/MR safety in team mode.
```

- [ ] **Step 5: Run a quick sanity check that no doc file is malformed**

Run: `python -c "import pathlib; [pathlib.Path(p).read_text() for p in ['README.md', 'CLAUDE.md', 'AGENTS.md', 'docs/TODO.md', 'docs/CHANGELOG.md']]"`
Expected: No errors.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/CHANGELOG.md CLAUDE.md AGENTS.md docs/TODO.md
git commit -m "docs: v1.3.0 GitLab repo-management — README/CHANGELOG/CLAUDE/AGENTS/TODO"
```

---

## Task 23: Version bump

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/link_project_to_chat/__init__.py`

- [ ] **Step 1: Locate the version strings**

Run: `grep -n "version" pyproject.toml src/link_project_to_chat/__init__.py | head -5`
Expected: lines showing the current version (e.g. `version = "1.2.0"` in pyproject.toml and `__version__ = "1.2.0"` in __init__.py).

- [ ] **Step 2: Bump both to `1.3.0`**

Edit `pyproject.toml`:

```toml
version = "1.3.0"
```

Edit `src/link_project_to_chat/__init__.py`:

```python
__version__ = "1.3.0"
```

- [ ] **Step 3: Run the version-consistency test**

Run: `pytest tests/ -k "version_is_consistent" -v`
Expected: PASS (this regression test was added in v1.0.0 and pins both files in sync).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/link_project_to_chat/__init__.py
git commit -m "chore: bump version to 1.3.0"
```

---

## Task 24: Final full-suite verification

**Files:**
- None

- [ ] **Step 1: Run the entire test suite**

Run: `pytest -q 2>&1 | tail -5`
Expected: `N passed, M skipped, K warnings in X.XXs`, where `N >= baseline + ~70` (45 from the gitlab_client + 3 repo_provider + 5+3 config + 3 cli + 2 security + 6 team-mode + ~10 manager tests).

- [ ] **Step 2: If any failure surfaces, isolate and fix in a follow-up commit**

For each failing test:
- Re-run the specific test with `-v --tb=short` to see the failure mode.
- Determine if the failure is in production code (fix the impl) or in the test (fix the test).
- Commit the fix as `fix(<scope>): <short reason>` referencing the failure.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin feat/v1.3.0-gitlab-repo-management
```

- [ ] **Step 4: Open a draft PR**

```bash
gh pr create --base dev --draft --title "v1.3.0 — GitLab repo management (browse + clone + manager wizard parity)" --body "$(cat <<'EOF'
## Summary

Adds GitLab as a first-class repo source in the manager bot's create-project and create-team wizards, mirroring the existing GitHub flow under a shared `RepoProvider` Protocol.

- New `repo_provider.py` — Protocol + `RepoInfo` (moved from `github_client.py`, re-exported for backward compat).
- New `gitlab_client.py` — mirrors `github_client.py`. Prefers `glab` CLI, falls back to `httpx + GITLAB_TOKEN`. Supports self-hosted GitLab via the new `Config.gitlab_host` field (default `gitlab.com`).
- Manager wizard gains one new state: `STATE_CREATE_PROVIDER_PICK` (buttons: GitHub / GitLab / Cancel) inserted before the existing repo-source step in both create-project and create-team flows.
- Two new global config fields: `gitlab_pat`, `gitlab_host`. Surfaces in `configure` CLI flags and the manager `/setup` wizard.
- Team-mode block list extended: `glab mr create`, `glab mr merge`, `glab release create`, `glab ci run`.

Design: `docs/superpowers/specs/2026-05-20-gitlab-repo-management-design.md`
Plan: `docs/superpowers/plans/2026-05-20-gitlab-repo-management.md`

## Test plan

- [ ] `pytest -q` — all tests pass.
- [ ] Create a project via the manager bot, pick GitLab, browse my projects, clone the picked one. Verify `repos/<name>/` is created and contains the cloned repo.
- [ ] Repeat with a GitHub project — verify no regression.
- [ ] Create a project via the manager bot, pick GitLab, paste a URL, clone. Verify subgroup URLs work.
- [ ] Create a team via the manager bot, pick GitLab. Verify the full team-create flow completes.
- [ ] Set `Config.gitlab_host` to a self-hosted instance and verify browse/clone work against it.
- [ ] Verify `GITLAB_TOKEN` is NOT visible inside any agent subprocess (`/run env | grep GITLAB` in a project bot).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Mark plan complete**

Update `docs/TODO.md` §1.6 from 🟡 to ✅ once the PR merges to `dev`.

---

## Self-review notes

(Inline self-review pass during plan drafting — recorded here for the implementer's awareness.)

- **Spec coverage:** Every section of the spec (1–9 + failure modes + risks + open questions) maps to one or more tasks above. The two "open questions" in the spec (`glab repo clone` env-var honoring; per-operator provider memory) are not implemented — they remain open per the spec's explicit decision.
- **Placeholder scan:** Several "Convention check" callouts remain in tasks 16–19 — these are deliberate because the exact existing helper names (`_extract_button_payload`, `_show_repo_source`, `_handle_setup_input`) and state-int values cannot be locked from outside the codebase without inspecting `manager/bot.py` first. The implementer is instructed to `grep` for the actual names in Step 1 of each affected task before writing code. This is preferable to fabricating names that may not match the existing code.
- **Type consistency:** `RepoInfo` is the same dataclass in every task. `RepoProvider` Protocol method signatures match across `repo_provider.py`, `github_client.py` (existing), and `gitlab_client.py` (new). `_build_repo_provider` returns `RepoProvider` (Protocol type) — all 5 call sites use the same method names.
- **Self-hosted host handling:** The `host` parameter threads through `__init__` → `_client.base_url` → `_gitlab_url_re(self._host)` → `_git_auth_env(pat, host)` → `_redact_secrets(..., host=host)`. No site hardcodes `gitlab.com`.
