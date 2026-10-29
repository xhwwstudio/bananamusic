"""Publish this directory to a GitHub repository without installing git.

Uses the GitHub Git Data API to create blobs, a tree, one commit and then
fast-forward the target branch.

Usage (PowerShell):
    $env:GITHUB_TOKEN = "github_pat_xxx"       # Contents: Read and write
    python tools/publish_github.py

    # preview only, no network / token required
    python tools/publish_github.py --dry-run

Optional environment variables:
    GITHUB_REPO     default: xhwwstudio/bananamusic
    GITHUB_BRANCH   default: main
    GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL
    COMMIT_MESSAGE  default: "feat: MusicOn 1.9.1 source with Subsonic sync fixes"

The token is only read from the environment and is never written to disk or
printed.  Revoke it on GitHub after publishing if it was created ad hoc.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "work", "data", ".venv", "venv", "node_modules", ".idea", ".vscode"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".db-journal", ".log", ".zip"}


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if rel.name.startswith(".env") and rel.name != ".env.example":
            continue
        yield rel, path


def request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "bananamusic-publisher")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        # Never echo the Authorization header; only the API error payload.
        raise SystemExit(f"GitHub API {method} {url} failed: HTTP {exc.code}\n{detail[:1200]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="list files only")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPO", "xhwwstudio/bananamusic"))
    parser.add_argument("--branch", default=os.environ.get("GITHUB_BRANCH", "main"))
    parser.add_argument("--message", default=os.environ.get("COMMIT_MESSAGE", "feat: MusicOn 1.9.1 source with Subsonic sync fixes"))
    args = parser.parse_args()

    files = list(iter_files(ROOT))
    total = sum(path.stat().st_size for _, path in files)
    print(f"files: {len(files)}, size: {total / 1024 / 1024:.2f} MiB, repo: {args.repo}, branch: {args.branch}")
    if args.dry_run:
        for rel, path in files:
            mode = "100755" if rel.suffix == ".sh" else "100644"
            print(f"  {mode} {rel.as_posix():60s} {path.stat().st_size:>9d}")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set. Create a fine-grained token with Contents: Read and write.", file=sys.stderr)
        return 2

    owner, _, repo = args.repo.partition("/")
    if not owner or not repo:
        raise SystemExit("--repo must look like owner/name")

    repo_info = request("GET", f"{API}/repos/{owner}/{repo}", token)
    default_branch = repo_info.get("default_branch") or args.branch
    if args.branch != default_branch:
        print(f"note: repository default branch is {default_branch!r}, publishing to {args.branch!r}")

    # Existing branch/commit (empty repositories have neither).
    parent_sha = None
    repo_empty = False
    ref_url = f"{API}/repos/{owner}/{repo}/git/ref/heads/{args.branch}"
    refs_url = f"{API}/repos/{owner}/{repo}/git/refs/heads/{args.branch}"
    try:
        ref = request("GET", ref_url, token)
        parent_sha = ref.get("object", {}).get("sha")
    except SystemExit as exc:
        message = str(exc)
        # Empty repositories answer 409 "Git Repository is empty." instead of 404.
        if "HTTP 404" not in message and not ("HTTP 409" in message and "empty" in message.lower()):
            raise
        repo_empty = True
        print("target branch does not exist yet; will create it")

    if repo_empty:
        # GitHub refuses Git Data API writes (blobs/trees) until the repository
        # has at least one commit, so seed it through the Contents API.
        seed = base64.b64encode(b"# initialized by publish_github.py\n").decode("ascii")
        request(
            "PUT",
            f"{API}/repos/{owner}/{repo}/contents/.gitignore",
            token,
            {"message": "chore: initialize repository", "content": seed, "branch": args.branch},
        )
        ref = request("GET", ref_url, token)
        parent_sha = ref.get("object", {}).get("sha")
        print(f"initialized empty repository at {parent_sha[:7]}")

    tree_entries = []
    for index, (rel, path) in enumerate(files, start=1):
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        blob = request("POST", f"{API}/repos/{owner}/{repo}/git/blobs", token, {"content": content, "encoding": "base64"})
        tree_entries.append(
            {
                "path": rel.as_posix(),
                "mode": "100755" if rel.suffix == ".sh" else "100644",
                "type": "blob",
                "sha": blob["sha"],
            }
        )
        print(f"  uploaded blob {index}/{len(files)}: {rel.as_posix()}")

    tree_payload: dict = {"tree": tree_entries}
    if parent_sha:
        base_tree = request("GET", f"{API}/repos/{owner}/{repo}/git/commits/{parent_sha}", token).get("tree", {}).get("sha")
        if base_tree:
            tree_payload["base_tree"] = base_tree
    tree = request("POST", f"{API}/repos/{owner}/{repo}/git/trees", token, tree_payload)

    author_name = os.environ.get("GIT_AUTHOR_NAME", "xhwwstudio")
    author_email = os.environ.get("GIT_AUTHOR_EMAIL", "48114670+xhwwstudio@users.noreply.github.com")
    commit_payload = {
        "message": args.message,
        "tree": tree["sha"],
        "parents": [parent_sha] if parent_sha else [],
        "author": {"name": author_name, "email": author_email},
        "committer": {"name": author_name, "email": author_email},
    }
    commit = request("POST", f"{API}/repos/{owner}/{repo}/git/commits", token, commit_payload)

    if parent_sha:
        request("PATCH", refs_url, token, {"sha": commit["sha"], "force": False})
    else:
        request("POST", f"{API}/repos/{owner}/{repo}/git/refs", token, {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]})

    print(f"published: {repo_info.get('html_url', '')}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
