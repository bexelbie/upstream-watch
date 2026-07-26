---
kind: service
purpose: Daily runner that checks upstream deps and files Todoist tasks
visibility: public
---

# PROJECT CONTEXT

## What this is

A daily runner that monitors upstream dependencies (GitHub repos, Docker Hub
images, PRs) and creates Todoist tasks when changes are detected. Optionally
enriches tasks with LLM analysis. Runs as a container on a timer.

## Tech stack

- Python 3.13 (stdlib only, no pip dependencies)
- Podman container (built via GitHub Actions, pushed to ghcr.io)
- Systemd quadlet + timer for scheduling
- Azure OpenAI for LLM analysis
- Todoist REST API v2 for task creation

## Build

No local build step. The container is built by GitHub Actions on push to main
and on version tags. See `.github/workflows/build.yml`.

## Release flow

GitHub Release notes are generated from the tagged commit message using a
release workflow. To cut a release:

1. Commit with release notes in the message (first line = version, body = changelog)
2. Tag with `v*` (e.g., `git tag v1.0.0`)
3. Push branch and tags (`git push origin main --tags`)

The release workflow creates a GitHub Release. The build workflow pushes
`:vX.Y.Z` and `:latest` container images.

## Project layout

- `check.py` — all logic in one file (checkers, LLM analysis, Todoist integration)
- `config.json.example` — example configuration (production config is not in git)
- `Containerfile` — container image definition
- `upstream-watch.container` — example quadlet unit (template, not production)
- `upstream-watch.timer` — example systemd timer (template, not production)

## Configuration

- `config.json` — static, describes what to watch (not in git, delivered via secrets manager)
- `state.json` — mutable runtime state with last-seen positions (not in git, lives on data volume)
- Environment variables for secrets: `TODOIST_API_TOKEN`, `AZURE_OPENAI_ENDPOINT`,
  `AZURE_OPENAI_API_KEY`, `GITHUB_TOKEN`
- Optional env overrides: `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`

## Testing

No test suite. This is a daily batch script with external dependencies (GitHub API,
Docker Hub API, Todoist API, Azure OpenAI). Validation is via `--dry-run`.

## Pointers

- deploy → `~/repos/infra/systems/vps-flatcar.md` (host, workload table)
- Azure OpenAI deployment → `~/repos/infra/systems/cloud-azure.md` (`gpt-4o-mini-upstream-watch`)

Update those infra files when host facts, what's deployed there, or the Azure deployment
change; keep this repo's own config/behavior detail here instead.
