"""
Todoist integration - fetch tasks due today and overdue.
"""

import requests
from datetime import datetime, date
from typing import Optional

from output import CheckResult, Status


TODOIST_API_URL = "https://api.todoist.com/rest/v2"


def check_todoist(token: Optional[str], config: dict) -> list[CheckResult]:
    """
    Check Todoist for due and overdue tasks.

    Args:
        token: Todoist API token
        config: Todoist config section

    Returns:
        List of CheckResult objects
    """
    if not token:
        return [CheckResult(
            name="todoist",
            status=Status.ERROR,
            message="Todoist: No API token configured"
        )]

    try:
        headers = {"Authorization": f"Bearer {token}"}

        # Fetch active tasks
        response = requests.get(
            f"{TODOIST_API_URL}/tasks",
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        tasks = response.json()

        # Filter to tasks with due dates
        today = date.today()
        overdue = []
        due_today = []
        project_filter = config.get("projects", [])

        for task in tasks:
            due = task.get("due")
            if not due:
                continue

            # Parse due date
            due_date_str = due.get("date", "")
            if not due_date_str:
                continue

            try:
                # Handle both date and datetime formats
                if "T" in due_date_str:
                    due_date = datetime.fromisoformat(due_date_str.replace("Z", "+00:00")).date()
                else:
                    due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            # Filter by project if specified
            if project_filter:
                project_id = task.get("project_id")
                if project_id not in project_filter:
                    continue

            # Categorize
            task_info = {
                "content": task.get("content", "Untitled"),
                "due_date": due_date,
                "due_string": due.get("string", ""),
            }

            if due_date < today:
                days_overdue = (today - due_date).days
                task_info["days_overdue"] = days_overdue
                overdue.append(task_info)
            elif due_date == today:
                due_today.append(task_info)

        # Build result
        results = []
        total_tasks = len(overdue) + len(due_today)

        if total_tasks == 0:
            results.append(CheckResult(
                name="todoist",
                status=Status.OK,
                message="Todoist: No tasks due today"
            ))
        else:
            # Determine status based on overdue
            if overdue:
                status = Status.WARNING
                msg = f"Todoist: {len(due_today)} due today, {len(overdue)} overdue"
            else:
                status = Status.OK
                msg = f"Todoist: {len(due_today)} task{'s' if len(due_today) != 1 else ''} due today"

            result = CheckResult(
                name="todoist",
                status=status,
                message=msg
            )

            # Add overdue details first
            for task in sorted(overdue, key=lambda t: t["days_overdue"], reverse=True):
                days = task["days_overdue"]
                day_str = "day" if days == 1 else "days"
                result.add_detail(f"[!] Overdue ({days} {day_str}): {task['content']}")

            # Add today's tasks
            for task in due_today:
                due_str = f" @ {task['due_string']}" if task["due_string"] and "today" not in task["due_string"].lower() else ""
                result.add_detail(f"Today: {task['content']}{due_str}")

            results.append(result)

        return results

    except requests.exceptions.Timeout:
        return [CheckResult(
            name="todoist",
            status=Status.ERROR,
            message="Todoist: Request timed out"
        )]
    except requests.exceptions.RequestException as e:
        return [CheckResult(
            name="todoist",
            status=Status.ERROR,
            message=f"Todoist: API error - {str(e)}"
        )]
