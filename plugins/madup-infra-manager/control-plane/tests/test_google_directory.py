from __future__ import annotations

import importlib
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_fakes = importlib.import_module("fakes")
_directory = importlib.import_module("mim_control_plane.adapters.google_directory")
_config = importlib.import_module("mim_control_plane.config")
_ports = importlib.import_module("mim_control_plane.ports.directory")

DirectoryProviderError = _ports.DirectoryProviderError
DirectoryRuntimeSettings = _config.DirectoryRuntimeSettings
GoogleDirectoryProvider = _directory.GoogleDirectoryProvider
ImpersonatedDirectoryTokenProvider = _directory.ImpersonatedDirectoryTokenProvider
build_directory_runtime_mapping = _fakes.build_directory_runtime_mapping

STARTED_AT = datetime(2026, 8, 3, 1, 0, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 8, 3, 1, 0, 5, tzinfo=UTC)
REQUIRED_GROUP = "mim-users"


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = list(values)

    def __call__(self) -> datetime:
        if not self._values:
            raise AssertionError("clock exhausted")
        return self._values.pop(0)


def raw_path(request: httpx.Request) -> str:
    return request.url.raw_path.decode().split("?", 1)[0]


class StaticTokenProvider:
    def __init__(
        self,
        *,
        token: str = "directory-access-token",
        error: Exception | None = None,
    ) -> None:
        self._token = token
        self._error = error
        self.calls: tuple[datetime, ...] = ()

    def get_token(self, *, now: datetime) -> str:
        self.calls = self.calls + (now,)
        if self._error is not None:
            raise self._error
        return self._token


def directory_settings(**overrides: object) -> DirectoryRuntimeSettings:
    return DirectoryRuntimeSettings.from_mapping(
        build_directory_runtime_mapping(**overrides),
    )


class ImpersonatedDirectoryTokenProviderTests(unittest.TestCase):
    def test_default_production_loader_uses_compute_metadata_credentials_only(
        self,
    ) -> None:
        source_credentials = object()
        captured: dict[str, object] = {}

        class FakeCredentials:
            token = "ya29.directory-token"

            def refresh(self, request: object) -> None:
                captured["refresh_request"] = request

        def fake_credentials_factory(
            *,
            source_credentials: object,
            target_principal: str,
            target_scopes: tuple[str, ...],
            subject: str,
        ) -> FakeCredentials:
            captured["source_credentials"] = source_credentials
            captured["target_principal"] = target_principal
            captured["target_scopes"] = target_scopes
            captured["subject"] = subject
            return FakeCredentials()

        with mock.patch.object(
            _directory,
            "_google_auth_compute_engine_credentials_factory",
            return_value=source_credentials,
        ) as compute_factory:
            provider = ImpersonatedDirectoryTokenProvider(
                settings=directory_settings(),
                credentials_factory=fake_credentials_factory,
                request_factory=object,
            )

            token = provider.get_token(now=STARTED_AT)

        self.assertEqual(token, "ya29.directory-token")
        compute_factory.assert_called_once_with()
        self.assertIs(captured["source_credentials"], source_credentials)
        self.assertEqual(
            captured["subject"],
            "directory.admin@madup.com",
        )

    def test_uses_exact_subject_target_and_readonly_scopes(self) -> None:
        source_credentials = object()
        captured: dict[str, object] = {}
        request_object = object()

        class FakeCredentials:
            def __init__(self) -> None:
                self.token = None

            def refresh(self, request: object) -> None:
                captured["refresh_request"] = request
                self.token = "ya29.directory-token"

        def fake_loader() -> object:
            captured["loader_called"] = True
            return source_credentials

        def fake_credentials_factory(
            *,
            source_credentials: object,
            target_principal: str,
            target_scopes: tuple[str, ...],
            subject: str,
        ) -> FakeCredentials:
            captured["source_credentials"] = source_credentials
            captured["target_principal"] = target_principal
            captured["target_scopes"] = target_scopes
            captured["subject"] = subject
            return FakeCredentials()

        provider = ImpersonatedDirectoryTokenProvider(
            settings=directory_settings(),
            source_credentials_loader=fake_loader,
            credentials_factory=fake_credentials_factory,
            request_factory=lambda: request_object,
        )

        token = provider.get_token(now=STARTED_AT)

        self.assertEqual(token, "ya29.directory-token")
        self.assertTrue(captured["loader_called"])
        self.assertIs(captured["source_credentials"], source_credentials)
        self.assertEqual(
            captured["target_principal"],
            "mim-directory-sync@mim-prod-123456.iam.gserviceaccount.com",
        )
        self.assertEqual(
            captured["target_scopes"],
            _ports.DIRECTORY_READONLY_SCOPES,
        )
        self.assertEqual(
            captured["subject"],
            "directory.admin@madup.com",
        )
        self.assertIs(captured["refresh_request"], request_object)

    def test_redacts_loader_and_refresh_failures(self) -> None:
        cases = (
            (
                "loader",
                dict(
                    source_credentials_loader=lambda: (_ for _ in ()).throw(
                        RuntimeError("operator.test@madup.com leaked"),
                    ),
                ),
            ),
            (
                "refresh",
                dict(
                    source_credentials_loader=lambda: object(),
                    credentials_factory=lambda **_: type(
                        "BrokenCredentials",
                        (),
                        {
                            "token": None,
                            "refresh": lambda self, request: (_ for _ in ()).throw(
                                RuntimeError(
                                    "mim-directory-sync@mim-prod-123456.iam.gserviceaccount.com"
                                ),
                            ),
                        },
                    )(),
                ),
            ),
        )

        for label, kwargs in cases:
            with self.subTest(label=label):
                provider = ImpersonatedDirectoryTokenProvider(
                    settings=directory_settings(),
                    request_factory=object,
                    **kwargs,
                )

                with self.assertRaises(DirectoryProviderError) as context:
                    provider.get_token(now=STARTED_AT)

                message = str(context.exception)
                self.assertEqual(message, "Directory snapshot failed.")
                self.assertNotIn("madup.com", message)
                self.assertNotIn("gserviceaccount.com", message)

    def test_genericizes_injected_directory_provider_errors(self) -> None:
        provider = ImpersonatedDirectoryTokenProvider(
            settings=directory_settings(),
            source_credentials_loader=lambda: (_ for _ in ()).throw(
                DirectoryProviderError("directory.admin@madup.com leaked"),
            ),
            request_factory=object,
        )

        with self.assertRaises(DirectoryProviderError) as context:
            provider.get_token(now=STARTED_AT)

        self.assertEqual(str(context.exception), "Directory snapshot failed.")

    def test_repr_redacts_sensitive_settings_and_factories(self) -> None:
        provider = ImpersonatedDirectoryTokenProvider(
            settings=directory_settings(),
        )

        rendered = repr(provider)

        self.assertIn("ImpersonatedDirectoryTokenProvider(", rendered)
        self.assertNotIn("madup.com", rendered)
        self.assertNotIn("gserviceaccount.com", rendered)
        self.assertNotIn("source_credentials_loader", rendered)


class GoogleDirectoryProviderTests(unittest.TestCase):
    def test_fetch_snapshot_uses_observed_clock_times_for_snapshot_bounds(
        self,
    ) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(raw_path(request))
            if "/groups/" in request.url.path and "/members" not in request.url.path:
                return httpx.Response(
                    200,
                    json={"id": "group-123", "email": "mim-users@madup.com"},
                )
            if request.url.path.endswith("/users"):
                return httpx.Response(
                    200,
                    json={
                        "users": [
                            {
                                "id": "dir-1",
                                "primaryEmail": "alpha@madup.com",
                                "suspended": False,
                                "archived": False,
                            },
                        ],
                    },
                )
            if "/members" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "members": [
                            {
                                "id": "dir-1",
                                "email": "alpha@madup.com",
                                "type": "USER",
                            },
                        ],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        snapshot = GoogleDirectoryProvider(
            settings=directory_settings(),
            token_provider=StaticTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=SequenceClock(
                STARTED_AT + timedelta(seconds=1),
                STARTED_AT + timedelta(seconds=4),
            ),
        ).fetch_snapshot(required_group=REQUIRED_GROUP, now=STARTED_AT)

        self.assertEqual(snapshot.started_at, STARTED_AT + timedelta(seconds=1))
        self.assertEqual(snapshot.completed_at, STARTED_AT + timedelta(seconds=4))
        self.assertEqual(
            requests,
            [
                "/admin/directory/v1/groups/mim-users%40madup.com",
                "/admin/directory/v1/users",
                "/admin/directory/v1/groups/group-123/members",
            ],
        )

    def test_rejects_required_group_label_mismatch_before_token_or_network(
        self,
    ) -> None:
        token_provider = StaticTokenProvider()
        provider = GoogleDirectoryProvider(
            settings=directory_settings(),
            token_provider=token_provider,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError(str(request.url))),
            ),
            clock=SequenceClock(STARTED_AT, STARTED_AT),
        )

        with self.assertRaises(DirectoryProviderError) as context:
            provider.fetch_snapshot(required_group="other-group", now=COMPLETED_AT)

        self.assertEqual(str(context.exception), "Directory snapshot failed.")
        self.assertEqual(token_provider.calls, ())

    def test_base_url_is_fixed_and_group_paths_are_quoted(self) -> None:
        with self.assertRaises(TypeError):
            GoogleDirectoryProvider(
                settings=directory_settings(),
                token_provider=StaticTokenProvider(),
                base_url="https://example.com",
            )

        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_path = raw_path(request)
            requests.append(request_path)
            if request_path == "/admin/directory/v1/groups/mim%2Busers%40madup.com":
                return httpx.Response(
                    200,
                    json={"id": "group/123", "email": "mim+users@madup.com"},
                )
            if request.url.path.endswith("/users"):
                return httpx.Response(
                    200,
                    json={
                        "users": [
                            {
                                "id": "dir-1",
                                "primaryEmail": "alpha@madup.com",
                                "suspended": False,
                                "archived": False,
                            },
                        ],
                    },
                )
            if request_path == "/admin/directory/v1/groups/group%2F123/members":
                return httpx.Response(
                    200,
                    json={"members": []},
                )
            raise AssertionError(f"unexpected request path: {request_path}")

        provider = GoogleDirectoryProvider(
            settings=directory_settings(
                MIM_DIRECTORY_REQUIRED_GROUP_EMAIL="mim+users@madup.com",
            ),
            token_provider=StaticTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=SequenceClock(STARTED_AT, STARTED_AT),
        )

        snapshot = provider.fetch_snapshot(
            required_group="mim+users",
            now=STARTED_AT + timedelta(seconds=1),
        )

        self.assertEqual(snapshot.required_group, "mim+users")
        self.assertEqual(
            requests,
            [
                "/admin/directory/v1/groups/mim%2Busers%40madup.com",
                "/admin/directory/v1/users",
                "/admin/directory/v1/groups/group%2F123/members",
            ],
        )

    def test_fetch_snapshot_collects_all_users_and_nested_memberships(self) -> None:
        requests: list[tuple[str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params.multi_items())
            requests.append((raw_path(request), params))
            self.assertEqual(
                request.headers["Authorization"],
                "Bearer directory-access-token",
            )
            if "/groups/" in request.url.path and "/members" not in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "id": "group-123",
                        "email": "MIM-USERS@madup.com",
                    },
                )
            if request.url.path.endswith("/users") and "pageToken" not in params:
                self.assertEqual(params["customer"], "my_customer")
                self.assertEqual(params["projection"], "basic")
                self.assertEqual(params["maxResults"], "500")
                return httpx.Response(
                    200,
                    json={
                        "users": [
                            {
                                "id": "dir-2",
                                "primaryEmail": "beta@madup.com",
                                "suspended": True,
                                "archived": False,
                            },
                            {
                                "id": "dir-1",
                                "primaryEmail": "alpha@madup.com",
                                "suspended": False,
                                "archived": False,
                            },
                        ],
                        "nextPageToken": "users-2",
                    },
                )
            if (
                request.url.path.endswith("/users")
                and params.get("pageToken") == "users-2"
            ):
                return httpx.Response(
                    200,
                    json={
                        "users": [
                            {
                                "id": "dir-3",
                                "primaryEmail": "gamma@madup.com",
                                "suspended": False,
                                "archived": True,
                            },
                        ],
                    },
                )
            if "/members" in request.url.path and "pageToken" not in params:
                self.assertIn("/groups/group-123/members", request.url.path)
                self.assertEqual(params["includeDerivedMembership"], "true")
                self.assertEqual(params["maxResults"], "200")
                return httpx.Response(
                    200,
                    json={
                        "members": [
                            {
                                "id": "dir-1",
                                "email": "alpha@madup.com",
                                "type": "USER",
                            },
                            {
                                "id": "grp-nested",
                                "email": "nested@madup.com",
                                "type": "GROUP",
                            },
                        ],
                        "nextPageToken": "members-2",
                    },
                )
            if (
                "/members" in request.url.path
                and params.get("pageToken") == "members-2"
            ):
                return httpx.Response(
                    200,
                    json={
                        "members": [
                            {
                                "id": "dir-2",
                                "email": "beta@madup.com",
                                "type": "USER",
                            },
                        ],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        provider = GoogleDirectoryProvider(
            settings=directory_settings(),
            token_provider=StaticTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=SequenceClock(
                STARTED_AT,
                COMPLETED_AT,
                STARTED_AT,
                COMPLETED_AT,
            ),
        )

        first = provider.fetch_snapshot(
            required_group=REQUIRED_GROUP,
            now=COMPLETED_AT,
        )
        second = provider.fetch_snapshot(
            required_group=REQUIRED_GROUP,
            now=COMPLETED_AT,
        )

        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.required_group, REQUIRED_GROUP)
        self.assertEqual(first.started_at, STARTED_AT)
        self.assertEqual(first.completed_at, COMPLETED_AT)
        self.assertEqual(
            tuple(
                (user.email, user.active, user.in_required_group)
                for user in first.users
            ),
            (
                ("alpha@madup.com", True, True),
                ("beta@madup.com", False, True),
                ("gamma@madup.com", False, False),
            ),
        )
        self.assertEqual(
            [path for path, _ in requests[:5]],
            [
                "/admin/directory/v1/groups/mim-users%40madup.com",
                "/admin/directory/v1/users",
                "/admin/directory/v1/users",
                "/admin/directory/v1/groups/group-123/members",
                "/admin/directory/v1/groups/group-123/members",
            ],
        )

    def test_external_group_members_do_not_provision_users(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params.multi_items())
            if "/groups/" in request.url.path and "/members" not in request.url.path:
                return httpx.Response(
                    200,
                    json={"id": "group-123", "email": "mim-users@madup.com"},
                )
            if request.url.path.endswith("/users"):
                return httpx.Response(
                    200,
                    json={
                        "users": [
                            {
                                "id": "dir-1",
                                "primaryEmail": "alpha@madup.com",
                                "suspended": False,
                                "archived": False,
                            },
                        ],
                    },
                )
            if "/members" in request.url.path:
                self.assertEqual(params["includeDerivedMembership"], "true")
                return httpx.Response(
                    200,
                    json={
                        "members": [
                            {
                                "id": "dir-1",
                                "email": "alpha@madup.com",
                                "type": "USER",
                            },
                            {
                                "id": "external-1",
                                "email": "outside@example.com",
                                "type": "EXTERNAL",
                            },
                        ],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        snapshot = GoogleDirectoryProvider(
            settings=directory_settings(),
            token_provider=StaticTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=SequenceClock(STARTED_AT, STARTED_AT),
        ).fetch_snapshot(required_group=REQUIRED_GROUP, now=COMPLETED_AT)

        self.assertEqual(len(snapshot.users), 1)
        self.assertEqual(snapshot.users[0].email, "alpha@madup.com")
        self.assertTrue(snapshot.users[0].in_required_group)

    def test_rejects_repeated_page_tokens_partial_failures_and_malformed_payloads(
        self,
    ) -> None:
        overflow_users = [
            {
                "id": f"dir-{index}",
                "primaryEmail": f"user-{index}@madup.com",
                "suspended": False,
                "archived": False,
            }
            for index in range(3)
        ]
        cases = (
            (
                "repeated-user-token",
                [
                    httpx.Response(
                        200,
                        json={"id": "group-123", "email": "mim-users@madup.com"},
                    ),
                    httpx.Response(
                        200,
                        json={
                            "users": [
                                {
                                    "id": "dir-1",
                                    "primaryEmail": "alpha@madup.com",
                                    "suspended": False,
                                    "archived": False,
                                },
                            ],
                            "nextPageToken": "same",
                        },
                    ),
                    httpx.Response(200, json={"users": [], "nextPageToken": "same"}),
                ],
            ),
            (
                "partial-members-failure",
                [
                    httpx.Response(
                        200,
                        json={"id": "group-123", "email": "mim-users@madup.com"},
                    ),
                    httpx.Response(
                        200,
                        json={
                            "users": [
                                {
                                    "id": "dir-1",
                                    "primaryEmail": "alpha@madup.com",
                                    "suspended": False,
                                    "archived": False,
                                },
                            ],
                        },
                    ),
                    httpx.Response(
                        500,
                        json={"error": "directory.admin@madup.com should not leak"},
                    ),
                ],
            ),
            (
                "wrong-group-email",
                [
                    httpx.Response(
                        200,
                        json={"id": "group-123", "email": "other@madup.com"},
                    ),
                ],
            ),
            (
                "duplicate-user-email",
                [
                    httpx.Response(
                        200,
                        json={"id": "group-123", "email": "mim-users@madup.com"},
                    ),
                    httpx.Response(
                        200,
                        json={
                            "users": [
                                {
                                    "id": "dir-1",
                                    "primaryEmail": "dup@madup.com",
                                    "suspended": False,
                                    "archived": False,
                                },
                                {
                                    "id": "dir-2",
                                    "primaryEmail": "DUP@madup.com",
                                    "suspended": False,
                                    "archived": False,
                                },
                            ],
                        },
                    ),
                ],
            ),
            (
                "malformed-user-member",
                [
                    httpx.Response(
                        200,
                        json={"id": "group-123", "email": "mim-users@madup.com"},
                    ),
                    httpx.Response(
                        200,
                        json={
                            "users": [
                                {
                                    "id": "dir-1",
                                    "primaryEmail": "alpha@madup.com",
                                    "suspended": False,
                                    "archived": False,
                                },
                            ],
                        },
                    ),
                    httpx.Response(
                        200,
                        json={"members": [{"id": "dir-1", "type": "USER"}]},
                    ),
                ],
            ),
            (
                "malformed-external-member",
                [
                    httpx.Response(
                        200,
                        json={"id": "group-123", "email": "mim-users@madup.com"},
                    ),
                    httpx.Response(
                        200,
                        json={
                            "users": [
                                {
                                    "id": "dir-1",
                                    "primaryEmail": "alpha@madup.com",
                                    "suspended": False,
                                    "archived": False,
                                },
                            ],
                        },
                    ),
                    httpx.Response(
                        200,
                        json={"members": [{"id": "ext-1", "type": "EXTERNAL"}]},
                    ),
                ],
            ),
            (
                "overflow",
                [
                    httpx.Response(
                        200,
                        json={"id": "group-123", "email": "mim-users@madup.com"},
                    ),
                    httpx.Response(200, json={"users": overflow_users}),
                    httpx.Response(200, json={"members": []}),
                ],
            ),
        )

        for label, responses in cases:
            with self.subTest(label=label):
                response_queue = list(responses)

                def handler(request: httpx.Request) -> httpx.Response:
                    if not response_queue:
                        raise AssertionError(f"unexpected request: {request.url}")
                    return response_queue.pop(0)

                provider = GoogleDirectoryProvider(
                    settings=directory_settings(),
                    token_provider=StaticTokenProvider(
                        token="secret-directory-token",
                    ),
                    transport=httpx.MockTransport(handler),
                    clock=SequenceClock(STARTED_AT, STARTED_AT),
                )

                with mock.patch.object(
                    _directory,
                    "MAX_DIRECTORY_SNAPSHOT_USERS",
                    2,
                ):
                    with self.assertRaises(DirectoryProviderError) as context:
                        provider.fetch_snapshot(
                            required_group=REQUIRED_GROUP,
                            now=COMPLETED_AT,
                        )

                self.assertEqual(str(context.exception), "Directory snapshot failed.")
                self.assertNotIn("madup.com", str(context.exception))
                self.assertNotIn("secret-directory-token", str(context.exception))

    def test_rejects_backward_clock_and_redacts_repr(self) -> None:
        provider = GoogleDirectoryProvider(
            settings=directory_settings(),
            token_provider=StaticTokenProvider(token="top-secret-token"),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"id": "group-123", "email": "mim-users@madup.com"},
                ),
            ),
            clock=SequenceClock(
                STARTED_AT + timedelta(seconds=4),
                STARTED_AT + timedelta(seconds=1),
            ),
        )

        with self.assertRaises(DirectoryProviderError) as context:
            provider.fetch_snapshot(required_group=REQUIRED_GROUP, now=STARTED_AT)

        self.assertEqual(str(context.exception), "Directory snapshot failed.")

        rendered = repr(provider)
        self.assertIn("GoogleDirectoryProvider(", rendered)
        self.assertNotIn("madup.com", rendered)
        self.assertNotIn("top-secret-token", rendered)
        self.assertNotIn("MockTransport", rendered)

    def test_genericizes_injected_token_provider_errors(self) -> None:
        provider = GoogleDirectoryProvider(
            settings=directory_settings(),
            token_provider=StaticTokenProvider(
                error=DirectoryProviderError("secret-token should not leak"),
            ),
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError(str(request.url))),
            ),
            clock=SequenceClock(STARTED_AT, STARTED_AT),
        )

        with self.assertRaises(DirectoryProviderError) as context:
            provider.fetch_snapshot(required_group=REQUIRED_GROUP, now=STARTED_AT)

        self.assertEqual(str(context.exception), "Directory snapshot failed.")


if __name__ == "__main__":
    unittest.main()
