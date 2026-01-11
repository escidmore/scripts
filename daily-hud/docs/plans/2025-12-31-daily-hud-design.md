# Daily HUD - Design Document

**Date:** 2025-12-31
**Status:** Approved

## Overview

A Python script to display a daily "heads-up display" of system status, tasks, and alerts. Runs in terminal with colorized, symbol-based output.

## Architecture

```
daily-hud/
├── daily_hud.py          # Main entry point, orchestrates checks
├── config.yaml           # User's hosts, domains, thresholds
├── checks/
│   ├── __init__.py
│   ├── todoist.py        # Todoist API integration
│   ├── github.py         # GitHub PRs/issues
│   ├── kubernetes.py     # K8s health (calls kubectl)
│   ├── truenas.py        # TrueNAS REST API
│   ├── ssh_hosts.py      # SSH-based checks (disk, updates)
│   ├── windows.py        # Windows host checks
│   ├── certs.py          # Certificate expiry checks
│   ├── domains.py        # Domain expiration (WHOIS)
│   ├── cves.py           # CVE advisories (OSV.dev)
│   └── containers.py     # Image updates, vulnerabilities
├── output.py             # Terminal formatting (colors, symbols)
└── secrets.py            # 1Password integration via `op`
```

## Checks

| Check | Data Source | What It Reports |
|-------|-------------|-----------------|
| Todoist | REST API | Tasks due today, overdue |
| GitHub | REST API | PRs awaiting review, assigned issues |
| Kubernetes | kubectl CLI | Node health, pod status, stale snapshots, CNPG backups, resource pressure |
| TrueNAS | REST API v2 | Pool usage, available updates |
| SSH Hosts | ssh CLI | Disk usage, pending package updates |
| Windows | SSH/WinRM | Disk usage, pending updates |
| Certificates | ssl stdlib | Expiration warnings |
| Domains | python-whois | Expiration warnings |
| CVEs | OSV.dev API | Advisories for installed packages |
| Containers | trivy CLI | Image vulnerabilities |

## Configuration

Location: `~/.config/daily-hud/config.yaml`

```yaml
secrets:
  todoist_token: "op://Personal/Todoist/api_token"
  github_token: "op://Personal/GitHub CLI/token"
  truenas_api_key: "op://Homelab/TrueNAS/api_key"

thresholds:
  disk_warning_percent: 80
  disk_critical_percent: 90
  cert_warning_days: 14
  cert_critical_days: 7
  domain_warning_days: 30
  snapshot_stale_hours: 24
  pod_memory_warning_percent: 80
  pod_cpu_warning_percent: 80

truenas:
  host: "truenas.local"

ssh_hosts:
  - name: "webserver"
    host: "web.example.com"
    user: "admin"
    checks: [disk, updates]

windows_hosts:
  - name: "workstation"
    host: "192.168.1.100"
    method: ssh
    user: "admin"
    checks: [disk, updates]

certificates:
  - https://myapp.example.com
  - mail.example.com:993

domains:
  - example.com

github:
  username: "your-username"

kubernetes:
  context: ""
  namespaces: []
  ignore_namespaces: [kube-system, kube-public]
```

## Output Format

- Minimal text with color AND symbols (accessible)
- ✓ (green) = OK
- ⚠ (yellow) = Warning
- ✗ (red) = Error
- OK sections show one line; problems expand with details
- Summary line at bottom with counts and timing

## CLI Options

- `--only <checks>` - Run specific checks only
- `--verbose` - Show OK details too
- `--json` - Output as JSON
- `--no-cache` - Force fresh data
- `--config <path>` - Alternate config file

## Error Handling

- Checks run in parallel with timeouts (default 30s)
- Failed checks show error message, don't block others
- Exit codes: 0 = OK, 1 = warnings, 2 = errors

## Dependencies

**Python:**
- requests
- python-whois
- pyyaml
- rich

**External tools:**
- op (1Password CLI)
- kubectl
- ssh
- trivy
