from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TEST_ROOT.parent / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# ruff: noqa: E402

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.config import Settings
from mim_control_plane.domain.models import (
    RepositoryAdmissionId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from tests.fakes import build_startup_mapping

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
CENTRAL_PROJECT = "mim-prod-123456"


def settings(*, project_id: str = CENTRAL_PROJECT) -> Settings:
    return Settings.from_mapping(build_startup_mapping(MIM_PROJECT_ID=project_id))


def user(*, user_id: str = "usr-1") -> User:
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=UserRole.USER,
        state=UserState.ACTIVE,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=90),
        updated_at=NOW - timedelta(days=1),
        version=1,
    )


def workload(*, workload_id: str, owner_id: str = "usr-1") -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name=workload_id,
        kind=WorkloadKind.NEXTJS,
        state=WorkloadState.ACTIVE,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=90),
        updated_at=NOW - timedelta(days=1),
        last_activity_at=NOW - timedelta(days=1),
        version=1,
    )


@dataclass
class FakeSnapshot:
    id: str
    exists: bool
    data: dict[str, object] | None

    def to_dict(self) -> dict[str, object] | None:
        return self.data


class FakeDocument:
    def __init__(self, collection: "FakeCollection", document_id: str) -> None:
        self._collection = collection
        self.id = document_id

    def get(self, *, transaction: object | None = None) -> FakeSnapshot:
        del transaction
        if self._collection.get_error is not None:
            raise self._collection.get_error
        data = self._collection.documents.get(self.id)
        return FakeSnapshot(self.id, data is not None, data)

    def set(self, data: dict[str, object]) -> None:
        self._collection.documents[self.id] = dict(data)

    def delete(self) -> None:
        self._collection.documents.pop(self.id, None)


class FakeCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, object]] = {}
        self.get_error: Exception | None = None

    def document(self, document_id: str) -> FakeDocument:
        return FakeDocument(self, document_id)


class FakeTransaction:
    def set(self, reference: FakeDocument, data: dict[str, object]) -> None:
        reference.set(data)

    def delete(self, reference: FakeDocument) -> None:
        reference.delete()


class FakeFirestoreClient:
    def __init__(self, *, project: str, database: str = "(default)") -> None:
        self.project = project
        self.database = database
        self.collections: dict[str, FakeCollection] = {}

    def collection(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())

    def run_transaction(self, operation: object) -> object:
        return operation(FakeTransaction())


def hold_document_id(user_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:lifecycle-hold-set:v1\x00")
    digest.update(user_id.encode("utf-8"))
    return digest.hexdigest()


class MaintenanceStateAdapterTests(unittest.TestCase):
    def test_overlap_lease_acquires_blocks_until_release_and_hashes_token(self) -> None:
        from mim_control_plane.adapters.maintenance_state import (
            FirestoreNamedOverlapLease,
        )

        client = FakeFirestoreClient(project=CENTRAL_PROJECT)
        lease = FirestoreNamedOverlapLease(
            settings=settings(),
            lease_name="lifecycle-maintenance",
            client_factory=lambda **_: client,
            credentials_loader=lambda: object(),
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
            token_factory=lambda: "opaque-token-1",
        )

        claim = lease.try_acquire(now=NOW, duration=timedelta(minutes=10))

        self.assertIsNotNone(claim)
        self.assertEqual(claim.expires_at, NOW + timedelta(minutes=10))
        self.assertIsNone(
            lease.try_acquire(
                now=NOW + timedelta(minutes=1),
                duration=timedelta(minutes=10),
            )
        )
        stored = next(
            iter(client.collection("maintenance_overlap_leases").documents.values())
        )
        self.assertNotIn("opaque-token-1", str(stored))
        self.assertIn("token_hash", stored)
        lease.release(claim)
        self.assertEqual(client.collection("maintenance_overlap_leases").documents, {})
        lease.release(claim)
        self.assertEqual(client.collection("maintenance_overlap_leases").documents, {})

    def test_overlap_lease_replaces_expired_record_and_rejects_wrong_claim(
        self,
    ) -> None:
        from mim_control_plane.adapters.maintenance_state import (
            FirestoreNamedOverlapLease,
            MaintenanceStateError,
        )

        client = FakeFirestoreClient(project=CENTRAL_PROJECT)
        lease = FirestoreNamedOverlapLease(
            settings=settings(),
            lease_name="usage-ingest",
            client_factory=lambda **_: client,
            credentials_loader=lambda: object(),
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
            token_factory=lambda: "opaque-token-2",
        )
        collection = client.collection("maintenance_overlap_leases")
        doc_id = next(iter([lease._reference.id]))  # type: ignore[attr-defined]
        collection.documents[doc_id] = {
            "schema_version": 1,
            "lease_name": "usage-ingest",
            "token_hash": "a" * 64,
            "acquired_at": NOW - timedelta(minutes=20),
            "expires_at": NOW - timedelta(minutes=1),
        }

        claim = lease.try_acquire(now=NOW, duration=timedelta(minutes=5))

        self.assertIsNotNone(claim)
        with self.assertRaises(MaintenanceStateError):
            lease.release(type(claim)(token="wrong-token", expires_at=claim.expires_at))

    def test_hold_resolver_returns_exact_holds_for_matching_document(self) -> None:
        from mim_control_plane.adapters.maintenance_state import (
            FirestoreLifecycleHoldResolver,
        )

        store = MemoryStore()
        store.create_user(user())
        store.create_workload(workload(workload_id="wrk-1"))
        store.create_workload(workload(workload_id="wrk-2"))
        client = FakeFirestoreClient(project=CENTRAL_PROJECT)
        client.collection("lifecycle_hold_sets").documents[
            hold_document_id("usr-1")
        ] = {
            "schema_version": 1,
            "user_id": "usr-1",
            "hold_workload_ids": ["wrk-1"],
            "owned_workload_ids": ["wrk-1", "wrk-2"],
            "issued_at": NOW - timedelta(minutes=5),
            "expires_at": NOW + timedelta(minutes=20),
        }
        resolver = FirestoreLifecycleHoldResolver(
            settings=settings(),
            store=store,
            client_factory=lambda **_: client,
            credentials_loader=lambda: object(),
        )

        holds = resolver.resolve_holds(user_id=UserId("usr-1"), now=NOW)

        self.assertEqual(holds, frozenset({WorkloadId("wrk-1")}))

    def test_hold_resolver_missing_document_means_empty_only_after_store_confirmation(
        self,
    ) -> None:
        from mim_control_plane.adapters.maintenance_state import (
            FirestoreLifecycleHoldResolver,
        )

        store = MemoryStore()
        store.create_user(user())
        store.create_workload(workload(workload_id="wrk-1"))
        store.create_workload(workload(workload_id="wrk-2"))
        client = FakeFirestoreClient(project=CENTRAL_PROJECT)
        resolver = FirestoreLifecycleHoldResolver(
            settings=settings(),
            store=store,
            client_factory=lambda **_: client,
            credentials_loader=lambda: object(),
        )

        holds = resolver.resolve_holds(user_id=UserId("usr-1"), now=NOW)

        self.assertEqual(holds, frozenset())

    def test_hold_resolver_fails_closed_on_malformed_stale_or_foreign_documents(
        self,
    ) -> None:
        from mim_control_plane.adapters.maintenance_state import (
            FirestoreLifecycleHoldResolver,
            MaintenanceStateError,
        )

        store = MemoryStore()
        store.create_user(user())
        store.create_workload(workload(workload_id="wrk-1"))
        store.create_workload(workload(workload_id="wrk-2"))
        bad_cases = (
            {
                "schema_version": 2,
                "user_id": "usr-1",
                "hold_workload_ids": ["wrk-1"],
                "owned_workload_ids": ["wrk-1", "wrk-2"],
                "issued_at": NOW - timedelta(minutes=5),
                "expires_at": NOW + timedelta(minutes=10),
            },
            {
                "schema_version": 1,
                "user_id": "usr-2",
                "hold_workload_ids": ["wrk-1"],
                "owned_workload_ids": ["wrk-1", "wrk-2"],
                "issued_at": NOW - timedelta(minutes=5),
                "expires_at": NOW + timedelta(minutes=10),
            },
            {
                "schema_version": 1,
                "user_id": "usr-1",
                "hold_workload_ids": ["wrk-9"],
                "owned_workload_ids": ["wrk-1", "wrk-2"],
                "issued_at": NOW - timedelta(minutes=5),
                "expires_at": NOW + timedelta(minutes=10),
            },
            {
                "schema_version": 1,
                "user_id": "usr-1",
                "hold_workload_ids": ["wrk-1"],
                "owned_workload_ids": ["wrk-1"],
                "issued_at": NOW - timedelta(minutes=5),
                "expires_at": NOW + timedelta(minutes=10),
            },
            {
                "schema_version": 1,
                "user_id": "usr-1",
                "hold_workload_ids": ["wrk-1"],
                "owned_workload_ids": ["wrk-1", "wrk-2"],
                "issued_at": NOW - timedelta(hours=2),
                "expires_at": NOW + timedelta(minutes=10),
            },
            {
                "schema_version": 1,
                "user_id": "usr-1",
                "hold_workload_ids": ["wrk-1"],
                "owned_workload_ids": ["wrk-1", "wrk-2"],
                "issued_at": NOW - timedelta(minutes=5),
                "expires_at": NOW - timedelta(seconds=1),
            },
        )

        for payload in bad_cases:
            with self.subTest(payload=payload):
                client = FakeFirestoreClient(project=CENTRAL_PROJECT)
                client.collection("lifecycle_hold_sets").documents[
                    hold_document_id("usr-1")
                ] = payload
                resolver = FirestoreLifecycleHoldResolver(
                    settings=settings(),
                    store=store,
                    client_factory=lambda **_: client,
                    credentials_loader=lambda: object(),
                )
                with self.assertRaises(MaintenanceStateError):
                    resolver.resolve_holds(user_id=UserId("usr-1"), now=NOW)

    def test_constructors_require_central_project_and_firestore_failures_close(
        self,
    ) -> None:
        from mim_control_plane.adapters.maintenance_state import (
            FirestoreLifecycleHoldResolver,
            FirestoreNamedOverlapLease,
            MaintenanceStateError,
        )

        with self.assertRaises(ValueError):
            FirestoreNamedOverlapLease(
                settings=settings(project_id="other-project-123456"),
                lease_name="lifecycle-maintenance",
            )

        with self.assertRaises(ValueError):
            FirestoreNamedOverlapLease(
                settings=settings(),
                lease_name="lifecycle-maintenance",
                client_factory=lambda **_: FakeFirestoreClient(
                    project=CENTRAL_PROJECT,
                    database="other",
                ),
                credentials_loader=lambda: object(),
            )

        store = MemoryStore()
        store.create_user(user())
        store.create_workload(workload(workload_id="wrk-1"))
        client = FakeFirestoreClient(project=CENTRAL_PROJECT)
        client.collection("lifecycle_hold_sets").get_error = RuntimeError("boom")
        resolver = FirestoreLifecycleHoldResolver(
            settings=settings(),
            store=store,
            client_factory=lambda **_: client,
            credentials_loader=lambda: object(),
        )

        with self.assertRaises(MaintenanceStateError):
            resolver.resolve_holds(user_id=UserId("usr-1"), now=NOW)


if __name__ == "__main__":
    unittest.main()
