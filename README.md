# stale-repo-report

A Python script that generates a CSV report of repositories in one or more GitHub
organizations that have not had meaningful activity for a configurable number of days.

A repository is considered **stale** when none of the following happened within the timeframe:

- Commits / pushes (any branch push, plus the latest commit on the default branch)
- Pull requests opened or updated (this includes code reviews and review comments)
- Issues created or updated (this includes issue comments)
- Releases published
- Contributor activity (covered by the commit, push, PR and issue signals above)

Repositories created within the timeframe are never reported as stale.

## Report fields

| Field | Description |
| --- | --- |
| Organization Name | Organization that was scanned |
| Repository Name | Repository name |
| Repository Visibility | `public`, `private` or `internal` |
| Repository Owner | Login of the account that owns the repository |
| Created Date | Date the repository was created |
| Last Commit Date | Date of the latest commit on the default branch |
| Last Pull Request Date | Date the most recently updated pull request was last updated |
| Last Activity Date | Most recent of commit, push, pull request, issue, release (or creation) dates |
| Days Since Last Activity | Whole days between the last activity and now |
| Number of Contributors | Number of contributors (blank if GitHub cannot compute it, e.g. very large repositories) |
| Archived Status | `True` / `False` |

A `TOTAL` summary row is appended at the bottom with the number of stale repositories,
the total number of contributors and the `True` / `False` counts for **Archived Status**.

## Dependencies

- Python 3.8+
- [`requests`](https://pypi.org/project/requests/)

```shell
pip install -r requirements.txt
```

## Authentication

The script authenticates with a GitHub **personal access token (PAT)**, provided via the
`GITHUB_TOKEN` environment variable or the `--token` argument.

**Fine-grained PAT** (recommended) – set the resource owner to the organization, select
**All repositories**, and grant these read-only repository permissions:

- **Metadata**: Read-only
- **Contents**: Read-only (commits, releases, contributors)
- **Issues**: Read-only
- **Pull requests**: Read-only

**Classic PAT** – grant the `repo` scope (needed for private and internal repositories) and
`read:org`. For public repositories only, `public_repo` and `read:org` are sufficient.

If the organization uses SAML single sign-on, authorize the token for the organization.

## Usage

```shell
export GITHUB_TOKEN=<your token>
python stale_repo_report.py <org> [<org> ...] [options]
```

| Option | Description |
| --- | --- |
| `-d`, `--days DAYS` | Number of days without activity before a repository is stale (default: `90`) |
| `--include-archived` | Include archived repositories (they are skipped by default) |
| `-o`, `--output FILE` | Output CSV file (default: `stale_repos.csv`) |
| `--api-url URL` | API base URL, e.g. `https://HOST/api/v3` for GitHub Enterprise Server (default: `GITHUB_API_URL` or `https://api.github.com`) |
| `--token TOKEN` | Personal access token (default: `GITHUB_TOKEN` environment variable) |

Examples:

```shell
# Repositories in my-org with no activity in the last 90 days
python stale_repo_report.py my-org

# 180 day timeframe, include archived repositories, custom output file
python stale_repo_report.py my-org other-org --days 180 --include-archived -o report.csv
```

## API usage and rate limiting

- Repository metadata and activity (latest commit, pull request, issue and release) are fetched
  with the GraphQL API, 50 repositories per request.
- Contributor counts are fetched with one REST request (`per_page=1`, reading the `Link` header)
  **only** for repositories included in the report.
- **Primary rate limits**: when the limit is exhausted the script sleeps until
  `x-ratelimit-reset`; it also pauses proactively when the GraphQL point budget is nearly spent.
- **Secondary rate limits**: the script honors the `retry-after` header, otherwise it backs off
  exponentially starting at 60 seconds. Transient 5xx and network errors are retried as well.

## Running tests

```shell
python -m unittest -v
```
