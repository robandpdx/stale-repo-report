import csv
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import stale_repo_report as srr

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def make_node(name, last="2024-05-20T00:00:00Z", archived=False, created="2020-01-01T00:00:00Z"):
    return {
        "name": name,
        "visibility": "PRIVATE",
        "owner": {"login": "acme"},
        "createdAt": created,
        "pushedAt": last,
        "isArchived": archived,
        "defaultBranchRef": {"target": {"history": {"nodes": [{"committedDate": last}]}}},
        "pullRequests": {"nodes": [{"updatedAt": last}]},
        "issues": {"nodes": []},
        "releases": {"nodes": []},
    }


def make_response(status=200, json_data=None, headers=None, text="", links=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text
    resp.links = links or {}
    resp.json.return_value = json_data
    return resp


class FakeClient:
    def __init__(self, nodes, contributors=3):
        self.nodes = nodes
        self.contributors = contributors
        self.contributor_calls = []

    def iter_repositories(self, login):
        return iter(self.nodes)

    def contributor_count(self, owner, repo):
        self.contributor_calls.append(repo)
        return self.contributors


class AnalyzeTests(unittest.TestCase):
    def test_active_repo_not_stale(self):
        repo = srr.analyze_repository(make_node("a"), NOW, NOW - srr.timedelta(days=90))
        self.assertFalse(repo["stale"])
        self.assertEqual(repo["days_since"], 12)

    def test_old_repo_is_stale(self):
        repo = srr.analyze_repository(
            make_node("a", last="2023-01-01T00:00:00Z"), NOW, NOW - srr.timedelta(days=90)
        )
        self.assertTrue(repo["stale"])
        self.assertEqual(srr.format_date(repo["last_commit"]), "2023-01-01")

    def test_recent_issue_counts_as_activity(self):
        node = make_node("a", last="2023-01-01T00:00:00Z")
        node["issues"] = {"nodes": [{"updatedAt": "2024-05-30T00:00:00Z"}]}
        repo = srr.analyze_repository(node, NOW, NOW - srr.timedelta(days=90))
        self.assertFalse(repo["stale"])

    def test_empty_repo_uses_created_date(self):
        node = make_node("a", last=None, created="2022-01-01T00:00:00Z")
        node["defaultBranchRef"] = None
        node["pullRequests"] = {"nodes": []}
        repo = srr.analyze_repository(node, NOW, NOW - srr.timedelta(days=90))
        self.assertTrue(repo["stale"])
        self.assertEqual(repo["last_commit"], None)
        self.assertEqual(srr.format_date(repo["last_activity"]), "2022-01-01")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [
            make_node("active"),
            make_node("stale", last="2023-01-01T00:00:00Z"),
            make_node("archived", last="2023-01-01T00:00:00Z", archived=True),
        ]

    def test_archived_skipped_by_default(self):
        client = FakeClient(self.nodes)
        rows = srr.generate_report(client, ["acme"], days=90, now=NOW)
        self.assertEqual([r["Repository Name"] for r in rows], ["stale"])
        self.assertEqual(client.contributor_calls, ["stale"])

    def test_include_archived(self):
        rows = srr.generate_report(FakeClient(self.nodes), ["acme"], 90, include_archived=True, now=NOW)
        self.assertEqual([r["Repository Name"] for r in rows], ["stale", "archived"])

    def test_days_argument(self):
        rows = srr.generate_report(FakeClient(self.nodes), ["acme"], days=5, now=NOW)
        self.assertEqual([r["Repository Name"] for r in rows], ["active", "stale"])

    def test_csv_with_summary_row(self):
        rows = srr.generate_report(FakeClient(self.nodes), ["acme"], 90, include_archived=True, now=NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            srr.write_csv(rows, path)
            with open(path, newline="") as fh:
                data = list(csv.DictReader(fh))
        self.assertEqual(list(data[0].keys()), srr.FIELDNAMES)
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["Archived Status"], "False")
        self.assertEqual(data[1]["Archived Status"], "True")
        summary = data[-1]
        self.assertEqual(summary["Organization Name"], "TOTAL")
        self.assertEqual(summary["Repository Name"], "2 stale repositories")
        self.assertEqual(summary["Number of Contributors"], "6")
        self.assertEqual(summary["Archived Status"], "True: 1 / False: 1")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.client = srr.GitHubClient("t", sleep=self.sleeps.append)

    def test_graphql_url_for_ghes(self):
        self.assertEqual(
            srr.GitHubClient._graphql_url("https://ghe.example.com/api/v3"),
            "https://ghe.example.com/api/graphql",
        )
        self.assertEqual(self.client.graphql_url, "https://api.github.com/graphql")

    def test_secondary_rate_limit_retry_after(self):
        responses = [
            make_response(403, headers={"retry-after": "7"}, text="secondary rate limit"),
            make_response(200, [{}]),
        ]
        with mock.patch.object(self.client.session, "request", side_effect=responses):
            resp = self.client.request("GET", "https://x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.sleeps, [7])

    def test_secondary_rate_limit_without_retry_after_backs_off(self):
        responses = [
            make_response(403, text="You have exceeded a secondary rate limit"),
            make_response(429, text=""),
            make_response(200, [{}]),
        ]
        with mock.patch.object(self.client.session, "request", side_effect=responses):
            self.client.request("GET", "https://x")
        self.assertEqual(self.sleeps, [60, 120])

    def test_primary_rate_limit_waits_for_reset(self):
        reset = str(int(srr.time.time()) + 30)
        responses = [
            make_response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
            make_response(200, [{}]),
        ]
        with mock.patch.object(self.client.session, "request", side_effect=responses):
            self.client.request("GET", "https://x")
        self.assertTrue(29 <= self.sleeps[0] <= 32)

    def test_plain_forbidden_not_retried(self):
        with mock.patch.object(
            self.client.session, "request", return_value=make_response(403, text="Forbidden")
        ):
            resp = self.client.request("GET", "https://x")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.sleeps, [])

    def test_graphql_rate_limited_error_retried(self):
        limited = make_response(
            200, {"data": None, "errors": [{"type": "RATE_LIMITED"}]}, headers={"x-ratelimit-reset": "0"}
        )
        ok = make_response(200, {"data": {"repositoryOwner": None}})
        with mock.patch.object(self.client.session, "request", side_effect=[limited, ok]):
            payload = self.client.graphql("q", {})
        self.assertEqual(payload["data"], {"repositoryOwner": None})
        self.assertEqual(len(self.sleeps), 1)

    def test_contributor_count_from_link_header(self):
        resp = make_response(
            200, [{}], links={"last": {"url": "https://api.github.com/repos/a/b/contributors?per_page=1&page=42"}}
        )
        with mock.patch.object(self.client.session, "request", return_value=resp):
            self.assertEqual(self.client.contributor_count("a", "b"), 42)

    def test_contributor_count_single_page_and_empty(self):
        with mock.patch.object(self.client.session, "request", return_value=make_response(200, [{}])):
            self.assertEqual(self.client.contributor_count("a", "b"), 1)
        with mock.patch.object(self.client.session, "request", return_value=make_response(204)):
            self.assertEqual(self.client.contributor_count("a", "b"), 0)

    def test_iter_repositories_paginates(self):
        def page(names, has_next, cursor):
            return make_response(
                200,
                {
                    "data": {
                        "rateLimit": {"cost": 1, "remaining": 4000, "resetAt": "2030-01-01T00:00:00Z"},
                        "repositoryOwner": {
                            "repositories": {
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                                "nodes": [make_node(n) for n in names],
                            }
                        },
                    }
                },
            )

        responses = [page(["a", "b"], True, "c1"), page(["c"], False, None)]
        with mock.patch.object(self.client.session, "request", side_effect=responses) as req:
            names = [n["name"] for n in self.client.iter_repositories("acme")]
        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(req.call_args_list[1].kwargs["json"]["variables"]["cursor"], "c1")


class CliTests(unittest.TestCase):
    def test_defaults(self):
        args = srr.parse_args(["acme"])
        self.assertEqual(args.days, 90)
        self.assertFalse(args.include_archived)

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(srr.main(["acme"]), 1)


if __name__ == "__main__":
    unittest.main()
