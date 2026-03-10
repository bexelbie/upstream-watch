# Upstream Watch

Monitors upstream GitHub repositories, Docker Hub images, and your own repos
for changes. Creates Todoist tasks when something new appears. Optionally
enriches tasks with LLM analysis of how changes affect your forks and
deployments.

## What it watches

| Name | Source | Detects |
|------|--------|---------|
| hedgedoc | `hedgedoc/hedgedoc` | New 1.x tags |
| minimal-mistakes | `mmistakes/minimal-mistakes` | New commits on master |
| signal-cli-rest-api | `bbernhard/signal-cli-rest-api` (Docker Hub) | New container tags |
| cloudflared | `cloudflare/cloudflared` | New releases (batched, deduped) |
| telegraf | `influxdata/telegraf` | New releases (batched, deduped) |
| python-slim | `python:3.13-slim` | Docker Hub digest changes |
| PRs | `bexelbie/*`, `ElectricPliers/*` | New open pull requests |

## How it works

`check.py` reads `config.json` for what to watch and `state.json` for tracking
positions. It queries GitHub/Docker Hub APIs for changes since the last run,
creates a Todoist task for each event, and updates state.

Each event is its own task: one release = one task. One new PR = one task. A
batch of new commits = one task.

### Config and state

Configuration (`config.json`) and runtime state (`state.json`) are separate
files. Config is static and committable — it describes what to watch and how
to analyze it. State is mutable and lives on the data volume — it tracks
"last seen" positions. You can copy in a new config without losing state.

State is keyed by entry name. Adding a new entry to config works without any
state changes — the first run will initialize its state.

### LLM analysis and priority

Entries with `"analyze": true` get enriched via Azure OpenAI before the Todoist
task is created. Two analysis modes:

- **Fork analysis** (`fork_patches` set): Assesses upstream changes against your
  fork patches — rebase risk, patch obsolescence, conflict likelihood.
- **Deployment analysis** (`usage_context` set): Assesses releases against your
  specific deployment — what's relevant, what's safe to ignore, breaking changes.

The LLM's risk assessment drives Todoist task priority:
- **HIGH** risk → P1 (urgent)
- **MEDIUM** risk → P2
- **LOW** risk → default priority

PRs always get P1 (time-sensitive — someone is waiting).

LLM analysis degrades gracefully: if credentials aren't set, tasks are created
without the analysis section and at default priority.

### Deduplication

Entries with `"batch_releases": true` (cloudflared, telegraf) check Todoist for
an existing open task for the same upstream before creating a new one. This
prevents pile-up when you skip a maintenance window — you get one task, not
three.

State is always updated regardless of dedup, so the next run only looks at
genuinely new releases.

### Docker Hub watching

Two modes for Docker Hub images:

- **Digest watching** (`"watch": "digest"`): Detects when a specific tag's digest
  changes (e.g., `python:3.13-slim` gets rebuilt).
- **Tag watching** (`"watch": "tags"`): Detects new tags matching a pattern (e.g.,
  new `0.98` release of signal-cli-rest-api). Optionally fetches GitHub release
  notes for the matching tag to enrich the task.

### Workflow keepalive

GitHub disables scheduled Actions workflows after 60 days of repository
inactivity. If `workflow_keepalive` is set in config, upstream-watch checks
those repos for disabled workflows and re-enables them automatically. A Todoist
task is created to let you know it happened.

Omit the `workflow_keepalive` key entirely to disable this feature.

## Running locally

```bash
# Dry run (no Todoist tasks, no state changes):
python3 check.py --dry-run

# Seed state (record current positions, no tasks, no LLM calls):
python3 check.py --seed

# Real run (requires Todoist API token and project):
TODOIST_API_TOKEN=your-token python3 check.py

# Override state file location:
UPSTREAM_WATCH_STATE=/path/to/state.json python3 check.py --dry-run
```

## Deployment (Podman on VPS)

### Container image

The container is built automatically by GitHub Actions on push to main and
published to `ghcr.io/bexelbie/upstream-watch:latest`. No manual build needed.

### Install quadlet files

Copy the example `.container` and `.timer` files from this repo, adapt paths
for your system, and install them:

```bash
cp upstream-watch.container /etc/containers/systemd/users/$(id -u)/
cp upstream-watch.timer /etc/containers/systemd/users/$(id -u)/
systemctl --user daemon-reload
```

### Set up data directory

```bash
mkdir -p /home/youruser/upstream-watch
# Place your config.json here (delivered via secret manager or manual copy)
```

### Seed initial state

On first deploy, run with `--seed` to record current positions without
creating tasks. This prevents a flood of notifications for already-known
releases:

```bash
podman run --rm \
  -v /home/youruser/upstream-watch:/data:Z \
  -e GITHUB_TOKEN=your-token \
  ghcr.io/bexelbie/upstream-watch:latest --seed
```

### Configure secrets

Deliver via environment variables (e.g., via op-secret-manager, systemd
`EnvironmentFile`, or your preferred mechanism):

- `TODOIST_API_TOKEN` — required
- `AZURE_OPENAI_ENDPOINT` — required for LLM analysis
- `AZURE_OPENAI_API_KEY` — required for LLM analysis
- `GITHUB_TOKEN` — required for private repo access and workflow keepalive

### Start

```bash
systemctl --user enable --now upstream-watch.timer
```

### Monitor

```bash
# Check timer schedule
systemctl --user list-timers upstream-watch.timer

# See last run output
journalctl --user -u upstream-watch.service --no-pager -n 50

# Manual trigger
systemctl --user start upstream-watch.service
```

## Configuration

`config.json` has five top-level sections:

- **todoist** — project name for tasks
- **github_repos** — repos to watch (each with `watch` type: `releases`, `tags`, or `commits`)
- **docker_images** — Docker Hub images to watch (`digest` or `tags`)
- **pr_watch** — GitHub namespaces to monitor for incoming PRs
- **workflow_keepalive** — repos to check for disabled scheduled workflows

### Adding a new upstream

Add an entry to `github_repos` or `docker_images`:

```json
{
  "name": "some-project",
  "repo": "owner/repo",
  "watch": "releases",
  "context": "Why this matters to you."
}
```

Set `last_seen` in `state.json` to the current latest release/tag/SHA so the
first run doesn't fire on already-known versions.

### GitHub repo entry options

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Display name (also keys into state) |
| `repo` | yes | GitHub `owner/repo` |
| `watch` | yes | `releases`, `tags`, or `commits` |
| `context` | no | Human context included in task description |
| `analyze` | no | Enable LLM analysis for this entry |
| `fork_patches_source` | no | GitHub file with fork patch descriptions (triggers fork analysis) |
| `usage_context_source` | no | GitHub file with deployment config (triggers deployment analysis) |
| `batch_releases` | no | Combine multiple new releases into one task + enable dedup |
| `tag_pattern` | no | Regex filter for tag watching |
| `branch` | no | Branch for commit watching (default: master) |

### Docker image entry options

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Display name (also keys into state) |
| `image` | yes | Docker Hub image (e.g., `python` or `user/repo`) |
| `watch` | yes | `digest` or `tags` |
| `tag` | for digest | Specific tag to monitor digest changes |
| `tag_pattern` | for tags | Regex filter for which tags to track |
| `github_repo` | no | GitHub `owner/repo` to fetch release notes from |
| `context` | no | Human context included in task description |
| `analyze` | no | Enable LLM analysis for this entry |
| `usage_context_source` | no | GitHub file with deployment config (triggers deployment analysis) |

## Secrets

Deliver via environment variables. On VPS, op-secret-manager populates these
from `/home/bexelbie/env`.

| Variable | Required | Purpose |
|----------|----------|---------|
| `TODOIST_API_TOKEN` | yes | Todoist REST API v2 |
| `AZURE_OPENAI_ENDPOINT` | for LLM | Azure OpenAI base URL (e.g. `https://foo.openai.azure.com`) |
| `AZURE_OPENAI_API_KEY` | for LLM | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | no | Model deployment name (default: `gpt-4o-mini`) |
| `AZURE_OPENAI_API_VERSION` | no | API version (default: `2025-01-01-preview`) |
| `GITHUB_TOKEN` | no | Higher GitHub API rate limits + private repo access |

## Open issues

### You need to do

- [ ] Create a Todoist project named "Upstream Watch"
- [ ] Create (or let auto-create) a Todoist label named `upstream-watch`
- [ ] Set environment variables: `TODOIST_API_TOKEN`,
  `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `GITHUB_TOKEN`
- [ ] Build and deploy the container image
- [ ] Copy `config.json` and `state.json` to the data directory on the VPS
- [ ] Test a real run with LLM analysis enabled

### Future

- General information triage system (email, USPS informed delivery, cloud
  provider announcements) — the "Problem B" from initial design
- n8n as a platform for the broader system
- Home Assistant notifications for urgent events (requires Tailscale or
  public API exposure)
- Automated actions: triggering rebase PRs, container rebuilds
- Cloud provider announcement monitoring (Azure, GCP)
- Todoist label auto-creation if it doesn't exist
