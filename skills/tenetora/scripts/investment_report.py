"""Read-only monthly commit classification; commit counts are not effort."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from worktree_fingerprint import git_repository_env

CATEGORIES = {'capability', 'distribution', 'debt', 'repair', 'unknown'}
MAX_HISTORY = 20000


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
        env={**git_repository_env(), 'GIT_OPTIONAL_LOCKS': '0'}, check=False)
    if result.returncode:
        raise ValueError('Monthly investment report requires readable Git history.')
    return result.stdout


def classify(commit_type: str, paths: list[str]) -> list[str]:
    labels = set()
    if any(path in {'install.sh', 'install.ps1', '.gitlab-ci.yml'} or
           path.startswith(('.github/', '.claude-plugin/', '.codex-plugin/', '.zcode-plugin/',
               'scripts/package-', 'scripts/publish-', 'scripts/install-',
               'scripts/package_', 'scripts/publish_', 'scripts/install_')) for path in paths):
        labels.add('distribution')
    if commit_type == 'feat' and any(path.startswith(('skills/', 'cli/', 'hooks/')) for path in paths):
        labels.add('capability')
    if commit_type == 'refactor':
        labels.add('debt')
    if commit_type in {'fix', 'revert'}:
        labels.add('repair')
    return sorted(labels or {'unknown'})


def monthly_report(root: Path, month: str, review_file: Path | None = None, revision: str = 'HEAD') -> dict[str, Any]:
    if not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('Month must use YYYY-MM.')
    try:
        start = dt.datetime.strptime(month, '%Y-%m').replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise ValueError('Invalid month.') from exc
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    head = git(root, 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}').decode().strip()
    fields = git(root, 'log', '--no-merges', f'-{MAX_HISTORY + 1}', '-z', '--format=%H%x00%ct%x00%s', head).split(b'\0')
    if fields and fields[-1] == b'':
        fields.pop()
    if len(fields) % 3:
        raise ValueError('Unexpected Git history format.')
    truncated = len(fields) // 3 > MAX_HISTORY
    items = []
    for index in range(0, min(len(fields), MAX_HISTORY * 3), 3):
        sha, timestamp, subject = fields[index:index + 3]
        when = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc)
        if not start <= when < end:
            continue
        sha_text = sha.decode('ascii')
        paths = [path.decode('utf-8', errors='surrogateescape') for path in
            git(root, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z', sha_text).split(b'\0') if path]
        match = re.match(rb'([a-z]+)(?:\([^)]*\))?!?:', subject)
        kind = match.group(1).decode() if match and match.group(1).decode() in {'feat', 'fix', 'docs', 'chore', 'test', 'refactor', 'ci', 'build', 'perf', 'revert'} else 'unknown'
        items.append({'commit': sha_text, 'committed_at': when.isoformat(), 'type_hint': kind,
            'changed_files': len(paths), 'suggested_categories': classify(kind, paths), 'review_status': 'unreviewed'})
    items.sort(key=lambda item: (item['committed_at'], item['commit']))
    by_sha = {item['commit']: item for item in items}
    review_hash = None
    if review_file is not None:
        raw = review_file.read_bytes()
        review_hash = hashlib.sha256(raw).hexdigest()
        review = json.loads(raw)
        if not isinstance(review, dict) or review.get('month') != month or review.get('subject_head') != head or not isinstance(review.get('reviews'), list):
            raise ValueError('Sampled review must match this month and exact observed HEAD.')
        seen = set()
        for annotation in review['reviews']:
            if not isinstance(annotation, dict) or not isinstance(annotation.get('commit'), str) or annotation['commit'] not in by_sha or annotation['commit'] in seen:
                raise ValueError('Review commit is missing, duplicated or outside this report.')
            labels, reviewer = annotation.get('categories'), annotation.get('reviewer_kind')
            if not isinstance(labels, list) or not labels or any(not isinstance(label, str) or label not in CATEGORIES for label in labels) or ('unknown' in labels and len(labels) != 1):
                raise ValueError('Review categories must be explicit known labels, with unknown kept separate.')
            if reviewer not in {'human', 'agent'} or not isinstance(annotation.get('reason'), str) or not annotation['reason'].strip():
                raise ValueError('Review needs an explicit reviewer kind and reason.')
            seen.add(annotation['commit'])
            by_sha[annotation['commit']].update(review_status='annotated', reviewed_categories=sorted(set(labels)),
                reviewer_kind=reviewer, review_reason=annotation['reason'].strip()[:400])
    memberships: Counter[str] = Counter()
    exclusive: Counter[str] = Counter()
    reviewers: Counter[str] = Counter()
    for item in items:
        labels = item.get('reviewed_categories', item['suggested_categories'])
        memberships.update(labels)
        exclusive[labels[0] if len(labels) == 1 else 'multi'] += 1
        if item.get('reviewer_kind'):
            reviewers[item['reviewer_kind']] += 1
    samples = items if len(items) <= 10 else items[:5] + items[-5:]
    return {'version': 1, 'status': 'partial' if truncated else 'ready' if items else 'empty',
        'read_only': True, 'month': month, 'timezone': 'UTC', 'subject_head': head,
        'unit': 'non-merge commits reachable from observed HEAD; uncommitted work excluded',
        'commits_observed': len(items), 'history_truncated': truncated,
        'exclusive_counts': dict(sorted(exclusive.items())), 'category_memberships': dict(sorted(memberships.items())),
        'sampled_review': {'annotated': sum(reviewers.values()), 'by_reviewer_kind': dict(sorted(reviewers.items())),
            'reviewer_provenance': 'caller-declared', 'source_sha256': review_hash,
            'sample_strategy': 'first five and last five chronological commits; descriptive, not statistically representative',
            'samples': samples},
        'items': items, 'effort_hours': None, 'business_value': None,
        'limitations': ['path and conventional type labels are heuristic review hints',
            'multi counts represent multiple categories; category memberships can exceed commit totals',
            'commit counts do not measure time, effort, user benefit or causal effects',
            'no roadmap threshold, automatic task, network report or state update is created']}


def render_text(report: dict[str, Any], language: str = 'en') -> str:
    if language == 'zh':
        labels = {'capability': '能力', 'distribution': '分发基建', 'debt': '还债', 'repair': '修复', 'unknown': '未知', 'multi': '多类'}
        lines = [f"月度提交构成：{report['month']} UTC", f"观测 HEAD：{report['subject_head']}",
            f"数据：{report['status']}；提交：{report['commits_observed']}", '', '| 分类 | 提交数 |', '| --- | ---: |']
        lines += [f'| {labels[key]} | {value} |' for key, value in report['exclusive_counts'].items()]
        lines += ['', '分类仅是复核提示；保留未知和多类，实际工时与业务价值不可推算。',
            f"样本批注：{report['sampled_review']['by_reviewer_kind']}（由调用者声明来源）"]
    else:
        lines = [f"Monthly commit mix: {report['month']} UTC", f"Observed HEAD: {report['subject_head']}",
            f"Data: {report['status']}; commits: {report['commits_observed']}", '', '| Category | Commits |', '| --- | ---: |']
        lines += [f'| {key} | {value} |' for key, value in report['exclusive_counts'].items()]
        lines += ['', 'Labels are review hints. Unknown and multi remain visible; effort and business value are unavailable.',
            f"Sample annotations: {report['sampled_review']['by_reviewer_kind']} (caller-declared)"]
    return '\n'.join(lines)
