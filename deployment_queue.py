"""Asynchronous deployment queue and background worker for AutoPod Cloud.

Decouples GitHub webhook reception from synchronous Docker builds and ECR
operations. Ensures:
1. Webhooks return HTTP 202 Accepted within <500ms to meet GitHub's 10s timeout.
2. Consecutive commits are queued sequentially per application.
3. Obsolete commits are superseded if a newer commit is queued before the older
   one starts building.
4. Gunicorn worker processes remain unblocked and fully responsive.
"""

import threading
import time
import logging
import json
import os
from typing import Optional, Dict, Any

import database

logger = logging.getLogger('autopod.queue')


class DeploymentQueueWorker:
    """Background worker daemon that polls and executes deployment queue jobs."""

    def __init__(self, docker_mgr=None, poll_interval_sec=2.0):
        self.docker_mgr = docker_mgr
        self.poll_interval = poll_interval_sec
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._is_running = False

    def start(self, docker_mgr=None):
        """Start the background queue worker thread if not already running."""
        with self._lock:
            if docker_mgr:
                self.docker_mgr = docker_mgr
            if self._is_running and self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._is_running = True
            self._worker_thread = threading.Thread(
                target=self._run_loop,
                name="AutoPodDeploymentQueueWorker",
                daemon=True
            )
            self._worker_thread.start()
            logger.info("AutoPod Deployment Queue Worker started")

    def stop(self, timeout=10.0):
        """Signal worker to stop and wait for completion."""
        with self._lock:
            if not self._is_running:
                return
            self._stop_event.set()
            self._is_running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            logger.info("AutoPod Deployment Queue Worker stopped")

    def _run_loop(self):
        """Continuous polling loop."""
        while not self._stop_event.is_set():
            try:
                job = database.claim_next_queued_deployment()
                if job:
                    self._process_job(job)
                else:
                    self._stop_event.wait(timeout=self.poll_interval)
            except Exception as e:
                logger.exception("Error in deployment queue worker loop: %s", e)
                self._stop_event.wait(timeout=self.poll_interval)

    def _process_job(self, job: Dict[str, Any]):
        """Process a single queued deployment job with commit ordering."""
        queue_id = job["id"]
        app_id = job["application_id"]
        commit_sha = job.get("commit_sha")

        logger.info(
            "Processing queued deployment #%d (app_id=%d, commit=%s)",
            queue_id, app_id, commit_sha
        )

        # Mark job as processing
        database.update_queued_deployment(queue_id, "processing")

        # Supersede any older queued jobs for this same application
        database.supersede_older_queued_deployments(app_id, queue_id)

        try:
            payload = json.loads(job["payload_json"])
        except Exception as e:
            err = f"Failed to parse queued payload JSON: {e}"
            logger.error(err)
            database.update_queued_deployment(queue_id, "failed", error_message=err)
            return

        if self.docker_mgr is None:
            err = "DockerManager not configured on queue worker"
            logger.error(err)
            database.update_queued_deployment(queue_id, "failed", error_message=err)
            return

        try:
            from deployment_engine import deploy
            result = deploy(
                self.docker_mgr,
                payload,
                triggered_by=job.get("triggered_by", "webhook"),
                allow_auto_register=False
            )

            status = result.get("status")
            if status == "success":
                database.update_queued_deployment(queue_id, "completed")
                logger.info("Queued deployment #%d completed successfully", queue_id)
            else:
                msg = result.get("message", "Deployment failed")
                database.update_queued_deployment(queue_id, "failed", error_message=msg)
                logger.warning("Queued deployment #%d ended with status '%s': %s", queue_id, status, msg)

        except Exception as e:
            err = f"Unhandled exception during queued deployment execution: {e}"
            logger.exception(err)
            database.update_queued_deployment(queue_id, "failed", error_message=str(e))


# Global singleton instance
_queue_worker = DeploymentQueueWorker()


def get_queue_worker() -> DeploymentQueueWorker:
    """Return the global DeploymentQueueWorker singleton."""
    return _queue_worker


def enqueue_webhook_deployment(application_id: int, payload: dict, triggered_by: str = "webhook") -> dict:
    """Enqueue a webhook deployment and return the queued record."""
    commit_sha = None
    branch = "main"

    if isinstance(payload, dict):
        commit_sha = payload.get("after") or (payload.get("head_commit") or {}).get("id")
        ref = payload.get("ref") or ""
        if ref.startswith("refs/heads/"):
            branch = ref.replace("refs/heads/", "")
        elif payload.get("branch"):
            branch = payload.get("branch")

    record = database.enqueue_deployment(
        application_id=application_id,
        payload_json=payload,
        commit_sha=commit_sha,
        branch=branch,
        triggered_by=triggered_by
    )

    # Ensure worker thread is running
    _queue_worker.start()

    return record

