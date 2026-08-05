from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
FIXTURE_ROOT = TEST_ROOT / "fixtures" / "repos"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_build_template = __import__(
    "mim_control_plane.services.build_template",
    fromlist=["BuildTemplate", "build_template_for"],
)
_classifier = __import__(
    "mim_control_plane.services.classifier",
    fromlist=[
        "ClassificationQuestion",
        "ManifestValidationError",
        "SnapshotValidationError",
        "WorkloadClassification",
        "classify_snapshot",
    ],
)
from mim_control_plane.domain.states import WorkloadKind  # noqa: E402

BuildTemplate = _build_template.BuildTemplate
build_template_for = _build_template.build_template_for
ClassificationQuestion = _classifier.ClassificationQuestion
ManifestValidationError = _classifier.ManifestValidationError
SnapshotValidationError = _classifier.SnapshotValidationError
WorkloadClassification = _classifier.WorkloadClassification
classify_snapshot = _classifier.classify_snapshot


def load_fixture_snapshot(name: str) -> dict[str, bytes]:
    fixture_dir = FIXTURE_ROOT / name
    snapshot: dict[str, bytes] = {}
    for path in sorted(fixture_dir.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(fixture_dir).as_posix()] = path.read_bytes()
    return snapshot


class WorkloadAdmissionTests(unittest.TestCase):
    def test_positive_fixtures_classify_only_the_supported_workloads(self) -> None:
        cases = (
            ("streamlit", WorkloadKind.STREAMLIT, "app.py", None),
            ("nextjs", WorkloadKind.NEXTJS, "app/page.tsx", None),
            ("scheduled_script", WorkloadKind.SCHEDULED_SCRIPT, "main.py", "0 * * * *"),
        )

        for fixture_name, expected_kind, expected_entrypoint, expected_cron in cases:
            with self.subTest(fixture=fixture_name):
                result = classify_snapshot(load_fixture_snapshot(fixture_name))
                self.assertIsInstance(result, WorkloadClassification)
                self.assertEqual(result.kind, expected_kind)
                self.assertEqual(result.entrypoint, expected_entrypoint)
                self.assertEqual(result.schedule_cron, expected_cron)

    def test_conflicting_or_ambiguous_evidence_returns_a_typed_question(self) -> None:
        ambiguous = load_fixture_snapshot("streamlit")
        ambiguous.update(load_fixture_snapshot("nextjs"))
        result = classify_snapshot(ambiguous)

        self.assertIsInstance(result, ClassificationQuestion)
        self.assertEqual(
            result.choices,
            (WorkloadKind.NEXTJS, WorkloadKind.STREAMLIT),
        )
        self.assertIn("disambiguat", result.prompt.lower())

        conflict = load_fixture_snapshot("streamlit")
        conflict["mim.yaml"] = b"kind: nextjs\n"
        result = classify_snapshot(conflict)
        self.assertIsInstance(result, ClassificationQuestion)
        self.assertEqual(
            result.choices,
            (WorkloadKind.NEXTJS, WorkloadKind.STREAMLIT),
        )

        declared_only = {"mim.yaml": b"kind: nextjs\n"}
        result = classify_snapshot(declared_only)
        self.assertIsInstance(result, ClassificationQuestion)
        self.assertEqual(
            result.choices,
            (
                WorkloadKind.NEXTJS,
                WorkloadKind.SCHEDULED_SCRIPT,
                WorkloadKind.STREAMLIT,
            ),
        )

        declared_web_with_scheduled = load_fixture_snapshot("scheduled_script")
        declared_web_with_scheduled["mim.yaml"] = b"kind: nextjs\n"
        result = classify_snapshot(declared_web_with_scheduled)
        self.assertIsInstance(result, ClassificationQuestion)
        self.assertEqual(
            result.choices,
            (WorkloadKind.NEXTJS, WorkloadKind.SCHEDULED_SCRIPT),
        )

        scheduled_declared_on_streamlit = load_fixture_snapshot("streamlit")
        scheduled_declared_on_streamlit["main.py"] = b"print('scheduled')\n"
        scheduled_declared_on_streamlit["mim.yaml"] = (
            b"kind: scheduled_script\nentrypoint: main.py\nschedule: hourly\n"
        )
        result = classify_snapshot(scheduled_declared_on_streamlit)
        self.assertIsInstance(result, ClassificationQuestion)
        self.assertEqual(
            result.choices,
            (WorkloadKind.SCHEDULED_SCRIPT, WorkloadKind.STREAMLIT),
        )

        scheduled_declared_on_next = load_fixture_snapshot("nextjs")
        scheduled_declared_on_next["main.py"] = b"print('scheduled')\n"
        scheduled_declared_on_next["mim.yaml"] = (
            b"kind: scheduled_script\nentrypoint: main.py\nschedule: hourly\n"
        )
        result = classify_snapshot(scheduled_declared_on_next)
        self.assertIsInstance(result, ClassificationQuestion)
        self.assertEqual(
            result.choices,
            (WorkloadKind.NEXTJS, WorkloadKind.SCHEDULED_SCRIPT),
        )

    def test_path_traversal_and_symlink_like_snapshot_keys_are_rejected(self) -> None:
        cases = (
            {"../evil.py": b"print('x')"},
            {"/absolute.py": b"print('x')"},
            {"nested\\evil.py": b"print('x')"},
            {"./relative.py": b"print('x')"},
            {"dir//file.py": b"print('x')"},
            {"-leading/file.py": b"print('x')"},
            {".hidden/file.py": b"print('x')"},
            {"control/\x01.py": b"print('x')"},
            {"a" * (_classifier.MAX_PATH_LENGTH + 1): b"x"},
        )
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(SnapshotValidationError):
                    classify_snapshot(snapshot)

    def test_snapshot_and_manifest_size_bounds_fail_closed(self) -> None:
        too_many_files = {
            f"file-{index}.txt": b"x"
            for index in range(_classifier.MAX_SNAPSHOT_FILES + 1)
        }
        with self.assertRaises(SnapshotValidationError):
            classify_snapshot(too_many_files)

        too_large_file = {"app.py": b"x" * (_classifier.MAX_SNAPSHOT_FILE_BYTES + 1)}
        with self.assertRaises(SnapshotValidationError):
            classify_snapshot(too_large_file)

        total_limit = _classifier.MAX_SNAPSHOT_TOTAL_BYTES // 2 + 1
        total_too_large = {
            "a.py": b"x" * total_limit,
            "b.py": b"x" * total_limit,
        }
        with self.assertRaises(SnapshotValidationError):
            classify_snapshot(total_too_large)

        manifest_too_large = {
            "mim.yaml": b"k" * (_classifier.MAX_MANIFEST_BYTES + 1),
            "main.py": b"print('x')\n",
        }
        with self.assertRaises(ManifestValidationError):
            classify_snapshot(manifest_too_large)

        deep_manifest = "kind: scheduled_script\nentrypoint: main.py\nschedule:\n"
        for _ in range(_classifier.MAX_MANIFEST_DEPTH + 1):
            deep_manifest += "  nested:\n"
        deep_manifest += "    value: hourly\n"
        with self.assertRaises(ManifestValidationError):
            classify_snapshot(
                {
                    "main.py": b"print('x')\n",
                    "mim.yaml": deep_manifest.encode("utf-8"),
                }
            )

    def test_malicious_manifest_shapes_are_rejected(self) -> None:
        bad_manifests = (
            b"kind: scheduled_script\nschedule: hourly\nextra: no\n",
            b"kind: scheduled_script\nservice_account: injected\n",
            b"kind: scheduled_script\nschedule: '*/30 * * * *'\n",
            b"kind: scheduled_script\nimage: ghcr.io/evil\n",
            b"kind: scheduled_script\ncpu: 2\n",
            b"kind: scheduled_script\nmemory: 1Gi\n",
            b"kind: scheduled_script\nproject: other\n",
            b"kind: scheduled_script\n---\nkind: nextjs\n",
            b"&anchor {kind: scheduled_script}\n",
            b"!Custom kind: scheduled_script\n",
            b"? 3\n: bad\n",
            b"\xff\xfe\x00",
        )
        for raw_manifest in bad_manifests:
            with self.subTest(raw_manifest=raw_manifest):
                snapshot = load_fixture_snapshot("scheduled_script")
                snapshot["mim.yaml"] = raw_manifest
                with self.assertRaises(ManifestValidationError):
                    classify_snapshot(snapshot)

    def test_scheduled_manifest_entrypoint_must_be_safe_python_file_present_in_snapshot(
        self,
    ) -> None:
        bad_snapshots = (
            b"kind: scheduled_script\nentrypoint: missing.py\nschedule: hourly\n",
            b"kind: scheduled_script\nentrypoint: ../main.py\nschedule: hourly\n",
            b"kind: scheduled_script\nentrypoint: task.sh\nschedule: hourly\n",
            b"kind: scheduled_script\nentrypoint: nested/task.py\nschedule: hourly\n",
            (
                b"kind: scheduled_script\nentrypoint: "
                + b"a" * (_classifier.MAX_ENTRYPOINT_LENGTH + 1)
                + b".py\nschedule: hourly\n"
            ),
        )
        for raw_manifest in bad_snapshots:
            with self.subTest(raw_manifest=raw_manifest):
                snapshot = load_fixture_snapshot("scheduled_script")
                snapshot["mim.yaml"] = raw_manifest
                with self.assertRaises(ManifestValidationError):
                    classify_snapshot(snapshot)

    def test_workflow_and_build_files_are_ignored_for_classification(self) -> None:
        snapshot = load_fixture_snapshot("streamlit")
        snapshot["Dockerfile"] = b"FROM python:3.13"
        snapshot["cloudbuild.yaml"] = b"steps: []"
        snapshot[".github/workflows/deploy.yml"] = b"name: deploy"
        snapshot["infra/main.tf"] = b"terraform {}"

        result = classify_snapshot(snapshot)
        self.assertIsInstance(result, WorkloadClassification)
        self.assertEqual(result.kind, WorkloadKind.STREAMLIT)

    def test_streamlit_detection_requires_exact_dependency_and_real_import(
        self,
    ) -> None:
        positives = (
            b"streamlit==1.38.0\n",
            b"streamlit[auth]>=1.38 ; python_version >= '3.13'\n",
        )
        for requirements in positives:
            with self.subTest(requirements=requirements):
                snapshot = {
                    "requirements.txt": requirements,
                    "app.py": b"from streamlit import runtime\n",
                }
                result = classify_snapshot(snapshot)
                self.assertIsInstance(result, WorkloadClassification)
                self.assertEqual(result.kind, WorkloadKind.STREAMLIT)

        negatives = (
            (
                b"# streamlit==1.38.0\n",
                b"import streamlit\n",
            ),
            (
                b"mystreamlithelper==1.0.0\n",
                b"import streamlit\n",
            ),
            (
                b"streamlit==1.38.0\n",
                b"print('no import here')\n",
            ),
        )
        for requirements, app_bytes in negatives:
            with self.subTest(requirements=requirements, app_bytes=app_bytes):
                snapshot = {
                    "requirements.txt": requirements,
                    "app.py": app_bytes,
                }
                result = classify_snapshot(snapshot)
                self.assertIsInstance(result, ClassificationQuestion)

    def test_trusted_templates_are_deterministic_immutable_and_authority_free(
        self,
    ) -> None:
        classified = classify_snapshot(load_fixture_snapshot("nextjs"))
        self.assertIsInstance(classified, WorkloadClassification)

        first = build_template_for(classified)
        second = build_template_for(copy.deepcopy(classified))
        self.assertEqual(first, second)
        self.assertIsInstance(first, BuildTemplate)

        with self.assertRaises(AttributeError):
            first.runtime = "mutated"

        rendered = repr(first)
        self.assertIn("npm", rendered)
        self.assertNotIn("service_account", rendered)
        self.assertNotIn("project", rendered)
        self.assertNotIn("vpc", rendered)
        self.assertNotIn("terraform", rendered)
        self.assertNotIn("cloudbuild", rendered)
        self.assertEqual(
            first.install_command,
            ("npm", "ci", "--ignore-scripts"),
        )
        self.assertEqual(
            first.build_command,
            ("./node_modules/.bin/next", "build"),
        )
        self.assertEqual(
            first.launch_command,
            (
                "./node_modules/.bin/next",
                "start",
                "--hostname",
                "0.0.0.0",
                "--port",
                "8080",
            ),
        )
        self.assertNotIn("npm run", rendered)
        self.assertNotIn("npx", rendered)

        scheduled = WorkloadClassification(
            kind=WorkloadKind.SCHEDULED_SCRIPT,
            entrypoint="jobs/run_hourly.py",
            schedule_cron="0 * * * *",
        )
        scheduled_template = build_template_for(scheduled)
        self.assertEqual(
            scheduled_template.required_files,
            ("jobs/run_hourly.py", "mim.yaml"),
        )

    def test_workload_classification_rejects_forged_entrypoints_and_cron_combinations(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            WorkloadClassification(
                kind=WorkloadKind.NEXTJS,
                entrypoint="server.py",
            )
        with self.assertRaises(ValueError):
            WorkloadClassification(
                kind=WorkloadKind.SCHEDULED_SCRIPT,
                entrypoint="main.py",
                schedule_cron=None,
            )
        with self.assertRaises(ValueError):
            WorkloadClassification(
                kind=WorkloadKind.STREAMLIT,
                entrypoint="app.py",
                schedule_cron="0 * * * *",
            )


if __name__ == "__main__":
    unittest.main()
