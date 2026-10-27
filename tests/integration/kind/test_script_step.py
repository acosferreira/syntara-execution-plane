"""Dispatch an SDK-authored script-step workload through the Execution Plane.

PoC (uncommitted): the workload image is built by ``syntara-plugin build
--with-workload`` from ``syntara-step-types/script-step`` and advertises two
step revisions — ``syntara.script-step.python`` and ``syntara.script-step.bash``
— through the SDK's container ABI (``syntara.container/v1alpha1``). The work
items below drive the real cold-start dispatch path: claim, placement, K8s
Job, gRPC Health/Execute over the API-server port-forward, and the terminal
Result persisted before Job cleanup.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest

from execution_plane.cluster.cluster_store import ClusterStore
from execution_plane.config import EPSettings, get_ep_settings
from execution_plane.execution_target.execution_target_store import ExecutionTargetStore
from execution_plane.execution_target_reconciler.adapters import build_placement_resolver
from execution_plane.execution_target_reconciler.placement import WorkerManagerRegistry
from execution_plane.models.execution_target import BackendType
from execution_plane.models.work_item import WorkItemStatus
from execution_plane.work_item.work_item_store import WorkStore
from execution_plane.work_item.work_scheduler import WorkScheduler
from execution_plane.worker import _process_item
from execution_plane.worker_manager.vanilla_k8s.manager import VanillaK8sWorkerManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from execution_plane.credential_resolution.resolver import CredentialResolver
    from execution_plane.models.cluster import Cluster

SDK_IMAGE = os.environ.get("EP_SDK_STEP_IMAGE", "localhost:5000/syntara-script-step:0.1.0")


def _script_payload(step_revision_id: str, code: str) -> dict[str, Any]:
    """Build a work.execute payload for one SDK script-step revision."""

    return {
        "invocation": {
            "workflow_run_id": f"script-step-poc-{uuid.uuid4()}",
            "step_revision_id": step_revision_id,
            "inputs": {"code": code},
            "timeout_seconds": 120,
        },
        "image": SDK_IMAGE,
        "selectors": {},
    }


@asynccontextmanager
async def _work_service(
    database_url: str,
    cluster_store: ClusterStore,
    target_store: ExecutionTargetStore,
    settings: EPSettings,
    credential_resolver: CredentialResolver,
) -> AsyncIterator[tuple[WorkStore, WorkScheduler, WorkerManagerRegistry]]:
    worker_managers = WorkerManagerRegistry()
    placement_resolver = build_placement_resolver(cluster_store, target_store, worker_managers)
    async with WorkStore.from_database_url(database_url) as work_item_store:
        work_scheduler = WorkScheduler(work_item_store, placement_resolver)
        workload_manager = VanillaK8sWorkerManager(
            target_store,
            cluster_store,
            work_item_store,
            settings,
            credential_resolver=credential_resolver,
        )
        worker_managers.register(BackendType.VANILLA_K8S, workload_manager)
        yield work_item_store, work_scheduler, worker_managers


async def _dispatch_and_complete(
    payload: dict[str, Any],
    ep_cluster: Cluster,
    credential_resolver: CredentialResolver,
) -> dict[str, Any] | None:
    database_url = os.environ["EP_TEST_DATABASE_URL"]
    settings = get_ep_settings()
    client_id = "ep-it-script-step"

    async with (
        ClusterStore.from_database_url(database_url) as cluster_store,
        ExecutionTargetStore.from_database_url(database_url) as target_store,
    ):
        async with _work_service(
            database_url, cluster_store, target_store, settings, credential_resolver
        ) as (work_store, scheduler, worker_managers):
            item = await work_store.dispatch(
                client_id=client_id,
                item_id=uuid.uuid4(),
                payload=payload,
            )
            assigned = await scheduler.claim_and_place_work()
            assert assigned is not None
            assert assigned.id == item.id

            await _process_item(assigned, work_store, target_store, worker_managers, settings)

            final_item = await work_store.get(item.id, client_id=client_id)

    assert final_item is not None
    assert final_item.status is WorkItemStatus.COMPLETED, (
        f"Expected COMPLETED but got {final_item.status}; result={final_item.result}"
    )
    return final_item.result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sdk_python_script_step_completes(
    ep_cluster: Cluster,
    integration_credential_resolver: CredentialResolver,
) -> None:
    """The SDK-authored Python step runs in a cold-start Pod and returns real output."""

    result = await _dispatch_and_complete(
        _script_payload(
            "syntara.script-step.python",
            "import json\nprint(json.dumps({'greeting': 'hello from the execution plane'}))",
        ),
        ep_cluster,
        integration_credential_resolver,
    )
    assert result is not None
    output = result.get("output")
    assert isinstance(output, dict)
    assert "hello from the execution plane" in output.get("stdout", "")
    assert output.get("stdout_json") == {"greeting": "hello from the execution plane"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sdk_bash_script_step_completes(
    ep_cluster: Cluster,
    integration_credential_resolver: CredentialResolver,
) -> None:
    """The SDK-authored Bash step runs in the same image under its own identity."""

    result = await _dispatch_and_complete(
        _script_payload("syntara.script-step.bash", "echo hello-from-bash-via-ep"),
        ep_cluster,
        integration_credential_resolver,
    )
    assert result is not None
    output = result.get("output")
    assert isinstance(output, dict)
    assert output.get("stdout", "").strip() == "hello-from-bash-via-ep"
