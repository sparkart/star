#!/usr/bin/env python3
"""Durable job, schedule and OAuth-state storage on SQLite.

Persistence only — nothing here runs a pipeline or talks to a provider, so it
can be unit-tested against a temp directory with no side effects.

Durability choices:
* WAL journal, so the background runner writing progress never blocks a reader.
* One short-lived connection per call. The HTTP server is threaded and SQLite
  connections are not safely shareable across threads; the cost of reconnecting
  is irrelevant at this request volume and it removes a whole class of bugs.
* `BEGIN IMMEDIATE` around the create path, which is what actually enforces the
  "exactly one active job" rule under concurrent POSTs.
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
STAGES = ("astro", "script", "audio", "video", "publish")
# Publishing is always an explicit opt-in: omitting `stages` runs everything
# that only touches this server, and never pushes anything to a platform.
DEFAULT_STAGES = ("astro", "script", "audio", "video")
PLATFORMS = ("youtube", "facebook", "line", "r2", "tiktok", "shopee")

# Platforms we can drive end-to-end versus ones that need a human in the loop.
AUTOMATABLE_PLATFORMS = ("youtube", "facebook", "line", "r2")
MANUAL_PLATFORMS = ("tiktok", "shopee")

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "blocked")
ALL_STATES = ACTIVE_STATES + TERMINAL_STATES

MAX_RANGE_DAYS = 31
MAX_JOBS_RETURNED = 100
MAX_EVENTS_RETURNED = 500
MAX_EVENT_MESSAGE = 4000

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Video customisation, one-off jobs only.
#
# "auto" is the default and means the overlay is derived from the script that
# was actually generated for that date and birth-day; there is no placeholder
# text anywhere in this system. "custom" means the operator typed one exact
# line that applies to every clip in the job.
OVERLAY_TEXT_MODES = ("auto", "custom")
DEFAULT_OVERLAY_TEXT_MODE = "auto"
MAX_CUSTOM_OVERLAY_TEXT = 220

# Asset ids are minted by the server (star_assets), never by a caller; the job
# input carries the id and nothing else — no path, no bytes, no filename.
ASSET_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# NUL and the C0/C1 control ranges, minus the whitespace a human may type.
_CONTROL_CHARS = frozenset(
    chr(code) for code in list(range(0x00, 0x20)) + [0x7F] + list(range(0x80, 0xA0))
    if chr(code) not in "\t\n\r")


class JobValidationError(Exception):
    """Caller-supplied job/schedule input was rejected. Maps to HTTP 400."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class JobConflict(Exception):
    """Another job is already active. Maps to HTTP 409."""

    def __init__(self, message, active):
        super().__init__(message)
        self.message = message
        self.active = active


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_job_id():
    return uuid.uuid4().hex


# ── validation ────────────────────────────────────────────────────────

def _valid_date(value, field):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise JobValidationError("%s must be YYYY-MM-DD" % field, field)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise JobValidationError("%s is not a real calendar date" % field, field)


def _valid_bool(value, field, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise JobValidationError("%s must be true or false" % field, field)
    return value


def _valid_subset(value, allowed, field, allow_all=True, required=True):
    """Normalise 'all' / list input to a canonically ordered, de-duplicated list."""
    if value is None or (allow_all and value == "all"):
        if allow_all and (value == "all" or value is None):
            return list(allowed)
    if not isinstance(value, list):
        raise JobValidationError(
            "%s must be a list%s" % (field, " or \"all\"" if allow_all else ""), field)
    seen = []
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            raise JobValidationError(
                "%s contains an unknown value; allowed: %s" % (field, " ".join(allowed)),
                field)
        if item in seen:
            raise JobValidationError("%s contains duplicate %r" % (field, item), field)
        seen.append(item)
    if required and not seen:
        raise JobValidationError("%s must not be empty" % field, field)
    # Canonical order: stages run in pipeline order regardless of input order.
    return [item for item in allowed if item in seen]


def _valid_overlay(payload):
    """Normalise the overlay pair into (mode, text-or-None).

    The two fields are validated together because they constrain each other:
    custom mode without text is unsatisfiable, and text in auto mode is an
    instruction the renderer would silently ignore. Both are rejected rather
    than quietly repaired, so what the operator sees on the job is exactly what
    the video stage will draw.
    """
    mode = payload.get("overlay_text_mode")
    if mode is None:
        mode = DEFAULT_OVERLAY_TEXT_MODE
    if not isinstance(mode, str) or mode not in OVERLAY_TEXT_MODES:
        raise JobValidationError(
            "overlay_text_mode must be one of: %s" % " ".join(OVERLAY_TEXT_MODES),
            "overlay_text_mode")

    raw = payload.get("custom_overlay_text")
    if raw is not None and not isinstance(raw, str):
        raise JobValidationError("custom_overlay_text must be a string",
                                 "custom_overlay_text")
    text = raw.strip() if isinstance(raw, str) else ""

    if mode == "auto":
        if text:
            raise JobValidationError(
                "custom_overlay_text is only accepted when overlay_text_mode "
                "is \"custom\"; automatic mode takes its text from each clip's "
                "own generated script", "custom_overlay_text")
        return mode, None

    if not text:
        raise JobValidationError(
            "custom_overlay_text is required when overlay_text_mode is "
            "\"custom\"", "custom_overlay_text")
    if any(ch in _CONTROL_CHARS for ch in text):
        raise JobValidationError(
            "custom_overlay_text must not contain control characters",
            "custom_overlay_text")
    if len(text) > MAX_CUSTOM_OVERLAY_TEXT:
        raise JobValidationError(
            "custom_overlay_text exceeds %d characters" % MAX_CUSTOM_OVERLAY_TEXT,
            "custom_overlay_text")
    return mode, text


def _valid_background_asset_id(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not ASSET_ID_RE.fullmatch(value):
        raise JobValidationError(
            "background_asset_id must be 32 lowercase hex characters as "
            "returned by the upload endpoint", "background_asset_id")
    return value


def validate_job_input(payload):
    """Validate a job creation body and return the normalised input dict.

    Raises JobValidationError with a `field` so the API can point at the input.
    """
    if not isinstance(payload, dict):
        raise JobValidationError("request body must be a JSON object")

    from_date = _valid_date(payload.get("from_date"), "from_date")
    raw_to = payload.get("to_date") or payload.get("from_date")
    to_date = _valid_date(raw_to, "to_date")
    if to_date < from_date:
        raise JobValidationError("to_date must not be before from_date", "to_date")
    span = (to_date - from_date).days + 1
    if span > MAX_RANGE_DAYS:
        raise JobValidationError(
            "date range covers %d days; the maximum is %d" % (span, MAX_RANGE_DAYS),
            "to_date")

    days = _valid_subset(payload.get("days", "all"), DAYS, "days")
    stages = _valid_subset(payload.get("stages") if "stages" in payload
                           else list(DEFAULT_STAGES), STAGES, "stages")
    platforms = _valid_subset(payload.get("platforms", []), PLATFORMS, "platforms",
                              allow_all=True, required=False)
    if payload.get("platforms") is None:
        platforms = []

    dry_run = _valid_bool(payload.get("dry_run"), "dry_run", default=True)
    force = _valid_bool(payload.get("force"), "force", default=False)

    if "publish" in stages and not platforms:
        raise JobValidationError(
            "publish stage requires at least one platform", "platforms")

    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise JobValidationError("note must be a string", "note")
        if len(note) > 500:
            raise JobValidationError("note exceeds 500 characters", "note")

    overlay_mode, overlay_text = _valid_overlay(payload)
    background_asset_id = _valid_background_asset_id(payload.get("background_asset_id"))

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "dates": [(from_date + timedelta(days=i)).isoformat() for i in range(span)],
        "days": days,
        "stages": stages,
        "platforms": platforms,
        "dry_run": dry_run,
        "force": force,
        "note": note,
        "overlay_text_mode": overlay_mode,
        "custom_overlay_text": overlay_text,
        "background_asset_id": background_asset_id,
    }


def validate_schedule_input(payload):
    """Validate a schedule PUT body and return the normalised config."""
    if not isinstance(payload, dict):
        raise JobValidationError("request body must be a JSON object")

    enabled = _valid_bool(payload.get("enabled"), "enabled", default=False)

    run_time = payload.get("time", "05:30")
    if not isinstance(run_time, str) or not TIME_RE.match(run_time):
        raise JobValidationError("time must be HH:MM in 24-hour form", "time")

    offset = payload.get("date_offset_days", 0)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise JobValidationError("date_offset_days must be an integer", "date_offset_days")
    if not -31 <= offset <= 31:
        raise JobValidationError("date_offset_days must be between -31 and 31",
                                 "date_offset_days")

    days = _valid_subset(payload.get("days", "all"), DAYS, "days")
    stages = _valid_subset(payload.get("stages") if "stages" in payload
                           else list(DEFAULT_STAGES), STAGES, "stages")
    # A scheduled run is unattended by definition, and MANUAL_PLATFORMS have no
    # unattended publishing path — the pipeline only stages a handoff package
    # somebody finishes by hand. Accepting them here would let the UI advertise
    # a scheduled "publish" nobody is watching, so a schedule only accepts
    # AUTOMATABLE_PLATFORMS. One-off jobs still accept the manual platforms:
    # there, a human is present to pick the handoff package up.
    raw_platforms = payload.get("platforms", [])
    if isinstance(raw_platforms, list):
        blocked = [item for item in raw_platforms if item in MANUAL_PLATFORMS]
        if blocked:
            raise JobValidationError(
                "%s cannot be published on a schedule; %s require a manual upload, "
                "so a scheduled run would only stage a handoff package. Run them "
                "from a one-off job instead."
                % (", ".join(blocked), "they" if len(blocked) > 1 else "it"),
                "platforms")
    platforms = _valid_subset(raw_platforms, AUTOMATABLE_PLATFORMS, "platforms",
                              allow_all=True, required=False)
    if payload.get("platforms") is None:
        platforms = []
    dry_run = _valid_bool(payload.get("dry_run"), "dry_run", default=True)

    if "publish" in stages and not platforms:
        raise JobValidationError(
            "publish stage requires at least one platform", "platforms")

    return {
        "enabled": enabled,
        "time": run_time,
        "date_offset_days": offset,
        "days": days,
        "stages": stages,
        "platforms": platforms,
        "dry_run": dry_run,
        "timezone": "Asia/Bangkok",
    }


def valid_job_id(value):
    if not isinstance(value, str) or not JOB_ID_RE.match(value):
        raise JobValidationError("job id must be a 32-character hex string", "id")
    return value


# ── store ─────────────────────────────────────────────────────────────

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id               TEXT PRIMARY KEY,
        status           TEXT NOT NULL,
        progress         INTEGER NOT NULL DEFAULT 0,
        current_stage    TEXT,
        input_json       TEXT NOT NULL,
        result_json      TEXT,
        safe_error       TEXT,
        parent_id        TEXT,
        origin           TEXT NOT NULL DEFAULT 'manual',
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        started_at       TEXT,
        finished_at      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at)",
    """
    CREATE TABLE IF NOT EXISTS job_events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id   TEXT NOT NULL,
        ts       TEXT NOT NULL,
        level    TEXT NOT NULL,
        stage    TEXT,
        message  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id)",
    """
    CREATE TABLE IF NOT EXISTS schedule (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        enabled          INTEGER NOT NULL DEFAULT 0,
        run_time         TEXT NOT NULL DEFAULT '05:30',
        date_offset_days INTEGER NOT NULL DEFAULT 0,
        days             TEXT NOT NULL DEFAULT '',
        stages           TEXT NOT NULL DEFAULT '',
        platforms        TEXT NOT NULL DEFAULT '',
        dry_run          INTEGER NOT NULL DEFAULT 1,
        last_run_date    TEXT,
        updated_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedule_runs (
        run_date   TEXT PRIMARY KEY,
        job_id     TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_state (
        state         TEXT PRIMARY KEY,
        provider      TEXT NOT NULL,
        code_verifier TEXT NOT NULL,
        redirect_uri  TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        used_at       TEXT
    )
    """,
)


class JobStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_schema()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_schema(self):
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        conn = self.connect()
        try:
            for statement in SCHEMA:
                conn.execute(statement)
        finally:
            conn.close()
        # WAL sidecars inherit the db file mode, so tighten all of them.
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

    # -- jobs -----------------------------------------------------------
    def create_job(self, job_input, parent_id=None, origin="manual"):
        """Insert a queued job, refusing if one is already active.

        The active check and the insert share one IMMEDIATE transaction so two
        simultaneous POSTs cannot both observe an idle queue.
        """
        job_id = new_job_id()
        now = utcnow()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at LIMIT 1",
                ACTIVE_STATES).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                raise JobConflict("another job is already active", _job_row(row))
            conn.execute(
                "INSERT INTO jobs (id, status, progress, current_stage, input_json,"
                " parent_id, origin, created_at, updated_at)"
                " VALUES (?, 'queued', 0, NULL, ?, ?, ?, ?, ?)",
                (job_id, json.dumps(job_input, ensure_ascii=False), parent_id, origin,
                 now, now))
            conn.execute("COMMIT")
        finally:
            conn.close()
        self.add_event(job_id, "info", "job queued", stage=None)
        return self.get_job(job_id)

    def get_job(self, job_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        return _job_row(row) if row else None

    def list_jobs(self, limit=25, status=None):
        limit = max(1, min(int(limit), MAX_JOBS_RETURNED))
        conn = self.connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,)).fetchall()
        finally:
            conn.close()
        return [_job_row(row) for row in rows]

    def active_job(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at LIMIT 1",
                ACTIVE_STATES).fetchone()
        finally:
            conn.close()
        return _job_row(row) if row else None

    def claim_next(self):
        """Move the oldest queued job to running and return it, or None."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            now = utcnow()
            conn.execute(
                "UPDATE jobs SET status='running', started_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]))
            conn.execute("COMMIT")
            job_id = row["id"]
        finally:
            conn.close()
        return self.get_job(job_id)

    def update_progress(self, job_id, progress=None, current_stage=None):
        sets, args = ["updated_at = ?"], [utcnow()]
        if progress is not None:
            sets.append("progress = ?")
            args.append(max(0, min(100, int(progress))))
        if current_stage is not None:
            sets.append("current_stage = ?")
            args.append(current_stage)
        args.append(job_id)
        conn = self.connect()
        try:
            conn.execute("UPDATE jobs SET %s WHERE id = ?" % ", ".join(sets), args)
        finally:
            conn.close()

    def finish_job(self, job_id, status, safe_error=None, result=None, progress=None):
        if status not in TERMINAL_STATES:
            raise ValueError("not a terminal status: %r" % status)
        now = utcnow()
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE jobs SET status=?, safe_error=?, result_json=?, progress=?,"
                " finished_at=?, updated_at=?, current_stage=NULL WHERE id=?",
                (status, safe_error,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 progress if progress is not None else (100 if status == "succeeded" else 0),
                 now, now, job_id))
        finally:
            conn.close()

    def request_cancel(self, job_id):
        """Flag a job for cancellation; cancel outright if it never started.

        Returns (changed, job). A queued job is terminal immediately because no
        runner has picked it up; a running job is left to notice the flag so its
        subprocess can be torn down cooperatively.
        """
        now = utcnow()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False, None
            if row["status"] not in ACTIVE_STATES:
                conn.execute("ROLLBACK")
                return False, _job_row(row)
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE jobs SET status='cancelled', cancel_requested=1,"
                    " finished_at=?, updated_at=?, safe_error=? WHERE id=?",
                    (now, now, "cancelled before it started", job_id))
            else:
                conn.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                    (now, job_id))
            conn.execute("COMMIT")
        finally:
            conn.close()
        return True, self.get_job(job_id)

    def cancel_requested(self, job_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?",
                               (job_id,)).fetchone()
        finally:
            conn.close()
        return bool(row and row["cancel_requested"])

    def recover_orphans(self, message="interrupted by a service restart"):
        """Fail jobs left `running` by a crash. Called once at startup."""
        now = utcnow()
        conn = self.connect()
        try:
            rows = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.execute(
                    "UPDATE jobs SET status='failed', safe_error=?, finished_at=?,"
                    " updated_at=?, current_stage=NULL WHERE status='running'",
                    (message, now, now))
        finally:
            conn.close()
        for job_id in ids:
            self.add_event(job_id, "error", message)
        return ids

    # -- events ---------------------------------------------------------
    def add_event(self, job_id, level, message, stage=None):
        """Append one log line. The caller is responsible for redacting it;
        we truncate here so a runaway subprocess cannot fill the database."""
        if not isinstance(message, str):
            message = str(message)
        if len(message) > MAX_EVENT_MESSAGE:
            message = message[:MAX_EVENT_MESSAGE] + " [truncated]"
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO job_events (job_id, ts, level, stage, message)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, utcnow(), level, stage, message))
        finally:
            conn.close()

    def list_events(self, job_id, limit=200, after_id=0):
        limit = max(1, min(int(limit), MAX_EVENTS_RETURNED))
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? AND id > ?"
                " ORDER BY id LIMIT ?", (job_id, int(after_id), limit)).fetchall()
        finally:
            conn.close()
        return [{"id": r["id"], "ts": r["ts"], "level": r["level"],
                 "stage": r["stage"], "message": r["message"]} for r in rows]

    # -- schedule -------------------------------------------------------
    def get_schedule(self):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM schedule WHERE id = 1").fetchone()
        finally:
            conn.close()
        if row is None:
            # DEFAULT_STAGES, not STAGES: publish is opt-in, so the unsaved
            # default must omit it while `platforms` is empty. Returning the
            # full stage list here produced a default the caller could not PUT
            # back unchanged — validate_schedule_input rejects publish with no
            # platform — so the UI's own "save" of an untouched form 400'd.
            return {
                "enabled": False,
                "time": "05:30",
                "date_offset_days": 0,
                "days": list(DAYS),
                "stages": list(DEFAULT_STAGES),
                "platforms": [],
                "dry_run": True,
                "timezone": "Asia/Bangkok",
                "last_run_date": None,
                "updated_at": None,
            }
        return {
            "enabled": bool(row["enabled"]),
            "time": row["run_time"],
            "date_offset_days": row["date_offset_days"],
            "days": _split(row["days"]),
            "stages": _split(row["stages"]),
            "platforms": _split(row["platforms"]),
            "dry_run": bool(row["dry_run"]),
            "timezone": "Asia/Bangkok",
            "last_run_date": row["last_run_date"],
            "updated_at": row["updated_at"],
        }

    def set_schedule(self, config):
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO schedule (id, enabled, run_time, date_offset_days, days,"
                " stages, platforms, dry_run, updated_at)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,"
                " run_time=excluded.run_time, date_offset_days=excluded.date_offset_days,"
                " days=excluded.days, stages=excluded.stages,"
                " platforms=excluded.platforms, dry_run=excluded.dry_run,"
                " updated_at=excluded.updated_at",
                (int(config["enabled"]), config["time"], config["date_offset_days"],
                 ",".join(config["days"]), ",".join(config["stages"]),
                 ",".join(config["platforms"]), int(config["dry_run"]), utcnow()))
        finally:
            conn.close()
        return self.get_schedule()

    def claim_schedule_run(self, run_date):
        """Reserve today's scheduled run. False if it was already reserved.

        The PRIMARY KEY on run_date is the actual guard — two scheduler ticks in
        the same minute cannot both win the insert.
        """
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO schedule_runs (run_date, created_at) VALUES (?, ?)",
                (run_date, utcnow()))
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()
        conn = self.connect()
        try:
            conn.execute("UPDATE schedule SET last_run_date = ? WHERE id = 1", (run_date,))
        finally:
            conn.close()
        return True

    def attach_schedule_job(self, run_date, job_id):
        conn = self.connect()
        try:
            conn.execute("UPDATE schedule_runs SET job_id = ? WHERE run_date = ?",
                         (job_id, run_date))
        finally:
            conn.close()

    def release_schedule_run(self, run_date):
        """Undo a claim when the run could not actually be started."""
        conn = self.connect()
        try:
            conn.execute("DELETE FROM schedule_runs WHERE run_date = ?", (run_date,))
        finally:
            conn.close()

    # -- oauth ----------------------------------------------------------
    def put_oauth_state(self, state, provider, code_verifier, redirect_uri, ttl_seconds=600):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expires = now + timedelta(seconds=ttl_seconds)
        conn = self.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_state"
                " (state, provider, code_verifier, redirect_uri, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (state, provider, code_verifier, redirect_uri,
                 now.isoformat().replace("+00:00", "Z"),
                 expires.isoformat().replace("+00:00", "Z")))
        finally:
            conn.close()
        return expires

    def consume_oauth_state(self, state, provider):
        """Single-use + expiry check in one transaction.

        Returns (record, reason). `record` is None when the state is unknown,
        already used or expired; `reason` says which, without echoing the state.
        """
        now = datetime.now(timezone.utc)
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM oauth_state WHERE state = ? AND provider = ?",
                (state, provider)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None, "unknown"
            if row["used_at"]:
                conn.execute("ROLLBACK")
                return None, "already used"
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expires < now:
                conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,))
                conn.execute("COMMIT")
                return None, "expired"
            conn.execute("UPDATE oauth_state SET used_at = ? WHERE state = ?",
                         (utcnow(), state))
            conn.execute("COMMIT")
            return {"provider": row["provider"],
                    "code_verifier": row["code_verifier"],
                    "redirect_uri": row["redirect_uri"]}, "ok"
        finally:
            conn.close()

    def purge_oauth_states(self):
        conn = self.connect()
        try:
            conn.execute("DELETE FROM oauth_state WHERE expires_at < ?", (utcnow(),))
        finally:
            conn.close()


def _split(value):
    return [part for part in (value or "").split(",") if part]


def _job_row(row):
    if row is None:
        return None
    try:
        job_input = json.loads(row["input_json"])
    except (ValueError, TypeError):
        job_input = {}
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except ValueError:
            result = None
    return {
        "id": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "current_stage": row["current_stage"],
        "input": job_input,
        "result": result,
        "safe_error": row["safe_error"],
        "parent_id": row["parent_id"],
        "origin": row["origin"],
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
