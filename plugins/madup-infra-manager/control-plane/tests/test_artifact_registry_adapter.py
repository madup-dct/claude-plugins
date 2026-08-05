from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.artifact_registry import (  # noqa: E402
    ArtifactRegistryAdapter,
    ArtifactRegistryAdapterError,
)

PROJECT_ID = "mim-prod-123456"
REGION = "asia-northeast3"
PACKAGE_PARENT = (
    f"projects/{PROJECT_ID}/locations/{REGION}/repositories/mim/packages/workloads"
)
DIGEST = "a" * 64


@dataclass(frozen=True, slots=True)
class FakeTag:
    name: str
    version: str


class FakeArtifactRegistryClient:
    def __init__(self) -> None:
        self.tags: dict[str, FakeTag] = {}
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, FakeTag, str]] = []
        self.delete_calls: list[str] = []

    def get_tag(self, *, name: str) -> FakeTag:
        self.get_calls.append(name)
        try:
            return self.tags[name]
        except KeyError as exc:
            raise LookupError(name) from exc

    def create_tag(self, *, parent: str, tag: object, tag_id: str) -> object:
        name = getattr(tag, "name", None)
        version = getattr(tag, "version", None)
        if type(name) is not str or type(version) is not str:
            raise TypeError("tag must expose exact name/version strings")
        stored = FakeTag(name=name, version=version)
        self.create_calls.append((parent, stored, tag_id))
        self.tags[stored.name] = stored
        return stored

    def delete_tag(self, *, name: str) -> None:
        self.delete_calls.append(name)
        self.tags.pop(name, None)


def adapter(
    client: FakeArtifactRegistryClient | None = None,
) -> tuple[ArtifactRegistryAdapter, FakeArtifactRegistryClient]:
    fake = client or FakeArtifactRegistryClient()
    return (
        ArtifactRegistryAdapter(
            client=fake,
            project_id=PROJECT_ID,
            region=REGION,
        ),
        fake,
    )


class ArtifactRegistryAdapterTests(unittest.TestCase):
    def test_retain_creates_exact_central_tag_and_replays_without_mutation(
        self,
    ) -> None:
        registry, client = adapter()

        first = registry.retain(DIGEST)
        second = registry.retain(DIGEST)

        self.assertEqual(first, DIGEST)
        self.assertEqual(second, DIGEST)
        self.assertEqual(len(client.create_calls), 1)
        self.assertEqual(client.delete_calls, [])
        parent, tag, tag_id = client.create_calls[0]
        self.assertEqual(parent, PACKAGE_PARENT)
        self.assertEqual(tag_id, f"sha256-{DIGEST}")
        self.assertEqual(tag.name, f"{PACKAGE_PARENT}/tags/sha256-{DIGEST}")
        self.assertEqual(tag.version, f"{PACKAGE_PARENT}/versions/sha256:{DIGEST}")

    def test_retain_repairs_same_tag_when_it_points_to_the_wrong_digest(self) -> None:
        registry, client = adapter()
        wrong_tag = FakeTag(
            name=f"{PACKAGE_PARENT}/tags/sha256-{DIGEST}",
            version=f"{PACKAGE_PARENT}/versions/sha256:{'b' * 64}",
        )
        client.tags[wrong_tag.name] = wrong_tag

        retained = registry.retain(DIGEST)

        self.assertEqual(retained, DIGEST)
        self.assertEqual(client.delete_calls, [wrong_tag.name])
        self.assertEqual(len(client.create_calls), 1)
        self.assertEqual(
            client.tags[wrong_tag.name].version,
            f"{PACKAGE_PARENT}/versions/sha256:{DIGEST}",
        )

    def test_retain_rejects_non_central_constructor_boundary(self) -> None:
        with self.assertRaises(ValueError):
            ArtifactRegistryAdapter(
                client=FakeArtifactRegistryClient(),
                project_id="other-project-12345",
                region=REGION,
            )

    def test_retain_fails_closed_when_cloud_returns_wrong_resource_shape(self) -> None:
        registry, client = adapter()

        def wrong_create(*, parent: str, tag: object, tag_id: str) -> object:
            del parent, tag_id
            version = getattr(tag, "version", None)
            if type(version) is not str:
                raise TypeError("tag must expose exact version strings")
            returned = FakeTag(
                name=f"{PACKAGE_PARENT}/tags/unexpected",
                version=version,
            )
            client.tags[returned.name] = returned
            return returned

        client.create_tag = wrong_create  # type: ignore[method-assign]

        with self.assertRaises(ArtifactRegistryAdapterError):
            registry.retain(DIGEST)
