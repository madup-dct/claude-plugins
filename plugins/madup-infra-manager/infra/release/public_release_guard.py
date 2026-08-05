#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import hashlib
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EXIT_USAGE = 2
EXIT_DENYLIST = 3
EXIT_GIT = 4
EXIT_LOCAL = 10
EXIT_BLOB = 11
EXIT_DIFF = 12

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_DENYLIST_RELATIVE = Path("plugins/madup-infra-manager/infra/release/denylist.exact")
MAX_DENYLIST_BYTES = 1024 * 1024
MAX_WORKTREE_BYTES = 8 * 1024 * 1024

PATH_SAFE_ASCII = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
GENERIC_PATTERNS = (
    ("pem-private-key", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("github-token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("google-api-key", re.compile(rb"AIza[0-9A-Za-z\-_]{35}")),
    ("slack-token", re.compile(rb"xox[baprs]-[0-9A-Za-z-]{10,}")),
    (
        "oauth-client-secret",
        re.compile(
            rb'(?i)(?:client_secret|refresh_token)\s*[:=]\s*["\']?[A-Za-z0-9._~+/=-]{12,}["\']?'
        ),
    ),
    (
        "generated-run-app-origin",
        re.compile(
            rb"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:\.a)?\.run\.app/?"
        ),
    ),
)
SERVICE_ACCOUNT_FIELD_PATTERNS = {
    "type": re.compile(rb'"type"\s*:\s*"service_account"'),
    "private_key": re.compile(rb'"private_key"\s*:\s*"[^"]+"'),
    "client_email": re.compile(rb'"client_email"\s*:\s*"[^"]+"'),
}


class GuardError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Match:
    rule_id: str
    fingerprint: str


@dataclass(frozen=True)
class Finding:
    rule_id: str
    scope: str
    path: str
    fingerprint: str
    commit: str | None = None

    def render(self) -> str:
        parts = [
            f"rule={self.rule_id}",
            f"scope={self.scope}",
            f"path={_display_path(self.path)}",
        ]
        if self.commit:
            parts.append(f"commit={self.commit[:12]}")
        return " ".join(parts)


@dataclass(frozen=True)
class Change:
    status: str
    path: str
    source_path: str | None = None

    def patch_paths(self) -> list[str]:
        paths: list[str] = []
        if self.source_path is not None:
            paths.append(self.source_path)
        paths.append(self.path)
        return paths


class Scanner:
    def __init__(self, repo_root: Path, exact_values: list[bytes]) -> None:
        self.repo_root = repo_root
        self.exact_values = exact_values

    def scan_bytes(self, content: bytes) -> list[Match]:
        matches: dict[tuple[str, str], Match] = {}

        for exact in self.exact_values:
            if exact and exact in content:
                fingerprint = _fingerprint(exact)
                matches[("exact-denylist", fingerprint)] = Match("exact-denylist", fingerprint)

        service_account_match = _detect_service_account_json(content)
        if service_account_match is not None:
            matches[(service_account_match.rule_id, service_account_match.fingerprint)] = service_account_match

        for rule_id, pattern in GENERIC_PATTERNS:
            for match in pattern.finditer(content):
                fingerprint = _fingerprint(match.group(0))
                matches[(rule_id, fingerprint)] = Match(rule_id, fingerprint)

        return sorted(matches.values(), key=lambda item: (item.rule_id, item.fingerprint))


class BaselineResolver:
    def contains(self, path: str, rule_id: str, fingerprint: str) -> bool:
        raise NotImplementedError


class CommitBaseline(BaselineResolver):
    def __init__(self, scanner: Scanner, commit: str) -> None:
        self.scanner = scanner
        self.commit = commit
        self._cache: dict[str, set[tuple[str, str, str]]] = {}

    def contains(self, path: str, rule_id: str, fingerprint: str) -> bool:
        return (path, rule_id, fingerprint) in self._keys_for_path(path)

    def _keys_for_path(self, path: str) -> set[tuple[str, str, str]]:
        if path not in self._cache:
            content = _git_show_file(self.scanner.repo_root, f"{self.commit}:{path}", missing_ok=True)
            keys: set[tuple[str, str, str]] = set()
            if content is not None:
                for match in self.scanner.scan_bytes(content):
                    keys.add((path, match.rule_id, match.fingerprint))
            self._cache[path] = keys
        return self._cache[path]


class NamespaceBaseline(BaselineResolver):
    def __init__(self, scanner: Scanner, refs: list[str]) -> None:
        self.scanner = scanner
        self.refs = refs
        self._cache: dict[str, set[tuple[str, str, str]]] = {}

    def contains(self, path: str, rule_id: str, fingerprint: str) -> bool:
        return (path, rule_id, fingerprint) in self._keys_for_path(path)

    def _keys_for_path(self, path: str) -> set[tuple[str, str, str]]:
        if path not in self._cache:
            keys: set[tuple[str, str, str]] = set()
            for ref in self.refs:
                content = _git_show_file(self.scanner.repo_root, f"{ref}:{path}", missing_ok=True)
                if content is None:
                    continue
                for match in self.scanner.scan_bytes(content):
                    keys.add((path, match.rule_id, match.fingerprint))
            self._cache[path] = keys
        return self._cache[path]


@dataclass
class RangeResult:
    diff_findings: list[Finding]
    blob_findings: list[Finding]

    def exit_code(self) -> int:
        if self.blob_findings:
            return EXIT_BLOB
        if self.diff_findings:
            return EXIT_DIFF
        return 0

    def render(self) -> str:
        return "\n".join(
            finding.render()
            for finding in sorted(self.blob_findings + self.diff_findings, key=_finding_sort_key)
        )


def _finding_sort_key(finding: Finding) -> tuple[str, str, str, str]:
    return (finding.scope, finding.path, finding.commit or "", finding.rule_id)


def _fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode_git_text(value: bytes) -> str:
    try:
        return value.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise GuardError(EXIT_GIT, "git output is not valid UTF-8") from exc


def _decode_git_path(raw: bytes) -> str:
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise GuardError(EXIT_GIT, "git path is not valid UTF-8") from exc


def _display_path(path: str) -> str:
    rendered: list[str] = []
    for char in path:
        code = ord(char)
        if char in PATH_SAFE_ASCII:
            rendered.append(char)
        else:
            rendered.append(_escape_codepoint(code))
    return "".join(rendered)


def _escape_codepoint(code: int) -> str:
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _detect_service_account_json(content: bytes) -> Match | None:
    ordered_matches: list[bytes] = []
    for key in ("type", "private_key", "client_email"):
        match = SERVICE_ACCOUNT_FIELD_PATTERNS[key].search(content)
        if match is None:
            return None
        ordered_matches.append(match.group(0))
    return Match("service-account-json", _fingerprint(b"\0".join(ordered_matches)))


def _run_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise GuardError(EXIT_GIT, "git command failed")
    return result


def _repo_root() -> Path:
    try:
        result = _run_git(Path.cwd(), "rev-parse", "--show-toplevel")
    except GuardError as exc:
        raise GuardError(EXIT_GIT, "not inside a git repository") from exc
    return Path(_decode_git_text(result.stdout).strip())


def _resolve_head_base(repo_root: Path) -> str:
    result = _run_git(repo_root, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}", check=False)
    if result.returncode == 0:
        return _decode_git_text(result.stdout).strip()
    return EMPTY_TREE


def _resolve_commitish(repo_root: Path, value: str) -> str:
    result = _run_git(repo_root, "rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}", check=False)
    if result.returncode != 0:
        raise GuardError(EXIT_GIT, "git commit resolution failed")
    return _decode_git_text(result.stdout).strip()


def _object_id_length(repo_root: Path) -> int:
    result = _run_git(repo_root, "rev-parse", "--show-object-format")
    object_format = _decode_git_text(result.stdout).strip()
    if object_format == "sha1":
        return 40
    if object_format == "sha256":
        return 64
    raise GuardError(EXIT_GIT, "unsupported git object format")


def _validate_ref_token(value: str) -> None:
    if not value or value.startswith("-"):
        raise GuardError(EXIT_USAGE, "pre-push ref is invalid")


def _validate_oid(value: str, *, oid_length: int) -> bool:
    if len(value) != oid_length or re.fullmatch(rf"[0-9a-fA-F]{{{oid_length}}}", value) is None:
        raise GuardError(EXIT_GIT, "pre-push object id is invalid")
    return value == ("0" * oid_length)


def _parse_range_expression(range_expr: str) -> tuple[str, str]:
    if range_expr.count("..") != 1 or "..." in range_expr:
        raise GuardError(EXIT_USAGE, "range must be exactly BASE..HEAD")
    base_ref, head_ref = range_expr.split("..", 1)
    if not base_ref or not head_ref or base_ref.startswith("-") or head_ref.startswith("-"):
        raise GuardError(EXIT_USAGE, "range must be exactly BASE..HEAD")
    return base_ref, head_ref


def _resolve_denylist_path(repo_root: Path) -> tuple[Path, bool]:
    override = os.environ.get("MIM_PUBLIC_RELEASE_DENYLIST_FILE")
    if override:
        return Path(override), True
    return repo_root / DEFAULT_DENYLIST_RELATIVE, False


def _bounded_read_fd(fd: int, limit: int, *, error_code: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise GuardError(error_code, f"{label} exceeds size bound")


def _open_regular_bytes(
    path: Path,
    *,
    error_code: int,
    label: str,
    limit: int,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise
        if exc.errno in {errno.ELOOP, errno.ENXIO, errno.ENOTDIR}:
            raise GuardError(error_code, f"{label} is not a regular readable file") from exc
        raise GuardError(error_code, f"{label} could not be opened") from exc

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise GuardError(error_code, f"{label} is non-regular")
        return _bounded_read_fd(fd, limit, error_code=error_code, label=label), metadata
    finally:
        os.close(fd)


def _read_worktree_bytes(path: Path) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise
        raise GuardError(EXIT_GIT, "worktree path could not be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode):
        return os.fsencode(os.readlink(path))
    if not stat.S_ISREG(metadata.st_mode):
        raise GuardError(EXIT_GIT, "worktree path is non-regular")
    return _open_regular_bytes(
        path,
        error_code=EXIT_GIT,
        label="worktree path",
        limit=MAX_WORKTREE_BYTES,
    )[0]


def _load_exact_values(
    repo_root: Path,
    *,
    require_file: bool,
    require_values: bool,
) -> list[bytes]:
    denylist_path, overridden = _resolve_denylist_path(repo_root)
    try:
        raw, metadata = _open_regular_bytes(
            denylist_path,
            error_code=EXIT_DENYLIST,
            label="exact denylist",
            limit=MAX_DENYLIST_BYTES,
        )
    except FileNotFoundError:
        if require_file or overridden:
            raise GuardError(EXIT_DENYLIST, "exact denylist is unavailable")
        return []

    if metadata.st_uid != os.getuid():
        raise GuardError(EXIT_DENYLIST, "exact denylist must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise GuardError(EXIT_DENYLIST, "exact denylist must have mode 0600")

    values: list[bytes] = []
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith(b"#"):
            continue
        values.append(candidate)
    if require_values and not values:
        raise GuardError(EXIT_DENYLIST, "exact denylist must contain at least one non-comment value")
    return values


def _git_show_file(repo_root: Path, spec: str, *, missing_ok: bool = False) -> bytes | None:
    if missing_ok:
        probe = _run_git(repo_root, "cat-file", "-e", "--end-of-options", spec, check=False)
        if probe.returncode != 0:
            return None
    result = _run_git(repo_root, "show", spec, check=False)
    if result.returncode == 0:
        return result.stdout
    raise GuardError(EXIT_GIT, "git object lookup failed")


def _decode_z_paths(raw: bytes) -> list[str]:
    return [_decode_git_path(item) for item in raw.split(b"\0") if item]


def _list_tracked_paths(repo_root: Path) -> list[str]:
    result = _run_git(repo_root, "ls-files", "-z", "--")
    return _decode_z_paths(result.stdout)


def _list_changed_index_paths(repo_root: Path, base_ref: str) -> list[str]:
    result = _run_git(repo_root, "diff", "--cached", "--name-only", "-z", base_ref, "--")
    return _decode_z_paths(result.stdout)


def _scan_local(repo_root: Path, scanner: Scanner, base_ref: str) -> list[Finding]:
    findings: dict[tuple[str, str, str], Finding] = {}

    for path in _list_tracked_paths(repo_root):
        if path == str(DEFAULT_DENYLIST_RELATIVE):
            continue
        index_content = _git_show_file(repo_root, f":{path}", missing_ok=True)
        if index_content is None:
            continue
        absolute = repo_root / path
        try:
            content = _read_worktree_bytes(absolute)
        except FileNotFoundError:
            continue
        if content == index_content:
            continue
        for match in scanner.scan_bytes(content):
            key = ("local", path, match.fingerprint)
            findings[key] = Finding(match.rule_id, "local", path, match.fingerprint)

    for path in _list_changed_index_paths(repo_root, base_ref):
        if path == str(DEFAULT_DENYLIST_RELATIVE):
            continue
        content = _git_show_file(repo_root, f":{path}", missing_ok=True)
        if content is None:
            continue
        for match in scanner.scan_bytes(content):
            key = ("index", path, match.fingerprint)
            findings[key] = Finding(match.rule_id, "index", path, match.fingerprint)

    return sorted(findings.values(), key=_finding_sort_key)


def _parse_name_status_between(repo_root: Path, parent: str, commit: str) -> list[Change]:
    result = _run_git(
        repo_root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-status",
        "-z",
        "-M",
        "-C",
        "--find-copies-harder",
        parent,
        commit,
        "--",
    )
    tokens = [item for item in result.stdout.split(b"\0") if item]
    changes: list[Change] = []
    index = 0
    while index < len(tokens):
        status_token = _decode_git_text(tokens[index])
        index += 1
        if not status_token:
            raise GuardError(EXIT_GIT, "empty git name-status entry")
        status = status_token[0]
        if status in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise GuardError(EXIT_GIT, "rename/copy status parsing failed")
            source_path = _decode_git_path(tokens[index])
            path = _decode_git_path(tokens[index + 1])
            index += 2
            changes.append(Change(status=status, path=path, source_path=source_path))
        elif status in {"A", "M", "D", "T"}:
            if index >= len(tokens):
                raise GuardError(EXIT_GIT, "name-status parsing failed")
            path = _decode_git_path(tokens[index])
            index += 1
            changes.append(Change(status=status, path=path))
        else:
            raise GuardError(EXIT_GIT, "unsupported git status entry")
    return changes


def _commit_parents(repo_root: Path, commit: str) -> list[str]:
    result = _run_git(repo_root, "rev-list", "--parents", "-n", "1", commit)
    tokens = _decode_git_text(result.stdout).split()
    if not tokens or tokens[0] != commit:
        raise GuardError(EXIT_GIT, "git parent resolution failed")
    return tokens[1:] or [EMPTY_TREE]


def _per_change_patch(repo_root: Path, parent: str, commit: str, change: Change) -> bytes:
    args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        "--find-renames",
        "--find-copies",
        "--find-copies-harder",
        parent,
        commit,
        "--",
        *change.patch_paths(),
    ]
    return _run_git(repo_root, *args).stdout


def _scan_commit_range(
    repo_root: Path,
    scanner: Scanner,
    commits: Iterable[str],
    baseline: BaselineResolver,
) -> RangeResult:
    diff_findings: dict[tuple[str, str, str, str], Finding] = {}
    blob_findings: dict[tuple[str, str, str, str], Finding] = {}

    for commit in commits:
        changed_paths: set[str] = set()
        for parent in _commit_parents(repo_root, commit):
            for change in _parse_name_status_between(repo_root, parent, commit):
                patch = _per_change_patch(repo_root, parent, commit, change)
                for match in scanner.scan_bytes(patch):
                    if baseline.contains(change.path, match.rule_id, match.fingerprint):
                        continue
                    key = (change.path, "outbound-diff", match.rule_id, match.fingerprint)
                    diff_findings[key] = Finding(
                        match.rule_id,
                        "outbound-diff",
                        change.path,
                        match.fingerprint,
                        commit=commit,
                    )
                if change.status != "D":
                    changed_paths.add(change.path)

        for path in changed_paths:
            content = _git_show_file(repo_root, f"{commit}:{path}", missing_ok=False)
            for match in scanner.scan_bytes(content):
                if baseline.contains(path, match.rule_id, match.fingerprint):
                    continue
                key = (path, "outbound-blob", match.rule_id, match.fingerprint)
                blob_findings[key] = Finding(
                    match.rule_id,
                    "outbound-blob",
                    path,
                    match.fingerprint,
                    commit=commit,
                )

    return RangeResult(
        diff_findings=sorted(diff_findings.values(), key=_finding_sort_key),
        blob_findings=sorted(blob_findings.values(), key=_finding_sort_key),
    )


def _resolve_commits(repo_root: Path, range_expr: str) -> list[str]:
    result = _run_git(repo_root, "rev-list", "--reverse", range_expr)
    return [_decode_git_text(line).strip() for line in result.stdout.splitlines() if line.strip()]


def _verify_range(repo_root: Path, scanner: Scanner, range_expr: str, baseline: BaselineResolver) -> RangeResult:
    commits = _resolve_commits(repo_root, range_expr)
    return _scan_commit_range(repo_root, scanner, commits, baseline)


def _remote_namespace_refs(repo_root: Path, remote_name: str) -> list[str]:
    result = _run_git(repo_root, "for-each-ref", "--format=%(refname)", f"refs/remotes/{remote_name}", check=False)
    if result.returncode != 0:
        raise GuardError(EXIT_GIT, "failed to enumerate remote namespace refs")
    refs = [_decode_git_text(line).strip() for line in result.stdout.splitlines() if line.strip()]
    if not refs:
        raise GuardError(EXIT_GIT, "remote namespace refs are unavailable for new ref scanning")
    return refs


def _parse_pre_push_lines(stdin_text: str) -> list[tuple[str, str, str, str]]:
    parsed: list[tuple[str, str, str, str]] = []
    for raw_line in stdin_text.splitlines():
        parts = raw_line.split()
        if len(parts) != 4:
            raise GuardError(EXIT_USAGE, "malformed pre-push input")
        parsed.append((parts[0], parts[1], parts[2], parts[3]))
    return parsed


def _verify_pre_push(repo_root: Path, scanner: Scanner, remote_name: str, stdin_text: str) -> RangeResult:
    diff_findings: list[Finding] = []
    blob_findings: list[Finding] = []
    oid_length = _object_id_length(repo_root)

    for local_ref, local_sha, remote_ref, remote_sha in _parse_pre_push_lines(stdin_text):
        _validate_ref_token(local_ref)
        _validate_ref_token(remote_ref)
        local_is_zero = _validate_oid(local_sha, oid_length=oid_length)
        remote_is_zero = _validate_oid(remote_sha, oid_length=oid_length)
        if local_is_zero:
            continue
        resolved_local_sha = _resolve_commitish(repo_root, local_sha)
        if remote_is_zero:
            refs = _remote_namespace_refs(repo_root, remote_name)
            commits_result = _run_git(repo_root, "rev-list", "--reverse", resolved_local_sha, "--not", *refs)
            commits = [
                _decode_git_text(line).strip()
                for line in commits_result.stdout.splitlines()
                if line.strip()
            ]
            baseline = NamespaceBaseline(scanner, refs)
        else:
            resolved_remote_sha = _resolve_commitish(repo_root, remote_sha)
            commits = _resolve_commits(repo_root, f"{resolved_remote_sha}..{resolved_local_sha}")
            baseline = CommitBaseline(scanner, resolved_remote_sha)

        if not commits:
            continue
        result = _scan_commit_range(repo_root, scanner, commits, baseline)
        diff_findings.extend(result.diff_findings)
        blob_findings.extend(result.blob_findings)

    return RangeResult(diff_findings=diff_findings, blob_findings=blob_findings)


def _print_lines(text: str) -> None:
    if text:
        sys.stderr.write(text)
        sys.stderr.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="public_release_guard.py")
    subparsers = parser.add_subparsers(dest="command")

    verify = subparsers.add_parser("verify")
    mode = verify.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--range", dest="range_expr")
    verify.add_argument("--base-ref")
    verify.add_argument("--require-exact-values", action="store_true")

    pre_push = subparsers.add_parser("pre-push")
    pre_push.add_argument("remote_name")
    pre_push.add_argument("remote_url")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE

    if args.command is None:
        return EXIT_USAGE

    try:
        repo_root = _repo_root()
        if args.command == "verify":
            if args.base_ref and not args.local:
                raise GuardError(EXIT_USAGE, "--base-ref is only valid with --local")
            if args.local:
                base_ref = _resolve_commitish(repo_root, args.base_ref) if args.base_ref else _resolve_head_base(repo_root)
                exact_values = _load_exact_values(
                    repo_root,
                    require_file=False,
                    require_values=args.require_exact_values,
                )
                scanner = Scanner(repo_root, exact_values)
                findings = _scan_local(repo_root, scanner, base_ref)
                if findings:
                    _print_lines("\n".join(finding.render() for finding in findings))
                    return EXIT_LOCAL
                return 0

            exact_values = _load_exact_values(repo_root, require_file=True, require_values=True)
            scanner = Scanner(repo_root, exact_values)
            range_expr = args.range_expr
            assert range_expr is not None
            base_ref, head_ref = _parse_range_expression(range_expr)
            resolved_base_ref = _resolve_commitish(repo_root, base_ref)
            resolved_head_ref = _resolve_commitish(repo_root, head_ref)
            baseline = CommitBaseline(scanner, resolved_base_ref)
            result = _verify_range(repo_root, scanner, f"{resolved_base_ref}..{resolved_head_ref}", baseline)
            if result.exit_code():
                _print_lines(result.render())
            return result.exit_code()

        exact_values = _load_exact_values(repo_root, require_file=True, require_values=True)
        scanner = Scanner(repo_root, exact_values)
        stdin_text = sys.stdin.read()
        result = _verify_pre_push(repo_root, scanner, args.remote_name, stdin_text)
        if result.exit_code():
            _print_lines(result.render())
        return result.exit_code()
    except GuardError as exc:
        sys.stderr.write(f"{exc}\n")
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
