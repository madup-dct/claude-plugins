from __future__ import annotations

import hashlib
import hmac
import importlib
import io
import json
import stat
import sys
import unittest
import warnings
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


WEBHOOK_SECRET = b"mim-webhook-secret-is-at-least-32-bytes"
WEBHOOK_BODY = b'{"repository":{"id":101}}'
NOW = datetime(2026, 8, 4, 2, 0, 0, tzinfo=UTC)
DELIVERY_ID = "01234567-89ab-cdef-0123-456789abcdef"


def signature_for(body: bytes) -> str:
    digest = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def push_body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "after": "a" * 40,
        "deleted": False,
        "head_commit": {"id": "a" * 40},
        "installation": {"id": 303},
        "ref": "refs/heads/main",
        "repository": {
            "fork": False,
            "full_name": "madupmarketing/sample-app",
            "id": 101,
            "name": "sample-app",
            "owner": {"login": "madupmarketing"},
        },
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def source_archive(
    entries: dict[str, bytes],
    *,
    symlink: str | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in entries.items():
                archive.writestr(path, content)
            if symlink is not None:
                info = zipfile.ZipInfo(symlink)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"app.py")
    return buffer.getvalue()


class GitHubWebhookSignatureTests(unittest.TestCase):
    def test_accepts_exact_sha256_signature_and_rejects_tampering(self) -> None:
        try:
            github = importlib.import_module("mim_control_plane.adapters.github")
        except ModuleNotFoundError:
            self.fail("GitHub adapter is missing")

        github.verify_github_webhook_signature(
            body=WEBHOOK_BODY,
            signature_header=signature_for(WEBHOOK_BODY),
            webhook_secret=WEBHOOK_SECRET,
        )

        with self.assertRaises(github.GitHubWebhookError):
            github.verify_github_webhook_signature(
                body=WEBHOOK_BODY + b" ",
                signature_header=signature_for(WEBHOOK_BODY),
                webhook_secret=WEBHOOK_SECRET,
            )


class GitHubPushVerificationTests(unittest.TestCase):
    def policy(self) -> object:
        repository_admission = importlib.import_module(
            "mim_control_plane.services.repository_admission"
        )
        return repository_admission.SelectedRepositoryPolicy(
            allowed_repository_ids=frozenset({101, 202}),
            installation_id=303,
        )

    def test_returns_only_an_exact_selected_default_branch_push(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        body = push_body()

        verified = github.verify_github_push(
            body=body,
            signature_header=signature_for(body),
            webhook_secret=WEBHOOK_SECRET,
            event_name="push",
            delivery_id=DELIVERY_ID,
            allowed_ref="refs/heads/main",
            policy=self.policy(),
        )

        self.assertEqual(verified.delivery_id, DELIVERY_ID)
        self.assertEqual(verified.repository_numeric_id, 101)
        self.assertEqual(verified.owner, "madupmarketing")
        self.assertEqual(verified.name, "sample-app")
        self.assertEqual(verified.installation_id, 303)
        self.assertEqual(verified.ref, "refs/heads/main")
        self.assertEqual(verified.sha, "a" * 40)

    def test_rejects_non_push_unselected_or_mutable_delivery_material(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        repository = {
            "fork": False,
            "full_name": "madupmarketing/sample-app",
            "id": 101,
            "name": "sample-app",
            "owner": {"login": "madupmarketing"},
        }
        cases = (
            (push_body(), "pull_request", "refs/heads/main"),
            (push_body(ref="refs/heads/feature"), "push", "refs/heads/main"),
            (
                push_body(repository={**repository, "id": 999}),
                "push",
                "refs/heads/main",
            ),
            (
                push_body(installation={"id": 404}),
                "push",
                "refs/heads/main",
            ),
            (
                push_body(
                    repository={
                        **repository,
                        "owner": {"login": "otherowner"},
                        "full_name": "otherowner/sample-app",
                    }
                ),
                "push",
                "refs/heads/main",
            ),
            (
                push_body(repository={**repository, "fork": True}),
                "push",
                "refs/heads/main",
            ),
            (push_body(deleted=True), "push", "refs/heads/main"),
            (
                push_body(head_commit={"id": "b" * 40}),
                "push",
                "refs/heads/main",
            ),
            (
                push_body(after="A" * 40, head_commit={"id": "A" * 40}),
                "push",
                "refs/heads/main",
            ),
            (
                push_body(after="0" * 40, head_commit={"id": "0" * 40}),
                "push",
                "refs/heads/main",
            ),
        )

        for body, event_name, allowed_ref in cases:
            with self.subTest(body=body, event_name=event_name):
                with self.assertRaises(github.GitHubWebhookError):
                    github.verify_github_push(
                        body=body,
                        signature_header=signature_for(body),
                        webhook_secret=WEBHOOK_SECRET,
                        event_name=event_name,
                        delivery_id=DELIVERY_ID,
                        allowed_ref=allowed_ref,
                        policy=self.policy(),
                    )

    def test_authenticates_raw_bytes_before_parsing_json(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        invalid_body = b"not-json"

        with mock.patch.object(github.json, "loads") as loads:
            with self.assertRaises(github.GitHubWebhookError):
                github.verify_github_push(
                    body=invalid_body,
                    signature_header=signature_for(b"different"),
                    webhook_secret=WEBHOOK_SECRET,
                    event_name="push",
                    delivery_id=DELIVERY_ID,
                    allowed_ref="refs/heads/main",
                    policy=self.policy(),
                )

        loads.assert_not_called()

    def test_rejects_duplicate_json_keys(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        body = b'{"after":"' + b"a" * 40 + b'","after":"' + b"b" * 40 + b'"}'

        with self.assertRaises(github.GitHubWebhookError):
            github.verify_github_push(
                body=body,
                signature_header=signature_for(body),
                webhook_secret=WEBHOOK_SECRET,
                event_name="push",
                delivery_id=DELIVERY_ID,
                allowed_ref="refs/heads/main",
                policy=self.policy(),
            )

    def test_rejects_delivery_ids_that_cannot_be_used_for_replay_dedup(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        body = push_body()

        for delivery_id in ("", "not-a-guid", "../same", DELIVERY_ID + "00"):
            with self.subTest(delivery_id=delivery_id):
                with self.assertRaises(github.GitHubWebhookError):
                    github.verify_github_push(
                        body=body,
                        signature_header=signature_for(body),
                        webhook_secret=WEBHOOK_SECRET,
                        event_name="push",
                        delivery_id=delivery_id,
                        allowed_ref="refs/heads/main",
                        policy=self.policy(),
                    )

    def test_rejects_oversized_body_before_hmac_work(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        oversized = b"x" * (github.MAX_WEBHOOK_BODY_BYTES + 1)

        with self.assertRaises(github.GitHubWebhookError):
            github.verify_github_webhook_signature(
                body=oversized,
                signature_header=signature_for(oversized),
                webhook_secret=WEBHOOK_SECRET,
            )


class StaticAppJwtProvider:
    def __init__(self, token: str = "header.payload.signature") -> None:
        self.token = token
        self.calls: tuple[datetime, ...] = ()

    def get_app_jwt(self, *, now: datetime) -> str:
        self.calls = self.calls + (now,)
        return self.token


class GitHubInstallationTokenProviderTests(unittest.TestCase):
    def policy(self) -> object:
        repository_admission = importlib.import_module(
            "mim_control_plane.services.repository_admission"
        )
        return repository_admission.SelectedRepositoryPolicy(
            allowed_repository_ids=frozenset({202, 101}),
            installation_id=303,
        )

    def test_mints_central_token_for_only_selected_repositories_and_read_scope(
        self,
    ) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        app_jwt_provider = StaticAppJwtProvider()

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                str(request.url),
                "https://api.github.com/app/installations/303/access_tokens",
            )
            self.assertEqual(
                request.headers["authorization"],
                "Bearer header.payload.signature",
            )
            self.assertEqual(
                json.loads(request.content),
                {
                    "permissions": {"contents": "read"},
                    "repository_ids": [101, 202],
                },
            )
            return httpx.Response(
                201,
                json={
                    "expires_at": (NOW + timedelta(minutes=59)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "permissions": {"contents": "read", "metadata": "read"},
                    "repositories": [{"id": 101}, {"id": 202}],
                    "repository_selection": "selected",
                    "token": "ghs_central-installation-token-value",
                },
            )

        provider = github.GitHubAppInstallationTokenProvider(
            policy=self.policy(),
            app_jwt_provider=app_jwt_provider,
            transport=httpx.MockTransport(handler),
        )

        token = provider.get_token(installation_id=303, now=NOW)

        self.assertEqual(token, "ghs_central-installation-token-value")
        self.assertEqual(app_jwt_provider.calls, (NOW,))
        self.assertNotIn(token, repr(provider))

    def test_rejects_wrong_installation_without_minting_or_calling_github(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        app_jwt_provider = StaticAppJwtProvider()
        transport = httpx.MockTransport(
            lambda request: self.fail(f"unexpected request: {request.url}")
        )
        provider = github.GitHubAppInstallationTokenProvider(
            policy=self.policy(),
            app_jwt_provider=app_jwt_provider,
            transport=transport,
        )

        with self.assertRaises(github.GitHubSourceError):
            provider.get_token(installation_id=404, now=NOW)

        self.assertEqual(app_jwt_provider.calls, ())

    def test_rejects_broader_repository_or_permission_token_responses(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        cases = (
            {
                "expires_at": (NOW + timedelta(minutes=59)).isoformat(),
                "permissions": {"contents": "write", "metadata": "read"},
                "repositories": [{"id": 101}, {"id": 202}],
                "repository_selection": "selected",
                "token": "ghs_central-installation-token-value",
            },
            {
                "expires_at": (NOW + timedelta(minutes=59)).isoformat(),
                "permissions": {"contents": "read", "metadata": "read"},
                "repositories": [{"id": 101}, {"id": 202}, {"id": 999}],
                "repository_selection": "selected",
                "token": "ghs_central-installation-token-value",
            },
            {
                "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
                "permissions": {"contents": "read", "metadata": "read"},
                "repositories": [{"id": 101}, {"id": 202}],
                "repository_selection": "selected",
                "token": "ghs_central-installation-token-value",
            },
        )

        for payload in cases:
            with self.subTest(payload=payload):
                provider = github.GitHubAppInstallationTokenProvider(
                    policy=self.policy(),
                    app_jwt_provider=StaticAppJwtProvider(),
                    transport=httpx.MockTransport(
                        lambda request, response=payload: httpx.Response(
                            201,
                            json=response,
                        )
                    ),
                )
                with self.assertRaises(github.GitHubSourceError):
                    provider.get_token(installation_id=303, now=NOW)


class StaticInstallationTokenProvider:
    def __init__(self, token: str = "ghs_central-installation-token-value") -> None:
        self.token = token
        self.calls: tuple[tuple[int, datetime], ...] = ()

    def get_token(self, *, installation_id: int, now: datetime) -> str:
        self.calls = self.calls + ((installation_id, now),)
        return self.token


class CountingByteStream(httpx.SyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.yield_count = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            self.yield_count += 1
            yield chunk


class GitHubSourceAdapterTests(unittest.TestCase):
    def policy(self) -> object:
        repository_admission = importlib.import_module(
            "mim_control_plane.services.repository_admission"
        )
        return repository_admission.SelectedRepositoryPolicy(
            allowed_repository_ids=frozenset({101}),
            installation_id=303,
        )

    def admission(self, **overrides: object) -> object:
        models = importlib.import_module("mim_control_plane.domain.models")
        states = importlib.import_module("mim_control_plane.domain.states")
        payload: dict[str, object] = {
            "id": models.RepositoryAdmissionId("repo-101"),
            "repository_numeric_id": 101,
            "owner": "madupmarketing",
            "name": "sample-app",
            "installation_id": 303,
            "state": states.RepositoryAdmissionState.ADMITTED,
            "admitted_sha": "a" * 40,
            "created_at": NOW,
            "updated_at": NOW,
        }
        payload.update(overrides)
        return models.RepositoryAdmission(**payload)

    def test_fetches_exact_commit_archive_without_forwarding_installation_token(
        self,
    ) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        source_port = importlib.import_module("mim_control_plane.ports.source")
        self.assertTrue(
            issubclass(
                github.GitHubSourceAdapter,
                source_port.SourceSnapshotPort,
            )
        )
        token_provider = StaticInstallationTokenProvider()
        seen_paths: list[str] = []
        archive_bytes = source_archive(
            {
                "sample-app-a/app.py": b"import streamlit\n",
                "sample-app-a/requirements.txt": b"streamlit==1.40.0\n",
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            if request.url.host == "api.github.com":
                self.assertEqual(
                    request.headers["authorization"],
                    "Bearer ghs_central-installation-token-value",
                )
                if request.url.path == "/repos/madupmarketing/sample-app":
                    return httpx.Response(
                        200,
                        json={
                            "fork": False,
                            "full_name": "madupmarketing/sample-app",
                            "id": 101,
                            "name": "sample-app",
                            "owner": {"login": "madupmarketing"},
                        },
                    )
                if request.url.path == (
                    "/repos/madupmarketing/sample-app/commits/" + "a" * 40
                ):
                    return httpx.Response(200, json={"sha": "a" * 40})
                if request.url.path == (
                    "/repos/madupmarketing/sample-app/zipball/" + "a" * 40
                ):
                    return httpx.Response(
                        302,
                        headers={
                            "Location": (
                                "https://codeload.github.com/madupmarketing/"
                                "sample-app/legacy.zip/" + "a" * 40
                            )
                        },
                    )
            if request.url.host == "codeload.github.com":
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(
                    200,
                    content=archive_bytes,
                    headers={"Content-Type": "application/zip"},
                )
            return httpx.Response(404)

        adapter = github.GitHubSourceAdapter(
            policy=self.policy(),
            token_provider=token_provider,
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )

        snapshot = adapter.fetch_snapshot(self.admission())

        self.assertEqual(
            snapshot,
            {
                "app.py": b"import streamlit\n",
                "requirements.txt": b"streamlit==1.40.0\n",
            },
        )
        self.assertEqual(token_provider.calls, ((303, NOW),))
        with self.assertRaises(TypeError):
            snapshot["attacker.py"] = b"changed"  # type: ignore[index]
        self.assertEqual(
            seen_paths,
            [
                "/repos/madupmarketing/sample-app",
                "/repos/madupmarketing/sample-app/commits/" + "a" * 40,
                "/repos/madupmarketing/sample-app/zipball/" + "a" * 40,
                "/madupmarketing/sample-app/legacy.zip/" + "a" * 40,
            ],
        )

    def test_rejects_untrusted_admission_before_requesting_a_token(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        states = importlib.import_module("mim_control_plane.domain.states")
        cases = (
            {"repository_numeric_id": 999},
            {"owner": "otherowner"},
            {"installation_id": 404},
            {"admitted_sha": "main"},
            {"admitted_sha": "0" * 40},
            {"state": states.RepositoryAdmissionState.REVOKED},
        )

        for overrides in cases:
            with self.subTest(overrides=overrides):
                token_provider = StaticInstallationTokenProvider()
                adapter = github.GitHubSourceAdapter(
                    policy=self.policy(),
                    token_provider=token_provider,
                    transport=httpx.MockTransport(
                        lambda request: self.fail(f"unexpected: {request.url}")
                    ),
                    clock=lambda: NOW,
                )
                with self.assertRaises(github.GitHubSourceError):
                    adapter.fetch_snapshot(self.admission(**overrides))
                self.assertEqual(token_provider.calls, ())

    def test_revalidates_repository_and_commit_before_requesting_archive(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        base_repository = {
            "fork": False,
            "full_name": "madupmarketing/sample-app",
            "id": 101,
            "name": "sample-app",
            "owner": {"login": "madupmarketing"},
        }
        cases = (
            ({**base_repository, "id": 999}, "a" * 40),
            ({**base_repository, "fork": True}, "a" * 40),
            (
                {
                    **base_repository,
                    "full_name": "otherowner/sample-app",
                    "owner": {"login": "otherowner"},
                },
                "a" * 40,
            ),
            (base_repository, "b" * 40),
        )

        for repository_payload, resolved_sha in cases:
            with self.subTest(
                repository_payload=repository_payload,
                resolved_sha=resolved_sha,
            ):
                seen: list[str] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    seen.append(request.url.path)
                    if request.url.path == "/repos/madupmarketing/sample-app":
                        return httpx.Response(200, json=repository_payload)
                    if "/commits/" in request.url.path:
                        return httpx.Response(200, json={"sha": resolved_sha})
                    return self.fail(
                        f"archive requested before validation: {request.url}"
                    )

                adapter = github.GitHubSourceAdapter(
                    policy=self.policy(),
                    token_provider=StaticInstallationTokenProvider(),
                    transport=httpx.MockTransport(handler),
                    clock=lambda: NOW,
                )
                with self.assertRaises(github.GitHubSourceError):
                    adapter.fetch_snapshot(self.admission())
                self.assertNotIn(
                    "/repos/madupmarketing/sample-app/zipball/" + "a" * 40,
                    seen,
                )

    def test_classifies_repository_and_commit_drift_as_integrity_failures(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        base_repository = {
            "fork": False,
            "full_name": "madupmarketing/sample-app",
            "id": 101,
            "name": "sample-app",
            "owner": {"login": "madupmarketing"},
        }
        cases = (
            ({**base_repository, "id": 999}, "a" * 40),
            (base_repository, "b" * 40),
        )

        for repository_payload, resolved_sha in cases:
            with self.subTest(
                repository_payload=repository_payload,
                resolved_sha=resolved_sha,
            ):
                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == "/repos/madupmarketing/sample-app":
                        return httpx.Response(200, json=repository_payload)
                    if "/commits/" in request.url.path:
                        return httpx.Response(200, json={"sha": resolved_sha})
                    return self.fail(
                        f"archive requested before validation: {request.url}"
                    )

                adapter = github.GitHubSourceAdapter(
                    policy=self.policy(),
                    token_provider=StaticInstallationTokenProvider(),
                    transport=httpx.MockTransport(handler),
                    clock=lambda: NOW,
                )
                with self.assertRaises(github.GitHubSourceIntegrityError):
                    adapter.fetch_snapshot(self.admission())

    def test_allows_only_exact_https_codeload_redirect(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        approved_path = (
            "/madupmarketing/sample-app/legacy.zip/" + "a" * 40
        )
        redirects = (
            "https://evil.example" + approved_path,
            "http://codeload.github.com" + approved_path,
            "https://codeload.github.com:444" + approved_path,
            "https://user@codeload.github.com" + approved_path,
            "https://codeload.github.com/madupmarketing/other/legacy.zip/"
            + "a" * 40,
            "https://codeload.github.com" + approved_path + "?token=leak",
        )

        for redirect in redirects:
            with self.subTest(redirect=redirect):
                seen_hosts: list[str | None] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    seen_hosts.append(request.url.host)
                    if request.url.path == "/repos/madupmarketing/sample-app":
                        return httpx.Response(
                            200,
                            json={
                                "fork": False,
                                "full_name": "madupmarketing/sample-app",
                                "id": 101,
                                "name": "sample-app",
                                "owner": {"login": "madupmarketing"},
                            },
                        )
                    if "/commits/" in request.url.path:
                        return httpx.Response(200, json={"sha": "a" * 40})
                    if "/zipball/" in request.url.path:
                        return httpx.Response(302, headers={"Location": redirect})
                    return self.fail(f"unapproved redirect followed: {request.url}")

                adapter = github.GitHubSourceAdapter(
                    policy=self.policy(),
                    token_provider=StaticInstallationTokenProvider(),
                    transport=httpx.MockTransport(handler),
                    clock=lambda: NOW,
                )
                with self.assertRaises(github.GitHubSourceIntegrityError):
                    adapter.fetch_snapshot(self.admission())
                self.assertEqual(set(seen_hosts), {"api.github.com"})

    def test_rejects_unsafe_or_oversized_archive_entries(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        classifier = importlib.import_module("mim_control_plane.services.classifier")
        unsafe_archives = (
            source_archive({"root/../evil.py": b"x"}),
            source_archive({"root/dir\\evil.py": b"x"}),
            source_archive({"root/./app.py": b"x"}),
            source_archive({"root/app.py": b"x"}, symlink="root/link"),
            source_archive({"root/app.py": b"x", "other/app.py": b"x"}),
            source_archive(
                {
                    f"root/file-{index}.py": b"x"
                    for index in range(classifier.MAX_SNAPSHOT_FILES + 1)
                }
            ),
            source_archive(
                {
                    "root/app.py": b"x"
                    * (classifier.MAX_SNAPSHOT_FILE_BYTES + 1)
                }
            ),
            source_archive(
                {
                    f"root/file-{index}.bin": b"x" * 220_000
                    for index in range(5)
                }
            ),
        )

        for archive_bytes in unsafe_archives:
            with self.subTest(size=len(archive_bytes)):
                with self.assertRaises(ValueError):
                    github._extract_snapshot(archive_bytes)

    def test_streams_archive_with_a_hard_compressed_byte_limit(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        chunk = b"x" * 65_536
        stream = CountingByteStream((chunk,) * 100)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/madupmarketing/sample-app":
                return httpx.Response(
                    200,
                    json={
                        "fork": False,
                        "full_name": "madupmarketing/sample-app",
                        "id": 101,
                        "name": "sample-app",
                        "owner": {"login": "madupmarketing"},
                    },
                )
            if "/commits/" in request.url.path:
                return httpx.Response(200, json={"sha": "a" * 40})
            if "/zipball/" in request.url.path:
                return httpx.Response(
                    302,
                    headers={
                        "Location": (
                            "https://codeload.github.com/madupmarketing/"
                            "sample-app/legacy.zip/" + "a" * 40
                        )
                    },
                )
            return httpx.Response(
                200,
                stream=stream,
                headers={"Content-Type": "application/zip"},
            )

        adapter = github.GitHubSourceAdapter(
            policy=self.policy(),
            token_provider=StaticInstallationTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )

        with self.assertRaises(github.GitHubSourceError):
            adapter.fetch_snapshot(self.admission())

        self.assertLess(stream.yield_count, len(stream.chunks))

    def test_rejects_non_zip_content_type_even_when_body_is_a_zip(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        archive_bytes = source_archive({"root/app.py": b"x"})

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/madupmarketing/sample-app":
                return httpx.Response(
                    200,
                    json={
                        "fork": False,
                        "full_name": "madupmarketing/sample-app",
                        "id": 101,
                        "name": "sample-app",
                        "owner": {"login": "madupmarketing"},
                    },
                )
            if "/commits/" in request.url.path:
                return httpx.Response(200, json={"sha": "a" * 40})
            if "/zipball/" in request.url.path:
                return httpx.Response(
                    302,
                    headers={
                        "Location": (
                            "https://codeload.github.com/madupmarketing/"
                            "sample-app/legacy.zip/" + "a" * 40
                        )
                    },
                )
            return httpx.Response(
                200,
                content=archive_bytes,
                headers={"Content-Type": "text/html"},
            )

        adapter = github.GitHubSourceAdapter(
            policy=self.policy(),
            token_provider=StaticInstallationTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )

        with self.assertRaises(github.GitHubSourceError):
            adapter.fetch_snapshot(self.admission())

    def test_classifies_unsafe_archives_as_integrity_failures(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        archive_bytes = source_archive({"root/../evil.py": b"x"})

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/madupmarketing/sample-app":
                return httpx.Response(
                    200,
                    json={
                        "fork": False,
                        "full_name": "madupmarketing/sample-app",
                        "id": 101,
                        "name": "sample-app",
                        "owner": {"login": "madupmarketing"},
                    },
                )
            if "/commits/" in request.url.path:
                return httpx.Response(200, json={"sha": "a" * 40})
            if "/zipball/" in request.url.path:
                return httpx.Response(
                    302,
                    headers={
                        "Location": (
                            "https://codeload.github.com/madupmarketing/"
                            "sample-app/legacy.zip/" + "a" * 40
                        )
                    },
                )
            return httpx.Response(
                200,
                content=archive_bytes,
                headers={"Content-Type": "application/zip"},
            )

        adapter = github.GitHubSourceAdapter(
            policy=self.policy(),
            token_provider=StaticInstallationTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )

        with self.assertRaises(github.GitHubSourceIntegrityError):
            adapter.fetch_snapshot(self.admission())

    def test_classifies_transport_outage_as_retryable_unavailability(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            raise httpx.ConnectTimeout("timeout")

        adapter = github.GitHubSourceAdapter(
            policy=self.policy(),
            token_provider=StaticInstallationTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )

        with self.assertRaises(github.GitHubSourceUnavailableError):
            adapter.fetch_snapshot(self.admission())


if __name__ == "__main__":
    unittest.main()
