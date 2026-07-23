from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dateutil import relativedelta
from lxml import etree


# ============================================================
# Profile configuration
# ============================================================

USER_NAME = os.getenv("USER_NAME", "decioduartee")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
BIRTHDAY = dt.datetime(2000, 4, 10)

GRAPHQL_URL = "https://api.github.com/graphql"
SVG_FILES = ("dark_mode.svg", "light_mode.svg")
CACHE_DIR = Path("cache")
CACHE_FILE = CACHE_DIR / f"{hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()}.json"

QUERY_COUNT = 0


# ============================================================
# Models
# ============================================================

@dataclass
class RepoStats:
    commit_count: int = 0
    my_commits: int = 0
    additions: int = 0
    deletions: int = 0


@dataclass
class ProfileStats:
    age: str
    commits: int
    stars: int
    owned_repos: int
    contributed_repos: int
    followers: int
    additions: int
    deletions: int

    @property
    def net_lines(self) -> int:
        return self.additions - self.deletions


# ============================================================
# GitHub GraphQL client
# ============================================================

class GitHubClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError(
                "ACCESS_TOKEN was not found.\n"
                "PowerShell example:\n"
                '$env:ACCESS_TOKEN="YOUR_GITHUB_TOKEN"\n'
                "Then run: python today.py"
            )

        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"{USER_NAME}-profile-readme",
        }

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        global QUERY_COUNT
        QUERY_COUNT += 1

        response = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=self.headers,
            timeout=60,
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"GitHub returned an invalid response ({response.status_code})."
            ) from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"GitHub API error {response.status_code}: {payload}"
            )

        if payload.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")

        return payload["data"]


# ============================================================
# Profile calculations
# ============================================================

def plural(value: int, singular: str) -> str:
    return singular if value == 1 else f"{singular}s"


def calculate_age(birthday: dt.datetime) -> str:
    difference = relativedelta.relativedelta(dt.datetime.now(), birthday)
    birthday_icon = " 🎂" if difference.months == 0 and difference.days == 0 else ""

    return (
        f"{difference.years} {plural(difference.years, 'year')}, "
        f"{difference.months} {plural(difference.months, 'month')}, "
        f"{difference.days} {plural(difference.days, 'day')}"
        f"{birthday_icon}"
    )


def fetch_account_data(client: GitHubClient) -> tuple[str, int, int]:
    query = """
    query($login: String!) {
      user(login: $login) {
        id
        followers {
          totalCount
        }
        contributionsCollection {
          contributionCalendar {
            totalContributions
          }
        }
      }
    }
    """

    user = client.query(query, {"login": USER_NAME})["user"]

    return (
        user["id"],
        int(user["followers"]["totalCount"]),
        int(user["contributionsCollection"]["contributionCalendar"]["totalContributions"]),
    )


def fetch_repositories(
    client: GitHubClient,
    affiliations: list[str],
) -> list[dict[str, Any]]:
    query = """
    query(
      $login: String!,
      $affiliations: [RepositoryAffiliation],
      $cursor: String
    ) {
      user(login: $login) {
        repositories(
          first: 60,
          after: $cursor,
          ownerAffiliations: $affiliations
        ) {
          edges {
            node {
              nameWithOwner
              stargazerCount
              defaultBranchRef {
                target {
                  ... on Commit {
                    history {
                      totalCount
                    }
                  }
                }
              }
            }
          }
          pageInfo {
            endCursor
            hasNextPage
          }
        }
      }
    }
    """

    repositories: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        data = client.query(
            query,
            {
                "login": USER_NAME,
                "affiliations": affiliations,
                "cursor": cursor,
            },
        )

        repo_data = data["user"]["repositories"]
        repositories.extend(edge["node"] for edge in repo_data["edges"])

        if not repo_data["pageInfo"]["hasNextPage"]:
            break

        cursor = repo_data["pageInfo"]["endCursor"]

    return repositories


def fetch_repo_commit_stats(
    client: GitHubClient,
    owner: str,
    repo: str,
    owner_id: str,
) -> RepoStats:
    query = """
    query($owner: String!, $repo: String!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                edges {
                  node {
                    additions
                    deletions
                    author {
                      user {
                        id
                      }
                    }
                  }
                }
                pageInfo {
                  endCursor
                  hasNextPage
                }
              }
            }
          }
        }
      }
    }
    """

    result = RepoStats()
    cursor: str | None = None

    while True:
        data = client.query(
            query,
            {"owner": owner, "repo": repo, "cursor": cursor},
        )

        repository = data.get("repository")
        branch = repository.get("defaultBranchRef") if repository else None

        if not branch:
            return result

        history = branch["target"]["history"]

        for edge in history["edges"]:
            commit = edge["node"]
            author = commit.get("author") or {}
            user = author.get("user") or {}

            result.commit_count += 1

            if user.get("id") == owner_id:
                adds = int(commit.get("additions", 0))
                dels = int(commit.get("deletions", 0))

                # Ignore commits with unrealistic line counts (usually generated files)
                if adds > 10000 or dels > 10000:
                    print(
                        f"Skipping suspicious commit in {owner}/{repo}: "
                        f"+{adds:,} / -{dels:,}"
                    )
                    continue

                result.my_commits += 1
                result.additions += adds
                result.deletions += dels

        if not history["pageInfo"]["hasNextPage"]:
            break

        cursor = history["pageInfo"]["endCursor"]

    return result


# ============================================================
# Cache
# ============================================================

def load_cache() -> dict[str, dict[str, int]]:
    if not CACHE_FILE.exists():
        return {}

    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict[str, dict[str, int]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def build_repository_stats(
    client: GitHubClient,
    repositories: list[dict[str, Any]],
    owner_id: str,
) -> RepoStats:
    old_cache = load_cache()
    new_cache: dict[str, dict[str, int]] = {}
    total = RepoStats()

    for repository in repositories:
        name_with_owner = repository["nameWithOwner"]
        branch = repository.get("defaultBranchRef")
        current_commit_count = 0

        if branch:
            current_commit_count = int(
                branch["target"]["history"]["totalCount"]
            )

        cached = old_cache.get(name_with_owner)

        if cached and int(cached.get("commit_count", -1)) == current_commit_count:
            stats = RepoStats(
                commit_count=int(cached.get("commit_count", 0)),
                my_commits=int(cached.get("my_commits", 0)),
                additions=int(cached.get("additions", 0)),
                deletions=int(cached.get("deletions", 0)),
            )
        elif current_commit_count == 0:
            stats = RepoStats()
        else:
            owner, repo = name_with_owner.split("/", 1)
            print(f"Updating cache: {name_with_owner}")
            stats = fetch_repo_commit_stats(client, owner, repo, owner_id)

        new_cache[name_with_owner] = {
            "commit_count": current_commit_count,
            "my_commits": stats.my_commits,
            "additions": stats.additions,
            "deletions": stats.deletions,
        }

        total.commit_count += current_commit_count
        total.my_commits += stats.my_commits
        total.additions += stats.additions
        total.deletions += stats.deletions

    save_cache(new_cache)
    return total


# ============================================================
# SVG updater
# ============================================================

def set_svg_text(root: etree._Element, element_id: str, value: Any) -> None:
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = str(value)


def set_justified_value(
    root: etree._Element,
    element_id: str,
    value: Any,
    target_length: int = 0,
) -> None:
    formatted = f"{value:,}" if isinstance(value, int) else str(value)
    set_svg_text(root, element_id, formatted)

    missing = max(0, target_length - len(formatted))

    if missing == 0:
        dots = ""
    elif missing == 1:
        dots = " "
    elif missing == 2:
        dots = ". "
    else:
        dots = f" {'.' * missing} "

    set_svg_text(root, f"{element_id}_dots", dots)


def update_svg(filename: str, stats: ProfileStats) -> None:
    path = Path(filename)

    if not path.exists():
        print(f"Skipped: {filename} was not found.")
        return

    tree = etree.parse(str(path))
    root = tree.getroot()

    set_justified_value(root, "age_data", stats.age)
    set_justified_value(root, "commit_data", stats.commits, 22)
    set_justified_value(root, "star_data", stats.stars, 14)
    set_justified_value(root, "repo_data", stats.owned_repos, 6)
    set_justified_value(root, "contrib_data", stats.contributed_repos)
    set_justified_value(root, "follower_data", stats.followers, 10)
    set_justified_value(root, "loc_data", stats.net_lines, 9)
    set_justified_value(root, "loc_add", stats.additions)
    set_justified_value(root, "loc_del", stats.deletions, 7)

    tree.write(
        str(path),
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=False,
    )

    print(f"Updated: {filename}")


# ============================================================
# Main
# ============================================================

def main() -> None:
    started_at = time.perf_counter()
    client = GitHubClient(ACCESS_TOKEN)

    print(f"Loading GitHub profile: {USER_NAME}")

    owner_id, followers, yearly_contributions = fetch_account_data(client)

    owned_repositories = fetch_repositories(client, ["OWNER"])
    all_repositories = fetch_repositories(
        client,
        ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"],
    )

    repository_stats = build_repository_stats(
        client,
        all_repositories,
        owner_id,
    )

    profile_stats = ProfileStats(
        age=calculate_age(BIRTHDAY),
        commits=repository_stats.my_commits,
        stars=sum(int(repo["stargazerCount"]) for repo in owned_repositories),
        owned_repos=len(owned_repositories),
        contributed_repos=len(all_repositories),
        followers=followers,
        additions=repository_stats.additions,
        deletions=repository_stats.deletions,
    )

    for svg_file in SVG_FILES:
        update_svg(svg_file, profile_stats)

    elapsed = time.perf_counter() - started_at

    print()
    print("Profile updated successfully.")
    print(f"Age: {profile_stats.age}")
    print(f"Commits found: {profile_stats.commits:,}")
    print(f"Contributions this year: {yearly_contributions:,}")
    print(f"Repositories: {profile_stats.owned_repos:,}")
    print(f"Followers: {profile_stats.followers:,}")
    print(f"Net lines: {profile_stats.net_lines:,}")
    print(f"GraphQL calls: {QUERY_COUNT}")
    print(f"Execution time: {elapsed:.2f}s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
    except Exception as error:
        raise SystemExit(f"\nError: {error}") from error