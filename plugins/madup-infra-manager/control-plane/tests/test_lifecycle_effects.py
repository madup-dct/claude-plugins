from __future__ import annotations

import hashlib
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import run_v2, scheduler_v1
from google.iam.v1 import iam_policy_pb2, policy_pb2
from google.protobuf import duration_pb2

from mim_control_plane.adapters.firestore_slack_oauth import (
    FirestoreSlackOAuthRepository,
)
from mim_control_plane.adapters.lifecycle_effects import (
    LifecycleAuditNotifier,
    LifecycleAuditSessionGate,
    LifecycleAuditTransferManager,
    LifecycleComputeManager,
    LifecycleEffectsError,
    LifecycleIapAccessManager,
    LifecycleScheduleManager,
    LifecycleSecretBindingManager,
    LifecycleSlackGrantManager,
)
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import (
    AppHostnameBindingState,
    AuditEventId,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthIdentityLink,
    SlackOAuthIdentityLinkState,
    SlackOAuthInstallState,
    SlackOAuthSharedInstall,
)
from mim_control_plane.domain.states import (
    ScheduleState,
    SecretLifecycleState,
    SecretRotationState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.store import VersionConflict
from mim_control_plane.services.app_hostname import AppHostnameBindingService
from mim_control_plane.services.runtime_naming import provider_secret_id

NOW = datetime(2026, 8, 4, 9, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
PROJECT_NUMBER = "123456789012"
REGION = "asia-northeast3"
SECRET_ID = "sec-0123456789abcdefabcd"
ADMIN_MEMBERS = (
    "group:mim-admins@madup.com",
    "user:operator@madup.com",
)


def workload_suffix(workload_id: str) -> str:
    return hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]


def runtime_member(workload_id: str) -> str:
    return (
        "serviceAccount:mim-wrk-"
        f"{workload_suffix(workload_id)}@{PROJECT_ID}.iam.gserviceaccount.com"
    )


def service_name(workload_id: str) -> str:
    return (
        f"projects/{PROJECT_ID}/locations/{REGION}/services/"
        f"mim-svc-{workload_suffix(workload_id)}"
    )


def job_name(workload_id: str) -> str:
    return (
        f"projects/{PROJECT_ID}/locations/{REGION}/jobs/"
        f"mim-job-{workload_suffix(workload_id)}"
    )


def scheduler_job_name(schedule_id: str) -> str:
    digest = hashlib.sha256(schedule_id.encode("utf-8")).hexdigest()[:20]
    return f"projects/{PROJECT_ID}/locations/{REGION}/jobs/mim-sch-{digest}"


def user(*, state: UserState, version: int = 1) -> User:
    return User(
        id=UserId("usr-1"),
        email="person@madup.com",
        role=UserRole.USER,
        state=state,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW - timedelta(minutes=5),
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(minutes=1),
        version=version,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    kind: WorkloadKind = WorkloadKind.NEXTJS,
    state: WorkloadState = WorkloadState.ACTIVE,
    version: int = 1,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name="sample",
        kind=kind,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-1",
        created_at=NOW - timedelta(days=20),
        updated_at=NOW - timedelta(minutes=2),
        last_activity_at=NOW - timedelta(minutes=2),
        last_healthy_image_digest="sha256:" + "b" * 64,
        version=version,
    )


def schedule(
    *,
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
    state: ScheduleState = ScheduleState.ENABLED,
    version: int = 1,
) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(minutes=3),
        version=version,
    )


def secret_record(
    *,
    secret_id: str = SECRET_ID,
    attached_workload_ids: tuple[WorkloadId, ...] = (
        WorkloadId("wrk-1"),
        WorkloadId("wrk-2"),
    ),
    lifecycle_state: SecretLifecycleState = SecretLifecycleState.ACTIVE,
    version: int = 1,
) -> SecretMetadata:
    return SecretMetadata(
        id=SecretId(secret_id),
        owner_id=UserId("usr-1"),
        name="slack-shared",
        integration_type="slack",
        attached_workload_ids=attached_workload_ids,
        active_version=3,
        rotation_state=SecretRotationState.STABLE,
        lifecycle_state=lifecycle_state,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(minutes=4),
        version=version,
    )


def shared_install(
    *,
    install_id: str,
    team_id: str,
) -> SlackOAuthSharedInstall:
    return SlackOAuthSharedInstall(
        install_id=install_id,
        app_id="A123",
        team_id=team_id,
        enterprise_id=None,
        is_enterprise_install=False,
        granted_scopes=("commands", "chat:write"),
        secret_ref=f"projects/{PROJECT_ID}/secrets/slack-{team_id}/versions/1",
        installer_mim_user_id=UserId("admin-1"),
        installer_email="admin@madup.com",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
        state=SlackOAuthInstallState.ACTIVE,
    )


def identity_link(
    *,
    install_id: str,
    team_id: str,
    slack_user_id: str,
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
        created_at=NOW - timedelta(days=1),
        updated_at=revoked_at or NOW - timedelta(hours=2),
        state=state,
        revoked_at=revoked_at,
    )


@dataclass
class FakeIapResponse:
    body: dict[str, object]
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("iap error")

    def json(self) -> object:
        return self.body


class FakeIapSession:
    def __init__(self, responses: tuple[FakeIapResponse, ...]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> FakeIapResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError("unexpected IAP call")
        return self._responses.pop(0)


class FakeSecretManagerClient:
    def __init__(
        self,
        *,
        workload_ids: tuple[str, ...] = ("wrk-1", "wrk-2"),
    ) -> None:
        bindings = []
        if workload_ids:
            bindings.append(
                policy_pb2.Binding(
                    role="roles/secretmanager.secretAccessor",
                    members=tuple(runtime_member(item) for item in workload_ids),
                )
            )
        bindings.append(
            policy_pb2.Binding(
                role="roles/secretmanager.viewer",
                members=(
                    f"serviceAccount:mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com",
                ),
            )
        )
        bindings.append(
            policy_pb2.Binding(
                role="roles/secretmanager.secretVersionManager",
                members=(
                    f"serviceAccount:mim-control-plane@{PROJECT_ID}.iam.gserviceaccount.com",
                ),
            )
        )
        self.policy = policy_pb2.Policy(
            version=3,
            etag=b"etag-1",
            bindings=tuple(bindings),
        )
        self.get_policy_requests: list[Any] = []
        self.set_policy_requests: list[Any] = []
        self.access_secret_version_calls = 0

    def get_iam_policy(self, request: Any) -> policy_pb2.Policy:
        self.get_policy_requests.append(request)
        copied = policy_pb2.Policy()
        copied.CopyFrom(self.policy)
        return copied

    def set_iam_policy(self, request: Any) -> policy_pb2.Policy:
        self.set_policy_requests.append(request)
        copied = policy_pb2.Policy()
        copied.CopyFrom(request.policy)
        self.policy = copied
        return copied

    def access_secret_version(self, *_: object, **__: object) -> None:
        self.access_secret_version_calls += 1
        raise AssertionError("payload API must never be called")


class FakeSchedulerClient:
    def __init__(self) -> None:
        self.jobs: dict[str, scheduler_v1.Job] = {}
        self.pause_requests: list[Any] = []
        self.delete_requests: list[Any] = []

    def get_job(self, request: Any) -> scheduler_v1.Job:
        try:
            return self.jobs[request.name]
        except KeyError:
            raise NotFound("missing") from None

    def pause_job(self, request: Any) -> scheduler_v1.Job:
        self.pause_requests.append(request)
        job = scheduler_v1.Job(self.jobs[request.name])
        job.state = scheduler_v1.Job.State.PAUSED
        self.jobs[request.name] = job
        return job

    def delete_job(self, request: Any) -> object:
        self.delete_requests.append(request)
        self.jobs.pop(request.name, None)
        return object()


class FakeOperation:
    def __init__(self) -> None:
        self.completed = False

    def result(self) -> None:
        self.completed = True


class FakeServicesClient:
    def __init__(self) -> None:
        self.existing: dict[str, run_v2.Service] = {}
        self.iam_policies: dict[str, policy_pb2.Policy] = {}
        self.delete_requests: list[str] = []
        self.delete_operations: list[FakeOperation] = []

    def get_service(self, *, name: str, **_: object) -> run_v2.Service:
        try:
            return self.existing[name]
        except KeyError:
            raise NotFound("missing") from None

    def get_iam_policy(
        self,
        request: iam_policy_pb2.GetIamPolicyRequest,
    ) -> policy_pb2.Policy:
        return self.iam_policies[request.resource]

    def delete_service(self, *, name: str, **_: object) -> FakeOperation:
        self.delete_requests.append(name)
        self.existing.pop(name, None)
        operation = FakeOperation()
        self.delete_operations.append(operation)
        return operation


class ConflictOnRetireStore(MemoryStore):
    def __init__(self, public_host: str) -> None:
        super().__init__()
        self._public_host = public_host

    def save_app_hostname_binding(  # type: ignore[override]
        self,
        binding,
        *,
        expected_version: int,
    ):
        if (
            binding.public_host == self._public_host
            and binding.state is AppHostnameBindingState.RETIRED
        ):
            raise VersionConflict("synthetic binding retire conflict")
        return super().save_app_hostname_binding(
            binding,
            expected_version=expected_version,
        )


class ConflictOnDisableStore(MemoryStore):
    def __init__(self, public_host: str) -> None:
        super().__init__()
        self._public_host = public_host

    def save_app_hostname_binding(  # type: ignore[override]
        self,
        binding,
        *,
        expected_version: int,
    ):
        if (
            binding.public_host == self._public_host
            and binding.state is AppHostnameBindingState.DISABLED
        ):
            raise VersionConflict("synthetic binding disable conflict")
        return super().save_app_hostname_binding(
            binding,
            expected_version=expected_version,
        )


class FakeJobsClient:
    def __init__(self) -> None:
        self.existing: dict[str, run_v2.Job] = {}
        self.iam_policies: dict[str, policy_pb2.Policy] = {}
        self.delete_requests: list[str] = []
        self.delete_operations: list[FakeOperation] = []

    def get_job(self, *, name: str, **_: object) -> run_v2.Job:
        try:
            return self.existing[name]
        except KeyError:
            raise NotFound("missing") from None

    def get_iam_policy(
        self,
        request: iam_policy_pb2.GetIamPolicyRequest,
    ) -> policy_pb2.Policy:
        return self.iam_policies[request.resource]

    def delete_job(self, *, name: str, **_: object) -> FakeOperation:
        self.delete_requests.append(name)
        self.existing.pop(name, None)
        operation = FakeOperation()
        self.delete_operations.append(operation)
        return operation


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
        return FakeSnapshot(self.id, data is not None, data)

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
            raise AssertionError("unexpected operator")
        return FakeQuery(self._collection, self._filters + ((field_name, value),))

    def stream(self) -> tuple[FakeSnapshot, ...]:
        return tuple(
            FakeSnapshot(document_id, True, data)
            for document_id, data in self._collection.documents.items()
            if all(
                data.get(field_name) == expected
                for field_name, expected in self._filters
            )
        )


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


def exact_iap_policy(*, members: tuple[str, ...]) -> dict[str, object]:
    return {
        "version": 3,
        "etag": "etag-1",
        "bindings": [
            {
                "role": "roles/iap.httpsResourceAccessor",
                "members": list(members),
            }
        ],
    }


def safe_service(workload_id: str = "wrk-1") -> run_v2.Service:
    suffix = workload_suffix(workload_id)
    return run_v2.Service(
        name=service_name(workload_id),
        uri=f"https://mim-svc-{suffix}-uc.a.run.app",
        labels={
            "managed-by": "mim-control-plane",
            "workload-hash": suffix,
            "owner-hash": workload_suffix("usr-1"),
        },
        ingress=run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL,
        iap_enabled=False,
        invoker_iam_disabled=False,
        latest_ready_revision=f"{service_name(workload_id)}/revisions/rev-1",
        traffic=(
            run_v2.TrafficTarget(
                percent=100,
                type_=(
                    run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
                ),
            ),
        ),
        template=run_v2.RevisionTemplate(
            service_account=runtime_member(workload_id).removeprefix("serviceAccount:"),
            scaling=run_v2.RevisionScaling(
                min_instance_count=0,
                max_instance_count=1,
            ),
            max_instance_request_concurrency=20,
            timeout=duration_pb2.Duration(seconds=300),
            containers=(
                run_v2.Container(
                    image=(
                        f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads"
                        "@sha256:" + "a" * 64
                    ),
                    command=("./node_modules/.bin/next",),
                    args=("start", "--hostname", "0.0.0.0", "--port", "8080"),
                    resources=run_v2.ResourceRequirements(
                        limits={"cpu": "1", "memory": "512Mi"},
                        cpu_idle=True,
                        startup_cpu_boost=False,
                    ),
                ),
            ),
        ),
    )


def safe_service_policy(
    *,
    reviewed_breakglass_members: tuple[str, ...] = ADMIN_MEMBERS,
) -> policy_pb2.Policy:
    policy = policy_pb2.Policy(etag=b"etag-1")
    binding = policy.bindings.add(role="roles/run.invoker")
    binding.members.extend(
        (
            f"serviceAccount:mim-app-gateway@{PROJECT_ID}.iam.gserviceaccount.com",
            *reviewed_breakglass_members,
        )
    )
    return policy


def create_active_binding(
    store: MemoryStore,
    *,
    workload_id: str = "wrk-1",
    state: AppHostnameBindingState = AppHostnameBindingState.ACTIVE,
) -> str:
    target = store.get_workload(WorkloadId(workload_id))
    binding = AppHostnameBindingService(store=store).create_active_binding(
        workload=target,
        service_resource=service_name(workload_id),
        service_uri=f"https://mim-svc-{workload_suffix(workload_id)}-uc.a.run.app",
        now=NOW,
    )
    if state is AppHostnameBindingState.ACTIVE:
        return binding.public_host
    updated = store.save_app_hostname_binding(
        binding.transition_state(state, at=NOW + timedelta(minutes=1)),
        expected_version=binding.version,
    )
    return updated.public_host


def safe_scheduler_job(
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
) -> scheduler_v1.Job:
    audience = f"https://mim-schedule-gateway-{PROJECT_NUMBER}.{REGION}.run.app"
    body = b'{"schedule_id":"sch-1","workload_id":"wrk-1"}'
    return scheduler_v1.Job(
        name=scheduler_job_name(schedule_id),
        description=(
            "MIM managed hourly schedule "
            f"{hashlib.sha256(schedule_id.encode('utf-8')).hexdigest()[:20]}"
        ),
        http_target=scheduler_v1.HttpTarget(
            uri=f"{audience}/v1/schedules/execute",
            http_method=scheduler_v1.HttpMethod.POST,
            headers={"Content-Type": "application/json"},
            body=body,
            oidc_token=scheduler_v1.OidcToken(
                service_account_email=f"mim-schedule-gateway@{PROJECT_ID}.iam.gserviceaccount.com",
                audience=audience,
            ),
        ),
        schedule="0 * * * *",
        time_zone="Asia/Seoul",
        retry_config=scheduler_v1.RetryConfig(retry_count=0),
        attempt_deadline=duration_pb2.Duration(seconds=30),
        state=scheduler_v1.Job.State.ENABLED,
    )


def deprecated_direct_scheduler_job(
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
) -> scheduler_v1.Job:
    return scheduler_v1.Job(
        name=scheduler_job_name(schedule_id),
        description=(
            "MIM managed hourly schedule "
            f"{hashlib.sha256(schedule_id.encode('utf-8')).hexdigest()[:20]}"
        ),
        http_target=scheduler_v1.HttpTarget(
            uri=f"https://run.googleapis.com/v2/{job_name(workload_id)}:run",
            http_method=scheduler_v1.HttpMethod.POST,
            headers={"Content-Type": "application/json"},
            body=b"{}",
            oauth_token=scheduler_v1.OAuthToken(
                service_account_email=(
                    f"mim-schedule-gateway@{PROJECT_ID}.iam.gserviceaccount.com"
                ),
                scope="https://www.googleapis.com/auth/cloud-platform",
            ),
        ),
        schedule="0 * * * *",
        time_zone="Asia/Seoul",
        retry_config=scheduler_v1.RetryConfig(retry_count=0),
        attempt_deadline=duration_pb2.Duration(seconds=30),
        state=scheduler_v1.Job.State.ENABLED,
    )


class LifecycleEffectsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(user(state=UserState.OFFBOARDED, version=1))
        self.store.create_workload(workload(version=1))
        self.store.create_workload(workload(workload_id="wrk-2", version=1))

    def test_remove_owner_access_preserves_admin_members_and_is_idempotent(
        self,
    ) -> None:
        public_host = create_active_binding(self.store)
        manager = LifecycleIapAccessManager(
            store=self.store,
            session=FakeIapSession(responses=()),
            project_number=PROJECT_NUMBER,
            admin_members=ADMIN_MEMBERS,
        )

        manager.remove_owner_access(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            reason="offboarded",
        )
        manager.remove_owner_access(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            reason="offboarded",
        )

        self.assertEqual(
            self.store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.DISABLED,
        )
        self.assertEqual(len(self.store.list_audit_events()), 1)

    def test_remove_owner_access_fails_closed_when_user_reactivated(self) -> None:
        store = MemoryStore()
        store.create_user(user(state=UserState.ACTIVE, version=1))
        store.create_workload(workload(version=1))
        public_host = create_active_binding(store)
        manager = LifecycleIapAccessManager(
            store=store,
            session=FakeIapSession(responses=()),
            project_number=PROJECT_NUMBER,
            admin_members=ADMIN_MEMBERS,
        )

        with self.assertRaises(LifecycleEffectsError):
            manager.remove_owner_access(
                workload_id=WorkloadId("wrk-1"),
                expected_workload_version=1,
                reason="offboarded",
            )

        self.assertEqual(
            store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.ACTIVE,
        )

    def test_remove_owner_access_allows_active_user_cost_pause(self) -> None:
        store = MemoryStore()
        store.create_user(user(state=UserState.ACTIVE, version=1))
        store.create_workload(workload(version=1))
        public_host = create_active_binding(store)
        manager = LifecycleIapAccessManager(
            store=store,
            session=FakeIapSession(responses=()),
            project_number=PROJECT_NUMBER,
            admin_members=ADMIN_MEMBERS,
        )

        manager.remove_owner_access(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            reason="user_cost_pause",
        )

        self.assertEqual(
            store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.DISABLED,
        )

    def test_remove_owner_access_treats_missing_web_binding_as_already_disabled(
        self,
    ) -> None:
        manager = LifecycleIapAccessManager(
            store=self.store,
            session=FakeIapSession(responses=()),
            project_number=PROJECT_NUMBER,
            admin_members=ADMIN_MEMBERS,
        )

        manager.remove_owner_access(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            reason="offboarded",
        )

        self.assertFalse(self.store.list_audit_events())

    def test_remove_owner_access_fails_closed_when_binding_disable_conflicts(
        self,
    ) -> None:
        seeded = MemoryStore()
        seeded.create_user(user(state=UserState.OFFBOARDED, version=1))
        seeded.create_workload(workload(version=1))
        seeded.create_workload(workload(workload_id="wrk-2", version=1))
        public_host = create_active_binding(seeded)
        store = ConflictOnDisableStore(public_host)
        store.create_user(user(state=UserState.OFFBOARDED, version=1))
        store.create_workload(workload(version=1))
        store.create_workload(workload(workload_id="wrk-2", version=1))
        binding = seeded.get_app_hostname_binding(public_host)
        store.create_app_hostname_binding(binding)
        manager = LifecycleIapAccessManager(
            store=store,
            session=FakeIapSession(responses=()),
            project_number=PROJECT_NUMBER,
            admin_members=ADMIN_MEMBERS,
        )

        with self.assertRaises(LifecycleEffectsError):
            manager.remove_owner_access(
                workload_id=WorkloadId("wrk-1"),
                expected_workload_version=1,
                reason="offboarded",
            )

        self.assertEqual(
            store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.ACTIVE,
        )

    def test_remove_workload_bindings_preserves_other_workloads_and_never_reads_payload(
        self,
    ) -> None:
        self.store.create_secret_metadata(secret_record())
        client = FakeSecretManagerClient()
        manager = LifecycleSecretBindingManager(store=self.store, client=client)

        manager.remove_workload_bindings(
            workload_id=WorkloadId("wrk-1"),
            secret_ids=(SecretId(SECRET_ID),),
            expected_workload_version=1,
        )

        manager.remove_workload_bindings(
            workload_id=WorkloadId("wrk-1"),
            secret_ids=(SecretId(SECRET_ID),),
            expected_workload_version=1,
        )

        request = client.set_policy_requests[0]
        accessor = next(
            binding
            for binding in request.policy.bindings
            if binding.role == "roles/secretmanager.secretAccessor"
        )
        viewer = next(
            binding
            for binding in request.policy.bindings
            if binding.role == "roles/secretmanager.viewer"
        )
        self.assertEqual(list(accessor.members), [runtime_member("wrk-2")])
        self.assertEqual(
            list(viewer.members),
            [f"serviceAccount:mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com"],
        )
        self.assertEqual(
            client.get_policy_requests[0].resource,
            f"projects/mim-prod-123456/secrets/{provider_secret_id(SECRET_ID)}",
        )
        self.assertEqual(len(client.set_policy_requests), 1)
        self.assertEqual(client.access_secret_version_calls, 0)

    def test_remove_last_binding_keeps_only_version_manager(self) -> None:
        self.store.create_secret_metadata(
            secret_record(attached_workload_ids=(WorkloadId("wrk-1"),))
        )
        client = FakeSecretManagerClient(workload_ids=("wrk-1",))
        manager = LifecycleSecretBindingManager(store=self.store, client=client)

        manager.remove_workload_bindings(
            workload_id=WorkloadId("wrk-1"),
            secret_ids=(SecretId(SECRET_ID),),
            expected_workload_version=1,
        )

        self.assertEqual(
            [binding.role for binding in client.policy.bindings],
            ["roles/secretmanager.viewer", "roles/secretmanager.secretVersionManager"],
        )

    def test_revoke_user_grant_revokes_all_active_links_without_uninstalling_shared_app(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        repository = FirestoreSlackOAuthRepository(
            client=client,
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
        )
        repository.save_shared_install(
            shared_install(install_id="a" * 32, team_id="T1")
        )
        repository.save_shared_install(
            shared_install(install_id="b" * 32, team_id="T2")
        )
        repository.save_identity_link(
            identity_link(install_id="a" * 32, team_id="T1", slack_user_id="U1")
        )
        repository.save_identity_link(
            identity_link(install_id="b" * 32, team_id="T2", slack_user_id="U2")
        )
        repository.save_identity_link(
            identity_link(
                install_id="a" * 32,
                team_id="T1",
                slack_user_id="U3",
                mim_user_id="usr-2",
            )
        )
        manager = LifecycleSlackGrantManager(
            store=self.store,
            repository=repository,
            clock=lambda: NOW,
        )

        manager.revoke_user_grant(user_id=UserId("usr-1"), reason="offboarded")

        revoked_a = repository.get_identity_link_by_slack_user(
            install_id="a" * 32,
            team_id="T1",
            slack_user_id="U1",
        )
        revoked_b = repository.get_identity_link_by_slack_user(
            install_id="b" * 32,
            team_id="T2",
            slack_user_id="U2",
        )
        untouched = repository.get_identity_link_by_slack_user(
            install_id="a" * 32,
            team_id="T1",
            slack_user_id="U3",
        )
        install_a = repository.get_shared_install(install_id="a" * 32)
        install_b = repository.get_shared_install(install_id="b" * 32)
        self.assertEqual(revoked_a.state, SlackOAuthIdentityLinkState.REVOKED)
        self.assertEqual(revoked_b.state, SlackOAuthIdentityLinkState.REVOKED)
        self.assertEqual(untouched.state, SlackOAuthIdentityLinkState.ACTIVE)
        self.assertEqual(install_a.state, SlackOAuthInstallState.ACTIVE)
        self.assertEqual(install_b.state, SlackOAuthInstallState.ACTIVE)

    def test_notifier_and_transfer_append_deterministic_audit_events(self) -> None:
        notifier = LifecycleAuditNotifier(store=self.store, clock=lambda: NOW)
        transfer = LifecycleAuditTransferManager(store=self.store, clock=lambda: NOW)
        sessions = LifecycleAuditSessionGate(store=self.store, clock=lambda: NOW)

        notifier.notify_admin(
            user_id=UserId("usr-1"),
            kind="notify_admin",
            reason="offboarded_notification",
        )
        notifier.notify_admin(
            user_id=UserId("usr-1"),
            kind="notify_admin",
            reason="offboarded_notification",
        )
        transfer.open_transfer_window(
            user_id=UserId("usr-1"),
            workload_ids=(WorkloadId("wrk-1"),),
            reason="offboarded_transfer_window",
        )
        sessions.deny_user_sessions(
            user_id=UserId("usr-1"),
            reason="offboarded",
        )

        events = self.store.list_audit_events()
        self.assertEqual(len(events), 3)
        self.assertEqual(
            {event.policy_decision for event in events},
            {
                "offboarded_notification",
                "offboarded_transfer_window",
                "offboarded",
            },
        )
        self.assertEqual(events[0].id, AuditEventId(events[0].id))

    def test_apply_schedule_state_allows_active_user_cost_pause(self) -> None:
        store = MemoryStore()
        store.create_user(user(state=UserState.ACTIVE, version=1))
        store.create_workload(workload(version=1))
        store.create_schedule(schedule(version=1))
        client = FakeSchedulerClient()
        client.jobs[scheduler_job_name("sch-1")] = safe_scheduler_job()
        manager = LifecycleScheduleManager(
            store=store,
            client=client,
            project_number=PROJECT_NUMBER,
        )

        manager.apply_schedule_state(
            schedule_id=ScheduleId("sch-1"),
            workload_id=WorkloadId("wrk-1"),
            target_state=ScheduleState.PAUSED,
            expected_schedule_version=1,
            reason="user_cost_pause",
        )

        self.assertEqual(len(client.pause_requests), 1)

    def test_apply_schedule_state_archives_exact_scheduler_job_and_rejects_drift(
        self,
    ) -> None:
        self.store.create_schedule(schedule(version=1))
        client = FakeSchedulerClient()
        client.jobs[scheduler_job_name("sch-1")] = safe_scheduler_job()
        manager = LifecycleScheduleManager(
            store=self.store,
            client=client,
            project_number=PROJECT_NUMBER,
        )

        manager.apply_schedule_state(
            schedule_id=ScheduleId("sch-1"),
            workload_id=WorkloadId("wrk-1"),
            target_state=ScheduleState.ARCHIVED,
            expected_schedule_version=1,
            reason="offboarded_7d_quarantined",
        )

        self.assertEqual(
            [request.name for request in client.delete_requests],
            [scheduler_job_name("sch-1")],
        )

        self.store.save_schedule(
            schedule(version=2, state=ScheduleState.DISABLED),
            expected_version=1,
        )
        drifted = safe_scheduler_job()
        drifted.http_target.uri = "https://example.com/other"
        client.jobs[scheduler_job_name("sch-1")] = drifted
        with self.assertRaises(LifecycleEffectsError):
            manager.apply_schedule_state(
                schedule_id=ScheduleId("sch-1"),
                workload_id=WorkloadId("wrk-1"),
                target_state=ScheduleState.ARCHIVED,
                expected_schedule_version=2,
                reason="offboarded_7d_quarantined",
            )

    def test_apply_schedule_state_rejects_deprecated_direct_job_target(self) -> None:
        self.store.create_schedule(schedule(version=1))
        client = FakeSchedulerClient()
        client.jobs[scheduler_job_name("sch-1")] = deprecated_direct_scheduler_job()
        manager = LifecycleScheduleManager(
            store=self.store,
            client=client,
            project_number=PROJECT_NUMBER,
        )

        with self.assertRaises(LifecycleEffectsError):
            manager.apply_schedule_state(
                schedule_id=ScheduleId("sch-1"),
                workload_id=WorkloadId("wrk-1"),
                target_state=ScheduleState.PAUSED,
                expected_schedule_version=1,
                reason="offboarded_disable_schedule",
            )

        self.assertEqual(client.pause_requests, [])

    def test_delete_compute_is_idempotent_for_missing_service_and_denies_label_drift(
        self,
    ) -> None:
        public_host = create_active_binding(self.store)
        services = FakeServicesClient()
        jobs = FakeJobsClient()
        scheduler = FakeSchedulerClient()
        manager = LifecycleComputeManager(
            store=self.store,
            services_client=services,
            jobs_client=jobs,
            scheduler_client=scheduler,
            project_number=PROJECT_NUMBER,
            reviewed_breakglass_members=ADMIN_MEMBERS,
        )

        manager.delete_compute(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            target_kinds=("cloud_run_service",),
            retain_image_until=NOW + timedelta(days=30),
        )
        self.assertEqual(services.delete_requests, [])
        self.assertEqual(
            self.store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.RETIRED,
        )

        drifted = safe_service()
        drifted.labels["managed-by"] = "other"
        services.existing[service_name("wrk-1")] = drifted
        services.iam_policies[service_name("wrk-1")] = safe_service_policy()
        with self.assertRaises(LifecycleEffectsError):
            manager.delete_compute(
                workload_id=WorkloadId("wrk-1"),
                expected_workload_version=1,
                target_kinds=("cloud_run_service",),
                retain_image_until=NOW + timedelta(days=30),
            )

    def test_delete_compute_retires_binding_after_service_deletion(self) -> None:
        public_host = create_active_binding(
            self.store,
            state=AppHostnameBindingState.DISABLED,
        )
        events: list[str] = []

        class RetireAwareServicesClient(FakeServicesClient):
            def delete_service(self, *, name: str, **_: object) -> FakeOperation:
                events.append(
                    "binding:"
                    + self_store.get_app_hostname_binding(public_host).state.value
                )
                result = super().delete_service(name=name)
                events.append(f"delete:{name}")
                return result

        self_store = self.store
        services = RetireAwareServicesClient()
        jobs = FakeJobsClient()
        scheduler = FakeSchedulerClient()
        services.existing[service_name("wrk-1")] = safe_service()
        services.iam_policies[service_name("wrk-1")] = safe_service_policy()
        manager = LifecycleComputeManager(
            store=self.store,
            services_client=services,
            jobs_client=jobs,
            scheduler_client=scheduler,
            project_number=PROJECT_NUMBER,
            reviewed_breakglass_members=ADMIN_MEMBERS,
            clock=lambda: NOW + timedelta(minutes=5),
        )

        manager.delete_compute(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            target_kinds=("cloud_run_service",),
            retain_image_until=NOW + timedelta(days=30),
        )

        self.assertEqual(
            self.store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.RETIRED,
        )
        self.assertEqual(
            events,
            [
                f"binding:{AppHostnameBindingState.DISABLED.value}",
                f"delete:{service_name('wrk-1')}",
            ],
        )

    def test_delete_compute_keeps_binding_when_service_deletion_fails(self) -> None:
        public_host = create_active_binding(self.store)

        class FailedDeleteOperation(FakeOperation):
            def result(self) -> None:
                raise RuntimeError("synthetic delete failure")

        class FailingServicesClient(FakeServicesClient):
            def delete_service(self, *, name: str, **_: object) -> FakeOperation:
                self.delete_requests.append(name)
                return FailedDeleteOperation()

        services = FailingServicesClient()
        services.existing[service_name("wrk-1")] = safe_service()
        services.iam_policies[service_name("wrk-1")] = safe_service_policy()
        manager = LifecycleComputeManager(
            store=self.store,
            services_client=services,
            jobs_client=FakeJobsClient(),
            scheduler_client=FakeSchedulerClient(),
            project_number=PROJECT_NUMBER,
            reviewed_breakglass_members=ADMIN_MEMBERS,
            clock=lambda: NOW + timedelta(minutes=5),
        )

        with self.assertRaises(LifecycleEffectsError):
            manager.delete_compute(
                workload_id=WorkloadId("wrk-1"),
                expected_workload_version=1,
                target_kinds=("cloud_run_service",),
                retain_image_until=NOW + timedelta(days=30),
            )

        self.assertEqual(
            self.store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.ACTIVE,
        )

    def test_delete_compute_treats_missing_web_binding_as_already_retired(
        self,
    ) -> None:
        store = MemoryStore()
        store.create_user(user(state=UserState.OFFBOARDED, version=1))
        store.create_workload(workload(version=1))
        store.create_workload(workload(workload_id="wrk-2", version=1))
        services = FakeServicesClient()
        services.existing[service_name("wrk-1")] = safe_service()
        services.iam_policies[service_name("wrk-1")] = safe_service_policy()
        manager = LifecycleComputeManager(
            store=store,
            services_client=services,
            jobs_client=FakeJobsClient(),
            scheduler_client=FakeSchedulerClient(),
            project_number=PROJECT_NUMBER,
            reviewed_breakglass_members=ADMIN_MEMBERS,
            clock=lambda: NOW + timedelta(minutes=5),
        )

        manager.delete_compute(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            target_kinds=("cloud_run_service",),
            retain_image_until=NOW + timedelta(days=30),
        )

        self.assertEqual(services.delete_requests, [service_name("wrk-1")])
        self.assertTrue(services.delete_operations[0].completed)

    def test_delete_compute_fails_closed_when_web_binding_retire_conflicts(
        self,
    ) -> None:
        seeded = MemoryStore()
        seeded.create_user(user(state=UserState.OFFBOARDED, version=1))
        seeded.create_workload(workload(version=1))
        seeded.create_workload(workload(workload_id="wrk-2", version=1))
        public_host = create_active_binding(seeded)
        store = ConflictOnRetireStore(public_host)
        store.create_user(user(state=UserState.OFFBOARDED, version=1))
        store.create_workload(workload(version=1))
        store.create_workload(workload(workload_id="wrk-2", version=1))
        binding = seeded.get_app_hostname_binding(public_host)
        store.create_app_hostname_binding(binding)
        services = FakeServicesClient()
        services.existing[service_name("wrk-1")] = safe_service()
        services.iam_policies[service_name("wrk-1")] = safe_service_policy()
        manager = LifecycleComputeManager(
            store=store,
            services_client=services,
            jobs_client=FakeJobsClient(),
            scheduler_client=FakeSchedulerClient(),
            project_number=PROJECT_NUMBER,
            reviewed_breakglass_members=ADMIN_MEMBERS,
            clock=lambda: NOW + timedelta(minutes=5),
        )

        with self.assertRaises(LifecycleEffectsError):
            manager.delete_compute(
                workload_id=WorkloadId("wrk-1"),
                expected_workload_version=1,
                target_kinds=("cloud_run_service",),
                retain_image_until=NOW + timedelta(days=30),
            )

        self.assertEqual(services.delete_requests, [service_name("wrk-1")])
        self.assertEqual(
            store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.ACTIVE,
        )

    def test_delete_compute_rejects_admin_invokers_when_breakglass_is_empty(
        self,
    ) -> None:
        create_active_binding(self.store)
        services = FakeServicesClient()
        services.existing[service_name("wrk-1")] = safe_service()
        services.iam_policies[service_name("wrk-1")] = safe_service_policy()
        manager = LifecycleComputeManager(
            store=self.store,
            services_client=services,
            jobs_client=FakeJobsClient(),
            scheduler_client=FakeSchedulerClient(),
            project_number=PROJECT_NUMBER,
            reviewed_breakglass_members=(),
            clock=lambda: NOW + timedelta(minutes=5),
        )

        with self.assertRaises(LifecycleEffectsError):
            manager.delete_compute(
                workload_id=WorkloadId("wrk-1"),
                expected_workload_version=1,
                target_kinds=("cloud_run_service",),
                retain_image_until=NOW + timedelta(days=30),
            )

        self.assertEqual(services.delete_requests, [])

    def test_delete_compute_waits_for_cloud_run_completion(self) -> None:
        public_host = create_active_binding(self.store)
        services = FakeServicesClient()
        jobs = FakeJobsClient()
        scheduler = FakeSchedulerClient()
        services.existing[service_name("wrk-1")] = safe_service()
        services.iam_policies[service_name("wrk-1")] = safe_service_policy()
        manager = LifecycleComputeManager(
            store=self.store,
            services_client=services,
            jobs_client=jobs,
            scheduler_client=scheduler,
            project_number=PROJECT_NUMBER,
            reviewed_breakglass_members=ADMIN_MEMBERS,
        )

        manager.delete_compute(
            workload_id=WorkloadId("wrk-1"),
            expected_workload_version=1,
            target_kinds=("cloud_run_service",),
            retain_image_until=NOW + timedelta(days=30),
        )

        self.assertEqual(services.delete_requests, [service_name("wrk-1")])
        self.assertTrue(services.delete_operations[0].completed)
        self.assertEqual(
            self.store.get_app_hostname_binding(public_host).state,
            AppHostnameBindingState.RETIRED,
        )


if __name__ == "__main__":
    unittest.main()
