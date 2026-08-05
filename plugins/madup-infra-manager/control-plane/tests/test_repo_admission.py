from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_repository_admission = importlib.import_module(
    "mim_control_plane.services.repository_admission"
)
_config = importlib.import_module("mim_control_plane.config")
RepositoryAdmissionError = _repository_admission.RepositoryAdmissionError
RepositoryCandidate = _repository_admission.RepositoryCandidate
SelectedRepositoryPolicy = _repository_admission.SelectedRepositoryPolicy
admit_repository = _repository_admission.admit_repository
GITHUB_OWNER = _config.GITHUB_OWNER


GOOD_SHA = "a" * 40


class RepositoryAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = SelectedRepositoryPolicy(
            allowed_repository_ids=frozenset({101, 202}),
            installation_id=303,
        )

    def candidate(self, **overrides: object) -> RepositoryCandidate:
        payload: dict[str, object] = {
            "repository_numeric_id": 101,
            "owner": "madupmarketing",
            "name": "sample-app",
            "installation_id": 303,
            "requested_ref": GOOD_SHA,
            "resolved_sha": GOOD_SHA,
            "is_fork": False,
            "redirected_from": None,
        }
        payload.update(overrides)
        return RepositoryCandidate(**payload)

    def test_accepts_selected_repository_only_when_every_boundary_matches(self) -> None:
        admitted = admit_repository(self.policy, self.candidate())

        self.assertEqual(admitted.repository_numeric_id, 101)
        self.assertEqual(admitted.owner, "madupmarketing")
        self.assertEqual(admitted.name, "sample-app")
        self.assertEqual(admitted.sha, GOOD_SHA)
        self.assertEqual(admitted.full_name, "madupmarketing/sample-app")

    def test_rejects_owner_fork_redirect_unselected_mutable_and_invalid_identifiers(
        self,
    ) -> None:
        cases = (
            {"owner": "otherowner"},
            {"owner": "madup-dct", "name": "claude-plugins"},
            {"is_fork": True},
            {"is_fork": "true"},
            {"redirected_from": "madupmarketing/renamed-app"},
            {"redirected_from": 7},
            {"repository_numeric_id": 999},
            {"installation_id": 404},
            {"requested_ref": "main"},
            {"requested_ref": GOOD_SHA.upper()},
            {"requested_ref": GOOD_SHA, "resolved_sha": "b" * 40},
            {"requested_ref": "0" * 40},
            {"resolved_sha": "0" * 40},
            {"repository_numeric_id": 0},
            {"repository_numeric_id": False},
            {"installation_id": -1},
            {"installation_id": True},
            {"owner": ""},
            {"name": ""},
            {"requested_ref": "not-a-sha"},
            {"resolved_sha": "not-a-sha"},
        )

        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RepositoryAdmissionError):
                    admit_repository(self.policy, self.candidate(**overrides))

    def test_rejects_platform_repository_even_with_selected_numeric_id(self) -> None:
        platform_policy = SelectedRepositoryPolicy(
            allowed_repository_ids=frozenset({101}),
            installation_id=303,
        )
        platform_candidate = self.candidate(
            owner="madup-dct",
            name="claude-plugins",
            repository_numeric_id=101,
        )

        with self.assertRaises(RepositoryAdmissionError):
            admit_repository(platform_policy, platform_candidate)

    def test_policy_requires_frozenset_positive_ids_and_fixed_owner(self) -> None:
        self.assertFalse(SelectedRepositoryPolicy.__dataclass_fields__["owner"].init)
        with self.assertRaises(RepositoryAdmissionError):
            SelectedRepositoryPolicy(
                allowed_repository_ids={101},
                installation_id=303,
            )
        with self.assertRaises(RepositoryAdmissionError):
            SelectedRepositoryPolicy(
                allowed_repository_ids=frozenset({False}),
                installation_id=303,
            )
        with self.assertRaises(RepositoryAdmissionError):
            SelectedRepositoryPolicy(
                allowed_repository_ids=frozenset({101}),
                installation_id=True,
            )
        with self.assertRaises(TypeError):
            SelectedRepositoryPolicy(  # type: ignore[call-arg]
                allowed_repository_ids=frozenset({101}),
                installation_id=303,
                owner=GITHUB_OWNER,
            )


if __name__ == "__main__":
    unittest.main()
