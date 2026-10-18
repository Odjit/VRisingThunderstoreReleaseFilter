import base64
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class StateSyncError(Exception):
    pass


class GitHubState:
    branch = "bot-state"
    filename = "release-state.json"

    def __init__(self, initialize=False):
        self.repository = os.environ.get("GITHUB_REPOSITORY", "")
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.commit = os.environ.get("GITHUB_SHA", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) or not self.token:
            raise StateSyncError("GitHub state requires GITHUB_REPOSITORY and GITHUB_TOKEN")
        self.initialize = initialize
        self.sha = None
        self.previous = None

    def api(self, method, path, data=None, missing_ok=False):
        request = Request(
            f"https://api.github.com/repos/{self.repository}/{path}", method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                     "User-Agent": "V-Rising-Release-Filter/1.0"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            raise StateSyncError(f"GitHub state request failed (HTTP {exc.code}); stopping to protect cooldowns") from None
        except (URLError, OSError, ValueError):
            raise StateSyncError("GitHub state request failed; stopping to protect cooldowns") from None

    def restore(self, store):
        ref = self.api("GET", f"git/ref/heads/{self.branch}", missing_ok=True)
        if ref is None:
            if not self.initialize or not re.fullmatch(r"[0-9a-f]{40}", self.commit):
                raise StateSyncError("State branch missing. Use Run workflow with Initialize state checked for first setup")
            self.api("POST", "git/refs", {"ref": f"refs/heads/{self.branch}", "sha": self.commit})
        file = self.api("GET", f"contents/{self.filename}?ref={self.branch}", missing_ok=True)
        if file is None:
            if not self.initialize:
                raise StateSyncError("Saved state missing; stopping instead of resetting cooldowns")
            return
        try:
            if file["encoding"] != "base64":
                raise ValueError("unsupported encoding")
            raw = base64.b64decode(file["content"])
            data = json.loads(raw)
            store.restore_snapshot(data)
            self.sha = file["sha"]
            self.previous = self.encode(store)
        except (ValueError, KeyError, TypeError):
            raise StateSyncError("Saved GitHub state is invalid; restore a known good state before running") from None

    def require_admin(self):
        if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise StateSyncError("This action requires a manual workflow run")
        actors = {os.environ.get("GITHUB_ACTOR", ""), os.environ.get("GITHUB_TRIGGERING_ACTOR", "")}
        for actor in sorted(actors):
            if not re.fullmatch(r"[A-Za-z0-9-]+", actor):
                raise StateSyncError("Cannot verify the requesting administrator")
            permissions = self.api("GET", f"collaborators/{actor}/permission")
            if not isinstance(permissions, dict) or permissions.get("permission") != "admin":
                raise StateSyncError("This action requires repository administrator access")

    @staticmethod
    def encode(store):
        return json.dumps(store.snapshot(), sort_keys=True, indent=2).encode() + b"\n"

    def save(self, store):
        raw = self.encode(store)
        if raw == self.previous:
            return
        if len(raw) > 900_000:
            raise StateSyncError("Saved state exceeds the supported size; stopping before posting")
        data = {"message": "Save release filter state", "branch": self.branch,
                "content": base64.b64encode(raw).decode()}
        if self.sha:
            data["sha"] = self.sha
        result = self.api("PUT", f"contents/{self.filename}", data)
        try:
            self.sha = result["content"]["sha"]
        except (KeyError, TypeError):
            raise StateSyncError("GitHub did not confirm saved state; stopping before further posts") from None
        self.previous = raw
