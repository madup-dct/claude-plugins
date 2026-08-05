from __future__ import annotations

import math
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import (
    DeploymentPlan,
    DeploymentPlanId,
    Operation,
    OperationId,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.plans import (
    PlanActorMismatch,
    PlanExpired,
    PlanMaterialMismatch,
    PlanNormalizationError,
    PlanStateMismatch,
    canonical_plan_payload,
    consume_plan_with_operation,
    hash_plan_material,
    validate_consumed_plan_repair,
    validate_plan_request,
)
from mim_control_plane.domain.states import OperationState, PlanState

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def sample_material(*, estimate_krw: int = 1000) -> dict[str, object]:
    return {
        "repository": {
            "owner": "madupmarketing",
            "name": "sample-app",
            "sha": "a" * 40,
        },
        "workload": {
            "kind": "streamlit",
            "name": "sample-app",
        },
        "estimate_krw": estimate_krw,
        "schedule": {
            "timezone": "Asia/Seoul",
            "cron": "0 * * * *",
        },
    }


def issued_plan(
    *,
    actor_id: str = "usr-1",
    material: dict[str, object] | None = None,
    policy_version: str = "policy-v1",
) -> DeploymentPlan:
    plan_material = sample_material() if material is None else material
    return DeploymentPlan(
        id=DeploymentPlanId("plan-1"),
        actor_id=UserId(actor_id),
        workload_id=WorkloadId("wrk-1"),
        action="deploy",
        material_hash=hash_plan_material(
            plan_material,
            action="deploy",
            policy_version=policy_version,
        ),
        policy_version=policy_version,
        state=PlanState.ISSUED,
        expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
        sanitized_summary=(
            ("repository", "madupmarketing/sample-app"),
            ("workload", "streamlit"),
        ),
    )


def planned_operation(
    *,
    operation_id: str = "op-1",
    idempotency_key: str = "idem-1",
    actor_id: str = "usr-1",
    action: str = "deploy",
) -> Operation:
    return Operation(
        id=OperationId(operation_id),
        actor_id=UserId(actor_id),
        workload_id=WorkloadId("wrk-1"),
        action=action,
        idempotency_key=idempotency_key,
        request_hash="request-hash-v1",
        state=OperationState.PLANNED,
        created_at=NOW,
        updated_at=NOW,
    )


class PlanHashingTests(unittest.TestCase):
    def test_hash_uses_sorted_compact_json_and_binds_policy_version(self) -> None:
        material = sample_material()
        reordered = {
            "schedule": material["schedule"],
            "workload": material["workload"],
            "estimate_krw": material["estimate_krw"],
            "repository": material["repository"],
        }

        self.assertEqual(
            canonical_plan_payload(
                material,
                action="deploy",
                policy_version="policy-v1",
            ),
            (
                '{"action":"deploy","material":{"estimate_krw":1000,"repository":{"name":"sample-app",'
                '"owner":"madupmarketing","sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},'
                '"schedule":{"cron":"0 * * * *","timezone":"Asia/Seoul"},'
                '"workload":{"kind":"streamlit","name":"sample-app"}},'
                '"policy_version":"policy-v1"}'
            ),
        )
        self.assertEqual(
            hash_plan_material(
                material,
                action="deploy",
                policy_version="policy-v1",
            ),
            hash_plan_material(
                reordered,
                action="deploy",
                policy_version="policy-v1",
            ),
        )
        self.assertNotEqual(
            hash_plan_material(
                material,
                action="deploy",
                policy_version="policy-v1",
            ),
            hash_plan_material(
                material,
                action="deploy",
                policy_version="policy-v2",
            ),
        )
        self.assertNotEqual(
            hash_plan_material(
                material,
                action="deploy",
                policy_version="policy-v1",
            ),
            hash_plan_material(
                sample_material(estimate_krw=2000),
                action="deploy",
                policy_version="policy-v1",
            ),
        )
        self.assertNotEqual(
            hash_plan_material(
                material,
                action="deploy",
                policy_version="policy-v1",
            ),
            hash_plan_material(
                material,
                action="repair",
                policy_version="policy-v1",
            ),
        )

    def test_hash_rejects_nan_and_infinity_values_with_safe_errors(self) -> None:
        bad_values = (math.nan, math.inf, -math.inf)

        for bad_value in bad_values:
            with self.subTest(bad_value=bad_value):
                with self.assertRaises(PlanNormalizationError) as context:
                    hash_plan_material(
                        sample_material(estimate_krw=bad_value),  # type: ignore[arg-type]
                        action="deploy",
                        policy_version="policy-v1",
                    )

                message = str(context.exception)
                self.assertIn("finite JSON number", message)
                self.assertNotIn(str(bad_value), message)

    def test_validate_plan_request_rejects_wrong_actor_expiry_and_material_drift(
        self,
    ) -> None:
        plan = issued_plan()
        material = sample_material()

        with self.assertRaises(PlanActorMismatch):
            validate_plan_request(
                plan,
                actor_id=UserId("usr-2"),
                material=material,
                action="deploy",
                policy_version="policy-v1",
                at=NOW + timedelta(minutes=1),
            )

        with self.assertRaises(PlanMaterialMismatch):
            validate_plan_request(
                plan,
                actor_id=UserId("usr-1"),
                material=sample_material(estimate_krw=9999),
                action="deploy",
                policy_version="policy-v1",
                at=NOW + timedelta(minutes=1),
            )

        with self.assertRaises(PlanExpired):
            validate_plan_request(
                plan,
                actor_id=UserId("usr-1"),
                material=material,
                action="deploy",
                policy_version="policy-v1",
                at=NOW + timedelta(minutes=16),
            )

    def test_consumed_plan_repair_ignores_expiry_but_binds_state_and_material(
        self,
    ) -> None:
        material = sample_material()
        consumed = issued_plan().transition_state(
            PlanState.CONSUMED,
            at=NOW + timedelta(minutes=1),
        )

        validate_consumed_plan_repair(
            consumed,
            actor_id=UserId("usr-1"),
            material=material,
            action="deploy",
            policy_version="policy-v1",
        )

        with self.assertRaises(PlanStateMismatch):
            validate_consumed_plan_repair(
                issued_plan(),
                actor_id=UserId("usr-1"),
                material=material,
                action="deploy",
                policy_version="policy-v1",
            )
        with self.assertRaises(PlanActorMismatch):
            validate_consumed_plan_repair(
                consumed,
                actor_id=UserId("usr-2"),
                material=material,
                action="deploy",
                policy_version="policy-v1",
            )
        with self.assertRaises(PlanMaterialMismatch):
            validate_consumed_plan_repair(
                consumed,
                actor_id=UserId("usr-1"),
                material=sample_material(estimate_krw=9999),
                action="deploy",
                policy_version="policy-v1",
            )

    def test_consume_plan_is_atomic_single_use_and_idempotent(self) -> None:
        store = MemoryStore()
        plan = issued_plan()
        store.create_deployment_plan(plan)

        consumed_plan, first_operation = consume_plan_with_operation(
            store,
            plan_id=plan.id,
            actor_id=UserId("usr-1"),
            material=sample_material(),
            action="deploy",
            policy_version="policy-v1",
            operation=planned_operation(),
            consumed_at=NOW + timedelta(minutes=1),
        )

        replay_plan, replay_operation = consume_plan_with_operation(
            store,
            plan_id=plan.id,
            actor_id=UserId("usr-1"),
            material=sample_material(),
            action="deploy",
            policy_version="policy-v1",
            operation=planned_operation(operation_id="op-retry"),
            consumed_at=NOW + timedelta(minutes=2),
        )

        self.assertEqual(consumed_plan.state, PlanState.CONSUMED)
        self.assertEqual(store.get_deployment_plan(plan.id).version, 2)
        self.assertEqual(replay_plan, consumed_plan)
        self.assertEqual(replay_operation, first_operation)
        self.assertEqual(replay_operation.id, first_operation.id)

    def test_consume_plan_rejects_material_mismatch_without_consuming(self) -> None:
        store = MemoryStore()
        plan = issued_plan()
        store.create_deployment_plan(plan)

        with self.assertRaises(PlanMaterialMismatch):
            consume_plan_with_operation(
                store,
                plan_id=plan.id,
                actor_id=UserId("usr-1"),
                material=sample_material(estimate_krw=5000),
                action="deploy",
                policy_version="policy-v1",
                operation=planned_operation(),
                consumed_at=NOW + timedelta(minutes=1),
            )

        self.assertEqual(store.get_deployment_plan(plan.id).state, PlanState.ISSUED)
        with self.assertRaisesRegex(Exception, "operation"):
            store.get_operation(OperationId("op-1"))

    def test_consume_plan_rejects_operation_actor_mismatch_without_side_effects(
        self,
    ) -> None:
        store = MemoryStore()
        plan = issued_plan()
        store.create_deployment_plan(plan)

        with self.assertRaisesRegex(Exception, "actor"):
            consume_plan_with_operation(
                store,
                plan_id=plan.id,
                actor_id=UserId("usr-1"),
                material=sample_material(),
                action="deploy",
                policy_version="policy-v1",
                operation=planned_operation(actor_id="usr-2"),
                consumed_at=NOW + timedelta(minutes=1),
            )

        self.assertEqual(store.get_deployment_plan(plan.id).state, PlanState.ISSUED)
        with self.assertRaisesRegex(Exception, "operation"):
            store.get_operation(OperationId("op-1"))

        consumed_plan, operation = consume_plan_with_operation(
            store,
            plan_id=plan.id,
            actor_id=UserId("usr-1"),
            material=sample_material(),
            action="deploy",
            policy_version="policy-v1",
            operation=planned_operation(),
            consumed_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(consumed_plan.state, PlanState.CONSUMED)
        self.assertEqual(operation.actor_id, UserId("usr-1"))

    def test_consume_plan_rejects_operation_action_mismatch_without_side_effects(
        self,
    ) -> None:
        store = MemoryStore()
        plan = issued_plan()
        store.create_deployment_plan(plan)

        with self.assertRaisesRegex(Exception, "action"):
            consume_plan_with_operation(
                store,
                plan_id=plan.id,
                actor_id=UserId("usr-1"),
                material=sample_material(),
                action="deploy",
                policy_version="policy-v1",
                operation=planned_operation(action="repair"),
                consumed_at=NOW + timedelta(minutes=1),
            )

        self.assertEqual(store.get_deployment_plan(plan.id).state, PlanState.ISSUED)
        with self.assertRaisesRegex(Exception, "operation"):
            store.get_operation(OperationId("op-1"))

        consumed_plan, operation = consume_plan_with_operation(
            store,
            plan_id=plan.id,
            actor_id=UserId("usr-1"),
            material=sample_material(),
            action="deploy",
            policy_version="policy-v1",
            operation=planned_operation(),
            consumed_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(consumed_plan.state, PlanState.CONSUMED)
        self.assertEqual(operation.action, "deploy")

    def test_contention_allows_one_first_distinct_consume_and_same_retry_reuses_it(
        self,
    ) -> None:
        store = MemoryStore()
        plan = issued_plan()
        store.create_deployment_plan(plan)

        def consume(operation: Operation) -> tuple[str, str]:
            try:
                consumed_plan, result_operation = consume_plan_with_operation(
                    store,
                    plan_id=plan.id,
                    actor_id=UserId("usr-1"),
                    material=sample_material(),
                    action="deploy",
                    policy_version="policy-v1",
                    operation=operation,
                    consumed_at=NOW + timedelta(minutes=1),
                )
                return (consumed_plan.state.value, result_operation.id)
            except Exception as exc:
                return (type(exc).__name__, str(exc))

        first = planned_operation(operation_id="op-1", idempotency_key="idem-1")
        distinct = planned_operation(operation_id="op-2", idempotency_key="idem-2")
        retry = planned_operation(operation_id="op-retry", idempotency_key="idem-1")

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_result, distinct_result = tuple(
                future.result()
                for future in (
                    executor.submit(consume, first),
                    executor.submit(consume, distinct),
                )
            )

        retry_result = consume(retry)

        self.assertIn(
            ("consumed", OperationId("op-1")),
            (first_result, distinct_result),
        )
        winner_operation_id = (
            first_result[1]
            if first_result[0] == "consumed"
            else distinct_result[1]
        )
        loser_result = (
            distinct_result if first_result[0] == "consumed" else first_result
        )
        self.assertEqual(loser_result[0], "VersionConflict")
        self.assertEqual(retry_result, ("consumed", winner_operation_id))


if __name__ == "__main__":
    unittest.main()
