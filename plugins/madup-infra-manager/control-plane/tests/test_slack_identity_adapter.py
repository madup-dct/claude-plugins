from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.firestore_slack_oauth import (
    FirestoreSlackOAuthRepository,
)
from mim_control_plane.adapters.slack_identity import FirestoreSlackIdentityDirectory
from mim_control_plane.domain.models import UserId
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthIdentityLink,
    SlackOAuthIdentityLinkState,
    SlackOAuthInstallState,
    SlackOAuthSharedInstall,
)
from mim_control_plane.ports.store import NotFound

NOW = datetime(2026, 8, 4, 4, 0, 0, tzinfo=UTC)


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
        data = self._collection.documents.get(self.id)
        return FakeSnapshot(id=self.id, exists=data is not None, data=data)

    def set(self, data: dict[str, object]) -> None:
        self._collection.documents[self.id] = dict(data)

    def create(self, data: dict[str, object]) -> None:
        if self.id in self._collection.documents:
            raise RuntimeError("duplicate")
        self._collection.documents[self.id] = dict(data)

    def delete(self) -> None:
        self._collection.documents.pop(self.id, None)


class FakeQuery:
    def __init__(
        self,
        collection: "FakeCollection",
        filters: tuple[tuple[str, object], ...] = (),
    ) -> None:
        self._collection = collection
        self._filters = filters

    def where(self, field_name: str, op_string: str, value: object) -> "FakeQuery":
        if op_string != "==":
            raise AssertionError(f"unexpected operator: {op_string}")
        return FakeQuery(self._collection, self._filters + ((field_name, value),))

    def stream(self) -> tuple[FakeSnapshot, ...]:
        matches: list[FakeSnapshot] = []
        for document_id, data in self._collection.documents.items():
            if all(
                data.get(field_name) == expected
                for field_name, expected in self._filters
            ):
                matches.append(FakeSnapshot(id=document_id, exists=True, data=data))
        return tuple(matches)


class FakeCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, object]] = {}

    def document(self, document_id: str) -> FakeDocument:
        return FakeDocument(self, document_id)

    def where(self, field_name: str, op_string: str, value: object) -> FakeQuery:
        return FakeQuery(self).where(field_name, op_string, value)


class FakeTransaction:
    def set(self, reference: FakeDocument, data: dict[str, object]) -> None:
        reference.set(data)

    def create(self, reference: FakeDocument, data: dict[str, object]) -> None:
        reference.create(data)

    def delete(self, reference: FakeDocument) -> None:
        reference.delete()


class FakeFirestoreClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def collection(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())

    def run_transaction(self, operation: object) -> object:
        return operation(FakeTransaction())


def install(
    *,
    install_id: str = "a" * 32,
    team_id: str = "T123",
    enterprise_id: str | None = None,
    state: SlackOAuthInstallState = SlackOAuthInstallState.ACTIVE,
    revoked_at: datetime | None = None,
) -> SlackOAuthSharedInstall:
    return SlackOAuthSharedInstall(
        install_id=install_id,
        app_id="A123",
        team_id=team_id,
        enterprise_id=enterprise_id,
        is_enterprise_install=enterprise_id is not None,
        granted_scopes=("commands", "chat:write"),
        secret_ref="projects/mim/secrets/slack/versions/1",
        installer_mim_user_id=UserId("admin-1"),
        installer_email="admin@madup.com",
        created_at=NOW - timedelta(days=1),
        updated_at=revoked_at or NOW,
        state=state,
        revoked_at=revoked_at,
    )


def link(
    *,
    install_id: str = "a" * 32,
    team_id: str = "T123",
    slack_user_id: str = "U123",
    mim_user_id: str = "usr-1",
    state: SlackOAuthIdentityLinkState = SlackOAuthIdentityLinkState.ACTIVE,
    revoked_at: datetime | None = None,
) -> SlackOAuthIdentityLink:
    return SlackOAuthIdentityLink(
        install_id=install_id,
        team_id=team_id,
        slack_user_id=slack_user_id,
        mim_user_id=UserId(mim_user_id),
        company_email="person@madup.com",
        created_at=NOW - timedelta(hours=2),
        updated_at=revoked_at or NOW - timedelta(hours=1),
        state=state,
        revoked_at=revoked_at,
    )


class FirestoreSlackIdentityDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeFirestoreClient()
        self.repository = FirestoreSlackOAuthRepository(
            client=self.client,
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
        )
        self.directory = FirestoreSlackIdentityDirectory(repository=self.repository)

    def test_get_shared_install_maps_metadata_without_secret_ref(self) -> None:
        saved = self.repository.save_shared_install(install(enterprise_id="E123"))

        record = self.directory.get_shared_install(
            install_id=saved.install_id,
            team_id="T123",
            enterprise_id="E123",
        )

        self.assertEqual(record.install_id, saved.install_id)
        self.assertEqual(record.team_id, "T123")
        self.assertEqual(record.enterprise_id, "E123")
        self.assertEqual(record.installer_mim_user_id, UserId("admin-1"))
        self.assertEqual(record.state.value, "active")
        self.assertFalse(hasattr(record, "secret_ref"))
        self.assertNotIn("secret_ref", repr(record))

    def test_get_shared_install_maps_revoked_state(self) -> None:
        saved = self.repository.save_shared_install(install())
        self.repository.revoke_shared_install(
            install_id=saved.install_id,
            revoked_at=NOW + timedelta(minutes=1),
        )

        record = self.directory.get_shared_install(
            install_id=saved.install_id,
            team_id="T123",
            enterprise_id=None,
        )
        self.assertEqual(record.state.value, "revoked")
        self.assertIsNotNone(record.revoked_at)

    def test_get_shared_install_fails_closed_on_exact_mismatch(self) -> None:
        saved = self.repository.save_shared_install(
            install(team_id="T123", enterprise_id=None)
        )

        with self.assertRaises(NotFound):
            self.directory.get_shared_install(
                install_id=saved.install_id,
                team_id="T999",
                enterprise_id=None,
            )
        with self.assertRaises(NotFound):
            self.directory.get_shared_install(
                install_id=saved.install_id,
                team_id="T123",
                enterprise_id="E123",
            )

    def test_get_identity_link_maps_exact_record_and_verification_time(self) -> None:
        self.repository.save_shared_install(install())
        saved = self.repository.save_identity_link(link())

        record = self.directory.get_identity_link(
            install_id=saved.install_id,
            team_id=saved.team_id,
            slack_user_id=saved.slack_user_id,
        )

        self.assertEqual(record.install_id, saved.install_id)
        self.assertEqual(record.team_id, saved.team_id)
        self.assertEqual(record.slack_user_id, saved.slack_user_id)
        self.assertEqual(record.mim_user_id, saved.mim_user_id)
        self.assertEqual(record.company_email, saved.company_email)
        self.assertEqual(record.verified_at, saved.created_at)
        self.assertEqual(record.state.value, "active")

    def test_get_identity_link_maps_revoked_and_fails_closed_on_duplicates(
        self,
    ) -> None:
        self.repository.save_shared_install(install())
        primary = self.repository.save_identity_link(link(mim_user_id="usr-1"))
        duplicate = self.repository.save_identity_link(link(mim_user_id="usr-2"))
        self.assertNotEqual(primary.mim_user_id, duplicate.mim_user_id)

        with self.assertRaises(NotFound):
            self.directory.get_identity_link(
                install_id=primary.install_id,
                team_id=primary.team_id,
                slack_user_id=primary.slack_user_id,
            )

        self.repository = FirestoreSlackOAuthRepository(
            client=FakeFirestoreClient(),
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
        )
        self.directory = FirestoreSlackIdentityDirectory(repository=self.repository)
        self.repository.save_shared_install(install())
        saved = self.repository.save_identity_link(
            link(
                state=SlackOAuthIdentityLinkState.REVOKED,
                revoked_at=NOW,
            )
        )
        record = self.directory.get_identity_link(
            install_id=saved.install_id,
            team_id=saved.team_id,
            slack_user_id=saved.slack_user_id,
        )
        self.assertEqual(record.state.value, "revoked")
        self.assertEqual(record.revoked_at, NOW)

    def test_get_identity_link_fails_closed_on_slack_or_team_mismatch(self) -> None:
        self.repository.save_shared_install(install())
        saved = self.repository.save_identity_link(link())

        with self.assertRaises(NotFound):
            self.directory.get_identity_link(
                install_id=saved.install_id,
                team_id="T999",
                slack_user_id=saved.slack_user_id,
            )
        with self.assertRaises(NotFound):
            self.directory.get_identity_link(
                install_id=saved.install_id,
                team_id=saved.team_id,
                slack_user_id="U999",
            )


if __name__ == "__main__":
    unittest.main()
