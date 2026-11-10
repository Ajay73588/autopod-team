"""
version_manager.py

Tracks image versions for AutoPod Cloud applications: tag generation and the
"what's current / what's available" bookkeeping needed for deployment
history and rollback (ARCHITECTURE.md Section 13).

This module only manages metadata -- it never shells out to Docker or Git
itself; `deployment_engine.py` is responsible for actually building/pushing
images and calls into this module to name and record them.
"""

import datetime as _dt
import database


def generate_tag(image_name, git_commit=None):
    """Build a unique image tag from a commit hash (if known) and a UTC
    timestamp, e.g. `autopod-myapp:a82f91c-20260827-104233`.

    If no commit is available (e.g. a demo/test deployment with no real
    git history), the tag falls back to `nogit-<timestamp>` so it is still
    guaranteed unique.
    """
    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    commit_part = git_commit if git_commit else "nogit"
    return f"{image_name}:{commit_part}-{timestamp}"


def record_version(application_id, image_tag, git_commit=None, framework=None):
    """Persist a newly built image as a version row."""
    return database.create_version(application_id, image_tag, git_commit, framework=framework)


def get_current_version(application_id):
    return database.get_current_version(application_id)


def list_versions(application_id, limit=50):
    return database.list_versions(application_id, limit=limit)


def get_version(version_id):
    return database.get_version(version_id)


def set_current(application_id, version_id):
    database.set_current_version(application_id, version_id)
