import sqlite3
import threading
import os
import json
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse

DB_NAME = os.environ.get("AUTOPOD_DB_PATH", "autopod.db")

# Use thread-local storage for database connections
thread_local = threading.local()

def get_db_connection():
    """Get a database connection for the current thread."""
    if not hasattr(thread_local, 'connection'):
        thread_local.connection = sqlite3.connect(DB_NAME, timeout=30.0)
        thread_local.connection.row_factory = sqlite3.Row
    return thread_local.connection

@contextmanager
def get_db_cursor():
    """Context manager for database operations with proper error handling."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()

def init_db():
    """Initialize the database tables and apply non-destructive schema migrations."""
    with get_db_cursor() as cursor:
        # Create containers table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS containers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_name TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Create logs table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_name TEXT,
            log TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # --- AutoPod Cloud applications table ---
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            repo_url TEXT,
            target_port TEXT,
            health_check_path TEXT NOT NULL DEFAULT '/',
            public_url TEXT,
            framework TEXT NOT NULL DEFAULT 'auto-detect',
            build_config TEXT DEFAULT '{}',
            env_vars TEXT DEFAULT '{}',
            routing_path TEXT NOT NULL DEFAULT '/',
            health_status TEXT NOT NULL DEFAULT 'unknown',
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            target_node TEXT,
            container_name TEXT,
            image_tag TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Migration for applications (SQLite has no "ADD COLUMN IF NOT EXISTS")
        cursor.execute("PRAGMA table_info(applications)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        app_migrations = [
            ("target_port", "ALTER TABLE applications ADD COLUMN target_port TEXT"),
            ("health_check_path", "ALTER TABLE applications ADD COLUMN health_check_path TEXT NOT NULL DEFAULT '/'"),
            ("public_url", "ALTER TABLE applications ADD COLUMN public_url TEXT"),
            ("framework", "ALTER TABLE applications ADD COLUMN framework TEXT NOT NULL DEFAULT 'auto-detect'"),
            ("build_config", "ALTER TABLE applications ADD COLUMN build_config TEXT DEFAULT '{}'"),
            ("env_vars", "ALTER TABLE applications ADD COLUMN env_vars TEXT DEFAULT '{}'"),
            ("routing_path", "ALTER TABLE applications ADD COLUMN routing_path TEXT NOT NULL DEFAULT '/'"),
            ("health_status", "ALTER TABLE applications ADD COLUMN health_status TEXT NOT NULL DEFAULT 'unknown'"),
            ("lifecycle_status", "ALTER TABLE applications ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'"),
            ("target_node", "ALTER TABLE applications ADD COLUMN target_node TEXT"),
            ("container_name", "ALTER TABLE applications ADD COLUMN container_name TEXT"),
            ("image_tag", "ALTER TABLE applications ADD COLUMN image_tag TEXT"),
            ("updated_at", "ALTER TABLE applications ADD COLUMN updated_at TIMESTAMP"),
        ]
        for col_name, sql in app_migrations:
            if col_name not in existing_cols:
                cursor.execute(sql)

        # Ensure updated_at is populated for any legacy records
        cursor.execute("UPDATE applications SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")

        # Backfill Sweet Treats Bakery if present to guarantee zero disruption
        cursor.execute("""
        UPDATE applications
        SET framework = CASE WHEN framework IS NULL OR framework = 'auto-detect' THEN 'static' ELSE framework END,
            target_port = CASE WHEN target_port IS NULL THEN '8081' ELSE target_port END,
            routing_path = CASE WHEN routing_path IS NULL THEN '/' ELSE routing_path END,
            container_name = CASE WHEN container_name IS NULL THEN 'autopod-sweet-treats-bakery-container' ELSE container_name END,
            health_check_path = CASE WHEN health_check_path IS NULL THEN '/' ELSE health_check_path END
        WHERE name = 'sweet-treats-bakery'
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            image_tag TEXT NOT NULL,
            git_commit TEXT,
            framework TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_current INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (application_id) REFERENCES applications(id)
        )
        """)

        cursor.execute("PRAGMA table_info(versions)")
        existing_version_cols = {row[1] for row in cursor.fetchall()}
        if "framework" not in existing_version_cols:
            cursor.execute("ALTER TABLE versions ADD COLUMN framework TEXT")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            version_id INTEGER,
            status TEXT NOT NULL DEFAULT 'in_progress',
            is_rollback INTEGER NOT NULL DEFAULT 0,
            triggered_by TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            message TEXT,
            target_node TEXT,
            port INTEGER,
            container_name TEXT,
            image_tag TEXT,
            FOREIGN KEY (application_id) REFERENCES applications(id),
            FOREIGN KEY (version_id) REFERENCES versions(id)
        )
        """)

        # Migration: target_node, port, container_name, image_tag for deployments
        cursor.execute("PRAGMA table_info(deployments)")
        existing_deploy_cols = {row[1] for row in cursor.fetchall()}
        deploy_migrations = [
            ("target_node", "ALTER TABLE deployments ADD COLUMN target_node TEXT"),
            ("port", "ALTER TABLE deployments ADD COLUMN port INTEGER"),
            ("container_name", "ALTER TABLE deployments ADD COLUMN container_name TEXT"),
            ("image_tag", "ALTER TABLE deployments ADD COLUMN image_tag TEXT"),
        ]
        for col_name, sql in deploy_migrations:
            if col_name not in existing_deploy_cols:
                cursor.execute(sql)

        # ARCHITECTURE.md Section 6.3: a lightweight record of which
        # Auto-Scaling-managed EC2 IPs AutoPod currently knows about. This
        # table is intentionally NOT trusted as the source of truth for
        # "what's alive" -- it's refreshed from the AWS API
        # (describe-auto-scaling-groups) by code that doesn't exist yet
        # (multi-node rollout is a later implementation phase; see
        # PROJECT_PROGRESS.md). The schema is added now so later work
        # doesn't need another migration, and so `target_node` on
        # `deployments` has something meaningful to reference.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL UNIQUE,
            instance_id TEXT,
            status TEXT NOT NULL DEFAULT 'in_service',
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cursor.execute("PRAGMA table_info(app_nodes)")
        app_nodes_cols = [c["name"] for c in cursor.fetchall()]
        if "instance_id" not in app_nodes_cols:
            cursor.execute("ALTER TABLE app_nodes ADD COLUMN instance_id TEXT")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id TEXT NOT NULL UNIQUE,
            event_type TEXT,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'processed'
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_delivery_id ON webhook_deliveries (delivery_id)")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS deployment_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            commit_sha TEXT,
            branch TEXT DEFAULT 'main',
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            triggered_by TEXT DEFAULT 'webhook',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            error_message TEXT,
            FOREIGN KEY (application_id) REFERENCES applications(id)
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deployment_queue_app_status ON deployment_queue (application_id, status)")


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

_APPLICATION_COLUMNS = "id, name, repo_url, target_port, health_check_path, public_url, framework, build_config, env_vars, routing_path, health_status, lifecycle_status, target_node, container_name, image_tag, created_at, updated_at"


def _serialize_json_field(val):
    """Serialize a field to a JSON string if dict or list, or return string/default."""
    if val is None:
        return "{}"
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    if isinstance(val, str):
        return val.strip() or "{}"
    return str(val)


def _format_app_row(row):
    """Format an application row as a dictionary with env_vars_json alias."""
    if not row:
        return None
    d = dict(row)
    if "env_vars" in d and "env_vars_json" not in d:
        d["env_vars_json"] = d["env_vars"]
    return d


def normalize_repo_url(url):
    """Normalize a repository URL for consistent storage and lookup.

    Strips whitespace, trailing slashes, and any '.git' suffix.
    Normalizes scheme and host to lowercase while preserving repository path case.
    Handles HTTPS, HTTP, and SSH git@... formats.
    Returns None if url is empty or None.
    """
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip()
    if not cleaned:
        return None

    # Strip trailing slashes first
    cleaned = cleaned.rstrip('/')

    # If HTTP/HTTPS URL, normalize scheme and netloc to lowercase
    if '://' in cleaned:
        try:
            parsed = urlparse(cleaned)
            clean_path = parsed.path.rstrip('/')
            if clean_path.endswith('.git'):
                clean_path = clean_path[:-4].rstrip('/')
            return urlunparse((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                clean_path,
                parsed.params,
                parsed.query,
                parsed.fragment
            ))
        except Exception:
            pass

    # Handle SSH git@github.com:owner/repo.git or non-standard URLs
    if cleaned.endswith('.git'):
        cleaned = cleaned[:-4].rstrip('/')

    return cleaned


def get_or_create_application(name, repo_url=None):
    """Return the application row for `name`, creating it if needed.

    This is the "convenience" path used for (a) payloads with no repo_url at
    all (synthetic/test/demo deploys, where there is nothing to pre-register)
    and (b) authenticated admin-triggered deploys, where a human operator is
    explicitly naming the app they want created. It intentionally does NOT
    set target_port/health_check_path -- use `register_application()` for
    that. See `find_application_by_repo_url` / deployment_engine.deploy for
    the separate, stricter path used by real GitHub webhook pushes, which
    must NOT silently register new applications (ARCHITECTURE.md Section 8).
    """
    norm_repo_url = normalize_repo_url(repo_url)
    slug = name.strip().lower().replace(' ', '-')
    default_routing_path = '/' if slug == 'sweet-treats-bakery' else f"/{slug}/"
    default_framework = 'static' if slug == 'sweet-treats-bakery' else 'auto-detect'
    default_container_name = f"autopod-{slug}-container"
    default_port = '8081' if slug == 'sweet-treats-bakery' else None

    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE name = ?", (name,))
        row = cursor.fetchone()
        if row:
            if norm_repo_url and not row["repo_url"]:
                cursor.execute(
                    "UPDATE applications SET repo_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (norm_repo_url, row["id"])
                )
            cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE id = ?", (row["id"],))
            return _format_app_row(cursor.fetchone())
        cursor.execute(
            """INSERT INTO applications
               (name, repo_url, target_port, health_check_path, framework, build_config, env_vars,
                routing_path, health_status, lifecycle_status, container_name)
               VALUES (?, ?, ?, '/', ?, '{}', '{}', ?, 'unknown', 'active', ?)""",
            (name, norm_repo_url, default_port, default_framework, default_routing_path, default_container_name)
        )
        app_id = cursor.lastrowid
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE id = ?", (app_id,))
        return _format_app_row(cursor.fetchone())


def register_application(name, repo_url=None, target_port=None, health_check_path="/", public_url=None,
                         framework="auto-detect", build_config=None, env_vars=None, env_vars_json=None,
                         routing_path=None, health_status="unknown", lifecycle_status="active",
                         target_node=None, container_name=None, image_tag=None, idempotent=False):
    """Explicitly register a new application (used by POST /api/applications).

    Unlike `get_or_create_application`, this is NOT idempotent-by-name unless
    idempotent=True is specified: it raises ValueError if an application with this
    name OR an equivalent repo_url already exists.
    """
    if not name or not name.strip():
        raise ValueError("Application name is required")

    norm_repo_url = normalize_repo_url(repo_url)
    slug = name.strip().lower().replace(' ', '-')

    # Determine routing_path default
    if routing_path is None:
        if str(target_port) == '8081' or slug == 'sweet-treats-bakery':
            routing_path = '/'
        else:
            routing_path = f"/{slug}/"
    else:
        rp = routing_path.strip()
        if rp != '/':
            if not rp.startswith('/'):
                rp = '/' + rp
            if not rp.endswith('/'):
                rp = rp + '/'
        routing_path = rp

    if not container_name:
        container_name = f"autopod-{slug}-container"

    if env_vars is None and env_vars_json is not None:
        env_vars = env_vars_json
    env_vars_str = _serialize_json_field(env_vars)
    build_config_str = _serialize_json_field(build_config)

    # Determine default public_url if not provided
    if not public_url and os.environ.get("AUTOPOD_ALB_URL"):
        alb_base = os.environ["AUTOPOD_ALB_URL"].strip().rstrip('/')
        if routing_path == '/':
            public_url = f"{alb_base}/"
        else:
            clean_path = routing_path.strip('/')
            public_url = f"{alb_base}/{clean_path}/"

    with get_db_cursor() as cursor:
        cursor.execute("SELECT id FROM applications WHERE name = ?", (name,))
        existing_name_row = cursor.fetchone()
        if existing_name_row:
            if idempotent:
                return update_application(
                    existing_name_row["id"],
                    repo_url=repo_url, target_port=target_port, health_check_path=health_check_path,
                    public_url=public_url, framework=framework, build_config=build_config,
                    env_vars=env_vars, routing_path=routing_path, health_status=health_status,
                    lifecycle_status=lifecycle_status, target_node=target_node,
                    container_name=container_name, image_tag=image_tag
                )
            raise ValueError(f"An application named '{name}' is already registered")

        if norm_repo_url:
            cursor.execute("SELECT id, repo_url FROM applications WHERE repo_url IS NOT NULL")
            for row_id, existing_url in cursor.fetchall():
                if normalize_repo_url(existing_url) == norm_repo_url:
                    if idempotent:
                        return update_application(
                            row_id,
                            name=name, target_port=target_port, health_check_path=health_check_path,
                            public_url=public_url, framework=framework, build_config=build_config,
                            env_vars=env_vars, routing_path=routing_path, health_status=health_status,
                            lifecycle_status=lifecycle_status, target_node=target_node,
                            container_name=container_name, image_tag=image_tag
                        )
                    raise ValueError(f"Repository '{repo_url}' is already registered to another application")

        cursor.execute(
            """INSERT INTO applications
               (name, repo_url, target_port, health_check_path, public_url,
                framework, build_config, env_vars, routing_path,
                health_status, lifecycle_status, target_node, container_name, image_tag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, norm_repo_url, str(target_port) if target_port else None, health_check_path or "/", public_url,
             framework or "auto-detect", build_config_str, env_vars_str, routing_path,
             health_status or "unknown", lifecycle_status or "active", target_node, container_name, image_tag)
        )
        app_id = cursor.lastrowid
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE id = ?", (app_id,))
        return _format_app_row(cursor.fetchone())


def allocate_next_target_port(start_port=8081, max_port=8099, extra_used_ports=None):
    """Find the next available target port in the worker port range (8081-8099)
    that is not already assigned to any registered application or active container."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT target_port FROM applications WHERE target_port IS NOT NULL")
        used_ports = set()
        if extra_used_ports:
            for p in extra_used_ports:
                try:
                    used_ports.add(int(p))
                except (ValueError, TypeError):
                    pass
        for row in cursor.fetchall():
            try:
                used_ports.add(int(row["target_port"]))
            except (ValueError, TypeError):
                pass
        for port in range(start_port, max_port + 1):
            if port not in used_ports:
                return port
    return None


def allocate_next_routing_path(app_name):
    """Determine the standard routing path for an application.
    Sweet Treats Bakery gets root path '/', while other applications get '/<slug>/'."""
    if not app_name:
        return "/"
    slug = app_name.strip().lower().replace(' ', '-').replace('_', '-')
    slug = ''.join(c for c in slug if c.isalnum() or c == '-')
    if slug in ('sweet-treats-bakery', 'bakery', 'root'):
        return '/'
    return f"/{slug}/"


def find_application_by_repo_url(repo_url):
    """Look up an application by its repo_url. Supports equivalent URL formats
    (.git suffixes, trailing slashes, scheme/host case variations).
    Returns None if no application has been registered for this repository -- callers on the
    webhook path use this to REJECT pushes from unregistered repos rather
    than silently creating a new application for them."""
    if not repo_url:
        return None
    norm_target = normalize_repo_url(repo_url)
    if not norm_target:
        return None

    with get_db_cursor() as cursor:
        # 1. Direct match on normalized URL
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE repo_url = ?", (norm_target,))
        row = cursor.fetchone()
        if row:
            return _format_app_row(row)

        # 2. Direct match with raw repo_url (for unnormalized legacy records)
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE repo_url = ?", (repo_url,))
        row = cursor.fetchone()
        if row:
            return _format_app_row(row)

        # 3. Fallback scan comparing normalized form across all registered apps
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE repo_url IS NOT NULL")
        for r in cursor.fetchall():
            if normalize_repo_url(r["repo_url"]) == norm_target:
                return _format_app_row(r)

        return None


def get_application(application_id):
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE id = ?", (application_id,))
        row = cursor.fetchone()
        return _format_app_row(row) if row else None


def get_application_by_name(name):
    """Look up an application by name."""
    if not name:
        return None
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE name = ?", (name,))
        row = cursor.fetchone()
        return _format_app_row(row) if row else None


def update_application(application_id, name=None, repo_url=None, target_port=None, health_check_path=None,
                       public_url=None, framework=None, build_config=None, env_vars=None, env_vars_json=None,
                       routing_path=None, health_status=None, lifecycle_status=None, target_node=None,
                       container_name=None, image_tag=None):
    """Update fields on an existing application."""
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if repo_url is not None:
        norm_repo_url = normalize_repo_url(repo_url)
        updates.append("repo_url = ?")
        params.append(norm_repo_url)
    if target_port is not None:
        updates.append("target_port = ?")
        params.append(str(target_port) if target_port else None)
    if health_check_path is not None:
        updates.append("health_check_path = ?")
        params.append(health_check_path or "/")
    if public_url is not None:
        updates.append("public_url = ?")
        params.append(public_url)
    if framework is not None:
        updates.append("framework = ?")
        params.append(framework)
    if build_config is not None:
        updates.append("build_config = ?")
        params.append(_serialize_json_field(build_config))
    if env_vars is None and env_vars_json is not None:
        env_vars = env_vars_json
    if env_vars is not None:
        updates.append("env_vars = ?")
        params.append(_serialize_json_field(env_vars))
    if routing_path is not None:
        rp = routing_path.strip()
        if rp != '/':
            if not rp.startswith('/'):
                rp = '/' + rp
            if not rp.endswith('/'):
                rp = rp + '/'
        updates.append("routing_path = ?")
        params.append(rp)
    if health_status is not None:
        updates.append("health_status = ?")
        params.append(health_status)
    if lifecycle_status is not None:
        updates.append("lifecycle_status = ?")
        params.append(lifecycle_status)
    if target_node is not None:
        updates.append("target_node = ?")
        params.append(target_node)
    if container_name is not None:
        updates.append("container_name = ?")
        params.append(container_name)
    if image_tag is not None:
        updates.append("image_tag = ?")
        params.append(image_tag)

    if not updates:
        return get_application(application_id)

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(application_id)
    with get_db_cursor() as cursor:
        cursor.execute(f"UPDATE applications SET {', '.join(updates)} WHERE id = ?", tuple(params))
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE id = ?", (application_id,))
        row = cursor.fetchone()
        return _format_app_row(row)


def delete_application(application_id):
    """Delete an application and its associated version and deployment records."""
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM deployments WHERE application_id = ?", (application_id,))
        cursor.execute("DELETE FROM versions WHERE application_id = ?", (application_id,))
        cursor.execute("DELETE FROM applications WHERE id = ?", (application_id,))
        return cursor.rowcount > 0


def list_applications():
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT {_APPLICATION_COLUMNS} FROM applications ORDER BY created_at DESC")
        return [_format_app_row(r) for r in cursor.fetchall()]


def resolve_container_public_url(container_name):
    """Resolve the public web URL for a container.

    Priority:
    1. Direct application match in database `applications` table -> `public_url`
    2. Environment variable: APP_PUBLIC_URL_<APP_NAME> (e.g. APP_PUBLIC_URL_SWEET_TREATS_BAKERY)
    3. Application-specific or generic ALB fallback from environment: AUTOPOD_ALB_URL
    4. None (for internal platform services like autopod-backend, autopod-frontend, etc.)
    """
    if not container_name:
        return None

    c_name = container_name.strip().lstrip('/').lower()

    # Internal platform services never have a public application URL
    internal_services = {'autopod-backend', 'autopod-frontend', 'autopod-webhook-proxy', 'autopod-db'}
    for internal in internal_services:
        if c_name == internal or c_name.startswith(f"{internal}-"):
            return None

    # Query registered applications
    try:
        apps = list_applications()
    except Exception:
        apps = []

    matched_app = None
    for app in apps:
        app_name = (app.get('name') or '').strip().lower().replace(' ', '-')
        if not app_name:
            continue
        expected_container = f"autopod-{app_name}-container"
        alt_container = f"{app_name}-container"
        if c_name in (expected_container, alt_container, app_name) or app_name in c_name:
            matched_app = app
            break

    # 1. Database public_url column (must be a valid public URL, never localhost/127.0.0.1)
    if matched_app and matched_app.get("public_url"):
        candidate = matched_app["public_url"].strip()
        if candidate and "localhost" not in candidate.lower() and "127.0.0.1" not in candidate:
            return candidate

    # 2. Environment variable: APP_PUBLIC_URL_<APP_NAME>
    if matched_app:
        env_key = f"APP_PUBLIC_URL_{matched_app['name'].upper().replace('-', '_').replace(' ', '_')}"
        if os.environ.get(env_key):
            candidate = os.environ[env_key].strip()
            if candidate and "localhost" not in candidate.lower() and "127.0.0.1" not in candidate:
                return candidate

    # 3. Sweet treats bakery fallback (preserves existing live deployment behind ALB port 8081)
    if 'sweet-treats-bakery' in c_name or (matched_app and 'sweet-treats-bakery' in matched_app.get('name', '')):
        if os.environ.get("APP_PUBLIC_URL_SWEET_TREATS_BAKERY"):
            candidate = os.environ["APP_PUBLIC_URL_SWEET_TREATS_BAKERY"].strip()
            if candidate and "localhost" not in candidate.lower() and "127.0.0.1" not in candidate:
                return candidate
        if os.environ.get("AUTOPOD_ALB_URL"):
            candidate = os.environ["AUTOPOD_ALB_URL"].strip()
            if candidate and "localhost" not in candidate.lower() and "127.0.0.1" not in candidate:
                return candidate

    # P0.3: Never invent a public URL for applications without verified ingress routing.
    # Returns None so callers produce an explicit unavailable-routing state (HTTP 409).
    return None


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def create_version(application_id, image_tag, git_commit=None, framework=None):
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO versions (application_id, image_tag, git_commit, framework) VALUES (?, ?, ?, ?)",
            (application_id, image_tag, git_commit, framework)
        )
        version_id = cursor.lastrowid
        cursor.execute("SELECT * FROM versions WHERE id = ?", (version_id,))
        return dict(cursor.fetchone())


def get_version(version_id):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM versions WHERE id = ?", (version_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_versions(application_id=None, limit=50):
    with get_db_cursor() as cursor:
        if application_id is not None:
            cursor.execute(
                "SELECT * FROM versions WHERE application_id = ? ORDER BY created_at DESC LIMIT ?",
                (application_id, limit)
            )
        else:
            cursor.execute("SELECT * FROM versions ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cursor.fetchall()]


def set_current_version(application_id, version_id):
    """Mark `version_id` as the current version for `application_id`,
    unmarking any previously-current version for the same application."""
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE versions SET is_current = 0 WHERE application_id = ?",
            (application_id,)
        )
        cursor.execute(
            "UPDATE versions SET is_current = 1 WHERE id = ?",
            (version_id,)
        )


def get_current_version(application_id):
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM versions WHERE application_id = ? AND is_current = 1 LIMIT 1",
            (application_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------

def create_deployment(application_id, version_id=None, status="in_progress",
                      is_rollback=False, triggered_by="webhook", message=None, target_node=None,
                      port=None, container_name=None, image_tag=None):
    """`target_node` identifies which app node this deployment row refers to.
    `port`, `container_name`, and `image_tag` capture runtime container metadata."""
    with get_db_cursor() as cursor:
        if image_tag is None and version_id is not None:
            cursor.execute("SELECT image_tag FROM versions WHERE id = ?", (version_id,))
            vrow = cursor.fetchone()
            if vrow:
                image_tag = vrow["image_tag"]

        cursor.execute(
            """INSERT INTO deployments
               (application_id, version_id, status, is_rollback, triggered_by, message, target_node, port, container_name, image_tag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (application_id, version_id, status, int(bool(is_rollback)), triggered_by, message, target_node,
             port, container_name, image_tag)
        )
        deployment_id = cursor.lastrowid
        cursor.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        return dict(cursor.fetchone())


def update_deployment(deployment_id, status=None, version_id=None, message=None, finished=False, target_node=None,
                      port=None, container_name=None, image_tag=None):
    fields, values = [], []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if version_id is not None:
        fields.append("version_id = ?")
        values.append(version_id)
    if message is not None:
        fields.append("message = ?")
        values.append(message)
    if target_node is not None:
        fields.append("target_node = ?")
        values.append(target_node)
    if port is not None:
        fields.append("port = ?")
        values.append(port)
    if container_name is not None:
        fields.append("container_name = ?")
        values.append(container_name)
    if image_tag is not None:
        fields.append("image_tag = ?")
        values.append(image_tag)
    if finished:
        fields.append("finished_at = CURRENT_TIMESTAMP")
    if not fields:
        return get_deployment(deployment_id)
    values.append(deployment_id)
    with get_db_cursor() as cursor:
        cursor.execute(f"UPDATE deployments SET {', '.join(fields)} WHERE id = ?", values)
        cursor.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# App nodes (Auto-Scaling-managed app instances -- ARCHITECTURE.md Section 6.3)
#
# NOT YET WIRED to anything live: no code currently populates this table
# from the AWS API, and no code currently performs a per-node deployment.
# These functions exist so the schema is in place ahead of that later
# implementation phase (see PROJECT_PROGRESS.md Section I, step 8/12).
# ---------------------------------------------------------------------------

def register_app_node(ip_address, status="in_service", instance_id=None):
    """Idempotent upsert: a re-registration of the same IP just refreshes
    its status/last_seen_at rather than erroring or duplicating. Optionally
    associates an AWS EC2 instance_id."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, instance_id FROM app_nodes WHERE ip_address = ?", (ip_address,))
        row = cursor.fetchone()
        if row:
            effective_instance_id = instance_id if instance_id is not None else row["instance_id"]
            cursor.execute(
                "UPDATE app_nodes SET status = ?, instance_id = ?, last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, effective_instance_id, row["id"])
            )
            node_id = row["id"]
        else:
            cursor.execute(
                "INSERT INTO app_nodes (ip_address, status, instance_id) VALUES (?, ?, ?)",
                (ip_address, status, instance_id)
            )
            node_id = cursor.lastrowid
        cursor.execute("SELECT * FROM app_nodes WHERE id = ?", (node_id,))
        return dict(cursor.fetchone())


def list_app_nodes(status=None):
    with get_db_cursor() as cursor:
        if status:
            cursor.execute("SELECT * FROM app_nodes WHERE status = ? ORDER BY registered_at", (status,))
        else:
            cursor.execute("SELECT * FROM app_nodes ORDER BY registered_at")
        return [dict(r) for r in cursor.fetchall()]


def set_app_node_status(ip_address, status):
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE app_nodes SET status = ?, last_seen_at = CURRENT_TIMESTAMP WHERE ip_address = ?",
            (status, ip_address)
        )
        cursor.execute("SELECT * FROM app_nodes WHERE ip_address = ?", (ip_address,))
        row = cursor.fetchone()
        return dict(row) if row else None


_DEPLOYMENT_COLUMNS = """
    d.id, d.application_id, d.version_id, d.status, d.is_rollback,
    d.triggered_by, d.started_at, d.finished_at, d.message, d.target_node,
    d.port, d.container_name, COALESCE(d.image_tag, v.image_tag) AS image_tag, v.git_commit
"""


def get_deployment(deployment_id):
    with get_db_cursor() as cursor:
        cursor.execute(f"""
            SELECT {_DEPLOYMENT_COLUMNS}
            FROM deployments d
            LEFT JOIN versions v ON v.id = d.version_id
            WHERE d.id = ?
        """, (deployment_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_deployments(application_id=None, limit=50, offset=0):
    with get_db_cursor() as cursor:
        if application_id is not None:
            cursor.execute(f"""
                SELECT {_DEPLOYMENT_COLUMNS}
                FROM deployments d
                LEFT JOIN versions v ON v.id = d.version_id
                WHERE d.application_id = ?
                ORDER BY d.started_at DESC LIMIT ? OFFSET ?
            """, (application_id, limit, offset))
        else:
            cursor.execute(f"""
                SELECT {_DEPLOYMENT_COLUMNS}
                FROM deployments d
                LEFT JOIN versions v ON v.id = d.version_id
                ORDER BY d.started_at DESC LIMIT ? OFFSET ?
            """, (limit, offset))
        return [dict(r) for r in cursor.fetchall()]


def get_container_status():
    """Get container statuses from database."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT container_name, status, created_at FROM containers ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [{"container_name": r[0], "status": r[1], "created_at": r[2]} for r in rows]

def get_container_logs():
    """Get container logs from database."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT container_name, log, timestamp FROM logs ORDER BY timestamp DESC LIMIT 100")
        rows = cursor.fetchall()
        return [{"container_name": r[0], "log": r[1], "timestamp": r[2]} for r in rows]

def record_webhook_delivery(delivery_id, event_type=None):
    """Atomically record a webhook delivery ID.

    Returns True if newly recorded, or False if this delivery ID was already
    processed or is currently being processed by a concurrent request.
    Uses SQLite's UNIQUE constraint on `delivery_id` for thread-safe atomicity.
    """
    if not delivery_id:
        return True
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO webhook_deliveries (delivery_id, event_type) VALUES (?, ?)",
                (delivery_id, event_type)
            )
        return True
    except sqlite3.IntegrityError:
        return False


def is_webhook_delivery_processed(delivery_id):
    """Check whether a delivery ID was already recorded."""
    if not delivery_id:
        return False
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id FROM webhook_deliveries WHERE delivery_id = ?", (delivery_id,))
        return cursor.fetchone() is not None


def get_webhook_delivery(delivery_id):
    """Get delivery details for a given delivery ID."""
    if not delivery_id:
        return None
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM webhook_deliveries WHERE delivery_id = ?", (delivery_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_webhook_deliveries(limit=20):
    """List recent webhook deliveries ordered by most recent."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM webhook_deliveries ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Deployment Queue Functions
# ---------------------------------------------------------------------------

def enqueue_deployment(application_id, payload_json, commit_sha=None, branch="main", triggered_by="webhook"):
    """Enqueue a deployment job for background processing."""
    if isinstance(payload_json, dict):
        payload_json = json.dumps(payload_json)
    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO deployment_queue (application_id, commit_sha, branch, payload_json, status, triggered_by)
            VALUES (?, ?, ?, ?, 'queued', ?)
        """, (application_id, commit_sha, branch, payload_json, triggered_by))
        queue_id = cursor.lastrowid
        cursor.execute("SELECT * FROM deployment_queue WHERE id = ?", (queue_id,))
        return dict(cursor.fetchone())


def claim_next_queued_deployment():
    """Atomically retrieve and claim the next pending deployment job in FIFO order."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id FROM deployment_queue 
            WHERE status = 'queued' 
            ORDER BY id ASC LIMIT 1
        """)
        row = cursor.fetchone()
        if not row:
            return None
        job_id = row["id"]
        cursor.execute("""
            UPDATE deployment_queue 
            SET status = 'processing', started_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND status = 'queued'
        """, (job_id,))
        if cursor.rowcount == 0:
            return None
        cursor.execute("SELECT * FROM deployment_queue WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_next_queued_deployment():
    """Retrieve the next pending deployment job in FIFO order (atomically claimed)."""
    return claim_next_queued_deployment()


def update_queued_deployment(queue_id, status, error_message=None):
    """Update status of a queued deployment job."""
    with get_db_cursor() as cursor:
        if status == 'processing':
            cursor.execute("""
                UPDATE deployment_queue 
                SET status = ?, started_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (status, queue_id))
        elif status in ('completed', 'failed', 'superseded'):
            cursor.execute("""
                UPDATE deployment_queue 
                SET status = ?, finished_at = CURRENT_TIMESTAMP, error_message = ? 
                WHERE id = ?
            """, (status, error_message, queue_id))
        else:
            cursor.execute("UPDATE deployment_queue SET status = ? WHERE id = ?", (status, queue_id))


def supersede_older_queued_deployments(application_id, current_queue_id):
    """Mark older queued (not yet started) deployments for the same application as superseded."""
    with get_db_cursor() as cursor:
        cursor.execute("""
            UPDATE deployment_queue 
            SET status = 'superseded', finished_at = CURRENT_TIMESTAMP, error_message = 'Superseded by newer commit' 
            WHERE application_id = ? AND status = 'queued' AND id < ?
        """, (application_id, current_queue_id))


def list_queued_deployments(limit=20):
    """List recent queued deployment entries."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM deployment_queue ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]


def get_application_by_container_name(container_name):
    """Find an application by its container name (with or without leading slash)."""
    if not container_name:
        return None
    clean = container_name.lstrip("/")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM applications 
            WHERE container_name = ? OR container_name = ?
            LIMIT 1
        """, (clean, f"/{clean}"))
        row = cursor.fetchone()
        return _format_app_row(row) if row else None


def close_db_connection():
    """Close the database connection for the current thread."""
    if hasattr(thread_local, 'connection'):
        thread_local.connection.close()
        del thread_local.connection