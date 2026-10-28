"""Quiet Git operations for isolated Actions data checkouts.

Git output is always captured. Credentials are supplied only through process
environment, never URLs, command arguments, repository files or diagnostics.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

LEGACY_URL = "https://github.com/topomat12138/daily-arXiv-ai-enhanced.git"


class WorkflowError(RuntimeError):
    pass


def git_environment(token: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("GIT_TRACE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) or key == "GIT_CURL_VERBOSE":
            del env[key]
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_COUNT="0", GIT_AUTHOR_NAME="github-actions[bot]",
               GIT_COMMITTER_NAME="github-actions[bot]",
               GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
               GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
    if token:
        server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{server}/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {credential}")
    return env


def run_git(directory: Path, *args: str, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["git", "-C", str(directory), *args], env=env,
                                capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkflowError("Git operation failed") from None
    if check and result.returncode:
        raise WorkflowError("Git operation failed")
    return result


def safe_repository(url: str) -> str:
    if not url or url.startswith("-") or any(char in url for char in "\r\n\x00"):
        raise WorkflowError("invalid repository")
    if "://" in url:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise WorkflowError("repository must use credential-free HTTPS")
    elif not Path(url).is_dir():
        raise WorkflowError("local repository does not exist")
    return url


def empty_external_directory(directory: Path) -> Path:
    directory = directory.resolve()
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
    if directory.is_relative_to(workspace):
        raise WorkflowError("data checkout must be outside the code workspace")
    if directory.exists() and any(directory.iterdir()):
        raise WorkflowError("checkout directory must be empty")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def checkout_data(repository: str, directory: str | Path, *, token: str | None = None) -> None:
    repository = safe_repository(repository)
    root = empty_external_directory(Path(directory))
    env = git_environment(token)
    found = run_git(root, "ls-remote", "--exit-code", "--heads", repository, "refs/heads/data", env=env, check=False)
    if found.returncode == 2:
        # An unborn branch in a new repository has no parents or main history.
        run_git(root, "init", "--quiet", "--initial-branch=data", env=env)
        run_git(root, "remote", "add", "origin", repository, env=env)
    elif found.returncode == 0:
        run_git(root, "clone", "--quiet", "--single-branch", "--branch", "data", repository, ".", env=env)
    else:
        raise WorkflowError("cannot inspect data branch")


def checkout_legacy(directory: str | Path) -> None:
    root = empty_external_directory(Path(directory))
    env = git_environment()
    run_git(root, "clone", "--quiet", "--depth", "1", "--single-branch", "--branch", "data",
            "--filter=blob:none", "--sparse", LEGACY_URL, ".", env=env)
    run_git(root, "sparse-checkout", "set", "--no-cone",
            "/data/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].jsonl", env=env)


def commit_message(report_path: str | Path, kind: str) -> str:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if kind == "backfill":
        first = date.fromisoformat(report["first_file_date"])
        last = date.fromisoformat(report["last_file_date"])
        return f"data: backfill {first.isoformat()}..{last.isoformat()}"
    reports = report.get("announcements", [report])
    dates = sorted({date.fromisoformat(item["date"]).isoformat() for item in reports})
    if not dates:
        raise WorkflowError("announcement dates missing")
    return "data: " + ", ".join(dates)


def publish_data(directory: str | Path, *, report_path: str | Path, kind: str,
                 token: str | None = None) -> bool:
    root = Path(directory).resolve()
    env = git_environment(token)
    if run_git(root, "rev-parse", "--show-toplevel", env=env).stdout.strip() != str(root):
        raise WorkflowError("not an isolated checkout")
    if run_git(root, "symbolic-ref", "--short", "HEAD", env=env).stdout.strip() != "data":
        raise WorkflowError("refusing to publish a code branch")
    safe_repository(run_git(root, "remote", "get-url", "origin", env=env).stdout.strip())
    for area in ("papers", "daily"):
        if (root / area).exists() or run_git(root, "ls-files", "--", area, env=env).stdout:
            run_git(root, "add", "--all", "--", area, env=env)
    changed = run_git(root, "diff", "--cached", "--quiet", env=env, check=False)
    if changed.returncode == 0:
        return False
    if changed.returncode != 1:
        raise WorkflowError("cannot inspect data changes")
    staged = run_git(root, "diff", "--cached", "--name-only", "-z", env=env).stdout.split("\0")
    if any(path and path.split("/", 1)[0] not in {"papers", "daily"} for path in staged):
        raise WorkflowError("refusing non-data changes")
    run_git(root, "commit", "--quiet", "-m", commit_message(report_path, kind), env=env)
    for attempt in range(3):
        pushed = run_git(root, "push", "origin", "HEAD:refs/heads/data", env=env, check=False)
        if pushed.returncode == 0:
            return True
        if attempt == 2:
            raise WorkflowError("data push failed after three attempts")
        pulled = run_git(root, "pull", "--rebase", "origin", "data", env=env, check=False)
        if pulled.returncode:
            run_git(root, "rebase", "--abort", env=env, check=False)
            raise WorkflowError("data rebase failed")
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    checkout = sub.add_parser("checkout")
    checkout.add_argument("--directory", type=Path, required=True)
    checkout.add_argument("--repository-url")
    legacy = sub.add_parser("legacy")
    legacy.add_argument("--directory", type=Path, required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--directory", type=Path, required=True)
    publish.add_argument("--report", type=Path, required=True)
    publish.add_argument("--kind", choices=["ingest", "backfill"], required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("FEED_GITHUB_TOKEN")
    try:
        if args.command == "checkout":
            server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            repository = args.repository_url or f"{server}/{os.environ['GITHUB_REPOSITORY']}.git"
            checkout_data(repository, args.directory, token=token)
        elif args.command == "legacy":
            checkout_legacy(args.directory)
        else:
            publish_data(args.directory, report_path=args.report, kind=args.kind, token=token)
    except (WorkflowError, OSError, ValueError, KeyError):
        return 1  # Never print Git/HTTP errors, credentials or identity metadata.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
