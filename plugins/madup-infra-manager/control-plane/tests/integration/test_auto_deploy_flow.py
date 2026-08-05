from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import unittest
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import cast

from mim_control_plane.adapters.fake_execution import FakeDeploymentQueue
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.dashboard import ControlPlaneReadService, ReadNotFound
from mim_control_plane.domain.models import (
    OperationId,
    OrgCostGuard,
    RepositoryAdmission,
    RepositoryAdmissionId,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    OperationState,
    RepositoryAdmissionState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.execution import PrivateDeployEnqueuer
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.deployments import DeploymentDenied, DeploymentService
from mim_control_plane.services.render import DesiredStateRenderContext
from mim_control_plane.services.repository_admission import SelectedRepositoryPolicy

NOW = datetime(2026, 8, 4, 5, 0, 0, tzinfo=UTC)
WEBHOOK_SECRET = b"w" * 32
DELIVERY_ID = "44444444-4444-4444-4444-444444444444"


class SourceByAdmission:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch_snapshot(
        self,
        admission: RepositoryAdmission,
    ) -> MappingProxyType[str, bytes]:
        self.calls.append((str(admission.id), admission.admitted_sha))
        return MappingProxyType(
            {
                "app.py": b"import streamlit as st\nst.write('auto')\n",
                "requirements.txt": b"streamlit==1.40.0\n",
            }
        )


def seed_store(*, enabled: bool = True) -> tuple[MemoryStore, Workload]:
    store = MemoryStore()
    store.create_org_cost_guard(
        OrgCostGuard(
            evaluated_at=NOW,
            latest_usage_collected_at=NOW,
            emergency_stop=False,
            org_policy_cost_krw=0,
        )
    )
    owner = store.create_user(
        User(
            id=UserId("usr-1"),
            email="person@madup.com",
            role=UserRole.USER,
            state=UserState.ACTIVE,
            groups=frozenset({"mim-users"}),
            identity_synced_at=NOW,
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
        )
    )
    admission = store.create_repository_admission(
        RepositoryAdmission(
            id=RepositoryAdmissionId("repo-old"),
            repository_numeric_id=123,
            owner="madupmarketing",
            name="streamlit-app",
            installation_id=456,
            state=RepositoryAdmissionState.ADMITTED,
            admitted_sha="a" * 40,
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
        )
    )
    workload = store.create_workload(
        Workload(
            id=WorkloadId("wrk-1"),
            owner_id=owner.id,
            repository_admission_id=admission.id,
            name="streamlit-app",
            kind=WorkloadKind.STREAMLIT,
            state=WorkloadState.ACTIVE,
            source_sha=admission.admitted_sha,
            desired_manifest_hash="manifest-old",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
            last_healthy_image_digest="sha256:" + "f" * 64,
            auto_deploy_enabled=enabled,
            auto_deploy_ref="refs/heads/main" if enabled else None,
        )
    )
    return store, workload


def push_body(*, sha: str = "b" * 40, ref: str = "refs/heads/main") -> bytes:
    return json.dumps(
        {
            "after": sha,
            "deleted": False,
            "head_commit": {"id": sha},
            "installation": {"id": 456},
            "ref": ref,
            "repository": {
                "fork": False,
                "full_name": "madupmarketing/streamlit-app",
                "id": 123,
                "name": "streamlit-app",
                "owner": {"login": "madupmarketing"},
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def signature(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()


def service_for(
    *,
    store: MemoryStore,
    source: SourceByAdmission,
    queue: FakeDeploymentQueue,
) -> DeploymentService:
    return DeploymentService(
        store=store,
        source=source,
        enqueuer=PrivateDeployEnqueuer(queue=queue),
        render_context=DesiredStateRenderContext(
            project_id="mim-prod-123456",
            key_id="deploy-key-1",
        ),
        signing_key=b"s" * 32,
        clock=lambda: NOW + timedelta(minutes=1),
        id_factory=lambda prefix: f"manual-{prefix}",
        github_policy=SelectedRepositoryPolicy(
            allowed_repository_ids=frozenset({123}),
            installation_id=456,
        ),
        github_webhook_secret=WEBHOOK_SECRET,
    )


class AutoDeployFlowTests(unittest.TestCase):
    def test_exact_delivery_replay_survives_unrelated_workload_version_advance(
        self,
    ) -> None:
        store, original = seed_store()
        queue = FakeDeploymentQueue()
        service = service_for(
            store=store,
            source=SourceByAdmission(),
            queue=queue,
        )
        body = push_body()
        first = service.deploy_from_github_webhook(
            body=body,
            signature_header=signature(body),
            event_name="push",
            delivery_id=DELIVERY_ID,
        )
        advanced = store.get_workload(original.id)
        refreshed = store.save_workload(
            dataclasses.replace(
                advanced,
                last_healthy_image_digest="sha256:" + "e" * 64,
                updated_at=NOW + timedelta(minutes=1),
                version=advanced.version + 1,
            ),
            expected_version=advanced.version,
        )
        with self.assertRaises(DeploymentDenied):
            service.deploy_from_github_webhook(
                body=body,
                signature_header=signature(body),
                event_name="push",
                delivery_id=DELIVERY_ID,
            )
        operation = store.get_operation(OperationId(cast(str, first["operation_id"])))
        for state in (
            OperationState.BUILDING,
            OperationState.DEPLOYING,
            OperationState.VERIFYING,
            OperationState.SUCCEEDED,
        ):
            next_operation = operation.transition(
                state,
                at=NOW + timedelta(minutes=1),
            )
            operation = store.save_operation(
                next_operation,
                expected_version=operation.version,
            )

        replay = service.deploy_from_github_webhook(
            body=body,
            signature_header=signature(body),
            event_name="push",
            delivery_id=DELIVERY_ID,
        )

        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["queued"])
        self.assertEqual(replay["state"], OperationState.SUCCEEDED.value)
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(store.get_workload(original.id), refreshed)

    def test_broken_candidate_does_not_block_valid_repository_delivery(self) -> None:
        store, valid = seed_store()
        broken = store.create_workload(
            Workload(
                id=WorkloadId("wrk-broken"),
                owner_id=valid.owner_id,
                repository_admission_id=RepositoryAdmissionId("repo-missing"),
                name="broken-app",
                kind=WorkloadKind.STREAMLIT,
                state=WorkloadState.ACTIVE,
                source_sha="d" * 40,
                desired_manifest_hash="manifest-broken",
                created_at=NOW - timedelta(days=2),
                updated_at=NOW,
                auto_deploy_enabled=True,
                auto_deploy_ref="refs/heads/main",
            )
        )
        queue = FakeDeploymentQueue()
        body = push_body()

        result = service_for(
            store=store,
            source=SourceByAdmission(),
            queue=queue,
        ).deploy_from_github_webhook(
            body=body,
            signature_header=signature(body),
            event_name="push",
            delivery_id="33333333-3333-3333-3333-333333333333",
        )

        self.assertEqual(result["workload_id"], str(valid.id))
        self.assertEqual(store.get_workload(broken.id), broken)
        self.assertEqual(
            queue.get(OperationId(cast(str, result["operation_id"]))).workload_id,
            valid.id,
        )

    def test_previous_month_cost_does_not_block_current_month_auto_deploy(self) -> None:
        store, current = seed_store()
        store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId("use-previous-month"),
                owner_id=current.owner_id,
                workload_id=current.id,
                service_category="cloud_run",
                estimated_cost_krw=900,
                finalized_cost_krw=900,
                confidence=UsageConfidence.FINALIZED,
                collected_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
            )
        )
        queue = FakeDeploymentQueue()
        service = service_for(
            store=store,
            source=SourceByAdmission(),
            queue=queue,
        )
        body = push_body()

        result = service.deploy_from_github_webhook(
            body=body,
            signature_header=signature(body),
            event_name="push",
            delivery_id="77777777-7777-7777-7777-777777777777",
        )

        self.assertFalse(result["replayed"])
        self.assertEqual(result["workload_id"], str(current.id))
        self.assertEqual(
            queue.get(OperationId(cast(str, result["operation_id"]))).workload_id,
            current.id,
        )

    def test_verified_new_default_branch_sha_advances_and_replays_exact_task(
        self,
    ) -> None:
        store, current = seed_store()
        source = SourceByAdmission()
        queue = FakeDeploymentQueue()
        service = service_for(store=store, source=source, queue=queue)
        body = push_body()

        first = service.deploy_from_github_webhook(
            body=body,
            signature_header=signature(body),
            event_name="push",
            delivery_id=DELIVERY_ID,
        )
        replay = service.deploy_from_github_webhook(
            body=body,
            signature_header=signature(body),
            event_name="push",
            delivery_id=DELIVERY_ID,
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["operation_id"], replay["operation_id"])
        advanced = store.get_workload(current.id)
        self.assertEqual(advanced.source_sha, "b" * 40)
        self.assertEqual(advanced.version, current.version + 1)
        self.assertTrue(advanced.auto_deploy_enabled)
        self.assertEqual(advanced.auto_deploy_ref, "refs/heads/main")
        task = queue.get(OperationId(cast(str, first["operation_id"])))
        self.assertEqual(task.expected_source_sha, advanced.source_sha)
        self.assertEqual(task.expected_workload_version, advanced.version)
        self.assertEqual(task.admission_id, advanced.repository_admission_id)
        self.assertEqual(store.get_deploy_task(task.operation_id), task)
        self.assertEqual(len(store.list_audit_events()), 1)
        owner = store.get_user(current.owner_id)
        owner_view = ControlPlaneReadService(
            store=store,
            clock=lambda: NOW + timedelta(minutes=1),
        ).get_operation(
            principal=AuthenticatedPrincipal(
                user_id=owner.id,
                email=owner.email,
                role=owner.role,
            ),
            operation_id=str(first["operation_id"]),
        )
        self.assertEqual(
            owner_view["operation"]["id"],
            first["operation_id"],
        )

        other = store.create_user(
            User(
                id=UserId("usr-2"),
                email="other@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset({"mim-users"}),
                identity_synced_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        with self.assertRaises(ReadNotFound):
            ControlPlaneReadService(
                store=store,
                clock=lambda: NOW + timedelta(minutes=1),
            ).get_operation(
                principal=AuthenticatedPrincipal(
                    user_id=other.id,
                    email=other.email,
                    role=other.role,
                ),
                operation_id=str(first["operation_id"]),
            )

        changed = push_body(sha="c" * 40)
        with self.assertRaises(DeploymentDenied):
            service.deploy_from_github_webhook(
                body=changed,
                signature_header=signature(changed),
                event_name="push",
                delivery_id=DELIVERY_ID,
            )
        self.assertEqual(store.get_workload(current.id), advanced)

    def test_disabled_wrong_ref_signature_and_cost_block_before_mutation(self) -> None:
        disabled_store, disabled = seed_store(enabled=False)
        disabled_queue = FakeDeploymentQueue()
        source = SourceByAdmission()
        disabled_service = service_for(
            store=disabled_store,
            source=source,
            queue=disabled_queue,
        )
        body = push_body()
        denied_cases = (
            {
                "body": body,
                "signature_header": "sha256=" + "0" * 64,
                "event_name": "push",
                "delivery_id": DELIVERY_ID,
            },
            {
                "body": push_body(ref="refs/heads/develop"),
                "signature_header": signature(
                    push_body(ref="refs/heads/develop")
                ),
                "event_name": "push",
                "delivery_id": DELIVERY_ID,
            },
            {
                "body": body,
                "signature_header": signature(body),
                "event_name": "push",
                "delivery_id": DELIVERY_ID,
            },
        )
        for case in denied_cases:
            with self.subTest(case=case["event_name"]):
                with self.assertRaises(DeploymentDenied):
                    disabled_service.deploy_from_github_webhook(
                        body=cast(bytes, case["body"]),
                        signature_header=cast(str, case["signature_header"]),
                        event_name=cast(str, case["event_name"]),
                        delivery_id=cast(str, case["delivery_id"]),
                    )
        self.assertEqual(disabled_store.get_workload(disabled.id), disabled)

        costly_store, costly = seed_store()
        costly_store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId("use-1"),
                owner_id=costly.owner_id,
                workload_id=costly.id,
                service_category="cloud_run",
                estimated_cost_krw=900,
                finalized_cost_krw=None,
                confidence=UsageConfidence.ESTIMATED,
                collected_at=NOW,
            )
        )
        costly_queue = FakeDeploymentQueue()
        costly_service = service_for(
            store=costly_store,
            source=SourceByAdmission(),
            queue=costly_queue,
        )
        with self.assertRaises(DeploymentDenied):
            costly_service.deploy_from_github_webhook(
                body=body,
                signature_header=signature(body),
                event_name="push",
                delivery_id="55555555-5555-5555-5555-555555555555",
            )
        self.assertEqual(costly_store.get_workload(costly.id), costly)


if __name__ == "__main__":
    unittest.main()
