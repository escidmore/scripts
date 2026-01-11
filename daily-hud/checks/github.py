"""
GitHub integration - check PRs awaiting review and assigned issues.
"""

import requests
from datetime import datetime, timezone
from typing import Optional

from output import CheckResult, Status


GITHUB_API_URL = "https://api.github.com"


def _format_age(created_at: str) -> str:
    """Format how old a PR/issue is."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - created).days

        if days == 0:
            return "today"
        elif days == 1:
            return "1 day old"
        else:
            return f"{days} days old"
    except (ValueError, TypeError):
        return ""


def check_github(token: Optional[str], config: dict) -> list[CheckResult]:
    """
    Check GitHub for PRs awaiting review and assigned issues.

    Args:
        token: GitHub personal access token
        config: GitHub config section

    Returns:
        List of CheckResult objects
    """
    if not token:
        return [CheckResult(
            name="github",
            status=Status.ERROR,
            message="GitHub: No API token configured"
        )]

    username = config.get("username", "")
    if not username:
        return [CheckResult(
            name="github",
            status=Status.ERROR,
            message="GitHub: No username configured"
        )]

    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        prs_to_review = []
        assigned_issues = []

        # Search for PRs where user is requested reviewer
        pr_query = f"is:pr is:open review-requested:{username}"
        response = requests.get(
            f"{GITHUB_API_URL}/search/issues",
            headers=headers,
            params={"q": pr_query, "per_page": 20},
            timeout=15
        )
        response.raise_for_status()

        for item in response.json().get("items", []):
            repo_url = item.get("repository_url", "")
            repo_name = "/".join(repo_url.split("/")[-2:]) if repo_url else "unknown"
            prs_to_review.append({
                "number": item.get("number"),
                "title": item.get("title", "Untitled"),
                "repo": repo_name,
                "created_at": item.get("created_at", ""),
                "url": item.get("html_url", ""),
            })

        # Search for issues assigned to user
        issue_query = f"is:issue is:open assignee:{username}"
        response = requests.get(
            f"{GITHUB_API_URL}/search/issues",
            headers=headers,
            params={"q": issue_query, "per_page": 20},
            timeout=15
        )
        response.raise_for_status()

        for item in response.json().get("items", []):
            repo_url = item.get("repository_url", "")
            repo_name = "/".join(repo_url.split("/")[-2:]) if repo_url else "unknown"
            assigned_issues.append({
                "number": item.get("number"),
                "title": item.get("title", "Untitled"),
                "repo": repo_name,
                "created_at": item.get("created_at", ""),
                "url": item.get("html_url", ""),
            })

        # Build result
        pr_count = len(prs_to_review)
        issue_count = len(assigned_issues)

        if pr_count == 0 and issue_count == 0:
            return [CheckResult(
                name="github",
                status=Status.OK,
                message="GitHub: No PRs or issues awaiting action"
            )]

        # Determine status
        status = Status.WARNING if pr_count > 0 or issue_count > 0 else Status.OK

        parts = []
        if pr_count > 0:
            parts.append(f"{pr_count} PR{'s' if pr_count != 1 else ''} awaiting review")
        if issue_count > 0:
            parts.append(f"{issue_count} issue{'s' if issue_count != 1 else ''} assigned")

        result = CheckResult(
            name="github",
            status=status,
            message=f"GitHub: {', '.join(parts)}"
        )

        # Add PR details
        for pr in prs_to_review:
            age = _format_age(pr["created_at"])
            age_str = f" - {age}" if age else ""
            result.add_detail(f"PR #{pr['number']}: {pr['title']} ({pr['repo']}){age_str}")

        # Add issue details
        for issue in assigned_issues:
            result.add_detail(f"Issue #{issue['number']}: {issue['title']} ({issue['repo']})")

        return [result]

    except requests.exceptions.Timeout:
        return [CheckResult(
            name="github",
            status=Status.ERROR,
            message="GitHub: Request timed out"
        )]
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        if "401" in error_msg:
            error_msg = "Authentication failed - check token"
        return [CheckResult(
            name="github",
            status=Status.ERROR,
            message=f"GitHub: API error - {error_msg}"
        )]
