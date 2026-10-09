"""GitHub-backed storage used by the dashboard when the bot runs on GitHub Actions.

The bot's log and settings.json live on the `bot-data` branch, and runs come from the
`trading-bot.yml` workflow. Configure with environment variables (or root-level
Streamlit secrets, which Streamlit exposes as environment variables):
    GITHUB_REPO  = "owner/repo"
    GITHUB_TOKEN = fine-grained token with Contents: read/write and Actions: read/write
"""

import base64
import os
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"
WORKFLOW = "trading-bot.yml"
DATA_BRANCH = "bot-data"
SCHEDULE_UTC = (13, 15)  # keep in sync with the cron in .github/workflows/trading-bot.yml


class GitHubStore:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    @classmethod
    def from_env(cls) -> "GitHubStore | None":
        repo, token = os.getenv("GITHUB_REPO"), os.getenv("GITHUB_TOKEN")
        return cls(repo, token) if repo and token else None

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        return self.session.request(method, f"{API}/repos/{self.repo}{path}", timeout=20, **kwargs)

    def default_branch(self) -> str:
        r = self._request("GET", "")
        r.raise_for_status()
        return r.json()["default_branch"]

    def read_text(self, path: str) -> str | None:
        r = self._request("GET", f"/contents/{path}", params={"ref": DATA_BRANCH},
                          headers={"Accept": "application/vnd.github.raw+json"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content.decode("utf-8")

    def _ensure_data_branch(self) -> None:
        if self._request("GET", f"/branches/{DATA_BRANCH}").status_code != 404:
            return
        r = self._request("GET", f"/git/ref/heads/{self.default_branch()}")
        r.raise_for_status()
        self._request("POST", "/git/refs", json={
            "ref": f"refs/heads/{DATA_BRANCH}", "sha": r.json()["object"]["sha"],
        }).raise_for_status()

    def write_text(self, path: str, text: str, message: str) -> None:
        self._ensure_data_branch()
        current = self._request("GET", f"/contents/{path}", params={"ref": DATA_BRANCH})
        body = {"message": message, "branch": DATA_BRANCH,
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        if current.status_code == 200:
            body["sha"] = current.json()["sha"]
        self._request("PUT", f"/contents/{path}", json=body).raise_for_status()

    def workflow_runs(self, limit: int = 10) -> list[dict]:
        r = self._request("GET", f"/actions/workflows/{WORKFLOW}/runs", params={"per_page": limit})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()["workflow_runs"]

    def dispatch(self, dry_run: bool) -> None:
        self._request("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={
            "ref": self.default_branch(), "inputs": {"dry_run": "true" if dry_run else "false"},
        }).raise_for_status()


def next_scheduled_run(now: datetime | None = None) -> datetime:
    """Next daily run at SCHEDULE_UTC, in UTC. GitHub may start scheduled runs a few minutes late."""
    now = now or datetime.now(timezone.utc)
    run = now.replace(hour=SCHEDULE_UTC[0], minute=SCHEDULE_UTC[1], second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)
