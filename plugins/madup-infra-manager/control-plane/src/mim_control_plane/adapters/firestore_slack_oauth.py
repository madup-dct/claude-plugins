"""Firestore persistence for Slack OAuth pending state and shared install metadata."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from typing import Protocol, cast

from mim_control_plane.domain.models import UserId
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthIdentityLink,
    SlackOAuthIdentityLinkState,
    SlackOAuthInstallState,
    SlackOAuthPendingState,
    SlackOAuthSharedInstall,
    SlackOAuthTenant,
)
from mim_control_plane.ports.slack_oauth import (
    SlackOAuthInstallRepositoryError,
    SlackOAuthStateOwnerMismatch,
    SlackOAuthStateRejected,
    SlackOAuthStateStoreError,
)

_SCHEMA_VERSION = 1
_DOCUMENT_ID_PREFIX = b"mim:firestore-slack-oauth:v1\x00"
_PENDING_COLLECTION = "slack_oauth_pending_states"
_INSTALLS_COLLECTION = "slack_oauth_shared_installs"
_LINKS_COLLECTION = "slack_oauth_identity_links"
_ACTIVE_TENANTS_COLLECTION = "slack_oauth_active_tenants"
_PENDING_FIELDS = frozenset(
    {
        "schema_version",
        "state_id",
        "state_hash",
        "installer_mim_user_id",
        "installer_email",
        "required_scopes",
        "redirect_uri",
        "install_tenant",
        "issued_at",
        "expires_at",
        "consumed_at",
        "version",
    }
)
_INSTALL_FIELDS = frozenset(
    {
        "schema_version",
        "install_id",
        "app_id",
        "team_id",
        "enterprise_id",
        "is_enterprise_install",
        "granted_scopes",
        "secret_ref",
        "installer_mim_user_id",
        "installer_email",
        "created_at",
        "updated_at",
        "state",
        "revoked_at",
        "version",
    }
)
_LINK_FIELDS = frozenset(
    {
        "schema_version",
        "install_id",
        "team_id",
        "slack_user_id",
        "mim_user_id",
        "company_email",
        "created_at",
        "updated_at",
        "state",
        "revoked_at",
        "version",
    }
)
_ACTIVE_TENANT_FIELDS = frozenset(
    {
        "schema_version",
        "team_id",
        "enterprise_id",
        "install_id",
    }
)


class _DocumentSnapshot(Protocol):
    id: str
    exists: bool

    def to_dict(self) -> dict[str, object] | None: ...


class _DocumentReference(Protocol):
    id: str

    def get(self, *, transaction: object | None = None) -> _DocumentSnapshot: ...
    def set(self, data: dict[str, object]) -> None: ...
    def create(self, data: dict[str, object]) -> None: ...


class _Collection(Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...

    def where(self, field_name: str, op_string: str, value: object) -> _Query: ...


class _Query(Protocol):
    def where(self, field_name: str, op_string: str, value: object) -> _Query: ...

    def stream(self) -> Iterable[_DocumentSnapshot]: ...


class _FirestoreClient(Protocol):
    def collection(self, name: str) -> _Collection: ...


class _Transaction(Protocol):
    def set(self, reference: _DocumentReference, data: dict[str, object]) -> None: ...

    def create(
        self, reference: _DocumentReference, data: dict[str, object]
    ) -> None: ...

    def delete(self, reference: _DocumentReference) -> None: ...


def _run_transaction(
    client: object,
    operation: Callable[[object], object],
) -> object:
    from google.cloud import firestore_v1

    transaction_factory = getattr(client, "transaction")
    transaction = transaction_factory(max_attempts=5)
    return firestore_v1.transactional(operation)(transaction)


def _document_id(*, kind: str, logical_id: str) -> str:
    digest = sha256()
    digest.update(_DOCUMENT_ID_PREFIX)
    digest.update(kind.encode("ascii"))
    digest.update(b"\x00")
    digest.update(logical_id.encode("utf-8"))
    return digest.hexdigest()


def _tenant_logical_id(*, team_id: str, enterprise_id: str | None) -> str:
    return f"{team_id}:{enterprise_id or '-'}"


def _require_exact_mapping(
    value: object,
    *,
    fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise ValueError
    return value


def _serialize_pending_state(state: SlackOAuthPendingState) -> dict[str, object]:
    payload = asdict(state)
    payload["schema_version"] = _SCHEMA_VERSION
    return cast(dict[str, object], payload)


def _serialize_shared_install(install: SlackOAuthSharedInstall) -> dict[str, object]:
    payload = asdict(install)
    payload["state"] = install.state.value
    payload["schema_version"] = _SCHEMA_VERSION
    return cast(dict[str, object], payload)


def _serialize_identity_link(link: SlackOAuthIdentityLink) -> dict[str, object]:
    payload = asdict(link)
    payload["state"] = link.state.value
    payload["schema_version"] = _SCHEMA_VERSION
    return cast(dict[str, object], payload)


def _serialize_active_tenant(
    *,
    team_id: str,
    enterprise_id: str | None,
    install_id: str,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "team_id": team_id,
        "enterprise_id": enterprise_id,
        "install_id": install_id,
    }


def _pending_from_snapshot(snapshot: _DocumentSnapshot) -> SlackOAuthPendingState:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(snapshot.to_dict(), fields=_PENDING_FIELDS)
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        state = SlackOAuthPendingState(
            state_id=cast(str, data["state_id"]),
            state_hash=cast(str, data["state_hash"]),
            installer_mim_user_id=UserId(cast(str, data["installer_mim_user_id"])),
            installer_email=cast(str, data["installer_email"]),
            required_scopes=tuple(cast(list[str], data["required_scopes"])),
            redirect_uri=cast(str, data["redirect_uri"]),
            install_tenant=SlackOAuthTenant(
                team_id=cast(dict[str, str], data["install_tenant"])["team_id"],
                enterprise_id=cast(dict[str, str | None], data["install_tenant"])[
                    "enterprise_id"
                ],
            ),
            issued_at=cast(datetime, data["issued_at"]),
            expires_at=cast(datetime, data["expires_at"]),
            consumed_at=cast(datetime | None, data["consumed_at"]),
            version=cast(int, data["version"]),
        )
        if snapshot.id != _document_id(kind="pending", logical_id=state.state_id):
            raise ValueError
        return state
    except Exception:
        raise SlackOAuthStateStoreError(
            "Slack OAuth state could not be read."
        ) from None


def _install_from_snapshot(snapshot: _DocumentSnapshot) -> SlackOAuthSharedInstall:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(snapshot.to_dict(), fields=_INSTALL_FIELDS)
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        install = SlackOAuthSharedInstall(
            install_id=cast(str, data["install_id"]),
            app_id=cast(str, data["app_id"]),
            team_id=cast(str, data["team_id"]),
            enterprise_id=cast(str | None, data["enterprise_id"]),
            is_enterprise_install=cast(bool, data["is_enterprise_install"]),
            granted_scopes=tuple(cast(list[str], data["granted_scopes"])),
            secret_ref=cast(str, data["secret_ref"]),
            installer_mim_user_id=UserId(cast(str, data["installer_mim_user_id"])),
            installer_email=cast(str, data["installer_email"]),
            created_at=cast(datetime, data["created_at"]),
            updated_at=cast(datetime, data["updated_at"]),
            state=SlackOAuthInstallState(cast(str, data["state"])),
            revoked_at=cast(datetime | None, data["revoked_at"]),
            version=cast(int, data["version"]),
        )
        if snapshot.id != _document_id(kind="install", logical_id=install.install_id):
            raise ValueError
        return install
    except Exception:
        raise SlackOAuthInstallRepositoryError(
            "Slack install metadata could not be read."
        ) from None


def _link_from_snapshot(snapshot: _DocumentSnapshot) -> SlackOAuthIdentityLink:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(snapshot.to_dict(), fields=_LINK_FIELDS)
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        link = SlackOAuthIdentityLink(
            install_id=cast(str, data["install_id"]),
            team_id=cast(str, data["team_id"]),
            slack_user_id=cast(str, data["slack_user_id"]),
            mim_user_id=UserId(cast(str, data["mim_user_id"])),
            company_email=cast(str, data["company_email"]),
            created_at=cast(datetime, data["created_at"]),
            updated_at=cast(datetime, data["updated_at"]),
            state=SlackOAuthIdentityLinkState(cast(str, data["state"])),
            revoked_at=cast(datetime | None, data["revoked_at"]),
            version=cast(int, data["version"]),
        )
        if snapshot.id != _document_id(
            kind="link",
            logical_id=f"{link.install_id}:{link.mim_user_id}",
        ):
            raise ValueError
        return link
    except Exception:
        raise SlackOAuthInstallRepositoryError(
            "Slack identity link could not be read."
        ) from None


def _install_id_from_active_tenant_snapshot(
    snapshot: _DocumentSnapshot,
    *,
    team_id: str,
    enterprise_id: str | None,
) -> str:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(snapshot.to_dict(), fields=_ACTIVE_TENANT_FIELDS)
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        if data["team_id"] != team_id or data["enterprise_id"] != enterprise_id:
            raise ValueError
        install_id = cast(str, data["install_id"])
        if snapshot.id != _document_id(
            kind="tenant",
            logical_id=_tenant_logical_id(
                team_id=team_id,
                enterprise_id=enterprise_id,
            ),
        ):
            raise ValueError
        return install_id
    except Exception:
        raise SlackOAuthInstallRepositoryError(
            "Slack install metadata could not be read."
        ) from None


class FirestoreSlackOAuthRepository:
    def __init__(
        self,
        *,
        client: _FirestoreClient,
        transaction_runner: Callable[[object, Callable[[object], object]], object]
        | None = None,
    ) -> None:
        self._client = client
        self._pending = client.collection(_PENDING_COLLECTION)
        self._installs = client.collection(_INSTALLS_COLLECTION)
        self._links = client.collection(_LINKS_COLLECTION)
        self._active_tenants = client.collection(_ACTIVE_TENANTS_COLLECTION)
        self._transaction_runner = transaction_runner or _run_transaction

    def create_pending_state(
        self,
        state: SlackOAuthPendingState,
    ) -> SlackOAuthPendingState:
        reference = self._pending.document(
            _document_id(kind="pending", logical_id=state.state_id)
        )
        try:
            reference.create(_serialize_pending_state(state))
            return state
        except Exception:
            raise SlackOAuthStateStoreError(
                "Slack OAuth state could not be saved."
            ) from None

    def consume_pending_state(
        self,
        *,
        state_id: str,
        state_hash: str,
        expected_installer_mim_user_id: str,
        expected_installer_email: str,
        expected_tenant: SlackOAuthTenant,
        expected_redirect_uri: str,
        expected_scopes: tuple[str, ...],
        now: datetime,
    ) -> SlackOAuthPendingState:
        reference = self._pending.document(
            _document_id(kind="pending", logical_id=state_id)
        )

        def operation(raw_transaction: object) -> SlackOAuthPendingState:
            transaction = cast(_Transaction, raw_transaction)
            record = _pending_from_snapshot(reference.get(transaction=transaction))
            if record.state_hash != state_hash:
                raise SlackOAuthStateRejected("state hash mismatch")
            if (
                record.installer_mim_user_id != expected_installer_mim_user_id
                or record.installer_email != expected_installer_email.casefold()
            ):
                raise SlackOAuthStateOwnerMismatch("wrong installer")
            if record.install_tenant != expected_tenant:
                raise SlackOAuthStateRejected("wrong tenant")
            if record.redirect_uri != expected_redirect_uri:
                raise SlackOAuthStateRejected("wrong redirect")
            if record.required_scopes != expected_scopes:
                raise SlackOAuthStateRejected("wrong scopes")
            if now >= record.expires_at or record.consumed_at is not None:
                raise SlackOAuthStateRejected("expired or replayed")
            consumed = SlackOAuthPendingState(
                state_id=record.state_id,
                state_hash=record.state_hash,
                installer_mim_user_id=record.installer_mim_user_id,
                installer_email=record.installer_email,
                required_scopes=record.required_scopes,
                redirect_uri=record.redirect_uri,
                install_tenant=record.install_tenant,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
                consumed_at=now,
                version=record.version + 1,
            )
            transaction.set(reference, _serialize_pending_state(consumed))
            return consumed

        try:
            return cast(
                SlackOAuthPendingState,
                self._transaction_runner(self._client, operation),
            )
        except (SlackOAuthStateRejected, SlackOAuthStateOwnerMismatch):
            raise
        except SlackOAuthStateStoreError:
            raise
        except Exception:
            raise SlackOAuthStateStoreError(
                "Slack OAuth state could not be consumed."
            ) from None

    def save_shared_install(
        self,
        install: SlackOAuthSharedInstall,
    ) -> SlackOAuthSharedInstall:
        install_reference = self._installs.document(
            _document_id(kind="install", logical_id=install.install_id)
        )
        tenant_reference = self._active_tenants.document(
            _document_id(
                kind="tenant",
                logical_id=_tenant_logical_id(
                    team_id=install.team_id,
                    enterprise_id=install.enterprise_id,
                ),
            )
        )

        def operation(raw_transaction: object) -> SlackOAuthSharedInstall:
            transaction = cast(_Transaction, raw_transaction)
            tenant_snapshot = tenant_reference.get(transaction=transaction)
            if tenant_snapshot.exists:
                active_install_id = _install_id_from_active_tenant_snapshot(
                    tenant_snapshot,
                    team_id=install.team_id,
                    enterprise_id=install.enterprise_id,
                )
                if active_install_id != install.install_id:
                    raise SlackOAuthInstallRepositoryError("active install exists")
            install_snapshot = install_reference.get(transaction=transaction)
            if install_snapshot.exists:
                current = _install_from_snapshot(install_snapshot)
                if current.state is SlackOAuthInstallState.ACTIVE:
                    raise SlackOAuthInstallRepositoryError("active install exists")
                persisted_install = SlackOAuthSharedInstall(
                    install_id=install.install_id,
                    app_id=install.app_id,
                    team_id=install.team_id,
                    enterprise_id=install.enterprise_id,
                    is_enterprise_install=install.is_enterprise_install,
                    granted_scopes=install.granted_scopes,
                    secret_ref=install.secret_ref,
                    installer_mim_user_id=install.installer_mim_user_id,
                    installer_email=install.installer_email,
                    created_at=install.created_at,
                    updated_at=install.updated_at,
                    state=install.state,
                    revoked_at=install.revoked_at,
                    version=current.version + 1,
                )
                transaction.set(
                    install_reference,
                    _serialize_shared_install(persisted_install),
                )
            else:
                transaction.create(
                    install_reference, _serialize_shared_install(install)
                )
                persisted_install = install
            transaction.set(
                tenant_reference,
                _serialize_active_tenant(
                    team_id=persisted_install.team_id,
                    enterprise_id=persisted_install.enterprise_id,
                    install_id=persisted_install.install_id,
                ),
            )
            return persisted_install

        try:
            return cast(
                SlackOAuthSharedInstall,
                self._transaction_runner(self._client, operation),
            )
        except SlackOAuthInstallRepositoryError:
            raise
        except Exception:
            raise SlackOAuthInstallRepositoryError(
                "Slack install metadata could not be saved."
            ) from None

    def get_shared_install(self, *, install_id: str) -> SlackOAuthSharedInstall:
        return _install_from_snapshot(
            self._installs.document(
                _document_id(kind="install", logical_id=install_id)
            ).get()
        )

    def get_active_install_for_tenant(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackOAuthSharedInstall | None:
        tenant_reference = self._active_tenants.document(
            _document_id(
                kind="tenant",
                logical_id=_tenant_logical_id(
                    team_id=team_id,
                    enterprise_id=enterprise_id,
                ),
            )
        )
        tenant_snapshot = tenant_reference.get()
        if tenant_snapshot.exists is not True:
            return None
        install_id = _install_id_from_active_tenant_snapshot(
            tenant_snapshot,
            team_id=team_id,
            enterprise_id=enterprise_id,
        )
        install = self.get_shared_install(install_id=install_id)
        if install.state is not SlackOAuthInstallState.ACTIVE:
            raise SlackOAuthInstallRepositoryError(
                "Slack install metadata could not be read."
            )
        return install

    def get_identity_link_by_slack_user(
        self,
        *,
        install_id: str,
        team_id: str,
        slack_user_id: str,
    ) -> SlackOAuthIdentityLink:
        try:
            query = (
                self._links.where("install_id", "==", install_id)
                .where("team_id", "==", team_id)
                .where("slack_user_id", "==", slack_user_id)
            )
            snapshots: tuple[_DocumentSnapshot, ...] = tuple(query.stream())
            if len(snapshots) != 1:
                raise ValueError
            link = _link_from_snapshot(snapshots[0])
            if (
                link.install_id != install_id
                or link.team_id != team_id
                or link.slack_user_id != slack_user_id
            ):
                raise ValueError
            return link
        except Exception:
            raise SlackOAuthInstallRepositoryError(
                "Slack identity link could not be read."
            ) from None

    def list_active_identity_links_for_mim_user(
        self,
        *,
        mim_user_id: str,
    ) -> tuple[SlackOAuthIdentityLink, ...]:
        try:
            query = self._links.where("mim_user_id", "==", mim_user_id).where(
                "state",
                "==",
                SlackOAuthIdentityLinkState.ACTIVE.value,
            )
            links = []
            for snapshot in query.stream():
                link = _link_from_snapshot(snapshot)
                if (
                    link.mim_user_id != mim_user_id
                    or link.state is not SlackOAuthIdentityLinkState.ACTIVE
                ):
                    raise ValueError
                links.append(link)
            return tuple(
                sorted(
                    links,
                    key=lambda item: (
                        item.install_id,
                        item.team_id,
                        item.slack_user_id,
                    ),
                )
            )
        except Exception:
            raise SlackOAuthInstallRepositoryError(
                "Slack identity link could not be read."
            ) from None

    def revoke_shared_install(
        self,
        *,
        install_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthSharedInstall:
        reference = self._installs.document(
            _document_id(kind="install", logical_id=install_id)
        )

        def operation(raw_transaction: object) -> SlackOAuthSharedInstall:
            transaction = cast(_Transaction, raw_transaction)
            current = _install_from_snapshot(reference.get(transaction=transaction))
            tenant_reference = self._active_tenants.document(
                _document_id(
                    kind="tenant",
                    logical_id=_tenant_logical_id(
                        team_id=current.team_id,
                        enterprise_id=current.enterprise_id,
                    ),
                )
            )
            revoked = SlackOAuthSharedInstall(
                install_id=current.install_id,
                app_id=current.app_id,
                team_id=current.team_id,
                enterprise_id=current.enterprise_id,
                is_enterprise_install=current.is_enterprise_install,
                granted_scopes=current.granted_scopes,
                secret_ref=current.secret_ref,
                installer_mim_user_id=current.installer_mim_user_id,
                installer_email=current.installer_email,
                created_at=current.created_at,
                updated_at=revoked_at,
                state=SlackOAuthInstallState.REVOKED,
                revoked_at=revoked_at,
                version=current.version + 1,
            )
            transaction.set(reference, _serialize_shared_install(revoked))
            tenant_snapshot = tenant_reference.get(transaction=transaction)
            if tenant_snapshot.exists:
                active_install_id = _install_id_from_active_tenant_snapshot(
                    tenant_snapshot,
                    team_id=current.team_id,
                    enterprise_id=current.enterprise_id,
                )
                if active_install_id == current.install_id:
                    transaction.delete(tenant_reference)
            return revoked

        try:
            return cast(
                SlackOAuthSharedInstall,
                self._transaction_runner(self._client, operation),
            )
        except SlackOAuthInstallRepositoryError:
            raise
        except Exception:
            raise SlackOAuthInstallRepositoryError(
                "Slack install metadata could not be revoked."
            ) from None

    def save_identity_link(
        self,
        link: SlackOAuthIdentityLink,
    ) -> SlackOAuthIdentityLink:
        try:
            self._links.document(
                _document_id(
                    kind="link", logical_id=f"{link.install_id}:{link.mim_user_id}"
                )
            ).set(_serialize_identity_link(link))
            return link
        except Exception:
            raise SlackOAuthInstallRepositoryError(
                "Slack identity link could not be saved."
            ) from None

    def revoke_identity_link(
        self,
        *,
        install_id: str,
        mim_user_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthIdentityLink:
        reference = self._links.document(
            _document_id(kind="link", logical_id=f"{install_id}:{mim_user_id}")
        )

        def operation(raw_transaction: object) -> SlackOAuthIdentityLink:
            transaction = cast(_Transaction, raw_transaction)
            current = _link_from_snapshot(reference.get(transaction=transaction))
            revoked = SlackOAuthIdentityLink(
                install_id=current.install_id,
                team_id=current.team_id,
                slack_user_id=current.slack_user_id,
                mim_user_id=current.mim_user_id,
                company_email=current.company_email,
                created_at=current.created_at,
                updated_at=revoked_at,
                state=SlackOAuthIdentityLinkState.REVOKED,
                revoked_at=revoked_at,
                version=current.version + 1,
            )
            transaction.set(reference, _serialize_identity_link(revoked))
            return revoked

        try:
            return cast(
                SlackOAuthIdentityLink,
                self._transaction_runner(self._client, operation),
            )
        except SlackOAuthInstallRepositoryError:
            raise
        except Exception:
            raise SlackOAuthInstallRepositoryError(
                "Slack identity link could not be revoked."
            ) from None
