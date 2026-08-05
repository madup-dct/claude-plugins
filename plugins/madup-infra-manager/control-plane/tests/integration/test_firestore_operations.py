from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import os
import unittest
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest import mock

try:
    from tests.fakes import FAKE_STARTUP_CONFIG, build_startup_mapping
except ModuleNotFoundError:  # pragma: no cover - unittest discovery path fallback
    from fakes import FAKE_STARTUP_CONFIG, build_startup_mapping

from mim_control_plane.config import Settings
from mim_control_plane.domain.models import (
    ActivityEvent,
    ActivityEventId,
    AppHostnameBinding,
    AppHostnameBindingState,
    AuditEvent,
    AuditEventId,
    DailyUsageAggregate,
    DeploymentPlan,
    DeploymentPlanId,
    LifecycleAction,
    LifecycleActionId,
    MaintenanceJobStatus,
    Operation,
    OperationId,
    OrgCostGuard,
    OriginRequestClaim,
    OriginRequestId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    ActivityOutcome,
    ActivitySurface,
    LifecycleActionKind,
    LifecycleActionState,
    OperationState,
    PlanState,
    RepositoryAdmissionState,
    ScheduleState,
    SecretLifecycleState,
    SecretRotationState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.execution import (
    QueuedDeployTask,
    SecretAttachmentReference,
    TaskConflictError,
)
from mim_control_plane.ports.store import (
    AUTO_DEPLOY_ACTOR_ID,
    IdempotencyConflict,
    InvariantViolation,
    ReplayDetected,
    StoreError,
    VersionConflict,
)
from mim_control_plane.services.app_hostname import workload_hash_suffix

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


class FakeSnapshot:
    def __init__(self, *, document_id: str, data: dict[str, object] | None) -> None:
        self.id = document_id
        self.exists = data is not None
        self._data = deepcopy(data)

    def to_dict(self) -> dict[str, object] | None:
        return deepcopy(self._data)


class FakeQuery:
    def __init__(
        self,
        *,
        client: FakeFirestoreClient,
        collection: str,
        limit_count: int | None = None,
        filters: tuple[tuple[str, str, object], ...] = (),
        order_bys: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._client = client
        self._collection = collection
        self._limit_count = limit_count
        self._filters = filters
        self._order_bys = order_bys

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(
            client=self._client,
            collection=self._collection,
            limit_count=count,
            filters=self._filters,
            order_bys=self._order_bys,
        )

    def where(self, field_name: str, op_string: str, value: object) -> FakeQuery:
        return FakeQuery(
            client=self._client,
            collection=self._collection,
            limit_count=self._limit_count,
            filters=(*self._filters, (field_name, op_string, value)),
            order_bys=self._order_bys,
        )

    def order_by(self, field_name: str, direction: object = "ASCENDING") -> FakeQuery:
        return FakeQuery(
            client=self._client,
            collection=self._collection,
            limit_count=self._limit_count,
            filters=self._filters,
            order_bys=(
                *self._order_bys,
                (field_name, _normalize_direction(direction)),
            ),
        )

    def stream(self) -> tuple[FakeSnapshot, ...]:
        self._client.stream_calls.append((self._collection, self._filters))
        self._client.advanced_query_calls.append(
            (
                self._collection,
                self._filters,
                self._order_bys,
                self._limit_count,
            )
        )
        documents = self._client.documents.get(self._collection, {})
        items = [
            (document_id, data)
            for document_id, data in documents.items()
            if _matches_filters(data, self._filters)
        ]
        for field_name, direction in reversed(self._order_bys):
            items.sort(
                key=lambda item: _order_value(
                    data=item[1],
                    document_id=item[0],
                    field_name=field_name,
                ),
                reverse=direction == "DESCENDING",
            )
        if self._limit_count is not None:
            items = items[: self._limit_count]
        return tuple(
            FakeSnapshot(document_id=document_id, data=data)
            for document_id, data in items
        )


class FakeCollection(FakeQuery):
    def document(self, document_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(
            client=self._client,
            collection=self._collection,
            document_id=document_id,
        )


class FakeDocumentReference:
    def __init__(
        self,
        *,
        client: FakeFirestoreClient,
        collection: str,
        document_id: str,
    ) -> None:
        self.client = client
        self.collection = collection
        self.id = document_id

    def get(self, *, transaction: FakeTransaction | None = None) -> FakeSnapshot:
        if transaction is not None:
            return transaction.get(self)
        data = self.client.documents.get(self.collection, {}).get(self.id)
        return FakeSnapshot(document_id=self.id, data=data)

    def set(self, data: dict[str, object]) -> None:
        collection = self.client.documents.setdefault(self.collection, {})
        collection[self.id] = deepcopy(data)

    def create(self, data: dict[str, object]) -> None:
        collection = self.client.documents.setdefault(self.collection, {})
        if self.id in collection:
            raise RuntimeError("document already exists")
        collection[self.id] = deepcopy(data)


class FakeTransaction:
    def __init__(self, *, client: FakeFirestoreClient) -> None:
        self._client = client
        self._snapshot = deepcopy(client.documents)
        self._writes: list[tuple[str, FakeDocumentReference, dict[str, object]]] = []
        self.log: list[tuple[str, str, str]] = []
        self._write_started = False

    def get(self, reference: FakeDocumentReference) -> FakeSnapshot:
        if self._write_started:
            raise AssertionError("transaction read happened after a write")
        self.log.append(("read", reference.collection, reference.id))
        data = self._snapshot.get(reference.collection, {}).get(reference.id)
        return FakeSnapshot(document_id=reference.id, data=data)

    def set(
        self,
        reference: FakeDocumentReference,
        data: dict[str, object],
    ) -> None:
        self._write_started = True
        self.log.append(("set", reference.collection, reference.id))
        self._writes.append(("set", reference, deepcopy(data)))

    def create(
        self,
        reference: FakeDocumentReference,
        data: dict[str, object],
    ) -> None:
        self._write_started = True
        self.log.append(("create", reference.collection, reference.id))
        self._writes.append(("create", reference, deepcopy(data)))

    def delete(self, reference: FakeDocumentReference) -> None:
        self._write_started = True
        self.log.append(("delete", reference.collection, reference.id))
        self._writes.append(("delete", reference, {}))

    def commit(self) -> None:
        candidate = deepcopy(self._client.documents)
        for operation, reference, data in self._writes:
            collection = candidate.setdefault(reference.collection, {})
            if operation == "create" and reference.id in collection:
                raise RuntimeError("document already exists")
            if operation == "delete":
                collection.pop(reference.id, None)
            else:
                collection[reference.id] = deepcopy(data)
        self._client.documents = candidate


class FakeFirestoreClient:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, dict[str, object]]] = {}
        self.transaction_calls = 0
        self.last_transaction: FakeTransaction | None = None
        self.stream_calls: list[tuple[str, tuple[tuple[str, str, object], ...]]] = []
        self.advanced_query_calls: list[
            tuple[
                str,
                tuple[tuple[str, str, object], ...],
                tuple[tuple[str, str], ...],
                int | None,
            ]
        ] = []

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(client=self, collection=name)

    def run_transaction(self, operation: Any) -> object:
        self.transaction_calls += 1
        transaction = FakeTransaction(client=self)
        self.last_transaction = transaction
        result = operation(transaction)
        transaction.commit()
        return result


def _matches_filters(
    data: dict[str, object],
    filters: tuple[tuple[str, str, object], ...],
) -> bool:
    for field_name, op_string, value in filters:
        if op_string != "==":
            raise AssertionError(f"unsupported fake query operator: {op_string}")
        if data.get(field_name) != value:
            return False
    return True


def _normalize_direction(direction: object) -> str:
    token = str(direction).upper()
    if token.endswith("DESCENDING"):
        return "DESCENDING"
    if token.endswith("ASCENDING"):
        return "ASCENDING"
    raise AssertionError(f"unsupported fake query direction: {direction!r}")


def _order_value(
    *,
    data: dict[str, object],
    document_id: str,
    field_name: str,
) -> object:
    if field_name == "__name__":
        return document_id
    return data[field_name]


def sample_user(*, user_id: str = "usr-1", version: int = 1) -> User:
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=UserRole.USER,
        state=UserState.ACTIVE,
        groups=frozenset({"mim-users", "team-alpha"}),
        identity_synced_at=NOW,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW,
        version=version,
    )


def sample_repository_admission(
    *,
    admission_id: str = "repo-1",
    version: int = 1,
) -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId(admission_id),
        repository_numeric_id=123,
        owner="madupmarketing",
        name="approved-app",
        installation_id=456,
        state=RepositoryAdmissionState.ADMITTED,
        admitted_sha="a" * 40,
        created_at=NOW - timedelta(days=20),
        updated_at=NOW,
        version=version,
    )


def sample_workload(
    *,
    workload_id: str = "wrk-1",
    owner_id: UserId = UserId("usr-1"),
    version: int = 1,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=owner_id,
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name="approved-app",
        kind=WorkloadKind.STREAMLIT,
        state=WorkloadState.ACTIVE,
        source_sha="b" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=10),
        updated_at=NOW,
        last_activity_at=NOW - timedelta(hours=1),
        last_healthy_image_digest="sha256:healthy",
        version=version,
    )


def sample_app_hostname_binding(
    *,
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
    state: AppHostnameBindingState = AppHostnameBindingState.ACTIVE,
    version: int = 1,
) -> AppHostnameBinding:
    suffix = workload_hash_suffix(workload_id)
    return AppHostnameBinding(
        public_host=f"approved-app-{suffix}.madup.app",
        workload_id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        workload_kind=WorkloadKind.STREAMLIT,
        service_resource=(
            "projects/mim-prod-123456/locations/asia-northeast3/"
            f"services/mim-svc-{suffix}"
        ),
        upstream_url=f"https://mim-svc-{suffix}-abcdefg-an.a.run.app",
        upstream_audience=f"https://mim-svc-{suffix}-abcdefg-an.a.run.app",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        version=version,
    )


def sample_plan(
    *, version: int = 1, state: PlanState = PlanState.ISSUED
) -> DeploymentPlan:
    return DeploymentPlan(
        id=DeploymentPlanId("plan-1"),
        actor_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        action="deploy",
        material_hash="plan-hash",
        policy_version="policy-v1",
        state=state,
        expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
        sanitized_summary=(("workload", "approved-app"),),
        version=version,
    )


def sample_operation(
    *,
    operation_id: str = "op-1",
    workload_id: WorkloadId | None = WorkloadId("wrk-1"),
    version: int = 1,
) -> Operation:
    return Operation(
        id=OperationId(operation_id),
        actor_id=UserId("usr-1"),
        workload_id=workload_id,
        action="deploy",
        idempotency_key="idem-1",
        request_hash="request-hash",
        state=OperationState.QUEUED,
        created_at=NOW,
        updated_at=NOW,
        version=version,
    )


def sample_schedule(*, version: int = 1) -> Schedule:
    return Schedule(
        id=ScheduleId("sch-1"),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=ScheduleState.ENABLED,
        created_at=NOW,
        updated_at=NOW,
        version=version,
    )


def sample_secret(*, version: int = 1) -> SecretMetadata:
    return SecretMetadata(
        id=SecretId("sec-1"),
        owner_id=UserId("usr-1"),
        name="slack",
        integration_type="slack_oauth",
        attached_workload_ids=(WorkloadId("wrk-1"),),
        active_version=1,
        rotation_state=SecretRotationState.STABLE,
        lifecycle_state=SecretLifecycleState.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        version=version,
    )


def sample_usage(*, entry_id: str = "use-1") -> UsageEntry:
    return UsageEntry(
        id=UsageEntryId(entry_id),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        service_category="cloud_run",
        estimated_cost_krw=100,
        finalized_cost_krw=None,
        confidence=UsageConfidence.ESTIMATED,
        collected_at=NOW,
    )


def sample_activity(*, event_id: str = "act-1") -> ActivityEvent:
    return ActivityEvent(
        id=ActivityEventId(event_id),
        user_id=UserId("usr-1"),
        surface=ActivitySurface.MCP,
        action="plan_deploy",
        target_ref="wrk-1",
        outcome=ActivityOutcome.SUCCEEDED,
        latency_bucket="lt_1s",
        correlation_id="corr-1",
        occurred_at=NOW,
    )


def sample_audit(*, event_id: str = "aud-1") -> AuditEvent:
    return AuditEvent(
        id=AuditEventId(event_id),
        actor_id=UserId("usr-1"),
        action="deploy",
        target_ref="wrk-1",
        policy_decision="allowed",
        before_ref=None,
        after_ref="op-1",
        correlation_id="corr-1",
        outcome="succeeded",
        occurred_at=NOW,
    )


def sample_daily_aggregate(*, version: int = 1) -> DailyUsageAggregate:
    return DailyUsageAggregate(
        day=date(2026, 8, 4),
        user_id=UserId("usr-1"),
        active_users=1,
        dashboard_visits=2,
        mcp_actions=3,
        deployments=4,
        schedule_executions=5,
        successes=6,
        failures=0,
        policy_denials=0,
        version=version,
        updated_at=NOW,
    )


def sample_lifecycle_action(*, version: int = 1) -> LifecycleAction:
    return LifecycleAction(
        id=LifecycleActionId("life-1"),
        workload_id=WorkloadId("wrk-1"),
        kind=LifecycleActionKind.DELETE_COMPUTE,
        state=LifecycleActionState.PLANNED,
        reason="inactive",
        eligible_at=NOW + timedelta(days=1),
        observed_workload_version=1,
        created_at=NOW,
        updated_at=NOW,
        version=version,
    )


def sample_maintenance_status(
    *,
    job_name: str = "identity-sync",
    run_id: str = "run-1",
    version: int = 1,
) -> MaintenanceJobStatus:
    return MaintenanceJobStatus(
        job_name=job_name,
        run_id=run_id,
        started_at=NOW,
        finished_at=None,
        succeeded_at=None,
        failed_at=None,
        outcome="started",
        summary=(),
        failure_code=None,
        failure_class=None,
        version=version,
    )


class FirestoreStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.from_mapping(build_startup_mapping())

    def adapter_module(self) -> Any:
        return importlib.import_module("mim_control_plane.adapters.firestore_store")

    def store_for(self, client: FakeFirestoreClient) -> Any:
        return self.adapter_module().FirestoreStore(
            settings=self.settings,
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
        )

    def document_id(self, *, kind: str, logical_id: str) -> str:
        document_id_factory = getattr(self.adapter_module(), "_document_id", None)
        if document_id_factory is None:
            self.fail("Firestore document ID derivation is missing")
        return document_id_factory(kind=kind, logical_id=logical_id)

    def test_production_adapter_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("mim_control_plane.adapters.firestore_store")
        )

    def test_constructs_client_with_explicit_default_database_and_metadata_identity(
        self,
    ) -> None:
        module = self.adapter_module()
        credentials = object()
        client = object()
        captured: dict[str, object] = {}

        def client_factory(**kwargs: object) -> object:
            captured.update(kwargs)
            return client

        with mock.patch.object(
            module,
            "_google_auth_compute_engine_credentials_factory",
            return_value=credentials,
        ) as compute_factory:
            store = module.FirestoreStore(
                settings=self.settings,
                client_factory=client_factory,
            )

        compute_factory.assert_called_once_with()
        self.assertEqual(captured["project"], FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"])
        self.assertEqual(captured["database"], "(default)")
        self.assertIs(captured["credentials"], credentials)
        self.assertNotIn("credentials", repr(store).casefold())

    def test_document_ids_are_stable_redacted_and_kind_bound(self) -> None:
        logical_id = "person@madup.com/../../sensitive"

        user_key = self.document_id(kind="user", logical_id=logical_id)
        audit_key = self.document_id(kind="audit_event", logical_id=logical_id)

        self.assertEqual(len(user_key), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in user_key))
        self.assertNotIn("person", user_key)
        self.assertNotEqual(user_key, audit_key)
        self.assertEqual(
            user_key,
            self.document_id(kind="user", logical_id=logical_id),
        )

    def test_round_trips_records_lists_sorted_and_returns_copies(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        user = store.create_user(sample_user(user_id="usr-z"))
        store.create_user(
            dataclasses.replace(
                sample_user(user_id="usr-a"),
                created_at=NOW - timedelta(days=40),
                updated_at=NOW - timedelta(days=40),
                identity_synced_at=NOW - timedelta(days=40),
            )
        )
        workload = store.create_workload(sample_workload())
        binding = store.create_app_hostname_binding(sample_app_hostname_binding())
        schedule = store.create_schedule(sample_schedule())
        secret = store.create_secret_metadata(sample_secret())
        usage = store.append_usage_entry(sample_usage())
        activity = store.append_activity_event(sample_activity())
        aggregate = store.create_daily_usage_aggregate(sample_daily_aggregate())
        audit = store.append_audit_event(sample_audit())
        lifecycle_action = store.create_lifecycle_action(sample_lifecycle_action())
        maintenance = store.record_maintenance_job_started(
            job_name="identity-sync",
            run_id="run-1",
            started_at=NOW,
        )

        self.assertEqual(store.get_user(user.id), user)
        self.assertIsNot(store.get_user(user.id), user)
        self.assertEqual(store.get_workload(workload.id), workload)
        self.assertEqual(store.list_workloads(owner_id=UserId("usr-1")), (workload,))
        self.assertEqual(store.get_app_hostname_binding(binding.public_host), binding)
        self.assertEqual(store.get_schedule(schedule.id), schedule)
        self.assertEqual(store.list_schedules(owner_id=UserId("usr-1")), (schedule,))
        self.assertEqual(store.get_secret_metadata(secret.id), secret)
        self.assertEqual(
            store.list_secret_metadata(owner_id=UserId("usr-1")),
            (secret,),
        )
        self.assertEqual(store.list_usage_entries(owner_id=UserId("usr-1")), (usage,))
        self.assertEqual(
            store.list_activity_events(user_id=UserId("usr-1")),
            (activity,),
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(date(2026, 8, 4), UserId("usr-1")),
            aggregate,
        )
        self.assertEqual(store.list_audit_events(), (audit,))
        self.assertEqual(
            store.get_lifecycle_action(lifecycle_action.id),
            lifecycle_action,
        )
        self.assertEqual(
            store.get_maintenance_job_status("identity-sync"),
            maintenance,
        )

    def test_maintenance_job_status_terminal_write_requires_current_run_id(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        second_store = self.store_for(client)

        first = first_store.record_maintenance_job_started(
            job_name="usage-ingest",
            run_id="run-old",
            started_at=NOW,
        )
        self.assertEqual(first.version, 1)
        current = second_store.record_maintenance_job_started(
            job_name="usage-ingest",
            run_id="run-new",
            started_at=NOW + timedelta(minutes=10),
        )
        self.assertEqual(current.version, 2)

        with self.assertRaises(VersionConflict):
            first_store.record_maintenance_job_terminal(
                job_name="usage-ingest",
                run_id="run-old",
                expected_version=current.version,
                finished_at=NOW + timedelta(minutes=11),
                outcome="failed",
                summary=(("billing_appended_entries", 0),),
                failure_code="runtime_error",
                failure_class="RuntimeError",
            )

    def test_expire_activity_events_removes_only_requested_existing_ids(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        current_user = store.create_user(sample_user())
        first = store.append_activity_event(sample_activity(event_id="act-1"))
        second = store.append_activity_event(
            dataclasses.replace(
                sample_activity(event_id="act-2"),
                occurred_at=NOW + timedelta(minutes=1),
            )
        )

        expired = store.expire_activity_events(
            event_ids=("act-2", "act-missing"),
        )

        self.assertEqual(expired, ("act-2",))
        self.assertEqual(store.list_activity_events(), (first,))
        self.assertEqual(store.get_user(current_user.id), current_user)
        self.assertNotIn(
            str(second.id),
            tuple(str(event.id) for event in store.list_activity_events()),
        )

    def test_reads_legacy_secret_document_without_new_optional_fields(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)

        secret = store.create_secret_metadata(sample_secret())
        secret_key = self.document_id(kind="secret", logical_id=str(secret.id))
        client.documents["secrets"][secret_key].pop("retiring_version")
        client.documents["secrets"][secret_key].pop("retirement_not_before")
        client.documents["secrets"][secret_key].pop("mutation_state")
        client.documents["secrets"][secret_key].pop("mutation_idempotency_key")
        client.documents["secrets"][secret_key].pop("pending_workload_ids")
        client.documents["secrets"][secret_key].pop("pending_payload_sha256")

        loaded = store.get_secret_metadata(secret.id)

        self.assertIsNone(loaded.retiring_version)
        self.assertIsNone(loaded.retirement_not_before)
        saved = store.save_secret_metadata(
            loaded.transition_rotation(
                SecretRotationState.ROTATING,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=loaded.version,
        )
        self.assertEqual(store.get_secret_metadata(secret.id), saved)

    def test_reads_legacy_operation_document_without_result_summary(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)

        operation = store.create_operation_once(sample_operation())
        operation_key = self.document_id(kind="operation", logical_id=str(operation.id))
        client.documents["operations"][operation_key].pop("result_summary")

        loaded = store.get_operation(operation.id)

        self.assertEqual(loaded.result_summary, ())
        saved = store.save_operation(
            loaded.transition(OperationState.BUILDING, at=NOW + timedelta(minutes=1)),
            expected_version=loaded.version,
        )
        self.assertEqual(store.get_operation(operation.id), saved)

    def test_round_trips_operation_result_summary(self) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        second_store = self.store_for(client)

        operation = first_store.create_operation_once(
            sample_operation()
            .transition(
                OperationState.BUILDING,
                at=NOW + timedelta(minutes=1),
            )
            .transition(
                OperationState.DEPLOYING,
                at=NOW + timedelta(minutes=2),
            )
            .transition(
                OperationState.VERIFYING,
                at=NOW + timedelta(minutes=3),
            )
            .transition(
                OperationState.SUCCEEDED,
                at=NOW + timedelta(minutes=4),
            )
            .record_result(
                result_summary=(
                    ("secret_id", "sec-1"),
                    ("mode", "rotate"),
                    ("active_version", "4"),
                    ("rotation_state", "retiring_old_version"),
                    ("retiring_version", "3"),
                    ("attached_workload_ids", "wrk-1"),
                ),
                at=NOW + timedelta(minutes=5),
            )
        )

        self.assertEqual(second_store.get_operation(operation.id), operation)

    def test_round_trips_non_idle_secret_draft_across_reload(self) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        second_store = self.store_for(client)

        draft = first_store.create_secret_metadata(
            SecretMetadata.create_draft(
                id=SecretId("sec-draft"),
                owner_id=UserId("usr-1"),
                name="slack",
                integration_type="slack_oauth",
                attached_workload_ids=(WorkloadId("wrk-1"), WorkloadId("wrk-2")),
                mutation_idempotency_key="secret-idem-draft",
                pending_payload_sha256="a" * 64,
                created_at=NOW,
            )
        )

        loaded = second_store.get_secret_metadata(draft.id)

        self.assertEqual(loaded.mutation_state.value, "creating")
        self.assertEqual(loaded.mutation_idempotency_key, "secret-idem-draft")
        self.assertEqual(
            loaded.pending_workload_ids,
            (WorkloadId("wrk-1"), WorkloadId("wrk-2")),
        )
        self.assertEqual(loaded.pending_payload_sha256, "a" * 64)

    def test_usage_upsert_is_atomic_monotonic_and_replay_safe(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        current = store.append_usage_entry(sample_usage())
        updated = dataclasses.replace(
            current,
            estimated_cost_krw=175,
            finalized_cost_krw=160,
            confidence=UsageConfidence.FINALIZED,
            collected_at=NOW + timedelta(hours=1),
        )

        first = store.upsert_usage_entry_monotonic(
            current=current,
            updated=updated,
        )
        first_log = tuple(client.last_transaction.log)
        replay = store.upsert_usage_entry_monotonic(
            current=current,
            updated=updated,
        )
        replay_log = tuple(client.last_transaction.log)

        self.assertEqual(first, updated)
        self.assertEqual(replay, updated)
        self.assertEqual(store.list_usage_entries(), (updated,))
        self.assertEqual([item[0] for item in first_log], ["read", "set"])
        self.assertEqual([item[0] for item in replay_log], ["read"])

        with self.assertRaises(VersionConflict):
            store.upsert_usage_entry_monotonic(
                current=current,
                updated=dataclasses.replace(
                    current,
                    estimated_cost_krw=180,
                    collected_at=NOW + timedelta(hours=2),
                ),
            )

    def test_save_and_list_operations_validate_versions_and_exact_schema(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        original = store.create_user(sample_user())
        updated = store.save_user(
            original.transition_state(
                UserState.SUSPENDED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        self.assertEqual(updated.state, UserState.SUSPENDED)

        with self.assertRaises(VersionConflict):
            store.save_user(
                original.transition_state(
                    UserState.SUSPENDED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        document_id = next(iter(client.documents["users"]))
        client.documents["users"][document_id]["schema_version"] = True
        with self.assertRaises(StoreError) as context:
            store.get_user(original.id)
        self.assertEqual(str(context.exception), "Firestore store operation failed.")

    def test_consume_plan_create_operation_once_and_recover_uncertain_commit(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        calls = {"count": 0}

        def commit_then_fail(
            supplied_client: FakeFirestoreClient,
            operation: Any,
        ) -> object:
            calls["count"] += 1
            supplied_client.run_transaction(operation)
            raise RuntimeError("commit response lost")

        seed_store = self.store_for(client)
        seed_store.create_user(sample_user())
        seed_store.create_repository_admission(sample_repository_admission())
        seed_store.create_workload(sample_workload())
        plan = seed_store.create_deployment_plan(sample_plan())
        operation = sample_operation()

        store = self.adapter_module().FirestoreStore(
            settings=self.settings,
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=commit_then_fail,
        )

        consumed_plan, created_operation = store.consume_deployment_plan_with_operation(
            plan_id=plan.id,
            actor_id=UserId("usr-1"),
            expected_material_hash="plan-hash",
            expected_action="deploy",
            policy_version="policy-v1",
            consumed_at=NOW + timedelta(minutes=1),
            operation=operation,
        )

        self.assertEqual(consumed_plan.state, PlanState.CONSUMED)
        self.assertEqual(created_operation, operation)
        self.assertGreater(calls["count"], 0)
        replay_plan, replay_operation = store.consume_deployment_plan_with_operation(
            plan_id=plan.id,
            actor_id=UserId("usr-1"),
            expected_material_hash="plan-hash",
            expected_action="deploy",
            policy_version="policy-v1",
            consumed_at=NOW + timedelta(minutes=1),
            operation=operation,
        )
        self.assertEqual(replay_plan, consumed_plan)
        self.assertEqual(replay_operation, created_operation)

        with self.assertRaises(IdempotencyConflict):
            store.create_operation_once(
                dataclasses.replace(
                    operation,
                    id=OperationId("op-2"),
                    request_hash="different-hash",
                )
            )

    def test_consume_schedule_plan_with_operation_is_atomic_and_replay_safe(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        seed = self.store_for(client)
        seed.create_user(sample_user())
        seed.create_repository_admission(sample_repository_admission())
        seed.create_workload(sample_workload())
        plan = seed.create_deployment_plan(
            dataclasses.replace(
                sample_plan(),
                action="create_schedule",
                material_hash="schedule-plan-hash",
                policy_version="mim-schedule-v1",
            )
        )
        schedule = sample_schedule()
        operation = dataclasses.replace(
            sample_operation(),
            action="create_schedule",
            request_hash=plan.material_hash,
        )
        requested_replay_operation = dataclasses.replace(
            operation,
            id=OperationId("op-schedule-replay"),
        )

        consumed_plan, saved_schedule, saved_operation = (
            seed.consume_schedule_plan_with_operation(
                plan_id=plan.id,
                actor_id=UserId("usr-1"),
                expected_material_hash=plan.material_hash,
                expected_action="create_schedule",
                policy_version="mim-schedule-v1",
                consumed_at=NOW + timedelta(minutes=1),
                schedule=schedule,
                operation=operation,
            )
        )
        replay_plan, replay_schedule, replay_operation = (
            seed.consume_schedule_plan_with_operation(
                plan_id=plan.id,
                actor_id=UserId("usr-1"),
                expected_material_hash=plan.material_hash,
                expected_action="create_schedule",
                policy_version="mim-schedule-v1",
                consumed_at=NOW + timedelta(minutes=1),
                schedule=schedule,
                operation=requested_replay_operation,
            )
        )

        self.assertEqual(consumed_plan.state, PlanState.CONSUMED)
        self.assertEqual(saved_schedule, schedule)
        self.assertEqual(saved_operation, operation)
        self.assertEqual(replay_plan, consumed_plan)
        self.assertEqual(replay_schedule, saved_schedule)
        self.assertEqual(replay_operation, saved_operation)

    def test_consume_schedule_plan_with_operation_recovers_uncertain_commit(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        calls = {"count": 0}

        def commit_then_fail(
            supplied_client: FakeFirestoreClient,
            operation: Any,
        ) -> object:
            calls["count"] += 1
            supplied_client.run_transaction(operation)
            raise RuntimeError("commit response lost")

        seed_store = self.store_for(client)
        seed_store.create_user(sample_user())
        seed_store.create_repository_admission(sample_repository_admission())
        seed_store.create_workload(sample_workload())
        plan = seed_store.create_deployment_plan(
            dataclasses.replace(
                sample_plan(),
                action="create_schedule",
                material_hash="schedule-plan-hash",
                policy_version="mim-schedule-v1",
            )
        )
        schedule = sample_schedule()
        operation = dataclasses.replace(
            sample_operation(),
            action="create_schedule",
            request_hash=plan.material_hash,
        )
        store = self.adapter_module().FirestoreStore(
            settings=self.settings,
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=commit_then_fail,
        )

        consumed_plan, saved_schedule, saved_operation = (
            store.consume_schedule_plan_with_operation(
                plan_id=plan.id,
                actor_id=UserId("usr-1"),
                expected_material_hash=plan.material_hash,
                expected_action="create_schedule",
                policy_version="mim-schedule-v1",
                consumed_at=NOW + timedelta(minutes=1),
                schedule=schedule,
                operation=operation,
            )
        )

        self.assertEqual(consumed_plan.state, PlanState.CONSUMED)
        self.assertEqual(saved_schedule, schedule)
        self.assertEqual(saved_operation, operation)
        self.assertGreater(calls["count"], 0)

    def test_github_delivery_commits_source_plan_operation_and_task_together(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        seed = self.store_for(client)
        seed.create_user(sample_user())
        old_admission = seed.create_repository_admission(
            dataclasses.replace(
                sample_repository_admission(admission_id="repo-old"),
                admitted_sha="a" * 40,
            )
        )
        current = seed.create_workload(
            dataclasses.replace(
                sample_workload(),
                repository_admission_id=old_admission.id,
                source_sha=old_admission.admitted_sha,
                auto_deploy_enabled=True,
                auto_deploy_ref="refs/heads/main",
            )
        )
        changed_at = NOW + timedelta(minutes=1)
        admission = dataclasses.replace(
            old_admission,
            id=RepositoryAdmissionId("repo-new"),
            admitted_sha="b" * 40,
            created_at=changed_at,
            updated_at=changed_at,
        )
        workload = current.advance_source(
            repository_admission_id=admission.id,
            source_sha=admission.admitted_sha,
            desired_manifest_hash="manifest-new",
            at=changed_at,
        )
        plan = DeploymentPlan(
            id=DeploymentPlanId("plan-auto"),
            actor_id=AUTO_DEPLOY_ACTOR_ID,
            workload_id=current.id,
            action="deploy",
            material_hash="9" * 64,
            policy_version="mim-deploy-v1",
            state=PlanState.ISSUED,
            expires_at=changed_at + timedelta(minutes=15),
            created_at=changed_at,
            updated_at=changed_at,
        )
        delivery_id = "33333333-3333-3333-3333-333333333333"
        operation = Operation(
            id=OperationId("op-auto"),
            actor_id=AUTO_DEPLOY_ACTOR_ID,
            workload_id=current.id,
            action="deploy",
            idempotency_key=f"github:{delivery_id}",
            request_hash=plan.material_hash,
            state=OperationState.QUEUED,
            created_at=changed_at,
            updated_at=changed_at,
        )
        task = QueuedDeployTask.from_snapshot(
            operation_id=operation.id,
            expected_operation_version=operation.version,
            workload_id=workload.id,
            expected_workload_version=workload.version,
            admission_id=admission.id,
            expected_admission_version=admission.version,
            expected_source_sha=admission.admitted_sha,
            idempotency_key=operation.idempotency_key,
            queued_at=operation.created_at,
            snapshot={"app.py": b"import streamlit\n"},
        )

        first = seed.apply_github_auto_deploy_once(
            delivery_id=delivery_id,
            delivery_hash="3" * 64,
            source_ref="refs/heads/main",
            expected_workload_version=current.version,
            admission=admission,
            workload=workload,
            plan=plan,
            operation=operation,
            task=task,
            consumed_at=changed_at,
        )
        restarted = self.store_for(client)
        replay = restarted.apply_github_auto_deploy_once(
            delivery_id=delivery_id,
            delivery_hash="3" * 64,
            source_ref="refs/heads/main",
            expected_workload_version=current.version,
            admission=admission,
            workload=workload,
            plan=plan,
            operation=operation,
            task=task,
            consumed_at=changed_at + timedelta(minutes=1),
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.operation, first.operation)
        self.assertEqual(restarted.get_workload(current.id), workload)
        self.assertEqual(restarted.get_repository_admission(admission.id), admission)
        self.assertEqual(restarted.get_deploy_task(operation.id), task)
        self.assertEqual(first.plan.state, PlanState.CONSUMED)
        self.assertIn("github_delivery_claims", client.documents)
        operation_key = self.document_id(
            kind="operation",
            logical_id=str(operation.id),
        )
        self.assertEqual(
            client.documents["operations"][operation_key].get("workload_owner_id"),
            "usr-1",
        )
        latest = restarted.get_latest_workload_operation(
            owner_id=UserId("usr-1"),
            workload_id=current.id,
        )
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.id, operation.id)

        with self.assertRaises(ReplayDetected):
            restarted.apply_github_auto_deploy_once(
                delivery_id=delivery_id,
                delivery_hash="4" * 64,
                source_ref="refs/heads/main",
                expected_workload_version=current.version,
                admission=admission,
                workload=workload,
                plan=plan,
                operation=operation,
                task=task,
                consumed_at=changed_at + timedelta(minutes=1),
            )

    def test_fresh_store_reads_all_plain_records_from_persisted_documents(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        user = first_store.create_user(sample_user())
        admission = first_store.create_repository_admission(
            sample_repository_admission()
        )
        workload = first_store.create_workload(sample_workload())
        first_store.create_app_hostname_binding(sample_app_hostname_binding())
        plan = first_store.create_deployment_plan(sample_plan())
        operation = first_store.create_operation_once(sample_operation())
        schedule = first_store.create_schedule(sample_schedule())
        secret = first_store.create_secret_metadata(sample_secret())
        usage = first_store.append_usage_entry(sample_usage())
        activity = first_store.append_activity_event(sample_activity())
        aggregate = first_store.create_daily_usage_aggregate(sample_daily_aggregate())
        audit = first_store.append_audit_event(sample_audit())
        action = first_store.create_lifecycle_action(sample_lifecycle_action())

        second_store = self.store_for(client)

        self.assertEqual(second_store.get_user(user.id), user)
        self.assertEqual(
            second_store.get_repository_admission(admission.id),
            admission,
        )
        self.assertEqual(second_store.get_workload(workload.id), workload)
        self.assertEqual(second_store.get_deployment_plan(plan.id), plan)
        self.assertEqual(second_store.get_operation(operation.id), operation)
        self.assertEqual(second_store.get_schedule(schedule.id), schedule)
        self.assertEqual(second_store.get_secret_metadata(secret.id), secret)
        self.assertEqual(
            second_store.get_daily_usage_aggregate(
                aggregate.day,
                aggregate.user_id,
            ),
            aggregate,
        )
        self.assertEqual(second_store.get_lifecycle_action(action.id), action)
        self.assertEqual(
            second_store.list_workloads(owner_id=UserId("usr-1")),
            (workload,),
        )
        self.assertEqual(
            second_store.list_schedules(owner_id=UserId("usr-1")),
            (schedule,),
        )
        self.assertEqual(
            second_store.list_secret_metadata(owner_id=UserId("usr-1")),
            (secret,),
        )
        self.assertEqual(
            second_store.list_usage_entries(owner_id=UserId("usr-1")),
            (usage,),
        )
        self.assertEqual(
            second_store.list_activity_events(user_id=UserId("usr-1")),
            (activity,),
        )
        self.assertEqual(second_store.list_audit_events(), (audit,))

    def test_phase_two_replay_and_claim_survive_restart(self) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        claim = OriginRequestClaim(
            request_id=OriginRequestId("req-1"),
            body_hash="body-hash",
            claimed_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        original_operation = first_store.create_operation_once(sample_operation())
        first_store.claim_origin_request(claim)

        second_store = self.store_for(client)

        replay = second_store.create_operation_once(
            dataclasses.replace(
                sample_operation(operation_id="op-retry"),
                request_hash=original_operation.request_hash,
            )
        )
        self.assertEqual(replay, original_operation)
        with self.assertRaises(ReplayDetected):
            second_store.claim_origin_request(claim)

    def test_phase_two_schedule_lease_and_claim_remain_process_local(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_user(sample_user())
        store.create_repository_admission(sample_repository_admission())
        store.create_workload(sample_workload())
        schedule = store.create_schedule(sample_schedule())

        leased = store.acquire_schedule_lease(
            schedule.id,
            expected_version=1,
            lease_token="lease-1",
            lease_expires_at=NOW + timedelta(minutes=30),
            now=NOW + timedelta(minutes=1),
        )
        completed = store.complete_schedule_run(
            schedule.id,
            expected_version=2,
            lease_token="lease-1",
            succeeded=False,
            completed_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(leased.lease_token, "lease-1")
        self.assertEqual(completed.consecutive_failures, 1)
        self.assertIsNone(completed.lease_token)

        with self.assertRaises(InvariantViolation):
            store.complete_schedule_run(
                schedule.id,
                expected_version=3,
                lease_token="lease-wrong",
                succeeded=True,
                completed_at=NOW + timedelta(minutes=3),
            )

        claim = OriginRequestClaim(
            request_id=OriginRequestId("req-1"),
            body_hash="body-hash",
            claimed_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        store.claim_origin_request(claim)
        with self.assertRaises(ReplayDetected):
            store.claim_origin_request(claim)

    def test_list_usage_entries_owner_scope_uses_firestore_query_and_skips_full_scan(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.append_usage_entry(sample_usage(entry_id="use-1"))
        store.append_usage_entry(
            dataclasses.replace(
                sample_usage(entry_id="use-2"),
                owner_id=UserId("usr-2"),
            )
        )
        store.append_usage_entry(
            dataclasses.replace(
                sample_usage(entry_id="use-3"),
                owner_id=None,
            )
        )
        client.stream_calls.clear()

        scoped = store.list_usage_entries(owner_id=UserId("usr-1"))

        self.assertEqual([str(entry.id) for entry in scoped], ["use-1"])
        self.assertEqual(
            client.stream_calls,
            [("usage_entries", (("owner_id", "==", "usr-1"),))],
        )

    def test_owner_scoped_record_lists_use_firestore_query_and_skip_full_scan(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_workload(
            sample_workload(workload_id="wrk-1", owner_id=UserId("usr-1"))
        )
        store.create_workload(
            sample_workload(workload_id="wrk-2", owner_id=UserId("usr-2"))
        )
        store.create_schedule(sample_schedule(version=1))
        store.create_schedule(
            dataclasses.replace(
                sample_schedule(version=1),
                id=ScheduleId("sch-2"),
                owner_id=UserId("usr-2"),
                workload_id=WorkloadId("wrk-2"),
            )
        )
        store.create_secret_metadata(sample_secret(version=1))
        store.create_secret_metadata(
            dataclasses.replace(
                sample_secret(version=1),
                id=SecretId("sec-2"),
                owner_id=UserId("usr-2"),
                attached_workload_ids=(WorkloadId("wrk-2"),),
            )
        )
        client.stream_calls.clear()

        workloads = store.list_workloads(owner_id=UserId("usr-1"))
        schedules = store.list_schedules(owner_id=UserId("usr-1"))
        secrets = store.list_secret_metadata(owner_id=UserId("usr-1"))

        self.assertEqual([str(workload.id) for workload in workloads], ["wrk-1"])
        self.assertEqual([str(schedule.id) for schedule in schedules], ["sch-1"])
        self.assertEqual([str(secret.id) for secret in secrets], ["sec-1"])
        self.assertEqual(
            client.stream_calls,
            [
                ("workloads", (("owner_id", "==", "usr-1"),)),
                ("schedules", (("owner_id", "==", "usr-1"),)),
                ("secrets", (("owner_id", "==", "usr-1"),)),
            ],
        )

    def test_owner_scoped_record_lists_fail_closed_on_firestore_query_error(
        self,
    ) -> None:
        store = self.store_for(FakeFirestoreClient())
        broken_query = mock.Mock()
        broken_query.stream.side_effect = RuntimeError("boom")
        broken_collection = mock.Mock()
        broken_collection.where.return_value = broken_query

        with mock.patch.object(store, "_collection", return_value=broken_collection):
            with self.assertRaises(StoreError):
                store.list_workloads(owner_id=UserId("usr-1"))
            with self.assertRaises(StoreError):
                store.list_schedules(owner_id=UserId("usr-1"))
            with self.assertRaises(StoreError):
                store.list_secret_metadata(owner_id=UserId("usr-1"))

    def test_latest_workload_operation_uses_scoped_firestore_query_and_stable_ordering(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_user(sample_user())
        store.create_user(sample_user(user_id="usr-2"))
        store.create_repository_admission(sample_repository_admission())
        store.create_workload(sample_workload(workload_id="wrk-1"))
        store.create_workload(
            sample_workload(workload_id="wrk-2", owner_id=UserId("usr-2"))
        )
        store.create_operation_once(
            dataclasses.replace(
                sample_operation(operation_id="op-1"),
                updated_at=NOW - timedelta(minutes=2),
                created_at=NOW - timedelta(minutes=2),
                idempotency_key="idem-op-1",
                request_hash="request-op-1",
            )
        )
        store.create_operation_once(
            dataclasses.replace(
                sample_operation(operation_id="op-7"),
                actor_id=AUTO_DEPLOY_ACTOR_ID,
                created_at=NOW - timedelta(minutes=1),
                updated_at=NOW,
                idempotency_key="idem-op-7",
                request_hash="request-op-7",
                state=OperationState.FAILED,
                sanitized_failure="deploy_failed",
            )
        )
        store.create_operation_once(
            dataclasses.replace(
                sample_operation(operation_id="op-9"),
                actor_id=UserId("adm-1"),
                created_at=NOW - timedelta(minutes=2),
                updated_at=NOW,
                idempotency_key="idem-op-9",
                request_hash="request-op-9",
                state=OperationState.QUARANTINED,
                sanitized_failure="deploy_denied",
            )
        )
        store.create_operation_once(
            dataclasses.replace(
                sample_operation(operation_id="op-8"),
                actor_id=AUTO_DEPLOY_ACTOR_ID,
                created_at=NOW - timedelta(minutes=1),
                updated_at=NOW,
                idempotency_key="idem-op-8",
                request_hash="request-op-8",
                state=OperationState.SUCCEEDED,
            )
        )
        store.create_operation_once(
            dataclasses.replace(
                sample_operation(operation_id="op-other"),
                workload_id=WorkloadId("wrk-2"),
                updated_at=NOW,
                created_at=NOW,
                idempotency_key="idem-op-other",
                request_hash="request-op-other",
            )
        )
        client.stream_calls.clear()
        client.advanced_query_calls.clear()

        latest = store.get_latest_workload_operation(
            owner_id=UserId("usr-1"),
            workload_id=WorkloadId("wrk-1"),
        )

        assert latest is not None
        self.assertEqual(str(latest.id), "op-8")
        self.assertEqual(
            client.stream_calls,
            [
                (
                    "operations",
                    (
                        ("workload_owner_id", "==", "usr-1"),
                        ("workload_id", "==", "wrk-1"),
                    ),
                )
            ],
        )
        self.assertEqual(
            client.advanced_query_calls,
            [
                (
                    "operations",
                    (
                        ("workload_owner_id", "==", "usr-1"),
                        ("workload_id", "==", "wrk-1"),
                    ),
                    (
                        ("updated_at", "DESCENDING"),
                        ("created_at", "DESCENDING"),
                        ("id", "DESCENDING"),
                    ),
                    1,
                )
            ],
        )

    def test_latest_workload_operation_fail_closed_on_firestore_query_error(
        self,
    ) -> None:
        store = self.store_for(FakeFirestoreClient())
        broken_query = mock.Mock()
        broken_query.where.return_value = broken_query
        broken_query.order_by.return_value = broken_query
        broken_query.limit.return_value = broken_query
        broken_query.stream.side_effect = RuntimeError("boom")
        broken_collection = mock.Mock()
        broken_collection.where.return_value = broken_query

        with mock.patch.object(store, "_collection", return_value=broken_collection):
            with self.assertRaises(StoreError):
                store.get_latest_workload_operation(
                    owner_id=UserId("usr-1"),
                    workload_id=WorkloadId("wrk-1"),
                )

    def test_latest_workload_operation_rejects_mismatched_query_result(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_user(sample_user())
        store.create_user(sample_user(user_id="usr-2"))
        store.create_repository_admission(sample_repository_admission())
        store.create_workload(sample_workload(workload_id="wrk-1"))
        store.create_workload(
            sample_workload(workload_id="wrk-2", owner_id=UserId("usr-2"))
        )
        other = store.create_operation_once(
            dataclasses.replace(
                sample_operation(operation_id="op-other"),
                workload_id=WorkloadId("wrk-2"),
                idempotency_key="idem-other",
                request_hash="request-other",
            )
        )
        other_key = self.document_id(kind="operation", logical_id=str(other.id))
        mismatched_snapshot = FakeSnapshot(
            document_id=other_key,
            data=client.documents["operations"][other_key],
        )
        broken_query = mock.Mock()
        broken_query.where.return_value = broken_query
        broken_query.order_by.return_value = broken_query
        broken_query.limit.return_value = broken_query
        broken_query.stream.return_value = (mismatched_snapshot,)
        broken_collection = mock.Mock()
        broken_collection.where.return_value = broken_query

        with mock.patch.object(store, "_collection", return_value=broken_collection):
            with self.assertRaises(StoreError):
                store.get_latest_workload_operation(
                    owner_id=UserId("usr-1"),
                    workload_id=WorkloadId("wrk-1"),
                )

    def test_unscoped_record_lists_preserve_full_collection_behavior(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_workload(
            sample_workload(workload_id="wrk-1", owner_id=UserId("usr-1"))
        )
        store.create_workload(
            sample_workload(workload_id="wrk-2", owner_id=UserId("usr-2"))
        )
        store.create_schedule(sample_schedule(version=1))
        store.create_schedule(
            dataclasses.replace(
                sample_schedule(version=1),
                id=ScheduleId("sch-2"),
                owner_id=UserId("usr-2"),
                workload_id=WorkloadId("wrk-2"),
            )
        )
        store.create_secret_metadata(sample_secret(version=1))
        store.create_secret_metadata(
            dataclasses.replace(
                sample_secret(version=1),
                id=SecretId("sec-2"),
                owner_id=UserId("usr-2"),
                attached_workload_ids=(WorkloadId("wrk-2"),),
            )
        )
        client.stream_calls.clear()

        workloads = store.list_workloads()
        schedules = store.list_schedules()
        secrets = store.list_secret_metadata()

        self.assertEqual(
            [str(workload.id) for workload in workloads],
            ["wrk-1", "wrk-2"],
        )
        self.assertEqual(
            [str(schedule.id) for schedule in schedules],
            ["sch-1", "sch-2"],
        )
        self.assertEqual([str(secret.id) for secret in secrets], ["sec-1", "sec-2"])
        self.assertEqual(
            client.stream_calls,
            [
                ("workloads", ()),
                ("schedules", ()),
                ("secrets", ()),
            ],
        )

    def test_org_cost_guard_round_trips_and_versions_across_restart(self) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        created = first_store.create_org_cost_guard(
            OrgCostGuard(
                evaluated_at=NOW,
                latest_usage_collected_at=NOW - timedelta(minutes=5),
                emergency_stop=False,
                org_policy_cost_krw=321,
            )
        )
        updated = first_store.save_org_cost_guard(
            OrgCostGuard(
                evaluated_at=NOW + timedelta(hours=1),
                latest_usage_collected_at=NOW + timedelta(minutes=30),
                emergency_stop=True,
                org_policy_cost_krw=12_345,
                version=2,
            ),
            expected_version=created.version,
        )

        second_store = self.store_for(client)

        self.assertEqual(created.version, 1)
        self.assertEqual(updated.version, 2)
        self.assertEqual(second_store.get_org_cost_guard(), updated)

    def test_create_deploy_task_once_and_get_survive_restart(self) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        task = QueuedDeployTask.from_snapshot(
            operation_id=OperationId("op-deploy-1"),
            expected_operation_version=2,
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=3,
            admission_id=RepositoryAdmissionId("repo-1"),
            expected_admission_version=4,
            expected_source_sha="a" * 40,
            idempotency_key="deploy-idem-1",
            queued_at=NOW,
            snapshot={
                "app/page.tsx": b"export default function Page() {}",
                "package.json": b'{"name":"demo"}',
            },
            secret_attachments=(
                SecretAttachmentReference(
                    secret_id="sec-1",
                    secret_version=1,
                    metadata_version=2,
                ),
            ),
        )

        stored, created = first_store.create_deploy_task_once(task)
        self.assertTrue(created)
        self.assertEqual(stored, task)

        second_store = self.store_for(client)
        loaded = second_store.get_deploy_task(task.operation_id)
        self.assertEqual(loaded, task)

        replay, replay_created = second_store.create_deploy_task_once(task)
        self.assertFalse(replay_created)
        self.assertEqual(replay, task)

        with self.assertRaises(TaskConflictError):
            second_store.create_deploy_task_once(
                QueuedDeployTask.from_snapshot(
                    operation_id=task.operation_id,
                    expected_operation_version=task.expected_operation_version,
                    workload_id=task.workload_id,
                    expected_workload_version=task.expected_workload_version,
                    admission_id=task.admission_id,
                    expected_admission_version=task.expected_admission_version,
                    expected_source_sha=task.expected_source_sha,
                    idempotency_key=task.idempotency_key,
                    queued_at=task.queued_at,
                    snapshot={"app/page.tsx": b"different"},
                )
            )

    def test_deploy_task_persists_only_snapshot_attestation_metadata(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        secret_value = "ghp_example_private_token"
        snapshot = {
            ".env": f"TOKEN={secret_value}\n".encode("utf-8"),
            "app/page.tsx": b"export default function Page() { return null; }\n",
        }
        task = QueuedDeployTask.from_snapshot(
            operation_id=OperationId("op-deploy-attested"),
            expected_operation_version=2,
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=3,
            admission_id=RepositoryAdmissionId("repo-1"),
            expected_admission_version=4,
            expected_source_sha="9" * 40,
            idempotency_key="deploy-idem-attested",
            queued_at=NOW,
            snapshot=snapshot,
        )

        stored, created = store.create_deploy_task_once(task)

        self.assertTrue(created)
        self.assertEqual(stored, task)
        self.assertEqual(task.expected_snapshot_file_count, 2)
        self.assertEqual(
            task.expected_snapshot_byte_count,
            sum(len(content) for content in snapshot.values()),
        )
        self.assertNotIn(".env", repr(task))
        self.assertNotIn(secret_value, repr(task))

        material_hash = task.material_hash
        metadata_id = self.document_id(kind="deploy_task", logical_id=material_hash)
        metadata = client.documents["deploy_tasks"][metadata_id]
        self.assertEqual(
            metadata,
            {
                "schema_version": 1,
                "operation_id": str(task.operation_id),
                "expected_operation_version": task.expected_operation_version,
                "workload_id": str(task.workload_id),
                "expected_workload_version": task.expected_workload_version,
                "admission_id": str(task.admission_id),
                "expected_admission_version": task.expected_admission_version,
                "expected_source_sha": task.expected_source_sha,
                "idempotency_key": task.idempotency_key,
                "queued_at": task.queued_at.isoformat(),
                "secret_attachments": [],
                "material_hash": material_hash,
                "expected_snapshot_digest": task.expected_snapshot_digest,
                "expected_snapshot_file_count": task.expected_snapshot_file_count,
                "expected_snapshot_byte_count": task.expected_snapshot_byte_count,
            },
        )
        self.assertNotIn("deploy_task_chunks", client.documents)
        serialized = json.dumps(client.documents, sort_keys=True, default=str)
        self.assertNotIn(secret_value, serialized)
        self.assertNotIn("TOKEN=", serialized)
        self.assertNotIn("c2VjcmV0", serialized)

    def test_deploy_task_rejects_legacy_raw_snapshot_document_schema(self) -> None:
        client = FakeFirestoreClient()
        material_hash = "a" * 64
        operation_id = "op-legacy-task"
        idempotency_key = "legacy-idem"
        metadata_id = self.document_id(kind="deploy_task", logical_id=material_hash)
        operation_index_id = self.document_id(
            kind="deploy_task_operation_index",
            logical_id=operation_id,
        )
        idempotency_index_id = self.document_id(
            kind="deploy_task_idempotency_index",
            logical_id=idempotency_key,
        )
        client.documents["deploy_tasks"] = {
            metadata_id: {
                "schema_version": 1,
                "operation_id": operation_id,
                "expected_operation_version": 1,
                "workload_id": "wrk-1",
                "expected_workload_version": 1,
                "admission_id": "repo-1",
                "expected_admission_version": 1,
                "expected_source_sha": "b" * 40,
                "idempotency_key": idempotency_key,
                "queued_at": NOW.isoformat(),
                "secret_attachments": [],
                "snapshot": [["app.py", "cHJpbnQoJ29rJykK"]],
            }
        }
        client.documents["deploy_task_operation_index"] = {
            operation_index_id: {"schema_version": 1, "material_hash": material_hash}
        }
        client.documents["deploy_task_idempotency_index"] = {
            idempotency_index_id: {"schema_version": 1, "material_hash": material_hash}
        }

        with self.assertRaises(StoreError):
            self.store_for(client).get_deploy_task(OperationId(operation_id))

    def test_deploy_task_recovery_after_commit_response_loss(self) -> None:
        client = FakeFirestoreClient()
        calls = {"count": 0}

        def commit_then_fail(
            supplied_client: FakeFirestoreClient,
            operation: Any,
        ) -> object:
            calls["count"] += 1
            supplied_client.run_transaction(operation)
            raise RuntimeError("commit response lost")

        task = QueuedDeployTask.from_snapshot(
            operation_id=OperationId("op-deploy-recover"),
            expected_operation_version=2,
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=3,
            admission_id=RepositoryAdmissionId("repo-1"),
            expected_admission_version=4,
            expected_source_sha="b" * 40,
            idempotency_key="deploy-idem-recover",
            queued_at=NOW,
            snapshot={"app/page.tsx": b"ok"},
        )
        store = self.adapter_module().FirestoreStore(
            settings=self.settings,
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=commit_then_fail,
        )

        recovered, created = store.create_deploy_task_once(task)

        self.assertEqual(recovered, task)
        self.assertFalse(created)
        self.assertGreater(calls["count"], 0)
        self.assertEqual(
            self.store_for(client).get_deploy_task(task.operation_id),
            task,
        )

    def test_deploy_task_detects_missing_or_mismatched_indexes(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        task = QueuedDeployTask.from_snapshot(
            operation_id=OperationId("op-deploy-corrupt"),
            expected_operation_version=2,
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=3,
            admission_id=RepositoryAdmissionId("repo-1"),
            expected_admission_version=4,
            expected_source_sha="c" * 40,
            idempotency_key="deploy-idem-corrupt",
            queued_at=NOW,
            snapshot={"app/page.tsx": b"ok"},
        )
        stored, created = store.create_deploy_task_once(task)
        self.assertTrue(created)
        self.assertEqual(stored, task)

        operation_index_id = self.document_id(
            kind="deploy_task_operation_index",
            logical_id=str(task.operation_id),
        )
        idempotency_index_id = self.document_id(
            kind="deploy_task_idempotency_index",
            logical_id=task.idempotency_key,
        )

        client.documents["deploy_task_idempotency_index"].pop(idempotency_index_id)
        with self.assertRaises(StoreError):
            self.store_for(client).get_deploy_task(task.operation_id)
        with self.assertRaises(InvariantViolation):
            self.store_for(client).create_deploy_task_once(task)

        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_deploy_task_once(task)
        client.documents["deploy_task_idempotency_index"][idempotency_index_id][
            "material_hash"
        ] = "0" * 64
        with self.assertRaises(StoreError):
            self.store_for(client).get_deploy_task(task.operation_id)
        with self.assertRaises(InvariantViolation):
            self.store_for(client).create_deploy_task_once(task)

        client.documents["deploy_task_operation_index"][operation_index_id][
            "material_hash"
        ] = "f" * 64
        with self.assertRaises(InvariantViolation):
            self.store_for(client).create_deploy_task_once(task)

    def test_deploy_task_rejects_snapshot_size_limits(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)

        too_many_files = {f"file-{index}.txt": b"x" for index in range(129)}
        with self.assertRaises(ValueError):
            store.create_deploy_task_once(
                QueuedDeployTask.from_snapshot(
                    operation_id=OperationId("op-too-many"),
                    expected_operation_version=1,
                    workload_id=WorkloadId("wrk-1"),
                    expected_workload_version=1,
                    admission_id=RepositoryAdmissionId("repo-1"),
                    expected_admission_version=1,
                    expected_source_sha="e" * 40,
                    idempotency_key="idem-too-many",
                    queued_at=NOW,
                    snapshot=too_many_files,
                )
            )

        with self.assertRaises(ValueError):
            store.create_deploy_task_once(
                QueuedDeployTask.from_snapshot(
                    operation_id=OperationId("op-too-large-file"),
                    expected_operation_version=1,
                    workload_id=WorkloadId("wrk-1"),
                    expected_workload_version=1,
                    admission_id=RepositoryAdmissionId("repo-1"),
                    expected_admission_version=1,
                    expected_source_sha="f" * 40,
                    idempotency_key="idem-too-large-file",
                    queued_at=NOW,
                    snapshot={"big.bin": b"x" * 262_145},
                )
            )

        snapshot = {f"file-{index}.bin": b"x" * 262_144 for index in range(4)}
        snapshot["overflow.bin"] = b"x"
        with self.assertRaises(ValueError):
            store.create_deploy_task_once(
                QueuedDeployTask.from_snapshot(
                    operation_id=OperationId("op-too-large-total"),
                    expected_operation_version=1,
                    workload_id=WorkloadId("wrk-1"),
                    expected_workload_version=1,
                    admission_id=RepositoryAdmissionId("repo-1"),
                    expected_admission_version=1,
                    expected_source_sha="1" * 40,
                    idempotency_key="idem-too-large-total",
                    queued_at=NOW,
                    snapshot=snapshot,
                )
            )

    def test_deploy_task_rejects_corrupt_attestation_metadata(self) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        task = QueuedDeployTask.from_snapshot(
            operation_id=OperationId("op-deploy-bad-chunk"),
            expected_operation_version=2,
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=3,
            admission_id=RepositoryAdmissionId("repo-1"),
            expected_admission_version=4,
            expected_source_sha="d" * 40,
            idempotency_key="deploy-idem-bad",
            queued_at=NOW,
            snapshot={"app/page.tsx": b"ok"},
        )
        store.create_deploy_task_once(task)
        material_hash = task.material_hash
        metadata_id = self.document_id(kind="deploy_task", logical_id=material_hash)

        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_deploy_task_once(task)
        client.documents["deploy_tasks"][metadata_id]["operation_id"] = "op-other"
        with self.assertRaises(StoreError):
            self.store_for(client).get_deploy_task(task.operation_id)

        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_deploy_task_once(task)
        client.documents["deploy_tasks"][metadata_id]["schema_version"] = True
        with self.assertRaises(StoreError):
            self.store_for(client).get_deploy_task(task.operation_id)

        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_deploy_task_once(task)
        client.documents["deploy_tasks"][metadata_id]["secret_attachments"] = [
            {
                "secret_id": "sec-1",
                "secret_version": 1,
                "metadata_version": 2,
                "extra": "nope",
            }
        ]
        with self.assertRaises(StoreError):
            self.store_for(client).get_deploy_task(task.operation_id)

        client = FakeFirestoreClient()
        store = self.store_for(client)
        store.create_deploy_task_once(task)
        client.documents["deploy_tasks"][metadata_id]["expected_snapshot_digest"] = (
            "0" * 64
        )
        with self.assertRaises(StoreError):
            self.store_for(client).get_deploy_task(task.operation_id)

    def test_fake_transaction_create_signature_is_strict(self) -> None:
        client = FakeFirestoreClient()
        transaction = FakeTransaction(client=client)
        reference = client.collection("demo").document("doc-1")

        with self.assertRaises(TypeError):
            transaction.create(reference, reference, {"schema_version": 1})  # type: ignore[call-arg]

    def test_lifecycle_actions_round_trip_and_preserve_execution_consistency(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        store = self.store_for(client)
        action = store.create_lifecycle_action(sample_lifecycle_action())
        executed = store.save_lifecycle_action(
            action.transition_state(
                LifecycleActionState.EXECUTED,
                at=NOW + timedelta(hours=1),
            ),
            expected_version=1,
        )

        self.assertEqual(executed.state, LifecycleActionState.EXECUTED)
        self.assertIsNotNone(executed.executed_at)
        self.assertEqual(store.get_lifecycle_action(action.id), executed)

    def test_fresh_store_plain_saves_require_exact_optimistic_versions(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        first_store = self.store_for(client)
        user = first_store.create_user(sample_user())
        admission = first_store.create_repository_admission(
            sample_repository_admission()
        )
        workload = first_store.create_workload(sample_workload())
        binding = first_store.create_app_hostname_binding(sample_app_hostname_binding())
        plan = first_store.create_deployment_plan(sample_plan())
        operation = first_store.create_operation_once(sample_operation())
        schedule = first_store.create_schedule(sample_schedule())
        secret = first_store.create_secret_metadata(sample_secret())
        aggregate = first_store.create_daily_usage_aggregate(sample_daily_aggregate())
        action = first_store.create_lifecycle_action(sample_lifecycle_action())

        second_store = self.store_for(client)

        saved_user = second_store.save_user(
            user.transition_state(UserState.SUSPENDED, at=NOW + timedelta(minutes=1)),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_user(
                user.transition_state(
                    UserState.SUSPENDED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_admission = second_store.save_repository_admission(
            admission.transition_state(
                RepositoryAdmissionState.REVOKED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_repository_admission(
                admission.transition_state(
                    RepositoryAdmissionState.REVOKED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_workload = second_store.save_workload(
            workload.transition_state(
                WorkloadState.PAUSED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_workload(
                workload.transition_state(
                    WorkloadState.PAUSED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_binding = second_store.save_app_hostname_binding(
            binding.transition_state(
                AppHostnameBindingState.DISABLED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_app_hostname_binding(
                binding.transition_state(
                    AppHostnameBindingState.DISABLED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_plan = second_store.save_deployment_plan(
            plan.transition_state(
                PlanState.CANCELLED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_deployment_plan(
                plan.transition_state(
                    PlanState.CANCELLED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_operation = second_store.save_operation(
            operation.transition(
                OperationState.BUILDING,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_operation(
                operation.transition(
                    OperationState.BUILDING,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_schedule = second_store.save_schedule(
            schedule.transition_state(
                ScheduleState.PAUSED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_schedule(
                schedule.transition_state(
                    ScheduleState.PAUSED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_secret = second_store.save_secret_metadata(
            secret.transition_rotation(
                SecretRotationState.ROTATING,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_secret_metadata(
                secret.transition_rotation(
                    SecretRotationState.ROTATING,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_aggregate = second_store.save_daily_usage_aggregate(
            dataclasses.replace(
                aggregate,
                successes=7,
                version=2,
                updated_at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_daily_usage_aggregate(
                dataclasses.replace(
                    aggregate,
                    successes=7,
                    version=2,
                    updated_at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        saved_action = second_store.save_lifecycle_action(
            action.transition_state(
                LifecycleActionState.EXECUTED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=1,
        )
        with self.assertRaises(VersionConflict):
            second_store.save_lifecycle_action(
                action.transition_state(
                    LifecycleActionState.EXECUTED,
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=1,
            )

        self.assertEqual(second_store.get_user(user.id), saved_user)
        self.assertEqual(
            second_store.get_repository_admission(admission.id),
            saved_admission,
        )
        self.assertEqual(second_store.get_workload(workload.id), saved_workload)
        self.assertEqual(
            second_store.get_app_hostname_binding(binding.public_host),
            saved_binding,
        )
        self.assertEqual(second_store.get_deployment_plan(plan.id), saved_plan)
        self.assertEqual(second_store.get_operation(operation.id), saved_operation)
        self.assertEqual(second_store.get_schedule(schedule.id), saved_schedule)
        self.assertEqual(second_store.get_secret_metadata(secret.id), saved_secret)
        self.assertEqual(
            second_store.get_daily_usage_aggregate(
                saved_aggregate.day,
                saved_aggregate.user_id,
            ),
            saved_aggregate,
        )
        self.assertEqual(second_store.get_lifecycle_action(action.id), saved_action)


@unittest.skipUnless(
    os.environ.get("FIRESTORE_EMULATOR_HOST"),
    "FIRESTORE_EMULATOR_HOST is not set.",
)
class FirestoreStoreEmulatorTests(unittest.TestCase):
    def test_emulator_smoke_placeholder(self) -> None:
        self.skipTest(
            "Firestore emulator smoke tests are not configured in this environment."
        )


if __name__ == "__main__":
    unittest.main()
