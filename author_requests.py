from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class AuthorRequest:
    package: str
    version: str
    author_id: int
    issue_number: int
    reason: str


def validate_authors(authors):
    if not isinstance(authors, dict):
        raise ValueError("verified_authors must be a table")
    names = set()
    for login, account in authors.items():
        if not re.fullmatch(r"[A-Za-z0-9-]+", login) or login.lower() in names:
            raise ValueError("Verified GitHub usernames must be unique")
        names.add(login.lower())
        if not isinstance(account, dict) or type(account.get("id")) is not int or account["id"] <= 0:
            raise ValueError("Each verified author needs a numeric GitHub account id")
        teams = account.get("teams")
        if not isinstance(teams, list) or not teams or not all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9_]+", t) for t in teams):
            raise ValueError("Each verified author needs a list of Thunderstore teams")


def parse_author_request(event, repository, authors):
    validate_authors(authors)
    if not isinstance(event, dict) or event.get("action") not in {"opened", "edited", "reopened"}:
        raise ValueError("Unsupported author request event")
    if event.get("repository", {}).get("full_name", "").lower() != repository.lower():
        raise ValueError("Author request belongs to another repository")
    issue = event.get("issue", {})
    user = issue.get("user", {})
    sender = event.get("sender", {})
    if issue.get("state") != "open" or "pull_request" in issue or not issue.get("title", "").startswith("[Emergency]"):
        raise ValueError("Use an open emergency release issue")
    if type(issue.get("number")) is not int or issue["number"] <= 0:
        raise ValueError("Invalid issue number")
    login = user.get("login", "").lower()
    accounts = {name.lower(): account for name, account in authors.items()}
    account = accounts.get(login)
    if (not account or type(user.get("id")) is not int or account["id"] != user["id"] or
            sender.get("id") != user["id"] or sender.get("login", "").lower() != login):
        raise ValueError("Request must be submitted by its verified author")
    body = issue.get("body") or ""
    match = re.fullmatch(
        r"\s*### Mod\s*\n+([A-Za-z0-9_]+-[A-Za-z0-9_]+)\s*\n+"
        r"### Version\s*\n+(\d+\.\d+\.\d+)\s*\n+### Reason\s*\n+(.+?)\s*",
        body, flags=re.DOTALL)
    if not match:
        raise ValueError("Request must include Mod, Version, and Reason from the issue form")
    package, version, reason = match.groups()
    if not reason.strip() or reason.strip() == "_No response_" or len(reason) > 1000:
        raise ValueError("An emergency reason is required (maximum 1000 characters)")
    if package.split("-", 1)[0] not in account["teams"]:
        raise ValueError("This author is not verified for that Thunderstore team")
    return AuthorRequest(package, version, user["id"], issue["number"], reason.strip())


def load_author_request(config):
    if os.environ.get("GITHUB_EVENT_NAME") != "issues":
        raise ValueError("Author requests must come from a GitHub issue event")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not event_path or not repository:
        raise ValueError("Missing GitHub issue context")
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    return parse_author_request(event, repository, config.get("verified_authors", {}))
