from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.domain.models import (  # noqa: E402
    RepositoryAdmissionId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (  # noqa: E402
    UsageConfidence,
    WorkloadKind,
    WorkloadState,
)

NOW = datetime(2026, 8, 4, 12, 34, 56, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
DATASET_ID = "mim_billing_secure"
VIEW_ID = "mim_usage_costs_v1"


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _load_module() -> Any:
    module_name = "mim_control_plane.adapters.billing_export"
    if importlib.util.find_spec(module_name) is None:
        raise AssertionError(f"{module_name} must exist.")
    return importlib.import_module(module_name)


def workload(*, workload_id: str, owner_id: str) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("adm-1"),
        name=workload_id,
        kind=WorkloadKind.NEXTJS,
        state=WorkloadState.ACTIVE,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-1",
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
        last_activity_at=NOW - timedelta(hours=1),
        version=1,
    )


class FakeStore:
    def __init__(self, *workloads: Workload) -> None:
        self._workloads = tuple(workloads)
        self.calls: list[UserId | None] = []

    def list_workloads(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Workload, ...]:
        self.calls.append(owner_id)
        if owner_id is None:
            return self._workloads
        return tuple(item for item in self._workloads if item.owner_id == owner_id)


class FakeQueryJob:
    def __init__(
        self,
        *,
        rows: tuple[dict[str, object], ...] = (),
        error: Exception | None = None,
    ) -> None:
        self._rows = rows
        self._error = error

    def result(self) -> tuple[dict[str, object], ...]:
        if self._error is not None:
            raise self._error
        return self._rows


class FakeBigQueryClient:
    def __init__(
        self,
        *,
        project: str = PROJECT_ID,
        rows: tuple[dict[str, object], ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.project = project
        self._rows = rows
        self._error = error
        self.last_query: str | None = None
        self.last_job_config: object | None = None

    def query(self, query: str, *, job_config: object) -> FakeQueryJob:
        self.last_query = query
        self.last_job_config = job_config
        return FakeQueryJob(rows=self._rows, error=self._error)


def _job_parameter_map(job_config: object) -> dict[str, object]:
    parameters = getattr(job_config, "query_parameters", None)
    if type(parameters) is not list:
        raise AssertionError("query_parameters must be a list.")
    mapped: dict[str, object] = {}
    for parameter in parameters:
        name = getattr(parameter, "name", None)
        if type(name) is not str:
            raise AssertionError("query parameter name must be a string.")
        value = getattr(parameter, "value", None)
        if value is None:
            value = getattr(parameter, "_value", None)
        mapped[name] = value
    return mapped


def _entry_digest(
    invoice_month: str,
    service_category: str,
    owner_hash: str | None,
    workload_hash: str | None,
) -> str:
    material = "\x00".join(
        (
            invoice_month,
            service_category,
            owner_hash or "shared",
            workload_hash or "shared",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


class BigQueryBillingExportAdapterTests(unittest.TestCase):
    def test_constructor_rejects_non_central_project_dataset_view_and_client(
        self,
    ) -> None:
        module = _load_module()
        adapter_type = module.BigQueryBillingExportAdapter
        store = FakeStore(workload(workload_id="wrk-1", owner_id="usr-1"))

        with self.assertRaises(ValueError):
            adapter_type(
                client=FakeBigQueryClient(project="other-project"),
                store=store,
            )
        with self.assertRaises(ValueError):
            adapter_type(
                client=FakeBigQueryClient(),
                store=store,
                project_id="other-project",
            )
        with self.assertRaises(ValueError):
            adapter_type(
                client=FakeBigQueryClient(),
                store=store,
                dataset_id="other_dataset",
            )
        with self.assertRaises(ValueError):
            adapter_type(
                client=FakeBigQueryClient(),
                store=store,
                view_id="other_view",
            )

    def test_fetch_cost_records_queries_current_month_only_and_maps_rows(self) -> None:
        module = _load_module()
        adapter_type = module.BigQueryBillingExportAdapter
        store = FakeStore(workload(workload_id="wrk-1", owner_id="usr-1"))
        client = FakeBigQueryClient(
            rows=(
                {
                    "invoice_month": "202608",
                    "service_description": "BigQuery",
                    "currency": "KRW",
                    "owner_hash": None,
                    "workload_hash": None,
                    "measured_cost_krw": 3200,
                    "source_finalized": False,
                },
                {
                    "invoice_month": "202608",
                    "service_description": "Cloud Run",
                    "currency": "KRW",
                    "owner_hash": _stable_hash("usr-1"),
                    "workload_hash": _stable_hash("wrk-1"),
                    "measured_cost_krw": 1500,
                    "source_finalized": False,
                },
            )
        )
        adapter = adapter_type(client=client, store=store)

        records = adapter.fetch_cost_records(now=NOW)
        retry = adapter.fetch_cost_records(now=NOW)

        self.assertEqual(store.calls, [None, None])
        self.assertEqual(len(records), 2)
        self.assertEqual(records, retry)
        self.assertEqual(records[0].project_id, PROJECT_ID)
        self.assertIsNone(records[0].owner_id)
        self.assertIsNone(records[0].workload_id)
        self.assertEqual(records[0].labels, (("managed-by", "mim-control-plane"),))
        self.assertEqual(records[0].service_category, "bigquery")
        self.assertEqual(records[0].estimated_cost_krw, 3200)
        self.assertIsNone(records[0].finalized_cost_krw)
        self.assertEqual(records[0].confidence, UsageConfidence.MEASURED)
        self.assertEqual(
            records[0].entry_id,
            "billing-"
            + _entry_digest(
                "202608",
                "bigquery",
                None,
                None,
            ),
        )
        self.assertEqual(records[1].owner_id, UserId("usr-1"))
        self.assertEqual(records[1].workload_id, WorkloadId("wrk-1"))
        self.assertEqual(
            records[1].labels,
            (
                ("managed-by", "mim-control-plane"),
                ("owner-hash", _stable_hash("usr-1")),
                ("workload-hash", _stable_hash("wrk-1")),
            ),
        )
        self.assertEqual(records[1].service_category, "cloud_run")
        self.assertEqual(
            client.last_query,
            cast(str, client.last_query),
        )
        self.assertEqual(
            client.last_query,
            "SELECT\n"
            "  invoice_month,\n"
            "  service_description,\n"
            "  currency,\n"
            "  owner_hash,\n"
            "  workload_hash,\n"
            "  measured_cost_krw,\n"
            "  source_finalized\n"
            "FROM `mim-prod-123456.mim_billing_secure.mim_usage_costs_v1`\n"
            "WHERE invoice_month = @invoice_month\n"
            "ORDER BY invoice_month, service_description, owner_hash, workload_hash\n"
        )
        self.assertNotIn(
            "gcp_billing_export_resource_v1_",
            cast(str, client.last_query),
        )
        self.assertNotIn("*", cast(str, client.last_query))
        self.assertNotIn("other-project", cast(str, client.last_query))
        params = _job_parameter_map(client.last_job_config)
        self.assertEqual(params, {"invoice_month": "202608"})

    def test_fetch_cost_records_sets_finalized_only_when_source_marks_finalized(
        self,
    ) -> None:
        module = _load_module()
        adapter = module.BigQueryBillingExportAdapter(
            client=FakeBigQueryClient(
                rows=(
                    {
                        "invoice_month": "202608",
                        "service_description": "Cloud Run",
                        "currency": "KRW",
                        "owner_hash": _stable_hash("usr-1"),
                        "workload_hash": _stable_hash("wrk-1"),
                        "measured_cost_krw": 4200,
                        "source_finalized": True,
                    },
                )
            ),
            store=FakeStore(workload(workload_id="wrk-1", owner_id="usr-1")),
        )

        record = adapter.fetch_cost_records(now=NOW)[0]

        self.assertEqual(record.estimated_cost_krw, 4200)
        self.assertEqual(record.finalized_cost_krw, 4200)
        self.assertEqual(record.confidence, UsageConfidence.FINALIZED)

    def test_fetch_cost_records_rejects_unknown_and_partial_hash_bindings(self) -> None:
        module = _load_module()
        adapter_type = module.BigQueryBillingExportAdapter
        store = FakeStore(workload(workload_id="wrk-1", owner_id="usr-1"))
        secret = "sk-live-row-123"

        cases: tuple[dict[str, object], ...] = (
            {
                "invoice_month": "202608",
                "service_description": "Cloud Run",
                "currency": "KRW",
                "owner_hash": _stable_hash("usr-9"),
                "workload_hash": _stable_hash("wrk-9"),
                "measured_cost_krw": 99,
                "source_finalized": False,
            },
            {
                "invoice_month": "202608",
                "service_description": "Cloud Run",
                "currency": "KRW",
                "owner_hash": secret,
                "workload_hash": None,
                "measured_cost_krw": 99,
                "source_finalized": False,
            },
        )

        for row in cases:
            adapter = adapter_type(client=FakeBigQueryClient(rows=(row,)), store=store)
            with self.subTest(row=copy.deepcopy(row)):
                with self.assertRaises(RuntimeError) as raised:
                    adapter.fetch_cost_records(now=NOW)
                self.assertNotIn(secret, str(raised.exception))

    def test_fetch_cost_records_rejects_duplicate_rows_and_hash_collisions(
        self,
    ) -> None:
        module = _load_module()
        adapter_type = module.BigQueryBillingExportAdapter
        row: dict[str, object] = {
            "invoice_month": "202608",
            "service_description": "Cloud Run",
            "currency": "KRW",
            "owner_hash": _stable_hash("usr-1"),
            "workload_hash": _stable_hash("wrk-1"),
            "measured_cost_krw": 100,
            "source_finalized": False,
        }

        adapter = adapter_type(
            client=FakeBigQueryClient(rows=(row, copy.deepcopy(row))),
            store=FakeStore(workload(workload_id="wrk-1", owner_id="usr-1")),
        )
        with self.assertRaises(RuntimeError):
            adapter.fetch_cost_records(now=NOW)

        original = module._stable_hash
        module._stable_hash = lambda _value: "deadbeefcafe"
        try:
            collision_store = FakeStore(
                workload(workload_id="wrk-1", owner_id="usr-1"),
                workload(workload_id="wrk-2", owner_id="usr-2"),
            )
            collision_adapter = adapter_type(
                client=FakeBigQueryClient(
                    rows=(
                        {
                            "invoice_month": "202608",
                            "service_description": "Cloud Run",
                            "currency": "KRW",
                            "owner_hash": "deadbeefcafe",
                            "workload_hash": "deadbeefcafe",
                            "measured_cost_krw": 100,
                            "source_finalized": False,
                        },
                    )
                ),
                store=collision_store,
            )
            with self.assertRaises(RuntimeError):
                collision_adapter.fetch_cost_records(now=NOW)
        finally:
            module._stable_hash = original

    def test_fetch_cost_records_rejects_non_utc_now_unexpected_currency_and_types(
        self,
    ) -> None:
        module = _load_module()
        adapter_type = module.BigQueryBillingExportAdapter
        store = FakeStore(workload(workload_id="wrk-1", owner_id="usr-1"))

        adapter = adapter_type(client=FakeBigQueryClient(), store=store)
        with self.assertRaises(ValueError):
            adapter.fetch_cost_records(now=datetime(2026, 8, 4, 12, 34, 56))

        bad_rows = (
            {
                "invoice_month": "202608",
                "service_description": "Cloud Run",
                "currency": "USD",
                "owner_hash": _stable_hash("usr-1"),
                "workload_hash": _stable_hash("wrk-1"),
                "measured_cost_krw": 10,
                "source_finalized": False,
            },
            {
                "invoice_month": "202608",
                "service_description": "Cloud Run",
                "currency": "KRW",
                "owner_hash": _stable_hash("usr-1"),
                "workload_hash": _stable_hash("wrk-1"),
                "measured_cost_krw": 10.5,
                "source_finalized": False,
            },
        )
        for row in bad_rows:
            failing = adapter_type(client=FakeBigQueryClient(rows=(row,)), store=store)
            with self.subTest(row=copy.deepcopy(row)):
                with self.assertRaises(RuntimeError):
                    failing.fetch_cost_records(now=NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
