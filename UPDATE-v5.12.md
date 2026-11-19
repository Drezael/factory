# Factory v5.12 - Planning Gate and automatic Kanban provisioning

This is a complete v5.12 deployment or a safe update from Factory v5.11. Factory remains the **entire** Docker Compose project: n8n + postgres + workflow-sync + github-registry + agent-manager and independently built Codex/Claude/Gemini worker images.

## Update on an existing installation

Use `factory-v5.12-planning-kanban-update.zip`; it does **not** include `.env` and does not reset databases or existing login bind mounts. From the directory that contains `factory/`:

```bash
cd ~/Documents
unzip -o ~/Downloads/factory-v5.12-planning-kanban-update.zip
cd factory
sudo ./factory.sh repair-env
sudo ./factory.sh config
sudo ./factory.sh build-workers all
sudo ./factory.sh start
sudo ./factory.sh workflows-sync-status
sudo ./factory.sh github-boards-ensure
sudo ./factory.sh github-scan
```

Workflow sync imports the changed n8n JSONs and the new board workflow automatically. If workflow 04 is not active, publish it in the n8n UI or, after confirming GitHub permissions, set `FACTORY_GITHUB_AUTO_PUBLISH=true`.

## Private registry repo (`repos/*.yaml` for every monitored target)

```yaml
version: 1
repository: meine-org/backend-api
enabled: true
issues:
  enabled: true
  state: open
  initial_sync: existing
  required_labels: []
  ignored_labels: [factory:review-defect]
agent:
  provider: codex
  role: developer
  auto_execute: false  # enable after agent sign-ins and CI are tested
  timeout_seconds: 900
kanban:
  enabled: true
  owner_type: organization
  owner: meine-org
  auto_create: true
  project_title: 'Factory - meine-org/backend-api'
  view_name: 'Factory Kanban'
  status_field: Status
  columns:
    backlog: Backlog
    ready: Bereit
    planning: Planung
    in_progress: In Arbeit
    waiting_for_answer: Rueckfrage
    review: Review
    done: Erledigt
    failed: Fehler
planning:
  enabled: true
  provider: codex
  max_tasks: 40
pipeline:
  enabled: true
  base_branch: main
  developer_provider: codex
  reviewer_provider: claude
  max_review_cycles: 3
  required_checks: [factory-tests]
  auto_merge: false
```

For an **existing board**, set `kanban.project_number` to its actual number to remove title ambiguity, and keep `auto_create: true` for safe addition of missing fields. For new boards **omit** `project_number`; the adapter searches by exact `project_title` and creates the board only if missing. Names must be unique within a GitHub owner. Board is an owner-level Project v2 linked to the repo, not a project literally inside the Git repository.

## Status and execution lifecycle

1. A feature request / bug / project plan root Issue appears as `Backlog`. A human puts the root Issue into `Bereit`.
2. Workflow 04 calls `POST /boards/ensure` first; its idempotent adapter creates/updates the Project, Status categories and `BOARD_LAYOUT` view, no AI needed. Then it scans Issues.
3. Workflow 07 claims Ready root Issue, sets `Planung` and starts a **fresh, read-only Planner agent**.
4. If information is missing, Planner comments a question in **the same parent issue**, exits and the board moves to `Rueckfrage`. The human answers and manually sets `Bereit`; a new Planner starts, with no prior in-memory session.
5. On success, a structured plan with features, steps, acceptance criteria and dependencies is checked. The adapter creates native GitHub linked sub-issues (Plan -> Feature -> Task) and comments links back into the Planning Issue. Children initially have `Backlog` Status; retries do not duplicate those with matching plan markers.
6. **Only after all sub-issues are published** is the parent Planning Issue set to `Erledigt` and closed. The next scan releases unblocked Tasks to `Bereit`, holding dependent Tasks in `Backlog` until prerequisites are `Erledigt`. No developer is started before this gate. Feature grouping issues are not dispatched.
7. Each Task receives its own disposable Developer agent, own `factory/issue-N` branch/PR, fresh Reviewer agent, GitHub CI checks and optional Squash merge when all required checks and statuses pass. Actual issue status stays in GitHub Projects. Review defects become separate linked error Issues.

**Important:** All *root* Issues in a registry repository with `planning.enabled: true` enter planning first. For special maintenance tickets that should not be planned, use a separate registry policy/repo until per-issue overrides are implemented. `planning.enabled: false` leaves the earlier v5.11 flow.

## Permissions and switches

Locally in `.env` set `FACTORY_GITHUB_REGISTRY_REPO`, `FACTORY_GITHUB_REGISTRY_TOKEN`, and `FACTORY_GITHUB_AUTO_PUBLISH`. Registry token must read the private registry, create/update owner Projects v2 and views, link Projects to target repos, read/write Issues including sub-issues, and write code/PRs/statuses for the later pipeline. Different GitHub account/PAT scopes or app permissions may need explicit organization approval. An insufficient token causes a clear provisioning error; do not put write tokens into Issue text or registry YAML.

```ini
FACTORY_GITHUB_AUTO_PUBLISH=true
FACTORY_GITHUB_AUTORUN_ENABLED=false
FACTORY_GITHUB_AUTOMERGE_ENABLED=false
```

Turn on `AUTORUN` only when all provider sign-ins have been configured, the per-repo `agent.auto_execute: true` has been set and testing completed. Turn on `AUTOMERGE` only after required GitHub Actions checks and branch protection are in place. `factory.sh github-boards-ensure` is a manual diagnostic shortcut; regular board provisioning is automatic through n8n workflow 04.

## Tests and limitations

Use `sudo ./factory.sh github-health`, `github-boards-ensure`, `github-scan` and `sudo docker compose logs --tail=100 github-registry`. Local tests validate the Python logic, n8n JSON syntax and Compose configuration; **actual authenticated GitHub API and Docker end-to-end behavior must be tested on your server**.

GitHub API outages, rate limits, simultaneous user edits and changing requirements during partially published plans can need reconciliation. Existing subissues are matched by stable markers; new or removed tasks of later plans are not automatically cancelled. No arbitrary external repos should be executed with elevated container permissions.
