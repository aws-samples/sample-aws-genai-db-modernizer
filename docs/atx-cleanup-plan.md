# ATX Orchestrator — Cleanup Plan & Divergence Notes

> **Status:** working notes / decision doc. **Untracked on purpose** — not staged, not committed.
> **Scope:** `src/atx_orchestrator/` (the AWS Transform integration) + its Docker/deploy story.
> **Author of this note:** review pass on branch `exp/agents-in-transform`, 2026-08-27.
> **Nothing in here has been executed.** It is a plan to agree on before any code moves.

This doc has two jobs:

1. Capture the **open questions** that block a clean path forward (several need Leo's input).
2. Lay out **how to make this better** — target structure, the correct Docker approach, an
   assessment of Bedrock AgentCore feature usage, and a phased cleanup plan.

---

## Part A — Repository divergence (needs Leo)

### A.1 What the git state actually is

- Current branch: `exp/agents-in-transform`, tracking `origin/exp/agents-in-transform`.
- After a fresh `git fetch`: **`ahead 126, behind 33`**.
- The branch was **rebased locally today** (reflog is a full `rebase (pick) … rebase (finish)` replay).
- Merge-base (divergence point): `a856060` — *"chore(deps): update all dependencies…"*, 2026-06-24.
- Remote HEAD `5e5f2a4` (committer **Leo Ciccone**, 2026-08-25).
- Local HEAD `63c60fc` — **same subject**, same author (Leo), same author-date (2026-08-25),
  but **committer = Esteban Serna, committer-date 2026-08-27**.

### A.2 What that means

The rebase **replayed Leo's atx commits onto a newer base**. Result:

- The **same logical atx work exists on both sides as different commits** (new SHAs, new committer
  timestamps). `git range-diff` confirms the atx commits are content-equivalent one-for-one.
- The **33 "behind" commits are Leo's originals** still sitting on `origin`.
- The **126 "ahead"** is *not* just the replayed atx work. Rebasing onto `a856060` pulled in a large
  body of **other** mainline work the old remote atx branch never had — e.g. load-testing,
  SQL Server / Oracle collectors, and an entire LadybugDB "context graph" layer.
- Why GitHub shows "2 days ago" while local shows ~2 hours: GitHub renders the **author date**
  (unchanged, Leo, Aug 25); local tooling shows the **committer date** (reset by the rebase).
- Working tree also has: `M uv.lock` (uncommitted) and `docs/aws-transform-handoff.md` (untracked).

**Consequence:** a plain `git push` will be rejected (non-fast-forward); a plain `git pull` would
merge Leo's originals back in as duplicate commits. The histories must be reconciled deliberately.

### A.3 Open questions for Leo (BLOCKING the git reconciliation only)

1. **Which history is canonical?** Your rebased local line (atx work + newer mainline base) or the
   `origin` line (Leo's original 33)? This decides whether we force-update remote or reset local.
2. **Is anyone else building on `origin/exp/agents-in-transform` right now?** If yes, rewriting that
   remote history would disrupt them — we'd pick a merge/PR path instead of a force-update.
3. **Why did the rebase happen?** Intentional (to get onto newer mainline for the graph/load-test
   work) or accidental? If intentional, the local line is probably what we want to keep.
4. **Should the atx branch even carry the graph/load-test/Oracle/SQL-Server work?** Those ~90 extra
   commits came along for the ride via the new base. If atx should stay focused, we may want a
   cleaner base branch.

> **None of the code-cleanup work below is blocked by these questions.** They only gate the git
> reconciliation. We can proceed with everything in Parts B–F on the working tree regardless.

---

## Part B — Duct-tape inventory (what "not up to standard" concretely means)

Ordered by pain. Each is cited to a real file.

1. **Deprecated tools are registered, then argued out of existence in the prompt.**
   `orchestrator.py::PIPELINE_TOOLS` registers the in-process `run_synthesis`, `run_full_assessment`,
   `run_reality_check`, `run_schema_design` *alongside* the real A2A tools — then `SYSTEM_PROMPT`
   spends huge paragraphs telling the LLM "NOT AVAILABLE / DEPRECATED / do NOT call." Unregister them
   and ~40% of the prompt disappears. **Highest value, lowest risk.**

2. **`core.py` is ~500 lines of copy-paste.** The six `run_analysis_*_core` functions are near-identical
   (same collector-key guard, same `AnalysisInput` build, same 3-artifact write, same `level_counts`
   confidence-band block pasted six times). They differ only by: engine name, `analyze_for_*` fn,
   whether `llm_mode="none"` is passed, whether a Mermaid diagram is produced.

3. **Two deployment topologies coexist.** v1 = one image per agent (`app.py`, `collector_app.py`,
   `analysis_*_app.py` + `Dockerfile`, `Dockerfile.collector`, `Dockerfile.analysis-*`). v2 =
   single image + `AGENT_TYPE` dispatch (`atx_entrypoint.py` + `Dockerfile.atx`). ~10 `*_app.py`
   files and ~10 Dockerfiles are dead weight unless v1 is still deployed. (See Part A / Leo.)

4. **`a2a.py` carries a dead primitive + empirical magic numbers.** `send_and_wait` is used **only by
   tests** now (prod uses `invoke_and_wait`). Timings like `post_ready_dwell=15.0` and the 60s
   `_wait_for_ready` are debugging scars with no named rationale in-code.

5. **Debug logging left in the hot path.** `subagent_base.py::extract_text` / `parse_invocation`
   log `"DIAG A16.10 …"` at INFO on **every message**, dumping raw payloads.

6. **The handoff doc describes a project that no longer exists.** `docs/aws-transform-handoff.md`
   says only collector+orchestrator are built and tools run in-process ("next task: A2A wiring").
   Reality: full A2A, 6 analysis subagents, assignment, synthesis, progress panel, artifact
   publishing are all done. It also references files that aren't in the repo
   (`docs-atx-poc/subagent-recipe.md`, `claude.md`, `ATX_POC_STATE.md`).

7. **Rationale lives in dangling references.** Tags like `F8`, `Y-3`, `A14`, `A16.9`, `test-24`,
   `v2-e2e-01`, `claude.md §97` are sprinkled through the code but resolve to nothing in-repo.

---

## Part C — Target folder structure

The clean bones already exist (`core.py` as single source of truth, thin subagent wrappers, one A2A
primitive). The goal is to make the layout reflect that and split "what the agent is" from "how it
ships."

```
src/atx_orchestrator/
  __init__.py
  core.py                 # deterministic phase fns (collapse the 6 analysis fns — see B.2)
  a2a.py                  # ONE primitive (invoke_and_wait); drop send_and_wait
  job_plan.py             # WebApp progress (unchanged)
  artifacts.py            # Artifacts-panel publishing (unchanged)
  store.py                # TransformS3Store / TransformLocalStore (unchanged)
  orchestrator.py         # DBModernizationOrchestrator: register ONLY real tools; trimmed prompt
  tools.py                # ONLY the A2A tools the LLM may call
  entrypoint.py           # the AGENT_TYPE dispatcher (renamed from atx_entrypoint.py)
  subagents/              # one module per AGENT_TYPE (moved out of the flat dir)
    __init__.py
    base.py               # make_subagent_factory (was subagent_base.py; DIAG logging removed)
    collector.py
    triage.py
    analysis_dynamodb.py
    … (one per engine) …
    assignment.py
    synthesis.py

infrastructure/docker/atx-agent/
  Dockerfile              # the single AGENT_TYPE-dispatch image (was Dockerfile.atx)

docs/
  atx-architecture.md     # rewritten, accurate handoff (replaces aws-transform-handoff.md)
  atx-cleanup-plan.md     # this file
```

Removed in this layout: the 10 `*_app.py` entrypoints, the per-agent Dockerfiles, `requirements.txt`
(folded into `pyproject.toml`), and `send_and_wait`. **All contingent on the Part A / v1 decision.**

---

## Part D — Docker: the standard vs. what atx did, and the right way forward

### D.1 The established standard (`infrastructure/docker/{core,agent,ui,agent-load-test}/`)

- Dockerfiles live **outside** the source tree, one dir per component under `infrastructure/docker/`.
- Base image **`python:3.12-slim` pinned by SHA256 digest** (reproducible).
- Dependencies via **`uv sync --frozen --no-dev --no-install-project`** against `pyproject.toml` +
  `uv.lock` — **one locked source of truth.**
- Non-root user, uid/gid 1000, `groupadd` + `useradd --no-create-home`.
- `ENV PYTHONDONTWRITEBYTECODE / PYTHONUNBUFFERED / UV_CACHE_DIR`.
- `checkov` security annotations where a rule is intentionally skipped
  (e.g. `# checkov:skip=CKV_DOCKER_2` on the run-to-completion agent).

### D.2 What the atx Dockerfiles do differently

`src/atx_orchestrator/Dockerfile`, `Dockerfile.atx`, `Dockerfile.collector`, and ~8 per-agent files:

| Aspect | Standard | atx | Verdict |
|---|---|---|---|
| Location | `infrastructure/docker/<c>/` | inside `src/atx_orchestrator/` | **gratuitous deviation** |
| Base image | `python:3.12-slim` **@sha256** | `python:3.11-slim` **unpinned**, from `public.ecr.aws` | **fix** (pin + reconcile version) |
| Dependencies | `uv sync --frozen` (uv.lock) | `pip install` vs hand-kept `requirements.txt` (a `>=`-ranged **duplicate subset** of pyproject) | **fix** (drift hazard, non-reproducible) |
| Non-root user | `groupadd`+`useradd --no-create-home` | `useradd --create-home` | minor |
| Security annotations | `checkov:skip=…` documented | none | **fix** |
| Duplication | one file per component | ~40-line SDK+model+MCP block copy-pasted across ~11 files | **fix** |
| Count | small | v1 per-agent files **and** the v2 `Dockerfile.atx` coexist | **fix** (Part A) |
| Stale comment | — | "Override at runtime via ECS task definition" (AgentCore runtimes aren't ECS tasks) | fix comment |

### D.3 Legitimately different — do NOT "fix" these

These diverge from `core`/`agent` **because AgentCore requires it**, not because of sloppiness:

- **`--platform=linux/arm64`** — AgentCore Runtime is Graviton-only; x86 fails with `exec format error`.
- **Port `8080` + `/ping` healthcheck** — AgentCore's expected contract (core uses 8000/`/health`).
- **SDK botocore model registration** (`atxagentregistryexternal`, `transformagenticservice`) copied
  to a world-readable path with `AWS_DATA_PATH` — required or the agent dies at startup with
  `Unknown service`.
- **MCP shim** at `/home/amazon/AgentBuilderAgenticMCP/bin/agent-builder-agentic-mcp` — `AgentRuntimeServer`
  looks for that exact path.

### D.4 The right way forward for Docker

1. **One image, one Dockerfile, in the standard location:**
   `infrastructure/docker/atx-agent/Dockerfile` = today's `Dockerfile.atx` (AGENT_TYPE dispatch).
   Delete the per-agent Dockerfiles once v1 is retired (Part A).
2. **Pin the base by digest** and **reconcile to Python 3.12** (SDK needs ≥3.11, so 3.12 is fine and
   matches the repo standard). The atx comment even admits 3.11 was only "what the template mandated."
3. **Kill `requirements.txt`.** Add the SDK runtime deps to `pyproject.toml` (e.g. an `atx` extra) and
   build with `uv sync --frozen` so there is exactly **one** dependency source of truth. This removes
   the single most dangerous drift hazard.
4. **Factor the shared AgentCore block** (SDK install + model registration + MCP shim) so it isn't
   copy-pasted. With a single image this mostly falls out for free.
5. **Add `checkov` annotations** consistent with the standard.

> Open question for the team: does the AWS Transform toolkit's `build_agent_image` expect the
> Dockerfile at a specific path relative to `agent_path`? If it hard-requires `Dockerfile` in the
> agent dir, we reconcile the "Dockerfiles live in `infrastructure/docker/`" convention against that
> (e.g. a thin `Dockerfile` that references the standard one, or pass an explicit path). **Confirm
> against the toolkit before finalizing D.1.**

---

## Part E — Bedrock AgentCore feature usage (verified vs. to-confirm)

### E.1 In use today (verified from the code)

- **`AgentRuntimeServer` with `delayed_timeout=3600`** (app.py, subagent_base.py) — correct choice for
  long-running work; avoids the 28s stateless timeout.
- **MCP shim** wired for the agentic MCP binary.
- **A2A over the Agentic API** — `a2a.invoke_and_wait` does `list_agent_instances → invoke_agent →
  send_message → poll get_agent_instance`, tolerating the expected `-32603`.
- **Job-plan progress panel** — `job_plan.py` (`PutJobPlan` / `UpdateJobPlanStep`).
- **Artifacts panel publishing** — `artifacts.py` (`upload_artifact`, best-effort).
- **`discover_subagents` is DISABLED** — intentionally omitted from `PIPELINE_TOOLS` because SDK v1.0.2
  returns a hardcoded mock.

### E.2 Available in the toolkit but apparently NOT used — **candidates to evaluate**

> These are drawn from the toolkit's tool list + steering index. **Each needs confirmation against the
> toolkit docs before we rely on it** (the deep steering read was deferred). Do not treat as settled.

- **Chat entry-point control** via registration metadata — `job_orchestrator`, `chat_ui_label`,
  `chat_agent_identifier`, `job_orchestrator_metadata`. This is almost certainly the fix for the
  "sits dead in there / hacky start" problem (see Part G).
- **HITL** (`get_hitl_generation_prompt` + the `hitl-*` doc sources) — human-in-the-loop review gates
  rendered in the WebApp. A natural fit for the "analysis done, proceed?" turns the roadmap wanted.
- **Skill registry** (`upload_skill` / `download_skill` / …) — reusable capabilities.
- **Observability** — `fetch_logs` / `list_log_streams` over CloudWatch for the deployed runtimes.
- **`validate_agent_setup`** — pre-flight check that a registered version is reachable.

### E.3 Open questions for the AgentCore review

1. Which of E.2 do we actually want? (chat-start = yes; HITL = likely; skills/obs = nice-to-have)
2. Are we on the latest SDK, and does a newer version fix the `discover_subagents` mock so we can
   re-enable real registry discovery?
3. Is `delayed_timeout=3600` enough for the worst-case analysis run, or should long phases stream
   progress via the job plan instead of holding a single long call?

---

## Part F — Phased cleanup plan (proposed sequencing)

Ordered so the **highest-value, lowest-risk, zero-deployment** work comes first. None of this touches
the deterministic pipeline behavior; it's organizational. Each phase is independently shippable.

- **Phase 0 — Reconcile git history.** *(blocked on Leo, Part A.)* Decide canonical line; do it once,
  cleanly, before refactors so we're not rebasing edits repeatedly.
- **Phase 1 — Prompt & tool surface.** Unregister the 4 deprecated in-process tools from
  `PIPELINE_TOOLS`; cut the "do NOT call" prose from `SYSTEM_PROMPT`. *(local, reversible, big clarity win)*
- **Phase 2 — Collapse `core.py` analysis fns** into one parametrized `run_analysis_core(engine, …)` +
  a per-engine config table. Verify byte-identical output against the `discourse/15e6403d` fixture.
- **Phase 3 — `a2a.py` tidy.** Drop `send_and_wait` (migrate its tests onto `invoke_and_wait`); lift
  magic timings to named constants with one-line rationale.
- **Phase 4 — Remove DIAG logging** in `subagent_base.py` (or demote to `debug`).
- **Phase 5 — Docker consolidation** (Part D.4). *(needs the v1-retirement decision + toolkit path check)*
- **Phase 6 — Docs.** Rewrite the handoff as an accurate `docs/atx-architecture.md`; fold the still-useful
  `Fxx/Axx` rationale in so the dangling refs resolve in-repo.
- **Phase 7 — Folder restructure** (Part C). Do this last — it's mechanical and easiest once the content
  is already trimmed.

**Verification for every phase:** run the existing `scripts/atx_*_test.py` (smoke, contract, tool,
subagent) — they diff against the golden `discourse/15e6403d` fixture, so any behavior change surfaces
immediately. No AWS or Docker required.

---

## Part G — Deferred: deploy my own agent + control the chat starting point

Explicitly parked until this plan is agreed. Captured so we don't lose it:

- **Goal:** push our own orchestrator so *we* control the chat's opening state, instead of it "sitting
  dead / hacky."
- **Likely mechanism:** orchestrator **registration metadata** — `job_orchestrator=True`,
  `chat_ui_label`, `chat_agent_identifier`, and `job_orchestrator_metadata`. Deploy via the toolkit's
  `deploy_agent_full_pipeline` / `deploy_agent_to_agentcore` + `register_agent` / `publish_agent_version`.
- **Blockers / cautions to resolve first:**
  - **Account allowlisting** for the AWS Transform registry (handoff doc says pending; account
    `123456789012`). Without it, registration fails — but we *can* deploy to AgentCore and invoke the
    runtime directly to test the container half without spend on registration.
  - This step **builds ARM64 images, pushes to ECR, and creates AgentCore runtimes** → real AWS spend
    and a production-touching action. **I will not run any deploy without explicit go-ahead.**
- **Next action when we pick this up:** read the toolkit steering `deploy-agent-workflow.md`,
  `agent-registration.md`, `orchestrator-patterns.md`, `dockerfile-orchestrator.md` (the read that was
  interrupted), then produce a concrete test-deploy runbook + the exact registration metadata that sets
  the chat starting point.

---

## Part H — Deployment automation (the "it's deployed by hand" problem)

The most production-unfriendly gap. The atx images have **no deployment automation at all**, in a
repo where the core workload ships via CloudFormation + scripts.

### H.1 How the CORE images ship (the bar already set in this repo)

- **IaC:** `infrastructure/cloudformation/*.yaml` — ECS infra, auth, storage, orchestration (Step
  Functions), api-service, ui-service.
- **Scripts:** `scripts/deploy-services.sh` deploys those stacks and wires in ECR image URIs;
  `scripts/promote-images.sh` copies dev→prod ECR with skopeo and immutable tags.
- **Runs in GitLab CI, not GitHub.** The build/push/deploy pipeline is GitLab's `.gitlab-ci.yml`, which
  invokes `deploy-services.sh` / `promote-images.sh`. Evidence in *this* repo: `cfn-helpers.sh` reads
  `CI_PIPELINE_ID` / `GITLAB_USER_LOGIN` ("set automatically by GitLab"); `.gitleaks.toml` allowlists
  `.gitlab-ci.yml`; `deploy-services.sh` reads `DOCKER_IMAGE_TAG` from that CI.
- **GitHub has no deploy workflow — by design.** GitHub is the public open-source window; GitLab is the
  deployment repo. `.github/workflows/` here is intentionally quality-gates only (`ci.yml`,
  `release.yml`, `api-docs.yml`). Its absence of a deploy workflow is **not** a defect. See Part I.

### H.2 How the ATX images ship today

- `pipeline/` contains exactly **one file: `iam-roles.yaml`.** No CloudFormation for the AgentCore
  runtimes. No deploy script. No CI job.
- The only documented path is `docs/aws-transform-handoff.md`'s copy-paste
  `deploy_agent_full_pipeline(...)` calls **run by hand through the Kiro toolkit on a laptop.**
- `deploy_agent_to_agentcore` creates runtimes imperatively via `bedrock-agentcore-control`. **Nothing
  in version control captures** the runtime definitions, the per-runtime `AGENT_TYPE` env var, the
  model id, or the registry registration.

> **Verdict:** ATX deployment is fully manual, non-reproducible, unaudited, and has no rollback. That
> is not acceptable for a production workload, and it's inconsistent with how core ships.

### H.3 The right way forward for ATX deployment

**The atx deploy automation belongs in GitLab's `.gitlab-ci.yml`, not GitHub Actions.** GitHub is the
public window; the build/push/deploy stages live in the GitLab deployment repo alongside core's.

1. **Build + push in a GitLab CI stage.** Add an atx stage to GitLab's `.gitlab-ci.yml` that builds the
   single arm64 image (`Dockerfile.atx`) and pushes to ECR with an **immutable tag = commit SHA** —
   same discipline `promote-images.sh` already assumes. (`.gitlab-ci.yml` is target-only; see Part I.)
2. **Declarative runtime map.** A committed `pipeline/atx-agents.yaml` (authored in GitHub, syncs to
   GitLab) listing the 11 `AGENT_TYPE`s → `{runtime name, AGENT_TYPE, model id, visibility, timeout}`.
   One image, N runtimes, differing only by env var — this is what makes "who is deployed" auditable.
3. **Codify the runtimes as IaC.** Prefer CloudFormation/CDK for the AgentCore runtimes + registration
   **if the resource types exist** — **CONFIRM `AWS::BedrockAgentCore::Runtime` availability**. If not
   yet in CFN, wrap the toolkit calls in a committed, parameterized `pipeline/deploy-atx.sh` invoked by
   the GitLab stage — reproducible and version-controlled even if imperative, mirroring
   `deploy-services.sh`.
4. **Scripted registration**, gated behind the allowlisting flag (pre-allowlisting:
   build→push→deploy→invoke-directly; post-allowlisting: switch the register step on).
5. **dev→prod promotion** for the atx image via the existing `promote-images.sh` pattern.

Net goal: get ATX to the same "IaC + GitLab CI, no laptop deploys" bar core already targets. This is
**Phase 8** (after the Docker consolidation in Phase 5).

---

## Part I — Two-repo model (GitHub public ↔ GitLab deploy)

This is very likely where the "deployment disconnection" lives, and it changes how several of the fixes
above must be carried out.

### I.1 The model (confirmed from this repo)

- **GitHub (this repo)** = source of truth for **development** + public open-source window. Work happens
  here on `exp/*` branches; `.github/workflows/` is **quality-gates only** by design.
- **GitLab (target)** = **deployment mirror**. Its `.gitlab-ci.yml` runs the real build/push/deploy
  (`deploy-services.sh`, `promote-images.sh`, CloudFormation). "Do NOT develop there directly."
- **Sync is one-way, GitHub → GitLab**, via `scripts/maintainer-sync.sh`: `rsync --delete` source→target,
  then commit + `git push --force-with-lease` on the target.
- **Branch mapping:** `exp/*` (GitHub) → `feat/*` (GitLab) — "the target only builds `feat/*`". Matches
  the range-diff commit `fix(sync): map exp/* source branches to feat/* on the target repo`.
- **Target-only files are protected** via `.sync-config` (`[exclude]`/`[protect]`) in the GitLab repo —
  e.g. `.gitlab-ci.yml` (also allowlisted in `.gitleaks.toml`). Those exist only on the deploy side and
  must survive the rsync.

### I.2 What this implies for the plan

- **The missing `deploy.yml` in GitHub is not a defect.** (Corrected in H.1.)
- **Any atx deploy automation goes on the GitLab side** (`.gitlab-ci.yml` + `.sync-config`), never as a
  GitHub Actions workflow. A GitHub-side deploy workflow would be the *wrong* fix.
- **Hazard — clobbering:** the sync does `rsync --delete`. An atx deploy file created directly in GitLab
  that is **not** registered in `.sync-config` `[protect]` will be **deleted on the next sync.** Adding
  atx to GitLab therefore means also updating `.sync-config`.
- **Hazard — disposable target history:** the sync force-pushes `feat/*`, so GitLab branch history is
  intentionally throwaway. Combined with local `exp/*` rebases (Part A), the **GitHub `exp/*` line is
  the only history worth reconciling carefully** — GitLab's just gets re-synced.

---

### Immediate decisions needed from you / Leo

1. **Part A** — canonical git history + is anyone on `origin/exp/agents-in-transform`?
2. **Part A / D** — is the v1 per-agent image layout still deployed anywhere? (gates deletions)
3. **Part E** — where should the `claude.md` / `ATX_POC_STATE.md` rationale live? in-repo `docs/`?
4. **Part H** — codify atx deployment as CloudFormation/CDK, or as a committed CI-invoked script
   wrapping the toolkit? (Depends on `AWS::BedrockAgentCore` CFN availability — needs confirming.)
5. **Part I** — does GitLab's `.gitlab-ci.yml` already have an atx build/deploy stage, or is atx still
   hand-deployed? Who owns adding it, and is `.sync-config` protecting the right target-only files?
6. **Green-light Phase 1 + 2** (no deployment impact) so cleanup can start while git/deploy decisions settle.

---

## Part J — Refreshed inventory & cleanup sequence (branch `feat/atx-refactor`)

Re-run against `feat/atx-refactor` @ `19e3b28` (latest `main`, PR #99 merged, so Leo's
schema-design + report renderers + storage refactor are all in). This section **supersedes
Parts B and E where they conflict.** All references verified by repo-wide grep.

### J.1 Corrections to the earlier analysis

- **Schema-design now exists** (6 A2A tools + a single parametrized subagent/core). **Reality-check
  is the only phase with no deployed subagent.**
- **The system prompt is now self-contradictory** (`orchestrator.py`): its intro says *"Reality Check
  and Schema Design have NO deployed subagent yet"*, but tool item 11 correctly documents schema-design
  as available, and "Key points" still calls empty `table_mappings`/`query_groups` a "known gap." All
  three must be reconciled: schema-design is available; only reality-check is missing.
- The **tool-layer duplication** is a new headline finding; the `core.py` duplication still stands.

### J.2 Dead code to delete

**Orphaned per-agent deployment topology.** The single-image path (`Dockerfile.atx` +
`atx_entrypoint.py`, dispatching on `AGENT_TYPE`) is the only one used. Nothing in CI, `scripts/`, or
`pipeline/` references the per-agent files — only `README.md` and this doc mention them.

- 9 fully-orphaned entrypoints: `collector_app.py`, `triage_app.py`, `assignment_app.py`,
  `analysis_{dynamodb,documentdb,elasticache,opensearch,aurora_pg,aurora_mysql}_app.py`.
- `app.py::main` (orphaned) — **keep `app.py::build_agent_factory`, it's live** (called by
  `atx_entrypoint` for the orchestrator).
- 10 per-agent Dockerfiles: `Dockerfile`, `Dockerfile.collector`, `Dockerfile.triage`,
  `Dockerfile.assignment`, `Dockerfile.analysis-*` (×6).
- Note: schema-design never had per-agent apps/Dockerfiles, so the topology was already inconsistent.
- **Caveat before deleting the Dockerfiles:** GitLab's `.gitlab-ci.yml` (target-only, not in this repo
  — see Part I) may still reference them. Confirm on the GitLab side first. The `*_app.py` deletions
  are safe now (only their own Dockerfiles reference them).

**Deprecated / unused tools in `tools.py`.**

- Registered in `PIPELINE_TOOLS` but the prompt forbids them: `run_reality_check`, `run_schema_design`,
  `run_synthesis`, `run_full_assessment`. Unregister and delete (reality-check/schema-design/synthesis
  in-process paths are superseded by the A2A tools).
- Unregistered and used only by `scripts/atx_*_test.py`: `run_collect`, `run_triage`,
  `run_collect_and_triage`, `run_assignment`. Delete and update those scripts to call the `*_core`
  functions directly, or drop the scripts.

**Other dead code.**

- `a2a.py::send_and_wait` — referenced only by tests (prod uses `invoke_and_wait`). Remove and migrate
  its ~15 tests onto `invoke_and_wait`. Then `StubAgenticApiClient.invoke_agent` (self-described as
  retained for old tests) can likely go too.
- `subagent_base.py` — strip the 7 `DIAG` `logger.info` calls in `extract_text`/`parse_invocation` and
  demote the 2 raw-payload INFO dumps (work_fn summary) to `debug`.

### J.3 Duplication to collapse — the target pattern already exists in-repo

Both duplication sites have a de-duplicated sibling to copy:

- **`core.py`:** collapse the 6 `run_analysis_*_core` functions (L314–894; the ~15-line `level_counts`
  confidence-band block is byte-identical in all six) into one `run_analysis_core(engine, ...)` + a
  per-engine config table. Per-engine variation is exactly 5 knobs: engine name/prefix, `analyze_for_*`
  fn, whether `llm_mode="none"` is passed, whether the Mermaid artifact is gated, and `llm_advisor_status`
  (read from trace vs `"not_applicable"`). **Template: `run_schema_design_core`** is already a single
  parametrized function driven by `_DESIGN_SHAPE`.
- **`tools.py`:** collapse the 10 hand-written `run_*_via_a2a` wrappers (collect, triage, 6 analysis,
  assignment, synthesis) into a shared `_run_via_a2a(...)` + thin per-phase tools. **Template:
  `_run_schema_design_via_a2a` + `_SCHEMA_ENGINES` + the `_SCHEMA_DOC` docstring loop** — the six
  schema wrappers are already done this way.
- **Dockerfiles / `*_app.py` / subagent skeletons:** the copy-paste here disappears once J.2 deletes
  the orphaned files (only `Dockerfile.atx` and the `*_subagent.py` factories remain).

### J.4 Prioritized sequence (lowest-risk / highest-value first)

Each step is independently shippable and verifiable; none changes deterministic behavior.

1. **Delete orphaned per-agent topology** — the 9 `*_app.py`, `app.py::main`, and (pending the GitLab
   check) the 10 per-agent Dockerfiles. Biggest surface reduction, near-zero risk.
2. **Trim `tools.py` + reconcile the prompt** — remove the 8 dead tools; fix the schema-design
   contradiction so the prompt states only reality-check is unavailable.
3. **Collapse `core.py` analysis functions** — behavior-preserving; diff against the golden fixture.
4. **Collapse `tools.py` `via_a2a` wrappers** — mirror the schema pattern.
5. **Strip DIAG logging; remove `send_and_wait`** and migrate its tests.
6. **Docs** — rewrite `aws-transform-handoff.md` to match reality; fold the `Fxx`/`Axx` rationale in-repo.

### J.5 Verification (every step)

Run the CI command in an SDK-absent env (mirrors CI):
`uv sync --extra dev` then `uv run --no-sync pytest tests/unit/ tests/contract/ --cov-fail-under=65`.
Where the SDK is present, also run `scripts/atx_*_test.py`. Each step must keep the
`artifacts/discourse/15e6403d` fixture reproduction byte-identical — the deterministic outputs must not
move.

---

## Deferred backlog (captured 2026-08-28)

### 1. Per-phase sub-step progress in the WebApp Job Plan (deferred — needs design)

**Goal:** make the Job Plan panel feel alive by showing intra-phase checkboxes as
each subagent runs, e.g. Collector → `collection retrieved` → `analysing schema &
queries` → `output contract written`; Triage → its own sub-steps. Today each phase
is a single flat step that flips NOT_STARTED → IN_PROGRESS → SUCCEEDED.

**Feasibility:** the API supports it. `PutJobPlan` nodes take a recursive
`subSteps` list (`{stepLabel, stepName, description, subSteps}`), `UpdateJobPlanStep`
updates any step by `stepId`, and `ListJobPlanSteps` can resolve step IDs.
`job_plan.py::put_job_plan` already forwards a `subSteps` field when present.

**Why it is NOT a small change:** phase work runs in the *subagent* over A2A, and
the orchestrator's `invoke_and_wait` is blocking — the orchestrator only sees
"phase started / finished", so it cannot tick sub-steps mid-phase. Real intra-phase
progress must be emitted BY THE SUBAGENT. That requires:

  1. `declare_pipeline_plan` (orchestrator) declares each phase node WITH `subSteps`;
     `put_job_plan` returns their stepIds in `mappings` (already flattened into the
     registry).
  2. `run_<phase>_via_a2a` passes that phase's sub-step stepIds to the subagent in
     the A2A message (or the subagent resolves them itself via `ListJobPlanSteps`
     by stepLabel — more decoupled, one extra API call).
  3. The subagent's core (`run_collect_core`, triage core, ...) calls
     `update_job_plan_step(substep_id, IN_PROGRESS/SUCCEEDED)` at each boundary.

**Open question to resolve first:** whether a *subagent* instance is permitted to
write the job plan (today only the orchestrator does). Unknown from the API model;
needs a live test.

**Recommended first step:** prototype on the collector only (3 sub-steps) on the
`-estserna` slice and watch whether the sub-steps light up. That one test validates
both the mechanism and the subagent-write permission. If it passes, extending to
triage + analysis/schema/synthesis is mechanical.

### 2. Pipeline-ise the ATX deploy (the real goal)

`pipeline/atx_deploy.py` (local, untracked) is the reproducible build/apply/destroy/
status harness — it is the thing to port into `gitlabs.dev/modernizer` and wire into
`.gitlab-ci.yml`, replacing the hand-run toolkit deploys (v2 reached runtime version
16 by manual updates, uncaptured in git). `apply` is idempotent (create on first run,
in-place `update_agent_runtime` after), so the pipeline calls it the same way per
environment. Known follow-up before productionising: `apply` skips re-publishing the
registry version when it already exists, so a change to an orchestrator's declared
`agentDependencies` (agentCard) needs a new published version — add version bumping
or a `--republish` flag so dependency changes propagate without a manual deregister.

### Collector input format note (resolved 2026-08-28)

The collector reads the uploaded collection via `store.read_json`, which assumes the
S3 object body is pure JSON. A WebApp upload that carries a stray leading label line
(observed: `collection_output\n{...}` on one `wordpress-collection.json`) fails with
`JSONDecodeError: Expecting value: line 1 column 1 (char 0)`. Clean collector output
(starting with `{`) parses fine. If the WebApp is found to prepend that line
consistently, make the read tolerant (strip a leading non-JSON label line before
`json.loads`); otherwise treat it as a bad input file.
