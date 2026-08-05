from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

import google_crc32c
from google.api_core.exceptions import NotFound
from google.cloud import secretmanager_v1
from google.iam.v1 import policy_pb2  # type: ignore[import-untyped]
from google.type import expr_pb2  # type: ignore[import-untyped]

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.adapters.secret_manager import (
    ManagedSecretMetadata,
    SecretManagerAdapter,
    SecretManagerAdapterError,
    SecretVersionMetadata,
    SecretVersionStateMetadata,
)
from mim_control_plane.domain.models import (
    SecretId,
    SecretMetadata,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    SecretLifecycleState,
    SecretRotationState,
)
from mim_control_plane.ports.execution import (
    SecretAttachmentReference,
    SecretMetadataDeniedError,
)
from mim_control_plane.services.runtime_naming import provider_secret_id

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
SECRET_ID = "sec-0123456789abcdefabcd"
SECOND_SECRET_ID = "sec-fedcba9876543210abcd"
WORKLOAD_ID = WorkloadId("wrk-1")
WORKLOAD_ID_2 = WorkloadId("wrk-2")
PROVIDER_SECRET_NAME = provider_secret_id(SECRET_ID)
SECRET_NAME = f"projects/{PROJECT_ID}/secrets/{PROVIDER_SECRET_NAME}"
VERSION_NAME = f"{SECRET_NAME}/versions/3"
RUNTIME_MEMBER = (
    "serviceAccount:mim-wrk-5251ebcdff9f@"
    f"{PROJECT_ID}.iam.gserviceaccount.com"
)
VERSION_MANAGER_MEMBER = (
    f"serviceAccount:mim-control-plane@{PROJECT_ID}.iam.gserviceaccount.com"
)
METADATA_READER_MEMBER = (
    f"serviceAccount:mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com"
)
RUNTIME_MEMBER_2 = (
    "serviceAccount:mim-wrk-184a8ef2ab96@"
    f"{PROJECT_ID}.iam.gserviceaccount.com"
)


def secret_record(
    *,
    lifecycle_state: SecretLifecycleState = SecretLifecycleState.ACTIVE,
    attached_workload_ids: tuple[WorkloadId, ...] = (WORKLOAD_ID,),
) -> SecretMetadata:
    return SecretMetadata(
        id=SecretId(SECRET_ID),
        owner_id=UserId("usr-1"),
        name="slack-shared",
        integration_type="slack",
        attached_workload_ids=attached_workload_ids,
        active_version=3,
        rotation_state=SecretRotationState.STABLE,
        lifecycle_state=lifecycle_state,
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )


def enabled_version(version: int = 3) -> secretmanager_v1.SecretVersion:
    return secretmanager_v1.SecretVersion(
        name=f"{SECRET_NAME}/versions/{version}",
        state=secretmanager_v1.SecretVersion.State.ENABLED,
        client_specified_payload_checksum=True,
    )


def exact_policy(
    runtime_members: tuple[str, ...] = (RUNTIME_MEMBER,),
) -> policy_pb2.Policy:
    return policy_pb2.Policy(
        version=3,
        etag=b"policy-etag",
        bindings=(
            policy_pb2.Binding(
                role="roles/secretmanager.secretAccessor",
                members=runtime_members,
            ),
            policy_pb2.Binding(
                role="roles/secretmanager.viewer",
                members=(METADATA_READER_MEMBER,),
            ),
            policy_pb2.Binding(
                role="roles/secretmanager.secretVersionManager",
                members=(VERSION_MANAGER_MEMBER,),
            ),
        ),
    )


def exact_secret(
    *,
    labels: dict[str, str] | None = None,
    version_aliases: dict[str, int] | None = None,
    automatic: bool = True,
) -> secretmanager_v1.Secret:
    replication = secretmanager_v1.Replication()
    if automatic:
        replication = secretmanager_v1.Replication(
            automatic=secretmanager_v1.Replication.Automatic()
        )
    else:
        replication = secretmanager_v1.Replication(
            user_managed=secretmanager_v1.Replication.UserManaged(
                replicas=(
                    secretmanager_v1.Replication.UserManaged.Replica(
                        location="asia-northeast3"
                    ),
                )
            )
        )
    return secretmanager_v1.Secret(
        name=SECRET_NAME,
        replication=replication,
        labels=labels or {"managed-by": "mim-control-plane"},
        version_aliases=version_aliases or {},
    )


def disabled_version(version: int) -> secretmanager_v1.SecretVersion:
    return secretmanager_v1.SecretVersion(
        name=f"{SECRET_NAME}/versions/{version}",
        state=secretmanager_v1.SecretVersion.State.DISABLED,
        client_specified_payload_checksum=True,
    )


def destroyed_version(version: int) -> secretmanager_v1.SecretVersion:
    return secretmanager_v1.SecretVersion(
        name=f"{SECRET_NAME}/versions/{version}",
        state=secretmanager_v1.SecretVersion.State.DESTROYED,
        client_specified_payload_checksum=True,
    )


class FakeSecretManagerClient:
    def __init__(self) -> None:
        self.secret: secretmanager_v1.Secret | None = exact_secret()
        self.versions = [enabled_version()]
        self.policy = exact_policy()
        self.add_response = enabled_version(4)
        self.create_error: Exception | None = None
        self.version_lookup = {
            VERSION_NAME: enabled_version(3),
            f"{SECRET_NAME}/versions/4": enabled_version(4),
            f"{SECRET_NAME}/versions/2": enabled_version(2),
        }
        self.get_secret_requests: list[Any] = []
        self.create_secret_requests: list[Any] = []
        self.list_version_requests: list[Any] = []
        self.get_version_requests: list[Any] = []
        self.get_policy_requests: list[Any] = []
        self.set_policy_requests: list[Any] = []
        self.add_version_requests: list[Any] = []
        self.disable_version_requests: list[Any] = []
        self.destroy_version_requests: list[Any] = []

    def get_secret(self, request: Any) -> secretmanager_v1.Secret:
        self.get_secret_requests.append(request)
        if self.secret is None:
            raise NotFound("missing")
        return self.secret

    def create_secret(self, request: Any) -> secretmanager_v1.Secret:
        self.create_secret_requests.append(request)
        if self.create_error is not None:
            raise self.create_error
        self.secret = secretmanager_v1.Secret(request.secret)
        self.secret.name = f"{request.parent}/secrets/{request.secret_id}"
        return self.secret

    def list_secret_versions(
        self,
        request: Any,
    ) -> list[secretmanager_v1.SecretVersion]:
        self.list_version_requests.append(request)
        return list(self.versions)

    def get_secret_version(self, request: Any) -> secretmanager_v1.SecretVersion:
        self.get_version_requests.append(request)
        try:
            return self.version_lookup[request.name]
        except KeyError:
            raise NotFound("missing version") from None

    def get_iam_policy(self, request: Any) -> policy_pb2.Policy:
        self.get_policy_requests.append(request)
        copied = policy_pb2.Policy()
        copied.CopyFrom(self.policy)
        return copied

    def set_iam_policy(self, request: Any) -> policy_pb2.Policy:
        self.set_policy_requests.append(request)
        self.policy = policy_pb2.Policy()
        self.policy.CopyFrom(request.policy)
        copied = policy_pb2.Policy()
        copied.CopyFrom(self.policy)
        return copied

    def add_secret_version(self, request: Any) -> secretmanager_v1.SecretVersion:
        self.add_version_requests.append(request)
        self.version_lookup[self.add_response.name] = self.add_response
        return self.add_response

    def disable_secret_version(self, request: Any) -> secretmanager_v1.SecretVersion:
        self.disable_version_requests.append(request)
        version = self.version_lookup[request.name]
        disabled = secretmanager_v1.SecretVersion(
            name=version.name,
            state=secretmanager_v1.SecretVersion.State.DISABLED,
            client_specified_payload_checksum=(
                version.client_specified_payload_checksum
            ),
        )
        self.version_lookup[request.name] = disabled
        return disabled

    def destroy_secret_version(self, request: Any) -> secretmanager_v1.SecretVersion:
        self.destroy_version_requests.append(request)
        version = self.version_lookup[request.name]
        destroyed = secretmanager_v1.SecretVersion(
            name=version.name,
            state=secretmanager_v1.SecretVersion.State.DESTROYED,
            client_specified_payload_checksum=(
                version.client_specified_payload_checksum
            ),
        )
        self.version_lookup[request.name] = destroyed
        return destroyed


def adapter(
    *,
    client: FakeSecretManagerClient | None = None,
    record: SecretMetadata | None = None,
) -> tuple[SecretManagerAdapter, FakeSecretManagerClient]:
    store = MemoryStore()
    store.create_secret_metadata(record or secret_record())
    fake = client or FakeSecretManagerClient()
    return (
        SecretManagerAdapter(
            client=fake,
            store=store,
            project_id=PROJECT_ID,
            version_manager_service_account=(
                f"mim-control-plane@{PROJECT_ID}.iam.gserviceaccount.com"
            ),
        ),
        fake,
    )


class SecretManagerMetadataTests(unittest.TestCase):
    def test_resolve_reads_only_metadata_and_audits_exact_secret_level_iam(
        self,
    ) -> None:
        manager, client = adapter()

        result = manager.resolve(
            workload_id=WORKLOAD_ID,
            attachments=(
                SecretAttachmentReference(
                    secret_id=SECRET_ID,
                    secret_version=3,
                    metadata_version=1,
                ),
            ),
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].secret_id, SECRET_ID)
        self.assertEqual(result[0].secret_name, PROVIDER_SECRET_NAME)
        self.assertEqual(result[0].secret_version, "3")
        self.assertEqual(result[0].env_name, "MIM_SECRET_SLACK_SHARED")
        self.assertEqual(client.get_secret_requests[0].name, SECRET_NAME)
        self.assertEqual(client.list_version_requests[0].parent, SECRET_NAME)
        self.assertEqual(client.list_version_requests[0].filter, "state:ENABLED")
        self.assertEqual(client.get_policy_requests[0].resource, SECRET_NAME)
        self.assertFalse(hasattr(client, "access_secret_version_requests"))
        self.assertNotIn("data", repr(result))

    def test_resolve_separates_logical_env_name_from_physical_provider_secret_name(
        self,
    ) -> None:
        manager, _ = adapter()

        result = manager.resolve(
            workload_id=WORKLOAD_ID,
            attachments=(
                SecretAttachmentReference(
                    secret_id=SECRET_ID,
                    secret_version=3,
                    metadata_version=1,
                ),
            ),
        )

        self.assertEqual(result[0].env_name, "MIM_SECRET_SLACK_SHARED")
        self.assertEqual(result[0].secret_name, PROVIDER_SECRET_NAME)
        self.assertNotEqual(result[0].env_name, result[0].secret_name)

    def test_resolve_rejects_extra_enabled_versions_or_unexpected_resource_names(
        self,
    ) -> None:
        cases = (
            [enabled_version(2), enabled_version(3)],
            [
                secretmanager_v1.SecretVersion(
                    name=(
                        "projects/other-project/secrets/slack-shared/versions/3"
                    ),
                    state=secretmanager_v1.SecretVersion.State.ENABLED,
                )
            ],
        )
        for versions in cases:
            with self.subTest(versions=versions):
                client = FakeSecretManagerClient()
                client.versions = versions
                manager, _ = adapter(client=client)
                with self.assertRaises(SecretMetadataDeniedError):
                    manager.resolve(
                        workload_id=WORKLOAD_ID,
                        attachments=(
                            SecretAttachmentReference(
                                secret_id=SECRET_ID,
                                secret_version=3,
                                metadata_version=1,
                            ),
                        ),
                    )

    def test_resolve_rejects_missing_or_expanded_managed_iam_bindings(self) -> None:
        malformed_policies = (
            policy_pb2.Policy(
                bindings=(
                    policy_pb2.Binding(
                        role="roles/secretmanager.secretAccessor",
                        members=(
                            RUNTIME_MEMBER,
                            (
                                "serviceAccount:unexpected@"
                                f"{PROJECT_ID}.iam.gserviceaccount.com"
                            ),
                        ),
                    ),
                    policy_pb2.Binding(
                        role="roles/secretmanager.secretVersionManager",
                        members=(VERSION_MANAGER_MEMBER,),
                    ),
                )
            ),
            policy_pb2.Policy(
                bindings=(
                    policy_pb2.Binding(
                        role="roles/secretmanager.secretAccessor",
                        members=(RUNTIME_MEMBER,),
                    ),
                )
            ),
            policy_pb2.Policy(
                bindings=tuple(exact_policy().bindings)
                + (
                    policy_pb2.Binding(
                        role="roles/viewer",
                        members=("serviceAccount:unexpected@example.invalid",),
                    ),
                )
            ),
            policy_pb2.Policy(
                bindings=(
                    policy_pb2.Binding(
                        role="roles/secretmanager.secretAccessor",
                        members=(RUNTIME_MEMBER,),
                        condition=expr_pb2.Expr(expression="true"),
                    ),
                    policy_pb2.Binding(
                        role="roles/secretmanager.secretVersionManager",
                        members=(VERSION_MANAGER_MEMBER,),
                    ),
                )
            ),
        )
        for iam_policy in malformed_policies:
            with self.subTest(policy=iam_policy):
                client = FakeSecretManagerClient()
                client.policy = iam_policy
                manager, _ = adapter(client=client)
                with self.assertRaises(SecretMetadataDeniedError):
                    manager.resolve(
                        workload_id=WORKLOAD_ID,
                        attachments=(
                            SecretAttachmentReference(
                                secret_id=SECRET_ID,
                                secret_version=3,
                                metadata_version=1,
                            ),
                        ),
                    )

    def test_resolve_requires_client_verified_crc_metadata(self) -> None:
        client = FakeSecretManagerClient()
        client.versions = [
            secretmanager_v1.SecretVersion(
                name=VERSION_NAME,
                state=secretmanager_v1.SecretVersion.State.ENABLED,
                client_specified_payload_checksum=False,
            )
        ]
        manager, _ = adapter(client=client)

        with self.assertRaises(SecretMetadataDeniedError):
            manager.resolve(
                workload_id=WORKLOAD_ID,
                attachments=(
                    SecretAttachmentReference(
                        secret_id=SECRET_ID,
                        secret_version=3,
                        metadata_version=1,
                    ),
                ),
            )

    def test_resolve_rejects_duplicate_references_to_the_same_secret(self) -> None:
        manager, client = adapter()
        attachment = SecretAttachmentReference(
            secret_id=SECRET_ID,
            secret_version=3,
            metadata_version=1,
        )

        with self.assertRaises(SecretMetadataDeniedError):
            manager.resolve(
                workload_id=WORKLOAD_ID,
                attachments=(attachment, attachment),
            )

        self.assertEqual(len(client.get_secret_requests), 1)

    def test_resolve_audits_every_attached_workload_identity(self) -> None:
        client = FakeSecretManagerClient()
        client.policy = exact_policy((RUNTIME_MEMBER, RUNTIME_MEMBER_2))
        manager, _ = adapter(
            client=client,
            record=secret_record(
                attached_workload_ids=(WORKLOAD_ID, WORKLOAD_ID_2),
            ),
        )

        result = manager.resolve(
            workload_id=WORKLOAD_ID,
            attachments=(
                SecretAttachmentReference(
                    secret_id=SECRET_ID,
                    secret_version=3,
                    metadata_version=1,
                ),
            ),
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].env_name, "MIM_SECRET_SLACK_SHARED")

    def test_resolve_rejects_locked_metadata_before_any_cloud_call(self) -> None:
        manager, client = adapter(
            record=secret_record(lifecycle_state=SecretLifecycleState.LOCKED)
        )

        with self.assertRaises(SecretMetadataDeniedError):
            manager.resolve(
                workload_id=WORKLOAD_ID,
                attachments=(
                    SecretAttachmentReference(
                        secret_id=SECRET_ID,
                        secret_version=3,
                        metadata_version=1,
                    ),
                ),
            )

        self.assertEqual(client.get_secret_requests, [])

    def test_constructor_rejects_cross_project_or_non_service_identity(self) -> None:
        for identity in (
            "person@madup.com",
            "mim-control-plane@other-project.iam.gserviceaccount.com",
            f"mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com",
            "default",
        ):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    SecretManagerAdapter(
                        client=FakeSecretManagerClient(),
                        store=MemoryStore(),
                        project_id=PROJECT_ID,
                        version_manager_service_account=identity,
                    )


class SecretManagerWriteTests(unittest.TestCase):
    def test_ensure_secret_creates_exact_managed_secret_and_reconciles_bindings(
        self,
    ) -> None:
        manager, client = adapter()
        client.secret = None

        result = manager.ensure_secret(
            secret_id=SecretId(SECRET_ID),
            workload_ids=(WORKLOAD_ID,),
        )

        self.assertIsInstance(result, ManagedSecretMetadata)
        self.assertEqual(result.name, SECRET_NAME)
        self.assertTrue(result.created)
        request = client.create_secret_requests[0]
        self.assertEqual(request.parent, f"projects/{PROJECT_ID}")
        self.assertEqual(request.secret_id, PROVIDER_SECRET_NAME)
        self.assertEqual(
            dict(request.secret.labels),
            {"managed-by": "mim-control-plane"},
        )
        self.assertEqual(
            request.secret.replication._pb.WhichOneof("replication"),
            "automatic",
        )
        self.assertEqual(client.set_policy_requests[0].resource, SECRET_NAME)

    def test_ensure_secret_rejects_existing_non_mim_secret_shape(self) -> None:
        drifted_secrets = (
            exact_secret(labels={"managed-by": "human"}),
            exact_secret(automatic=False),
            exact_secret(version_aliases={"active": 3}),
        )
        for secret in drifted_secrets:
            with self.subTest(secret=secret):
                manager, client = adapter()
                client.secret = secret
                with self.assertRaises(SecretManagerAdapterError):
                    manager.ensure_secret(
                        secret_id=SecretId(SECRET_ID),
                        workload_ids=(WORKLOAD_ID,),
                    )

    def test_rotate_secret_creates_new_version_and_returns_metadata_only(
        self,
    ) -> None:
        manager, client = adapter()

        result = manager.rotate_secret(
            secret_id=SecretId(SECRET_ID),
            workload_ids=(WORKLOAD_ID,),
            payload=b"rotated-secret",
        )

        self.assertIsInstance(result, SecretVersionMetadata)
        self.assertEqual(result.name, f"{SECRET_NAME}/versions/4")
        self.assertEqual(result.version, 4)
        self.assertEqual(result.state, "enabled")
        self.assertTrue(result.checksum_verified)
        self.assertEqual(
            client.get_version_requests[0].name,
            f"{SECRET_NAME}/versions/4",
        )
        self.assertEqual(client.set_policy_requests[0].resource, SECRET_NAME)

    def test_provider_identity_never_uses_logical_name_even_when_names_collide(
        self,
    ) -> None:
        store = MemoryStore()
        store.create_secret_metadata(secret_record())
        store.create_secret_metadata(
            SecretMetadata(
                id=SecretId(SECOND_SECRET_ID),
                owner_id=UserId("usr-2"),
                name="slack-shared",
                integration_type="slack",
                attached_workload_ids=(WORKLOAD_ID,),
                active_version=1,
                rotation_state=SecretRotationState.STABLE,
                lifecycle_state=SecretLifecycleState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        fake = FakeSecretManagerClient()
        fake.secret = None
        manager = SecretManagerAdapter(
            client=fake,
            store=store,
            project_id=PROJECT_ID,
            version_manager_service_account=(
                f"mim-control-plane@{PROJECT_ID}.iam.gserviceaccount.com"
            ),
        )

        manager.ensure_secret(
            secret_id=SecretId(SECOND_SECRET_ID),
            workload_ids=(WORKLOAD_ID,),
        )

        self.assertEqual(
            fake.create_secret_requests[0].secret_id,
            provider_secret_id(SECOND_SECRET_ID),
        )
        self.assertNotEqual(fake.create_secret_requests[0].secret_id, "slack-shared")

    def test_disable_old_version_requires_exact_numeric_resource_and_window(
        self,
    ) -> None:
        manager, client = adapter()
        not_before = NOW + timedelta(days=6)
        bad_resources = (
            f"{SECRET_NAME}/versions/latest",
            "projects/other/secrets/slack-shared/versions/2",
            f"projects/{PROJECT_ID}/secrets/other/versions/2",
        )

        for version_name in bad_resources:
            with self.subTest(version_name=version_name):
                with self.assertRaises(SecretManagerAdapterError):
                    manager.disable_old_version(
                        secret_id=SecretId(SECRET_ID),
                        version_name=version_name,
                        active_version=4,
                        retirement_not_before=NOW + timedelta(days=7),
                        now=NOW,
                    )
        with self.assertRaises(SecretManagerAdapterError):
            manager.disable_old_version(
                secret_id=SecretId(SECRET_ID),
                version_name=f"{SECRET_NAME}/versions/4",
                active_version=4,
                retirement_not_before=NOW + timedelta(days=7),
                now=NOW,
            )
        with self.assertRaises(SecretManagerAdapterError):
            manager.disable_old_version(
                secret_id=SecretId(SECRET_ID),
                version_name=f"{SECRET_NAME}/versions/2",
                active_version=4,
                retirement_not_before=not_before,
                now=NOW,
            )
        self.assertEqual(client.disable_version_requests, [])

    def test_disable_old_version_disables_exact_old_numeric_version(self) -> None:
        manager, client = adapter()

        result = manager.disable_old_version(
            secret_id=SecretId(SECRET_ID),
            version_name=f"{SECRET_NAME}/versions/2",
            active_version=4,
            retirement_not_before=NOW + timedelta(days=7),
            now=NOW,
        )

        self.assertIsInstance(result, SecretVersionStateMetadata)
        self.assertEqual(result.name, f"{SECRET_NAME}/versions/2")
        self.assertEqual(result.version, 2)
        self.assertEqual(result.state, "disabled")
        self.assertEqual(
            client.disable_version_requests[0].name,
            f"{SECRET_NAME}/versions/2",
        )
        self.assertEqual(
            client.get_version_requests[-1].name,
            f"{SECRET_NAME}/versions/2",
        )

    def test_destroy_old_version_requires_due_window_and_disabled_state(
        self,
    ) -> None:
        manager, client = adapter()
        client.version_lookup[f"{SECRET_NAME}/versions/2"] = enabled_version(2)

        with self.assertRaises(SecretManagerAdapterError):
            manager.destroy_old_version(
                secret_id=SecretId(SECRET_ID),
                version_name=f"{SECRET_NAME}/versions/2",
                active_version=4,
                retirement_not_before=NOW + timedelta(days=7),
                now=NOW + timedelta(days=6),
            )
        with self.assertRaises(SecretManagerAdapterError):
            manager.destroy_old_version(
                secret_id=SecretId(SECRET_ID),
                version_name=f"{SECRET_NAME}/versions/2",
                active_version=4,
                retirement_not_before=NOW + timedelta(days=7),
                now=NOW + timedelta(days=7),
            )
        self.assertEqual(client.destroy_version_requests, [])

    def test_destroy_old_version_destroys_exact_disabled_version_once_due(self) -> None:
        manager, client = adapter()
        client.version_lookup[f"{SECRET_NAME}/versions/2"] = disabled_version(2)

        result = manager.destroy_old_version(
            secret_id=SecretId(SECRET_ID),
            version_name=f"{SECRET_NAME}/versions/2",
            active_version=4,
            retirement_not_before=NOW + timedelta(days=7),
            now=NOW + timedelta(days=7, minutes=1),
        )

        self.assertIsInstance(result, SecretVersionStateMetadata)
        self.assertEqual(result.name, f"{SECRET_NAME}/versions/2")
        self.assertEqual(result.version, 2)
        self.assertEqual(result.state, "destroyed")
        self.assertEqual(
            client.destroy_version_requests[0].name,
            f"{SECRET_NAME}/versions/2",
        )
        self.assertEqual(
            client.get_version_requests[-1].name,
            f"{SECRET_NAME}/versions/2",
        )

    def test_add_version_sends_crc32c_and_returns_metadata_only(self) -> None:
        manager, client = adapter()
        payload = b"synthetic-integration-value"

        result = manager.add_version(secret_id=SecretId(SECRET_ID), payload=payload)

        self.assertIsInstance(result, SecretVersionMetadata)
        self.assertEqual(result.name, f"{SECRET_NAME}/versions/4")
        self.assertEqual(result.version, 4)
        self.assertTrue(result.checksum_verified)
        self.assertFalse(hasattr(result, "data"))
        request = client.add_version_requests[0]
        self.assertEqual(request.parent, SECRET_NAME)
        self.assertEqual(request.payload.data, payload)
        expected_crc = int.from_bytes(google_crc32c.Checksum(payload).digest(), "big")
        self.assertEqual(request.payload.data_crc32c, expected_crc)
        self.assertNotIn(payload.decode(), repr(result))

    def test_add_version_rejects_unverified_checksum_and_never_echoes_value(
        self,
    ) -> None:
        manager, client = adapter()
        client.add_response = secretmanager_v1.SecretVersion(
            name=f"{SECRET_NAME}/versions/4",
            state=secretmanager_v1.SecretVersion.State.ENABLED,
            client_specified_payload_checksum=False,
        )
        payload = b"do-not-echo-this-value"

        with self.assertRaises(SecretManagerAdapterError) as caught:
            manager.add_version(secret_id=SecretId(SECRET_ID), payload=payload)

        self.assertNotIn(payload.decode(), str(caught.exception))

    def test_ensure_exact_bindings_replaces_unexpected_secret_bindings(self) -> None:
        manager, client = adapter()
        client.policy = policy_pb2.Policy(
            version=3,
            etag=b"before",
            bindings=(
                policy_pb2.Binding(
                    role="roles/viewer",
                    members=("serviceAccount:unrelated@example.invalid",),
                ),
            ),
        )

        manager.ensure_exact_bindings(
            secret_id=SecretId(SECRET_ID),
            workload_ids=(WORKLOAD_ID,),
        )

        request = client.set_policy_requests[0]
        self.assertEqual(request.resource, SECRET_NAME)
        self.assertEqual(request.policy.etag, b"before")
        bindings = {
            binding.role: set(binding.members)
            for binding in request.policy.bindings
        }
        self.assertEqual(
            bindings["roles/secretmanager.secretAccessor"],
            {RUNTIME_MEMBER},
        )
        self.assertEqual(
            bindings["roles/secretmanager.viewer"],
            {METADATA_READER_MEMBER},
        )
        self.assertEqual(
            bindings["roles/secretmanager.secretVersionManager"],
            {VERSION_MANAGER_MEMBER},
        )
        self.assertNotIn("roles/viewer", bindings)

    def test_ensure_exact_bindings_rejects_empty_duplicate_or_excess_attachments(
        self,
    ) -> None:
        invalid_workload_ids = (
            (),
            (WORKLOAD_ID, WORKLOAD_ID),
            tuple(WorkloadId(f"wrk-{index}") for index in range(6)),
        )
        for workload_ids in invalid_workload_ids:
            with self.subTest(workload_ids=workload_ids):
                manager, client = adapter()
                with self.assertRaises(SecretMetadataDeniedError):
                    manager.ensure_exact_bindings(
                        secret_id=SecretId(SECRET_ID),
                        workload_ids=workload_ids,
                    )
                self.assertEqual(client.get_policy_requests, [])

    def test_resolve_rejects_missing_metadata_reader_binding(self) -> None:
        client = FakeSecretManagerClient()
        client.policy = policy_pb2.Policy(
            bindings=(
                policy_pb2.Binding(
                    role="roles/secretmanager.secretAccessor",
                    members=(RUNTIME_MEMBER,),
                ),
                policy_pb2.Binding(
                    role="roles/secretmanager.secretVersionManager",
                    members=(VERSION_MANAGER_MEMBER,),
                ),
            )
        )
        manager, _ = adapter(client=client)

        with self.assertRaises(SecretMetadataDeniedError):
            manager.resolve(
                workload_id=WORKLOAD_ID,
                attachments=(
                    SecretAttachmentReference(
                        secret_id=SECRET_ID,
                        secret_version=3,
                        metadata_version=1,
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
