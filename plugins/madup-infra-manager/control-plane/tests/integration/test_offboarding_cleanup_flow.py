from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore  # noqa: E402
from mim_control_plane.domain.directory_sync import (  # noqa: E402
    DirectoryAuthoritativeSnapshot,
    DirectorySnapshotUser,
)
from mim_control_plane.domain.models import (  # noqa: E402
    RepositoryAdmission,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (  # noqa: E402
    LifecycleActionState,
    RepositoryAdmissionState,
    ScheduleState,
    SecretLifecycleState,
    SecretRotationState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.services.repair import (  # noqa: E402
    DriftComponent,
    DriftObservation,
    RepairGateSnapshot,
)
from mim_control_plane.workers.identity_sync import (  # noqa: E402
    DirectoryIdentitySyncWorker,
)
from mim_control_plane.workers.lifecycle import LifecycleWorker  # noqa: E402
from mim_control_plane.workers.reconcile import (  # noqa: E402
    ReconcileGateResolution,
    ReconcileWorker,
)

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def user(
    *,
    user_id: str = "usr-1",
    state: UserState = UserState.ACTIVE,
    version: int = 1,
    updated_at: datetime = NOW - timedelta(days=2),
) -> User:
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=UserRole.USER,
        state=state,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=120),
        updated_at=updated_at,
        version=version,
    )


def admission(*, admission_id: str = "repo-1") -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId(admission_id),
        repository_numeric_id=42,
        owner="madupmarketing",
        name="sample-app",
        installation_id=9,
        state=RepositoryAdmissionState.ADMITTED,
        admitted_sha="a" * 40,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
        version=1,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
    kind: WorkloadKind = WorkloadKind.SCHEDULED_SCRIPT,
    state: WorkloadState = WorkloadState.ACTIVE,
    updated_at: datetime = NOW - timedelta(days=2),
    last_activity_at: datetime | None = NOW - timedelta(days=40),
    version: int = 1,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name=workload_id,
        kind=kind,
        state=state,
        source_sha="b" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=60),
        updated_at=updated_at,
        last_activity_at=last_activity_at,
        last_healthy_image_digest="sha256:" + "1" * 64,
        version=version,
    )


def schedule(
    *,
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
    state: ScheduleState = ScheduleState.ENABLED,
    version: int = 1,
) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId(owner_id),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=1),
        version=version,
    )


def secret(
    *,
    secret_id: str = "sec-1",
    owner_id: str = "usr-1",
    workload_ids: tuple[str, ...] = ("wrk-1",),
) -> SecretMetadata:
    return SecretMetadata(
        id=SecretId(secret_id),
        owner_id=UserId(owner_id),
        name="shared-slack-install",
        integration_type="slack_oauth",
        attached_workload_ids=tuple(WorkloadId(item) for item in workload_ids),
        active_version=1,
        rotation_state=SecretRotationState.STABLE,
        lifecycle_state=SecretLifecycleState.ACTIVE,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
        version=1,
    )


def directory_snapshot(
    *,
    users: tuple[DirectorySnapshotUser, ...],
    snapshot_id: str = "snap-1",
    completed_at: datetime = NOW,
) -> DirectoryAuthoritativeSnapshot:
    return DirectoryAuthoritativeSnapshot(
        snapshot_id=snapshot_id,
        required_group="mim-users",
        started_at=completed_at - timedelta(minutes=2),
        completed_at=completed_at,
        users=users,
    )


class StaticDirectoryProvider:
    def __init__(self, snapshot: DirectoryAuthoritativeSnapshot) -> None:
        self._snapshot = snapshot

    def fetch_snapshot(
        self,
        *,
        required_group: str,
        now: datetime,
    ) -> DirectoryAuthoritativeSnapshot:
        self.last_required_group = required_group
        self.last_now = now
        return self._snapshot


class RecordingSessionGate:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[UserId, str]] = []

    def deny_user_sessions(self, *, user_id: UserId, reason: str) -> None:
        self.events.append(f"session:{user_id}:{reason}")
        self.calls.append((user_id, reason))


class RecordingAccessManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[WorkloadId, int, str]] = []

    def remove_owner_access(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        reason: str,
    ) -> None:
        self.events.append(f"access:{workload_id}:{reason}")
        self.calls.append((workload_id, expected_workload_version, reason))


class RecordingSecretBindingManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[WorkloadId, tuple[SecretId, ...], int]] = []

    def remove_workload_bindings(
        self,
        *,
        workload_id: WorkloadId,
        secret_ids: tuple[SecretId, ...],
        expected_workload_version: int,
    ) -> None:
        self.events.append(f"bindings:{workload_id}")
        self.calls.append((workload_id, secret_ids, expected_workload_version))


class RecordingSlackGrantManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[UserId, str]] = []

    def revoke_user_grant(self, *, user_id: UserId, reason: str) -> None:
        self.events.append(f"slack:{user_id}:{reason}")
        self.calls.append((user_id, reason))


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def notify_admin(self, *, user_id: UserId, kind: str, reason: str) -> None:
        self.calls.append((str(user_id), kind, reason))

    def notify_owner(
        self,
        *,
        user_id: UserId,
        workload_id: WorkloadId,
        reason: str,
    ) -> None:
        self.calls.append((str(user_id), str(workload_id), reason))


class RecordingTransferManager:
    def __init__(self) -> None:
        self.calls: list[tuple[UserId, tuple[WorkloadId, ...], str]] = []

    def open_transfer_window(
        self,
        *,
        user_id: UserId,
        workload_ids: tuple[WorkloadId, ...],
        reason: str,
    ) -> None:
        self.calls.append((user_id, workload_ids, reason))


class RecordingScheduleManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[ScheduleId, ScheduleState, int]] = []

    def apply_schedule_state(
        self,
        *,
        schedule_id: ScheduleId,
        workload_id: WorkloadId,
        target_state: ScheduleState,
        expected_schedule_version: int,
        reason: str,
    ) -> None:
        del workload_id, reason
        self.events.append(f"schedule:{schedule_id}:{target_state.value}")
        self.calls.append((schedule_id, target_state, expected_schedule_version))


class RecordingComputeManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[WorkloadId, int, tuple[str, ...]]] = []

    def delete_compute(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        target_kinds: tuple[str, ...],
        retain_image_until: datetime | None,
    ) -> None:
        del retain_image_until
        self.events.append(f"compute:{workload_id}")
        self.calls.append((workload_id, expected_workload_version, target_kinds))


class RecordingRuntimeReconciler:
    def __init__(self) -> None:
        self.calls: list[tuple[WorkloadId, int, int, tuple[str, ...]]] = []

    def reconcile_runtime(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        expected_admission_version: int,
        fields: tuple[str, ...],
    ) -> None:
        self.calls.append(
            (
                workload_id,
                expected_workload_version,
                expected_admission_version,
                fields,
            )
        )


class RecordingReconcileGateResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[WorkloadId, datetime]] = []

    def resolve(
        self,
        *,
        workload_id: WorkloadId,
        workload_version: int,
        admission_version: int,
        now: datetime,
    ) -> ReconcileGateResolution:
        self.calls.append((workload_id, now))
        return ReconcileGateResolution(
            gates=RepairGateSnapshot(
                holds_clear=True,
                quota_clear=True,
                emergency_stop_clear=True,
                policy_clear=True,
                admission_current=True,
                workload_version_current=True,
            ),
            expected_workload_version=workload_version,
            expected_admission_version=admission_version,
        )


class OffboardingCleanupFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(user())
        self.store.create_repository_admission(admission())
        self.store.create_workload(workload())
        self.store.create_schedule(schedule())
        self.store.create_secret_metadata(secret())

    def test_directory_offboarding_quarantines_access_and_executes_cleanup_after_seven_days(  # noqa: E501
        self,
    ) -> None:
        sync_worker = DirectoryIdentitySyncWorker(
            directory=StaticDirectoryProvider(directory_snapshot(users=())),
            repository=self.store,
            required_group="mim-users",
            max_snapshot_age=timedelta(minutes=30),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW,
        )

        sync_result = sync_worker.run(now=NOW)

        self.assertEqual(sync_result.locked_user_ids, (UserId("usr-1"),))
        self.assertEqual(
            self.store.get_user(UserId("usr-1")).state,
            UserState.OFFBOARDED,
        )

        events: list[str] = []
        transfer = RecordingTransferManager()
        lifecycle = LifecycleWorker(
            store=self.store,
            sessions=RecordingSessionGate(events),
            access=RecordingAccessManager(events),
            secret_bindings=RecordingSecretBindingManager(events),
            slack_grants=RecordingSlackGrantManager(events),
            notifier=RecordingNotifier(),
            transfer=transfer,
            schedules=RecordingScheduleManager(events),
            compute=RecordingComputeManager(events),
        )

        first = lifecycle.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=NOW,
            holds=frozenset(),
            now=NOW,
        )

        self.assertEqual(first.user.state, UserState.OFFBOARDED)
        self.assertEqual(first.planned_actions, ())
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.QUARANTINED,
        )
        self.assertEqual(
            self.store.get_schedule(ScheduleId("sch-1")).state,
            ScheduleState.DISABLED,
        )
        self.assertEqual(
            self.store.get_secret_metadata(SecretId("sec-1")).lifecycle_state,
            SecretLifecycleState.LOCKED,
        )
        self.assertEqual(
            events[:5],
            [
                "session:usr-1:offboarded",
                "access:wrk-1:offboarded",
                "bindings:wrk-1",
                "slack:usr-1:offboarded",
                "schedule:sch-1:disabled",
            ],
        )
        self.assertEqual(
            transfer.calls,
            [
                (
                    UserId("usr-1"),
                    (WorkloadId("wrk-1"),),
                    "offboarded_transfer_window",
                )
            ],
        )

        later = lifecycle.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=NOW,
            holds=frozenset(),
            now=NOW + timedelta(days=7),
        )

        self.assertEqual(len(later.planned_actions), 1)
        planned = later.planned_actions[0]
        executed = lifecycle.execute_planned_action(
            planned,
            user_id=UserId("usr-1"),
            holds=frozenset(),
            account_locked_at=NOW,
            now=NOW + timedelta(days=7),
        )

        self.assertEqual(executed.action.state, LifecycleActionState.EXECUTED)
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.ARCHIVED,
        )
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).last_healthy_image_digest,
            "sha256:" + "1" * 64,
        )
        self.assertLess(
            events.index("schedule:sch-1:archived"),
            events.index("compute:wrk-1"),
        )

    def test_reactivation_cancels_stale_cleanup_before_compute_mutation(self) -> None:
        suspended_snapshot = directory_snapshot(
            users=(
                DirectorySnapshotUser(
                    directory_user_id="dir-1",
                    email="usr-1@madup.com",
                    active=True,
                    in_required_group=False,
                ),
            )
        )
        sync_worker = DirectoryIdentitySyncWorker(
            directory=StaticDirectoryProvider(suspended_snapshot),
            repository=self.store,
            required_group="mim-users",
            max_snapshot_age=timedelta(minutes=30),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW,
        )
        sync_worker.run(now=NOW)

        lifecycle = LifecycleWorker(
            store=self.store,
            sessions=RecordingSessionGate([]),
            access=RecordingAccessManager([]),
            secret_bindings=RecordingSecretBindingManager([]),
            slack_grants=RecordingSlackGrantManager([]),
            notifier=RecordingNotifier(),
            transfer=RecordingTransferManager(),
            schedules=RecordingScheduleManager([]),
            compute=RecordingComputeManager([]),
        )
        lifecycle.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=NOW,
            holds=frozenset(),
            now=NOW,
        )
        planned = lifecycle.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=NOW,
            holds=frozenset(),
            now=NOW + timedelta(days=7),
        ).planned_actions[0]

        reactivation_snapshot = directory_snapshot(
            users=(
                DirectorySnapshotUser(
                    directory_user_id="dir-1",
                    email="usr-1@madup.com",
                    active=True,
                    in_required_group=True,
                ),
            ),
            snapshot_id="snap-2",
            completed_at=NOW + timedelta(days=7),
        )
        DirectoryIdentitySyncWorker(
            directory=StaticDirectoryProvider(reactivation_snapshot),
            repository=self.store,
            required_group="mim-users",
            max_snapshot_age=timedelta(minutes=30),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW + timedelta(days=7),
        ).run(now=NOW + timedelta(days=7))

        executed = lifecycle.execute_planned_action(
            planned,
            user_id=UserId("usr-1"),
            holds=frozenset(),
            account_locked_at=None,
            now=NOW + timedelta(days=7),
        )

        self.assertEqual(executed.action.state, LifecycleActionState.CANCELLED)

    def test_execute_planned_action_rejects_forged_cleanup_wrapper(self) -> None:
        locked_at = NOW - timedelta(days=7)
        DirectoryIdentitySyncWorker(
            directory=StaticDirectoryProvider(directory_snapshot(users=())),
            repository=self.store,
            required_group="mim-users",
            max_snapshot_age=timedelta(minutes=30),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW,
        ).run(now=NOW)
        lifecycle = LifecycleWorker(
            store=self.store,
            sessions=RecordingSessionGate([]),
            access=RecordingAccessManager([]),
            secret_bindings=RecordingSecretBindingManager([]),
            slack_grants=RecordingSlackGrantManager([]),
            notifier=RecordingNotifier(),
            transfer=RecordingTransferManager(),
            schedules=RecordingScheduleManager([]),
            compute=RecordingComputeManager([]),
        )
        lifecycle.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=locked_at,
            holds=frozenset(),
            now=NOW,
        )
        planned = lifecycle.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=locked_at,
            holds=frozenset(),
            now=locked_at + timedelta(days=7),
        ).planned_actions[0]
        forged = replace(
            planned,
            cleanup_guard=replace(
                planned.cleanup_guard,
                expected_workload_owner_id=UserId("usr-2"),
            ),
        )

        with self.assertRaises(ValueError):
            lifecycle.execute_planned_action(
                forged,
                user_id=UserId("usr-1"),
                holds=frozenset(),
                account_locked_at=locked_at,
                now=locked_at + timedelta(days=7),
            )

    def test_reconcile_worker_repairs_safe_drift_but_quarantines_privilege_expansion(
        self,
    ) -> None:
        runtime = RecordingRuntimeReconciler()
        access = RecordingAccessManager([])
        resolver = RecordingReconcileGateResolver()
        worker = ReconcileWorker(
            store=self.store,
            runtime=runtime,
            access=access,
            gates=resolver,
        )

        safe = worker.reconcile(
            workload_id=WorkloadId("wrk-1"),
            drift=DriftObservation(
                components=(DriftComponent.RUNTIME_ENV, DriftComponent.LABELS)
            ),
            now=NOW,
        )
        unsafe = worker.reconcile(
            workload_id=WorkloadId("wrk-1"),
            drift=DriftObservation(components=(DriftComponent.IAM_POLICY,)),
            now=NOW + timedelta(minutes=1),
        )

        self.assertEqual(safe.kind, "reconcile_runtime")
        self.assertEqual(
            runtime.calls,
            [(WorkloadId("wrk-1"), 1, 1, ("runtime_env", "labels"))],
        )
        self.assertEqual(
            resolver.calls,
            [
                (WorkloadId("wrk-1"), NOW),
                (WorkloadId("wrk-1"), NOW + timedelta(minutes=1)),
            ],
        )
        self.assertEqual(unsafe.kind, "quarantine_escalate")
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.QUARANTINED,
        )
        self.assertEqual(
            access.calls,
            [(WorkloadId("wrk-1"), 1, "unsafe_drift_detected")],
        )

    def test_reconcile_privilege_drift_removes_access_even_if_already_quarantined(
        self,
    ) -> None:
        access = RecordingAccessManager([])
        resolver = RecordingReconcileGateResolver()
        current = self.store.get_workload(WorkloadId("wrk-1"))
        self.store.save_workload(
            current.transition_state(WorkloadState.QUARANTINED, at=NOW),
            expected_version=current.version,
        )
        worker = ReconcileWorker(
            store=self.store,
            runtime=RecordingRuntimeReconciler(),
            access=access,
            gates=resolver,
        )

        result = worker.reconcile(
            workload_id=WorkloadId("wrk-1"),
            drift=DriftObservation(components=(DriftComponent.IAM_POLICY,)),
            now=NOW + timedelta(minutes=2),
        )

        self.assertEqual(result.kind, "quarantine_escalate")
        self.assertEqual(
            access.calls,
            [(WorkloadId("wrk-1"), 2, "unsafe_drift_detected")],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
