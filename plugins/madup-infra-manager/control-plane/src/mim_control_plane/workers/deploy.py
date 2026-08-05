"""Private deploy worker executing verified build and runtime effects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from mim_control_plane.adapters.github import (
    GitHubSourceIntegrityError,
    GitHubSourceUnavailableError,
)
from mim_control_plane.config import GITHUB_OWNER
from mim_control_plane.domain.models import Operation, RepositoryAdmission, Workload
from mim_control_plane.domain.states import (
    OperationState,
    RepositoryAdmissionState,
    WorkloadState,
)
from mim_control_plane.ports.execution import (
    ArtifactConflictError,
    ArtifactRegistryPort,
    BuildPort,
    BuildRequest,
    DeploymentQueuePort,
    DesiredStateArtifactPort,
    ExecutionPlaneError,
    QueuedDeployTask,
    RetryableExecutionPlaneError,
    RuntimeIdentityPort,
    RuntimePort,
    SecretMetadataDeniedError,
    SecretMetadataPort,
    SnapshotAttestation,
)
from mim_control_plane.ports.source import SourceSnapshotPort
from mim_control_plane.ports.store import (
    IdempotencyConflict,
    InvariantViolation,
    NotFound,
    Store,
    VersionConflict,
)
from mim_control_plane.services.app_hostname import AppHostnameBindingService
from mim_control_plane.services.build_template import build_template_for
from mim_control_plane.services.classifier import (
    ManifestValidationError,
    SnapshotValidationError,
    WorkloadClassification,
    classify_snapshot,
)
from mim_control_plane.services.render import (
    DesiredStateDenied,
    DesiredStateRenderContext,
    DesiredStateTarget,
    VerifiedDesiredState,
    render_signed_desired_state,
    verify_signed_desired_state,
)

_LOWER_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RECORDED_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL_STATES = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.ROLLED_BACK,
        OperationState.CANCELLED,
        OperationState.QUARANTINED,
    }
)
_IN_PROGRESS_STATES = frozenset(
    {
        OperationState.BUILDING,
        OperationState.DEPLOYING,
        OperationState.VERIFYING,
    }
)
_SAFE_WORKLOAD_STATES = frozenset({WorkloadState.ACTIVE, WorkloadState.FAILED})
_TRUST_FAILURE = "deploy_denied"
_BUILD_FAILURE = "build_failed"
_RUNTIME_FAILURE = "deploy_failed"
_HEALTH_FAILURE = "deploy_unhealthy"


@dataclass(frozen=True, slots=True)
class DeployWorkerResult:
    operation: Operation
    status: str

    @classmethod
    def completed(cls, operation: Operation) -> DeployWorkerResult:
        return cls(operation=operation, status="completed")

    @classmethod
    def in_progress(cls, operation: Operation) -> DeployWorkerResult:
        return cls(operation=operation, status="in_progress")


@dataclass(frozen=True, slots=True)
class PrivateDeployWorker:
    store: Store
    queue: DeploymentQueuePort
    source: SourceSnapshotPort
    build: BuildPort
    registry: ArtifactRegistryPort
    artifacts: DesiredStateArtifactPort
    runtime_identity: RuntimeIdentityPort
    runtime: RuntimePort
    secrets: SecretMetadataPort
    render_context: DesiredStateRenderContext
    signing_key: bytes = field(repr=False)

    def run(
        self,
        *,
        operation_id: str,
        now: datetime,
    ) -> DeployWorkerResult:
        task = self.queue.get(operation_id)  # type: ignore[arg-type]
        operation = self.store.get_operation(task.operation_id)
        if operation.state in _TERMINAL_STATES:
            return DeployWorkerResult.completed(operation)
        if operation.state in _IN_PROGRESS_STATES:
            return DeployWorkerResult.in_progress(operation)

        workload, admission = self._load_trusted_records(task)
        denied = self._preflight_denial(
            operation=operation,
            task=task,
            workload=workload,
            admission=admission,
            now=now,
        )
        if denied is not None:
            return denied
        assert workload is not None
        assert admission is not None

        try:
            snapshot = self._verified_snapshot(task=task, admission=admission)
            classification = self._classify(snapshot=snapshot, workload=workload)
            template = build_template_for(classification)
        except RetryableExecutionPlaneError:
            raise
        except DesiredStateDenied:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )

        building = self._advance(
            operation=operation,
            target=OperationState.BUILDING,
            now=now,
        )
        if building.status != "advanced":
            assert building.result is not None
            return building.result
        current = building.operation

        try:
            build_request = BuildRequest.from_task(
                task=task,
                admission=admission,
                classification=classification,
                template=template,
            )
            image_digest = self.build.build(build_request)
            if _LOWER_SHA256_PATTERN.fullmatch(image_digest) is None:
                raise DesiredStateDenied("build digest must be exact.")
            self.registry.retain(image_digest)
            try:
                runtime_identity_email = self.runtime_identity.ensure_exact(workload.id)
            except ExecutionPlaneError:
                return self._finish(
                    operation=current,
                    target=OperationState.FAILED,
                    now=now,
                    sanitized_failure=_RUNTIME_FAILURE,
                )
            secret_attachments = self.secrets.resolve(
                workload_id=workload.id,
                attachments=task.secret_attachments,
            )
            envelope = render_signed_desired_state(
                workload=workload,
                admission=admission,
                snapshot=snapshot,
                image_digest=image_digest,
                context=self.render_context,
                issued_at=now,
                signing_key=self.signing_key,
                secret_attachments=secret_attachments,
            )
            if envelope.payload.runtime_service_account != runtime_identity_email:
                raise DesiredStateDenied("runtime identity material drifted.")
            persisted_envelope = self.artifacts.create_once(
                operation_id=task.operation_id,
                envelope=envelope,
            )
            verified = verify_signed_desired_state(
                persisted_envelope,
                context=self.render_context,
                signing_key=self.signing_key,
                now=now,
            )
        except (
            ArtifactConflictError,
            DesiredStateDenied,
            SecretMetadataDeniedError,
            ValueError,
        ):
            return self._finish(
                operation=current,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        except ExecutionPlaneError:
            return self._finish(
                operation=current,
                target=OperationState.FAILED,
                now=now,
                sanitized_failure=_BUILD_FAILURE,
            )

        midflight = self._revalidate_before_runtime(
            task=task,
            operation=current,
            expected_state=OperationState.BUILDING,
            now=now,
        )
        if midflight is not None:
            return midflight

        deploying = self._advance(
            operation=current,
            target=OperationState.DEPLOYING,
            now=now,
        )
        if deploying.status != "advanced":
            assert deploying.result is not None
            return deploying.result
        current = deploying.operation

        pre_apply = self._revalidate_before_runtime(
            task=task,
            operation=current,
            expected_state=OperationState.DEPLOYING,
            now=now,
        )
        if pre_apply is not None:
            return pre_apply

        try:
            self.runtime.apply(verified)
        except ExecutionPlaneError:
            return self._finish(
                operation=current,
                target=OperationState.FAILED,
                now=now,
                sanitized_failure=_RUNTIME_FAILURE,
            )

        verifying = self._advance(
            operation=current,
            target=OperationState.VERIFYING,
            now=now,
        )
        if verifying.status != "advanced":
            assert verifying.result is not None
            return verifying.result
        current = verifying.operation

        try:
            if self.runtime.verify_health(verified):
                try:
                    self._persist_app_hostname_binding(
                        workload=workload,
                        verified=verified,
                        now=now,
                    )
                except ExecutionPlaneError:
                    return self._finish(
                        operation=current,
                        target=OperationState.FAILED,
                        now=now,
                        sanitized_failure=_RUNTIME_FAILURE,
                    )
                except (IdempotencyConflict, InvariantViolation, ValueError):
                    return self._finish(
                        operation=current,
                        target=OperationState.QUARANTINED,
                        now=now,
                        sanitized_failure=_TRUST_FAILURE,
                    )
                return self._finish(
                    operation=current,
                    target=OperationState.SUCCEEDED,
                    now=now,
                )
        except (IdempotencyConflict, InvariantViolation, ValueError):
            return self._finish(
                operation=current,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        except ExecutionPlaneError:
            return self._finish(
                operation=current,
                target=OperationState.FAILED,
                now=now,
                sanitized_failure=_RUNTIME_FAILURE,
            )

        rollback_workload = self._reload_workload(task)
        if rollback_workload is not None and _has_recorded_healthy_digest(
            rollback_workload.last_healthy_image_digest
        ):
            try:
                self.runtime.rollback(
                    workload_id=rollback_workload.id,
                    workload_owner_id=rollback_workload.owner_id,
                    image_digest=str(rollback_workload.last_healthy_image_digest),
                )
            except ExecutionPlaneError:
                return self._finish(
                    operation=current,
                    target=OperationState.FAILED,
                    now=now,
                    sanitized_failure=_RUNTIME_FAILURE,
                )
            return self._finish(
                operation=current,
                target=OperationState.ROLLED_BACK,
                now=now,
                sanitized_failure=_HEALTH_FAILURE,
            )
        return self._finish(
            operation=current,
            target=OperationState.FAILED,
            now=now,
            sanitized_failure=_HEALTH_FAILURE,
        )

    def _persist_app_hostname_binding(
        self,
        *,
        workload: Workload,
        verified: VerifiedDesiredState,
        now: datetime,
    ) -> None:
        if type(verified) is not VerifiedDesiredState:
            raise ExecutionPlaneError("verified desired state was malformed.")
        payload = verified.envelope.payload
        if payload.target is not DesiredStateTarget.CLOUD_RUN_SERVICE:
            return
        route = self.runtime.readback_service_route(verified)
        if route is None:
            raise ExecutionPlaneError("runtime service route was missing.")
        AppHostnameBindingService(store=self.store).create_active_binding(
            workload=workload,
            service_resource=route.resource_name,
            service_uri=route.uri,
            now=now,
        )

    def _load_trusted_records(
        self,
        task: QueuedDeployTask,
    ) -> tuple[Workload | None, RepositoryAdmission | None]:
        workload: Workload | None
        admission: RepositoryAdmission | None
        try:
            workload = self.store.get_workload(task.workload_id)
        except NotFound:
            workload = None
        try:
            admission = self.store.get_repository_admission(task.admission_id)
        except NotFound:
            admission = None
        return workload, admission

    def _preflight_denial(
        self,
        *,
        operation: Operation,
        task: QueuedDeployTask,
        workload: Workload | None,
        admission: RepositoryAdmission | None,
        now: datetime,
    ) -> DeployWorkerResult | None:
        if operation.workload_id != task.workload_id:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if operation.action != "deploy":
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if operation.state is not OperationState.QUEUED:
            return self._finish_or_report(operation=operation)
        if operation.version != task.expected_operation_version:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload is None or admission is None:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.version != task.expected_workload_version:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.state not in _SAFE_WORKLOAD_STATES:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.version != task.expected_admission_version:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.repository_admission_id != admission.id:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.state is not RepositoryAdmissionState.ADMITTED:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.source_sha != task.expected_source_sha:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.admitted_sha != task.expected_source_sha:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.owner != GITHUB_OWNER:
            return self._finish(
                operation=operation,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        return None

    def _revalidate_before_runtime(
        self,
        *,
        task: QueuedDeployTask,
        operation: Operation,
        expected_state: OperationState,
        now: datetime,
    ) -> DeployWorkerResult | None:
        refreshed = self.store.get_operation(operation.id)
        if refreshed.state in _TERMINAL_STATES:
            return DeployWorkerResult.completed(refreshed)
        if (
            refreshed.state is not expected_state
            or refreshed.version != operation.version
            or refreshed.workload_id != task.workload_id
            or refreshed.action != "deploy"
        ):
            return DeployWorkerResult.in_progress(refreshed)
        workload, admission = self._load_trusted_records(task)
        if workload is None or admission is None:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.version != task.expected_workload_version:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.state not in _SAFE_WORKLOAD_STATES:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.version != task.expected_admission_version:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.state is not RepositoryAdmissionState.ADMITTED:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.repository_admission_id != admission.id:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if workload.source_sha != task.expected_source_sha:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.admitted_sha != task.expected_source_sha:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        if admission.owner != GITHUB_OWNER:
            return self._finish(
                operation=refreshed,
                target=OperationState.QUARANTINED,
                now=now,
                sanitized_failure=_TRUST_FAILURE,
            )
        return None

    def _verified_snapshot(
        self,
        *,
        task: QueuedDeployTask,
        admission: RepositoryAdmission,
    ) -> dict[str, bytes]:
        try:
            snapshot = {
                path: bytes(content)
                for path, content in self.source.fetch_snapshot(admission).items()
            }
        except RetryableExecutionPlaneError:
            raise
        except GitHubSourceUnavailableError:
            raise RetryableExecutionPlaneError("source_fetch_failed") from None
        except GitHubSourceIntegrityError:
            raise DesiredStateDenied("snapshot source validation was denied.") from None
        except Exception:
            raise DesiredStateDenied("snapshot source validation was denied.") from None
        attestation = SnapshotAttestation.from_snapshot(snapshot)
        if (
            attestation.digest != task.expected_snapshot_digest
            or attestation.file_count != task.expected_snapshot_file_count
            or attestation.byte_count != task.expected_snapshot_byte_count
        ):
            raise DesiredStateDenied("snapshot attestation changed.")
        return snapshot

    def _classify(
        self,
        *,
        snapshot: dict[str, bytes],
        workload: Workload,
    ) -> WorkloadClassification:
        try:
            classified = classify_snapshot(snapshot)
        except (ManifestValidationError, SnapshotValidationError):
            raise DesiredStateDenied("snapshot classification was denied.") from None
        if type(classified) is not WorkloadClassification:
            raise DesiredStateDenied("snapshot classification was denied.")
        if classified.kind is not workload.kind:
            raise DesiredStateDenied("snapshot kind changed.")
        return classified

    def _finish_or_report(self, *, operation: Operation) -> DeployWorkerResult:
        if operation.state in _TERMINAL_STATES:
            return DeployWorkerResult.completed(operation)
        return DeployWorkerResult.in_progress(operation)

    def _reload_workload(self, task: QueuedDeployTask) -> Workload | None:
        try:
            return self.store.get_workload(task.workload_id)
        except NotFound:
            return None

    def _finish(
        self,
        *,
        operation: Operation,
        target: OperationState,
        now: datetime,
        sanitized_failure: str | None = None,
    ) -> DeployWorkerResult:
        save_attempt = self._advance(
            operation=operation,
            target=target,
            now=now,
            sanitized_failure=sanitized_failure,
        )
        if save_attempt.status != "advanced":
            assert save_attempt.result is not None
            return save_attempt.result
        return DeployWorkerResult.completed(save_attempt.operation)

    def _advance(
        self,
        *,
        operation: Operation,
        target: OperationState,
        now: datetime,
        sanitized_failure: str | None = None,
    ) -> _AdvanceResult:
        try:
            updated = operation.transition(
                target,
                at=now,
                sanitized_failure=sanitized_failure,
            )
            saved = self.store.save_operation(
                updated, expected_version=operation.version
            )
            return _AdvanceResult(status="advanced", operation=saved)
        except VersionConflict:
            current = self.store.get_operation(operation.id)
            return _AdvanceResult(
                status="reconciled",
                operation=current,
                result=self._finish_or_report(operation=current),
            )


@dataclass(frozen=True, slots=True)
class _AdvanceResult:
    status: str
    operation: Operation
    result: DeployWorkerResult | None = None


def _has_recorded_healthy_digest(value: str | None) -> bool:
    return type(value) is str and _RECORDED_SHA256_PATTERN.fullmatch(value) is not None
