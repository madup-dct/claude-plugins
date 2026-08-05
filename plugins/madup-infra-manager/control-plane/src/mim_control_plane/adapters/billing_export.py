"""MIM-only BigQuery billing export adapter.

This adapter queries only the central MIM sanitized billing view and projects a
minimal, closed row contract:

- ``invoice_month``: exact ``YYYYMM`` string for the current UTC calendar month
- ``service_description``: non-empty BigQuery service description
- ``currency``: exact ``KRW``
- ``owner_hash`` / ``workload_hash``: either both absent for shared platform
  spend, or both exact 12-character MIM label hashes
- ``measured_cost_krw``: exact non-negative ``INT64`` aggregate in KRW
- ``source_finalized``: exact boolean; raw current-month export rows project
  ``FALSE`` and therefore remain measured-only

Any schema drift, unexpected types, ambiguous label mapping, or out-of-bound
table material is rejected with a redacted deterministic failure.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from mim_control_plane.domain.models import UserId, Workload, WorkloadId
from mim_control_plane.domain.states import UsageConfidence
from mim_control_plane.services.schedules import require_utc_datetime
from mim_control_plane.workers.usage_ingest import BillingCostRecord

bigquery: Any

try:
    from google.cloud import bigquery as _bigquery
except ModuleNotFoundError:
    class _ScalarQueryParameter:
        def __init__(self, name: str, type_: str, value: object) -> None:
            self.name = name
            self.type_ = type_
            self.value = value

    class _QueryJobConfig:
        def __init__(self, *, query_parameters: list[object]) -> None:
            self.query_parameters = query_parameters

    class _BigQueryFallback:
        QueryJobConfig = _QueryJobConfig
        ScalarQueryParameter = _ScalarQueryParameter

    bigquery = _BigQueryFallback()
else:
    bigquery = _bigquery

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_DATASET_ID = "mim_billing_secure"
_VIEW_ID = "mim_usage_costs_v1"
_MANAGED_BY_KEY = "managed-by"
_MANAGED_BY_VALUE = "mim-control-plane"
_OWNER_HASH_KEY = "owner-hash"
_WORKLOAD_HASH_KEY = "workload-hash"
_FAILED = "billing export is invalid."
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DATASET_RE = re.compile(r"^[A-Za-z0-9_]+$")
_VIEW_RE = re.compile(r"^[A-Za-z0-9_]+$")
_INVOICE_MONTH_RE = re.compile(r"^[0-9]{6}$")
_HASH_RE = re.compile(r"^[0-9a-f]{12}$")
_SERVICE_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_EXPECTED_ROW_FIELDS = frozenset(
    {
        "invoice_month",
        "service_description",
        "currency",
        "owner_hash",
        "workload_hash",
        "measured_cost_krw",
        "source_finalized",
    }
)
_QUERY = """\
SELECT
  invoice_month,
  service_description,
  currency,
  owner_hash,
  workload_hash,
  measured_cost_krw,
  source_finalized
FROM `{resource_expression}`
WHERE invoice_month = @invoice_month
ORDER BY invoice_month, service_description, owner_hash, workload_hash
"""

class BillingExportClient(Protocol):
    project: str

    def query(
        self,
        query: str,
        *,
        job_config: bigquery.QueryJobConfig,
    ) -> "_QueryJob": ...


class _QueryJob(Protocol):
    def result(self) -> object: ...


class BillingExportStore(Protocol):
    def list_workloads(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Workload, ...]: ...


@dataclass(frozen=True, slots=True)
class _ResolvedBinding:
    owner_id: UserId | None
    workload_id: WorkloadId | None
    labels: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _NormalizedRow:
    invoice_month: str
    service_category: str
    owner_hash: str | None
    workload_hash: str | None
    measured_cost_krw: int
    source_finalized: bool


@dataclass(frozen=True, slots=True)
class BigQueryBillingExportAdapter:
    client: BillingExportClient
    store: BillingExportStore
    project_id: str = _CENTRAL_PROJECT_ID
    dataset_id: str = _DATASET_ID
    view_id: str = _VIEW_ID

    def __post_init__(self) -> None:
        if not callable(getattr(self.client, "query", None)):
            raise ValueError("billing export client is invalid.")
        if not callable(getattr(self.store, "list_workloads", None)):
            raise ValueError("billing export store is invalid.")
        if type(self.project_id) is not str or self.project_id != _CENTRAL_PROJECT_ID:
            raise ValueError("billing export project is invalid.")
        if type(self.dataset_id) is not str or self.dataset_id != _DATASET_ID:
            raise ValueError("billing export dataset is invalid.")
        if type(self.view_id) is not str or self.view_id != _VIEW_ID:
            raise ValueError("billing export view is invalid.")
        self._require_exact_client_identity()
        self._resource_expression()

    def fetch_cost_records(
        self,
        *,
        now: datetime,
    ) -> tuple[BillingCostRecord, ...]:
        current_now = require_utc_datetime(now, label="billing export")
        invoice_month = current_now.strftime("%Y%m")
        bindings = self._binding_index()
        records: list[BillingCostRecord] = []
        seen_entry_ids: set[str] = set()
        try:
            query_job = self.client.query(
                _QUERY.format(resource_expression=self._resource_expression()),
                job_config=self._job_config(invoice_month=invoice_month),
            )
            raw_rows = query_job.result()
        except Exception:
            raise RuntimeError(_FAILED) from None

        if not isinstance(raw_rows, tuple):
            try:
                rows: tuple[object, ...] = tuple(raw_rows)  # type: ignore[arg-type]
            except Exception:
                raise RuntimeError(_FAILED) from None
        else:
            rows = raw_rows
        for raw_row in rows:
            row = _normalize_row(raw_row, expected_invoice_month=invoice_month)
            binding = _resolve_binding(row=row, bindings=bindings)
            entry_id = _entry_id(
                invoice_month=row.invoice_month,
                service_category=row.service_category,
                owner_hash=row.owner_hash,
                workload_hash=row.workload_hash,
            )
            if entry_id in seen_entry_ids:
                raise RuntimeError(_FAILED)
            seen_entry_ids.add(entry_id)
            finalized_cost_krw = (
                row.measured_cost_krw if row.source_finalized else None
            )
            confidence = (
                UsageConfidence.FINALIZED
                if row.source_finalized
                else UsageConfidence.MEASURED
            )
            records.append(
                BillingCostRecord(
                    entry_id=entry_id,
                    project_id=self.project_id,
                    workload_id=binding.workload_id,
                    owner_id=binding.owner_id,
                    service_category=row.service_category,
                    estimated_cost_krw=row.measured_cost_krw,
                    finalized_cost_krw=finalized_cost_krw,
                    confidence=confidence,
                    collected_at=current_now,
                    labels=binding.labels,
                )
            )
        return tuple(
            sorted(
                records,
                key=lambda record: (
                    record.entry_id,
                    record.service_category,
                    str(record.owner_id) if record.owner_id is not None else "",
                    str(record.workload_id) if record.workload_id is not None else "",
                ),
            )
        )

    def _job_config(
        self,
        *,
        invoice_month: str,
    ) -> bigquery.QueryJobConfig:
        return bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "invoice_month",
                    "STRING",
                    invoice_month,
                ),
            ]
        )

    def _binding_index(self) -> dict[tuple[str, str], tuple[UserId, WorkloadId]]:
        mapping: dict[tuple[str, str], tuple[UserId, WorkloadId]] = {}
        owner_hashes: dict[str, UserId] = {}
        workload_hashes: dict[str, WorkloadId] = {}
        for workload in self.store.list_workloads():
            owner_hash = _stable_hash(str(workload.owner_id))
            workload_hash = _stable_hash(str(workload.id))
            if (
                owner_hash in owner_hashes
                and owner_hashes[owner_hash] != workload.owner_id
            ):
                raise RuntimeError(_FAILED)
            owner_hashes[owner_hash] = workload.owner_id
            if (
                workload_hash in workload_hashes
                and workload_hashes[workload_hash] != workload.id
            ):
                raise RuntimeError(_FAILED)
            workload_hashes[workload_hash] = workload.id
            key = (owner_hash, workload_hash)
            if key in mapping:
                raise RuntimeError(_FAILED)
            mapping[key] = (workload.owner_id, workload.id)
        return mapping

    def _require_exact_client_identity(self) -> None:
        project = getattr(self.client, "project", None)
        if type(project) is not str or project != self.project_id:
            raise ValueError("billing export client project is invalid.")

    def _resource_expression(self) -> str:
        project = _validated_project_id(self.project_id)
        dataset = _validated_dataset_id(self.dataset_id)
        view_id = _validated_view_id(self.view_id)
        return f"{project}.{dataset}.{view_id}"


def _resolve_binding(
    *,
    row: _NormalizedRow,
    bindings: Mapping[tuple[str, str], tuple[UserId, WorkloadId]],
) -> _ResolvedBinding:
    if row.owner_hash is None and row.workload_hash is None:
        return _ResolvedBinding(
            owner_id=None,
            workload_id=None,
            labels=((_MANAGED_BY_KEY, _MANAGED_BY_VALUE),),
        )
    if row.owner_hash is None or row.workload_hash is None:
        raise RuntimeError(_FAILED)
    binding = bindings.get((row.owner_hash, row.workload_hash))
    if binding is None:
        raise RuntimeError(_FAILED)
    owner_id, workload_id = binding
    return _ResolvedBinding(
        owner_id=owner_id,
        workload_id=workload_id,
        labels=(
            (_MANAGED_BY_KEY, _MANAGED_BY_VALUE),
            (_OWNER_HASH_KEY, row.owner_hash),
            (_WORKLOAD_HASH_KEY, row.workload_hash),
        ),
    )


def _normalize_row(
    row: object,
    *,
    expected_invoice_month: str,
) -> _NormalizedRow:
    mapping = _row_mapping(row)
    if frozenset(mapping) != _EXPECTED_ROW_FIELDS:
        raise RuntimeError(_FAILED)
    invoice_month = mapping["invoice_month"]
    service_description = mapping["service_description"]
    currency = mapping["currency"]
    owner_hash = mapping["owner_hash"]
    workload_hash = mapping["workload_hash"]
    measured_cost_krw = mapping["measured_cost_krw"]
    source_finalized = mapping["source_finalized"]
    if (
        type(invoice_month) is not str
        or _INVOICE_MONTH_RE.fullmatch(invoice_month) is None
        or invoice_month != expected_invoice_month
    ):
        raise RuntimeError(_FAILED)
    if type(service_description) is not str or not service_description.strip():
        raise RuntimeError(_FAILED)
    if currency != "KRW":
        raise RuntimeError(_FAILED)
    if owner_hash is not None and (
        type(owner_hash) is not str or _HASH_RE.fullmatch(owner_hash) is None
    ):
        raise RuntimeError(_FAILED)
    if workload_hash is not None and (
        type(workload_hash) is not str or _HASH_RE.fullmatch(workload_hash) is None
    ):
        raise RuntimeError(_FAILED)
    if type(measured_cost_krw) is not int or measured_cost_krw < 0:
        raise RuntimeError(_FAILED)
    if type(source_finalized) is not bool:
        raise RuntimeError(_FAILED)
    return _NormalizedRow(
        invoice_month=invoice_month,
        service_category=_service_category(service_description),
        owner_hash=owner_hash,
        workload_hash=workload_hash,
        measured_cost_krw=measured_cost_krw,
        source_finalized=source_finalized,
    )


def _row_mapping(row: object) -> Mapping[str, object]:
    if isinstance(row, dict):
        return row
    items = getattr(row, "items", None)
    if not callable(items):
        raise RuntimeError(_FAILED)
    raw = dict(items())
    if any(type(key) is not str for key in raw):
        raise RuntimeError(_FAILED)
    return raw


def _validated_project_id(value: str) -> str:
    if _PROJECT_RE.fullmatch(value) is None:
        raise ValueError("billing export view is invalid.")
    return value


def _validated_dataset_id(value: str) -> str:
    if _DATASET_RE.fullmatch(value) is None:
        raise ValueError("billing export view is invalid.")
    return value


def _validated_view_id(value: str) -> str:
    if _VIEW_RE.fullmatch(value) is None:
        raise ValueError("billing export view is invalid.")
    return value


def _service_category(value: str) -> str:
    normalized = _SERVICE_TOKEN_RE.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise RuntimeError(_FAILED)
    return normalized


def _entry_id(
    *,
    invoice_month: str,
    service_category: str,
    owner_hash: str | None,
    workload_hash: str | None,
) -> str:
    digest = hashlib.sha256(
        "\x00".join(
            (
                invoice_month,
                service_category,
                owner_hash or "shared",
                workload_hash or "shared",
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"billing-{digest}"


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


__all__ = ["BigQueryBillingExportAdapter"]
