# GitLab repo management (browse + clone) — manager wizard parity with GitHub

**Date:** 2026-05-20
**Status:** Approved (brainstorming complete; awaiting plan)
**Author:** Revaz Chikashua (drafted with Claude)
**Target release:** v1.4.0 (additive on top of v1.2.0; no dependency on the v1.2.0 GitLab parity sweep)

## Summary

Add GitLab as a first-class repo source in the manager bot's create-project and create-team wizards, mirroring the existing GitHub flow. Operators can browse their GitLab projects (gitlab.com or one configured self-hosted host), paste a GitLab URL, and clone — exactly as they do for GitHub today. Auth uses `glab` CLI when available, with `httpx + GITLAB_TOKEN` as a fallback (the same dual-backend pattern as `GitHubClient`).

The change introduces a `RepoProvider` Protocol over a new `GitLabClient` and the existing `GitHubClient`, adds two global config fields (`gitlab_pat`, `gitlab_host`), and inserts a single new wizard state (provider picker) before the existing repo-source step. No changes to the cloned-project artifact: the `repos/<name>/` directory layout, the `RepoInfo` dataclass, and `ctx.user_data["create"]["repo"]` round-trip are unchanged.

## Background

The manager bot's project- and team-creation wizards use [`src/link_project_to_chat/github_client.py`](../../src/link_project_to_chat/github_client.py) to list the operator's GitHub repos (paginated), validate a pasted GitHub URL, and clone the selected repo into `repos/<name>/`. The client probes `gh auth status`; if `gh` is installed and authenticated, it shells out to `gh api /user/repos` and `gh repo clone`. Otherwise it falls back to `httpx` against `api.github.com` with `Authorization: Bearer {github_pat}` and to `git clone` with a one-shot `GIT_CONFIG_*` extra-header carrying a base64-encoded `x-access-token:{pat}` blob — avoiding the PAT ever appearing in argv. Stderr is run through `_redact_secrets` so the PAT, its base64 form, and `https://<token>@github.com/...` URL forms are stripped before logging.

Call sites in [`src/link_project_to_chat/manager/bot.py`](../../src/link_project_to_chat/manager/bot.py):

- `_load_team_create_dependencies` — lazy-imports `GitHubClient` and `RepoInfo`.
- `_create_repo_list_callback` (also reused by `team_create`) — paginated browse.
- URL-paste validation in `_create_repo_url_handler` (one each for project + team flows).
- `_execute_clone` (project) and the `team_create` clone path — instantiate `GitHubClient(pat=config.github_pat)`, call `clone_repo(repo, dest)`, then `close()`.

`Config.github_pat` is a global string field (not per-project) defined at [`config.py:485`](../../src/link_project_to_chat/config.py).

GitLab is not currently a supported repo source. The codebase's "gitlab" references are limited to the v1.0.0 plugin-system port (which inherited the GitLab fork's plugin framework) and the v1.2.0 GitLab parity sweep (safety prompt, hot-reload, etc.) — neither touches repo provisioning. There is no `gitlab_client.py`, no `glab` CLI integration, no `GITLAB_TOKEN` handling, and no `gitlab_*` field on `Config`.

This spec closes that gap with the smallest viable change: a parallel `GitLabClient`, a thin Protocol so the manager wizard doesn't grow per-provider branches, and one new wizard state for provider selection.

## Goals

1. **Operator picks the repo provider in the wizard.** A new `STATE_CREATE_PROVIDER_PICK` step is added before `STATE_CREATE_REPO_SOURCE` in both the create-project and create-team conversation handlers. Buttons: `GitHub` / `GitLab` / `Cancel`. The choice is stored in `ctx.user_data["create"]["provider"]`.
2. **GitLab repos are browsable and pasteable.** With `GitLab` selected, the existing `Browse my repos` and `Paste URL` UI works identically — same pagination, same per-page count, same selection → review → clone flow.
3. **Self-hosted GitLab support without forcing config.** `Config.gitlab_host` defaults to `gitlab.com`; operators on self-hosted instances set it once (CLI or manager Setup wizard). Per-project host override is **out of scope** (see §Non-goals).
4. **Mirror the GitHub auth model exactly.** `glab` CLI is preferred if installed + authenticated; `httpx + GITLAB_TOKEN` is the fallback. Errors point users at the same two install paths (`glab` install URL / `pip install …[create]`).
5. **No change to the cloned-project artifact.** `repos/<name>/` layout, the `RepoInfo` dict round-trip in `ctx.user_data`, and the subsequent `botfather` / supergroup / persona steps in `create_team` are all provider-agnostic and need no per-call branching.
6. **PAT and host never leak.** `GITLAB_TOKEN` is scrubbed from the agent subprocess env by the existing `*_TOKEN` generic pattern in `BaseBackend._prepare_env` (a regression test pins this). PAT redaction in `gitlab_client._redact_secrets` mirrors GitHub: raw PAT, base64 form, and credential-URL form for the configured `gitlab_host`.
7. **Team-mode safety stays tight.** The existing `pr_create` and `push` block lists in [`backends/claude.py:105`](../../src/link_project_to_chat/backends/claude.py) gain GitLab equivalents (`glab mr create`, `glab release create`, `glab workflow run`) so team-mode bots cannot raise MRs or cut releases without an explicit `--auth <scope>` directive.

## Non-goals

- **Per-project GitLab host.** `ProjectConfig.gitlab_host` override (options C/D from the brainstorm) is deferred. Operators with multiple GitLab tenants on one install can re-export `GITLAB_TOKEN` and toggle `Config.gitlab_host` between projects, or wait for a follow-up.
- **Bitbucket, Gitea, Codeberg, sr.ht.** The `RepoProvider` Protocol leaves the door open; this spec implements GitLab only.
- **SSH clone URLs.** HTTPS-only, mirrors the existing GitHub flow. Users who need SSH can clone manually and skip the wizard.
- **GitLab write paths in the manager bot.** Browse, validate, clone only. No MR creation, no project creation, no release management from LPTC's manager wizard.
- **Migrating existing projects between providers.** If a project was cloned from GitHub and the operator later wants to switch its remote to GitLab, that's a `git remote set-url` operation the operator runs themselves — not a wizard feature.
- **A `default_repo_provider` config field.** The provider-picker step is shown every time; making it skippable via a default is a follow-up if operators report friction.
- **Plugin opt-in to the provider choice.** Plugins don't see the provider selection; the cloned project looks identical to a GitHub-cloned project from a plugin's perspective.

## Architecture

**Approach:** a `RepoProvider` Protocol over two concrete clients (`GitHubClient`, `GitLabClient`), a single helper in `manager/bot.py` that builds the right client from `ctx.user_data["create"]["provider"]`, and one new conversation state that gates the existing flow.

```
Manager wizard (create_project or create_team)
  ↓
STATE_CREATE_PROVIDER_PICK   ← NEW
  buttons: GitHub / GitLab / Cancel
  ↓ ctx.user_data["create"]["provider"] = "github" | "gitlab"
STATE_CREATE_REPO_SOURCE     (existing)
  buttons: Browse my repos / Paste URL
  ↓
STATE_CREATE_REPO_LIST   or   STATE_CREATE_REPO_URL   (existing)
  each callsite does:
      provider: RepoProvider = _build_repo_provider(ctx, config)
      … list_repos / validate_repo_url …
  ↓
STATE_CREATE_REPO_REVIEW     (existing)
  ↓
_execute_clone (project) or team_create clone path  (existing)
  provider = _build_repo_provider(ctx, config)
  await provider.clone_repo(repo, dest)
  await provider.close()
```

### 1. New module: `src/link_project_to_chat/repo_provider.py`

A small, pure-Protocol module — no I/O, no implementations.

```python
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

`RepoInfo` moves here from `github_client.py`. `github_client.py` re-exports `RepoInfo` so existing imports (`from .github_client import RepoInfo`, ≈8 sites in `manager/bot.py` + tests) keep working without churn. After this change lands, downstream code should prefer `from .repo_provider import RepoInfo` but the re-export stays indefinitely.

`GitHubClient` is not modified beyond the import path — it already satisfies the Protocol structurally (same method names, same signatures, same return shapes).

### 2. New module: `src/link_project_to_chat/gitlab_client.py`

Mirrors `github_client.py` structure. Top-level functions:

- `_glab_available() -> bool` — runs `glab auth status` with a 5s timeout, returns `True` only on exit 0. Same shape as `_gh_available`.
- `_run_glab(*args) -> tuple[int, str, str]` — async subprocess wrapper, returns `(returncode, stdout, stderr)`.
- `_git_auth_env(pat: str, host: str) -> dict[str, str]` — like the GitHub version but the extra-header carries `Authorization: Bearer {pat}` and the URL prefix uses the configured `host`. Keeps the `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_{n}` / `GIT_CONFIG_VALUE_{n}` env-injection trick so the PAT never appears in argv.
- `_redact_secrets(text, *secrets, host) -> str` — redacts raw PATs, their base64 forms (for symmetry with GitHub even though GitLab doesn't use base64), and `https://<token>@{host}/...` URL forms. The `host` parameter is passed in so self-hosted instances are covered.
- `_GITLAB_URL_RE(host: str)` — compiled regex factory: `re.compile(rf"https?://{re.escape(host)}/((?:[^/]+/)+?[^/]+?)(?:\.git)?/?$")`. Captures the full namespaced path (handles subgroups: `group/subgroup/project`).

Class:

```python
class GitLabClient:
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

    async def list_repos(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        if self._use_glab:
            return await self._list_repos_glab(page, per_page)
        return await self._list_repos_api(page, per_page)

    async def validate_repo_url(self, url: str) -> RepoInfo | None: ...
    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None: ...
    async def close(self) -> None: ...
```

Key endpoint differences from GitHub:

- **List:** `GET /projects?membership=true&order_by=updated_at&page=N&per_page=M`. `membership=true` is the GitLab equivalent of GitHub's "include org-member repos" — returns projects the user is a direct member of, including via groups. Pagination follows the `Link: rel="next"` convention (compatible with the GitHub parser, just different base URL).
- **Validate:** `GET /projects/{url-encoded-full-path}` — e.g. `group%2Fsubgroup%2Fproject`. The `glab` path uses `glab api projects/{full_path}` which handles the encoding internally.
- **Clone:** `glab repo clone {full_path} {dest}` when available; otherwise `git clone {clone_url} {dest}` with the `GIT_CONFIG_*` Bearer header injected.

The `RepoInfo` mapping:

| `RepoInfo` field | GitLab API field |
|---|---|
| `name`         | `path` (repo slug, e.g. `my-app`) |
| `full_name`    | `path_with_namespace` (e.g. `group/subgroup/my-app`) |
| `html_url`     | `web_url` |
| `clone_url`    | `http_url_to_repo` |
| `description`  | `description` (may be `null` → coalesce to `""`) |
| `private`      | `visibility != "public"` (GitLab uses `"private"`/`"internal"`/`"public"`; both private and internal are non-public from LPTC's perspective) |

### 3. Config schema — `src/link_project_to_chat/config.py`

Two new global fields on `Config`:

```python
@dataclass
class Config:
    ...
    github_pat: str = ""               # existing
    gitlab_pat: str = ""               # NEW — mirrors github_pat
    gitlab_host: str = "gitlab.com"    # NEW — default; users override for self-hosted
```

Loader (`config.py:1144` neighborhood):

```python
config.gitlab_pat = raw.get("gitlab_pat", "")
config.gitlab_host = raw.get("gitlab_host", "gitlab.com")
```

Saver (`config.py:1407` neighborhood):

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

Defaults are omitted on save (keeps `config.json` clean for the gitlab.com common case). No migration: older configs read with no error because both fields have safe defaults.

### 4. CLI surface — `src/link_project_to_chat/cli.py`

`configure` subcommand gains:

- `--gitlab-pat <token>` — sets `config.gitlab_pat` (analogous to existing `--github-pat`)
- `--gitlab-host <host>` — sets `config.gitlab_host`; rejects values containing a scheme or path (must be a bare host like `gitlab.example.com`)

No changes to `start` or `start-manager`.

### 5. Manager wizard wiring — `src/link_project_to_chat/manager/bot.py`

#### New state

```python
STATE_CREATE_PROVIDER_PICK = <next free state int>
```

Inserted in **both** the project-create and team-create `ConversationHandler` state maps, before `STATE_CREATE_REPO_SOURCE`.

#### New entry callback

After the existing `_create_command` (project) and `_create_team_command` (team) collect the project name, they transition to `STATE_CREATE_PROVIDER_PICK` instead of directly to `STATE_CREATE_REPO_SOURCE`. The new screen shows three inline buttons: `GitHub` / `GitLab` / `Cancel`.

```python
async def _create_provider_pick_callback(self, update, ctx) -> int:
    incoming = self._incoming_from_update(update)
    choice = self._extract_button_payload(update)   # "github" / "gitlab" / "cancel"
    if choice == "cancel":
        return await self._cancel_create(update, ctx)
    ctx.user_data["create"]["provider"] = choice
    return await self._show_repo_source(update, ctx)
```

#### Provider-instance helper

A single module-level function in `manager/bot.py` (or a method on `ManagerBot`):

```python
def _build_repo_provider(ctx, config) -> RepoProvider:
    provider = ctx.user_data["create"]["provider"]
    if provider == "gitlab":
        from ..gitlab_client import GitLabClient
        return GitLabClient(pat=config.gitlab_pat, host=config.gitlab_host)
    from ..github_client import GitHubClient
    return GitHubClient(pat=config.github_pat)
```

All 5 GitHub-client instantiation sites switch to this helper:

| Site (approx. line) | Current | New |
|---|---|---|
| `_create_repo_list_callback` ≈1957 | `GitHubClient(pat=config.github_pat)` | `_build_repo_provider(ctx, config)` |
| URL-paste (project) ≈2032 | same | same |
| URL-paste (team) — same flow | same | same |
| `_execute_clone` (project) ≈2172 | same | same |
| team-create clone path ≈2411 | same | same |

#### Setup wizard fields

The manager bot's `/setup` flow already edits `github_pat`. Add two new editable fields next to it:

- `gitlab_pat` — masked input, stored to `config.gitlab_pat`.
- `gitlab_host` — plain input, validated (no scheme, no path, no whitespace), stored to `config.gitlab_host`.

Both fields are executor-gated (the existing setup-field auth already covers this).

### 6. Security

#### Env scrub

`BaseBackend._prepare_env` already scrubs `*_TOKEN` patterns from the agent subprocess env (specified by the Phase 5 design + team-mode safety spec, implemented in `backends/base.py`). `GITLAB_TOKEN` falls under this pattern. No code change, but:

- Add a regression test to `tests/test_security.py` setting `GITLAB_TOKEN=glpat-…` and asserting it is **not** present in the subprocess env (mirrors the existing `GITHUB_TOKEN` test at `tests/test_security.py:158`).
- Add the same assertion to `tests/backends/test_base_backend.py:34` neighborhood (mirrors the existing `monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")` block).

#### Team-mode block list

Extend `_BLOCKED_TOOLS` (or the equivalent constant) in [`backends/claude.py`](../../src/link_project_to_chat/backends/claude.py:105):

```python
"push": (
    "Bash(git push:*)", "Bash(git push)",
    "Bash(gh pr merge:*)", "Bash(glab mr merge:*)",  # ← + glab mr merge
),
"pr_create": (
    "Bash(gh pr create:*)", "Bash(glab mr create:*)",  # ← + glab mr create
),
"release_create": (
    "Bash(gh release create:*)", "Bash(glab release create:*)",  # ← + glab release create
),
"workflow_run": (
    "Bash(gh workflow run:*)", "Bash(glab ci run:*)",  # ← + glab ci run
),
```

(Exact category names match whatever the existing team-mode-safety spec settled on. The spec at `docs/superpowers/specs/2026-05-15-team-mode-safety-design.md` is the source of truth for the category list and existing entries.)

Update the corresponding tests in `tests/backends/test_claude_backend.py:52` and `:68` to assert the new GitLab entries are present in `blocked`.

#### PAT redaction

`gitlab_client._redact_secrets(text, *secrets, host=...)`:

- Replace any occurrence of each secret with `[REDACTED]`.
- Replace base64-encoded form `base64(f"x-access-token:{secret}".encode())` (kept for symmetry even though GitLab uses Bearer — defends against accidental code reuse).
- Replace `https://<token>@{host}/...` URLs with `https://[REDACTED]@{host}` (regex compiled from `host` at call time).

All `clone_repo` stderr decoding goes through this before raising or logging.

### 7. Testing

#### `tests/test_gitlab_client.py` (new)

Mirrors `tests/test_github_client.py` (≈210 lines, ~15 tests). The structure:

- **Fixture:** force `httpx` mode (`monkeypatch.setattr(..._glab_available, lambda: False)`) and instantiate `GitLabClient(pat="glpat-test123")`.
- **`list_repos`** — mocked `httpx` returns 2 projects with all `RepoInfo` fields populated. Assert mapping (`path` → `name`, `path_with_namespace` → `full_name`, `web_url` → `html_url`, `http_url_to_repo` → `clone_url`, `visibility="private"` → `private=True`, `visibility="internal"` → `private=True`, `visibility="public"` → `private=False`).
- **`list_repos` next-page** — `Link: <…>; rel="next"` → `has_next=True`.
- **`validate_repo_url`** — accepts `https://gitlab.com/group/project`, `https://gitlab.com/group/subgroup/project`, and `https://my.gitlab.example.com/g/p` (with `host="my.gitlab.example.com"`). Rejects `https://github.com/u/r`, bare paths, and URLs targeting a different `host`.
- **`clone_repo`** — patches `asyncio.create_subprocess_exec` and asserts argv is `["git", "clone", clone_url, str(dest)]` with `env` containing `GIT_CONFIG_KEY_n = "http.https://{host}/.extraHeader"` and `GIT_CONFIG_VALUE_n` starting with `"AUTHORIZATION: Bearer "`. (Note: GitLab uses Bearer; the GitHub flow uses `basic` with a base64'd `x-access-token` pair — these MUST differ.)
- **`_glab_available`** — three cases: (a) `glab` not on PATH → False, (b) `glab auth status` exits non-zero → False, (c) exits zero → True. Patches `shutil.which` and `subprocess.run`.
- **`_redact_secrets`** — input contains the PAT, the PAT's base64 form, and a `https://<pat>@gitlab.example.com/...` URL. All three are redacted; surrounding text is preserved.
- **`GitLabClient.__init__` error paths** — (a) `httpx is None` and `glab` unavailable → `ImportError` with both install pointers, (b) `httpx` available but `pat=""` and `glab` unavailable → `ValueError`.

#### `tests/test_manager_create_gitlab.py` (new)

Mirrors the structure of `tests/test_manager_create_team.py`. Uses a `FakeGitLabClient` injected via monkeypatch on the import path in `_build_repo_provider`. Coverage runs against **both** the create-project and create-team conversation handlers (parametrized fixture), since the provider-picker state is inserted into both:

- Provider picker shows `GitHub` / `GitLab` / `Cancel` buttons.
- Selecting `GitLab` stores `ctx.user_data["create"]["provider"] = "gitlab"`.
- The next-state browse list calls `FakeGitLabClient.list_repos` (and NOT `GitHubClient.list_repos`).
- URL paste with a `gitlab.com` URL is validated against the GitLab client; a `github.com` URL after picking `GitLab` is **rejected** by `validate_repo_url` returning `None`.
- Clone path (`_execute_clone` for project, the team-create clone block for team) calls `FakeGitLabClient.clone_repo` and writes `repos/<name>/` (asserted via the same FS scaffolding the GitHub test uses).
- Cancel from the provider picker returns to the manager root menu (no `ctx.user_data["create"]` left behind).
- Picking `GitHub` after this change still works end-to-end (parity test — no regression on the GitHub path).

#### `tests/test_config.py` additions

- Round-trip `gitlab_pat` and `gitlab_host` through `save_config` / `load_config`.
- Asserting default `gitlab_host = "gitlab.com"` is **not** written when defaulted (keeps `config.json` minimal).
- Asserting a custom `gitlab_host` IS persisted.

#### `tests/test_security.py` and `tests/backends/test_base_backend.py`

- `GITLAB_TOKEN` regression tests, paralleling the existing `GITHUB_TOKEN` blocks.

#### `tests/backends/test_claude_backend.py`

- Assert `Bash(glab mr create:*)`, `Bash(glab mr merge:*)`, `Bash(glab release create:*)`, `Bash(glab ci run:*)` appear in `blocked`.

### 8. Packaging

No new optional dependency. `glab` is an external CLI tool the operator installs themselves (same model as `gh`). `httpx` is already in the `[create]` extra. No `pyproject.toml` change.

### 9. Docs

- README "Quick start" section: add a `glab auth login` line next to the existing `gh auth login` line in the optional-prereqs list, and document `gitlab_host` for self-hosted operators.
- `CHANGELOG.md` v1.4.0 entry calling out: new `gitlab_pat` / `gitlab_host` config fields, manager wizard provider-picker step, `glab` CLI auto-detection, GitLab team-mode block-list additions.
- `CLAUDE.md` / `AGENTS.md`: brief mention that `gitlab_client.py` mirrors `github_client.py` under a shared `RepoProvider` Protocol.
- `docs/TODO.md` §1 (or a new sub-section): mark the spec + plan, status 🟡 → ✅ when shipped.

## Failure modes and edge cases

| Scenario | Behavior |
|---|---|
| `glab` installed but not authenticated | `_glab_available()` returns False; client tries httpx; if `gitlab_pat=""` raises `ValueError` with the same shape as the GitHub error. |
| Operator sets `gitlab_host="https://gitlab.example.com"` (with scheme) | `configure --gitlab-host` rejects the value; manager Setup wizard rejects it; config-load via `raw.get("gitlab_host", ...)` would accept it silently but the `_GITLAB_URL_RE` build would still work (`re.escape` handles it). **Decision:** validate in the loader too — strip scheme + trailing slash, log a warning, persist the cleaned form on next save. Keeps `host` shape invariant downstream. |
| `gitlab_host` is set but `glab` is auth'd to a different host | Prefer `httpx + pat`. The probe order is `(prefer_api and pat) → httpx; else → glab`. If both are stale, the user sees an actionable error from the first failed API call. |
| Subgroups deeper than one level (`group/sub1/sub2/project`) | Supported by the `_GITLAB_URL_RE` (non-greedy multi-segment capture). `validate_repo_url` URL-encodes the full namespaced path. `glab repo clone` accepts the namespaced form natively. |
| Pasted URL targets `github.com` after picking GitLab | `validate_repo_url` returns `None` (host mismatch); wizard re-prompts with "Not a valid GitLab URL". Symmetric: GitHub paste after picking GitHub still works. |
| `gitlab.com` project the PAT can't access (revoked / wrong scope) | `httpx` returns 403/404; client raises a redacted error message. Wizard surfaces "GitLab API error: 404 Not Found" (PAT scrubbed). |
| `git clone` over HTTPS fails mid-transfer | Same recovery as GitHub: the dest dir is left in a partial state and surfaced to the operator. We do NOT auto-clean — operator decides whether to retry or pick a different repo. |
| Operator switches `gitlab_host` mid-wizard | `ctx.user_data["create"]["provider"]` is "gitlab" but the in-flight `RepoInfo.html_url` references the old host. The clone will fail or clone the wrong project. **Mitigation:** the wizard reads `config.gitlab_host` once per state via `_build_repo_provider`; we don't snapshot to `ctx.user_data`. Operators changing host mid-wizard get a fresh-load result on the next step. |

## Migration / rollback

- **Forward:** None required. Old configs load with `gitlab_pat=""` and `gitlab_host="gitlab.com"`. Operators who never set them never see GitLab in the wizard (the picker still shows the button, but selecting it fails with "GitLab PAT required" — same UX as GitHub when `github_pat=""` and `gh` isn't authenticated).
- **Backward:** If a user downgrades to v1.2.x after setting `gitlab_pat` / `gitlab_host` in `config.json`, the unknown keys are ignored by the dataclass loader (`raw.get(...)` doesn't error on missing fields; `Config(**raw)` is not used — the loader is field-by-field). Safe.

## Risks

- **`glab` API surface differences from `gh`.** The `glab api` subcommand's output format and headers are slightly different from `gh api --include`. The plan must include manual verification that the Link-header parser works on both CLIs' output. If it doesn't, the `_list_repos_glab` path uses a different output flag (`glab api --include` is supported as of glab 1.41) or falls back to two calls (one for the body, one for pagination).
- **Self-hosted GitLab API version drift.** `/api/v4/projects?membership=true` has been stable since GitLab 12.x (2019). LPTC's stated floor is GitLab 14+ (matches `glab`'s floor). Documented in README.
- **PAT redaction completeness.** The base64 form is redacted defensively even though GitLab uses Bearer — a future change to share auth-env helpers with GitHub would otherwise leak. Pin this with a unit test.
- **Wizard state-int collisions.** `STATE_CREATE_PROVIDER_PICK` must be added to **both** `ConversationHandler` state maps and to the `BACK` button routing if the manager UI supports back-navigation. Tests pin the state-map shape.

## Open questions

- **Does `glab repo clone` honor `GIT_CONFIG_*` env overrides?** GitHub's `gh repo clone` does; need to verify before falling back to `git clone` only on the httpx path. If `glab repo clone` ignores `GIT_CONFIG_*`, the glab-path clone uses `glab`'s own auth (which is fine — `glab` is auth'd at that point). The httpx path always uses raw `git clone` with `GIT_CONFIG_*`, no ambiguity.
- **Should the provider picker remember the last choice per-operator?** YAGNI for v1.4.0; revisit if friction is reported.

## References

- Existing GitHub flow: [`src/link_project_to_chat/github_client.py`](../../src/link_project_to_chat/github_client.py), [`src/link_project_to_chat/manager/bot.py`](../../src/link_project_to_chat/manager/bot.py) (5 instantiation sites), [`tests/test_github_client.py`](../../tests/test_github_client.py).
- Team-mode block list: [`src/link_project_to_chat/backends/claude.py:105`](../../src/link_project_to_chat/backends/claude.py), spec [`docs/superpowers/specs/2026-05-15-team-mode-safety-design.md`](2026-05-15-team-mode-safety-design.md).
- Env scrub: [`src/link_project_to_chat/backends/base.py`](../../src/link_project_to_chat/backends/base.py) `_prepare_env`, regression tests [`tests/test_security.py:158`](../../tests/test_security.py), [`tests/backends/test_base_backend.py:34`](../../tests/backends/test_base_backend.py).
- Config conventions: [`src/link_project_to_chat/config.py`](../../src/link_project_to_chat/config.py) — `0o600` save, `raw.get(...)` defaults, omit-on-default save behavior.
- GitLab REST API: [https://docs.gitlab.com/ee/api/projects.html](https://docs.gitlab.com/ee/api/projects.html), [https://docs.gitlab.com/ee/api/repositories.html](https://docs.gitlab.com/ee/api/repositories.html).
- `glab` CLI: [https://gitlab.com/gitlab-org/cli](https://gitlab.com/gitlab-org/cli).
