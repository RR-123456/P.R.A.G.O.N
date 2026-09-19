"""
agentic/github_monitor.py
──────────────────────────
Jarvis v2.0 — GitHub Monitoring (PRD Pillar 1)

Proactively monitors:
  - Failed CI/CD builds
  - PR reviews awaiting your response
  - Merge conflicts
  - Project milestone status

Setup (either works — githubapi.txt takes priority if both are set):
  Option A: open api/githubapi.txt and paste your token in, replacing the placeholder line
  Option B: set environment variable GITHUB_TOKEN=your_personal_access_token
  Get token: github.com Settings Developer Settings Personal Access Tokens
"""

from __future__ import annotations
import os
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone


GITHUB_API = "https://api.github.com"


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # features/agentic_feature/github_monitor.py -> project root is two levels up
    return Path(__file__).resolve().parent.parent.parent


def _load_github_token() -> str:
    """Look in api/githubapi.txt first, then fall back to the GITHUB_TOKEN env var."""
    token_file = _get_base_dir() / "api" / "githubapi.txt"
    if token_file.exists():
        try:
            raw = token_file.read_text(encoding="utf-8").strip()
            # Ignore comment lines (#...) and the unfilled placeholder
            lines = [
                ln.strip() for ln in raw.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
            if lines:
                token = lines[0]
                if token and token != "PASTE_YOUR_GITHUB_TOKEN_HERE":
                    return token
        except Exception as e:
            print(f"[ GitHub ] Failed to read api/githubapi.txt: {e}")
    return os.environ.get("GITHUB_TOKEN", "").strip()


GITHUB_TOKEN = _load_github_token()


class GitHubMonitor:
    def __init__(self):
        self._token = _load_github_token()
        self._enabled = bool(self._token)
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept" : "application/vnd.github.v3+json",
            "User-Agent" : "Jarvis-v2"
        }
        if self._enabled:
            print("[ GitHub ] GitHub monitoring active")
        else:
            print("[ GitHub ] No GitHub token found — GitHub monitoring disabled")
            print(" Paste your token into api/githubapi.txt, then restart Pragon.")

    def _get(self, endpoint: str) -> dict | list | None:
        if not self._enabled:
            return None
        try:
            req = urllib.request.Request(
                f"{GITHUB_API}{endpoint}",
                headers=self._headers
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            hint = ""
            if e.code == 403 and "/notifications" in endpoint:
                hint = (
                    " -- if you're using a fine-grained personal access token, "
                    "it likely doesn't have permission for the notifications "
                    "endpoint; generate a classic token with the 'notifications' "
                    "scope instead (github.com Settings > Developer settings > "
                    "Personal access tokens > Tokens (classic))"
                )
            print(f"[ GitHub ] HTTP {e.code}: {endpoint}{hint}")
            return None
        except Exception as e:
            print(f"[ GitHub ] Error: {e}")
            return None

    def get_notifications(self) -> list[dict]:
        """Get unread GitHub notifications."""
        data = self._get("/notifications?all=false&participating=true")
        if not data:
            return []

        alerts = []
        for notif in data[:10]:
            reason = notif.get("reason", "")
            subject = notif.get("subject", {})
            repo = notif.get("repository", {}).get("full_name", "")

            alerts.append({
                "type" : subject.get("type", ""),
                "title" : subject.get("title", ""),
                "repo" : repo,
                "reason": reason,
            })
        return alerts

    def get_pr_status(self, owner: str, repo: str) -> list[dict]:
        """Get open PRs for a repository."""
        data = self._get(f"/repos/{owner}/{repo}/pulls?state=open")
        if not data:
            return []

        return [
            {
                "number" : pr.get("number"),
                "title" : pr.get("title"),
                "author" : pr.get("user", {}).get("login"),
                "mergeable" : pr.get("mergeable"),
                "created_at": pr.get("created_at"),
            }
            for pr in data[:5]
        ]

    def get_repo_status(self, owner: str, repo: str) -> dict:
        """Get latest commit + CI status for a repo."""
        commits = self._get(f"/repos/{owner}/{repo}/commits?per_page=1")
        if not commits or not isinstance(commits, list):
            return {"summary": "Could not fetch repo status"}

        latest = commits[0]
        sha = latest.get("sha", "")[:7]
        message = latest.get("commit", {}).get("message", "")[:60]
        author = latest.get("commit", {}).get("author", {}).get("name", "")
        commit_time = latest.get("commit", {}).get("author", {}).get("date", "")

        # Check CI status
        ci_data = self._get(f"/repos/{owner}/{repo}/commits/{latest.get('sha','')}/check-runs")
        ci_status = "unknown"
        if ci_data and isinstance(ci_data, dict):
            runs = ci_data.get("check_runs", [])
            if runs:
                conclusions = [r.get("conclusion") for r in runs if r.get("conclusion")]
                if "failure" in conclusions:
                    ci_status = "FAILING"
                elif all(c == "success" for c in conclusions):
                    ci_status = "PASSING"
                else:
                    ci_status = "IN PROGRESS"

        return {
            "sha" : sha,
            "message" : message,
            "author" : author,
            "ci_status" : ci_status,
            "summary" : f"Latest commit by {author}: {message}. CI: {ci_status}."
        }

    def get_urgent_alerts(self) -> list[str]:
        """
        Pillar 1: Get all urgent GitHub alerts for proactive notification.
        Returns list of alert strings to speak.
        """
        if not self._enabled:
            return []

        alerts = []
        notifs = self.get_notifications()

        for n in notifs:
            if n["reason"] in ("review_requested", "mention"):
                alerts.append(
                    f"GitHub: You have a {n['reason'].replace('_',' ')} "
                    f"on {n['title'][:40]} in {n['repo']}."
                )

        return alerts[:3] # max 3 alerts at once

    @property
    def is_connected(self) -> bool:
        return self._enabled