# ATX AgentCore GitLab Pipeline

Status: **implemented and validated end-to-end** on branch `feat/atx-refactor`
(2026-08-28). A full build -> ECR -> deploy -> registry run succeeded on an
ephemeral per-branch fleet (`db-modernization-feat-atx-refact`): all three agents
created their AgentCore runtimes, registered, published v1.0.0, and were enabled.

Pipeline repo: `/Users/estserna/Documents/gitlabs.dev/modernizer` (synced manually
from GitHub). This doc lives in GitHub (the sync source).

## Goal

Pipeline-driven create/update of the AWS Transform ATX AgentCore agents,
per-environment isolation (fleet prefix `db-modernization-<env>`), and teardown of
test environments. The deploy logic is `pipeline/atx_deploy.py` (verbs: build /
apply / destroy / status); CI drives it rather than reimplementing it, because the
registry calls (`register_agent`, `publish_agent_version`) go through a custom
botocore model.

## What was built (all on `feat/atx-refactor`)

Build (`.gitlab/ci/build.yml`, `build-branches.yml`):

- Reusable `.kaniko_build_arm64` template: `tags: [arch:arm64, size:xlarge]`,
  `gcr.io/kaniko-project/executor:debug`, static ECR token auth, build+push via
  `--destination`, `--custom-platform=linux/arm64`, `--digest-file`, and the
  pushed `repo@digest` written to a `build.env` dotenv artifact (`IMAGE_URI`).
- `build-agent-atx` (main) and `build-feature-agent-atx` (feature branches), both
  `ECR_REPO_SUFFIX: atx`, `DOCKERFILE_PATH: src/atx_orchestrator/Dockerfile.atx`.

Deploy (`.gitlab/ci/deploy-dev.yml`, `deploy-branches.yml`):

- `deploy-agent-atx` (main, `ATX_ENV=dev` -> `db-modernization-dev`). Runs on a
  stock `python:3.12-slim` image: pip-installs boto3 + awscli +
  `agent-builder-sdk-aws-transform`, registers the `atxagentregistryexternal`
  botocore model (mirrors `Dockerfile.atx`), then `atx_deploy.py apply
  --image-uri $IMAGE_URI` (digest from `build.env`).
- `deploy-feature-agent-atx` (ephemeral per-branch): its own GitLab environment
  `atx-feature/<slug>`, `on_stop: destroy-feature-agent-atx`, `auto_stop_in: 1
  week`, manual trigger. `destroy-feature-agent-atx` does a full-fleet reap.
- Slug capped at 10 chars (tighter than the ECS 15) so
  `db_modernization_feat_<slug>_orchestrator` stays under AgentCore's 48-char
  runtime-name limit.

Prod + cleanup (`scripts/promote-images.sh`, `.gitlab/ci/deploy-prod.yml`,
`.gitlab/ci/cleanup.yml`):

- `promote-images.sh` `SERVICES` includes `atx` (skopeo `copy --all` carries the
  arm64 manifest to `modernizer-prod-atx`). It now skips a service whose prod repo
  does not exist yet (warn + continue), so a newly added service can't hard-fail
  the shared promote job before its prod repo is created.
- `deploy-agent-atx-prod` (main, `ATX_ENV=prod` -> `dbmod-prod`): mirror of
  `deploy-agent-atx`, manual like `deploy-production`. Deploys the promoted,
  immutable prod image `modernizer-prod-atx:<tag>` (fails fast if that tag is
  missing). AgentCore roles and runner Policy 8 are account-level, shared with
  dev, so no prod-specific IAM.
- `cleanup-agent-atx-on-merge` (main): detects the merged feature branch (message
  parse + squash-merge API fallback) and full-fleet-destroys its fleet.

Infra:

- `core-infra.yaml`: `EcrRepositoryAtx` (`modernizer-<env>-atx`) — KMS, IMMUTABLE,
  scan-on-push, standard lifecycle, plus Uri/Arn outputs.
- `gitlab-runner-iam.yaml`: Policy 8 `AgentCorePolicy` (see IAM section).

## Resolved unknowns (the things we had to discover)

### arm64 build — solved cleanly

The shared `gitlab.aws.dev` runner fleet offers every runner size in **both**
`amd64` and `arm64`; you pick with a job tag. So `tags: [arch:arm64, size:xlarge]`
is all it takes — no separate ASG, no emulation. Kaniko builds natively for the
runner's architecture (it cannot cross-build), so landing on an arm64 runner is
what makes the image arm64; `--custom-platform=linux/arm64` just stamps the
manifest. Confirmed against BuilderHub docs and multiple production repos, then
proven by the actual build.

### ECR repo name — `modernizer-dev-atx`, not `agent-atx`

`modernizer-dev-agent-atx` already existed as an ad-hoc repo the harness
auto-created for the manual `estserna` test slice (MUTABLE, AES256, ~19 images,
in use). ECR encryption is fixed at creation, so it can't be reconciled to the
CFN template's KMS/IMMUTABLE without recreating it. Rather than delete a live
repo, the CFN/pipeline repo is named with the suffix `atx` ->
`modernizer-dev-atx`, created clean by CloudFormation. The old `agent-atx` repo is
left untouched. The `*-agent-atx` CI **job names** are unchanged (cosmetic; the
repo name is controlled only by `RepositoryName`, `ECR_REPO_SUFFIX`, and the
`promote-images.sh` `SERVICES` entry).

### Runner IAM — the exact action set (Policy 8, `gitlab-runner-iam.yaml`)

Discovered by successive `AccessDenied` failures; `create_agent_runtime` fans out
into several resources.

- `bedrock-agentcore:*AgentRuntime*` and `bedrock-agentcore:*WorkloadIdentity*`
  on `*`. `create_agent_runtime` provisions the runtime, its DEFAULT **endpoint**
  (`CreateAgentRuntimeEndpoint`), and a **workload identity**
  (`CreateWorkloadIdentity`). Enumerating each sub-action meant a stack redeploy
  per discovery, so the grant is scoped to those two resource families with
  action wildcards.
- `transform-registry:*` for the agent registry
  (`RegisterAgent`/`PublishAgentVersion`/`UpdatePublisherAccessControl`/`GetAgent`/
  `DeregisterAgent`). **Not `transform-agents`** — that prefix is the Agentic API
  used by the runtime execution/invoke roles, not the registry. This one bit us:
  the first guess was `transform-agents:*` and it failed with
  `transform-registry:RegisterAgent` denied.
- `iam:PassRole` for `AgentCoreExecutionRole` and `AWSTransformAgentInvokeRole`.
  The runner's existing PassRole is scoped to `modernizer-*`, which does not match
  these two role names, so they're granted explicitly.

The registry endpoint (`iad.prod.agent-registry-external.elastic-gumby.ai.aws.dev`)
is reachable from the runner (register/publish succeeded).

## Deploy sequence (per environment)

One-time infra prerequisites (manual, independent of branch):

1. Deploy the `gitlab-runner-iam` stack so the runner role carries Policy 8.
2. Deploy `core-infra` so the `modernizer-<env>-atx` ECR repo exists. On main this
   is automatic via `deploy-shared`; on a feature branch it must be deployed once
   manually (deploy-shared is main-only). The `deploy_stack` helper needs
   `STACK_NAME_PREFIX` and `ENV` exported and must run from the repo root; the
   equivalent direct call is `aws cloudformation deploy --template-file
   infrastructure/cloudformation/core-infra.yaml --stack-name modernizer-dev
   --parameter-overrides Environment=dev ProjectName=modernizer --capabilities
   CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset`.

Then: push the branch -> `build-*-agent-atx` builds+pushes the arm64 image ->
play `deploy-feature-agent-atx` (or, on main, `deploy-agent-atx` runs when
`src/atx_orchestrator/**` or `pipeline/atx_deploy.py` change). No merge is needed
to test on a branch. `apply` is idempotent (update-in-place; destroy skips agents
that don't exist).

## The two ATX roles

`AgentCoreExecutionRole` (runtime execution) and `AWSTransformAgentInvokeRole`
(Transform -> runtime invoke) already exist in the account from the estserna
setup. `pipeline/iam-roles.yaml` defines them for fresh-account reproducibility,
but deploying it where they already exist conflicts (roles already exist,
un-managed), so it is not deployed here. Bringing them under CloudFormation is a
separate import exercise if desired.

## Remaining / follow-ups

- **Prod atx ECR repo bootstrap.** `modernizer-prod-atx` does not exist until prod
  core-infra is deployed (defined in `core-infra.yaml`; `deploy-production` creates
  it). Until then `promote-to-prod` skips atx and `deploy-agent-atx-prod` fails
  fast on the missing image. Deploy prod core-infra once to activate the prod path.
- **Full 16-agent fleet.** The pipeline defaults to the orchestrator/collector/
  triage subset (`ATX_AGENTS`). Scaling to the full fleet is a variable change;
  watch the 48-char runtime-name limit for longer suffixes on non-`dev` envs.

## Harness code-quality notes (deploy repo runs mypy + bandit + checkov)

`atx_deploy.py`:

- mypy `no-any-return`: wrap boto3 dict lookups declared `str` in `str(...)`.
- bandit B404/B603 (subprocess) and B607 (partial path): use `shutil.which`
  instead of `subprocess.run(["which", ...])`; `# nosec B603` on the fixed-argv
  builder calls; `# nosec B404` on the import.

`pipeline/iam-roles.yaml` (checkov, CKV_AWS_111): scope Bedrock invoke to
foundation-model + inference-profile ARNs; documented `checkov:skip=CKV_AWS_111`
for the remaining actions with no resource-level support (xray, logs for
AgentCore-managed groups, ECR auth, the Transform Agentic API).

`core-infra.yaml` / `gitlab-runner-iam.yaml` use the repo's own cfn-nag
(`W13`/`W28`) and checkov (`CKV_AWS_111`) suppression style.
