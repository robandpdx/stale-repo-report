#!/usr/bin/env python3
"""Generate a CSV report of stale repositories in one or more GitHub organizations.

A repository is considered stale when none of the following happened within the
configured timeframe (default 90 days):

* commits / pushes
* pull requests opened or updated (includes code reviews and review comments)
* issues created or updated (includes issue comments)
* releases published

Repository metadata and activity are fetched in bulk with the GraphQL API (one
request per 50 repositories). Contributor counts are fetched with the REST API
only for the repositories that end up in the report.
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_DAYS = 90
PAGE_SIZE = 50
MAX_RETRIES = 6

FIELDNAMES = [
    "Organization Name",
    "Repository Name",
    "Repository Visibility",
    "Repository Owner",
    "Created Date",
    "Last Commit Date",
    "Last Pull Request Date",
    "Last Activity Date",
    "Days Since Last Activity",
    "Number of Contributors",
    "Archived Status",
]

REPOS_QUERY = """
query($login: String!, $cursor: String, $pageSize: Int!) {
  rateLimit { cost remaining resetAt }
  repositoryOwner(login: $login) {
    login
    repositories(first: $pageSize, after: $cursor, orderBy: {field: NAME, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        visibility
        owner { login }
        createdAt
        pushedAt
        isArchived
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 1) { nodes { committedDate } }
            }
          }
        }
        pullRequests(first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes { updatedAt }
        }
        issues(first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes { updatedAt }
        }
        releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { createdAt }
        }
      }
    }
  }
}
"""


def log(message):
    print(message, file=sys.stderr)


def parse_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_date(value):
    return value.strftime("%Y-%m-%d") if value else ""


class GitHubClient:
    """Minimal GitHub API client with primary and secondary rate limit handling."""

    def __init__(self, token, api_url=DEFAULT_API_URL, max_retries=MAX_RETRIES, sleep=time.sleep):
        self.api_url = api_url.rstrip("/")
        self.graphql_url = self._graphql_url(self.api_url)
        self.max_retries = max_retries
        self.sleep = sleep
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "stale-repo-report",
            }
        )

    @staticmethod
    def _graphql_url(api_url):
        # GitHub Enterprise Server: https://HOST/api/v3 -> https://HOST/api/graphql
        if api_url.endswith("/api/v3"):
            return api_url[: -len("/v3")] + "/graphql"
        return api_url + "/graphql"

    @staticmethod
    def _seconds_until_reset(resp):
        reset = resp.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return max(int(reset) - int(time.time()), 0) + 1
        return 60

    def _rate_limit_wait(self, resp, attempt):
        """Return seconds to wait if resp is rate limited, otherwise None."""
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return int(retry_after)
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return self._seconds_until_reset(resp)
        if resp.status_code == 429 or "rate limit" in resp.text.lower():
            # Secondary rate limit without retry-after: wait at least a minute, backing off.
            return 60 * (2 ** attempt)
        return None

    def request(self, method, url, **kwargs):
        attempt = 0
        while True:
            try:
                resp = self.session.request(method, url, timeout=60, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise
                wait = 2 ** attempt
                log(f"Request error ({exc}); retrying in {wait}s")
                self.sleep(wait)
                attempt += 1
                continue

            if attempt < self.max_retries:
                wait = self._rate_limit_wait(resp, attempt)
                if wait is not None:
                    log(f"Rate limited (HTTP {resp.status_code}); sleeping {wait}s")
                    self.sleep(wait)
                    attempt += 1
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    wait = 2 ** attempt
                    log(f"Server error (HTTP {resp.status_code}); retrying in {wait}s")
                    self.sleep(wait)
                    attempt += 1
                    continue
            return resp

    def graphql(self, query, variables):
        attempt = 0
        while True:
            resp = self.request("POST", self.graphql_url, json={"query": query, "variables": variables})
            resp.raise_for_status()
            payload = resp.json()
            errors = payload.get("errors") or []
            if any(err.get("type") == "RATE_LIMITED" for err in errors) and attempt < self.max_retries:
                wait = self._seconds_until_reset(resp)
                log(f"GraphQL rate limit exceeded; sleeping {wait}s")
                self.sleep(wait)
                attempt += 1
                continue
            if errors and not payload.get("data"):
                raise RuntimeError(f"GraphQL error: {errors}")
            self._throttle(payload.get("data") or {})
            return payload

    def _throttle(self, data):
        """Proactively wait for the reset when the GraphQL budget is nearly spent."""
        rate = data.get("rateLimit")
        if not rate or rate.get("remaining", 1) > rate.get("cost", 1):
            return
        reset_at = parse_datetime(rate.get("resetAt"))
        if reset_at:
            wait = max((reset_at - datetime.now(timezone.utc)).total_seconds(), 0) + 1
            log(f"GraphQL rate limit nearly exhausted; sleeping {int(wait)}s")
            self.sleep(wait)

    def iter_repositories(self, login):
        cursor = None
        while True:
            payload = self.graphql(
                REPOS_QUERY, {"login": login, "cursor": cursor, "pageSize": PAGE_SIZE}
            )
            owner = (payload.get("data") or {}).get("repositoryOwner")
            if owner is None:
                raise RuntimeError(f"Organization or user '{login}' not found or not accessible")
            repos = owner["repositories"]
            for node in repos["nodes"]:
                if node:
                    yield node
            if not repos["pageInfo"]["hasNextPage"]:
                return
            cursor = repos["pageInfo"]["endCursor"]

    def contributor_count(self, owner, repo):
        """Return the number of contributors, or None if GitHub cannot compute it."""
        resp = self.request(
            "GET",
            f"{self.api_url}/repos/{owner}/{repo}/contributors",
            params={"per_page": 1},
        )
        if resp.status_code == 204:
            return 0
        if resp.status_code != 200:
            # e.g. 403 when the contributor list is too large to be computed
            log(f"Could not get contributors for {owner}/{repo} (HTTP {resp.status_code})")
            return None
        last = resp.links.get("last", {}).get("url")
        if last:
            match = re.search(r"[?&]page=(\d+)", last)
            if match:
                return int(match.group(1))
        return len(resp.json())


def analyze_repository(node, now, cutoff):
    """Return a dict describing the repo's activity and whether it is stale."""

    def first_date(connection, field):
        nodes = (connection or {}).get("nodes") or []
        return parse_datetime(nodes[0].get(field)) if nodes and nodes[0] else None

    target = (node.get("defaultBranchRef") or {}).get("target") or {}
    last_commit = first_date(target.get("history"), "committedDate")
    last_pr = first_date(node.get("pullRequests"), "updatedAt")
    last_issue = first_date(node.get("issues"), "updatedAt")
    last_release = first_date(node.get("releases"), "createdAt")
    created = parse_datetime(node.get("createdAt"))
    pushed = parse_datetime(node.get("pushedAt"))

    candidates = [d for d in (last_commit, last_pr, last_issue, last_release, pushed, created) if d]
    last_activity = max(candidates) if candidates else None
    days_since = (now - last_activity).days if last_activity else None

    return {
        "name": node["name"],
        "visibility": (node.get("visibility") or "").lower(),
        "owner": (node.get("owner") or {}).get("login", ""),
        "created": created,
        "last_commit": last_commit,
        "last_pr": last_pr,
        "last_activity": last_activity,
        "days_since": days_since,
        "archived": bool(node.get("isArchived")),
        "stale": last_activity is None or last_activity < cutoff,
    }


def build_row(org, repo, contributors):
    return {
        "Organization Name": org,
        "Repository Name": repo["name"],
        "Repository Visibility": repo["visibility"],
        "Repository Owner": repo["owner"],
        "Created Date": format_date(repo["created"]),
        "Last Commit Date": format_date(repo["last_commit"]),
        "Last Pull Request Date": format_date(repo["last_pr"]),
        "Last Activity Date": format_date(repo["last_activity"]),
        "Days Since Last Activity": "" if repo["days_since"] is None else repo["days_since"],
        "Number of Contributors": "" if contributors is None else contributors,
        "Archived Status": repo["archived"],
    }


def build_summary_row(rows):
    archived = sum(1 for r in rows if r["Archived Status"] is True)
    contributors = sum(
        r["Number of Contributors"] for r in rows if isinstance(r["Number of Contributors"], int)
    )
    summary = {name: "" for name in FIELDNAMES}
    summary.update(
        {
            "Organization Name": "TOTAL",
            "Repository Name": f"{len(rows)} stale repositories",
            "Number of Contributors": contributors,
            "Archived Status": f"True: {archived} / False: {len(rows) - archived}",
        }
    )
    return summary


def generate_report(client, orgs, days=DEFAULT_DAYS, include_archived=False, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = []
    for org in orgs:
        log(f"Scanning {org} ...")
        scanned = 0
        for node in client.iter_repositories(org):
            scanned += 1
            if node.get("isArchived") and not include_archived:
                continue
            repo = analyze_repository(node, now, cutoff)
            if not repo["stale"]:
                continue
            contributors = client.contributor_count(repo["owner"], repo["name"])
            rows.append(build_row(org, repo, contributors))
        log(f"Scanned {scanned} repositories in {org}")
    return rows


def write_csv(rows, output):
    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
        writer.writerow(build_summary_row(rows))


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a CSV report of stale repositories in GitHub organizations."
    )
    parser.add_argument("orgs", nargs="+", help="One or more organization (or user) logins to scan")
    parser.add_argument(
        "-d",
        "--days",
        type=positive_int,
        default=DEFAULT_DAYS,
        help=f"Repositories with no activity in this many days are stale (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived repositories (skipped by default)",
    )
    parser.add_argument(
        "-o", "--output", default="stale_repos.csv", help="Output CSV file (default: stale_repos.csv)"
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL", DEFAULT_API_URL),
        help="GitHub API base URL, e.g. https://HOST/api/v3 for GHES (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub personal access token (default: GITHUB_TOKEN environment variable)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.token:
        log("Error: a GitHub token is required (set GITHUB_TOKEN or pass --token)")
        return 1
    client = GitHubClient(args.token, args.api_url)
    try:
        rows = generate_report(client, args.orgs, args.days, args.include_archived)
    except (RuntimeError, requests.HTTPError) as exc:
        log(f"Error: {exc}")
        return 1
    write_csv(rows, args.output)
    log(f"Wrote {len(rows)} stale repositories to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
