#!/usr/bin/env python3
# ABOUTME: Checks configured upstream repositories and registries for changes.
# ABOUTME: Creates Todoist tasks when new releases, tags, commits, or PRs are detected.

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_config(path):
    with open(path) as f:
        return json.load(f)


def load_state(path):
    """Load state from a JSON file. Returns empty dict if file doesn't exist."""
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(state, path):
    """Save state to a JSON file."""
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def get_entry_state(state, section, name):
    """Get the state dict for a specific entry, creating it if needed."""
    if section not in state:
        state[section] = {}
    if name not in state[section]:
        state[section][name] = {}
    return state[section][name]


def github_api(path, method="GET"):
    """Call GitHub API, returns parsed JSON. Returns None for 204 responses."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        if resp.status == 204:
            return None
        return json.loads(resp.read())


def fetch_github_file(repo, path, ref=None):
    """Fetch raw file content from a GitHub repository."""
    url_path = f"/repos/{repo}/contents/{urllib.parse.quote(path)}"
    if ref:
        url_path += f"?ref={urllib.parse.quote(ref)}"
    url = f"https://api.github.com{url_path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github.raw")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8")


def fetch_analysis_context(entry):
    """Fetch fork patches and/or usage context from GitHub source files."""
    fork_content = None
    usage_content = None

    source = entry.get("fork_patches_source")
    if source:
        try:
            fork_content = fetch_github_file(
                source["repo"], source["path"], source.get("ref")
            )
        except Exception as e:
            print(f"  Failed to fetch fork patches: {e}", file=sys.stderr)

    source = entry.get("usage_context_source")
    if source:
        try:
            usage_content = fetch_github_file(
                source["repo"], source["path"], source.get("ref")
            )
        except Exception as e:
            print(f"  Failed to fetch usage context: {e}", file=sys.stderr)

    return fork_content, usage_content


def dockerhub_namespace(image):
    """Resolve Docker Hub API namespace. Official images use library/ prefix."""
    return image if "/" in image else f"library/{image}"


def dockerhub_api(image, tag):
    """Get tag info from Docker Hub."""
    namespace = dockerhub_namespace(image)
    url = f"https://hub.docker.com/v2/repositories/{namespace}/tags/{tag}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def dockerhub_list_tags(image, page_size=50):
    """List recent tags from Docker Hub, ordered by most recently updated."""
    namespace = dockerhub_namespace(image)
    url = (
        f"https://hub.docker.com/v2/repositories/{namespace}/tags/"
        f"?page_size={page_size}&ordering=last_updated"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
        return data.get("results", [])


def todoist_api(method, path, body=None, token=None):
    """Call Todoist REST API v2."""
    url = f"https://api.todoist.com/rest/v2{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        if resp.status == 204:
            return None
        return json.loads(resp.read())


def llm_call(messages):
    """Call Azure OpenAI chat completions API. Returns None if credentials missing."""
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    if not endpoint or not api_key:
        return None

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    url = (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )

    body = {"messages": messages, "max_tokens": 1500}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("api-key", api_key)
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"]


def parse_risk_level(text):
    """Extract risk level from LLM analysis and return Todoist priority value.

    Todoist API: 4=P1(urgent), 3=P2, 2=P3, 1=P4(default).
    Returns None if the risk level cannot be parsed.
    """
    if not text:
        return None
    match = re.search(
        r"[Rr]isk\s+level[^:]*:\s*\*{0,2}(HIGH|MEDIUM|LOW)\*{0,2}",
        text, re.IGNORECASE,
    )
    if not match:
        return None
    return {"HIGH": 4, "MEDIUM": 3, "LOW": 1}[match.group(1).upper()]


def analyze_fork_event(event, fork_patches_content):
    """Ask the LLM to assess upstream changes against fork patches."""
    if not fork_patches_content:
        return event

    messages = [
        {
            "role": "system",
            "content": (
                "You assess upstream repository changes against a "
                "maintained fork. Be concise and specific. Use markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"## Upstream changes\n\n{event['description']}\n\n"
                f"## My fork patches\n\n{fork_patches_content}\n\n"
                f"## Assess\n\n"
                "1. Are any of my patches now unnecessary because "
                "upstream addressed the same issue?\n"
                "2. Are any of my patches likely to conflict with "
                "these upstream changes?\n"
                "3. Risk level for rebase: LOW (clean), MEDIUM "
                "(minor conflicts likely), HIGH (patches need rework)\n"
                "4. Recommended action in one sentence."
            ),
        },
    ]

    analysis = llm_call(messages)
    if analysis:
        event["description"] += f"\n\n### 🤖 Analysis\n\n{analysis}"
        priority = parse_risk_level(analysis)
        if priority is not None:
            event["_priority"] = priority
        else:
            event["_risk_unparsed"] = True

    return event


def analyze_deployment_event(event, usage_context):
    """Ask the LLM to assess release impact on a specific deployment."""
    if not usage_context:
        return event

    messages = [
        {
            "role": "system",
            "content": (
                "You assess software releases for impact on a specific "
                "deployment configuration. Be concise. Clearly separate "
                "what's relevant from what's not. Use markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"## Release details\n\n{event['description']}\n\n"
                f"## My deployment\n\n{usage_context}\n\n"
                f"## Assess\n\n"
                "1. What in this release is relevant to my use case?\n"
                "2. What can I safely ignore?\n"
                "3. Any breaking changes or required action?\n"
                "4. Risk level: LOW (safe to roll), MEDIUM (test first), "
                "HIGH (review carefully)\n"
                "5. Recommended action in one sentence."
            ),
        },
    ]

    analysis = llm_call(messages)
    if analysis:
        event["description"] += f"\n\n### 🤖 Analysis\n\n{analysis}"
        priority = parse_risk_level(analysis)
        if priority is not None:
            event["_priority"] = priority
        else:
            event["_risk_unparsed"] = True

    return event


# --- Source checkers ---
# Each returns a list of events (dicts with 'title' and 'description' keys).
# Each event becomes one Todoist task.


def check_releases(entry, entry_state):
    """Check for new GitHub releases. Supports batching for low-priority deps."""
    repo = entry["repo"]
    last_seen = entry_state.get("last_seen", "")
    batch_mode = entry.get("batch_releases", False)

    releases = github_api(f"/repos/{repo}/releases?per_page=20")
    new_releases = []

    for release in releases:
        tag = release["tag_name"]
        if tag == last_seen:
            break
        new_releases.append(release)

    if not new_releases:
        return []

    entry_state["last_seen"] = new_releases[0]["tag_name"]

    if batch_mode and len(new_releases) > 0:
        oldest = new_releases[-1]["tag_name"]
        newest = new_releases[0]["tag_name"]
        release_list = "\n".join(
            f"- [{r['tag_name']}]({r.get('html_url', '')})"
            f" ({r.get('published_at', '')[:10]})"
            for r in new_releases
        )
        return [{
            "title": (
                f"🔭 {entry['name']}: {len(new_releases)} "
                f"release{'s' if len(new_releases) != 1 else ''} "
                f"({oldest} → {newest})"
            ),
            "description": (
                f"**Repo:** [{repo}](https://github.com/{repo})\n"
                f"**Releases:**\n\n{release_list}\n\n"
                f"---\n*{entry.get('context', '')}*"
            ),
            "_dedup_name": entry["name"],
        }]

    events = []
    for release in new_releases:
        tag = release["tag_name"]
        body = release.get("body", "") or ""
        if len(body) > 2000:
            body = body[:2000] + "\n\n_(truncated)_"

        events.append({
            "title": f"🔭 {entry['name']}: {release.get('name', tag)} released",
            "description": (
                f"**Repo:** [{repo}](https://github.com/{repo})\n"
                f"**Release:** [{tag}]({release.get('html_url', '')})\n"
                f"**Published:** {release.get('published_at', 'unknown')}\n\n"
                f"### Release notes\n\n{body}\n\n"
                f"---\n*{entry.get('context', '')}*"
            ),
        })

    return events


def check_tags(entry, entry_state):
    """Check for new GitHub tags matching a pattern."""
    repo = entry["repo"]
    pattern = entry.get("tag_pattern", ".*")
    last_seen = entry_state.get("last_seen", "")

    tags = github_api(f"/repos/{repo}/tags?per_page=30")
    matching = [t for t in tags if re.match(pattern, t["name"])]
    events = []

    for tag in matching:
        if tag["name"] == last_seen:
            break

        tag_url = f"https://github.com/{repo}/releases/tag/{tag['name']}"

        # Try to fetch release notes for this tag
        release_body = ""
        try:
            release = github_api(
                f"/repos/{repo}/releases/tags/{tag['name']}"
            )
            release_body = release.get("body", "") or ""
            if len(release_body) > 2000:
                release_body = release_body[:2000] + "\n\n_(truncated)_"
        except urllib.error.HTTPError:
            pass

        description = (
            f"**Repo:** [{repo}](https://github.com/{repo})\n"
            f"**Tag:** [{tag['name']}]({tag_url})\n"
        )
        if release_body:
            description += f"\n### Release notes\n\n{release_body}\n"
        description += f"\n---\n*{entry.get('context', '')}*"

        events.append({
            "title": f"🔭 {entry['name']}: {tag['name']} tagged",
            "description": description,
        })

    if events:
        entry_state["last_seen"] = matching[0]["name"]

    return events


def check_commits(entry, entry_state):
    """Check for new commits on a branch. Reports as a single batch event."""
    repo = entry["repo"]
    branch = entry.get("branch", "master")
    last_seen = entry_state.get("last_seen", "")

    commits = github_api(
        f"/repos/{repo}/commits?sha={branch}&per_page=50"
    )
    new_commits = []

    for commit in commits:
        sha = commit["sha"]
        if sha == last_seen or sha.startswith(last_seen):
            break
        new_commits.append({
            "sha": sha[:12],
            "message": commit["commit"]["message"].split("\n")[0],
            "url": commit["html_url"],
        })

    if not new_commits:
        return []

    entry_state["last_seen"] = commits[0]["sha"]

    commit_list = "\n".join(
        f"- [`{c['sha']}`]({c['url']}) {c['message']}"
        for c in new_commits
    )

    return [{
        "title": (
            f"🔭 {entry['name']}: "
            f"{len(new_commits)} new commit{'s' if len(new_commits) != 1 else ''}"
        ),
        "description": (
            f"**Repo:** [{repo}](https://github.com/{repo})\n"
            f"**Branch:** {branch}\n"
            f"**Commits:**\n\n{commit_list}\n\n"
            f"---\n*{entry.get('context', '')}*"
        ),
    }]


def check_docker_digest(entry, entry_state):
    """Check if a Docker Hub image digest has changed."""
    image = entry["image"]
    tag = entry["tag"]
    last_seen = entry_state.get("last_seen_digest", "")

    data = dockerhub_api(image, tag)
    current_digest = data.get("digest", "")

    if not current_digest:
        return []

    if current_digest == last_seen:
        return []

    entry_state["last_seen_digest"] = current_digest

    return [{
        "title": f"🔭 {entry['name']}: {image}:{tag} base image updated",
        "description": (
            f"**Image:** `{image}:{tag}`\n"
            f"**Previous digest:** `{last_seen[:20]}...`\n"
            f"**Current digest:** `{current_digest[:20]}...`\n"
            f"**Updated:** {data.get('last_updated', 'unknown')}\n\n"
            f"---\n*{entry.get('context', '')}*"
        ),
    }]


def check_docker_tags(entry, entry_state):
    """Check for new Docker Hub tags matching a pattern.

    Optionally fetches GitHub release notes for LLM enrichment when
    the entry includes a github_repo field.
    """
    image = entry["image"]
    pattern = entry.get("tag_pattern", ".*")
    last_seen = entry_state.get("last_seen", "")
    github_repo = entry.get("github_repo", "")

    tags = dockerhub_list_tags(image)
    matching = [t for t in tags if re.match(pattern, t["name"])]
    new_tags = []

    for tag in matching:
        if tag["name"] == last_seen:
            break
        new_tags.append(tag)

    if not new_tags:
        return []

    entry_state["last_seen"] = new_tags[0]["name"]

    events = []
    for tag in new_tags:
        tag_name = tag["name"]

        release_body = ""
        if github_repo:
            try:
                release = github_api(
                    f"/repos/{github_repo}/releases/tags/{tag_name}"
                )
                release_body = release.get("body", "") or ""
                if len(release_body) > 2000:
                    release_body = release_body[:2000] + "\n\n_(truncated)_"
            except urllib.error.HTTPError:
                pass

        description = (
            f"**Image:** `{image}:{tag_name}`\n"
            f"**Published:** {tag.get('last_updated', 'unknown')}\n"
        )
        if release_body:
            description += f"\n### Release notes\n\n{release_body}\n"
        elif github_repo:
            description += (
                f"\n⚠️ No release notes found for tag `{tag_name}` on "
                f"[GitHub]"
                f"(https://github.com/{github_repo}/releases). "
                f"Check the repo manually.\n"
            )
        description += f"\n---\n*{entry.get('context', '')}*"

        event = {
            "title": f"🔭 {entry['name']}: {tag_name} container published",
            "description": description,
        }
        if not release_body:
            event["_skip_analysis"] = True
        events.append(event)

    return events


def check_prs(pr_config, pr_state):
    """Check for new open PRs across configured namespaces."""
    known = set(pr_state.get("known_prs", []))
    namespaces = pr_config.get("namespaces", [])
    events = []
    all_current_prs = set()

    for ns in namespaces:
        ns_type = ns.get("type", "user")
        ns_name = ns["name"]
        query = urllib.parse.quote(f"is:pr is:open {ns_type}:{ns_name}")

        try:
            results = github_api(f"/search/issues?q={query}&per_page=100")
        except urllib.error.HTTPError as e:
            print(f"  Warning: PR search failed for {ns_name}: {e}",
                  file=sys.stderr)
            continue

        for item in results.get("items", []):
            pr_url = item["html_url"]
            all_current_prs.add(pr_url)

            if pr_url not in known:
                repo_name = "/".join(pr_url.split("/")[3:5])
                events.append({
                    "title": (
                        f"🔭 PR on {repo_name}: "
                        f"{item['title']}"
                    ),
                    "description": (
                        f"**PR:** [{item['title']}]({pr_url})\n"
                        f"**Author:** {item['user']['login']}\n"
                        f"**Opened:** {item['created_at']}\n"
                    ),
                    "_priority": 4,
                })

    # Update known PRs: add new ones, remove closed ones
    pr_state["known_prs"] = sorted(all_current_prs)

    return events


def reenable_disabled_workflows(repos, dry_run=False):
    """Check repos for workflows disabled by inactivity and re-enable them.

    Returns a list of event dicts for any workflows that were re-enabled
    (or would be, in dry-run mode).
    """
    events = []
    for repo in repos:
        try:
            data = github_api(f"/repos/{repo}/actions/workflows")
        except urllib.error.HTTPError as e:
            print(f"  Warning: workflow list failed for {repo}: {e}",
                  file=sys.stderr)
            continue

        for wf in data.get("workflows", []):
            if wf.get("state") != "disabled_inactivity":
                continue

            wf_name = wf.get("name", wf["path"])
            if not dry_run:
                try:
                    github_api(
                        f"/repos/{repo}/actions/workflows/{wf['id']}/enable",
                        method="PUT",
                    )
                    print(f"  Re-enabled: {repo} / {wf_name}")
                except urllib.error.HTTPError as e:
                    print(f"  Failed to re-enable {repo} / {wf_name}: {e}",
                          file=sys.stderr)
                    continue

            events.append({
                "title": f"🔭 Re-enabled workflow: {repo} / {wf_name}",
                "description": (
                    f"**Repo:** [{repo}](https://github.com/{repo})\n"
                    f"**Workflow:** {wf_name}\n"
                    f"**Path:** `{wf['path']}`\n\n"
                    f"GitHub disabled this workflow due to 60 days of "
                    f"repository inactivity. It has been re-enabled "
                    f"automatically."
                ),
            })

    return events


# --- Todoist integration ---


def resolve_todoist_project(project_name, token):
    """Look up a Todoist project by name, return its ID."""
    projects = todoist_api("GET", "/projects", token=token)
    for project in projects:
        if project["name"] == project_name:
            return project["id"]
    return None


def find_open_todoist_tasks(project_id, token):
    """Fetch all open tasks in the upstream-watch project."""
    tasks = todoist_api("GET", f"/tasks?project_id={project_id}", token=token)
    return tasks or []


def has_existing_task_for(upstream_name, open_tasks):
    """Check if there's already an open task for this upstream."""
    prefix = f"🔭 {upstream_name}:"
    return any(t.get("content", "").startswith(prefix) for t in open_tasks)


def create_todoist_task(event, project_id, token):
    """Create a Todoist task from an event."""
    body = {
        "content": event["title"],
        "description": event["description"],
        "project_id": project_id,
        "labels": ["upstream-watch"],
    }
    priority = event.get("_priority", 1)
    if priority > 1:
        body["priority"] = priority
    return todoist_api("POST", "/tasks", body=body, token=token)


# --- Main ---


def main():
    dry_run = "--dry-run" in sys.argv
    seed_mode = "--seed" in sys.argv

    config_path = os.environ.get(
        "UPSTREAM_WATCH_CONFIG",
        os.path.join(os.path.dirname(__file__), "config.json"),
    )
    state_path = os.environ.get(
        "UPSTREAM_WATCH_STATE",
        os.path.join(os.path.dirname(config_path), "state.json"),
    )

    print(f"Loading config from {config_path}")
    config = load_config(config_path)
    print(f"Loading state from {state_path}")
    state = load_state(state_path)

    if seed_mode:
        print("Seed mode: running all checkers to initialize state, "
              "no tasks will be created")

    todoist_token = os.environ.get("TODOIST_API_TOKEN", "")
    project_name = config.get("todoist", {}).get(
        "project_name", "Upstream Watch"
    )
    llm_available = not seed_mode and bool(
        os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        and os.environ.get("AZURE_OPENAI_API_KEY", "")
    )
    if not seed_mode:
        if llm_available:
            print("LLM analysis enabled (Azure OpenAI)")
        else:
            print("LLM analysis disabled (no credentials)")

    project_id = None
    open_tasks = []
    if not dry_run and not seed_mode:
        if not todoist_token:
            print("Error: TODOIST_API_TOKEN not set", file=sys.stderr)
            sys.exit(1)
        project_id = resolve_todoist_project(project_name, todoist_token)
        if not project_id:
            print(
                f"Error: Todoist project '{project_name}' not found",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Todoist project '{project_name}' -> {project_id}")
        open_tasks = find_open_todoist_tasks(project_id, todoist_token)
        print(f"Found {len(open_tasks)} open task(s) in project")

    all_events = []

    # Check GitHub repos
    for entry in config.get("github_repos", []):
        watch_type = entry.get("watch", "releases")
        entry_state = get_entry_state(state, "github_repos", entry["name"])
        print(f"Checking {entry['name']} ({watch_type})...", end=" ")

        try:
            if watch_type == "releases":
                events = check_releases(entry, entry_state)
            elif watch_type == "tags":
                events = check_tags(entry, entry_state)
            elif watch_type == "commits":
                events = check_commits(entry, entry_state)
            else:
                print(f"unknown watch type '{watch_type}', skipping")
                continue

            if events and entry.get("analyze") and llm_available:
                fork_content, usage_content = fetch_analysis_context(entry)
                print(f"{len(events)} event(s), analyzing...", end=" ")
                for i, event in enumerate(events):
                    try:
                        if fork_content:
                            events[i] = analyze_fork_event(
                                event, fork_content
                            )
                        elif usage_content:
                            events[i] = analyze_deployment_event(
                                event, usage_content
                            )
                    except Exception as e:
                        print(f"LLM error: {e}", file=sys.stderr)
                print("done")
            else:
                print(f"{len(events)} event(s)")

            all_events.extend(events)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)

    # Check Docker images
    for entry in config.get("docker_images", []):
        watch_type = entry.get("watch", "digest")
        entry_state = get_entry_state(state, "docker_images", entry["name"])
        print(f"Checking {entry['name']} ({watch_type})...", end=" ")

        try:
            if watch_type == "digest":
                events = check_docker_digest(entry, entry_state)
            elif watch_type == "tags":
                events = check_docker_tags(entry, entry_state)
            else:
                print(f"unknown watch type '{watch_type}', skipping")
                continue

            if events and entry.get("analyze") and llm_available:
                fork_content, usage_content = fetch_analysis_context(entry)
                print(f"{len(events)} event(s), analyzing...", end=" ")
                for i, event in enumerate(events):
                    if event.get("_skip_analysis"):
                        continue
                    try:
                        if usage_content:
                            events[i] = analyze_deployment_event(
                                event, usage_content
                            )
                    except Exception as e:
                        print(f"LLM error: {e}", file=sys.stderr)
                print("done")
            else:
                print(f"{len(events)} event(s)")

            all_events.extend(events)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)

    # Check PRs
    pr_config = config.get("pr_watch")
    if pr_config:
        if "pr_watch" not in state:
            state["pr_watch"] = {}
        pr_state = state["pr_watch"]
        print("Checking for new PRs...", end=" ")
        try:
            events = check_prs(pr_config, pr_state)
            print(f"{len(events)} event(s)")
            all_events.extend(events)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)

    # Re-enable workflows disabled by inactivity
    keepalive_repos = config.get("workflow_keepalive", [])
    if keepalive_repos:
        print("Checking for disabled workflows...", end=" ")
        try:
            events = reenable_disabled_workflows(keepalive_repos, dry_run)
            print(f"{len(events)} re-enabled")
            all_events.extend(events)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)

    # Create a P1 meta-task if any LLM analyses had unparseable risk levels
    unparsed = [e for e in all_events if e.get("_risk_unparsed")]
    if unparsed:
        names = ", ".join(e["title"] for e in unparsed)
        all_events.append({
            "title": "🔭 ⚠️ Could not determine risk level from LLM analysis",
            "description": (
                f"The following {len(unparsed)} task(s) got LLM analysis "
                f"but the risk level could not be parsed from the response. "
                f"Check them manually to assess urgency.\n\n"
                + "\n".join(f"- {e['title']}" for e in unparsed)
            ),
            "_priority": 4,
        })

    # Report results
    print(f"\nTotal: {len(all_events)} event(s)")

    if seed_mode:
        save_state(state, state_path)
        print(f"\nState saved to {state_path}. All current positions recorded.")
        print("Next run will only report changes after this point.")
        return

    if dry_run:
        for event in all_events:
            dedup_tag = " [DEDUP]" if event.get("_dedup_name") else ""
            priority = event.get("_priority", 1)
            priority_tag = f" [P{5 - priority}]" if priority > 1 else ""
            print(f"\n{'=' * 60}")
            print(f"TASK: {event['title']}{dedup_tag}{priority_tag}")
            print(f"{'=' * 60}")
            print(event["description"])
        if not all_events:
            print("No changes detected.")
        print("\n(dry run — no tasks created, state not saved)")
        print("Note: [DEDUP] tasks would be skipped if a matching "
              "open task exists in Todoist")
        return

    # Create Todoist tasks, then save state
    created = 0
    skipped = 0
    for event in all_events:
        dedup_name = event.pop("_dedup_name", None)
        if dedup_name and has_existing_task_for(dedup_name, open_tasks):
            print(f"  Skipped (existing task): {event['title']}")
            skipped += 1
            continue
        try:
            create_todoist_task(event, project_id, todoist_token)
            print(f"  Created: {event['title']}")
            created += 1
        except Exception as e:
            print(
                f"  Failed to create task '{event['title']}': {e}",
                file=sys.stderr,
            )

    save_state(state, state_path)
    msg = f"\nState saved. {created}/{len(all_events)} tasks created."
    if skipped:
        msg += f" ({skipped} skipped as duplicates)"
    print(msg)


if __name__ == "__main__":
    main()
