"""Tests for strict per-workload runtime identity provisioning."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import dataclass, replace
from types import SimpleNamespace

from google.api_core.exceptions import AlreadyExists, NotFound
from google.iam.v1 import policy_pb2

from mim_control_plane.adapters.runtime_identity import RuntimeIdentityAdapter
from mim_control_plane.domain.models import WorkloadId
from mim_control_plane.ports.execution import ExecutionPlaneError

PROJECT_ID = "madup-prod1"
WORKLOAD_ID = WorkloadId("wrk-1")
SUFFIX = hashlib.sha256(str(WORKLOAD_ID).encode("utf-8")).hexdigest()[:12]
ACCOUNT_ID = f"mim-wrk-{SUFFIX}"
EMAIL = f"{ACCOUNT_ID}@{PROJECT_ID}.iam.gserviceaccount.com"
NAME = f"projects/{PROJECT_ID}/serviceAccounts/{EMAIL}"
DEPLOYER_MEMBER = (
    f"serviceAccount:mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com"
)
DISPLAY_NAME = f"MIM workload {SUFFIX}"
DESCRIPTION = f"MIM managed runtime identity for workload {SUFFIX}; no project roles."


@dataclass(frozen=True, slots=True)
class FakeServiceAccount:
    name: str = NAME
    project_id: str = PROJECT_ID
    unique_id: str = "123456789012345678901"
    email: str = EMAIL
    display_name: str = DISPLAY_NAME
    description: str = DESCRIPTION
    disabled: bool = False


class FakeIamAdminClient:
    def __init__(
        self,
        *,
        account: FakeServiceAccount | None = None,
        first_get_error: Exception | None = None,
        create_error: Exception | None = None,
        keys: tuple[object, ...] = (),
        policy: policy_pb2.Policy | None = None,
        set_policy_response: policy_pb2.Policy | None = None,
    ) -> None:
        self.account = account
        self.first_get_error = first_get_error
        self.create_error = create_error
        self.keys = keys
        self.policy = policy or policy_pb2.Policy(etag=b"policy-etag")
        self.set_policy_response = set_policy_response
        self.get_account_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.list_key_calls: list[dict[str, object]] = []
        self.get_policy_calls: list[dict[str, object]] = []
        self.set_policy_calls: list[dict[str, object]] = []

    def get_service_account(self, request: dict[str, object]) -> FakeServiceAccount:
        self.get_account_calls.append(dict(request))
        if self.first_get_error is not None:
            error = self.first_get_error
            self.first_get_error = None
            raise error
        if self.account is None:
            raise NotFound("missing")
        return self.account

    def create_service_account(self, request: dict[str, object]) -> FakeServiceAccount:
        self.create_calls.append(dict(request))
        if self.create_error is not None:
            raise self.create_error
        body = request["service_account"]
        assert isinstance(body, dict)
        self.account = FakeServiceAccount(
            display_name=str(body["display_name"]),
            description=str(body["description"]),
        )
        return self.account

    def list_service_account_keys(self, request: dict[str, object]) -> object:
        self.list_key_calls.append(dict(request))
        return SimpleNamespace(keys=self.keys)

    def get_iam_policy(self, request: dict[str, object]) -> policy_pb2.Policy:
        self.get_policy_calls.append(dict(request))
        observed = policy_pb2.Policy()
        observed.CopyFrom(self.policy)
        return observed

    def set_iam_policy(self, request: dict[str, object]) -> policy_pb2.Policy:
        self.set_policy_calls.append(dict(request))
        written = policy_pb2.Policy()
        candidate = self.set_policy_response or request["policy"]
        assert isinstance(candidate, policy_pb2.Policy)
        written.CopyFrom(candidate)
        self.policy = written
        return written


class FakeResourceManagerClient:
    def __init__(self, policy: policy_pb2.Policy | None = None) -> None:
        self.policy = policy or policy_pb2.Policy(etag=b"project-etag")
        self.calls: list[dict[str, object]] = []

    def get_iam_policy(self, request: dict[str, object]) -> policy_pb2.Policy:
        self.calls.append(dict(request))
        observed = policy_pb2.Policy()
        observed.CopyFrom(self.policy)
        return observed


def exact_act_as_policy(*, etag: bytes = b"policy-etag") -> policy_pb2.Policy:
    policy = policy_pb2.Policy(version=3, etag=etag)
    policy.bindings.add(
        role="roles/iam.serviceAccountUser",
        members=(DEPLOYER_MEMBER,),
    )
    return policy


def make_adapter(
    *,
    iam: FakeIamAdminClient,
    resources: FakeResourceManagerClient | None = None,
) -> RuntimeIdentityAdapter:
    return RuntimeIdentityAdapter(
        project_id=PROJECT_ID,
        iam_admin_client=iam,
        resource_manager_client=resources or FakeResourceManagerClient(),
        retry_sleeper=lambda _: None,
    )


class RuntimeIdentityAdapterTests(unittest.TestCase):
    def test_creates_exact_keyless_identity_and_authoritative_act_as_policy(
        self,
    ) -> None:
        iam = FakeIamAdminClient(first_get_error=NotFound("missing"))
        resources = FakeResourceManagerClient()
        adapter = make_adapter(iam=iam, resources=resources)

        self.assertEqual(adapter.ensure_exact(WORKLOAD_ID), EMAIL)

        self.assertEqual(
            iam.create_calls,
            [
                {
                    "name": f"projects/{PROJECT_ID}",
                    "account_id": ACCOUNT_ID,
                    "service_account": {
                        "display_name": DISPLAY_NAME,
                        "description": DESCRIPTION,
                    },
                }
            ],
        )
        self.assertEqual(
            iam.get_account_calls,
            [{"name": NAME}, {"name": NAME}],
        )
        self.assertEqual(
            resources.calls,
            [
                {
                    "resource": f"projects/{PROJECT_ID}",
                    "options": {"requested_policy_version": 3},
                }
            ],
        )
        self.assertEqual(
            iam.list_key_calls,
            [{"name": NAME, "key_types": ["USER_MANAGED"]}],
        )
        self.assertEqual(len(iam.set_policy_calls), 1)
        requested = iam.set_policy_calls[0]
        self.assertEqual(requested["resource"], NAME)
        policy = requested["policy"]
        assert isinstance(policy, policy_pb2.Policy)
        self.assertEqual(policy.etag, b"policy-etag")
        self.assertEqual(len(policy.bindings), 1)
        self.assertEqual(policy.bindings[0].role, "roles/iam.serviceAccountUser")
        self.assertEqual(list(policy.bindings[0].members), [DEPLOYER_MEMBER])
        self.assertFalse(policy.bindings[0].HasField("condition"))
        self.assertEqual(len(iam.get_policy_calls), 2)

    def test_existing_exact_identity_is_idempotent(self) -> None:
        iam = FakeIamAdminClient(
            account=FakeServiceAccount(),
            policy=exact_act_as_policy(),
        )
        adapter = make_adapter(iam=iam)

        self.assertEqual(adapter.ensure_exact(WORKLOAD_ID), EMAIL)

        self.assertEqual(iam.create_calls, [])
        self.assertEqual(len(iam.set_policy_calls), 1)

    def test_creation_race_is_idempotent_and_re_reads(self) -> None:
        iam = FakeIamAdminClient(
            account=FakeServiceAccount(),
            first_get_error=NotFound("stale read"),
            create_error=AlreadyExists("raced"),
            policy=exact_act_as_policy(),
        )
        adapter = make_adapter(iam=iam)

        self.assertEqual(adapter.ensure_exact(WORKLOAD_ID), EMAIL)

        self.assertEqual(len(iam.create_calls), 1)
        self.assertEqual(len(iam.get_account_calls), 2)

    def test_rejects_existing_identity_field_drift(self) -> None:
        drifted_accounts = (
            replace(FakeServiceAccount(), display_name="Human managed"),
            replace(FakeServiceAccount(), description="wrong"),
            replace(FakeServiceAccount(), disabled=True),
            replace(FakeServiceAccount(), email="other@example.com"),
            replace(FakeServiceAccount(), project_id="other-project"),
        )

        for account in drifted_accounts:
            with self.subTest(account=account):
                iam = FakeIamAdminClient(account=account)
                with self.assertRaises(ExecutionPlaneError):
                    make_adapter(iam=iam).ensure_exact(WORKLOAD_ID)
                self.assertEqual(iam.set_policy_calls, [])

    def test_rejects_any_project_role_for_runtime_principal(self) -> None:
        for member in (f"serviceAccount:{EMAIL}", "allAuthenticatedUsers"):
            with self.subTest(member=member):
                project_policy = policy_pb2.Policy()
                project_policy.bindings.add(
                    role="roles/viewer",
                    members=(member,),
                )
                iam = FakeIamAdminClient(account=FakeServiceAccount())
                resources = FakeResourceManagerClient(project_policy)

                with self.assertRaises(ExecutionPlaneError):
                    make_adapter(iam=iam, resources=resources).ensure_exact(WORKLOAD_ID)

                self.assertEqual(iam.list_key_calls, [])
                self.assertEqual(iam.set_policy_calls, [])

    def test_rejects_user_managed_keys(self) -> None:
        iam = FakeIamAdminClient(
            account=FakeServiceAccount(),
            keys=(SimpleNamespace(name=f"{NAME}/keys/key-1"),),
        )

        with self.assertRaises(ExecutionPlaneError):
            make_adapter(iam=iam).ensure_exact(WORKLOAD_ID)

        self.assertEqual(iam.set_policy_calls, [])

    def test_rejects_resource_policy_write_or_readback_drift(self) -> None:
        invalid = exact_act_as_policy(etag=b"next")
        invalid.bindings.add(
            role="roles/iam.serviceAccountTokenCreator",
            members=(DEPLOYER_MEMBER,),
        )
        iam = FakeIamAdminClient(
            account=FakeServiceAccount(),
            set_policy_response=invalid,
        )

        with self.assertRaises(ExecutionPlaneError):
            make_adapter(iam=iam).ensure_exact(WORKLOAD_ID)

        self.assertEqual(len(iam.set_policy_calls), 1)

    def test_constructor_and_workload_id_are_fail_closed(self) -> None:
        iam = FakeIamAdminClient(account=FakeServiceAccount())
        with self.assertRaises(ValueError):
            RuntimeIdentityAdapter(
                project_id="bad",
                iam_admin_client=iam,
                resource_manager_client=FakeResourceManagerClient(),
            )
        with self.assertRaises(ValueError):
            RuntimeIdentityAdapter(
                project_id=PROJECT_ID,
                iam_admin_client=None,
                resource_manager_client=FakeResourceManagerClient(),
            )

        adapter = make_adapter(iam=iam)
        for workload_id in (WorkloadId(""), WorkloadId(" wrk-1"), None):
            with self.subTest(workload_id=workload_id):
                with self.assertRaises(ExecutionPlaneError):
                    adapter.ensure_exact(workload_id)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
