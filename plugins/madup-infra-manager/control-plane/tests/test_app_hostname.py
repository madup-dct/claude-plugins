from __future__ import annotations

import dataclasses
import re
import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import (
    AppHostnameBindingState,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import WorkloadKind, WorkloadState
from mim_control_plane.ports.store import IdempotencyConflict, InvariantViolation
from mim_control_plane.services.app_hostname import (
    AppHostnameBindingService,
    build_app_hostname,
    validate_app_public_host,
    validate_service_resource,
    validate_service_uri,
)

NOW = datetime(2026, 8, 5, 3, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
REGION = "asia-northeast3"
RUN_APP_SUFFIX = ".run" + ".app"
SAFE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def workload(
    *,
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
    name: str = "Sample App",
    kind: WorkloadKind = WorkloadKind.NEXTJS,
    state: WorkloadState = WorkloadState.ACTIVE,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id="adm-1",
        name=name,
        kind=kind,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-1",
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=1),
        last_activity_at=NOW - timedelta(minutes=30),
    )


def service_resource(workload_id: str) -> str:
    service_suffix = (
        build_app_hostname("x", workload_id).split(".")[0].rsplit("-", 1)[-1]
    )
    return (
        f"projects/{PROJECT_ID}/locations/{REGION}/services/"
        f"mim-svc-{service_suffix}"
    )


def service_uri(workload_id: str) -> str:
    suffix = build_app_hostname("x", workload_id).split(".")[0].rsplit("-", 1)[-1]
    return f"https://mim-svc-{suffix}-abcdefg-an.a{RUN_APP_SUFFIX}"


class AppHostnameBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.service = AppHostnameBindingService(store=self.store)

    def test_slug_and_host_are_deterministic_dns_safe_and_first_level(self) -> None:
        host = build_app_hostname("My Sample App", "wrk-123")
        label = host.removesuffix(".madup.app")

        self.assertEqual(host, build_app_hostname("My Sample App", "wrk-123"))
        self.assertTrue(host.endswith(".madup.app"))
        self.assertEqual(host.count("."), 2)
        self.assertLessEqual(len(label), 63)
        self.assertRegex(label, SAFE_LABEL)
        self.assertRegex(label, r"-[0-9a-f]{12}$")
        self.assertNotEqual(label, "mim")
        self.assertEqual(validate_app_public_host(host), host)

        for invalid in ("madup.app", "deep.app.madup.app", "UPPER.madup.app"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_app_public_host(invalid)

    def test_create_is_idempotent_only_for_identical_material(self) -> None:
        target = workload(workload_id="wrk-1", name="North Star")
        binding = self.service.create_active_binding(
            workload=target,
            service_resource=service_resource("wrk-1"),
            service_uri=service_uri("wrk-1"),
            now=NOW,
        )
        replay = self.service.create_active_binding(
            workload=target,
            service_resource=service_resource("wrk-1"),
            service_uri=service_uri("wrk-1"),
            now=NOW + timedelta(seconds=5),
        )

        self.assertEqual(replay, binding)
        with self.assertRaises(IdempotencyConflict):
            self.service.create_active_binding(
                workload=target,
                service_resource=service_resource("wrk-1"),
                service_uri=(
                    f"https://mim-svc-{service_resource('wrk-1').rsplit('-', 1)[-1]}"
                    "-different-an.a.run.app"
                ),
                now=NOW + timedelta(seconds=10),
            )

    def test_binding_material_is_immutable_and_transitions_are_reviewed(self) -> None:
        target = workload(workload_id="wrk-1", name="North Star")
        binding = self.service.create_active_binding(
            workload=target,
            service_resource=service_resource("wrk-1"),
            service_uri=service_uri("wrk-1"),
            now=NOW,
        )

        disabled = self.service.transition_binding(
            public_host=binding.public_host,
            target_state=AppHostnameBindingState.DISABLED,
            now=NOW + timedelta(minutes=1),
        )
        restored = self.service.transition_binding(
            public_host=binding.public_host,
            target_state=AppHostnameBindingState.ACTIVE,
            now=NOW + timedelta(minutes=2),
        )
        retired = self.service.transition_binding(
            public_host=binding.public_host,
            target_state=AppHostnameBindingState.RETIRED,
            now=NOW + timedelta(minutes=3),
        )

        self.assertEqual(disabled.state, AppHostnameBindingState.DISABLED)
        self.assertEqual(restored.state, AppHostnameBindingState.ACTIVE)
        self.assertEqual(retired.state, AppHostnameBindingState.RETIRED)

        with self.assertRaises(ValueError):
            self.service.transition_binding(
                public_host=binding.public_host,
                target_state=AppHostnameBindingState.ACTIVE,
                now=NOW + timedelta(minutes=4),
            )

        with self.assertRaises(InvariantViolation):
            self.store.save_app_hostname_binding(
                dataclasses.replace(
                    self.store.get_app_hostname_binding(binding.public_host),
                    owner_id=UserId("usr-2"),
                    updated_at=NOW + timedelta(minutes=4),
                    version=retired.version + 1,
                ),
                expected_version=retired.version,
            )

    def test_exact_service_resource_and_service_uri_are_required(self) -> None:
        target = workload(workload_id="wrk-1", name="North Star")

        with self.assertRaises(ValueError):
            self.service.create_active_binding(
                workload=target,
                service_resource=(
                    f"projects/{PROJECT_ID}/locations/us-central1/services/"
                    "mim-svc-deadbeefcafe"
                ),
                service_uri=service_uri("wrk-1"),
                now=NOW,
            )
        with self.assertRaises(ValueError):
            self.service.create_active_binding(
                workload=target,
                service_resource=service_resource("wrk-1"),
                service_uri=(
                    "https://mim-svc-deadbeefcafe-an.a" + RUN_APP_SUFFIX + "/path"
                ),
                now=NOW,
            )
        with self.assertRaises(ValueError):
            self.service.create_active_binding(
                workload=target,
                service_resource=service_resource("wrk-1"),
                service_uri="https://example.com",
                now=NOW,
            )

    def test_service_uri_must_match_the_reviewed_service_resource_name(self) -> None:
        target = workload(workload_id="wrk-1", name="North Star")
        reviewed_resource = validate_service_resource(
            service_resource=service_resource("wrk-1"),
            workload_id=str(target.id),
        )

        with self.assertRaises(ValueError):
            validate_service_uri(
                service_uri=service_uri("wrk-2"),
                workload_id=str(target.id),
                service_resource=reviewed_resource,
            )


if __name__ == "__main__":
    unittest.main()
