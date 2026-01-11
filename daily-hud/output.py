"""
Terminal output formatting with colors and symbols.
"""

import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Status(Enum):
    """Check result status levels."""
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    """Result from a single check."""
    name: str
    status: Status
    message: str
    details: list[str] = field(default_factory=list)

    def add_detail(self, detail: str):
        self.details.append(detail)


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"


# Symbols with color
SYMBOLS = {
    Status.OK: (Colors.GREEN, "✓"),
    Status.WARNING: (Colors.YELLOW, "⚠"),
    Status.ERROR: (Colors.RED, "✗"),
    Status.UNKNOWN: (Colors.WHITE, "?"),
}


def supports_color() -> bool:
    """Check if terminal supports color output."""
    if not hasattr(sys.stdout, "isatty"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


def colorize(text: str, color: str, use_color: bool = True) -> str:
    """Apply color to text if supported."""
    if not use_color:
        return text
    return f"{color}{text}{Colors.RESET}"


def format_status(status: Status, use_color: bool = True) -> str:
    """Format a status with symbol and color."""
    color, symbol = SYMBOLS[status]
    if use_color:
        return f"{color}{symbol}{Colors.RESET}"
    return symbol


def print_header(use_color: bool = True):
    """Print the main header."""
    now = datetime.now().strftime("%a %b %d, %Y %I:%M %p")
    line = "═" * 60

    if use_color:
        print(colorize(line, Colors.CYAN))
        print(colorize(f"  DAILY HUD - {now}", Colors.BOLD + Colors.WHITE))
        print(colorize(line, Colors.CYAN))
    else:
        print(line)
        print(f"  DAILY HUD - {now}")
        print(line)
    print()


def print_section(name: str, use_color: bool = True):
    """Print a section header."""
    header = f"── {name} "
    header = header + "─" * (60 - len(header))

    if use_color:
        print(colorize(header, Colors.DIM))
    else:
        print(header)


def print_result(result: CheckResult, use_color: bool = True, verbose: bool = False):
    """Print a check result."""
    symbol = format_status(result.status, use_color)

    # Main status line
    print(f"{symbol} {result.message}")

    # Details (only for non-OK or if verbose)
    if result.details and (result.status != Status.OK or verbose):
        for detail in result.details:
            # Indent details
            if detail.startswith("  "):
                print(f"  {detail}")
            else:
                print(f"    {detail}")


def print_section_results(section_name: str, results: list[CheckResult],
                          use_color: bool = True, verbose: bool = False):
    """Print a complete section with all its results."""
    print_section(section_name, use_color)

    if not results:
        symbol = format_status(Status.UNKNOWN, use_color)
        print(f"{symbol} No data available")
    else:
        for result in results:
            print_result(result, use_color, verbose)

    print()


def print_summary(results: list[CheckResult], elapsed_seconds: float,
                  use_color: bool = True):
    """Print the summary footer."""
    ok_count = sum(1 for r in results if r.status == Status.OK)
    warn_count = sum(1 for r in results if r.status == Status.WARNING)
    error_count = sum(1 for r in results if r.status == Status.ERROR)
    total = len(results)

    line = "═" * 60

    # Build summary message
    parts = []
    if error_count > 0:
        msg = f"{error_count} error{'s' if error_count != 1 else ''}"
        parts.append(colorize(msg, Colors.RED, use_color) if use_color else msg)
    if warn_count > 0:
        msg = f"{warn_count} warning{'s' if warn_count != 1 else ''}"
        parts.append(colorize(msg, Colors.YELLOW, use_color) if use_color else msg)
    if ok_count > 0 and (error_count > 0 or warn_count > 0):
        msg = f"{ok_count} OK"
        parts.append(colorize(msg, Colors.GREEN, use_color) if use_color else msg)

    if not parts:
        summary = colorize("All systems OK", Colors.GREEN, use_color)
    else:
        summary = ", ".join(parts)

    timing = f"{total} checks in {elapsed_seconds:.1f}s"

    print()
    if use_color:
        print(colorize(line, Colors.CYAN))
    else:
        print(line)
    print(f"Summary: {summary} - {timing}")
    if use_color:
        print(colorize(line, Colors.CYAN))
    else:
        print(line)


def results_to_json(results: dict[str, list[CheckResult]]) -> dict:
    """Convert results to JSON-serializable format."""
    output = {}
    for section, section_results in results.items():
        output[section] = [
            {
                "name": r.name,
                "status": r.status.value,
                "message": r.message,
                "details": r.details,
            }
            for r in section_results
        ]
    return output
