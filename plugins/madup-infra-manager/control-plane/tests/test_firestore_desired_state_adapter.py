from __future__ import annotations

import base64
import copy
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.firestore_desired_state import (  # noqa: E402
    FirestoreDesiredStateArtifactPort,
)
from mim_control_plane.domain.models import (  # noqa: E402
    OperationId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (  # noqa: E402
    RepositoryAdmissionState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.execution import (  # noqa: E402
    ArtifactConflictError,
    TaskNotFoundError,
)
from mim_control_plane.services.render import (  # noqa: E402
    DesiredStateRenderContext,
    SignedDesiredStateEnvelope,
    render_signed_desired_state,
)

NOW = datetime(2026, 8, 4, 0, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
REGION = "asia-northeast3"
KEY = b"k" * 32


class FakeSnapshot:
    def __init__(self, *, document_id: str, data: dict[str, object] | None) -> None:
        self.id = document_id
        self.exists = data is not None
        self._data = copy.deepcopy(data)

    def to_dict(self) -> dict[str, object] | None:
        return copy.deepcopy(self._data)


class FakeDocumentReference:
    def __init__(
        self,
        *,
        client: "FakeFirestoreClient",
        collection: str,
        document_id: str,
    ) -> None:
        self._client = client
        self.id = document_id
        self._collection = collection

    def get(self) -> FakeSnapshot:
        data = self._client.documents.get(self._collection, {}).get(self.id)
        return FakeSnapshot(document_id=self.id, data=data)

    def create(self, data: dict[str, object]) -> None:
        collection = self._client.documents.setdefault(self._collection, {})
        if self.id in collection:
            raise RuntimeError("document already exists")
        collection[self.id] = copy.deepcopy(data)


class FakeCollection:
    def __init__(self, *, client: "FakeFirestoreClient", name: str) -> None:
        self._client = client
        self._name = name

    def document(self, document_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(
            client=self._client,
            collection=self._name,
            document_id=document_id,
        )


class FakeFirestoreClient:
    def __init__(
        self,
        *,
        project: str = PROJECT_ID,
        database: str = "(default)",
    ) -> None:
        self.project = project
        self.database = database
        self.database_string = f"projects/{project}/databases/{database}"
        self.documents: dict[str, dict[str, dict[str, object]]] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(client=self, name=name)


def sample_admission() -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId("adm-1"),
        owner="madupmarketing",
        name="campaign-bot",
        repository_numeric_id=101,
        installation_id=202,
        admitted_sha="a" * 40,
        state=RepositoryAdmissionState.ADMITTED,
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW,
    )


def sample_workload() -> Workload:
    return Workload(
        id=WorkloadId("wrk-1"),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId("adm-1"),
        name="campaign-bot",
        kind=WorkloadKind.STREAMLIT,
        state=WorkloadState.ACTIVE,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-1",
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW,
        last_activity_at=NOW - timedelta(minutes=5),
        version=3,
    )


def sample_envelope() -> SignedDesiredStateEnvelope:
    return render_signed_desired_state(
        workload=sample_workload(),
        admission=sample_admission(),
        snapshot={
            "app.py": b"import streamlit as st\nst.title('hello')\n",
            "requirements.txt": b"streamlit==1.38.0\n",
        },
        image_digest="b" * 64,
        context=DesiredStateRenderContext(project_id=PROJECT_ID, key_id="test-key"),
        issued_at=NOW,
        signing_key=KEY,
    )


def adapter(
    client: FakeFirestoreClient | None = None,
) -> tuple[FirestoreDesiredStateArtifactPort, FakeFirestoreClient]:
    fake = client or FakeFirestoreClient()
    return (
        FirestoreDesiredStateArtifactPort(
            client=fake,
            project_id=PROJECT_ID,
            region=REGION,
        ),
        fake,
    )


class FirestoreDesiredStateArtifactPortTests(unittest.TestCase):
    def test_v2_schema_is_explicit_and_legacy_v1_is_rejected(self) -> None:
        store, _client = adapter()
        envelope = sample_envelope()

        self.assertEqual(envelope.schema_version, "mim-desired-state-v2")
        with self.assertRaises(ArtifactConflictError):
            store.create_once(
                operation_id=OperationId("op-legacy-v1"),
                envelope=replace(
                    envelope,
                    schema_version="mim-desired-state-v1",
                ),
            )

    def test_constructor_rejects_non_central_client_project_or_database(self) -> None:
        with self.assertRaises(ValueError):
            adapter(client=FakeFirestoreClient(project="other-project-12345"))
        with self.assertRaises(ValueError):
            adapter(client=FakeFirestoreClient(database="analytics"))

    def test_create_once_persists_exact_envelope_and_replays_same_material(
        self,
    ) -> None:
        store, client = adapter()
        envelope = sample_envelope()

        created = store.create_once(
            operation_id=OperationId("op-1"),
            envelope=envelope,
        )
        replayed = store.create_once(
            operation_id=OperationId("op-1"),
            envelope=envelope,
        )
        loaded = store.get(OperationId("op-1"))

        self.assertEqual(created, envelope)
        self.assertEqual(replayed, envelope)
        self.assertEqual(loaded, envelope)
        documents = client.documents["desired_state_artifacts"]
        self.assertEqual(len(documents), 1)
        stored = next(iter(documents.values()))
        self.assertEqual(stored["operation_id"], "op-1")
        self.assertEqual(
            base64.b64decode(cast(str, stored["canonical_unsigned_b64"])),
            store._canonical_unsigned_for_test(envelope),  # type: ignore[attr-defined]
        )

    def test_create_once_rejects_conflicting_material_for_the_same_operation(
        self,
    ) -> None:
        store, _client = adapter()
        envelope = sample_envelope()
        conflicting = SignedDesiredStateEnvelope(
            schema_version=envelope.schema_version,
            key_id=envelope.key_id,
            audience=envelope.audience,
            issued_at=envelope.issued_at,
            expires_at=envelope.expires_at,
            payload=envelope.payload,
            signature=("0" if envelope.signature[-1] != "0" else "1")
            + envelope.signature[1:],
        )
        store.create_once(operation_id=OperationId("op-1"), envelope=envelope)

        with self.assertRaises(ArtifactConflictError):
            store.create_once(
                operation_id=OperationId("op-1"),
                envelope=conflicting,
            )

    def test_get_rejects_corrupted_operation_binding_and_missing_records(self) -> None:
        store, client = adapter()
        envelope = sample_envelope()
        store.create_once(operation_id=OperationId("op-1"), envelope=envelope)
        document_id = next(iter(client.documents["desired_state_artifacts"]))
        client.documents["desired_state_artifacts"][document_id]["operation_id"] = (
            "op-2"
        )

        with self.assertRaises(ArtifactConflictError):
            store.get(OperationId("op-1"))

        with self.assertRaises(TaskNotFoundError):
            store.get(OperationId("op-missing"))
