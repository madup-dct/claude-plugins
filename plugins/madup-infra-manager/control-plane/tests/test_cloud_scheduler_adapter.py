from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import scheduler_v1

from mim_control_plane.adapters.cloud_scheduler import (
    CloudSchedulerAdapter,
    CloudSchedulerAdapterError,
    SchedulerAuthMode,
)
from mim_control_plane.domain.models import Schedule, ScheduleId, UserId, WorkloadId
from mim_control_plane.domain.states import ScheduleState

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
PROJECT_NUMBER = "123456789012"
REGION = "asia-northeast3"
PARENT = f"projects/{PROJECT_ID}/locations/{REGION}"
SCHEDULER_IDENTITY = f"mim-schedule-gateway@{PROJECT_ID}.iam.gserviceaccount.com"
GATEWAY_AUDIENCE = f"https://mim-schedule-gateway-{PROJECT_NUMBER}.{REGION}.run.app"
GATEWAY_URI = f"{GATEWAY_AUDIENCE}/v1/schedules/execute"


def schedule(
    *,
    cron: str = "0 * * * *",
    timezone: str = "Asia/Seoul",
    state: ScheduleState = ScheduleState.ENABLED,
) -> Schedule:
    return Schedule(
        id=ScheduleId("sch-1"),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        cron=cron,
        timezone=timezone,
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeSchedulerClient:
    def __init__(self) -> None:
        self.jobs: dict[str, scheduler_v1.Job] = {}
        self.get_requests: list[Any] = []
        self.create_requests: list[Any] = []
        self.update_requests: list[Any] = []
        self.pause_requests: list[Any] = []
        self.resume_requests: list[Any] = []

    def get_job(self, request: Any) -> scheduler_v1.Job:
        self.get_requests.append(request)
        try:
            return self.jobs[request.name]
        except KeyError:
            raise NotFound("missing") from None

    def create_job(self, request: Any) -> scheduler_v1.Job:
        self.create_requests.append(request)
        created = scheduler_v1.Job(request.job)
        created.state = scheduler_v1.Job.State.ENABLED
        self.jobs[created.name] = created
        return created

    def update_job(self, request: Any) -> scheduler_v1.Job:
        self.update_requests.append(request)
        updated = scheduler_v1.Job(request.job)
        updated.state = self.jobs[updated.name].state
        self.jobs[updated.name] = updated
        return updated

    def pause_job(self, request: Any) -> scheduler_v1.Job:
        self.pause_requests.append(request)
        paused = scheduler_v1.Job(self.jobs[request.name])
        paused.state = scheduler_v1.Job.State.PAUSED
        self.jobs[request.name] = paused
        return paused

    def resume_job(self, request: Any) -> scheduler_v1.Job:
        self.resume_requests.append(request)
        resumed = scheduler_v1.Job(self.jobs[request.name])
        resumed.state = scheduler_v1.Job.State.ENABLED
        self.jobs[request.name] = resumed
        return resumed


def adapter(
    client: FakeSchedulerClient | None = None,
) -> tuple[CloudSchedulerAdapter, FakeSchedulerClient]:
    fake = client or FakeSchedulerClient()
    return (
        CloudSchedulerAdapter(
            client=fake,
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            scheduler_service_account=SCHEDULER_IDENTITY,
        ),
        fake,
    )


class GatewaySchedulerTests(unittest.TestCase):
    def test_gateway_target_uses_exact_oidc_audience_and_bounded_policy(self) -> None:
        manager, client = adapter()

        result = manager.ensure_gateway_schedule(schedule())

        self.assertTrue(result.created)
        self.assertEqual(result.auth_mode, SchedulerAuthMode.OIDC)
        request = client.create_requests[0]
        self.assertEqual(request.parent, PARENT)
        job = request.job
        self.assertTrue(job.name.startswith(f"{PARENT}/jobs/mim-sch-"))
        self.assertEqual(job.schedule, "0 * * * *")
        self.assertEqual(job.time_zone, "Asia/Seoul")
        self.assertEqual(job.http_target.uri, GATEWAY_URI)
        self.assertEqual(job.http_target.http_method, scheduler_v1.HttpMethod.POST)
        self.assertEqual(
            job.http_target.oidc_token.service_account_email,
            SCHEDULER_IDENTITY,
        )
        self.assertEqual(job.http_target.oidc_token.audience, GATEWAY_AUDIENCE)
        self.assertEqual(
            job.http_target._pb.WhichOneof("authorization_header"),
            "oidc_token",
        )
        self.assertEqual(job.retry_config.retry_count, 0)
        self.assertEqual(job.attempt_deadline.seconds, 30)

    def test_identical_gateway_schedule_is_idempotent_without_update(self) -> None:
        manager, client = adapter()

        first = manager.ensure_gateway_schedule(schedule())
        second = manager.ensure_gateway_schedule(schedule())

        self.assertEqual(first.name, second.name)
        self.assertFalse(second.created)
        self.assertEqual(len(client.create_requests), 1)
        self.assertEqual(client.update_requests, [])

    def test_gateway_schedule_updates_drift_using_an_exact_field_mask(self) -> None:
        manager, client = adapter()
        first = manager.ensure_gateway_schedule(schedule())
        drifted = scheduler_v1.Job(client.jobs[first.name])
        drifted.time_zone = "UTC"
        client.jobs[first.name] = drifted

        result = manager.ensure_gateway_schedule(schedule())

        self.assertFalse(result.created)
        self.assertEqual(len(client.update_requests), 1)
        request = client.update_requests[0]
        self.assertEqual(
            set(request.update_mask.paths),
            {
                "attempt_deadline",
                "description",
                "http_target",
                "retry_config",
                "schedule",
                "time_zone",
            },
        )
        self.assertEqual(request.job.time_zone, "Asia/Seoul")

    def test_paused_gateway_schedule_is_resumed_to_enabled(self) -> None:
        manager, client = adapter()
        first = manager.ensure_gateway_schedule(schedule())
        paused = scheduler_v1.Job(client.jobs[first.name])
        paused.state = scheduler_v1.Job.State.PAUSED
        client.jobs[first.name] = paused

        result = manager.ensure_gateway_schedule(schedule())

        self.assertFalse(result.created)
        self.assertEqual(len(client.resume_requests), 1)
        self.assertEqual(client.resume_requests[0].name, first.name)
        self.assertEqual(
            client.jobs[first.name].state,
            scheduler_v1.Job.State.ENABLED,
        )

    def test_disabled_or_failed_cloud_job_state_fails_closed(self) -> None:
        for state in (
            scheduler_v1.Job.State.DISABLED,
            scheduler_v1.Job.State.UPDATE_FAILED,
            scheduler_v1.Job.State.STATE_UNSPECIFIED,
        ):
            with self.subTest(state=state):
                manager, client = adapter()
                first = manager.ensure_gateway_schedule(schedule())
                invalid = scheduler_v1.Job(client.jobs[first.name])
                invalid.state = state
                client.jobs[first.name] = invalid

                with self.assertRaises(CloudSchedulerAdapterError):
                    manager.ensure_gateway_schedule(schedule())

                self.assertEqual(client.resume_requests, [])

    def test_invalid_schedule_policy_or_state_fails_before_cloud_calls(self) -> None:
        invalid = (
            schedule(cron="*/5 * * * *"),
            schedule(timezone="UTC"),
            schedule(state=ScheduleState.DISABLED),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                manager, client = adapter()
                with self.assertRaises(CloudSchedulerAdapterError):
                    manager.ensure_gateway_schedule(candidate)
                self.assertEqual(client.get_requests, [])
                self.assertEqual(client.create_requests, [])

    def test_pause_gateway_schedule_uses_exact_pause_transition(self) -> None:
        manager, client = adapter()
        created = manager.ensure_gateway_schedule(schedule())

        paused = manager.pause(schedule(state=ScheduleState.PAUSED))

        self.assertEqual(paused.name, created.name)
        self.assertEqual(len(client.pause_requests), 1)
        self.assertEqual(client.pause_requests[0].name, created.name)
        self.assertEqual(client.jobs[created.name].state, scheduler_v1.Job.State.PAUSED)

    def test_resume_gateway_schedule_is_idempotent_for_enabled_and_repairs_paused(
        self,
    ) -> None:
        manager, client = adapter()
        created = manager.ensure_gateway_schedule(schedule())

        manager.resume(schedule())
        self.assertEqual(client.resume_requests, [])

        paused = scheduler_v1.Job(client.jobs[created.name])
        paused.state = scheduler_v1.Job.State.PAUSED
        client.jobs[created.name] = paused

        manager.resume(schedule())
        self.assertEqual(len(client.resume_requests), 1)
        self.assertEqual(client.resume_requests[0].name, created.name)


class DirectRunApiSchedulerTests(unittest.TestCase):
    def test_direct_cloud_run_jobs_api_target_is_hard_disabled(self) -> None:
        manager, client = adapter()
        cloud_run_job = f"{PARENT}/jobs/mim-wrk-123456789abc"

        with self.assertRaises(CloudSchedulerAdapterError):
            manager.ensure_direct_job_schedule(
                schedule(),
                cloud_run_job_name=cloud_run_job,
            )

        self.assertEqual(client.get_requests, [])
        self.assertEqual(client.create_requests, [])

    def test_direct_target_rejects_cross_project_region_and_unqualified_names(
        self,
    ) -> None:
        invalid_names = (
            "mim-job",
            f"projects/other-project/locations/{REGION}/jobs/mim-job",
            f"projects/{PROJECT_ID}/locations/us-central1/jobs/mim-job",
            f"projects/{PROJECT_ID}/locations/{REGION}/services/mim-job",
        )
        for name in invalid_names:
            with self.subTest(name=name):
                manager, client = adapter()
                with self.assertRaises(CloudSchedulerAdapterError):
                    manager.ensure_direct_job_schedule(
                        schedule(),
                        cloud_run_job_name=name,
                    )
                self.assertEqual(client.get_requests, [])

    def test_constructor_derives_gateway_and_rejects_untrusted_identity_inputs(
        self,
    ) -> None:
        cases = (
            {
                "project_number": "not-numeric",
                "scheduler_service_account": SCHEDULER_IDENTITY,
            },
            {
                "project_number": "012345678901",
                "scheduler_service_account": SCHEDULER_IDENTITY,
            },
            {
                "project_number": PROJECT_NUMBER,
                "scheduler_service_account": (
                    "mim-schedule-gateway@other-project.iam.gserviceaccount.com"
                ),
            },
            {
                "project_number": PROJECT_NUMBER,
                "scheduler_service_account": (
                    f"mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com"
                ),
            },
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    CloudSchedulerAdapter(
                        client=FakeSchedulerClient(),
                        project_id=PROJECT_ID,
                        region=REGION,
                        **values,
                    )

        foreign_gateway = {
            "gateway_uri": (
                "https://mim-schedule-gateway-999999999999."
                f"{REGION}.run.app/v1/schedules/execute"
            ),
            "gateway_audience": (
                f"https://mim-schedule-gateway-999999999999.{REGION}.run.app"
            ),
        }
        with self.assertRaises(TypeError):
            CloudSchedulerAdapter(
                client=FakeSchedulerClient(),
                project_id=PROJECT_ID,
                project_number=PROJECT_NUMBER,
                region=REGION,
                scheduler_service_account=SCHEDULER_IDENTITY,
                **foreign_gateway,
            )


if __name__ == "__main__":
    unittest.main()
