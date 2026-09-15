#!/usr/bin/env python3
"""Structured installer events, bounded logs, and terminal progress rendering."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import atexit
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
from typing import TextIO
import unicodedata

from path_security import is_redirected_path


EVENT_SCHEMA_VERSION = 1
MAX_LOG_FILES = 10
MAX_LOG_BYTES = 20 * 1024 * 1024
RECENT_LINES = 3
SECRET_FIELD_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "password",
    "passwd",
    "proxyauthorization",
    "refreshtoken",
    "secret",
    "token",
}
SECRET_VALUE_PATTERN = r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,;&]+)'
SECRET_PATTERNS = (
    re.compile(
        r"(?i)((?:proxy[_-]?)?authorization\s*[:=]\s*)(?:(?:basic|bearer)\s+)?[^\s,;&]+"
    ),
    re.compile(
        r"(?i)((?:\"|\')(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"proxy[_-]?authorization|authorization|token|password|passwd|secret|api[_-]?key)"
        r"(?:\"|\')\s*:\s*)"
        + SECRET_VALUE_PATTERN
    ),
    re.compile(
        r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"proxy[_-]?authorization|authorization|token|password|passwd|secret|api[_-]?key)"
        r"\b\s*[:=]\s*)"
        + SECRET_VALUE_PATTERN
    ),
    re.compile(r"glpat-[A-Za-z0-9_.-]+"),
    re.compile(
        r"\b(?:"
        r"ghp_[A-Za-z0-9]{12,}|"
        r"github_pat_[A-Za-z0-9_]{12,}|"
        r"sk-[A-Za-z0-9_.-]{12,}|"
        r"xox[baprs]-[A-Za-z0-9-]{12,}"
        r")\b"
    ),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@"),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact(text: str) -> str:
    sanitized = text
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("glpat"):
            sanitized = pattern.sub("[REDACTED_TOKEN]", sanitized)
        elif pattern.pattern.startswith("\\b"):
            sanitized = pattern.sub("[REDACTED_TOKEN]", sanitized)
        elif pattern.pattern.startswith("(?i)([a-z]"):
            sanitized = pattern.sub(r"\1[REDACTED]@", sanitized)
        else:
            sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            normalized = re.sub(r"[^a-z0-9]", "", name.strip().lower())
            redacted[name] = "[REDACTED]" if normalized in SECRET_FIELD_KEYS else redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


def reject_unsafe_directory_ancestors(directory: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(str(directory)))
    root = Path(absolute.anchor)
    for candidate in reversed((absolute, *absolute.parents)):
        if candidate == root or candidate.parent == root:
            continue
        if is_redirected_path(candidate):
            raise RuntimeError(f"installer {label} directory has a symlink ancestor: {candidate}")


def ensure_secure_directory(directory: Path, *, label: str) -> None:
    reject_unsafe_directory_ancestors(directory, label=label)
    if directory.exists() and (is_redirected_path(directory) or not directory.is_dir()):
        raise RuntimeError(f"installer {label} directory is unsafe: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    if is_redirected_path(directory) or not directory.is_dir():
        raise RuntimeError(f"installer {label} directory is unsafe: {directory}")
    try:
        directory.chmod(0o700)
    except OSError as exc:
        if os.name != "nt":
            raise RuntimeError(f"cannot secure installer {label} directory: {directory}: {exc}") from exc


def append_secure(path: Path, text: str, *, label: str) -> None:
    if path.exists() and (is_redirected_path(path) or not path.is_file()):
        raise RuntimeError(f"installer {label} path is unsafe: {path}")
    ensure_secure_directory(path.parent, label=label)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"cannot open installer {label} safely: {path}: {exc}") from exc
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(text)
    try:
        path.chmod(0o600)
    except OSError as exc:
        if os.name != "nt":
            raise RuntimeError(f"cannot secure installer {label} file: {path}: {exc}") from exc


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def truncate_display(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    result: list[str] = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > max(width - 1, 0):
            break
        result.append(char)
        used += char_width
    return "".join(result) + ("…" if width > 0 else "")


def rotate_logs(directory: Path) -> None:
    ensure_secure_directory(directory, label="log")
    files = sorted(
        (path for path in directory.glob("install-*.log") if path.is_file() and not is_redirected_path(path)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total = 0
    for index, path in enumerate(files):
        size = path.stat().st_size
        total += size
        if index >= MAX_LOG_FILES or total > MAX_LOG_BYTES:
            path.unlink(missing_ok=True)


class EventWriter:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            if path.exists() and (is_redirected_path(path) or not path.is_file()):
                raise RuntimeError(f"installer event path is unsafe: {path}")
            ensure_secure_directory(path.parent, label="event")

    def write(self, event: str, **fields: object) -> None:
        if self.path is None:
            return
        payload = redact_value({"schema_version": EVENT_SCHEMA_VERSION, "time": now(), "event": event, **fields})
        append_secure(
            self.path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            label="event",
        )


class RunLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            if path.exists() and (is_redirected_path(path) or not path.is_file()):
                raise RuntimeError(f"installer log path is unsafe: {path}")
            ensure_secure_directory(path.parent, label="log")
            rotate_logs(path.parent)

    def write(self, text: str) -> None:
        if self.path is None or not text:
            return
        append_secure(self.path, f"{now()} {redact(text).rstrip()}\n", label="log")
        rotate_logs(self.path.parent)


def write_failure_artifacts(
    event_path: Path | None,
    log_path: Path | None,
    *,
    title: str,
    phase: str,
    message: str,
    details: list[str] | None = None,
) -> None:
    """Persist a failure even when the normal progress reporter was never created."""

    safe_message = redact(message).strip()
    safe_details: list[str] = []
    for detail in details or []:
        sanitized = redact(str(detail)).strip()
        if sanitized:
            safe_details.append(sanitized)
    errors: list[str] = []

    if event_path is not None:
        try:
            events = EventWriter(event_path)
            events.write("start", title=title, total=1, phase=phase)
            if safe_message:
                events.write("detail", message=safe_message)
            for detail in safe_details:
                events.write("detail", message=detail)
            events.write(
                "complete",
                current=0,
                total=1,
                status="BLOCKED",
                summary=safe_message,
                phase=phase,
            )
        except Exception as exc:
            errors.append(redact(str(exc)))

    if log_path is not None:
        try:
            log = RunLog(log_path)
            log.write(f"phase={phase} status=BLOCKED")
            if safe_message:
                log.write(safe_message)
            for detail in safe_details:
                log.write(detail)
        except Exception as exc:
            errors.append(redact(str(exc)))

    if errors:
        raise RuntimeError("; ".join(errors))


class InstallProgress:
    def __init__(
        self,
        *,
        total: int,
        title: str,
        mode: str = "auto",
        compact: bool = False,
        verbose: bool = False,
        event_path: Path | None = None,
        log_path: Path | None = None,
        stream: TextIO = sys.stdout,
    ) -> None:
        self.total = max(total, 1)
        self.title = title
        self.compact = compact
        self.verbose = verbose
        self.stream = stream
        self.tty = (mode == "always" or mode == "auto" and stream.isatty()) and not verbose
        self.enabled = mode != "never"
        self.current = 0
        self.recent: deque[str] = deque(maxlen=RECENT_LINES)
        self.surface_lines: dict[str, str] = {}
        self.surface_statuses: dict[str, str] = {}
        self.rendered_lines = 0
        self.cursor_hidden = False
        self.signal_handlers: dict[int, object] = {}
        self.events = EventWriter(event_path)
        self.log_file = RunLog(log_path)
        self.events.write("start", title=title, total=total)
        atexit.register(self.restore_cursor)
        atexit.register(self.restore_signal_handlers)
        if self.tty:
            self.install_signal_handlers()

    def detail(self, line: str) -> None:
        sanitized = redact(line.strip())
        if not sanitized:
            return
        self.log_file.write(sanitized)
        self.events.write("detail", message=sanitized)
        self.recent.append(f"  {sanitized}")
        if self.enabled and self.tty:
            self._render()
        if self.verbose and not self.tty:
            print(sanitized, file=self.stream)

    def _status_line(self, label: str, status: str) -> str:
        markers = (
            {"ok": "完成", "current": "验证", "pending": "等待", "failed": "失败", "blocked": "阻断"}
            if os.environ.get("TENETORA_LANG") == "zh"
            else {"ok": "OK", "current": "VERIFY", "pending": "WAIT", "failed": "FAIL", "blocked": "BLOCK"}
        )
        marker = markers.get(status, status.upper())
        return f"{marker} {label}"

    def _set_surface_status(self, label: str, status: str) -> str:
        previous = self.surface_lines.get(label)
        if previous is not None:
            rows = list(self.recent)
            for index in range(len(rows) - 1, -1, -1):
                if rows[index] == previous:
                    del rows[index]
                    break
            self.recent = deque(rows, maxlen=RECENT_LINES)
        line = self._status_line(label, status)
        self.recent.append(line)
        self.surface_lines[label] = line
        self.surface_statuses[label] = status
        return line

    def advance(self, label: str, *, status: str = "ok", details: list[str] | None = None) -> None:
        self.current = min(self.current + 1, self.total)
        for detail in details or []:
            self.detail(detail)
        line = self._set_surface_status(label, status)
        self.events.write("progress", current=self.current, total=self.total, status=status, label=label)
        if not self.enabled:
            return
        if self.tty:
            self._render()
        elif self.compact and status != "current":
            print(f"[{self.current}/{self.total}] {line}", file=self.stream)

    def resolve(self, label: str, *, status: str, details: list[str] | None = None) -> None:
        """Replace an in-flight surface with its verified final status without advancing twice."""
        for detail in details or []:
            self.detail(detail)
        line = self._set_surface_status(label, status)
        self.events.write(
            "progress",
            current=self.current,
            total=self.total,
            status=status,
            label=label,
            resolved=True,
        )
        if not self.enabled:
            return
        if self.tty:
            self._render()
        elif self.compact:
            print(f"[{self.current}/{self.total}] {line}", file=self.stream)

    def fail_unresolved(self, detail: str) -> int:
        """Resolve every in-flight surface as blocked so failures leave no stale VERIFY rows."""
        pending = [label for label, status in self.surface_statuses.items() if status == "current"]
        for index, label in enumerate(pending):
            self.resolve(
                label,
                status="blocked",
                details=[detail] if index == len(pending) - 1 else None,
            )
        return len(pending)

    def finish(self, status: str, *, summary: str = "") -> None:
        self.events.write("complete", current=self.current, total=self.total, status=status, summary=summary)
        self.log_file.write(f"complete status={status} {summary}".strip())
        if self.enabled and self.tty:
            self._render(status=status)
            self.restore_cursor()
            self.restore_signal_handlers()
            self.stream.write("\n")
            self.stream.flush()

    def restore_cursor(self) -> None:
        if self.cursor_hidden:
            self.stream.write("\x1b[?25h")
            self.stream.flush()
            self.cursor_hidden = False

    def install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            self.signal_handlers[signum] = previous
            signal.signal(signum, self._handle_signal)

    def restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, previous in list(self.signal_handlers.items()):
            signal.signal(signum, previous)
        self.signal_handlers.clear()

    def _handle_signal(self, signum: int, frame: object) -> None:
        previous = self.signal_handlers.get(signum)
        self.restore_cursor()
        self.restore_signal_handlers()
        if callable(previous):
            previous(signum, frame)
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    def _render(self, *, status: str | None = None) -> None:
        width = max(40, shutil.get_terminal_size((100, 24)).columns)
        bar_width = min(36, max(12, width - 36))
        ratio = min(self.current / self.total, 1.0)
        filled = int(bar_width * ratio)
        bar = "#" * filled + "-" * (bar_width - filled)
        headline = f"{self.title} [{bar}] {self.current}/{self.total} {int(ratio * 100):3d}%"
        if status:
            if os.environ.get("TENETORA_LANG") == "zh":
                status = {
                    "FULL": "完成",
                    "READY": "就绪",
                    "PENDING_TRUST": "完成，等待信任",
                    "PARTIAL": "部分失败",
                    "BLOCKED": "失败",
                    "DRY_RUN": "演练",
                }.get(status, status)
            else:
                status = {
                    "FULL": "COMPLETE",
                    "READY": "READY",
                    "PENDING_TRUST": "COMPLETE, TRUST REQUIRED",
                    "PARTIAL": "PARTIAL FAILURE",
                    "BLOCKED": "FAILED",
                    "DRY_RUN": "DRY RUN",
                }.get(status, status)
            headline += f" {status}"
        lines = [truncate_display(headline, width), *(truncate_display(line, width) for line in self.recent)]
        if not self.cursor_hidden:
            self.stream.write("\x1b[?25l")
            self.cursor_hidden = True
        if self.rendered_lines:
            self.stream.write(f"\x1b[{self.rendered_lines}F")
        for line in lines:
            self.stream.write("\x1b[2K" + line + "\n")
        self.stream.flush()
        self.rendered_lines = len(lines)
