from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from github_state import GitHubState, StateSyncError
from author_requests import load_author_request, validate_authors

LOG = logging.getLogger("release-filter")
SOURCE_EXTENSIONS = {".cs", ".fs", ".vb", ".c", ".h", ".cpp", ".hpp", ".rs",
                     ".go", ".py", ".js", ".ts", ".tsx", ".jsx", ".lua"}


class RemoteError(Exception):
    pass


class RejectedPost(RemoteError):

    def __init__(self, message, retry_after=300):
        super().__init__(message)
        self.retry_after = retry_after


def get_json(url: str):
    request = Request(url, headers={"User-Agent": "V-Rising-Release-Filter/1.0",
                                   "Accept": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RemoteError(f"Public API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RemoteError("Public API unavailable or returned invalid JSON") from None


def post_discord(url: str, release: Release, source: str):
    embed = {
        "author": {"name": release.owner[:256]},
        "title": f"{release.name} v{release.version}"[:256],
        "url": release.url,
        "description": release.description[:2000],
        "color": 0x5546FF,
        "fields": [
            {"name": "Total downloads", "value": str(release.downloads)},
            {"name": "Categories", "value": release.categories[:1024] or "None"},
            {"name": "Source", "value": source[:1024]},
        ],
        "timestamp": datetime.fromtimestamp(release.created, timezone.utc).isoformat(),
    }
    if release.icon and https_url(release.icon):
        embed["thumbnail"] = {"url": release.icon}
    payload = {
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }
    send_payload(url, payload)


def send_payload(url: str, payload: dict):
    request = Request(url + "?wait=true", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json",
                               "User-Agent": "V-Rising-Release-Filter/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            retry_after = 300
            if exc.code == 429:
                try:
                    retry_after = max(300, float(json.load(exc)["retry_after"]))
                except (KeyError, TypeError, ValueError):
                    pass
            raise RejectedPost(f"Discord rejected announcement (HTTP {exc.code}); retry after backoff", retry_after) from None
        raise RemoteError("Discord response uncertain; holding this mod for the cooldown") from None
    except (URLError, TimeoutError, OSError):
        raise RemoteError("Discord delivery uncertain; holding this mod for the cooldown") from None


def https_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and bool(parsed.hostname) and
            not parsed.username and not parsed.password and len(value) <= 1000)


def github_repo(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts[:2]):
        return None
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    if owner in {".", ".."} or repo in {"", ".", ".."}:
        return None
    return f"{owner}/{repo}"


@dataclass(frozen=True)
class Release:
    key: str
    name: str
    owner: str
    version: str
    created: float
    website: str
    description: str
    url: str
    icon: str = ""
    downloads: int = 0
    categories: str = ""


def releases_from_feed(feed, community: str) -> dict[str, Release]:
    if not isinstance(feed, list) or not feed:
        raise RemoteError("Thunderstore feed is empty or invalid; state left unchanged")
    releases = {}
    for package in feed:
        if package.get("is_deprecated"):
            continue
        versions = [v for v in package["versions"] if v.get("is_active", True)]
        if not versions:
            continue
        latest = max(versions, key=lambda v: datetime.fromisoformat(v["date_created"]).timestamp())
        owner, name = package["owner"], package["name"]
        if not all(re.fullmatch(r"[A-Za-z0-9_]+", value) for value in (owner, name)):
            raise RemoteError("Thunderstore returned an invalid package name")
        key = f"{owner}-{name}"
        releases[key] = Release(key, name, owner, latest["version_number"],
                                datetime.fromisoformat(latest["date_created"]).timestamp(),
                                latest.get("website_url") or "", latest.get("description") or "",
                                f"https://thunderstore.io/c/{community}/p/{owner}/{name}/",
                                icon=latest.get("icon") or "",
                                downloads=sum(v.get("downloads", 0) for v in package["versions"]),
                                categories=", ".join(package.get("categories") or []))
    return releases


class SourceChecker:
    def __init__(self, config, fetch=get_json):
        self.config, self.fetch = config, fetch
        self.cache = {}

    def check(self, release: Release, now: float) -> tuple[bool, str]:
        if (release.key in self.config.get("blocked_packages", []) or
                release.owner in self.config.get("blocked_authors", [])):
            return False, "blocked by configuration"
        reviewed = self.config.get("reviewed_sources", {}).get(release.key)
        if reviewed:
            return True, reviewed
        url = self.config.get("source_overrides", {}).get(release.key, release.website)
        repo = github_repo(url)
        if not repo:
            return False, "no supported source repository; needs manual review"
        if repo in self.cache and self.cache[repo][0] > now:
            return self.cache[repo][1]
        metadata = self.fetch(f"https://api.github.com/repos/{repo}")
        result = (False, "repository is missing, private, or has no source files")
        if metadata and metadata.get("private") is False and not metadata.get("disabled"):
            tree = self.fetch(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1")
            if tree and any(entry.get("type") == "blob" and
                            Path(entry.get("path", "")).suffix.lower() in SOURCE_EXTENSIONS
                            for entry in tree.get("tree", [])):
                result = (True, f"https://github.com/{repo}")
        self.cache[repo] = (now + 3600, result)
        return result


class Store:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.checkpoint = lambda: None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS packages (
                key TEXT PRIMARY KEY, newest REAL NOT NULL,
                version TEXT NOT NULL, pending TEXT, last_sent REAL
            );
        """)

    def snapshot(self):
        return {"format": 1,
                "metadata": {row["key"]: row["value"] for row in self.db.execute("SELECT * FROM metadata")},
                "packages": [dict(row) for row in self.db.execute("SELECT * FROM packages ORDER BY key")]}

    def restore_snapshot(self, data):
        if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("metadata"), dict) or not isinstance(data.get("packages"), list):
            raise ValueError("Unsupported state format")
        if data["metadata"].get("initialized") != "1" or not data["metadata"].get("community"):
            raise ValueError("Incomplete state")
        fields = {"key", "newest", "version", "pending", "last_sent"}
        for row in data["packages"]:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError("Invalid package state")
            if not isinstance(row["newest"], (int, float)) or (row["last_sent"] is not None and not isinstance(row["last_sent"], (int, float))):
                raise ValueError("Invalid package timestamps")
            if row["pending"] is not None:
                Release(**json.loads(row["pending"]))
        with self.db:
            self.db.execute("DELETE FROM packages")
            self.db.execute("DELETE FROM metadata")
            self.db.executemany("INSERT INTO metadata VALUES (?, ?)", data["metadata"].items())
            self.db.executemany(
                "INSERT INTO packages (key, newest, version, pending, last_sent) VALUES (:key, :newest, :version, :pending, :last_sent)",
                data["packages"])

    def ingest(self, releases: dict[str, Release]) -> bool:
        initialized = self.db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone()
        with self.db:
            for release in releases.values():
                row = self.db.execute("SELECT * FROM packages WHERE key=?", (release.key,)).fetchone()
                data = json.dumps(asdict(release))
                if row is None:
                    self.db.execute("INSERT INTO packages VALUES (?, ?, ?, ?, NULL)",
                                    (release.key, release.created, release.version, data if initialized else None))
                elif release.created > row["newest"]:
                    self.db.execute("UPDATE packages SET newest=?, version=?, pending=? WHERE key=?",
                                    (release.created, release.version, data, release.key))
                elif row["pending"] and release.created == row["newest"] and release.version == row["version"]:
                    self.db.execute("UPDATE packages SET pending=? WHERE key=?", (data, release.key))
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('initialized', '1')")
        self.checkpoint()
        return not initialized

    def deliver(self, releases, now, cooldown, checker, send, clock=None, emergency=None, author_request=None):
        if author_request and emergency != (author_request.package, author_request.version):
            raise ValueError("Author request does not match the emergency release")
        backoff = self.db.execute("SELECT value FROM metadata WHERE key='retry_after'").fetchone()
        if backoff and now < float(backoff["value"]):
            if emergency:
                raise ValueError("Discord backoff is active; try again later")
            return
        marker = None
        author_limit = None
        previous_author_time = None
        request_marker = None
        if emergency:
            key, version = emergency
            current = releases.get(key)
            if not current or current.version != version:
                raise ValueError("Emergency version must match the latest active release")
            row = self.db.execute("SELECT * FROM packages WHERE key=?", (key,)).fetchone()
            if not row or not row["pending"]:
                raise ValueError("This release is not pending; it may already have been announced")
            if row["version"] != version or row["newest"] != current.created:
                raise ValueError("Emergency release differs from saved state")
            marker = f"emergency:{key}:{version}"
            if self.db.execute("SELECT 1 FROM metadata WHERE key=?", (marker,)).fetchone():
                raise ValueError("Emergency announcement already attempted for this release")
            if author_request:
                author_limit = f"author-emergency:{key}"
                previous = self.db.execute("SELECT value FROM metadata WHERE key=?", (author_limit,)).fetchone()
                previous_author_time = previous["value"] if previous else None
                if previous_author_time is not None and now - float(previous_author_time) < 7 * 86400:
                    raise ValueError("Authors may request one emergency announcement per mod every seven days")
                request_marker = f"author-request:{author_request.issue_number}"
                if self.db.execute("SELECT 1 FROM metadata WHERE key=?", (request_marker,)).fetchone():
                    raise ValueError("This issue has already been used for an emergency announcement")
            rows = [row]
        else:
            rows = self.db.execute("SELECT * FROM packages WHERE pending IS NOT NULL ORDER BY newest").fetchall()
        for row in rows:
            now = clock() if clock else now
            current = releases.get(row["key"])
            if not current or current.created != row["newest"] or current.version != row["version"]:
                continue
            if not emergency and row["last_sent"] is not None and now - row["last_sent"] < cooldown:
                continue
            release = Release(**json.loads(row["pending"]))
            try:
                approved, reason = checker.check(release, now)
            except RemoteError as exc:
                if emergency:
                    raise
                LOG.warning("Source check deferred for %s: %s", release.key, exc)
                break
            if not approved:
                if emergency:
                    raise ValueError(f"Emergency announcement denied: {reason}")
                LOG.info("Held %s %s: %s", release.key, release.version, reason)
                continue
            now = clock() if clock else now
            with self.db:
                claimed = self.db.execute(
                    "UPDATE packages SET last_sent=? WHERE key=? AND last_sent IS ? AND pending=?",
                    (now, release.key, row["last_sent"], row["pending"])).rowcount
                if claimed and marker:
                    self.db.execute("INSERT INTO metadata VALUES (?, ?)", (marker, str(now)))
                if claimed and author_request:
                    self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (author_limit, str(now)))
                    self.db.execute("INSERT INTO metadata VALUES (?, ?)",
                                    (request_marker, json.dumps(asdict(author_request))))
            if not claimed:
                if emergency:
                    raise ValueError("Release state changed; try again")
                continue
            self.checkpoint()
            try:
                send(release, reason)
            except RejectedPost as exc:
                with self.db:
                    self.db.execute("UPDATE packages SET last_sent=? WHERE key=? AND last_sent=?",
                                    (row["last_sent"], release.key, now))
                    self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('retry_after', ?)",
                                    (str(now + exc.retry_after),))
                    if marker:
                        self.db.execute("DELETE FROM metadata WHERE key=?", (marker,))
                    if author_request:
                        self.db.execute("DELETE FROM metadata WHERE key=?", (request_marker,))
                        if previous_author_time is None:
                            self.db.execute("DELETE FROM metadata WHERE key=?", (author_limit,))
                        else:
                            self.db.execute("UPDATE metadata SET value=? WHERE key=?", (previous_author_time, author_limit))
                self.checkpoint()
                if emergency:
                    raise
                LOG.warning("%s: %s", release.key, exc)
                break
            except RemoteError as exc:
                if emergency:
                    raise
                LOG.warning("%s: %s", release.key, exc)
                break
            else:
                with self.db:
                    self.db.execute("UPDATE packages SET pending=NULL WHERE key=? AND pending=?",
                                    (release.key, row["pending"]))
                self.checkpoint()
                LOG.info("Announced %s %s", release.key, release.version)


def load_config():
    with Path(__file__).with_name("config.toml").open("rb") as file:
        config = tomllib.load(file)
    config.setdefault("community", "v-rising")
    config.setdefault("cooldown_hours", 24)
    if not re.fullmatch(r"[a-z0-9-]+", config["community"]):
        raise ValueError("Invalid community")
    if not 24 <= config["cooldown_hours"] <= 8760:
        raise ValueError("cooldown_hours must be 24-8760")
    for field in ("source_overrides", "reviewed_sources"):
        for url in config.get(field, {}).values():
            if not https_url(url):
                raise ValueError(f"{field} must contain HTTPS URLs without credentials")
    validate_authors(config.get("verified_authors", {}))
    return config


def main():
    parser = argparse.ArgumentParser(description="V Rising release filter")
    parser.add_argument("--test-webhook", action="store_true", help="Send a test message without changing release history")
    parser.add_argument("--initialize-state", action="store_true", help="Allow initial creation of missing GitHub state")
    parser.add_argument("--emergency-package", metavar="Team-ModName", help="Announce one pending release without waiting for its cooldown")
    parser.add_argument("--emergency-version", metavar="1.2.3", help="Exact latest version to announce")
    parser.add_argument("--author-request", action="store_true", help="Process a verified author's emergency issue")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = None
    try:
        config = load_config()
        if args.test_webhook and (args.initialize_state or args.author_request or
                                  args.emergency_package is not None or args.emergency_version is not None):
            raise ValueError("Webhook tests cannot be combined with other actions")
        emergency = None
        author_request = None
        if args.author_request:
            if args.initialize_state or args.emergency_package is not None or args.emergency_version is not None:
                raise ValueError("Author requests cannot be combined with other actions")
            author_request = load_author_request(config)
            emergency = (author_request.package, author_request.version)
        if args.emergency_package is not None or args.emergency_version is not None:
            if not args.emergency_package or not args.emergency_version:
                raise ValueError("Emergency announcements require both a package and a version")
            if not re.fullmatch(r"[A-Za-z0-9_]+-[A-Za-z0-9_]+", args.emergency_package) or not re.fullmatch(r"\d+\.\d+\.\d+", args.emergency_version):
                raise ValueError("Use an exact Team-ModName and version such as 1.2.3")
            if args.initialize_state:
                raise ValueError("Emergency announcements cannot be combined with initialization")
            emergency = (args.emergency_package, args.emergency_version)
        checker = SourceChecker(config)
        feed_url = f"https://thunderstore.io/c/{config['community']}/api/v1/package/"
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not re.fullmatch(r"https://(?:canary\.|ptb\.)?discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]+", webhook):
            raise ValueError("Set DISCORD_WEBHOOK_URL to a Discord webhook URL (without query parameters)")
        sync = GitHubState(initialize=args.initialize_state)
        if args.test_webhook:
            sync.require_admin()
            send_payload(webhook, {"allowed_mentions": {"parse": []},
                                   "content": "Homework for Thiaz"})
            LOG.info("Webhook test sent")
            return 0
        store = Store()
        if emergency and not author_request:
            sync.require_admin()
        sync.restore(store)
        store.checkpoint = lambda: sync.save(store)
        saved = store.db.execute("SELECT value FROM metadata WHERE key='community'").fetchone()
        if saved and saved["value"] != config["community"]:
            raise ValueError("Community differs from saved release history")
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO metadata VALUES ('community', ?)", (config["community"],))
        releases = releases_from_feed(get_json(feed_url), config["community"])
        if emergency:
            current = releases.get(emergency[0])
            if not current or current.version != emergency[1]:
                raise ValueError("Emergency version must match the latest active release")
            if not store.db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
                raise ValueError("Initialize release history before using an emergency announcement")
        if store.ingest(releases):
            LOG.info("Initialized %d packages; historical releases will not be announced", len(releases))
        store.deliver(releases, time.time(), config["cooldown_hours"] * 3600, checker,
                      lambda release, source: post_discord(webhook, release, source), clock=time.time,
                      emergency=emergency, author_request=author_request)
        return 0
    except (ValueError, OSError, RemoteError, StateSyncError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.info("Stopped")
        return 0
    except (KeyError, TypeError):
        LOG.error("Invalid configuration, release data, or saved state")
        return 1
    finally:
        if store is not None:
            store.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
