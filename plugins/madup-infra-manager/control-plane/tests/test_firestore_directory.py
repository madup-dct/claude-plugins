from __future__ import annotations

import importlib
import importlib.util
import inspect
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from fakes import FAKE_STARTUP_CONFIG, build_startup_mapping

from mim_control_plane.config import Settings
from mim_control_plane.domain.directory_sync import (
    DirectoryUserReconciliation,
    build_directory_audit_event,
)
from mim_control_plane.domain.models import AuditEvent, User, UserId
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.ports.store import (
    IdempotencyConflict,
    InvariantViolation,
    StoreError,
)

NOW = datetime(2026, 8, 3, 2, 0, 0, tzinfo=UTC)


class FakeSnapshot:
    def __init__(
        self,
        *,
        document_id: str,
        data: dict[str, object] | None,
    ) -> None:
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
    ) -> None:
        self._client = client
        self._collection = collection
        self._limit_count = limit_count

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(
            client=self._client,
            collection=self._collection,
            limit_count=count,
        )

    def stream(self) -> tuple[FakeSnapshot, ...]:
        documents = self._client.documents.get(self._collection, {})
        items = tuple(documents.items())
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

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(client=self, collection=name)

    def run_transaction(self, operation: Any) -> object:
        self.transaction_calls += 1
        transaction = FakeTransaction(client=self)
        self.last_transaction = transaction
        result = operation(transaction)
        transaction.commit()
        return result


def user_document(*, user_id: str, email: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": user_id,
        "email": email,
        "role": "user",
        "state": "active",
        "groups": ["mim-users", "team-alpha"],
        "identity_synced_at": NOW - timedelta(minutes=1),
        "created_at": NOW - timedelta(days=90),
        "updated_at": NOW - timedelta(minutes=1),
        "version": 7,
    }


def domain_user(
    *,
    user_id: str,
    email: str,
    groups: frozenset[str],
    state: UserState = UserState.ACTIVE,
    synced_at: datetime = NOW - timedelta(days=1),
    updated_at: datetime | None = None,
    version: int = 1,
) -> User:
    return User(
        id=UserId(user_id),
        email=email,
        role=UserRole.USER,
        state=state,
        groups=groups,
        identity_synced_at=synced_at,
        created_at=NOW - timedelta(days=90),
        updated_at=updated_at or synced_at,
        version=version,
    )


def persisted_user_document(user: User) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "state": user.state.value,
        "groups": sorted(user.groups),
        "identity_synced_at": user.identity_synced_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "version": user.version,
    }


class FirestoreDirectoryIdentityRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "mim_control_plane.adapters.firestore_directory"
        )
        self.settings = Settings.from_mapping(build_startup_mapping())

    def repository_for(
        self,
        client: FakeFirestoreClient,
    ) -> Any:
        return self.module.FirestoreDirectoryIdentityRepository(
            settings=self.settings,
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
        )

    def document_id(self, *, kind: str, logical_id: str) -> str:
        document_id_factory = getattr(self.module, "_document_id", None)
        if document_id_factory is None:
            self.fail("Firestore document ID derivation is missing")
        return document_id_factory(kind=kind, logical_id=logical_id)

    def test_production_adapter_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec(
                "mim_control_plane.adapters.firestore_directory",
            )
        )

    def test_constructs_client_with_explicit_central_boundary_and_metadata_identity(
        self,
    ) -> None:
        module = importlib.import_module(
            "mim_control_plane.adapters.firestore_directory"
        )
        repository_type = getattr(
            module,
            "FirestoreDirectoryIdentityRepository",
            None,
        )
        if repository_type is None:
            self.fail("FirestoreDirectoryIdentityRepository is missing")
        credentials = object()
        client = object()
        captured: dict[str, object] = {}

        def client_factory(**kwargs: object) -> object:
            captured.update(kwargs)
            return client

        settings = Settings.from_mapping(build_startup_mapping())
        with mock.patch.object(
            module,
            "_google_auth_compute_engine_credentials_factory",
            return_value=credentials,
        ) as compute_factory:
            repository = repository_type(
                settings=settings,
                client_factory=client_factory,
            )

        compute_factory.assert_called_once_with()
        self.assertEqual(captured["project"], FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"])
        self.assertEqual(captured["database"], "(default)")
        self.assertIs(captured["credentials"], credentials)
        self.assertNotIn(FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"], repr(repository))
        self.assertNotIn("credentials", repr(repository).casefold())

    def test_list_users_deserializes_exact_records_and_sorts_by_logical_user_id(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        client.documents["users"] = {
            self.document_id(kind="user", logical_id="usr-z"): user_document(
                user_id="usr-z",
                email="z@madup.com",
            ),
            self.document_id(kind="user", logical_id="usr-a"): user_document(
                user_id="usr-a",
                email="a@madup.com",
            ),
        }

        users = self.repository_for(client).list_users()

        self.assertEqual(
            tuple(user.id for user in users),
            (UserId("usr-a"), UserId("usr-z")),
        )
        self.assertEqual(users[0].groups, frozenset({"mim-users", "team-alpha"}))
        self.assertEqual(users[0].version, 7)

    def test_list_users_rejects_malformed_or_misaddressed_documents_generically(
        self,
    ) -> None:
        cases = {
            "missing": {
                key: value
                for key, value in user_document(
                    user_id="usr-1", email="person@madup.com"
                ).items()
                if key != "role"
            },
            "wrong_document_id": user_document(
                user_id="usr-1",
                email="person@madup.com",
            ),
            "unsorted_groups": {
                **user_document(user_id="usr-1", email="person@madup.com"),
                "groups": ["team-alpha", "mim-users"],
            },
        }
        for label, document in cases.items():
            with self.subTest(label=label):
                client = FakeFirestoreClient()
                document_id = self.document_id(
                    kind="user",
                    logical_id="usr-1",
                )
                if label == "wrong_document_id":
                    document_id = "0" * 64
                client.documents["users"] = {document_id: document}

                with self.assertRaises(StoreError) as context:
                    self.repository_for(client).list_users()

                self.assertEqual(
                    str(context.exception),
                    "Directory repository operation failed.",
                )
                self.assertNotIn("person@madup.com", str(context.exception))

    def test_document_ids_are_stable_redacted_and_kind_bound(self) -> None:
        logical_id = "person@madup.com/../../sensitive"

        user_key = self.document_id(kind="user", logical_id=logical_id)
        audit_key = self.document_id(kind="audit", logical_id=logical_id)

        self.assertEqual(len(user_key), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in user_key))
        self.assertNotIn("person", user_key)
        self.assertNotEqual(user_key, audit_key)
        self.assertEqual(
            user_key,
            self.document_id(kind="user", logical_id=logical_id),
        )

    def test_supports_an_injected_atomic_transaction_runner(self) -> None:
        parameters = inspect.signature(
            self.module.FirestoreDirectoryIdentityRepository
        ).parameters

        self.assertIn("transaction_runner", parameters)

    def test_apply_snapshot_once_persists_exact_users_audits_and_ledger_atomically(
        self,
    ) -> None:
        first = domain_user(
            user_id="usr-1",
            email="one@madup.com",
            groups=frozenset({"team-alpha"}),
        )
        second = domain_user(
            user_id="usr-2",
            email="two@madup.com",
            groups=frozenset({"team-beta", "mim-users"}),
        )
        synced_at = NOW - timedelta(minutes=1)
        first_after = first.reconcile_identity(
            target_state=UserState.ACTIVE,
            required_group="mim-users",
            in_required_group=True,
            synced_at=synced_at,
        )
        second_after = second.reconcile_identity(
            target_state=UserState.SUSPENDED,
            required_group="mim-users",
            in_required_group=False,
            synced_at=synced_at,
        )
        reconciliations = (
            DirectoryUserReconciliation(
                user=first_after,
                expected_version=first.version,
                required_group="mim-users",
                policy_decision="directory_active_member",
            ),
            DirectoryUserReconciliation(
                user=second_after,
                expected_version=second.version,
                required_group="mim-users",
                policy_decision="directory_group_missing",
            ),
        )
        audits: tuple[AuditEvent, ...] = tuple(
            build_directory_audit_event(
                snapshot_id="snap-atomic",
                required_group=reconciliation.required_group,
                policy_decision=reconciliation.policy_decision,
                user_before=before,
                user_after=reconciliation.user,
                synced_at=synced_at,
            )
            for before, reconciliation in zip(
                (first, second),
                reconciliations,
                strict=True,
            )
        )
        client = FakeFirestoreClient()
        client.documents["users"] = {
            self.document_id(kind="user", logical_id=str(record.id)): (
                persisted_user_document(record)
            )
            for record in (first, second)
        }
        repository = self.repository_for(client)
        apply_snapshot_once = getattr(repository, "apply_snapshot_once", None)
        if apply_snapshot_once is None:
            self.fail("Firestore snapshot transaction is missing")

        result = apply_snapshot_once(
            snapshot_id="snap-atomic",
            material_hash="a" * 64,
            reconciliations=reconciliations,
            audit_events=audits,
        )

        self.assertFalse(result.replayed)
        self.assertEqual(result.applied_user_ids, (first.id, second.id))
        self.assertEqual(result.locked_user_ids, (second.id,))
        self.assertEqual(result.audit_event_ids, tuple(event.id for event in audits))
        self.assertEqual(client.transaction_calls, 1)
        self.assertIsNotNone(client.last_transaction)
        assert client.last_transaction is not None
        operations = tuple(item[0] for item in client.last_transaction.log)
        first_write_index = next(
            index for index, operation in enumerate(operations) if operation != "read"
        )
        self.assertTrue(all(item == "read" for item in operations[:first_write_index]))
        self.assertEqual(len(operations[first_write_index:]), 5)
        self.assertEqual(repository.list_users(), (first_after, second_after))
        self.assertEqual(len(client.documents["audit_events"]), 2)
        self.assertEqual(len(client.documents["directory_snapshot_ledger"]), 1)
        for document_id in client.documents["users"]:
            self.assertNotIn("usr-", document_id)

    def test_apply_snapshot_redacts_unexpected_store_backend_failures(self) -> None:
        leaked = (
            f"{FAKE_STARTUP_CONFIG['MIM_PROJECT_ID']} "
            "person@madup.com users/private-document"
        )
        repository = self.module.FirestoreDirectoryIdentityRepository(
            settings=self.settings,
            credentials_loader=object,
            client_factory=lambda **_: FakeFirestoreClient(),
            transaction_runner=lambda _client, _operation: (_ for _ in ()).throw(
                StoreError(leaked)
            ),
        )

        with self.assertRaises(StoreError) as context:
            repository.apply_snapshot_once(
                snapshot_id="snap-redacted",
                material_hash="b" * 64,
                reconciliations=(),
                audit_events=(),
            )

        self.assertEqual(
            str(context.exception),
            "Directory repository operation failed.",
        )
        self.assertNotIn(leaked, str(context.exception))

    def test_apply_snapshot_once_recovers_when_commit_succeeds_but_response_is_lost(
        self,
    ) -> None:
        client = FakeFirestoreClient()

        def commit_then_fail(
            supplied_client: FakeFirestoreClient,
            operation: Any,
        ) -> object:
            supplied_client.run_transaction(operation)
            raise RuntimeError("person@madup.com commit response lost")

        repository = self.module.FirestoreDirectoryIdentityRepository(
            settings=self.settings,
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=commit_then_fail,
        )

        try:
            result = repository.apply_snapshot_once(
                snapshot_id="snap-uncertain-commit",
                material_hash="c" * 64,
                reconciliations=(),
                audit_events=(),
            )
        except StoreError:
            self.fail("committed snapshot was not recovered from its ledger")

        self.assertTrue(result.replayed)
        self.assertEqual(result.applied_user_ids, ())
        self.assertEqual(len(client.documents["directory_snapshot_ledger"]), 1)

    def test_replay_rejects_a_ledger_that_exceeds_the_fixed_pilot_cap(self) -> None:
        snapshot_id = "snap-oversized-ledger"
        client = FakeFirestoreClient()
        client.documents["directory_snapshot_ledger"] = {
            self.document_id(kind="snapshot", logical_id=snapshot_id): {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "material_hash": "d" * 64,
                "applied_user_ids": [f"usr-{index}" for index in range(51)],
                "locked_user_ids": [],
                "audit_event_ids": [f"audit-{index}" for index in range(51)],
            }
        }

        with self.assertRaises(InvariantViolation):
            self.repository_for(client).apply_snapshot_once(
                snapshot_id=snapshot_id,
                material_hash="d" * 64,
                reconciliations=(),
                audit_events=(),
            )

    def test_noop_snapshot_is_recorded_once_then_replayed_or_conflicted(self) -> None:
        client = FakeFirestoreClient()
        repository = self.repository_for(client)

        applied = repository.apply_snapshot_once(
            snapshot_id="snap-noop",
            material_hash="e" * 64,
            reconciliations=(),
            audit_events=(),
        )
        replayed = repository.apply_snapshot_once(
            snapshot_id="snap-noop",
            material_hash="e" * 64,
            reconciliations=(),
            audit_events=(),
        )

        self.assertFalse(applied.replayed)
        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.applied_user_ids, ())
        self.assertEqual(replayed.locked_user_ids, ())
        self.assertEqual(replayed.audit_event_ids, ())
        self.assertEqual(len(client.documents["directory_snapshot_ledger"]), 1)
        with self.assertRaises(IdempotencyConflict):
            repository.apply_snapshot_once(
                snapshot_id="snap-noop",
                material_hash="f" * 64,
                reconciliations=(),
                audit_events=(),
            )
        self.assertEqual(len(client.documents["directory_snapshot_ledger"]), 1)

    def test_more_than_fifty_reconciliations_fail_before_a_transaction(self) -> None:
        client = FakeFirestoreClient()
        repository = self.repository_for(client)

        with self.assertRaises(InvariantViolation):
            repository.apply_snapshot_once(
                snapshot_id="snap-over-cap",
                material_hash="1" * 64,
                reconciliations=tuple(object() for _ in range(51)),  # type: ignore[arg-type]
                audit_events=(),
            )

        self.assertEqual(client.transaction_calls, 0)
        self.assertNotIn("directory_snapshot_ledger", client.documents)


class FirestoreDirectorySyncLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "mim_control_plane.adapters.firestore_directory"
        )
        self.settings = Settings.from_mapping(build_startup_mapping())

    def lease_for(
        self,
        client: FakeFirestoreClient,
        *,
        token_factory: Any = lambda: "opaque-lease-token-1234567890",
    ) -> Any:
        return self.module.FirestoreDirectorySyncLease(
            settings=self.settings,
            required_group="mim-users",
            token_factory=token_factory,
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
        )

    def test_constructor_uses_explicit_project_database_and_metadata_identity(
        self,
    ) -> None:
        credentials = object()
        client = FakeFirestoreClient()
        captured: dict[str, object] = {}

        def client_factory(**kwargs: object) -> FakeFirestoreClient:
            captured.update(kwargs)
            return client

        with mock.patch.object(
            self.module,
            "_google_auth_compute_engine_credentials_factory",
            return_value=credentials,
        ) as compute_factory:
            lease = self.module.FirestoreDirectorySyncLease(
                settings=self.settings,
                required_group="mim-users",
                client_factory=client_factory,
            )

        compute_factory.assert_called_once_with()
        self.assertEqual(captured["project"], "mim-prod-123456")
        self.assertEqual(captured["database"], "(default)")
        self.assertIs(captured["credentials"], credentials)
        self.assertNotIn("mim-prod-123456", repr(lease))

    def test_lease_duration_is_bounded_before_transaction_execution(self) -> None:
        client = FakeFirestoreClient()
        lease = self.lease_for(client)

        with self.assertRaisesRegex(ValueError, "duration is invalid"):
            lease.try_acquire(now=NOW, duration=timedelta(minutes=16))

        self.assertEqual(client.transaction_calls, 0)

    def test_acquire_is_single_winner_and_persists_only_a_token_hash(self) -> None:
        client = FakeFirestoreClient()
        lease = self.lease_for(client)

        claim = lease.try_acquire(now=NOW, duration=timedelta(minutes=10))
        overlap = lease.try_acquire(
            now=NOW + timedelta(seconds=1),
            duration=timedelta(minutes=10),
        )

        self.assertIsNotNone(claim)
        self.assertIsNone(overlap)
        self.assertEqual(claim.expires_at, NOW + timedelta(minutes=10))
        self.assertNotIn("opaque-lease-token", repr(claim))
        documents = client.documents["directory_sync_leases"]
        self.assertEqual(len(documents), 1)
        stored = next(iter(documents.values()))
        self.assertEqual(
            frozenset(stored),
            frozenset(
                {
                    "schema_version",
                    "required_group",
                    "token_hash",
                    "acquired_at",
                    "expires_at",
                }
            ),
        )
        self.assertEqual(stored["required_group"], "mim-users")
        self.assertEqual(len(stored["token_hash"]), 64)
        self.assertNotIn("opaque-lease-token", repr(stored))
        self.assertEqual(client.transaction_calls, 2)

    def test_acquire_recovers_when_commit_succeeds_but_response_is_lost(
        self,
    ) -> None:
        client = FakeFirestoreClient()

        def commit_then_fail(
            supplied_client: FakeFirestoreClient,
            operation: Any,
        ) -> object:
            supplied_client.run_transaction(operation)
            raise RuntimeError("person@madup.com lease commit response lost")

        lease = self.module.FirestoreDirectorySyncLease(
            settings=self.settings,
            required_group="mim-users",
            token_factory=lambda: "opaque-recovered-lease-token",
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=commit_then_fail,
        )

        try:
            claim = lease.try_acquire(now=NOW, duration=timedelta(minutes=10))
        except StoreError:
            self.fail("committed lease was not recovered from Firestore")

        self.assertEqual(claim.token, "opaque-recovered-lease-token")
        self.assertEqual(claim.expires_at, NOW + timedelta(minutes=10))
        stored = next(iter(client.documents["directory_sync_leases"].values()))
        self.assertNotEqual(stored["token_hash"], claim.token)
        self.assertNotIn(claim.token, repr(stored))

    def test_acquire_retry_recognizes_its_own_committed_claim(self) -> None:
        client = FakeFirestoreClient()

        def commit_then_retry(
            supplied_client: FakeFirestoreClient,
            operation: Any,
        ) -> object:
            supplied_client.run_transaction(operation)
            return supplied_client.run_transaction(operation)

        lease = self.module.FirestoreDirectorySyncLease(
            settings=self.settings,
            required_group="mim-users",
            token_factory=lambda: "opaque-retried-lease-token",
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=commit_then_retry,
        )

        claim = lease.try_acquire(now=NOW, duration=timedelta(minutes=10))

        self.assertIsNotNone(claim)
        self.assertEqual(claim.token, "opaque-retried-lease-token")
        self.assertEqual(client.transaction_calls, 2)

    def test_acquire_recovery_never_claims_a_different_owner(self) -> None:
        client = FakeFirestoreClient()

        def commit_replace_then_fail(
            supplied_client: FakeFirestoreClient,
            operation: Any,
        ) -> object:
            supplied_client.run_transaction(operation)
            stored = next(
                iter(supplied_client.documents["directory_sync_leases"].values())
            )
            stored["token_hash"] = "b" * 64
            raise RuntimeError("different owner replaced the uncertain claim")

        lease = self.module.FirestoreDirectorySyncLease(
            settings=self.settings,
            required_group="mim-users",
            token_factory=lambda: "opaque-original-lease-token",
            credentials_loader=object,
            client_factory=lambda **_: client,
            transaction_runner=commit_replace_then_fail,
        )

        with self.assertRaisesRegex(StoreError, "operation failed"):
            lease.try_acquire(now=NOW, duration=timedelta(minutes=10))

    def test_expired_lease_is_replaced_atomically(self) -> None:
        client = FakeFirestoreClient()
        tokens = iter(("lease-token-first", "lease-token-second"))
        lease = self.lease_for(client, token_factory=lambda: next(tokens))

        first = lease.try_acquire(now=NOW, duration=timedelta(minutes=5))
        second = lease.try_acquire(
            now=NOW + timedelta(minutes=6),
            duration=timedelta(minutes=10),
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.token, second.token)
        stored = next(iter(client.documents["directory_sync_leases"].values()))
        self.assertEqual(stored["acquired_at"], NOW + timedelta(minutes=6))
        self.assertEqual(stored["expires_at"], NOW + timedelta(minutes=16))

    def test_persisted_lease_duration_cannot_exceed_the_fixed_maximum(self) -> None:
        client = FakeFirestoreClient()
        lease = self.lease_for(client)
        reference = lease._reference
        client.documents["directory_sync_leases"] = {
            reference.id: {
                "schema_version": 1,
                "required_group": "mim-users",
                "token_hash": "a" * 64,
                "acquired_at": NOW,
                "expires_at": NOW + timedelta(days=365),
            }
        }

        with self.assertRaisesRegex(StoreError, "operation failed"):
            lease.try_acquire(
                now=NOW + timedelta(minutes=1), duration=timedelta(minutes=10)
            )

        self.assertEqual(client.transaction_calls, 1)

    def test_persisted_lease_schema_version_must_be_an_exact_integer(self) -> None:
        client = FakeFirestoreClient()
        lease = self.lease_for(client)
        reference = lease._reference
        client.documents["directory_sync_leases"] = {
            reference.id: {
                "schema_version": True,
                "required_group": "mim-users",
                "token_hash": "a" * 64,
                "acquired_at": NOW,
                "expires_at": NOW + timedelta(minutes=10),
            }
        }

        with self.assertRaisesRegex(StoreError, "operation failed"):
            lease.try_acquire(
                now=NOW + timedelta(minutes=1), duration=timedelta(minutes=10)
            )

        self.assertEqual(client.transaction_calls, 1)

    def test_persisted_lease_group_uses_the_same_fixed_bound(self) -> None:
        client = FakeFirestoreClient()
        lease = self.lease_for(client)
        reference = lease._reference
        client.documents["directory_sync_leases"] = {
            reference.id: {
                "schema_version": 1,
                "required_group": "g" * 129,
                "token_hash": "a" * 64,
                "acquired_at": NOW,
                "expires_at": NOW + timedelta(minutes=10),
            }
        }

        with self.assertRaisesRegex(StoreError, "operation failed"):
            lease.try_acquire(
                now=NOW + timedelta(minutes=1), duration=timedelta(minutes=10)
            )

        self.assertEqual(client.transaction_calls, 1)

    def test_current_owner_can_release_the_lease(self) -> None:
        client = FakeFirestoreClient()
        lease = self.lease_for(client)
        claim = lease.try_acquire(now=NOW, duration=timedelta(minutes=10))
        self.assertIsNotNone(claim)

        lease.release(claim)

        self.assertEqual(client.documents["directory_sync_leases"], {})

    def test_stale_owner_cannot_release_a_reacquired_lease(self) -> None:
        client = FakeFirestoreClient()
        tokens = iter(("lease-token-stale", "lease-token-current"))
        lease = self.lease_for(client, token_factory=lambda: next(tokens))
        stale = lease.try_acquire(now=NOW, duration=timedelta(minutes=5))
        current = lease.try_acquire(
            now=NOW + timedelta(minutes=6),
            duration=timedelta(minutes=10),
        )
        self.assertIsNotNone(stale)
        self.assertIsNotNone(current)
        before = deepcopy(client.documents["directory_sync_leases"])

        with self.assertRaisesRegex(StoreError, "operation failed"):
            lease.release(stale)

        self.assertEqual(client.documents["directory_sync_leases"], before)
        lease.release(current)
        self.assertEqual(client.documents["directory_sync_leases"], {})


if __name__ == "__main__":
    unittest.main()
