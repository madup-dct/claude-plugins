"""Workload classification from immutable repository snapshots."""

from __future__ import annotations

import json
import re
from ast import Import, ImportFrom
from ast import parse as parse_python
from dataclasses import dataclass
from typing import Any, Mapping

import yaml  # type: ignore[import-untyped]

from mim_control_plane.domain.states import WorkloadKind


class SnapshotValidationError(ValueError):
    """Raised when a snapshot mapping is unsafe or malformed."""


class ManifestValidationError(ValueError):
    """Raised when mim.yaml requests unsupported platform behavior."""


@dataclass(frozen=True, slots=True)
class WorkloadClassification:
    kind: WorkloadKind
    entrypoint: str
    schedule_cron: str | None = None

    def __post_init__(self) -> None:
        if self.kind is WorkloadKind.STREAMLIT:
            if self.entrypoint != "app.py" or self.schedule_cron is not None:
                raise ValueError(
                    "streamlit classification must use app.py with no cron."
                )
            return
        if self.kind is WorkloadKind.NEXTJS:
            if self.entrypoint != "app/page.tsx" or self.schedule_cron is not None:
                raise ValueError(
                    "nextjs classification must use app/page.tsx with no cron."
                )
            return
        if self.kind is WorkloadKind.SCHEDULED_SCRIPT:
            if self.schedule_cron != _SCHEDULE_CRON:
                raise ValueError(
                    "scheduled_script classification must use the approved hourly cron."
                )
            _validate_python_entrypoint(self.entrypoint)
            return
        raise ValueError("unsupported workload classification kind.")


@dataclass(frozen=True, slots=True)
class ClassificationQuestion:
    prompt: str
    choices: tuple[WorkloadKind, ...]
    reason: str


_ALLOWED_MANIFEST_KEYS = frozenset({"entrypoint", "kind", "schedule"})
_FORBIDDEN_MANIFEST_KEYS = frozenset(
    {
        "build_steps",
        "container",
        "cpu",
        "iam",
        "image",
        "memory",
        "project",
        "resources",
        "service_account",
        "vpc",
    }
)
_SCHEDULE_CRON = "0 * * * *"
_SNAPSHOT_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_SNAPSHOT_FILES = 128
MAX_SNAPSHOT_FILE_BYTES = 262_144
MAX_SNAPSHOT_TOTAL_BYTES = 1_048_576
MAX_PATH_LENGTH = 180
MAX_MANIFEST_BYTES = 16_384
MAX_MANIFEST_NODES = 256
MAX_MANIFEST_DEPTH = 12
MAX_ENTRYPOINT_LENGTH = 120
_REQUIREMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+")


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ManifestValidationError("mim.yaml keys must be strings.")
        if key in mapping:
            raise ManifestValidationError("mim.yaml contains duplicate keys.")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def classify_snapshot(
    snapshot: Mapping[str, bytes],
) -> WorkloadClassification | ClassificationQuestion:
    normalized = _normalize_snapshot(snapshot)
    declared = _parse_manifest(normalized.get("mim.yaml"), frozenset(normalized))
    detected = _detect_kinds(normalized)

    if declared is not None:
        declared_kind = declared["kind"]
        if declared_kind is WorkloadKind.SCHEDULED_SCRIPT:
            if detected:
                return _disambiguation(
                    tuple(detected | {WorkloadKind.SCHEDULED_SCRIPT})
                )
            return WorkloadClassification(
                kind=declared_kind,
                entrypoint=declared["entrypoint"],
                schedule_cron=_SCHEDULE_CRON,
            )
        if declared_kind not in detected:
            if not detected and _has_scheduled_python_candidate(normalized):
                return _disambiguation((declared_kind, WorkloadKind.SCHEDULED_SCRIPT))
            if detected:
                return _disambiguation(tuple(detected | {declared_kind}))
            return _disambiguation(
                (
                    WorkloadKind.NEXTJS,
                    WorkloadKind.SCHEDULED_SCRIPT,
                    WorkloadKind.STREAMLIT,
                )
            )

    if len(detected) > 1:
        return _disambiguation(tuple(detected))
    if not detected:
        return _disambiguation(
            (
                WorkloadKind.NEXTJS,
                WorkloadKind.SCHEDULED_SCRIPT,
                WorkloadKind.STREAMLIT,
            )
        )

    kind = next(iter(detected))
    if kind is WorkloadKind.STREAMLIT:
        return WorkloadClassification(kind=kind, entrypoint="app.py")
    if kind is WorkloadKind.NEXTJS:
        return WorkloadClassification(kind=kind, entrypoint="app/page.tsx")
    raise ManifestValidationError("scheduled_script requires mim.yaml.")


def _normalize_snapshot(snapshot: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(snapshot, Mapping):
        raise SnapshotValidationError("Snapshot must be a mapping.")
    normalized: dict[str, bytes] = {}
    total_bytes = 0
    if len(snapshot) > MAX_SNAPSHOT_FILES:
        raise SnapshotValidationError("Snapshot exceeds the maximum file count.")
    for raw_path, raw_bytes in snapshot.items():
        if (
            not isinstance(raw_path, str)
            or _SNAPSHOT_PATH_PATTERN.fullmatch(raw_path) is None
        ):
            raise SnapshotValidationError("Snapshot paths must be safe relative paths.")
        if len(raw_path) > MAX_PATH_LENGTH:
            raise SnapshotValidationError("Snapshot paths exceed the allowed length.")
        if raw_path.startswith("/") or raw_path.startswith("./") or "\\" in raw_path:
            raise SnapshotValidationError("Snapshot paths must be safe relative paths.")
        parts = raw_path.split("/")
        if any(
            part in {"", ".", ".."}
            or (part.startswith(".") and part != ".github")
            or part.startswith("-")
            or any(ord(char) < 0x21 for char in part)
            for part in parts
        ):
            raise SnapshotValidationError("Snapshot paths must be safe relative paths.")
        if not isinstance(raw_bytes, bytes):
            raise SnapshotValidationError("Snapshot contents must be bytes.")
        if len(raw_bytes) > MAX_SNAPSHOT_FILE_BYTES:
            raise SnapshotValidationError("Snapshot file exceeds the allowed size.")
        total_bytes += len(raw_bytes)
        if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
            raise SnapshotValidationError("Snapshot exceeds the allowed total size.")
        normalized[raw_path] = raw_bytes
    return normalized


def _parse_manifest(
    raw_manifest: bytes | None,
    snapshot_paths: frozenset[str],
) -> dict[str, Any] | None:
    if raw_manifest is None:
        return None
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise ManifestValidationError("mim.yaml exceeds the allowed size.")
    try:
        text = raw_manifest.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestValidationError("mim.yaml must be valid UTF-8.") from None
    if "<<" in text or re.search(r"(^|[\s\[{,:])[*&][A-Za-z0-9_-]+", text):
        raise ManifestValidationError("mim.yaml anchors and aliases are not allowed.")
    if "!" in text:
        raise ManifestValidationError("mim.yaml custom tags are not allowed.")
    try:
        documents = list(yaml.load_all(text, Loader=_StrictSafeLoader))
    except (yaml.YAMLError, RecursionError):
        raise ManifestValidationError("mim.yaml is invalid.") from None
    if len(documents) != 1:
        raise ManifestValidationError("mim.yaml must contain exactly one document.")
    document = documents[0]
    if not isinstance(document, dict):
        raise ManifestValidationError("mim.yaml must define a mapping.")
    _validate_manifest_shape(document)
    unknown = set(document) - _ALLOWED_MANIFEST_KEYS
    forbidden = unknown & _FORBIDDEN_MANIFEST_KEYS
    if forbidden:
        raise ManifestValidationError(
            "mim.yaml requests forbidden infrastructure keys."
        )
    if unknown:
        raise ManifestValidationError("mim.yaml contains unsupported keys.")

    kind_value = document.get("kind")
    if kind_value not in {kind.value for kind in WorkloadKind}:
        raise ManifestValidationError("mim.yaml kind is not supported.")
    kind = WorkloadKind(kind_value)

    entrypoint = document.get("entrypoint")
    if kind is WorkloadKind.SCHEDULED_SCRIPT:
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            raise ManifestValidationError(
                "mim.yaml entrypoint must be a non-empty string."
            )
        _validate_python_entrypoint(entrypoint)
        if entrypoint not in snapshot_paths:
            raise ManifestValidationError(
                "mim.yaml entrypoint must exist in the reviewed snapshot."
            )
    elif entrypoint is not None:
        raise ManifestValidationError(
            "mim.yaml entrypoint is only supported for scheduled_script."
        )

    schedule = document.get("schedule")
    if kind is WorkloadKind.SCHEDULED_SCRIPT:
        if schedule != "hourly":
            raise ManifestValidationError(
                "mim.yaml schedule must use the approved hourly form."
            )
    elif schedule is not None:
        raise ManifestValidationError(
            "mim.yaml schedule is only supported for scheduled_script."
        )

    return {
        "entrypoint": entrypoint or _default_entrypoint_for(kind),
        "kind": kind,
        "schedule_cron": (
            _SCHEDULE_CRON if kind is WorkloadKind.SCHEDULED_SCRIPT else None
        ),
    }


def _detect_kinds(snapshot: Mapping[str, bytes]) -> set[WorkloadKind]:
    kinds: set[WorkloadKind] = set()
    if _looks_like_streamlit(snapshot):
        kinds.add(WorkloadKind.STREAMLIT)
    if _looks_like_nextjs(snapshot):
        kinds.add(WorkloadKind.NEXTJS)
    return kinds


def _looks_like_streamlit(snapshot: Mapping[str, bytes]) -> bool:
    requirements = snapshot.get("requirements.txt")
    app = snapshot.get("app.py")
    if requirements is None or app is None:
        return False
    return _has_streamlit_requirement(requirements) and _has_streamlit_import(app)


def _looks_like_nextjs(snapshot: Mapping[str, bytes]) -> bool:
    package_json = snapshot.get("package.json")
    package_lock = snapshot.get("package-lock.json")
    page = snapshot.get("app/page.tsx")
    if package_json is None or package_lock is None or page is None or not page.strip():
        return False
    try:
        parsed = json.loads(package_json.decode("utf-8"))
        lock = json.loads(package_lock.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict) or not isinstance(lock, dict):
        return False
    dependencies = parsed.get("dependencies")
    if not isinstance(dependencies, dict):
        return False
    next_dependency = dependencies.get("next")
    if not isinstance(next_dependency, str) or not next_dependency.strip():
        return False
    return True


def _disambiguation(kinds: tuple[WorkloadKind, ...]) -> ClassificationQuestion:
    choices = tuple(sorted(set(kinds), key=lambda item: item.value))
    return ClassificationQuestion(
        prompt="Disambiguate the workload kind from the reviewed snapshot.",
        choices=choices,
        reason="Conflicting or insufficient workload evidence was detected.",
    )


def _default_entrypoint_for(kind: WorkloadKind) -> str:
    if kind is WorkloadKind.STREAMLIT:
        return "app.py"
    if kind is WorkloadKind.NEXTJS:
        return "app/page.tsx"
    return "main.py"


def _validate_python_entrypoint(entrypoint: str) -> None:
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise ManifestValidationError(
            "mim.yaml entrypoint must be a non-empty string."
        )
    if len(entrypoint) > MAX_ENTRYPOINT_LENGTH:
        raise ManifestValidationError(
            "mim.yaml entrypoint exceeds the allowed length."
        )
    parts = entrypoint.split("/")
    if (
        entrypoint.startswith("/")
        or entrypoint.startswith("./")
        or ".." in parts
        or "\\" in entrypoint
        or any(
            part in {"", ".", ".."}
            or part.startswith(".")
            or part.startswith("-")
            or any(ord(char) < 0x21 for char in part)
            for part in parts
        )
        or not entrypoint.endswith(".py")
    ):
        raise ManifestValidationError(
            "mim.yaml entrypoint must be a safe relative Python path."
        )


def _validate_manifest_shape(
    value: Any,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_MANIFEST_NODES or depth > MAX_MANIFEST_DEPTH:
        raise ManifestValidationError("mim.yaml is too deeply nested.")
    if isinstance(value, dict):
        for inner_key, inner_value in value.items():
            if not isinstance(inner_key, str):
                raise ManifestValidationError("mim.yaml keys must be strings.")
            _validate_manifest_shape(inner_value, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, list):
        for item in value:
            _validate_manifest_shape(item, depth=depth + 1, nodes=nodes)


def _has_streamlit_requirement(raw_requirements: bytes) -> bool:
    try:
        text = raw_requirements.decode("utf-8")
    except UnicodeDecodeError:
        return False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT_NAME_PATTERN.match(line)
        if match is None:
            continue
        normalized = match.group(0).replace("_", "-").replace(".", "-").lower()
        if normalized == "streamlit":
            return True
    return False


def _has_streamlit_import(raw_app: bytes) -> bool:
    try:
        module = parse_python(raw_app.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in module.body:
        if isinstance(node, Import):
            if any(alias.name == "streamlit" for alias in node.names):
                return True
        if isinstance(node, ImportFrom) and node.module == "streamlit":
            return True
    return False


def _has_scheduled_python_candidate(snapshot: Mapping[str, bytes]) -> bool:
    return "main.py" in snapshot
