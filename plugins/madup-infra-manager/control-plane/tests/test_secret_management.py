from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.adapters.secret_manager import (
    ManagedSecretMetadata,
    ObservedSecretState,
    SecretVersionMetadata,
    SecretVersionStateMetadata,
)
from mim_control_plane.domain.models import (
    Operation,
    OperationId,
    OrgCostGuard,
    SecretId,
    SecretMetadata,
    UsageEntry,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    OperationState,
    SecretLifecycleState,
    SecretRotationState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.store import InvariantViolation, StoreError
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.runtime_naming import provider_secret_id
from mim_control_plane.services.secret_management import (
    SecretDenied,
    SecretManagementService,
)

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
SECRET_ID = "sec-0123456789abcdefabcd"


def seed_org_cost_guard(
    store: MemoryStore,
    *,
    evaluated_at: datetime = NOW,
) -> None:
    store.create_org_cost_guard(
        OrgCostGuard(
            evaluated_at=evaluated_at,
            latest_usage_collected_at=evaluated_at,
            emergency_stop=False,
            org_policy_cost_krw=0,
        )
    )


def principal(
    *,
    user_id: str = "usr-1",
    role: UserRole = UserRole.USER,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=role,
    )


def user(
    *,
    user_id: str = "usr-1",
    role: UserRole = UserRole.USER,
    state: UserState = UserState.ACTIVE,
) -> User:
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=role,
        state=state,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW,
    )


def workload(
    *,
    workload_id: str,
    owner_id: str = "usr-1",
    state: WorkloadState = WorkloadState.ACTIVE,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id="repo-1",
        name=f"workload-{workload_id}",
        kind=WorkloadKind.NEXTJS,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=5),
        updated_at=NOW,
        last_activity_at=NOW - timedelta(hours=1),
    )


def secret_record(
    *,
    secret_id: str = SECRET_ID,
    owner_id: str = "usr-1",
    name: str = "slack-bot",
    attached_workload_ids: tuple[str, ...] = ("wrk-1",),
    active_version: int = 3,
    rotation_state: SecretRotationState = SecretRotationState.STABLE,
    lifecycle_state: SecretLifecycleState = SecretLifecycleState.ACTIVE,
    retiring_version: int | None = None,
    retirement_not_before: datetime | None = None,
    version: int = 1,
) -> SecretMetadata:
    return SecretMetadata(
        id=SecretId(secret_id),
        owner_id=UserId(owner_id),
        name=name,
        integration_type="slack_oauth",
        attached_workload_ids=tuple(WorkloadId(item) for item in attached_workload_ids),
        active_version=active_version,
        rotation_state=rotation_state,
        lifecycle_state=lifecycle_state,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW,
        retiring_version=retiring_version,
        retirement_not_before=retirement_not_before,
        version=version,
    )


class RecordingManagedSecretPort:
    def __init__(self) -> None:
        self.ensure_calls: list[tuple[str, tuple[WorkloadId, ...]]] = []
        self.add_calls: list[tuple[str, bytes]] = []
        self.bind_calls: list[tuple[str, tuple[WorkloadId, ...]]] = []
        self.resolve_calls: list[tuple[WorkloadId, tuple[object, ...]]] = []
        self.disable_calls: list[tuple[str, str, int, datetime, datetime]] = []
        self.destroy_calls: list[tuple[str, str, int, datetime, datetime]] = []
        self.existing_names: set[str] = set()
        self.next_versions: dict[str, int] = {}
        self.bound_workloads: dict[str, tuple[WorkloadId, ...]] = {}
        self.enabled_versions: dict[str, set[int]] = {}
        self.disabled_versions: dict[str, set[int]] = {}
        self.destroyed_versions: dict[str, set[int]] = {}
        self.after_effect_hook = None

    def ensure_secret(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> ManagedSecretMetadata:
        physical_name = provider_secret_id(secret_id)
        self.ensure_calls.append((physical_name, workload_ids))
        created = physical_name not in self.existing_names
        self.existing_names.add(physical_name)
        self.bound_workloads[physical_name] = workload_ids
        self.enabled_versions.setdefault(physical_name, set())
        self.disabled_versions.setdefault(physical_name, set())
        self.destroyed_versions.setdefault(physical_name, set())
        self._after_effect("ensure_secret")
        return ManagedSecretMetadata(
            name=f"projects/{PROJECT_ID}/secrets/{physical_name}",
            created=created,
            labels=(("managed-by", "mim-control-plane"),),
        )

    def add_version(
        self,
        *,
        secret_id: SecretId,
        payload: bytes,
    ) -> SecretVersionMetadata:
        physical_name = provider_secret_id(secret_id)
        self.add_calls.append((physical_name, payload))
        version = self.next_versions.get(physical_name, 1)
        self.next_versions[physical_name] = version + 1
        self.enabled_versions.setdefault(physical_name, set()).add(version)
        self.disabled_versions.setdefault(physical_name, set()).discard(version)
        self.destroyed_versions.setdefault(physical_name, set()).discard(version)
        self._after_effect("add_version")
        return SecretVersionMetadata(
            name=f"projects/{PROJECT_ID}/secrets/{physical_name}/versions/{version}",
            version=version,
            state="enabled",
            checksum_verified=True,
        )

    def ensure_exact_bindings(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> None:
        physical_name = provider_secret_id(secret_id)
        self.bind_calls.append((physical_name, workload_ids))
        self.bound_workloads[physical_name] = workload_ids
        self._after_effect("ensure_exact_bindings")

    def disable_old_version(
        self,
        *,
        secret_id: SecretId,
        version_name: str,
        active_version: int,
        retirement_not_before: datetime,
        now: datetime,
    ) -> SecretVersionStateMetadata:
        physical_name = provider_secret_id(secret_id)
        self.disable_calls.append(
            (physical_name, version_name, active_version, retirement_not_before, now)
        )
        version = int(version_name.rsplit("/", 1)[1])
        self.enabled_versions.setdefault(physical_name, set()).discard(version)
        self.disabled_versions.setdefault(physical_name, set()).add(version)
        self._after_effect("disable_old_version")
        return SecretVersionStateMetadata(
            name=version_name,
            version=version,
            state="disabled",
        )

    def destroy_old_version(
        self,
        *,
        secret_id: SecretId,
        version_name: str,
        active_version: int,
        retirement_not_before: datetime,
        now: datetime,
    ) -> SecretVersionStateMetadata:
        physical_name = provider_secret_id(secret_id)
        self.destroy_calls.append(
            (physical_name, version_name, active_version, retirement_not_before, now)
        )
        version = int(version_name.rsplit("/", 1)[1])
        self.disabled_versions.setdefault(physical_name, set()).discard(version)
        self.destroyed_versions.setdefault(physical_name, set()).add(version)
        self._after_effect("destroy_old_version")
        return SecretVersionStateMetadata(
            name=version_name,
            version=version,
            state="destroyed",
        )

    def resolve(
        self,
        *,
        workload_id: WorkloadId,
        attachments: tuple[object, ...],
    ) -> tuple[object, ...]:
        self.resolve_calls.append((workload_id, attachments))
        return ()

    def probe_secret(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> ObservedSecretState:
        physical_name = provider_secret_id(secret_id)
        exists = physical_name in self.existing_names
        return ObservedSecretState(
            name=f"projects/{PROJECT_ID}/secrets/{physical_name}",
            exists=exists,
            exact_bindings=(
                exists and self.bound_workloads.get(physical_name) == workload_ids
            ),
            enabled_versions=tuple(
                sorted(self.enabled_versions.get(physical_name, set()))
            ),
            disabled_versions=tuple(
                sorted(self.disabled_versions.get(physical_name, set()))
            ),
            destroyed_versions=tuple(
                sorted(self.destroyed_versions.get(physical_name, set()))
            ),
        )

    def _after_effect(self, effect: str) -> None:
        if self.after_effect_hook is not None:
            self.after_effect_hook(effect)


class InjectedStoreFailure(StoreError):
    pass


class FailingMemoryStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self._fail_next_secret_write = False
        self._fail_next_operation_save = False

    def arm_next_secret_write(self) -> None:
        self._fail_next_secret_write = True

    def arm_next_operation_save(self) -> None:
        self._fail_next_operation_save = True

    def create_secret_metadata(self, secret: SecretMetadata) -> SecretMetadata:
        if self._fail_next_secret_write:
            self._fail_next_secret_write = False
            raise InjectedStoreFailure("secret write failed")
        return super().create_secret_metadata(secret)

    def save_secret_metadata(
        self,
        secret: SecretMetadata,
        *,
        expected_version: int,
    ) -> SecretMetadata:
        if self._fail_next_secret_write:
            self._fail_next_secret_write = False
            raise InjectedStoreFailure("secret write failed")
        return super().save_secret_metadata(secret, expected_version=expected_version)

    def save_operation(
        self,
        operation: Operation,
        *,
        expected_version: int,
    ) -> Operation:
        if self._fail_next_operation_save:
            self._fail_next_operation_save = False
            raise InjectedStoreFailure("operation write failed")
        return super().save_operation(operation, expected_version=expected_version)


class RecordingUsageScopeStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.usage_owner_ids: list[UserId | None] = []

    def list_usage_entries(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[UsageEntry, ...]:
        self.usage_owner_ids.append(owner_id)
        return super().list_usage_entries(owner_id=owner_id)


class SecretManagementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(user())
        seed_org_cost_guard(self.store)
        self.store.create_workload(workload(workload_id="wrk-1"))
        self.store.create_workload(workload(workload_id="wrk-2"))
        self.port = RecordingManagedSecretPort()
        self.service = SecretManagementService(
            store=self.store,
            secret_port=self.port,
            clock=lambda: NOW,
            id_factory=self._id_factory,
        )

    def _seed_external_secret(
        self,
        *,
        secret_id: str = SECRET_ID,
        workload_ids: tuple[str, ...] = ("wrk-1",),
        active_version: int = 3,
        disabled_versions: tuple[int, ...] = (),
        destroyed_versions: tuple[int, ...] = (),
    ) -> None:
        provider_name = provider_secret_id(secret_id)
        normalized_workloads = tuple(WorkloadId(item) for item in workload_ids)
        self.port.existing_names.add(provider_name)
        self.port.bound_workloads[provider_name] = normalized_workloads
        self.port.enabled_versions[provider_name] = {active_version}
        self.port.disabled_versions[provider_name] = set(disabled_versions)
        self.port.destroyed_versions[provider_name] = set(destroyed_versions)
        self.port.next_versions[provider_name] = active_version + 1

    def _id_factory(self, prefix: str) -> str:
        counters = getattr(self, "_counters", {})
        next_value = counters.get(prefix, 0) + 1
        counters[prefix] = next_value
        self._counters = counters
        return f"{prefix}-{next_value}"

    def _build_failing_service(
        self,
    ) -> tuple[
        FailingMemoryStore,
        RecordingManagedSecretPort,
        SecretManagementService,
    ]:
        store = FailingMemoryStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        store.create_workload(workload(workload_id="wrk-1"))
        store.create_workload(workload(workload_id="wrk-2"))
        port = RecordingManagedSecretPort()
        service = SecretManagementService(
            store=store,
            secret_port=port,
            clock=lambda: NOW,
            id_factory=self._id_factory,
        )
        return store, port, service

    def test_plan_and_apply_create_secret_persists_value_free_metadata(self) -> None:
        reviewed = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )

        applied = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-1",
            payload=b"super-secret-token",
        )

        self.assertEqual(applied["mode"], "create")
        self.assertEqual(applied["active_version"], 1)
        self.assertFalse(applied["replayed"])
        self.assertEqual(len(self.port.add_calls), 1)
        persisted = self.store.get_secret_metadata(SecretId(applied["secret_id"]))
        self.assertEqual(persisted.name, "slack-bot")
        self.assertEqual(persisted.active_version, 1)
        self.assertEqual(persisted.rotation_state, SecretRotationState.STABLE)
        self.assertIsNone(persisted.retiring_version)
        self.assertIsNone(persisted.retirement_not_before)
        operation = self.store.get_operation(OperationId(applied["operation_id"]))
        self.assertEqual(operation.state, OperationState.SUCCEEDED)
        self.assertEqual(
            set(applied),
            {
                "action",
                "operation_id",
                "secret_id",
                "mode",
                "active_version",
                "rotation_state",
                "retiring_version",
                "attached_workload_ids",
                "replayed",
            },
        )
        self.assertEqual(applied["retiring_version"], None)
        self.assertEqual(applied["attached_workload_ids"], ("wrk-1",))
        provider_name = provider_secret_id(applied["secret_id"])
        self.assertEqual(self.port.ensure_calls[0][0], provider_name)
        self.assertEqual(self.port.add_calls[0][0], provider_name)
        self.assertNotIn("payload_sha256", str(operation.result_summary))
        self.assertNotIn("payload_sha256", repr(operation))

    def test_apply_rotate_secret_disables_old_version_and_persists_retirement_window(
        self,
    ) -> None:
        self.store.create_secret_metadata(secret_record())
        self._seed_external_secret()

        reviewed = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1", "wrk-2"),
        )

        applied = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-2",
            payload=b"rotated-token",
        )

        self.assertEqual(applied["mode"], "rotate")
        persisted = self.store.get_secret_metadata(SecretId(SECRET_ID))
        self.assertEqual(persisted.active_version, 4)
        self.assertEqual(persisted.retiring_version, 3)
        self.assertEqual(
            persisted.retirement_not_before,
            NOW + timedelta(days=7),
        )
        self.assertEqual(
            persisted.rotation_state,
            SecretRotationState.RETIRING_OLD_VERSION,
        )
        self.assertEqual(
            persisted.attached_workload_ids,
            (WorkloadId("wrk-1"), WorkloadId("wrk-2")),
        )
        self.assertEqual(applied["retiring_version"], 3)
        self.assertEqual(applied["attached_workload_ids"], ("wrk-1", "wrk-2"))
        self.assertEqual(len(self.port.disable_calls), 1)
        self.assertEqual(
            self.port.disable_calls[0][1],
            (
                f"projects/{PROJECT_ID}/secrets/"
                f"{provider_secret_id(SECRET_ID)}/versions/3"
            ),
        )

    def test_same_logical_name_for_different_owners_uses_distinct_provider_secret_ids(
        self,
    ) -> None:
        self.store.create_user(user(user_id="usr-2"))
        self.store.create_workload(workload(workload_id="wrk-3", owner_id="usr-2"))
        reviewed_one = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )
        applied_one = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed_one["plan_id"],
            plan_hash=reviewed_one["plan_hash"],
            idempotency_key="secret-idem-owner-1",
            payload=b"owner-one",
        )

        reviewed_two = self.service.plan_secret_write(
            principal=principal(user_id="usr-2"),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-3",),
        )
        applied_two = self.service.apply_secret_plan(
            principal=principal(user_id="usr-2"),
            plan_id=reviewed_two["plan_id"],
            plan_hash=reviewed_two["plan_hash"],
            idempotency_key="secret-idem-owner-2",
            payload=b"owner-two",
        )

        self.assertNotEqual(
            provider_secret_id(applied_one["secret_id"]),
            provider_secret_id(applied_two["secret_id"]),
        )

    def test_plan_and_apply_attach_secret_updates_exact_workload_bindings(self) -> None:
        self.store.create_secret_metadata(secret_record())
        self._seed_external_secret()

        reviewed = self.service.plan_secret_attach(
            principal=principal(),
            secret_id=SECRET_ID,
            workload_ids=("wrk-1", "wrk-2"),
        )

        applied = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-3",
        )

        self.assertEqual(applied["mode"], "attach")
        self.assertEqual(len(self.port.add_calls), 0)
        self.assertEqual(len(self.port.bind_calls), 1)
        persisted = self.store.get_secret_metadata(SecretId(SECRET_ID))
        self.assertEqual(
            persisted.attached_workload_ids,
            (WorkloadId("wrk-1"), WorkloadId("wrk-2")),
        )

    def test_create_secret_plan_blocks_when_owner_reaches_secret_limit(self) -> None:
        for index in range(5):
            self.store.create_secret_metadata(
                secret_record(
                    secret_id=f"sec-{index:020x}",
                    name=f"secret-{index}",
                    attached_workload_ids=("wrk-1",),
                )
            )

        with self.assertRaises(SecretDenied):
            self.service.plan_secret_write(
                principal=principal(),
                secret_name="another-secret",
                integration_type="slack_oauth",
                workload_ids=("wrk-1",),
            )

    def test_plan_secret_write_uses_owner_scoped_usage_entries_for_cost_checks(
        self,
    ) -> None:
        store = RecordingUsageScopeStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        store.create_workload(workload(workload_id="wrk-1"))
        store.create_workload(workload(workload_id="wrk-2"))
        service = SecretManagementService(
            store=store,
            secret_port=RecordingManagedSecretPort(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
        )

        reviewed = service.plan_secret_write(
            principal=principal(),
            secret_name="scoped-secret",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )

        self.assertEqual(reviewed["mode"], "create")
        self.assertEqual(store.usage_owner_ids, [UserId("usr-1")])

    def test_plan_secret_write_fails_closed_when_org_guard_is_missing(self) -> None:
        store = RecordingUsageScopeStore()
        store.create_user(user())
        store.create_workload(workload(workload_id="wrk-1"))
        store.create_workload(workload(workload_id="wrk-2"))
        service = SecretManagementService(
            store=store,
            secret_port=RecordingManagedSecretPort(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
        )

        with self.assertRaises(SecretDenied):
            service.plan_secret_write(
                principal=principal(),
                secret_name="scoped-secret",
                integration_type="slack_oauth",
                workload_ids=("wrk-1",),
            )

        self.assertEqual(store.usage_owner_ids, [])

    def test_apply_secret_plan_replays_success_without_creating_another_version(
        self,
    ) -> None:
        self.store.create_secret_metadata(secret_record())
        self._seed_external_secret()

        reviewed = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )
        first = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-4",
            payload=b"rotated-token",
        )
        replay = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-4",
            payload=b"rotated-token",
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.port.add_calls), 1)
        self.assertEqual(replay["secret_id"], first["secret_id"])

    def test_apply_create_denies_stale_plan_after_competing_create(self) -> None:
        reviewed = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )
        self.store.create_secret_metadata(
            secret_record(
                secret_id=reviewed["secret_id"],
                active_version=1,
            )
        )

        with self.assertRaises(SecretDenied):
            self.service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-stale-create",
                payload=b"super-secret-token",
            )

    def test_apply_attach_denies_stale_plan_even_when_shape_now_matches(self) -> None:
        current = self.store.create_secret_metadata(secret_record())
        self._seed_external_secret()
        reviewed = self.service.plan_secret_attach(
            principal=principal(),
            secret_id=SECRET_ID,
            workload_ids=("wrk-1", "wrk-2"),
        )
        attaching = self.store.save_secret_metadata(
            current.begin_attachment(
                attached_workload_ids=(WorkloadId("wrk-1"), WorkloadId("wrk-2")),
                mutation_idempotency_key="competing-attach",
                at=NOW + timedelta(seconds=30),
            ),
            expected_version=current.version,
        )
        self.store.save_secret_metadata(
            attaching.finalize_attachment(at=NOW + timedelta(minutes=1)),
            expected_version=attaching.version,
        )
        self.port.bound_workloads[provider_secret_id(SECRET_ID)] = (
            WorkloadId("wrk-1"),
            WorkloadId("wrk-2"),
        )

        with self.assertRaises(SecretDenied):
            self.service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-stale-attach",
            )

    def test_apply_rotate_denies_stale_plan_after_competing_rotation(self) -> None:
        current = self.store.create_secret_metadata(secret_record())
        self._seed_external_secret()
        reviewed = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )
        rotating = self.store.save_secret_metadata(
            current.begin_rotation(
                attached_workload_ids=(WorkloadId("wrk-1"),),
                mutation_idempotency_key="competing-rotate",
                pending_payload_sha256="a" * 64,
                at=NOW + timedelta(seconds=30),
            ),
            expected_version=current.version,
        )
        self.store.save_secret_metadata(
            rotating.record_rotation(
                active_version=4,
                retiring_version=3,
                retirement_not_before=NOW + timedelta(days=7),
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=rotating.version,
        )
        self.port.enabled_versions[provider_secret_id(SECRET_ID)] = {4}
        self.port.disabled_versions[provider_secret_id(SECRET_ID)] = {3}

        with self.assertRaises(SecretDenied):
            self.service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-stale-rotate",
                payload=b"rotated-token",
            )
        self.assertEqual(len(self.port.add_calls), 0)

    def test_finalize_secret_retirement_destroys_old_version_once_due(self) -> None:
        self._seed_external_secret(active_version=4, disabled_versions=(3,))
        self.store.create_secret_metadata(
            secret_record(
                active_version=4,
                rotation_state=SecretRotationState.RETIRING_OLD_VERSION,
                retiring_version=3,
                retirement_not_before=NOW - timedelta(seconds=1),
            )
        )

        result = self.service.finalize_secret_retirement(secret_id=SECRET_ID)

        self.assertEqual(result["state"], "retired")
        self.assertFalse(result["replayed"])
        persisted = self.store.get_secret_metadata(SecretId(SECRET_ID))
        self.assertEqual(persisted.rotation_state, SecretRotationState.STABLE)
        self.assertIsNone(persisted.retiring_version)
        self.assertIsNone(persisted.retirement_not_before)
        self.assertEqual(len(self.port.destroy_calls), 1)

    def test_create_recovers_when_secret_write_fails_after_ensure_secret(self) -> None:
        store, port, service = self._build_failing_service()
        port.after_effect_hook = (
            lambda effect: store.arm_next_secret_write()
            if effect == "ensure_secret"
            else None
        )
        reviewed = service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )

        with self.assertRaises(SecretDenied):
            service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-recover-create-1",
                payload=b"super-secret-token",
            )

        port.after_effect_hook = None
        replay = service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-recover-create-1",
            payload=b"super-secret-token",
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(
            store.get_secret_metadata(
                SecretId(replay["secret_id"])
            ).attached_workload_ids,
            (WorkloadId("wrk-1"),),
        )

    def test_create_recovers_when_secret_write_fails_after_add_version(self) -> None:
        store, port, service = self._build_failing_service()
        port.after_effect_hook = (
            lambda effect: store.arm_next_secret_write()
            if effect == "add_version"
            else None
        )
        reviewed = service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )

        with self.assertRaises(SecretDenied):
            service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-recover-create-2",
                payload=b"super-secret-token",
            )

        port.after_effect_hook = None
        replay = service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-recover-create-2",
            payload=b"super-secret-token",
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(len(port.add_calls), 1)

    def test_attach_recovers_when_secret_write_fails_after_binding(self) -> None:
        store, port, service = self._build_failing_service()
        store.create_secret_metadata(secret_record())
        port.after_effect_hook = (
            lambda effect: store.arm_next_secret_write()
            if effect == "ensure_exact_bindings"
            else None
        )
        port.existing_names.add(provider_secret_id(SECRET_ID))
        port.bound_workloads[provider_secret_id(SECRET_ID)] = (WorkloadId("wrk-1"),)
        port.enabled_versions[provider_secret_id(SECRET_ID)] = {3}
        port.next_versions[provider_secret_id(SECRET_ID)] = 4
        reviewed = service.plan_secret_attach(
            principal=principal(),
            secret_id=SECRET_ID,
            workload_ids=("wrk-1", "wrk-2"),
        )

        with self.assertRaises(SecretDenied):
            service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-recover-attach",
            )

        port.after_effect_hook = None
        replay = service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-recover-attach",
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(
            store.get_secret_metadata(SecretId(SECRET_ID)).attached_workload_ids,
            (WorkloadId("wrk-1"), WorkloadId("wrk-2")),
        )

    def test_rotate_recovers_when_secret_write_fails_after_binding_fix(self) -> None:
        store, port, service = self._build_failing_service()
        store.create_secret_metadata(secret_record())
        port.after_effect_hook = (
            lambda effect: store.arm_next_secret_write()
            if effect == "ensure_secret"
            else None
        )
        port.existing_names.add(provider_secret_id(SECRET_ID))
        port.bound_workloads[provider_secret_id(SECRET_ID)] = (WorkloadId("wrk-1"),)
        port.enabled_versions[provider_secret_id(SECRET_ID)] = {3}
        port.next_versions[provider_secret_id(SECRET_ID)] = 4
        reviewed = service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1", "wrk-2"),
        )

        with self.assertRaises(SecretDenied):
            service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-recover-rotate-bind",
                payload=b"rotated-token",
            )

        port.after_effect_hook = None
        replay = service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-recover-rotate-bind",
            payload=b"rotated-token",
        )

        self.assertTrue(replay["replayed"])
        persisted = store.get_secret_metadata(SecretId(SECRET_ID))
        self.assertEqual(persisted.active_version, 4)
        self.assertEqual(
            persisted.attached_workload_ids,
            (WorkloadId("wrk-1"), WorkloadId("wrk-2")),
        )

    def test_rotate_recovers_when_secret_write_fails_after_add_version(self) -> None:
        store, port, service = self._build_failing_service()
        store.create_secret_metadata(secret_record())
        port.after_effect_hook = (
            lambda effect: store.arm_next_secret_write()
            if effect == "add_version"
            else None
        )
        port.existing_names.add(provider_secret_id(SECRET_ID))
        port.bound_workloads[provider_secret_id(SECRET_ID)] = (WorkloadId("wrk-1"),)
        port.enabled_versions[provider_secret_id(SECRET_ID)] = {3}
        port.next_versions[provider_secret_id(SECRET_ID)] = 4
        reviewed = service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )

        with self.assertRaises(SecretDenied):
            service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-recover-rotate-add",
                payload=b"rotated-token",
            )

        port.after_effect_hook = None
        replay = service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-recover-rotate-add",
            payload=b"rotated-token",
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(
            store.get_secret_metadata(SecretId(SECRET_ID)).active_version,
            4,
        )

    def test_rotate_recovers_when_secret_write_fails_after_disable_old_version(
        self,
    ) -> None:
        store, port, service = self._build_failing_service()
        store.create_secret_metadata(secret_record())
        port.after_effect_hook = (
            lambda effect: store.arm_next_secret_write()
            if effect == "disable_old_version"
            else None
        )
        port.existing_names.add(provider_secret_id(SECRET_ID))
        port.bound_workloads[provider_secret_id(SECRET_ID)] = (WorkloadId("wrk-1"),)
        port.enabled_versions[provider_secret_id(SECRET_ID)] = {3}
        port.next_versions[provider_secret_id(SECRET_ID)] = 4
        reviewed = service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )

        with self.assertRaises(SecretDenied):
            service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-recover-rotate-disable",
                payload=b"rotated-token",
            )

        port.after_effect_hook = None
        replay = service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-recover-rotate-disable",
            payload=b"rotated-token",
        )

        self.assertTrue(replay["replayed"])
        persisted = store.get_secret_metadata(SecretId(SECRET_ID))
        self.assertEqual(persisted.active_version, 4)
        self.assertEqual(persisted.retiring_version, 3)

    def test_store_rejects_direct_secret_rewrite_detach_and_version_jump(self) -> None:
        current = self.store.create_secret_metadata(secret_record())
        with self.assertRaises(InvariantViolation):
            self.store.save_secret_metadata(
                current.bind_workloads(
                    (WorkloadId("wrk-2"),),
                    at=NOW + timedelta(minutes=1),
                ),
                expected_version=current.version,
            )
        with self.assertRaises(InvariantViolation):
            self.store.save_secret_metadata(
                dataclasses.replace(
                    current,
                    active_version=9,
                    updated_at=NOW + timedelta(minutes=1),
                    version=current.version + 1,
                ),
                expected_version=current.version,
            )

    def test_secret_plan_and_apply_enforce_name_workload_and_payload_bounds(
        self,
    ) -> None:
        with self.assertRaises(SecretDenied):
            self.service.plan_secret_write(
                principal=principal(),
                secret_name="Bad_Name",
                integration_type="slack_oauth",
                workload_ids=("wrk-1",),
            )

        for index in range(3, 7):
            self.store.create_workload(workload(workload_id=f"wrk-{index}"))
        with self.assertRaises(SecretDenied):
            self.service.plan_secret_write(
                principal=principal(),
                secret_name="safe-name",
                integration_type="slack_oauth",
                workload_ids=("wrk-1", "wrk-2", "wrk-3", "wrk-4", "wrk-5", "wrk-6"),
            )

        reviewed = self.service.plan_secret_write(
            principal=principal(),
            secret_name="safe-name",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )
        with self.assertRaises(SecretDenied):
            self.service.apply_secret_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="secret-idem-bounds",
                payload=b"a" * (16 * 1024 + 1),
            )

    def test_replay_of_successful_attach_returns_exact_recorded_result_after_later_mutation(  # noqa: E501
        self,
    ) -> None:
        self.store.create_secret_metadata(secret_record())
        self._seed_external_secret()
        attach = self.service.plan_secret_attach(
            principal=principal(),
            secret_id=SECRET_ID,
            workload_ids=("wrk-1", "wrk-2"),
        )
        first = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=attach["plan_id"],
            plan_hash=attach["plan_hash"],
            idempotency_key="secret-idem-attach-proof",
        )

        reviewed = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1", "wrk-2"),
        )
        self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-later-rotate",
            payload=b"later-rotation",
        )

        replay = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=attach["plan_id"],
            plan_hash=attach["plan_hash"],
            idempotency_key="secret-idem-attach-proof",
        )

        self.assertEqual(
            {key: value for key, value in replay.items() if key != "replayed"},
            {key: value for key, value in first.items() if key != "replayed"},
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        operation = self.store.get_operation(OperationId(first["operation_id"]))
        self.assertNotIn("payload_sha256", str(operation.result_summary))
        self.assertNotIn("payload_sha256", repr(operation))

    def test_replay_of_successful_create_returns_exact_recorded_result_after_later_mutation(  # noqa: E501
        self,
    ) -> None:
        create = self.service.plan_secret_write(
            principal=principal(),
            secret_name="fresh-secret",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )
        first = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=create["plan_id"],
            plan_hash=create["plan_hash"],
            idempotency_key="secret-idem-create-proof",
            payload=b"first-create",
        )

        reviewed = self.service.plan_secret_attach(
            principal=principal(),
            secret_id=first["secret_id"],
            workload_ids=("wrk-1", "wrk-2"),
        )
        self.service.apply_secret_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="secret-idem-create-later-attach",
        )

        replay = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=create["plan_id"],
            plan_hash=create["plan_hash"],
            idempotency_key="secret-idem-create-proof",
            payload=b"first-create",
        )

        self.assertEqual(
            {key: value for key, value in replay.items() if key != "replayed"},
            {key: value for key, value in first.items() if key != "replayed"},
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        operation = self.store.get_operation(OperationId(first["operation_id"]))
        self.assertNotIn("payload_sha256", str(operation.result_summary))
        self.assertNotIn("payload_sha256", repr(operation))

    def test_replay_of_successful_rotate_returns_exact_recorded_result_after_later_mutation(  # noqa: E501
        self,
    ) -> None:
        self.store.create_secret_metadata(secret_record())
        self._seed_external_secret()
        rotate = self.service.plan_secret_write(
            principal=principal(),
            secret_name="slack-bot",
            integration_type="slack_oauth",
            workload_ids=("wrk-1",),
        )
        first = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=rotate["plan_id"],
            plan_hash=rotate["plan_hash"],
            idempotency_key="secret-idem-rotate-proof",
            payload=b"first-rotation",
        )

        self.service.finalize_secret_retirement(secret_id=SECRET_ID)
        second = self.service.plan_secret_attach(
            principal=principal(),
            secret_id=SECRET_ID,
            workload_ids=("wrk-1", "wrk-2"),
        )
        self.service.apply_secret_plan(
            principal=principal(),
            plan_id=second["plan_id"],
            plan_hash=second["plan_hash"],
            idempotency_key="secret-idem-later-attach",
        )

        replay = self.service.apply_secret_plan(
            principal=principal(),
            plan_id=rotate["plan_id"],
            plan_hash=rotate["plan_hash"],
            idempotency_key="secret-idem-rotate-proof",
            payload=b"first-rotation",
        )

        self.assertEqual(
            {key: value for key, value in replay.items() if key != "replayed"},
            {key: value for key, value in first.items() if key != "replayed"},
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        operation = self.store.get_operation(OperationId(first["operation_id"]))
        self.assertNotIn("payload_sha256", str(operation.result_summary))
        self.assertNotIn("payload_sha256", repr(operation))


if __name__ == "__main__":
    unittest.main()
