"""Fixed production runtime composition for the MIM control-plane image."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, Callable, Mapping, cast

from fastapi import FastAPI
from google.auth import compute_engine
from starlette.applications import Starlette
from starlette.routing import Route

from mim_control_plane.config import (
    APP_HOST_SUFFIX,
    COMPANY_DOMAIN,
    FIRESTORE_DATABASE_ID,
    GITHUB_OWNER,
    IDENTITY_MAX_STALENESS_MINUTES,
    ORIGIN_HMAC_WINDOW_SECONDS,
    REGION,
    ConfigError,
    DirectoryRuntimeSettings,
    Settings,
    _validate_billing_account_id,
    _validate_cloudflare_audience,
    _validate_cloudflare_issuer,
    _validate_directory_admin_subject,
    _validate_directory_required_group_email,
    _validate_directory_service_account_email,
    _validate_operator_email,
    _validate_organization_id,
    _validate_project_id,
)

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_MAX_BOOTSTRAP_BYTES = 64 * 1024
_RUNTIME_ENV_KEYS = frozenset(
    {
        "MIM_RUNTIME_MODE",
        "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION",
        "MIM_ENABLE_MUTATIONS",
    }
)
_BOOTSTRAP_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "project_id",
        "project_number",
        "organization_id",
        "billing_account_id",
        "operator_email",
        "cloudflare_issuer",
        "cloudflare_audience",
        "app_cloudflare_issuer",
        "app_cloudflare_audience",
        "public_host_suffix",
        "region",
        "directory_required_group_email",
        "admin_members",
        "breakglass_members",
        "directory",
        "slack",
        "origin_hmac_keys",
        "app_origin_hmac_keys",
        "desired_state_signing_key_id",
        "desired_state_signing_secret_version",
        "github_webhook_secret_version",
        "github_app",
        "build",
        "deploy_worker",
        "app_gateway",
        "app_authorization",
        "schedule_gateway",
    }
)
_OPTIONAL_BOOTSTRAP_TOP_LEVEL_KEYS = frozenset({"breakglass_members", "slack"})
_ORIGIN_KEY_FIELDS = frozenset({"key_id", "secret_version"})
_SLACK_FIELDS = frozenset({"required_scopes"})
_GITHUB_APP_FIELDS = frozenset(
    {
        "app_id",
        "private_key_secret_version",
        "installation_id",
        "allowed_repository_ids",
        "bindings",
    }
)
_GITHUB_BINDING_FIELDS = frozenset(
    {
        "repository_numeric_id",
        "owner",
        "name",
        "installation_id",
        "repository_resource",
    }
)
_BUILD_FIELDS = frozenset({"builder_image", "build_service_account"})
_MACHINE_SERVICE_FIELDS = frozenset(
    {"url", "audience", "service_account_email"}
)
_DIRECTORY_FIELDS = frozenset({"admin_subject", "service_account_email"})
_HEX_64_CHARS = frozenset("0123456789abcdef")
_MISSING = object()


class RuntimeMode(StrEnum):
    CONTROL_PLANE = "control-plane"
    DEPLOY_WORKER = "deploy-worker"
    SCHEDULE_GATEWAY = "schedule-gateway"
    IDENTITY_SYNC = "identity-sync"
    LIFECYCLE = "lifecycle"
    USAGE_INGEST = "usage-ingest"


@dataclass(frozen=True, slots=True)
class NamedSecretVersion:
    key_id: str
    secret_version: str


@dataclass(frozen=True, slots=True)
class RepositoryBindingBootstrap:
    repository_numeric_id: int
    owner: str
    name: str
    installation_id: int
    repository_resource: str


@dataclass(frozen=True, slots=True)
class GitHubAppBootstrap:
    app_id: str
    private_key_secret_version: str
    installation_id: int
    allowed_repository_ids: frozenset[int]
    bindings: tuple[RepositoryBindingBootstrap, ...]


@dataclass(frozen=True, slots=True)
class BuildBootstrap:
    builder_image: str
    build_service_account: str


@dataclass(frozen=True, slots=True)
class MachineServiceBootstrap:
    url: str
    audience: str
    service_account_email: str


@dataclass(frozen=True, slots=True)
class DirectoryBootstrap:
    admin_subject: str
    service_account_email: str


@dataclass(frozen=True, slots=True)
class RuntimeBootstrap:
    project_id: str
    project_number: str
    organization_id: str
    billing_account_id: str
    operator_email: str
    cloudflare_issuer: str
    cloudflare_audience: str
    app_cloudflare_issuer: str
    app_cloudflare_audience: str
    public_host_suffix: str
    directory_required_group_email: str
    admin_members: tuple[str, ...]
    breakglass_members: tuple[str, ...]
    directory: DirectoryBootstrap
    slack_required_scopes: tuple[str, ...]
    origin_hmac_keys: tuple[NamedSecretVersion, ...]
    app_origin_hmac_keys: tuple[NamedSecretVersion, ...]
    desired_state_signing_key_id: str
    desired_state_signing_secret_version: str
    github_webhook_secret_version: str
    github_app: GitHubAppBootstrap
    build: BuildBootstrap
    deploy_worker: MachineServiceBootstrap
    app_gateway: MachineServiceBootstrap
    app_authorization: MachineServiceBootstrap
    schedule_gateway: MachineServiceBootstrap
    firestore_database_id: str = FIRESTORE_DATABASE_ID
    region: str = REGION
    github_owner: str = GITHUB_OWNER
    company_domain: str = COMPANY_DOMAIN

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_required_scopes)

    @property
    def directory_required_group_label(self) -> str:
        return self.directory_required_group_email.partition("@")[0].casefold()

    @property
    def directory_runtime_settings(self) -> DirectoryRuntimeSettings:
        return DirectoryRuntimeSettings(
            operator_email=self.operator_email,
            directory_admin_subject=self.directory.admin_subject,
            directory_service_account_email=self.directory.service_account_email,
            directory_required_group_email=self.directory_required_group_email,
        )

    @property
    def public_settings(self) -> Settings:
        return Settings(
            project_id=self.project_id,
            organization_id=self.organization_id,
            billing_account_id=self.billing_account_id,
            operator_email=self.operator_email,
            cloudflare_issuer=self.cloudflare_issuer,
            cloudflare_audience=self.cloudflare_audience,
        )


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    mode: RuntimeMode
    bootstrap_secret_version: str
    mutations_enabled: bool
    bootstrap: RuntimeBootstrap


@dataclass(frozen=True, slots=True)
class PublicRuntimeParts:
    api_app: FastAPI
    mcp_app: Starlette


BootstrapSecretLoader = Callable[..., bytes]
AppBuilder = Callable[[RuntimeEnvironment, "ProductionDependencies"], FastAPI]
PublicBuilder = Callable[
    [RuntimeEnvironment, "ProductionDependencies"],
    PublicRuntimeParts,
]


def _default_metadata_credentials_loader() -> object:
    return compute_engine.Credentials()


def _default_bootstrap_secret_loader(
    *,
    secret_version: str,
    credentials: object,
) -> bytes:
    from google.cloud import secretmanager_v1

    client = secretmanager_v1.SecretManagerServiceClient(
        credentials=cast(Any, credentials)
    )
    response = client.access_secret_version(request={"name": secret_version})
    payload = getattr(response, "payload", None)
    data = getattr(payload, "data", None)
    if type(data) is not bytes:
        raise ConfigError("Runtime bootstrap secret could not be read.")
    return data


@dataclass(frozen=True, slots=True)
class ProductionDependencies:
    metadata_credentials_loader: Callable[
        [],
        object,
    ] = _default_metadata_credentials_loader
    bootstrap_secret_loader: BootstrapSecretLoader = _default_bootstrap_secret_loader
    build_public_runtime_parts: PublicBuilder | None = None
    build_deploy_worker_runtime_app: AppBuilder | None = None
    build_schedule_gateway_runtime_app: AppBuilder | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class _ScheduleControlAdapter:
    inner: Any

    def ensure_enabled(self, schedule: object) -> None:
        self.inner.ensure_enabled(schedule)

    def pause(self, schedule: object) -> None:
        self.inner.pause(schedule)

    def resume(self, schedule: object) -> None:
        self.inner.resume(schedule)


class _FirestoreScheduleDispatchLedger:
    def __init__(self, *, client: Any) -> None:
        project = getattr(client, "project", None)
        database = getattr(client, "database", None)
        if project != _CENTRAL_PROJECT_ID:
            raise ValueError(
                "schedule dispatch ledger client must use the central project."
            )
        if database != FIRESTORE_DATABASE_ID:
            raise ValueError(
                "schedule dispatch ledger client must use the default database."
            )
        self._collection = client.collection("schedule_dispatch_ledger")

    def get(self, *, schedule_id: str, tick_at: datetime) -> object | None:
        snapshot = self._document(schedule_id=schedule_id, tick_at=tick_at).get()
        if getattr(snapshot, "exists", None) is not True:
            return None
        payload = snapshot.to_dict()
        return _require_ledger_record(payload)

    def claim(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> object:
        payload = self._record(
            state="claimed",
            stable_token=stable_token,
            run_reference=None,
        )
        reference = self._document(schedule_id=schedule_id, tick_at=tick_at)
        try:
            reference.create(payload)
            return dict(payload)
        except Exception:
            existing = self.get(schedule_id=schedule_id, tick_at=tick_at)
            if existing is None:
                raise
            return existing

    def mark_succeeded(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
        run_reference: str,
    ) -> object:
        payload = self._record(
            state="succeeded",
            stable_token=stable_token,
            run_reference=run_reference,
        )
        self._document(schedule_id=schedule_id, tick_at=tick_at).set(payload)
        return _require_ledger_record(payload)

    def mark_ambiguous(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> object:
        payload = self._record(
            state="ambiguous",
            stable_token=stable_token,
            run_reference=None,
        )
        self._document(schedule_id=schedule_id, tick_at=tick_at).set(payload)
        return _require_ledger_record(payload)

    def _document(self, *, schedule_id: str, tick_at: datetime) -> Any:
        digest = sha256()
        digest.update(schedule_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(tick_at.isoformat().encode("ascii"))
        return self._collection.document(digest.hexdigest())

    def _record(
        self,
        *,
        state: str,
        stable_token: str,
        run_reference: str | None,
    ) -> dict[str, object]:
        return {
            "state": state,
            "stable_token": stable_token,
            "run_reference": run_reference,
        }


def build_runtime_app_from_environment(
    mapping: Mapping[str, str] | None = None,
    *,
    dependencies: ProductionDependencies | None = None,
) -> FastAPI:
    runtime_dependencies = dependencies or ProductionDependencies()
    environment = load_runtime_environment(
        os.environ if mapping is None else mapping,
        dependencies=runtime_dependencies,
    )
    return build_runtime_app(environment, dependencies=runtime_dependencies)


def load_runtime_environment(
    mapping: Mapping[str, str],
    *,
    dependencies: ProductionDependencies,
) -> RuntimeEnvironment:
    _reject_unknown_runtime_keys(mapping)
    mode = _require_runtime_mode(mapping.get("MIM_RUNTIME_MODE"))
    bootstrap_secret_version = _require_secret_version_ref(
        mapping.get("MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION"),
        field_name="MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION",
        project_id=_CENTRAL_PROJECT_ID,
    )
    mutations_enabled = _require_exact_bool_string(mapping.get("MIM_ENABLE_MUTATIONS"))
    credentials = dependencies.metadata_credentials_loader()
    payload = dependencies.bootstrap_secret_loader(
        secret_version=bootstrap_secret_version,
        credentials=credentials,
    )
    bootstrap = _parse_runtime_bootstrap(payload)
    return RuntimeEnvironment(
        mode=mode,
        bootstrap_secret_version=bootstrap_secret_version,
        mutations_enabled=mutations_enabled,
        bootstrap=bootstrap,
    )


def build_runtime_app(
    runtime_env: RuntimeEnvironment,
    *,
    dependencies: ProductionDependencies,
) -> FastAPI:
    if runtime_env.mode is RuntimeMode.CONTROL_PLANE:
        public_builder = (
            dependencies.build_public_runtime_parts or _build_public_runtime_parts
        )
        return _assemble_public_runtime(public_builder(runtime_env, dependencies))
    if runtime_env.mode is RuntimeMode.DEPLOY_WORKER:
        app_builder = (
            dependencies.build_deploy_worker_runtime_app
            or _build_deploy_worker_runtime_app
        )
        return app_builder(runtime_env, dependencies)
    if runtime_env.mode is RuntimeMode.SCHEDULE_GATEWAY:
        app_builder = (
            dependencies.build_schedule_gateway_runtime_app
            or _build_schedule_gateway_runtime_app
        )
        return app_builder(runtime_env, dependencies)
    raise ConfigError("MIM_RUNTIME_MODE is invalid.")


def _build_public_runtime_parts(
    runtime_env: RuntimeEnvironment,
    dependencies: ProductionDependencies,
) -> PublicRuntimeParts:
    from google.cloud import firestore_v1

    from mim_control_plane.adapters.firestore_store import FirestoreStore
    from mim_control_plane.api import build_api_app
    from mim_control_plane.dashboard import ControlPlaneReadService
    from mim_control_plane.mcp import build_mcp_server
    from mim_control_plane.mcp_http import build_mcp_http_app
    from mim_control_plane.secret_api import build_secret_router

    bootstrap = runtime_env.bootstrap
    credentials = dependencies.metadata_credentials_loader()
    store = FirestoreStore(
        settings=bootstrap.public_settings,
        credentials_loader=lambda: credentials,
    )
    slack_repository: object | None = None
    if bootstrap.slack_enabled:
        from mim_control_plane.adapters.firestore_slack_oauth import (
            FirestoreSlackOAuthRepository,
        )

        slack_client = firestore_v1.Client(
            project=bootstrap.project_id,
            database=bootstrap.firestore_database_id,
            credentials=credentials,
        )
        slack_repository = FirestoreSlackOAuthRepository(client=cast(Any, slack_client))
    gateway = _build_central_identity_gateway(
        store=store,
        bootstrap=bootstrap,
        credentials=credentials,
        clock=dependencies.clock,
        slack_repository=slack_repository,
    )
    deployment_service = _build_deployment_service(
        store=store,
        bootstrap=bootstrap,
        credentials=credentials,
        clock=dependencies.clock,
    )
    schedule_management = _build_schedule_management_service(
        store=store,
        bootstrap=bootstrap,
        credentials=credentials,
        clock=dependencies.clock,
    )
    secret_management = _build_secret_management_service(
        store=store,
        bootstrap=bootstrap,
        credentials=credentials,
        clock=dependencies.clock,
    )
    origin_verifier = _build_origin_verifier(
        store=store,
        bootstrap=bootstrap,
        credentials=credentials,
        clock=dependencies.clock,
    )
    gateway_ref = cast(Any, gateway)
    deployment_ref = cast(Any, deployment_service)
    origin_ref = cast(Any, origin_verifier)
    schedule_ref = cast(Any, schedule_management)
    secret_ref = cast(Any, secret_management)

    def readiness_check() -> None:
        store.list_users()

    api_app = build_api_app(
        store=store,
        gateway=gateway_ref,
        clock=dependencies.clock,
        deployment_service=deployment_ref,
        github_origin_verifier=origin_ref,
        mutations_enabled=runtime_env.mutations_enabled,
        schedule_management=schedule_ref,
        readiness_check=readiness_check,
    )
    api_app.include_router(
        build_secret_router(
            gateway=gateway_ref,
            secret_management=secret_ref,
            mutations_enabled=runtime_env.mutations_enabled,
        )
    )
    service = ControlPlaneReadService(
        store=store,
        clock=dependencies.clock,
        deployment_planner=deployment_ref,
    )
    mcp_server = build_mcp_server(
        service=service,
        gateway=gateway_ref,
        deployment_service=deployment_ref,
        schedule_management=schedule_ref,
        secret_management=secret_ref,
        mutations_enabled=runtime_env.mutations_enabled,
    )
    return PublicRuntimeParts(
        api_app=api_app,
        mcp_app=build_mcp_http_app(fastmcp=mcp_server, gateway=gateway_ref),
    )


def _build_deploy_worker_runtime_app(
    runtime_env: RuntimeEnvironment,
    dependencies: ProductionDependencies,
) -> FastAPI:
    from mim_control_plane.adapters.firestore_store import FirestoreStore
    from mim_control_plane.machine_api import build_deploy_worker_app
    from mim_control_plane.security.google_machine_identity import (
        GoogleOidcMachineAuthenticator,
    )

    bootstrap = runtime_env.bootstrap
    credentials = dependencies.metadata_credentials_loader()
    store = FirestoreStore(
        settings=bootstrap.public_settings,
        credentials_loader=lambda: credentials,
    )
    worker = _build_private_deploy_worker(
        bootstrap=bootstrap,
        store=store,
        credentials=credentials,
    )
    queue = _build_cloud_tasks_queue(
        bootstrap=bootstrap,
        store=store,
        credentials=credentials,
    )
    authenticator = GoogleOidcMachineAuthenticator(
        audience=bootstrap.deploy_worker.audience,
        service_account_email=bootstrap.deploy_worker.service_account_email,
    )
    def readiness_check() -> None:
        store.list_users()

    return build_deploy_worker_app(
        authenticator=authenticator,
        expected_service_account_email=bootstrap.deploy_worker.service_account_email,
        queue=cast(Any, queue),
        worker=cast(Any, worker),
        clock=dependencies.clock,
        readiness_check=readiness_check,
    )


def _build_schedule_gateway_runtime_app(
    runtime_env: RuntimeEnvironment,
    dependencies: ProductionDependencies,
) -> FastAPI:
    from mim_control_plane.adapters.firestore_store import FirestoreStore
    from mim_control_plane.machine_api import build_schedule_gateway_app
    from mim_control_plane.security.authorization import IdentityPolicy
    from mim_control_plane.security.google_machine_identity import (
        GoogleOidcMachineAuthenticator,
    )
    from mim_control_plane.services.app_gateway_authorization import (
        AppGatewayAuthorizationService,
    )

    bootstrap = runtime_env.bootstrap
    credentials = dependencies.metadata_credentials_loader()
    store = FirestoreStore(
        settings=bootstrap.public_settings,
        credentials_loader=lambda: credentials,
    )
    schedule_management = _build_schedule_management_service(
        store=store,
        bootstrap=bootstrap,
        credentials=credentials,
        clock=dependencies.clock,
    )
    authenticator = GoogleOidcMachineAuthenticator(
        audience=bootstrap.schedule_gateway.audience,
        service_account_email=bootstrap.schedule_gateway.service_account_email,
    )
    app_authorization = AppGatewayAuthorizationService(
        store=store,
        identity_policy=IdentityPolicy(
            store=store,
            issuer=bootstrap.app_cloudflare_issuer,
            audience=bootstrap.app_cloudflare_audience,
            company_domain=bootstrap.company_domain,
            required_group=bootstrap.directory_required_group_label,
            max_staleness=timedelta(minutes=IDENTITY_MAX_STALENESS_MINUTES),
            clock=dependencies.clock,
        ),
        clock=dependencies.clock,
    )

    def readiness_check() -> None:
        store.list_users()

    return build_schedule_gateway_app(
        authenticator=authenticator,
        expected_service_account_email=bootstrap.schedule_gateway.service_account_email,
        schedule_management=cast(Any, schedule_management),
        scheduler_project_id=bootstrap.project_id,
        scheduler_region=bootstrap.region,
        readiness_check=readiness_check,
        expected_app_service_account_email=(
            bootstrap.app_gateway.service_account_email
        ),
        app_authorization=app_authorization,
    )


def _build_private_deploy_worker(
    *,
    bootstrap: RuntimeBootstrap,
    store: object,
    credentials: object,
) -> object:
    from google.cloud import firestore_v1, run_v2, secretmanager_v1
    from google.cloud.devtools import cloudbuild_v1

    from mim_control_plane.adapters.artifact_registry import ArtifactRegistryAdapter
    from mim_control_plane.adapters.cloud_build import (
        CloudBuildAdapter,
        ConnectedRepositoryBinding,
    )
    from mim_control_plane.adapters.cloud_run import CloudRunRuntimePort
    from mim_control_plane.adapters.firestore_desired_state import (
        FirestoreDesiredStateArtifactPort,
    )
    from mim_control_plane.adapters.google_rest import (
        ArtifactRegistryRestClient,
        IamAdminRestClient,
        ResourceManagerRestClient,
        build_authorized_session,
    )
    from mim_control_plane.adapters.runtime_identity import RuntimeIdentityAdapter
    from mim_control_plane.adapters.secret_manager import SecretManagerAdapter
    from mim_control_plane.services.render import DesiredStateRenderContext
    from mim_control_plane.workers.deploy import PrivateDeployWorker

    artifacts_client = firestore_v1.Client(
        project=bootstrap.project_id,
        database=bootstrap.firestore_database_id,
        credentials=credentials,
    )
    authorized_session = build_authorized_session(credentials=credentials)
    return PrivateDeployWorker(
        store=cast(Any, store),
        queue=cast(
            Any,
            _build_cloud_tasks_queue(
                bootstrap=bootstrap,
                store=store,
                credentials=credentials,
            ),
        ),
        source=cast(
            Any,
            _build_github_source_port(
                bootstrap=bootstrap,
                credentials=credentials,
            ),
        ),
        build=CloudBuildAdapter(
            project_id=bootstrap.project_id,
            region=bootstrap.region,
            build_service_account=bootstrap.build.build_service_account,
            builder_image=bootstrap.build.builder_image,
            bindings=tuple(
                ConnectedRepositoryBinding(
                    repository_numeric_id=item.repository_numeric_id,
                    owner=item.owner,
                    name=item.name,
                    installation_id=item.installation_id,
                    repository_resource=item.repository_resource,
                )
                for item in bootstrap.github_app.bindings
            ),
            client=cloudbuild_v1.CloudBuildClient(credentials=cast(Any, credentials)),
        ),
        registry=ArtifactRegistryAdapter(
            client=ArtifactRegistryRestClient(session=authorized_session),
            project_id=bootstrap.project_id,
            region=bootstrap.region,
        ),
        artifacts=FirestoreDesiredStateArtifactPort(
            client=cast(Any, artifacts_client),
            project_id=bootstrap.project_id,
            region=bootstrap.region,
        ),
        runtime_identity=RuntimeIdentityAdapter(
            project_id=bootstrap.project_id,
            iam_admin_client=IamAdminRestClient(session=authorized_session),
            resource_manager_client=ResourceManagerRestClient(
                session=authorized_session
            ),
        ),
        runtime=CloudRunRuntimePort(
            project_id=bootstrap.project_id,
            project_number=bootstrap.project_number,
            region=bootstrap.region,
            services_client=run_v2.ServicesClient(credentials=cast(Any, credentials)),
            jobs_client=run_v2.JobsClient(credentials=cast(Any, credentials)),
            revisions_client=run_v2.RevisionsClient(credentials=cast(Any, credentials)),
            reviewed_breakglass_members=bootstrap.breakglass_members,
        ),
        secrets=SecretManagerAdapter(
            client=secretmanager_v1.SecretManagerServiceClient(
                credentials=cast(Any, credentials)
            ),
            store=cast(Any, store),
            project_id=bootstrap.project_id,
            version_manager_service_account=(
                f"mim-control-plane@{bootstrap.project_id}.iam.gserviceaccount.com"
            ),
        ),
        render_context=DesiredStateRenderContext(
            project_id=bootstrap.project_id,
            key_id=bootstrap.desired_state_signing_key_id,
        ),
        signing_key=_load_secret_bytes(
            secret_version=bootstrap.desired_state_signing_secret_version,
            credentials=credentials,
        ),
    )


def _build_cloud_tasks_queue(
    *,
    bootstrap: RuntimeBootstrap,
    store: object,
    credentials: object,
) -> object:
    from google.cloud import tasks_v2

    from mim_control_plane.adapters.cloud_tasks import (
        CloudTasksDeploymentQueue,
        CloudTasksSettings,
    )

    return CloudTasksDeploymentQueue(
        settings=CloudTasksSettings(
            project_id=bootstrap.project_id,
            location=bootstrap.region,
            queue_id="mim-private-workers",
            worker_url=bootstrap.deploy_worker.url,
            worker_audience=bootstrap.deploy_worker.audience,
            oidc_service_account_email=bootstrap.deploy_worker.service_account_email,
        ),
        material_store=cast(Any, store),
        client=tasks_v2.CloudTasksClient(credentials=cast(Any, credentials)),
    )


def _build_deployment_service(
    *,
    store: object,
    bootstrap: RuntimeBootstrap,
    credentials: object,
    clock: Callable[[], datetime],
) -> object:
    from mim_control_plane.services.deployments import DeploymentService
    from mim_control_plane.services.render import DesiredStateRenderContext
    source = cast(
        Any,
        _build_github_source_port(bootstrap=bootstrap, credentials=credentials),
    )
    return DeploymentService(
        store=cast(Any, store),
        source=source,
        enqueuer=cast(
            Any,
            _build_cloud_tasks_queue(
                bootstrap=bootstrap,
                store=store,
                credentials=credentials,
            ),
        ),
        render_context=DesiredStateRenderContext(
            project_id=bootstrap.project_id,
            key_id=bootstrap.desired_state_signing_key_id,
        ),
        signing_key=_load_secret_bytes(
            secret_version=bootstrap.desired_state_signing_secret_version,
            credentials=credentials,
        ),
        clock=clock,
        github_policy=source.policy,
        github_webhook_secret=_load_secret_bytes(
            secret_version=bootstrap.github_webhook_secret_version,
            credentials=credentials,
        ),
    )


def _build_github_source_port(
    *,
    bootstrap: RuntimeBootstrap,
    credentials: object,
) -> object:
    from mim_control_plane.adapters.github import (
        GitHubAppInstallationTokenProvider,
        GitHubSourceAdapter,
    )
    from mim_control_plane.adapters.github_jwt import GitHubAppPrivateKeyJwtProvider
    from mim_control_plane.services.repository_admission import SelectedRepositoryPolicy

    policy = SelectedRepositoryPolicy(
        allowed_repository_ids=bootstrap.github_app.allowed_repository_ids,
        installation_id=bootstrap.github_app.installation_id,
    )
    token_provider = GitHubAppInstallationTokenProvider(
        policy=policy,
        app_jwt_provider=GitHubAppPrivateKeyJwtProvider(
            app_id=bootstrap.github_app.app_id,
            private_key_pem=_load_secret_text(
                secret_version=bootstrap.github_app.private_key_secret_version,
                credentials=credentials,
            ),
        ),
    )
    return GitHubSourceAdapter(policy=policy, token_provider=token_provider)


def _build_schedule_management_service(
    *,
    store: object,
    bootstrap: RuntimeBootstrap,
    credentials: object,
    clock: Callable[[], datetime],
) -> object:
    from google.cloud import firestore_v1, run_v2, scheduler_v1

    from mim_control_plane.adapters.cloud_run_job_dispatch import (
        CloudRunJobDispatcher,
    )
    from mim_control_plane.adapters.cloud_scheduler import CloudSchedulerAdapter
    from mim_control_plane.services.schedule_management import (
        ScheduleManagementService,
    )

    scheduler = CloudSchedulerAdapter(
        client=scheduler_v1.CloudSchedulerClient(credentials=cast(Any, credentials)),
        project_id=bootstrap.project_id,
        project_number=bootstrap.project_number,
        region=bootstrap.region,
        scheduler_service_account=bootstrap.schedule_gateway.service_account_email,
    )
    ledger = _FirestoreScheduleDispatchLedger(
        client=firestore_v1.Client(
            project=bootstrap.project_id,
            database=bootstrap.firestore_database_id,
            credentials=credentials,
        )
    )
    dispatcher = CloudRunJobDispatcher(
        jobs_client=cast(Any, run_v2.JobsClient(credentials=cast(Any, credentials))),
        executions_client=cast(
            Any,
            run_v2.ExecutionsClient(credentials=cast(Any, credentials)),
        ),
        ledger=ledger,
        project_id=bootstrap.project_id,
        region=bootstrap.region,
    )
    return ScheduleManagementService(
        store=cast(Any, store),
        scheduler=_ScheduleControlAdapter(scheduler),
        dispatcher=dispatcher,
        clock=clock,
        id_factory=_runtime_id,
        lease_token_factory=_runtime_lease_token,
    )


def _build_secret_management_service(
    *,
    store: object,
    bootstrap: RuntimeBootstrap,
    credentials: object,
    clock: Callable[[], datetime],
) -> object:
    from google.cloud import secretmanager_v1

    from mim_control_plane.adapters.secret_manager import SecretManagerAdapter
    from mim_control_plane.services.secret_management import SecretManagementService

    return SecretManagementService(
        store=cast(Any, store),
        secret_port=SecretManagerAdapter(
            client=secretmanager_v1.SecretManagerServiceClient(
                credentials=cast(Any, credentials)
            ),
            store=cast(Any, store),
            project_id=bootstrap.project_id,
            version_manager_service_account=(
                f"mim-control-plane@{bootstrap.project_id}.iam.gserviceaccount.com"
            ),
        ),
        clock=clock,
        id_factory=_runtime_id,
    )


def _build_central_identity_gateway(
    *,
    store: object,
    bootstrap: RuntimeBootstrap,
    credentials: object,
    clock: Callable[[], datetime],
    slack_repository: object | None,
) -> object:
    from mim_control_plane.adapters.action_policy import ClosedActionPolicyAuthorizer
    from mim_control_plane.security.authorization import IdentityPolicy
    from mim_control_plane.security.identity import (
        CloudflareJwtVerifier,
        IdentityAuthenticator,
    )
    from mim_control_plane.services.central_identity import CentralIdentityGateway

    origin_verifier = _build_origin_verifier(
        store=store,
        bootstrap=bootstrap,
        credentials=credentials,
        clock=clock,
    )
    identity_policy = IdentityPolicy(
        store=cast(Any, store),
        issuer=bootstrap.cloudflare_issuer,
        audience=bootstrap.cloudflare_audience,
        company_domain=bootstrap.company_domain,
        required_group=bootstrap.directory_required_group_label,
        max_staleness=timedelta(minutes=IDENTITY_MAX_STALENESS_MINUTES),
        clock=clock,
    )
    shared_install_directory = None
    identity_link_directory = None
    if bootstrap.slack_enabled:
        from mim_control_plane.adapters.slack_identity import (
            FirestoreSlackIdentityDirectory,
        )

        if slack_repository is None:
            raise ConfigError("Slack repository is required when Slack is enabled.")
        shared_install_directory = FirestoreSlackIdentityDirectory(
            repository=cast(Any, slack_repository)
        )
        identity_link_directory = FirestoreSlackIdentityDirectory(
            repository=cast(Any, slack_repository)
        )
    return CentralIdentityGateway(
        browser_authenticator=IdentityAuthenticator(
            origin_verifier=cast(Any, origin_verifier),
            jwt_verifier=CloudflareJwtVerifier(
                issuer=bootstrap.cloudflare_issuer,
                audience=bootstrap.cloudflare_audience,
            ),
            identity_policy=identity_policy,
        ),
        identity_policy=identity_policy,
        shared_install_directory=shared_install_directory,
        identity_link_directory=identity_link_directory,
        action_authorizer=ClosedActionPolicyAuthorizer(store=cast(Any, store)),
        required_slack_scopes=frozenset(bootstrap.slack_required_scopes),
        clock=clock,
    )


def _build_origin_verifier(
    *,
    store: object,
    bootstrap: RuntimeBootstrap,
    credentials: object,
    clock: Callable[[], datetime],
) -> object:
    from mim_control_plane.security.origin import OriginHmacVerifier

    keys = {
        item.key_id: _load_secret_bytes(
            secret_version=item.secret_version,
            credentials=credentials,
        )
        for item in bootstrap.origin_hmac_keys
    }
    return OriginHmacVerifier(
        keys=keys,
        store=cast(Any, store),
        clock=clock,
        window=timedelta(seconds=ORIGIN_HMAC_WINDOW_SECONDS),
    )


def _assemble_public_runtime(parts: PublicRuntimeParts) -> FastAPI:
    app = parts.api_app
    mcp_route = _require_single_route(parts.mcp_app, path="/mcp")
    app.router.routes.append(mcp_route)
    api_lifespan = app.router.lifespan_context
    mcp_lifespan = parts.mcp_app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(_: FastAPI):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(api_lifespan(app))
            await stack.enter_async_context(mcp_lifespan(parts.mcp_app))
            yield

    app.router.lifespan_context = combined_lifespan
    return app


def _require_single_route(app: Starlette, *, path: str) -> Route:
    routes = [
        route
        for route in app.routes
        if isinstance(route, Route) and route.path == path
    ]
    if len(routes) != 1:
        raise ConfigError("Public MCP route must expose exactly one /mcp endpoint.")
    return routes[0]


def _parse_runtime_bootstrap(payload: bytes) -> RuntimeBootstrap:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_BOOTSTRAP_BYTES:
        raise ConfigError("Runtime bootstrap payload is invalid.")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("Runtime bootstrap payload is invalid.") from None
    if not isinstance(decoded, Mapping):
        raise ConfigError("Runtime bootstrap payload is invalid.")
    _require_exact_mapping_keys(
        decoded,
        expected=_BOOTSTRAP_TOP_LEVEL_KEYS,
        optional=_OPTIONAL_BOOTSTRAP_TOP_LEVEL_KEYS,
        field_name="Runtime bootstrap",
    )
    schema_version = decoded.get("schema_version")
    if schema_version != 1:
        raise ConfigError("Runtime bootstrap schema_version is invalid.")
    project_id = _validate_fixed_project_id(decoded.get("project_id"))
    project_number = _require_project_number(decoded.get("project_number"))
    public_host_suffix = _require_text(
        decoded.get("public_host_suffix"),
        "public_host_suffix",
    ).casefold()
    if public_host_suffix != APP_HOST_SUFFIX:
        raise ConfigError("public_host_suffix is invalid.")
    if _require_text(decoded.get("region"), "region") != REGION:
        raise ConfigError("region is invalid.")
    return RuntimeBootstrap(
        project_id=project_id,
        project_number=project_number,
        organization_id=_validate_organization_id(
            _require_text(decoded.get("organization_id"), "organization_id")
        ),
        billing_account_id=_validate_billing_account_id(
            _require_text(decoded.get("billing_account_id"), "billing_account_id")
        ),
        operator_email=_validate_operator_email(
            _require_text(decoded.get("operator_email"), "operator_email")
        ),
        cloudflare_issuer=_validate_cloudflare_issuer(
            _require_text(decoded.get("cloudflare_issuer"), "cloudflare_issuer")
        ),
        cloudflare_audience=_validate_cloudflare_audience(
            _require_text(decoded.get("cloudflare_audience"), "cloudflare_audience")
        ),
        app_cloudflare_issuer=_validate_cloudflare_issuer(
            _require_text(
                decoded.get("app_cloudflare_issuer"),
                "app_cloudflare_issuer",
            )
        ),
        app_cloudflare_audience=_validate_cloudflare_audience(
            _require_text(
                decoded.get("app_cloudflare_audience"),
                "app_cloudflare_audience",
            )
        ),
        public_host_suffix=public_host_suffix,
        directory_required_group_email=_validate_directory_required_group_email(
            _require_text(
                decoded.get("directory_required_group_email"),
                "directory_required_group_email",
            )
        ),
        admin_members=_require_admin_members(
            decoded.get("admin_members"),
            operator_email=_validate_operator_email(
                _require_text(decoded.get("operator_email"), "operator_email")
            ),
        ),
        breakglass_members=_require_breakglass_members(
            decoded.get("breakglass_members")
        ),
        directory=_require_directory(decoded.get("directory")),
        slack_required_scopes=_require_slack_scopes(
            decoded["slack"] if "slack" in decoded else _MISSING
        ),
        origin_hmac_keys=_require_origin_keys(
            decoded.get("origin_hmac_keys"),
            field_name="origin_hmac_keys",
        ),
        app_origin_hmac_keys=_require_origin_keys(
            decoded.get("app_origin_hmac_keys"),
            field_name="app_origin_hmac_keys",
            max_items=2,
        ),
        desired_state_signing_key_id=_require_identifier(
            decoded.get("desired_state_signing_key_id"),
            "desired_state_signing_key_id",
        ),
        desired_state_signing_secret_version=_require_secret_version_ref(
            decoded.get("desired_state_signing_secret_version"),
            field_name="desired_state_signing_secret_version",
            project_id=project_id,
        ),
        github_webhook_secret_version=_require_secret_version_ref(
            decoded.get("github_webhook_secret_version"),
            field_name="github_webhook_secret_version",
            project_id=project_id,
        ),
        github_app=_require_github_app(
            decoded.get("github_app"),
            project_id=project_id,
        ),
        build=_require_build(decoded.get("build")),
        deploy_worker=_require_machine_service(
            decoded.get("deploy_worker"),
            field_name="deploy_worker",
            expected_service_account=(
                f"mim-deploy-worker@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-deploy-worker-{project_number}."
                f"{REGION}.run.app"
            ),
            expected_path="/internal/deploy",
        ),
        app_gateway=_require_machine_service(
            decoded.get("app_gateway"),
            field_name="app_gateway",
            expected_service_account=(
                f"mim-app-gateway@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-app-gateway-{project_number}.{REGION}.run.app"
            ),
            expected_path="",
        ),
        app_authorization=_require_machine_service(
            decoded.get("app_authorization"),
            field_name="app_authorization",
            expected_service_account=(
                f"mim-schedule-gateway@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-schedule-gateway-{project_number}.{REGION}.run.app"
            ),
            expected_path="/v1/apps/authorize",
        ),
        schedule_gateway=_require_machine_service(
            decoded.get("schedule_gateway"),
            field_name="schedule_gateway",
            expected_service_account=(
                f"mim-schedule-gateway@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-schedule-gateway-{project_number}."
                f"{REGION}.run.app"
            ),
            expected_path="/v1/schedules/execute",
        ),
    )


def _require_origin_keys(
    value: object,
    *,
    field_name: str,
    max_items: int | None = None,
) -> tuple[NamedSecretVersion, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{field_name} must be a non-empty list.")
    if max_items is not None and len(value) > max_items:
        raise ConfigError(
            f"{field_name} must not contain more than {max_items} entries."
        )
    keys: list[NamedSecretVersion] = []
    seen_ids: set[str] = set()
    for item in value:
        mapping = _require_mapping(item, field_name)
        _require_exact_mapping_keys(
            mapping,
            expected=_ORIGIN_KEY_FIELDS,
            field_name=field_name,
        )
        key_id = _require_identifier(
            mapping.get("key_id"),
            f"{field_name}.key_id",
        )
        if key_id in seen_ids:
            raise ConfigError(f"{field_name}.key_id must be unique.")
        seen_ids.add(key_id)
        keys.append(
            NamedSecretVersion(
                key_id=key_id,
                secret_version=_require_secret_version_ref(
                    mapping.get("secret_version"),
                    field_name=f"{field_name}.secret_version",
                    project_id=_CENTRAL_PROJECT_ID,
                ),
            )
        )
    return tuple(keys)


def _require_admin_members(
    value: object,
    *,
    operator_email: str,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("admin_members must be a non-empty list.")
    normalized = tuple(
        _require_company_member(item, field_name="admin_members") for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ConfigError("admin_members must be unique.")
    if normalized != tuple(sorted(normalized)):
        raise ConfigError("admin_members must be sorted.")
    if f"user:{operator_email}" not in normalized:
        raise ConfigError("admin_members must include the operator.")
    return normalized


def _require_breakglass_members(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            "breakglass_members must contain only @madup.com users/groups."
        )
    normalized = tuple(
        _require_company_member(item, field_name="breakglass_members")
        for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ConfigError("breakglass_members must be unique.")
    if normalized != tuple(sorted(normalized)):
        raise ConfigError("breakglass_members must be sorted.")
    return normalized


def _require_directory(value: object) -> DirectoryBootstrap:
    mapping = _require_mapping(value, "directory")
    _require_exact_mapping_keys(
        mapping,
        expected=_DIRECTORY_FIELDS,
        field_name="directory",
    )
    admin_subject = _validate_directory_admin_subject(
        _require_text(mapping.get("admin_subject"), "directory.admin_subject")
    )
    service_account_email = _validate_directory_service_account_email(
        _require_text(
            mapping.get("service_account_email"),
            "directory.service_account_email",
        )
    )
    expected_service_account = (
        f"mim-identity-sync@{_CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"
    )
    if service_account_email != expected_service_account:
        raise ConfigError("directory.service_account_email is invalid.")
    return DirectoryBootstrap(
        admin_subject=admin_subject,
        service_account_email=service_account_email,
    )


def _require_github_app(value: object, *, project_id: str) -> GitHubAppBootstrap:
    mapping = _require_mapping(value, "github_app")
    _require_exact_mapping_keys(
        mapping,
        expected=_GITHUB_APP_FIELDS,
        field_name="github_app",
    )
    bindings_value = mapping.get("bindings")
    if not isinstance(bindings_value, list) or not bindings_value:
        raise ConfigError("github_app.bindings must be a non-empty list.")
    bindings: list[RepositoryBindingBootstrap] = []
    seen_binding_ids: set[int] = set()
    seen_binding_resources: set[str] = set()
    installation_id = _require_positive_int(
        mapping.get("installation_id"),
        "github_app.installation_id",
    )
    for item in bindings_value:
        entry = _require_mapping(item, "github_app.bindings")
        _require_exact_mapping_keys(
            entry,
            expected=_GITHUB_BINDING_FIELDS,
            field_name="github_app.bindings",
        )
        repository_numeric_id = _require_positive_int(
            entry.get("repository_numeric_id"),
            "github_app.bindings.repository_numeric_id",
        )
        owner = _require_text(entry.get("owner"), "github_app.bindings.owner")
        if owner != GITHUB_OWNER:
            raise ConfigError("github_app.bindings.owner must be madupmarketing.")
        binding_installation_id = _require_positive_int(
            entry.get("installation_id"),
            "github_app.bindings.installation_id",
        )
        if binding_installation_id != installation_id:
            raise ConfigError(
                "github_app.bindings.installation_id must match "
                "github_app.installation_id."
            )
        repository_resource = _require_text(
            entry.get("repository_resource"),
            "github_app.bindings.repository_resource",
        )
        _require_repository_resource(repository_resource, project_id=project_id)
        if repository_numeric_id in seen_binding_ids:
            raise ConfigError(
                "github_app.bindings.repository_numeric_id must be unique."
            )
        if repository_resource in seen_binding_resources:
            raise ConfigError("github_app.bindings.repository_resource must be unique.")
        seen_binding_ids.add(repository_numeric_id)
        seen_binding_resources.add(repository_resource)
        bindings.append(
            RepositoryBindingBootstrap(
                repository_numeric_id=repository_numeric_id,
                owner=owner,
                name=_require_text(entry.get("name"), "github_app.bindings.name"),
                installation_id=binding_installation_id,
                repository_resource=repository_resource,
            )
        )
    repository_ids = mapping.get("allowed_repository_ids")
    if not isinstance(repository_ids, list) or not repository_ids:
        raise ConfigError("github_app.allowed_repository_ids must be a non-empty list.")
    allowlist: list[int] = []
    seen_allowlist_ids: set[int] = set()
    for item in repository_ids:
        repository_id = _require_positive_int(
            item,
            "github_app.allowed_repository_ids",
        )
        if repository_id in seen_allowlist_ids:
            raise ConfigError("github_app.allowed_repository_ids must be unique.")
        seen_allowlist_ids.add(repository_id)
        allowlist.append(repository_id)
    if seen_binding_ids != seen_allowlist_ids:
        raise ConfigError(
            "github_app.bindings.repository_numeric_id must match "
            "the repository allowlist."
        )
    return GitHubAppBootstrap(
        app_id=_require_numeric_string(mapping.get("app_id"), "github_app.app_id"),
        private_key_secret_version=_require_secret_version_ref(
            mapping.get("private_key_secret_version"),
            field_name="github_app.private_key_secret_version",
            project_id=project_id,
        ),
        installation_id=installation_id,
        allowed_repository_ids=frozenset(allowlist),
        bindings=tuple(bindings),
    )


def _require_build(value: object) -> BuildBootstrap:
    mapping = _require_mapping(value, "build")
    _require_exact_mapping_keys(
        mapping,
        expected=_BUILD_FIELDS,
        field_name="build",
    )
    builder_image = _require_text(mapping.get("builder_image"), "build.builder_image")
    _require_builder_image(builder_image)
    build_service_account = _require_text(
        mapping.get("build_service_account"),
        "build.build_service_account",
    )
    expected = (
        f"projects/{_CENTRAL_PROJECT_ID}/serviceAccounts/"
        f"mim-build@{_CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"
    )
    if build_service_account != expected:
        raise ConfigError("build.build_service_account is invalid.")
    return BuildBootstrap(
        builder_image=builder_image,
        build_service_account=build_service_account,
    )


def _require_machine_service(
    value: object,
    *,
    field_name: str,
    expected_service_account: str,
    expected_origin: str,
    expected_path: str,
) -> MachineServiceBootstrap:
    mapping = _require_mapping(value, field_name)
    _require_exact_mapping_keys(
        mapping,
        expected=_MACHINE_SERVICE_FIELDS,
        field_name=field_name,
    )
    url = _require_text(mapping.get("url"), f"{field_name}.url")
    audience = _require_text(mapping.get("audience"), f"{field_name}.audience")
    service_account = _require_text(
        mapping.get("service_account_email"),
        f"{field_name}.service_account_email",
    )
    if service_account != expected_service_account:
        raise ConfigError(f"{field_name}.service_account_email is invalid.")
    if audience != expected_origin or url != f"{expected_origin}{expected_path}":
        raise ConfigError(f"{field_name} service URL or audience is invalid.")
    return MachineServiceBootstrap(
        url=url,
        audience=audience,
        service_account_email=service_account,
    )


def _reject_unknown_runtime_keys(mapping: Mapping[str, str]) -> None:
    for key in mapping:
        if key.startswith("MIM_") and key not in _RUNTIME_ENV_KEYS:
            raise ConfigError(f"{key} is not a supported runtime startup key.")


def _require_runtime_mode(value: object) -> RuntimeMode:
    if value == RuntimeMode.CONTROL_PLANE.value:
        return RuntimeMode.CONTROL_PLANE
    if value == RuntimeMode.DEPLOY_WORKER.value:
        return RuntimeMode.DEPLOY_WORKER
    if value == RuntimeMode.SCHEDULE_GATEWAY.value:
        return RuntimeMode.SCHEDULE_GATEWAY
    if value == RuntimeMode.IDENTITY_SYNC.value:
        return RuntimeMode.IDENTITY_SYNC
    if value == RuntimeMode.LIFECYCLE.value:
        return RuntimeMode.LIFECYCLE
    if value == RuntimeMode.USAGE_INGEST.value:
        return RuntimeMode.USAGE_INGEST
    raise ConfigError(
        "MIM_RUNTIME_MODE must be exactly control-plane, deploy-worker, "
        "schedule-gateway, identity-sync, lifecycle, or usage-ingest."
    )


def _require_exact_bool_string(value: object) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ConfigError("MIM_ENABLE_MUTATIONS must be exact true or false.")


def _require_secret_version_ref(
    value: object,
    *,
    field_name: str,
    project_id: str,
) -> str:
    text = _require_text(value, field_name)
    parts = text.split("/")
    if (
        len(parts) != 6
        or parts[0] != "projects"
        or parts[2] != "secrets"
        or parts[4] != "versions"
        or parts[1] != project_id
        or not parts[5].isdigit()
        or parts[5].startswith("0")
    ):
        raise ConfigError(
            f"{field_name} must be a full numeric Secret Manager version."
        )
    return text


def _require_identifier(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    if any(char.isspace() for char in text):
        raise ConfigError(f"{field_name} must not contain whitespace.")
    return text


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ConfigError(f"{field_name} must be exact non-empty text.")
    return value.strip()


def _require_numeric_string(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    if not text.isdigit():
        raise ConfigError(f"{field_name} must be numeric text.")
    return text


def _require_project_number(value: object) -> str:
    text = _require_numeric_string(value, "project_number")
    if text.startswith("0"):
        raise ConfigError("project_number must be a non-zero numeric string.")
    return text


def _require_positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ConfigError(f"{field_name} must be a positive integer.")
    return value


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field_name} must be a JSON object.")
    return value


def _require_scopes(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{field_name} must be a non-empty list.")
    scopes = tuple(_require_identifier(item, field_name) for item in value)
    if len(set(scopes)) != len(scopes):
        raise ConfigError(f"{field_name} must be unique.")
    if set(scopes) != {"chat:write", "commands"}:
        raise ConfigError(f"{field_name} must be exactly chat:write and commands.")
    return ("chat:write", "commands")


def _require_slack_scopes(value: object) -> tuple[str, ...]:
    if value is _MISSING:
        return ()
    mapping = _require_mapping(value, "slack")
    _require_exact_mapping_keys(
        mapping,
        expected=_SLACK_FIELDS,
        field_name="slack",
    )
    return _require_scopes(
        mapping.get("required_scopes"),
        field_name="slack.required_scopes",
    )


def _validate_fixed_project_id(value: object) -> str:
    project_id = _validate_project_id(_require_text(value, "project_id"))
    if project_id != _CENTRAL_PROJECT_ID:
        raise ConfigError("project_id must match the central MIM project.")
    return project_id


def _require_company_member(value: object, *, field_name: str) -> str:
    text = _require_text(value, field_name).casefold()
    if not (text.startswith("user:") or text.startswith("group:")):
        raise ConfigError(f"{field_name} must contain only @madup.com users/groups.")
    if not text.endswith(f"@{COMPANY_DOMAIN}"):
        raise ConfigError(f"{field_name} must contain only @madup.com users/groups.")
    return text


def _load_secret_bytes(*, secret_version: str, credentials: object) -> bytes:
    from google.cloud import secretmanager_v1

    client = secretmanager_v1.SecretManagerServiceClient(
        credentials=cast(Any, credentials)
    )
    response = client.access_secret_version(request={"name": secret_version})
    payload = getattr(response, "payload", None)
    data = getattr(payload, "data", None)
    if type(data) is not bytes or len(data) < 1:
        raise ConfigError("Referenced secret payload could not be read.")
    return data


def _load_secret_text(*, secret_version: str, credentials: object) -> str:
    try:
        return _load_secret_bytes(
            secret_version=secret_version,
            credentials=credentials,
        ).decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("Referenced secret payload could not be read.") from None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Runtime bootstrap contains duplicate JSON keys.")
        result[key] = value
    return result


def _require_exact_mapping_keys(
    mapping: Mapping[str, object],
    *,
    expected: frozenset[str],
    optional: frozenset[str] = frozenset(),
    field_name: str,
) -> None:
    keys = frozenset(mapping)
    required = expected - optional
    if keys - expected or required - keys:
        unexpected = sorted(keys - expected)
        missing = sorted(required - keys)
        details = ", ".join(unexpected + missing)
        raise ConfigError(f"{field_name} contains unsupported keys: {details}")


def _require_repository_resource(value: str, *, project_id: str) -> None:
    parts = value.split("/")
    if (
        len(parts) != 8
        or parts[0] != "projects"
        or parts[1] != project_id
        or parts[2] != "locations"
        or parts[3] != REGION
        or parts[4] != "connections"
        or parts[6] != "repositories"
    ):
        raise ConfigError("github_app.bindings.repository_resource is invalid.")
    if parts[5] == "" or parts[7] == "":
        raise ConfigError("github_app.bindings.repository_resource is invalid.")
    if not value.startswith(
        f"projects/{project_id}/locations/{REGION}/connections/"
    ) or "/repositories/" not in value:
        raise ConfigError("github_app.bindings.repository_resource is invalid.")


def _require_builder_image(value: str) -> None:
    prefix = (
        f"{REGION}-docker.pkg.dev/{_CENTRAL_PROJECT_ID}/mim-platform/mim-builder@sha256:"
    )
    if not value.startswith(prefix):
        raise ConfigError("build.builder_image is invalid.")
    digest = value.removeprefix(prefix)
    if len(digest) != 64 or any(char not in _HEX_64_CHARS for char in digest):
        raise ConfigError("build.builder_image is invalid.")


def _require_ledger_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != {
        "state",
        "stable_token",
        "run_reference",
    }:
        raise ValueError("schedule dispatch ledger record is invalid.")
    state = value.get("state")
    if state not in {"claimed", "ambiguous", "succeeded"}:
        raise ValueError("schedule dispatch ledger record is invalid.")
    stable_token = value.get("stable_token")
    if (
        type(stable_token) is not str
        or len(stable_token) != 64
        or any(char not in _HEX_64_CHARS for char in stable_token)
    ):
        raise ValueError("schedule dispatch ledger record is invalid.")
    run_reference = value.get("run_reference")
    if run_reference is not None and (
        type(run_reference) is not str or not run_reference
    ):
        raise ValueError("schedule dispatch ledger record is invalid.")
    return dict(value)


def _runtime_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _runtime_lease_token() -> str:
    return secrets.token_urlsafe(32)
