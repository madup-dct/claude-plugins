"""Tests for the strict IAP access adapter."""

from __future__ import annotations

import copy
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from mim_control_plane.adapters.iap_access import (
    IapAccessAdapter,
    StoreIapPrincipalResolver,
)
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.config import REGION
from mim_control_plane.domain.models import User, UserId
from mim_control_plane.domain.states import UserRole, UserState

PROJECT_ID = "mim-prod-123456"
PROJECT_NUMBER = "123456789012"
SERVICE_NAME = (
    "projects/mim-prod-123456/locations/asia-northeast3/services/mim-svc-wrk-1"
)
SERVICE_ID = "mim-svc-wrk-1"
OWNER_ID = "usr-1"
ACCESSOR_ROLE = "roles/iap.httpsResourceAccessor"
GENERIC_ERROR = "IAP access policy reconciliation failed."
NOW = datetime(2026, 8, 4, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self,
        payload: Mapping[str, object],
        *,
        status_code: int = 200,
        error: Exception | None = None,
    ) -> None:
        self._payload = dict(payload)
        self.status_code = status_code
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error
        if self.status_code >= 400:
            raise RuntimeError("http failure")

    def json(self) -> dict[str, object]:
        return copy.deepcopy(self._payload)


class FakeSession:
    def __init__(
        self,
        responses: Sequence[FakeResponse] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._responses = list(responses or ())
        self._error = error
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        timeout: float,
    ) -> FakeResponse:
        self.calls.append(
            {
                "url": url,
                "json": copy.deepcopy(dict(json)),
                "timeout": timeout,
            }
        )
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError("unexpected HTTP call")
        return self._responses.pop(0)


class FakeResolver:
    def __init__(
        self,
        result: Mapping[str, object] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.result = result or {
            "owner_member": "user:owner@madup.com",
            "admin_members": (
                "group:mim-admins@madup.com",
                "user:admin@madup.com",
            ),
        }

    def __call__(self, *, workload_owner_id: str) -> Mapping[str, object]:
        self.calls.append(workload_owner_id)
        return copy.deepcopy(dict(self.result))


def policy_payload(
    *,
    etag: str = "etag-1",
    bindings: Sequence[Mapping[str, object]] | None = None,
    version: int = 3,
    audit_configs: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": version,
        "etag": etag,
        "bindings": copy.deepcopy(list(bindings or ())),
    }
    if audit_configs is not None:
        payload["auditConfigs"] = copy.deepcopy(list(audit_configs))
    return payload


def accessor_binding(
    *members: str,
    condition: Mapping[str, object] | None = None,
) -> dict[str, object]:
    binding: dict[str, object] = {
        "role": ACCESSOR_ROLE,
        "members": list(members),
    }
    if condition is not None:
        binding["condition"] = dict(condition)
    return binding


class IapAccessAdapterTests(unittest.TestCase):
    def test_store_resolver_uses_only_active_central_owner_and_exact_admins(
        self,
    ) -> None:
        store = MemoryStore()
        store.create_user(
            User(
                id=UserId(OWNER_ID),
                email="owner@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset({"mim-users@madup.com"}),
                identity_synced_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        resolver = StoreIapPrincipalResolver(
            store=store,
            admin_members=("group:mim-admins@madup.com",),
        )

        self.assertEqual(
            resolver(workload_owner_id=OWNER_ID),
            {
                "owner_member": "user:owner@madup.com",
                "admin_members": ("group:mim-admins@madup.com",),
            },
        )
        with self.assertRaises(ValueError):
            resolver(workload_owner_id="owner@madup.com")

    def test_store_resolver_rejects_inactive_or_external_owner(self) -> None:
        for state, email in (
            (UserState.SUSPENDED, "owner@madup.com"),
            (UserState.OFFBOARDED, "owner@madup.com"),
            (UserState.ACTIVE, "owner@example.com"),
        ):
            with self.subTest(state=state, email=email):
                store = MemoryStore()
                store.create_user(
                    User(
                        id=UserId(OWNER_ID),
                        email=email,
                        role=UserRole.USER,
                        state=state,
                        groups=frozenset(),
                        identity_synced_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                resolver = StoreIapPrincipalResolver(
                    store=store,
                    admin_members=("group:mim-admins@madup.com",),
                )

                with self.assertRaises(ValueError):
                    resolver(workload_owner_id=OWNER_ID)

    def test_constructor_requires_explicit_exact_inputs(self) -> None:
        session = FakeSession()
        resolver = FakeResolver()

        with self.assertRaises(ValueError):
            IapAccessAdapter(
                project_id="bad",
                project_number=PROJECT_NUMBER,
                region=REGION,
                session=session,
                principal_resolver=resolver,
            )
        with self.assertRaises(ValueError):
            IapAccessAdapter(
                project_id=PROJECT_ID,
                project_number=" 123456789012 ",
                region=REGION,
                session=session,
                principal_resolver=resolver,
            )
        with self.assertRaises(ValueError):
            IapAccessAdapter(
                project_id=PROJECT_ID,
                project_number="0123456789012",
                region=REGION,
                session=session,
                principal_resolver=resolver,
            )
        with self.assertRaises(ValueError):
            IapAccessAdapter(
                project_id=PROJECT_ID,
                project_number="0",
                region=REGION,
                session=session,
                principal_resolver=resolver,
            )
        with self.assertRaises(ValueError):
            IapAccessAdapter(
                project_id=PROJECT_ID,
                project_number=PROJECT_NUMBER,
                region="us-central1",
                session=session,
                principal_resolver=resolver,
            )

    def test_ensure_exact_access_posts_exact_urls_policy_and_etag(self) -> None:
        current_policy = policy_payload(
            etag="etag-1",
            bindings=(
                {
                    "role": "roles/viewer",
                    "members": ["group:viewers@madup.com"],
                    "condition": {
                        "title": "viewer-boundary",
                        "expression": (
                            "request.time < "
                            "timestamp('2030-01-01T00:00:00Z')"
                        ),
                    },
                },
                accessor_binding("user:stale-owner@madup.com"),
                {
                    "role": "roles/iap.admin",
                    "members": ["user:stale-admin@madup.com"],
                },
            ),
            audit_configs=(
                {
                    "service": "allServices",
                    "auditLogConfigs": [{"logType": "ADMIN_READ"}],
                },
            ),
        )
        requested_policy = policy_payload(
            etag="etag-1",
            bindings=(
                accessor_binding(
                    "user:owner@madup.com",
                    "group:mim-admins@madup.com",
                    "user:admin@madup.com",
                ),
            )
        )
        set_policy = policy_payload(
            etag="etag-2",
            bindings=(
                accessor_binding(
                    "group:mim-admins@madup.com",
                    "user:admin@madup.com",
                    "user:owner@madup.com",
                ),
            ),
        )
        readback_policy = copy.deepcopy(set_policy)
        session = FakeSession(
            responses=(
                FakeResponse(current_policy),
                FakeResponse(set_policy),
                FakeResponse(readback_policy),
            )
        )
        resolver = FakeResolver()
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=session,
            principal_resolver=resolver,
            timeout=12.5,
        )

        adapter.ensure_exact_access(SERVICE_NAME, OWNER_ID)

        resource = (
            f"projects/{PROJECT_NUMBER}/iap_web/cloud_run-{REGION}/services/{SERVICE_ID}"
        )
        self.assertEqual(
            session.calls,
            [
                {
                    "url": f"https://iap.googleapis.com/v1/{resource}:getIamPolicy",
                    "json": {"options": {"requestedPolicyVersion": 3}},
                    "timeout": 12.5,
                },
                {
                    "url": f"https://iap.googleapis.com/v1/{resource}:setIamPolicy",
                    "json": {"policy": requested_policy},
                    "timeout": 12.5,
                },
                {
                    "url": f"https://iap.googleapis.com/v1/{resource}:getIamPolicy",
                    "json": {"options": {"requestedPolicyVersion": 3}},
                    "timeout": 12.5,
                },
            ],
        )
        self.assertEqual(resolver.calls, [OWNER_ID])

    def test_ensure_exact_access_initializes_policy_with_no_bindings_field(
        self,
    ) -> None:
        current_policy: dict[str, object] = {"version": 1, "etag": "etag-1"}
        expected_binding = accessor_binding(
            "user:owner@madup.com",
            "group:mim-admins@madup.com",
            "user:admin@madup.com",
        )
        set_policy: dict[str, object] = {
            "version": 1,
            "etag": "etag-2",
            "bindings": [expected_binding],
        }
        session = FakeSession(
            responses=(
                FakeResponse(current_policy),
                FakeResponse(set_policy),
                FakeResponse(set_policy),
            )
        )
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=session,
            principal_resolver=FakeResolver(),
        )

        adapter.ensure_exact_access(SERVICE_NAME, OWNER_ID)

        self.assertEqual(
            session.calls[1]["json"],
            {
                "policy": {
                    "version": 1,
                    "etag": "etag-1",
                    "bindings": [expected_binding],
                }
            },
        )

    def test_ensure_exact_access_denies_foreign_service_before_http(self) -> None:
        session = FakeSession()
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=session,
            principal_resolver=FakeResolver(),
        )

        with self.assertRaises(RuntimeError) as raised:
            adapter.ensure_exact_access(
                "projects/other-project/locations/asia-northeast3/services/mim-svc-wrk-1",
                OWNER_ID,
            )

        self.assertEqual(str(raised.exception), GENERIC_ERROR)
        self.assertEqual(session.calls, [])

        with self.assertRaises(RuntimeError):
            adapter.ensure_exact_access(
                (
                    "projects/mim-prod-123456/locations/asia-northeast3/services/"
                    "mim-svc-wrk-1?bad=true"
                ),
                OWNER_ID,
            )

        self.assertEqual(session.calls, [])

    def test_ensure_exact_access_rejects_invalid_resolver_structure_before_http(
        self,
    ) -> None:
        session = FakeSession()
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=session,
            principal_resolver=FakeResolver(
                {
                    "owner_member": "user:owner@madup.com",
                    "admin_members": ("group:mim-admins@madup.com",),
                    "email": "owner@madup.com",
                }
            ),
        )

        with self.assertRaises(RuntimeError):
            adapter.ensure_exact_access(SERVICE_NAME, OWNER_ID)

        self.assertEqual(session.calls, [])

    def test_verify_exact_access_accepts_reordered_unique_accessor_members(
        self,
    ) -> None:
        session = FakeSession(
            responses=(
                FakeResponse(
                    policy_payload(
                        bindings=(
                            accessor_binding(
                                "group:mim-admins@madup.com",
                                "user:admin@madup.com",
                                "user:owner@madup.com",
                            ),
                        )
                    )
                ),
            )
        )
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=session,
            principal_resolver=FakeResolver(),
        )

        self.assertTrue(adapter.verify_exact_access(SERVICE_NAME, OWNER_ID))

    def test_verify_exact_access_denies_invalid_accessor_shapes(self) -> None:
        resolver = FakeResolver()
        cases = (
            policy_payload(),
            policy_payload(
                bindings=(
                    accessor_binding(
                        "user:not-the-owner@madup.com",
                        "group:mim-admins@madup.com",
                        "user:admin@madup.com",
                    ),
                )
            ),
            policy_payload(
                bindings=(
                    accessor_binding(
                        "user:owner@madup.com",
                        "group:mim-admins@madup.com",
                        "user:admin@madup.com",
                        "group:extra-admins@madup.com",
                    ),
                )
            ),
            policy_payload(
                bindings=(
                    {
                        "role": ACCESSOR_ROLE,
                        "members": [
                            "user:owner@madup.com",
                            "group:mim-admins@madup.com",
                            "user:admin@madup.com",
                        ],
                        "title": "extra-structure",
                    },
                )
            ),
            policy_payload(
                bindings=(
                    accessor_binding(
                        "user:owner@madup.com",
                        "group:mim-admins@madup.com",
                        condition={"title": "temporary", "expression": "true"},
                    ),
                )
            ),
            policy_payload(
                bindings=(
                    accessor_binding(
                        "user:owner@madup.com",
                        "group:mim-admins@madup.com",
                    ),
                    accessor_binding("user:admin@madup.com"),
                )
            ),
            policy_payload(
                bindings=(
                    accessor_binding(
                        "user:owner@madup.com",
                        "group:mim-admins@madup.com",
                        "user:admin@madup.com",
                    ),
                    {
                        "role": "roles/iap.admin",
                        "members": ["user:admin@madup.com"],
                    },
                )
            ),
            policy_payload(
                bindings=(
                    accessor_binding(
                        "user:owner@madup.com",
                        "group:mim-admins@madup.com",
                        "user:admin@madup.com",
                    ),
                    {
                        "role": "projects/mim-prod-123456/roles/iapPolicyAdmin",
                        "members": ["user:admin@madup.com"],
                    },
                )
            ),
            policy_payload(
                bindings=(
                    accessor_binding(
                        "user:owner@madup.com",
                        "group:mim-admins@madup.com",
                        "user:admin@madup.com",
                    ),
                ),
                audit_configs=(
                    {
                        "service": "allServices",
                        "auditLogConfigs": [{"logType": "ADMIN_READ"}],
                    },
                ),
            ),
        )

        for current_policy in cases:
            session = FakeSession(responses=(FakeResponse(current_policy),))
            adapter = IapAccessAdapter(
                project_id=PROJECT_ID,
                project_number=PROJECT_NUMBER,
                region=REGION,
                session=session,
                principal_resolver=resolver,
            )
            with self.subTest(policy=current_policy):
                self.assertFalse(adapter.verify_exact_access(SERVICE_NAME, OWNER_ID))

    def test_ensure_exact_access_rejects_set_response_drift(self) -> None:
        current_policy = policy_payload(
            bindings=(accessor_binding("user:stale-owner@madup.com"),)
        )
        drifted_policy = policy_payload(
            bindings=(
                accessor_binding(
                    "user:owner@madup.com",
                    "group:mim-admins@madup.com",
                    "user:admin@madup.com",
                    "group:extra-admins@madup.com",
                ),
            )
        )
        session = FakeSession(
            responses=(
                FakeResponse(current_policy),
                FakeResponse(drifted_policy),
            )
        )
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=session,
            principal_resolver=FakeResolver(),
        )

        with self.assertRaises(RuntimeError) as raised:
            adapter.ensure_exact_access(SERVICE_NAME, OWNER_ID)

        self.assertEqual(str(raised.exception), GENERIC_ERROR)

    def test_ensure_exact_access_rejects_readback_drift(self) -> None:
        current_policy = policy_payload(
            bindings=(accessor_binding("user:stale-owner@madup.com"),)
        )
        desired_policy = policy_payload(
            bindings=(
                accessor_binding(
                    "user:owner@madup.com",
                    "group:mim-admins@madup.com",
                    "user:admin@madup.com",
                ),
            )
        )
        set_policy = copy.deepcopy(desired_policy)
        set_policy["etag"] = "etag-2"
        drifted_readback = policy_payload(
            etag="etag-2",
            bindings=(
                accessor_binding(
                    "user:owner@madup.com",
                    "group:mim-admins@madup.com",
                    "group:extra-admins@madup.com",
                ),
            )
        )
        session = FakeSession(
            responses=(
                FakeResponse(current_policy),
                FakeResponse(set_policy),
                FakeResponse(drifted_readback),
            )
        )
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=session,
            principal_resolver=FakeResolver(),
        )

        with self.assertRaises(RuntimeError):
            adapter.ensure_exact_access(SERVICE_NAME, OWNER_ID)

    def test_transport_errors_fail_closed(self) -> None:
        adapter = IapAccessAdapter(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            session=FakeSession(error=TimeoutError("timed out")),
            principal_resolver=FakeResolver(),
        )

        with self.assertRaises(RuntimeError) as raised:
            adapter.ensure_exact_access(SERVICE_NAME, OWNER_ID)

        self.assertEqual(str(raised.exception), GENERIC_ERROR)
        self.assertFalse(adapter.verify_exact_access(SERVICE_NAME, OWNER_ID))


if __name__ == "__main__":
    unittest.main()
