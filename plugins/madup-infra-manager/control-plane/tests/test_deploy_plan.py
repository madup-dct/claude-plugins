from __future__ import annotations

import dataclasses
import hashlib
import hmac
import inspect
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
FIXTURE_ROOT = TEST_ROOT / "fixtures" / "repos"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import mim_control_plane.services.classifier as classifier_module  # noqa: E402
from mim_control_plane.domain.models import (  # noqa: E402
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
from mim_control_plane.services.render import (  # noqa: E402
    DesiredStateAuthMode,
    DesiredStateDenied,
    DesiredStateIngress,
    DesiredStatePayload,
    DesiredStateRenderContext,
    DesiredStateSecretAttachment,
    DesiredStateTarget,
    SignedDesiredStateEnvelope,
    VerifiedDesiredState,
    canonical_unsigned_desired_state_bytes,
    render_signed_desired_state,
    verify_signed_desired_state,
)

NOW = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)
KEY = b"k" * 32
OTHER_KEY = b"o" * 32


def load_fixture_snapshot(name: str) -> dict[str, bytes]:
    fixture_dir = FIXTURE_ROOT / name
    snapshot: dict[str, bytes] = {}
    for path in sorted(fixture_dir.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(fixture_dir).as_posix()] = path.read_bytes()
    return snapshot


def admission(
    *,
    admission_id: str = "repo-1",
    owner: str = "madupmarketing",
    name: str = "sample-app",
    state: RepositoryAdmissionState = RepositoryAdmissionState.ADMITTED,
    admitted_sha: str = "b" * 40,
    version: int = 3,
) -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId(admission_id),
        repository_numeric_id=42,
        owner=owner,
        name=name,
        installation_id=99,
        state=state,
        admitted_sha=admitted_sha,
        created_at=NOW - timedelta(days=14),
        updated_at=NOW - timedelta(hours=1),
        version=version,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    admission_id: str = "repo-1",
    kind: WorkloadKind = WorkloadKind.NEXTJS,
    source_sha: str = "b" * 40,
    version: int = 7,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId(admission_id),
        name="sample-app",
        kind=kind,
        state=WorkloadState.ACTIVE,
        source_sha=source_sha,
        desired_manifest_hash="manifest-hash-1",
        created_at=NOW - timedelta(days=14),
        updated_at=NOW - timedelta(minutes=10),
        last_activity_at=NOW - timedelta(minutes=10),
        last_healthy_image_digest="sha256:" + "a" * 64,
        version=version,
    )


def context(
    *,
    project_id: str = "madup-prod1",
    key_id: str = "deploy-key-1",
) -> DesiredStateRenderContext:
    return DesiredStateRenderContext(project_id=project_id, key_id=key_id)


def render(
    *,
    repo_fixture: str,
    kind: WorkloadKind,
    image_digest: str = "a" * 64,
    issued_at: datetime = NOW,
    secret_attachments: tuple[DesiredStateSecretAttachment, ...] = (),
    admission_record: RepositoryAdmission | None = None,
    workload_record: Workload | None = None,
    render_context: DesiredStateRenderContext | None = None,
) -> SignedDesiredStateEnvelope:
    current_admission = admission_record or admission()
    current_workload = workload_record or workload(kind=kind)
    current_context = render_context or context()
    return render_signed_desired_state(
        workload=current_workload,
        admission=current_admission,
        snapshot=load_fixture_snapshot(repo_fixture),
        image_digest=image_digest,
        context=current_context,
        issued_at=issued_at,
        signing_key=KEY,
        secret_attachments=secret_attachments,
    )


def unsafe_replace_payload(
    payload: DesiredStatePayload,
    **changes: Any,
) -> DesiredStatePayload:
    values = {
        field.name: getattr(payload, field.name)
        for field in dataclasses.fields(DesiredStatePayload)
    }
    values.update(changes)
    instance = object.__new__(DesiredStatePayload)
    for field in dataclasses.fields(DesiredStatePayload):
        object.__setattr__(instance, field.name, values[field.name])
    return instance


class DeployPlanTests(unittest.TestCase):
    def test_render_binds_exact_web_and_scheduled_runtime_shapes(self) -> None:
        web_cases = (
            (
                "streamlit",
                WorkloadKind.STREAMLIT,
                "python3.13",
                3600,
                (
                    "streamlit",
                    "run",
                    "app.py",
                    "--server.address",
                    "0.0.0.0",
                    "--server.port",
                    "8080",
                ),
            ),
            (
                "nextjs",
                WorkloadKind.NEXTJS,
                "node20",
                300,
                (
                    "./node_modules/.bin/next",
                    "start",
                    "--hostname",
                    "0.0.0.0",
                    "--port",
                    "8080",
                ),
            ),
        )
        for fixture_name, kind, runtime, timeout_seconds, launch in web_cases:
            with self.subTest(fixture=fixture_name):
                envelope = render(repo_fixture=fixture_name, kind=kind)
                payload = envelope.payload
                self.assertEqual(payload.target, DesiredStateTarget.CLOUD_RUN_SERVICE)
                self.assertEqual(payload.runtime, runtime)
                self.assertEqual(payload.launch_command, launch)
                self.assertEqual(payload.cpu, 1)
                self.assertEqual(payload.memory_mib, 512)
                self.assertEqual(payload.service_min_instances, 0)
                self.assertEqual(payload.service_max_instances, 1)
                self.assertFalse(payload.request_cpu_always_allocated)
                self.assertGreaterEqual(payload.service_concurrency, 1)
                self.assertEqual(payload.service_timeout_seconds, timeout_seconds)
                self.assertIsNone(payload.schedule_cron)
                self.assertEqual(
                    payload.ingress,
                    DesiredStateIngress.PUBLIC_IAM,
                )
                self.assertEqual(
                    payload.auth_mode,
                    DesiredStateAuthMode.GATEWAY_IAM,
                )
                self.assertFalse(payload.allow_unauthenticated)

        scheduled = render(
            repo_fixture="scheduled_script",
            kind=WorkloadKind.SCHEDULED_SCRIPT,
        )
        self.assertEqual(scheduled.payload.target, DesiredStateTarget.CLOUD_RUN_JOB)
        self.assertEqual(scheduled.payload.launch_command, ("python", "main.py"))
        self.assertEqual(scheduled.payload.job_task_count, 1)
        self.assertEqual(scheduled.payload.job_parallelism, 1)
        self.assertIsNotNone(scheduled.payload.job_retry_count)
        self.assertIsNotNone(scheduled.payload.job_timeout_seconds)
        self.assertGreaterEqual(cast(int, scheduled.payload.job_retry_count), 0)
        self.assertGreaterEqual(cast(int, scheduled.payload.job_timeout_seconds), 60)
        self.assertEqual(scheduled.payload.schedule_cron, "0 * * * *")
        self.assertEqual(scheduled.payload.service_min_instances, 0)
        self.assertEqual(scheduled.payload.service_max_instances, 1)
        self.assertEqual(scheduled.payload.ingress, DesiredStateIngress.NONE)
        self.assertEqual(
            scheduled.payload.auth_mode,
            DesiredStateAuthMode.MACHINE_ONLY,
        )

    def test_same_input_is_deterministic_and_signature_stable(self) -> None:
        first = render(repo_fixture="nextjs", kind=WorkloadKind.NEXTJS)
        second = render(repo_fixture="nextjs", kind=WorkloadKind.NEXTJS)

        self.assertEqual(first, second)
        self.assertEqual(
            canonical_unsigned_desired_state_bytes(first),
            canonical_unsigned_desired_state_bytes(second),
        )
        self.assertEqual(first.signature, second.signature)

    def test_snapshot_digest_binds_full_snapshot_even_ignored_files(self) -> None:
        baseline_snapshot = load_fixture_snapshot("nextjs")
        changed_snapshot = load_fixture_snapshot("nextjs")
        changed_snapshot["Dockerfile"] = b"FROM node:20\n"

        baseline = render_signed_desired_state(
            workload=workload(kind=WorkloadKind.NEXTJS),
            admission=admission(),
            snapshot=baseline_snapshot,
            image_digest="a" * 64,
            context=context(),
            issued_at=NOW,
            signing_key=KEY,
        )
        changed = render_signed_desired_state(
            workload=workload(kind=WorkloadKind.NEXTJS),
            admission=admission(),
            snapshot=changed_snapshot,
            image_digest="a" * 64,
            context=context(),
            issued_at=NOW,
            signing_key=KEY,
        )

        self.assertRegex(baseline.payload.snapshot_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(changed.payload.snapshot_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(
            baseline.payload.snapshot_digest,
            changed.payload.snapshot_digest,
        )
        self.assertNotEqual(
            canonical_unsigned_desired_state_bytes(baseline),
            canonical_unsigned_desired_state_bytes(changed),
        )
        self.assertNotEqual(baseline.signature, changed.signature)

        verified = verify_signed_desired_state(
            baseline,
            context=context(),
            signing_key=KEY,
            now=NOW + timedelta(minutes=1),
        )
        self.assertEqual(verified.snapshot_digest, baseline.payload.snapshot_digest)

    def test_oversized_snapshot_is_rejected_before_digesting(self) -> None:
        oversized = {
            f"file-{index}.txt": b"x"
            for index in range(classifier_module.MAX_SNAPSHOT_FILES + 1)
        }

        with patch(
            "mim_control_plane.services.render._snapshot_digest",
            side_effect=AssertionError("digest should not run"),
        ):
            with self.assertRaises(DesiredStateDenied):
                render_signed_desired_state(
                    workload=workload(kind=WorkloadKind.NEXTJS),
                    admission=admission(),
                    snapshot=oversized,
                    image_digest="a" * 64,
                    context=context(),
                    issued_at=NOW,
                    signing_key=KEY,
                )

    def test_kind_mismatch_or_ambiguous_snapshot_is_denied(self) -> None:
        ambiguous = load_fixture_snapshot("streamlit")
        ambiguous.update(load_fixture_snapshot("nextjs"))
        cases = (
            (
                workload(kind=WorkloadKind.NEXTJS),
                load_fixture_snapshot("streamlit"),
            ),
            (
                workload(kind=WorkloadKind.STREAMLIT),
                load_fixture_snapshot("scheduled_script"),
            ),
            (
                workload(kind=WorkloadKind.NEXTJS),
                ambiguous,
            ),
        )
        for current_workload, snapshot in cases:
            with self.subTest(workload_kind=current_workload.kind, snapshot=snapshot):
                with self.assertRaises(DesiredStateDenied):
                    render_signed_desired_state(
                        workload=current_workload,
                        admission=admission(),
                        snapshot=snapshot,
                        image_digest="a" * 64,
                        context=context(),
                        issued_at=NOW,
                        signing_key=KEY,
                    )

    def test_mutable_or_external_image_inputs_are_denied(self) -> None:
        for bad_digest in (
            "latest",
            "asia-northeast3-docker.pkg.dev/other/mim/app:latest",
            "sha256:" + "a" * 64,
            "A" * 64,
            "a" * 63,
            True,
        ):
            with self.subTest(bad_digest=bad_digest):
                with self.assertRaises(DesiredStateDenied):
                    render_signed_desired_state(
                        workload=workload(kind=WorkloadKind.NEXTJS),
                        admission=admission(),
                        snapshot=load_fixture_snapshot("nextjs"),
                        image_digest=cast(Any, bad_digest),
                        context=context(),
                        issued_at=NOW,
                        signing_key=KEY,
                    )

    def test_verify_rejects_wrong_tampered_or_expired_material(self) -> None:
        envelope = render(repo_fixture="nextjs", kind=WorkloadKind.NEXTJS)
        verified = verify_signed_desired_state(
            envelope,
            context=context(),
            signing_key=KEY,
            now=NOW + timedelta(minutes=1),
        )
        self.assertIsInstance(verified, VerifiedDesiredState)
        self.assertEqual(verified.snapshot_digest, envelope.payload.snapshot_digest)

        bad_cases = (
            dataclasses.replace(envelope, signature="0" * 64),
            dataclasses.replace(
                envelope,
                payload=dataclasses.replace(
                    envelope.payload,
                    image_uri=envelope.payload.image_uri.replace("a" * 64, "b" * 64),
                ),
            ),
            dataclasses.replace(
                envelope,
                payload=dataclasses.replace(
                    envelope.payload,
                    snapshot_digest="sha256:" + "b" * 64,
                ),
            ),
            dataclasses.replace(envelope, schema_version="mim-desired-state-v1"),
            dataclasses.replace(envelope, audience="other-worker"),
            dataclasses.replace(envelope, key_id="rotated-key"),
        )
        for candidate in bad_cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(DesiredStateDenied):
                    verify_signed_desired_state(
                        candidate,
                        context=context(),
                        signing_key=KEY,
                        now=NOW + timedelta(minutes=1),
                    )

        with self.assertRaises(DesiredStateDenied):
            verify_signed_desired_state(
                envelope,
                context=context(),
                signing_key=OTHER_KEY,
                now=NOW + timedelta(minutes=1),
            )
        with self.assertRaises(DesiredStateDenied):
            verify_signed_desired_state(
                envelope,
                context=context(),
                signing_key=KEY,
                now=envelope.expires_at + timedelta(seconds=1),
            )
        with self.assertRaises(DesiredStateDenied):
            verify_signed_desired_state(
                envelope,
                context=context(),
                signing_key=KEY,
                now=envelope.issued_at - timedelta(seconds=1),
            )

    def test_verify_rejects_manually_signed_malformed_payload_values(self) -> None:
        envelope = render(repo_fixture="nextjs", kind=WorkloadKind.NEXTJS)

        def signed_with_payload(**changes: Any) -> SignedDesiredStateEnvelope:
            mutated_payload = unsafe_replace_payload(envelope.payload, **changes)
            unsigned = dataclasses.replace(
                envelope,
                payload=mutated_payload,
                signature="0" * 64,
            )
            signature = hmac.new(
                KEY,
                canonical_unsigned_desired_state_bytes(unsigned),
                hashlib.sha256,
            ).hexdigest()
            return dataclasses.replace(unsigned, signature=signature)

        malformed_cases = (
            {"repository_admission_id": ""},
            {"repository_numeric_id": True},
            {"repository_numeric_id": -1},
            {"repository_numeric_id": 0},
            {"repository_owner": ""},
            {"repository_owner": "MadupMarketing"},
            {"repository_owner": "otherowner"},
            {"repository_name": ""},
            {"repository_name": "-bad"},
            {"admission_version": True},
            {"admission_version": 0},
            {"admission_version": -1},
            {"workload_id": ""},
            {"workload_owner_id": ""},
            {"workload_version": True},
            {"workload_version": 0},
            {"workload_version": -1},
            {"source_sha": ""},
            {"source_sha": "a" * 39},
            {"source_sha": "A" * 40},
            {"source_sha": "0" * 40},
            {"source_sha": "g" * 40},
            {"desired_manifest_hash": ""},
            {"request_cpu_always_allocated": True},
            {"ingress": DesiredStateIngress.NONE},
            {"auth_mode": DesiredStateAuthMode.MACHINE_ONLY},
            {"allow_unauthenticated": True},
        )

        for changes in malformed_cases:
            with self.subTest(changes=changes):
                with self.assertRaises(DesiredStateDenied):
                    verify_signed_desired_state(
                        signed_with_payload(**changes),
                        context=context(),
                        signing_key=KEY,
                        now=NOW + timedelta(minutes=1),
                    )

    def test_injection_attempts_are_denied_by_closed_api_and_exact_types(
        self,
    ) -> None:
        for kwargs in (
            {"region": "us-central1"},
            {"service_account": "evil@example.iam.gserviceaccount.com"},
            {"vpc_connector": "projects/x/locations/y/connectors/z"},
            {"ingress": "all"},
            {"auth_mode": "unauthenticated"},
            {"labels": (("owner_id", "usr-1"),)},
            {"launch_command": ("curl", "https://evil.example")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(TypeError):
                    render_signed_desired_state(
                        workload=workload(kind=WorkloadKind.NEXTJS),
                        admission=admission(),
                        snapshot=load_fixture_snapshot("nextjs"),
                        image_digest="a" * 64,
                        context=context(),
                        issued_at=NOW,
                        signing_key=KEY,
                        **kwargs,  # type: ignore[arg-type]
                    )

        class ContextProxy:
            project_id = "madup-prod1"
            key_id = "deploy-key-1"

        with self.assertRaises(DesiredStateDenied):
            render_signed_desired_state(
                workload=workload(kind=WorkloadKind.NEXTJS),
                admission=admission(),
                snapshot=load_fixture_snapshot("nextjs"),
                image_digest="a" * 64,
                context=cast(Any, ContextProxy()),
                issued_at=NOW,
                signing_key=KEY,
            )

    def test_repository_admission_and_workload_identity_are_exactly_bound(
        self,
    ) -> None:
        envelope = render(repo_fixture="nextjs", kind=WorkloadKind.NEXTJS)
        payload = envelope.payload
        self.assertEqual(payload.repository_admission_id, "repo-1")
        self.assertEqual(payload.repository_numeric_id, 42)
        self.assertEqual(payload.repository_owner, "madupmarketing")
        self.assertEqual(payload.repository_name, "sample-app")
        self.assertEqual(payload.admission_version, 3)
        self.assertEqual(payload.workload_id, "wrk-1")
        self.assertEqual(payload.workload_owner_id, "usr-1")
        self.assertEqual(payload.workload_kind, WorkloadKind.NEXTJS.value)
        self.assertEqual(payload.workload_version, 7)
        self.assertEqual(payload.source_sha, "b" * 40)
        self.assertEqual(payload.desired_manifest_hash, "manifest-hash-1")

        bad_cases = (
            (
                workload(kind=WorkloadKind.NEXTJS, admission_id="repo-2"),
                admission(),
            ),
            (
                workload(kind=WorkloadKind.NEXTJS, source_sha="c" * 40),
                admission(),
            ),
            (
                workload(kind=WorkloadKind.NEXTJS),
                admission(owner="otherowner"),
            ),
        )
        for current_workload, current_admission in bad_cases:
            with self.subTest(
                workload_id=current_workload.repository_admission_id,
                owner=current_admission.owner,
            ):
                with self.assertRaises(DesiredStateDenied):
                    render_signed_desired_state(
                        workload=current_workload,
                        admission=current_admission,
                        snapshot=load_fixture_snapshot("nextjs"),
                        image_digest="a" * 64,
                        context=context(),
                        issued_at=NOW,
                        signing_key=KEY,
                    )

    def test_secret_metadata_is_bound_and_invalid_values_do_not_echo(self) -> None:
        attachments = (
            DesiredStateSecretAttachment(
                secret_id="sec-1",
                secret_name="slack-bot-token",
                secret_version="3",
                env_name="MIM_SECRET_SLACK_BOT_TOKEN",
            ),
            DesiredStateSecretAttachment(
                secret_id="sec-2",
                secret_name="notion-oauth",
                secret_version="7",
                env_name="MIM_SECRET_NOTION_OAUTH",
            ),
        )
        envelope = render(
            repo_fixture="scheduled_script",
            kind=WorkloadKind.SCHEDULED_SCRIPT,
            secret_attachments=attachments,
        )
        self.assertEqual(envelope.payload.secret_attachments, attachments)
        verified = verify_signed_desired_state(
            envelope,
            context=context(),
            signing_key=KEY,
            now=NOW + timedelta(minutes=1),
        )
        self.assertEqual(verified.envelope.payload.secret_attachments, attachments)
        self.assertEqual(verified.snapshot_digest, envelope.payload.snapshot_digest)

        reordered = render(
            repo_fixture="scheduled_script",
            kind=WorkloadKind.SCHEDULED_SCRIPT,
            secret_attachments=(attachments[1], attachments[0]),
        )
        self.assertEqual(
            envelope.payload.secret_attachments,
            reordered.payload.secret_attachments,
        )
        self.assertEqual(
            canonical_unsigned_desired_state_bytes(envelope),
            canonical_unsigned_desired_state_bytes(reordered),
        )
        self.assertEqual(envelope.signature, reordered.signature)

        reversed_payload = dataclasses.replace(
            envelope.payload,
            secret_attachments=(attachments[1], attachments[0]),
        )
        reversed_unsigned = dataclasses.replace(
            envelope,
            payload=reversed_payload,
            signature="0" * 64,
        )
        reversed_signature = hmac.new(
            KEY,
            canonical_unsigned_desired_state_bytes(reversed_unsigned),
            hashlib.sha256,
        ).hexdigest()
        reversed_envelope = dataclasses.replace(
            reversed_unsigned,
            signature=reversed_signature,
        )
        with self.assertRaises(DesiredStateDenied):
            verify_signed_desired_state(
                reversed_envelope,
                context=context(),
                signing_key=KEY,
                now=NOW + timedelta(minutes=1),
            )

        bad_cases = (
            tuple(
                DesiredStateSecretAttachment(
                    secret_id=f"sec-{index}",
                    secret_name=f"safe-{index}",
                    secret_version="1",
                    env_name=f"MIM_SECRET_SAFE_{index}",
                )
                for index in range(6)
            ),
            (
                DesiredStateSecretAttachment(
                    secret_id="sec-1",
                    secret_name="slack-bot-token",
                    secret_version="3",
                    env_name="MIM_SECRET_SLACK_BOT_TOKEN",
                ),
                DesiredStateSecretAttachment(
                    secret_id="sec-1",
                    secret_name="other-name",
                    secret_version="4",
                    env_name="MIM_SECRET_OTHER_NAME",
                ),
            ),
            (
                DesiredStateSecretAttachment(
                    secret_id="sec-1",
                    secret_name="slack-bot-token",
                    secret_version="3",
                    env_name="MIM_SECRET_SLACK_BOT_TOKEN",
                ),
                DesiredStateSecretAttachment(
                    secret_id="sec-2",
                    secret_name="slack-bot-token",
                    secret_version="4",
                    env_name="MIM_SECRET_SLACK_BOT_TOKEN",
                ),
            ),
            (
                DesiredStateSecretAttachment(
                    secret_id="sec-1",
                    secret_name="projects/p/secrets/x",
                    secret_version="1",
                    env_name="MIM_SECRET_PROJECTS_P_SECRETS_X",
                ),
            ),
            (
                DesiredStateSecretAttachment(
                    secret_id="sec-1",
                    secret_name="slack-bot-token",
                    secret_version="latest",
                    env_name="MIM_SECRET_SLACK_BOT_TOKEN",
                ),
            ),
            (
                DesiredStateSecretAttachment(
                    secret_id="sk-secret",
                    secret_name="slack-bot-token",
                    secret_version="1",
                    env_name="MIM_SECRET_SLACK_BOT_TOKEN",
                ),
            ),
            (
                DesiredStateSecretAttachment(
                    secret_id="sec-1",
                    secret_name="slack-bot-token",
                    secret_version="1",
                    env_name="PLAIN_SECRET_NAME",
                ),
            ),
        )
        for bad_attachments in bad_cases:
            with self.subTest(bad_attachments=bad_attachments):
                with self.assertRaises(DesiredStateDenied) as raised:
                    render(
                        repo_fixture="nextjs",
                        kind=WorkloadKind.NEXTJS,
                        secret_attachments=bad_attachments,
                    )
                message = str(raised.exception)
                self.assertNotIn("projects/p/secrets/x", message)
                self.assertNotIn("latest", message)
                self.assertNotIn("sk-secret", message)

    def test_snapshot_requires_exact_dict_copy_and_denies_mapping_bypass(self) -> None:
        class LenBypassSnapshot(dict[str, bytes]):
            def __len__(self) -> int:
                return 0

        malicious = LenBypassSnapshot(
            {
                f"file-{index}.txt": b"x"
                for index in range(classifier_module.MAX_SNAPSHOT_FILES + 1)
            }
        )

        with self.assertRaises(DesiredStateDenied):
            render_signed_desired_state(
                workload=workload(kind=WorkloadKind.NEXTJS),
                admission=admission(),
                snapshot=malicious,
                image_digest="a" * 64,
                context=context(),
                issued_at=NOW,
                signing_key=KEY,
            )

    def test_signing_key_is_never_exposed_and_api_is_separate_from_settings(
        self,
    ) -> None:
        envelope = render(repo_fixture="nextjs", kind=WorkloadKind.NEXTJS)
        rendered = repr(envelope)
        self.assertNotIn(KEY.decode("ascii"), rendered)
        self.assertNotIn(KEY.decode("ascii"), envelope.signature)
        self.assertNotIn(
            KEY.decode("ascii"),
            canonical_unsigned_desired_state_bytes(envelope).decode("utf-8"),
        )

        with self.assertRaises(DesiredStateDenied) as raised:
            verify_signed_desired_state(
                envelope,
                context=context(),
                signing_key=b"short-key",
                now=NOW + timedelta(minutes=1),
            )
        self.assertNotIn("short-key", str(raised.exception))

        signature = inspect.signature(render_signed_desired_state)
        self.assertNotIn("settings", signature.parameters)
        self.assertNotIn("origin_keys", signature.parameters)

    def test_classification_and_template_are_internal_only(self) -> None:
        signature = inspect.signature(render_signed_desired_state)
        self.assertNotIn("classification", signature.parameters)
        self.assertNotIn("build_template", signature.parameters)

        with self.assertRaises(TypeError):
            render_signed_desired_state(
                workload=workload(kind=WorkloadKind.NEXTJS),
                admission=admission(),
                snapshot=load_fixture_snapshot("nextjs"),
                image_digest="a" * 64,
                context=context(),
                issued_at=NOW,
                signing_key=KEY,
                classification="nextjs",  # type: ignore[call-arg]
            )
        with self.assertRaises(TypeError):
            render_signed_desired_state(
                workload=workload(kind=WorkloadKind.NEXTJS),
                admission=admission(),
                snapshot=load_fixture_snapshot("nextjs"),
                image_digest="a" * 64,
                context=context(),
                issued_at=NOW,
                signing_key=KEY,
                build_template="node20",  # type: ignore[call-arg]
            )


if __name__ == "__main__":
    unittest.main()
