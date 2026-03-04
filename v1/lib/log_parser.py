"""Phase 1: Parse Ruby hash log file to build repo_branches config."""

import re
from pathlib import Path
from typing import Dict, List, Optional, Set


def _version_to_branch(version: str) -> str:
    """
    Convert version tag to branch name.

    Rules:
    - 4+ parts (e.g. 6.2.1.5): take first 3; if 3rd part is '0', use first 2
    - 3 parts (e.g. 6.2.2): take first 2
    - 2 parts (e.g. 6.3): use as-is
    Prefix with 'v'.
    """
    parts = version.split('.')
    if len(parts) >= 4:
        major, minor, patch = parts[0], parts[1], parts[2]
        if patch == '0':
            return f"v{major}.{minor}"
        return f"v{major}.{minor}.{patch}"
    elif len(parts) == 3:
        return f"v{parts[0]}.{parts[1]}"
    else:
        return f"v{'.'.join(parts)}"


def parse_log_to_repo_branches(
    log_path: Path,
    ignore_tasks: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """
    Parse Ruby hash from log file, extract {project: [branches]}.

    For each project, iterates version entries, skips those with empty tasks
    (or tasks containing only ignored task names), converts version key to
    branch name via _version_to_branch().
    Returns only projects that have at least one qualifying version.
    """
    _ignore: Set[str] = set(ignore_tasks) if ignore_tasks else set()
    text = log_path.read_text(encoding='utf-8')
    result: Dict[str, List[str]] = {}

    # Top-level project names: line starts with '{' or single space
    project_re = re.compile(r'^[{ ]"?([a-zA-Z][a-zA-Z0-9_-]*)"?\s*:\s*$', re.MULTILINE)
    # Version keys like "6.2.1.5" =>
    version_re = re.compile(r'"([^"]+)"\s*=>')
    # tasks: [...] (possibly multiline)
    tasks_re = re.compile(r'tasks:\s*\[([^\]]*)\]')
    # Individual quoted values inside tasks array
    task_value_re = re.compile(r'"([^"]+)"')

    matches = list(project_re.finditer(text))
    for i, m in enumerate(matches):
        project = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end]

        branches: set = set()
        version_matches = list(version_re.finditer(section))
        for vi, vm in enumerate(version_matches):
            version = vm.group(1)
            v_start = vm.end()
            v_end = version_matches[vi + 1].start() if vi + 1 < len(version_matches) else len(section)
            version_section = section[v_start:v_end]

            # Check tasks is non-empty and has at least one non-ignored task
            tm = tasks_re.search(version_section)
            if tm:
                bracket = tm.group(1).strip()
                if bracket:
                    tasks = [t.group(1) for t in task_value_re.finditer(bracket)]
                    meaningful = [t for t in tasks if t not in _ignore]
                    if meaningful:
                        branches.add(_version_to_branch(version))

        if branches:
            result[project] = sorted(branches)

    return result
