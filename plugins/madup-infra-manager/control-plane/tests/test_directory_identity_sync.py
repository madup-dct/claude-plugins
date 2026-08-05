from __future__ import annotations

import dataclasses
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

from mim_control_plane.adapters.fake_directory import (  # noqa: E402
    FakeDirectoryProvider,
)
from mim_control_plane.adapters.google_directory import (  # noqa: E402
    GoogleDirectoryProvider,
)
from mim_control_plane.adapters.memory_store import MemoryStore  # noqa: E402
from mim_control_plane.config import DirectoryRuntimeSettings  # noqa: E402
from mim_control_plane.domain.directory_sync import (  # noqa: E402
    DIRECTORY_READONLY_SCOPES,
    DirectoryAuthoritativeSnapshot,
    DirectorySnapshotUser,
    DirectoryUserReconciliation,
    build_directory_audit_event,
)
from mim_control_plane.domain.models import (  # noqa: E402
    AuditEventId,
    User,
    UserId,
)
from mim_control_plane.domain.states import UserRole, UserState  # noqa: E402
from mim_control_plane.ports.directory import (  # noqa: E402
    DirectoryIdentityRepositoryResult,
    DirectoryProviderError,
)
from mim_control_plane.ports.store import (  # noqa: E402
    IdempotencyConflict,
    InvariantViolation,
    VersionConflict,
)
from mim_control_plane.security.authorization import (  # noqa: E402
    AccessDenied,
    IdentityPolicy,
)
from mim_control_plane.workers.identity_sync import (  # noqa: E402
    DirectoryIdentitySyncWorker,
    DirectorySyncFailed,
)

NOW = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)
GROUP = "mim-users"


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = list(values)

    def __call__(self) -> datetime:
        if not self._values:
            raise AssertionError("clock exhausted")
        return self._values.pop(0)


class StaticTokenProvider:
    def __init__(self, *, token: str = "directory-access-token") -> None:
        self._token = token
        self.calls: tuple[datetime, ...] = ()

    def get_token(self, *, now: datetime) -> str:
        self.calls = self.calls + (now,)
        return self._token


def directory_runtime_settings(
    *,
    required_group_email: str = "mim-users@madup.com",
) -> DirectoryRuntimeSettings:
    return DirectoryRuntimeSettings(
        operator_email="operator.test@madup.com",
        directory_admin_subject="directory.admin@madup.com",
        directory_service_account_email=(
            "mim-directory-sync@mim-prod-123456.iam.gserviceaccount.com"
        ),
        directory_required_group_email=required_group_email,
    )


def user(
    *,
    user_id: str = "usr-1",
    email: str = "person@madup.com",
    role: UserRole = UserRole.USER,
    state: UserState = UserState.ACTIVE,
    groups: frozenset[str] = frozenset({"team-alpha"}),
    synced_at: datetime = NOW - timedelta(days=1),
    updated_at: datetime | None = None,
    version: int = 1,
) -> User:
    return User(
        id=UserId(user_id),
        email=email,
        role=role,
        state=state,
        groups=groups,
        identity_synced_at=synced_at,
        created_at=NOW - timedelta(days=90),
        updated_at=updated_at or synced_at,
        version=version,
    )


def snapshot_user(
    *,
    directory_user_id: str = "dir-1",
    email: str = "person@madup.com",
    active: bool = True,
    in_required_group: bool = True,
) -> DirectorySnapshotUser:
    return DirectorySnapshotUser(
        directory_user_id=directory_user_id,
        email=email,
        active=active,
        in_required_group=in_required_group,
    )


def snapshot(
    *,
    snapshot_id: str = "snap-1",
    users: tuple[DirectorySnapshotUser, ...] = (snapshot_user(),),
    started_at: datetime = NOW - timedelta(minutes=2),
    completed_at: datetime = NOW - timedelta(minutes=1),
) -> DirectoryAuthoritativeSnapshot:
    return DirectoryAuthoritativeSnapshot(
        snapshot_id=snapshot_id,
        required_group=GROUP,
        started_at=started_at,
        completed_at=completed_at,
        users=users,
    )


def policy(store: MemoryStore) -> IdentityPolicy:
    return IdentityPolicy(
        store=store,
        issuer="https://tenant.cloudflareaccess.com",
        audience="aud-1",
        company_domain="madup.com",
        required_group=GROUP,
        max_staleness=timedelta(minutes=30),
        clock=lambda: NOW,
    )


def unsafe_snapshot(
    *,
    snapshot_id: str = "snap-unsafe",
    users: tuple[DirectorySnapshotUser, ...],
    started_at: datetime,
    completed_at: datetime,
) -> DirectoryAuthoritativeSnapshot:
    record = object.__new__(DirectoryAuthoritativeSnapshot)
    object.__setattr__(record, "snapshot_id", snapshot_id)
    object.__setattr__(record, "required_group", GROUP)
    object.__setattr__(record, "started_at", started_at)
    object.__setattr__(record, "completed_at", completed_at)
    object.__setattr__(record, "users", users)
    return record


class DirectoryIdentitySyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()

    def worker_with(
        self,
        *,
        directory: FakeDirectoryProvider | None = None,
    ) -> DirectoryIdentitySyncWorker:
        return DirectoryIdentitySyncWorker(
            directory=directory or FakeDirectoryProvider(snapshot=snapshot()),
            repository=self.store,
            required_group=GROUP,
            max_snapshot_age=timedelta(minutes=15),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW,
        )

    def test_active_refresh_preserves_local_role_and_groups(self) -> None:
        self.store.create_user(
            user(
                role=UserRole.ADMIN,
                groups=frozenset({"team-alpha", GROUP}),
                synced_at=NOW - timedelta(days=2),
            )
        )

        result = self.worker_with(
            directory=FakeDirectoryProvider(
                snapshot=snapshot(
                    completed_at=NOW - timedelta(minutes=1),
                )
            )
        ).run(now=NOW)

        saved = self.store.get_user(UserId("usr-1"))
        self.assertEqual(saved.role, UserRole.ADMIN)
        self.assertEqual(saved.state, UserState.ACTIVE)
        self.assertEqual(saved.groups, frozenset({"team-alpha", GROUP}))
        self.assertEqual(saved.identity_synced_at, NOW - timedelta(minutes=1))
        self.assertEqual(result.active_users, 1)
        self.assertEqual(result.updated_users, 1)
        self.assertEqual(result.locked_user_ids, ())
        self.assertEqual(len(self.store.list_audit_events()), 1)

    def test_group_removal_and_missing_user_lock_accounts_and_policy_denies_immediately(
        self,
    ) -> None:
        suspended = user(user_id="usr-1", groups=frozenset({"team-alpha", GROUP}))
        missing = user(
            user_id="usr-2",
            email="two@madup.com",
            groups=frozenset({"team-beta", GROUP}),
        )
        self.store.create_user(suspended)
        self.store.create_user(missing)

        result = self.worker_with(
            directory=FakeDirectoryProvider(
                snapshot=snapshot(
                    users=(
                        snapshot_user(
                            directory_user_id="dir-1",
                            email="person@madup.com",
                            in_required_group=False,
                        ),
                    ),
                )
            )
        ).run(now=NOW)

        first = self.store.get_user(UserId("usr-1"))
        second = self.store.get_user(UserId("usr-2"))
        self.assertEqual(first.state, UserState.SUSPENDED)
        self.assertNotIn(GROUP, first.groups)
        self.assertEqual(second.state, UserState.OFFBOARDED)
        self.assertNotIn(GROUP, second.groups)
        self.assertEqual(
            result.locked_user_ids,
            (UserId("usr-1"), UserId("usr-2")),
        )

        local_policy = policy(self.store)
        with self.assertRaises(AccessDenied):
            local_policy.authorize_resolved_user(
                user_id=UserId("usr-1"),
                email="person@madup.com",
            )
        with self.assertRaises(AccessDenied):
            local_policy.authorize_resolved_user(
                user_id=UserId("usr-2"),
                email="two@madup.com",
            )

    def test_suspended_user_can_reactivate_but_offboarded_user_stays_terminal(
        self,
    ) -> None:
        self.store.create_user(
            user(
                user_id="usr-1",
                state=UserState.SUSPENDED,
                groups=frozenset({"team-alpha"}),
            )
        )
        self.store.create_user(
            user(
                user_id="usr-2",
                email="two@madup.com",
                state=UserState.OFFBOARDED,
                groups=frozenset({"team-beta"}),
            )
        )

        self.worker_with(
            directory=FakeDirectoryProvider(
                snapshot=snapshot(
                    users=(
                        snapshot_user(
                            email="person@madup.com",
                            directory_user_id="dir-1",
                        ),
                        snapshot_user(
                            email="two@madup.com",
                            directory_user_id="dir-2",
                        ),
                    ),
                )
            )
        ).run(now=NOW)

        self.assertEqual(self.store.get_user(UserId("usr-1")).state, UserState.ACTIVE)
        self.assertEqual(
            self.store.get_user(UserId("usr-2")).state,
            UserState.OFFBOARDED,
        )

    def test_unknown_directory_users_are_ignored_and_not_provisioned(self) -> None:
        self.store.create_user(user(groups=frozenset({GROUP, "team-alpha"})))

        result = self.worker_with(
            directory=FakeDirectoryProvider(
                snapshot=snapshot(
                    users=(
                        snapshot_user(
                            email="person@madup.com",
                            directory_user_id="dir-1",
                        ),
                        snapshot_user(
                            email="unknown@madup.com",
                            directory_user_id="dir-2",
                        ),
                    ),
                )
            )
        ).run(now=NOW)

        self.assertEqual(len(self.store.list_users()), 1)
        self.assertEqual(result.ignored_directory_users, 1)

    def test_invalid_or_stale_snapshot_cases_fail_closed_without_writes(self) -> None:
        original = user(groups=frozenset({"team-alpha", GROUP}))
        self.store.create_user(original)
        cases: tuple[tuple[str, FakeDirectoryProvider], ...] = (
            (
                "provider-error",
                FakeDirectoryProvider(error=DirectoryProviderError("directory failed")),
            ),
            (
                "future",
                FakeDirectoryProvider(
                    snapshot=snapshot(completed_at=NOW + timedelta(seconds=1))
                ),
            ),
            (
                "stale",
                FakeDirectoryProvider(
                    snapshot=snapshot(
                        started_at=NOW - timedelta(minutes=17),
                        completed_at=NOW - timedelta(minutes=16),
                    )
                ),
            ),
            (
                "too-long",
                FakeDirectoryProvider(
                    snapshot=snapshot(started_at=NOW - timedelta(minutes=8))
                ),
            ),
            (
                "duplicate-email",
                FakeDirectoryProvider(
                    snapshot_override=unsafe_snapshot(
                        users=(
                            snapshot_user(directory_user_id="dir-1"),
                            snapshot_user(
                                directory_user_id="dir-2",
                                email="PERSON@madup.com",
                            ),
                        ),
                        started_at=NOW - timedelta(minutes=2),
                        completed_at=NOW - timedelta(minutes=1),
                    )
                ),
            ),
            (
                "duplicate-directory-id",
                FakeDirectoryProvider(
                    snapshot_override=unsafe_snapshot(
                        users=(
                            snapshot_user(directory_user_id="dir-1"),
                            snapshot_user(
                                directory_user_id="DIR-1",
                                email="other@madup.com",
                            ),
                        ),
                        started_at=NOW - timedelta(minutes=2),
                        completed_at=NOW - timedelta(minutes=1),
                    )
                ),
            ),
            (
                "malformed",
                FakeDirectoryProvider(snapshot_override=object()),
            ),
        )

        for label, directory in cases:
            with self.subTest(label=label):
                store = MemoryStore()
                store.create_user(original)
                worker = DirectoryIdentitySyncWorker(
                    directory=directory,
                    repository=store,
                    required_group=GROUP,
                    max_snapshot_age=timedelta(minutes=15),
                    max_collection_duration=timedelta(minutes=5),
                    clock=lambda: NOW,
                )
                with self.assertRaises(DirectorySyncFailed):
                    worker.run(now=NOW)
                self.assertEqual(store.get_user(original.id), original)
                self.assertEqual(store.list_audit_events(), ())

    def test_reconcile_identity_preserves_groups_and_rejects_backwards_sync(
        self,
    ) -> None:
        original = user(
            groups=frozenset({"team-alpha"}),
            synced_at=NOW - timedelta(days=1),
        )

        updated = original.reconcile_identity(
            target_state=UserState.ACTIVE,
            required_group=GROUP,
            in_required_group=True,
            synced_at=NOW - timedelta(minutes=1),
        )

        self.assertEqual(updated.groups, frozenset({"team-alpha", GROUP}))
        with self.assertRaises(ValueError):
            updated.reconcile_identity(
                target_state=UserState.ACTIVE,
                required_group=GROUP,
                in_required_group=True,
                synced_at=NOW - timedelta(days=2),
            )

    def test_snapshot_required_group_drift_fails_closed(self) -> None:
        original = self.store.create_user(
            user(groups=frozenset({"team-alpha", GROUP}))
        )
        wrong_group_snapshot = DirectoryAuthoritativeSnapshot(
            snapshot_id="snap-wrong-group",
            required_group="another-mim-group",
            started_at=NOW - timedelta(minutes=2),
            completed_at=NOW - timedelta(minutes=1),
            users=(snapshot_user(),),
        )

        with self.assertRaises(DirectorySyncFailed):
            self.worker_with(
                directory=FakeDirectoryProvider(snapshot=wrong_group_snapshot)
            ).run(now=NOW)

        self.assertEqual(self.store.get_user(original.id), original)
        self.assertEqual(self.store.list_audit_events(), ())

    def test_worker_replays_same_snapshot_without_duplicate_writes(self) -> None:
        self.store.create_user(user(groups=frozenset({"team-alpha"})))
        directory = FakeDirectoryProvider(snapshot=snapshot())
        worker = self.worker_with(directory=directory)

        first = worker.run(now=NOW)
        saved_after_first = self.store.get_user(UserId("usr-1"))
        second = worker.run(now=NOW)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.updated_users, 0)
        self.assertEqual(self.store.get_user(UserId("usr-1")), saved_after_first)
        self.assertEqual(len(self.store.list_audit_events()), 1)

    def test_noop_snapshot_emits_no_per_user_audit(self) -> None:
        completed_at = NOW - timedelta(minutes=1)
        original = self.store.create_user(
            user(
                groups=frozenset({"team-alpha", GROUP}),
                synced_at=completed_at,
            )
        )

        result = self.worker_with(
            directory=FakeDirectoryProvider(
                snapshot=snapshot(
                    snapshot_id="snap-noop",
                    completed_at=completed_at,
                )
            )
        ).run(now=NOW)

        self.assertFalse(result.replayed)
        self.assertEqual(result.updated_users, 0)
        self.assertEqual(self.store.get_user(original.id), original)
        self.assertEqual(self.store.list_audit_events(), ())

    def test_post_fetch_clock_validates_real_provider_completion_time(self) -> None:
        self.store.create_user(user(groups=frozenset({"team-alpha"})))
        request_now = NOW
        observed_started_at = NOW + timedelta(minutes=2)
        observed_completed_at = NOW + timedelta(minutes=4)

        def handler(request: httpx.Request) -> httpx.Response:
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
                                "primaryEmail": "person@madup.com",
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
                                "email": "person@madup.com",
                                "type": "USER",
                            },
                        ],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        provider = GoogleDirectoryProvider(
            settings=directory_runtime_settings(),
            token_provider=StaticTokenProvider(),
            transport=httpx.MockTransport(handler),
            clock=SequenceClock(observed_started_at, observed_completed_at),
        )
        worker = DirectoryIdentitySyncWorker(
            directory=provider,
            repository=self.store,
            required_group=GROUP,
            max_snapshot_age=timedelta(minutes=15),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW + timedelta(minutes=5),
        )

        result = worker.run(now=request_now)

        self.assertEqual(result.updated_users, 1)
        self.assertEqual(
            self.store.get_user(UserId("usr-1")).identity_synced_at,
            observed_completed_at,
        )

    def test_post_fetch_clock_fails_closed_when_snapshot_is_still_in_future(
        self,
    ) -> None:
        self.store.create_user(user(groups=frozenset({"team-alpha", GROUP})))
        worker = DirectoryIdentitySyncWorker(
            directory=FakeDirectoryProvider(
                snapshot=snapshot(completed_at=NOW + timedelta(minutes=4)),
            ),
            repository=self.store,
            required_group=GROUP,
            max_snapshot_age=timedelta(minutes=15),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW + timedelta(minutes=3),
        )

        with self.assertRaises(DirectorySyncFailed):
            worker.run(now=NOW + timedelta(minutes=5))

    def test_malformed_repository_user_list_fails_before_apply(self) -> None:
        original = user(groups=frozenset({"team-alpha", GROUP}))
        for label, users in (
            ("mutable-list", [original]),
            ("duplicate-users", (original, original)),
            ("wrong-record", (object(),)),
        ):
            with self.subTest(label=label):
                repository = mock.Mock()
                repository.list_users.return_value = users
                worker = DirectoryIdentitySyncWorker(
                    directory=FakeDirectoryProvider(snapshot=snapshot()),
                    repository=repository,
                    required_group=GROUP,
                    max_snapshot_age=timedelta(minutes=15),
                    max_collection_duration=timedelta(minutes=5),
                    clock=lambda: NOW,
                )

                with self.assertRaises(DirectorySyncFailed):
                    worker.run(now=NOW)
                repository.apply_snapshot_once.assert_not_called()

    def test_replayed_repository_result_cannot_claim_prior_side_effect_ids(
        self,
    ) -> None:
        original = user(groups=frozenset({"team-alpha", GROUP}))
        repository = mock.Mock()
        repository.list_users.return_value = (original,)
        repository.apply_snapshot_once.side_effect = lambda **kwargs: (
            DirectoryIdentityRepositoryResult(
                snapshot_id=kwargs["snapshot_id"],
                material_hash=kwargs["material_hash"],
                replayed=True,
                applied_user_ids=(original.id,),
                locked_user_ids=(),
                audit_event_ids=(AuditEventId("unrelated-audit"),),
            )
        )
        worker = DirectoryIdentitySyncWorker(
            directory=FakeDirectoryProvider(snapshot=snapshot()),
            repository=repository,
            required_group=GROUP,
            max_snapshot_age=timedelta(minutes=15),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW,
        )

        with self.assertRaises(DirectorySyncFailed):
            worker.run(now=NOW)

    def test_audit_uses_configured_group_and_redacts_user_identity(self) -> None:
        configured_group = "mim-special-users"
        original = self.store.create_user(
            user(user_id="sensitive-subject", groups=frozenset({"team-alpha"}))
        )
        directory_snapshot = DirectoryAuthoritativeSnapshot(
            snapshot_id="snap-special-group",
            required_group=configured_group,
            started_at=NOW - timedelta(minutes=2),
            completed_at=NOW - timedelta(minutes=1),
            users=(snapshot_user(),),
        )
        worker = DirectoryIdentitySyncWorker(
            directory=FakeDirectoryProvider(snapshot=directory_snapshot),
            repository=self.store,
            required_group=configured_group,
            max_snapshot_age=timedelta(minutes=15),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW,
        )

        worker.run(now=NOW)

        (event,) = self.store.list_audit_events()
        self.assertEqual(event.policy_decision, "directory_active_member")
        self.assertEqual(event.before_ref, "active:0:v1")
        self.assertEqual(event.after_ref, "active:1:v2")
        self.assertNotIn(str(original.id), event.target_ref)
        self.assertNotIn(original.email, repr(event))

    def test_repository_rejects_non_required_group_mutation_atomically(self) -> None:
        original = self.store.create_user(
            user(groups=frozenset({"team-alpha", GROUP}))
        )
        malicious = dataclasses.replace(
            original,
            groups=frozenset({GROUP}),
            identity_synced_at=NOW - timedelta(minutes=1),
            version=original.version + 1,
        )

        with self.assertRaises(InvariantViolation):
            self.store.apply_snapshot_once(
                snapshot_id="snap-non-required-group",
                material_hash="c" * 64,
                reconciliations=(
                    DirectoryUserReconciliation(
                        user=malicious,
                        expected_version=original.version,
                        required_group=GROUP,
                        policy_decision="directory_active_member",
                    ),
                ),
                audit_events=(),
            )

        self.assertEqual(self.store.get_user(original.id), original)
        self.assertEqual(self.store.list_audit_events(), ())

    def test_tampered_valid_repository_result_is_rejected_before_writes(self) -> None:
        original = self.store.create_user(user(groups=frozenset({"team-alpha"})))
        self.store.directory_result_override = DirectoryIdentityRepositoryResult(
            snapshot_id="different-snapshot",
            material_hash="d" * 64,
            replayed=False,
            applied_user_ids=(original.id,),
            locked_user_ids=(),
            audit_event_ids=(AuditEventId("audit-tampered"),),
        )

        with self.assertRaises(DirectorySyncFailed):
            self.worker_with().run(now=NOW)

        self.assertEqual(self.store.get_user(original.id), original)
        self.assertEqual(self.store.list_audit_events(), ())

    def test_shuffled_repository_result_is_rejected_before_writes(self) -> None:
        first = self.store.create_user(
            user(user_id="usr-1", groups=frozenset({"team"}))
        )
        second = self.store.create_user(
            user(
                user_id="usr-2",
                email="two@madup.com",
                groups=frozenset({"team"}),
            )
        )
        synced_at = NOW - timedelta(minutes=1)
        first_update = first.reconcile_identity(
            target_state=UserState.ACTIVE,
            required_group=GROUP,
            in_required_group=True,
            synced_at=synced_at,
        )
        second_update = second.reconcile_identity(
            target_state=UserState.ACTIVE,
            required_group=GROUP,
            in_required_group=True,
            synced_at=synced_at,
        )
        reconciliations = (
            DirectoryUserReconciliation(
                user=first_update,
                expected_version=first.version,
                required_group=GROUP,
                policy_decision="directory_active_member",
            ),
            DirectoryUserReconciliation(
                user=second_update,
                expected_version=second.version,
                required_group=GROUP,
                policy_decision="directory_active_member",
            ),
        )
        audits = tuple(
            build_directory_audit_event(
                snapshot_id="snap-shuffled",
                required_group=GROUP,
                policy_decision="directory_active_member",
                user_before=before,
                user_after=after,
                synced_at=synced_at,
            )
            for before, after in (
                (first, first_update),
                (second, second_update),
            )
        )
        self.store.directory_result_override = DirectoryIdentityRepositoryResult(
            snapshot_id="snap-shuffled",
            material_hash="e" * 64,
            replayed=False,
            applied_user_ids=(second.id, first.id),
            locked_user_ids=(),
            audit_event_ids=tuple(event.id for event in audits),
        )

        with self.assertRaises(InvariantViolation):
            self.store.apply_snapshot_once(
                snapshot_id="snap-shuffled",
                material_hash="e" * 64,
                reconciliations=reconciliations,
                audit_events=audits,
            )

        self.assertEqual(self.store.get_user(first.id), first)
        self.assertEqual(self.store.get_user(second.id), second)
        self.assertEqual(self.store.list_audit_events(), ())

    def test_repository_apply_snapshot_is_atomic_and_idempotent(self) -> None:
        first = self.store.create_user(
            user(user_id="usr-1", groups=frozenset({"team"}))
        )
        second = self.store.create_user(
            user(
                user_id="usr-2",
                email="two@madup.com",
                groups=frozenset({"team"}),
            )
        )
        first_update = first.reconcile_identity(
            target_state=UserState.ACTIVE,
            required_group=GROUP,
            in_required_group=True,
            synced_at=NOW - timedelta(minutes=1),
        )
        second_update = second.reconcile_identity(
            target_state=UserState.SUSPENDED,
            required_group=GROUP,
            in_required_group=False,
            synced_at=NOW - timedelta(minutes=1),
        )
        audits = (
            build_directory_audit_event(
                snapshot_id="snap-1",
                required_group=GROUP,
                policy_decision="directory_active_member",
                user_before=first,
                user_after=first_update,
                synced_at=NOW - timedelta(minutes=1),
            ),
            build_directory_audit_event(
                snapshot_id="snap-1",
                required_group=GROUP,
                policy_decision="directory_inactive",
                user_before=second,
                user_after=second_update,
                synced_at=NOW - timedelta(minutes=1),
            ),
        )

        with self.assertRaises(VersionConflict):
            self.store.apply_snapshot_once(
                snapshot_id="snap-1",
                material_hash="a" * 64,
                reconciliations=(
                    DirectoryUserReconciliation(
                        user=first_update,
                        expected_version=1,
                        required_group=GROUP,
                        policy_decision="directory_active_member",
                    ),
                    DirectoryUserReconciliation(
                        user=dataclasses.replace(second_update, version=1000),
                        expected_version=999,
                        required_group=GROUP,
                        policy_decision="directory_inactive",
                    ),
                ),
                audit_events=audits,
            )
        self.assertEqual(self.store.get_user(first.id), first)
        self.assertEqual(self.store.get_user(second.id), second)
        self.assertEqual(self.store.list_audit_events(), ())

        applied = self.store.apply_snapshot_once(
            snapshot_id="snap-1",
            material_hash="a" * 64,
            reconciliations=(
                DirectoryUserReconciliation(
                    user=first_update,
                    expected_version=1,
                    required_group=GROUP,
                    policy_decision="directory_active_member",
                ),
                DirectoryUserReconciliation(
                    user=second_update,
                    expected_version=1,
                    required_group=GROUP,
                    policy_decision="directory_inactive",
                ),
            ),
            audit_events=audits,
        )
        replay = self.store.apply_snapshot_once(
            snapshot_id="snap-1",
            material_hash="a" * 64,
            reconciliations=(
                DirectoryUserReconciliation(
                    user=first_update,
                    expected_version=1,
                    required_group=GROUP,
                    policy_decision="directory_active_member",
                ),
                DirectoryUserReconciliation(
                    user=second_update,
                    expected_version=1,
                    required_group=GROUP,
                    policy_decision="directory_inactive",
                ),
            ),
            audit_events=audits,
        )
        with self.assertRaises(IdempotencyConflict):
            self.store.apply_snapshot_once(
                snapshot_id="snap-1",
                material_hash="b" * 64,
                reconciliations=(
                    DirectoryUserReconciliation(
                        user=first_update,
                        expected_version=1,
                        required_group=GROUP,
                        policy_decision="directory_active_member",
                    ),
                ),
                audit_events=audits[:1],
            )

        self.assertFalse(applied.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.applied_user_ids, ())
        self.assertEqual(replay.locked_user_ids, ())
        self.assertEqual(replay.audit_event_ids, ())
        self.assertEqual(len(self.store.list_audit_events()), 2)

    def test_memory_repository_enforces_the_same_fifty_identity_pilot_cap(
        self,
    ) -> None:
        with self.assertRaisesRegex(InvariantViolation, "pilot identity limit"):
            self.store.apply_snapshot_once(
                snapshot_id="snap-over-pilot-cap",
                material_hash="1" * 64,
                reconciliations=tuple(object() for _ in range(51)),  # type: ignore[arg-type]
                audit_events=(),
            )

    def test_audit_policy_decision_mismatch_is_rejected_atomically(self) -> None:
        original = self.store.create_user(user(groups=frozenset({"team-alpha"})))
        synced_at = NOW - timedelta(minutes=1)
        updated = original.reconcile_identity(
            target_state=UserState.ACTIVE,
            required_group=GROUP,
            in_required_group=True,
            synced_at=synced_at,
        )
        reconciliation = DirectoryUserReconciliation(
            user=updated,
            expected_version=original.version,
            required_group=GROUP,
            policy_decision="directory_active_member",
        )
        mismatched_audit = build_directory_audit_event(
            snapshot_id="snap-audit-mismatch",
            required_group=GROUP,
            policy_decision="directory_inactive",
            user_before=original,
            user_after=updated,
            synced_at=synced_at,
        )

        with self.assertRaises(InvariantViolation):
            self.store.apply_snapshot_once(
                snapshot_id="snap-audit-mismatch",
                material_hash="f" * 64,
                reconciliations=(reconciliation,),
                audit_events=(mismatched_audit,),
            )

        self.assertEqual(self.store.get_user(original.id), original)
        self.assertEqual(self.store.list_audit_events(), ())

    def test_malformed_repository_result_fails_closed(self) -> None:
        self.store.create_user(user(groups=frozenset({"team-alpha"})))
        repository = MemoryStore()
        repository.create_user(user(groups=frozenset({"team-alpha"})))
        repository.directory_result_override = object()
        worker = DirectoryIdentitySyncWorker(
            directory=FakeDirectoryProvider(snapshot=snapshot()),
            repository=repository,
            required_group=GROUP,
            max_snapshot_age=timedelta(minutes=15),
            max_collection_duration=timedelta(minutes=5),
            clock=lambda: NOW,
        )

        with self.assertRaises(DirectorySyncFailed):
            worker.run(now=NOW)
        self.assertEqual(repository.list_audit_events(), ())

    def test_public_directory_surfaces_expose_only_central_readonly_contract(
        self,
    ) -> None:
        self.assertEqual(
            DIRECTORY_READONLY_SCOPES,
            (
                "https://www.googleapis.com/auth/admin.directory.user.readonly",
                "https://www.googleapis.com/auth/admin.directory.group.readonly",
                "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
            ),
        )
        forbidden = (
            "secret",
            "token",
            "key",
            "project",
            "cloud",
            "bigquery",
            "billing",
        )
        for record_type in (
            DirectorySnapshotUser,
            DirectoryAuthoritativeSnapshot,
            DirectoryUserReconciliation,
            DirectoryIdentityRepositoryResult,
        ):
            for field in dataclasses.fields(record_type):
                self.assertFalse(
                    any(marker in field.name for marker in forbidden),
                    msg=f"{record_type.__name__}.{field.name} leaked forbidden config",
                )
