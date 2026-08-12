#!/usr/bin/env python3
"""Star content-ops API — dependency-free, stdlib only.

Serves JSON on 127.0.0.1:9001 behind the site's nginx (which keeps Basic Auth).
Every number it reports is read from the filesystem at request time; nothing is
cached and nothing is invented. The regenerate route republishes CDN copies of
scripts that already exist on disk — it never generates text.

Layout it works with (relative to the project root):

    content/overrides/<date>/<day>.txt   hand-edited script (wins over source)
    content/scripts/claude_<date>_<day>.txt
                                         source script produced offline
    content/backups/<date>/<day>.<ts>.txt
                                         previous override, kept on overwrite
    cdn/star/<date>/<day>.txt            published copy served to the site
    cdn/star/manifest.json               day index the frontend reads
    output/<date>/...                    rendered media (audio/video) if any
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

VERSION = "1.0.0"
HOST = "127.0.0.1"
PORT = 9001

DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FIND_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
AUDIO_EXTS = (".mp3", ".wav", ".ogg")
VIDEO_EXTS = (".mp4", ".mov", ".webm")


def is_real_date(text):
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


MAX_BODY = 256 * 1024
MAX_SCRIPT = MAX_BODY

CONTENT_DIRS = ("content/overrides", "content/scripts", "content/backups", "cdn/star")


class ApiError(Exception):
    """Error with an HTTP status and a message safe to show to the operator."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ── validation ────────────────────────────────────────────────────────

def valid_date(value):
    """Return a strict YYYY-MM-DD string or raise ApiError(400)."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError(400, "date must be YYYY-MM-DD", field="date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, "date is not a real calendar date", field="date")
    return value


def valid_day(value):
    """Return one of DAYS or raise ApiError(400)."""
    if not isinstance(value, str) or value not in DAYS:
        raise ApiError(400, "day must be one of: " + " ".join(DAYS), field="day")
    return value


def valid_script(value):
    if not isinstance(value, str):
        raise ApiError(400, "script must be a string", field="script")
    if not value.strip():
        raise ApiError(400, "script must not be empty", field="script")
    if len(value.encode("utf-8")) > MAX_SCRIPT:
        raise ApiError(413, "script exceeds %d bytes" % MAX_SCRIPT, field="script")
    return value


# ── filesystem helpers ────────────────────────────────────────────────

def safe_join(root, *parts):
    """Join under root and refuse anything that escapes it.

    Validation already rejects traversal in date/day, but the containment
    check stays as the backstop that does not depend on the regexes.
    """
    root = os.path.realpath(root)
    for part in parts:
        if not isinstance(part, str) or not part or part in (".", ".."):
            raise ApiError(400, "invalid path component")
        if "/" in part or "\\" in part or "\x00" in part:
            raise ApiError(400, "invalid path component")
    target = os.path.realpath(os.path.join(root, *parts))
    if target != root and not target.startswith(root + os.sep):
        raise ApiError(400, "path escapes the project root")
    return target


def atomic_write(path, text):
    """Write text to path atomically (same-filesystem temp file + rename)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── store: all filesystem knowledge lives here ────────────────────────

class Store:
    def __init__(self, root):
        self.root = os.path.realpath(root)

    # paths -----------------------------------------------------------
    def override_path(self, date, day):
        return safe_join(self.root, "content", "overrides", date, day + ".txt")

    def source_path(self, date, day):
        return safe_join(self.root, "content", "scripts",
                         "claude_%s_%s.txt" % (date, day))

    def cdn_path(self, date, day):
        return safe_join(self.root, "cdn", "star", date, day + ".txt")

    def backup_path(self, date, day):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return safe_join(self.root, "content", "backups", date,
                         "%s.%s.txt" % (day, stamp))

    def manifest_path(self):
        return os.path.join(self.root, "cdn", "star", "manifest.json")

    # operations ------------------------------------------------------
    def resolve_source(self, date, day):
        """(path, kind) of the newest authoritative script, or (None, None)."""
        override = self.override_path(date, day)
        if os.path.isfile(override):
            return override, "override"
        source = self.source_path(date, day)
        if os.path.isfile(source):
            return source, "script"
        return None, None

    def save_script(self, date, day, script):
        """Persist an edited script to override, source and CDN copies."""
        override = self.override_path(date, day)
        backed_up = None
        if os.path.isfile(override):
            backup = self.backup_path(date, day)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(override, backup)
            backed_up = os.path.relpath(backup, self.root)

        written = []
        for path in (override, self.source_path(date, day), self.cdn_path(date, day)):
            atomic_write(path, script)
            written.append(os.path.relpath(path, self.root))
        return written, backed_up

    def publish(self, date, day):
        """Copy the on-disk source for one day into the CDN tree."""
        source, kind = self.resolve_source(date, day)
        if source is None:
            return None
        atomic_write(self.cdn_path(date, day), read_text(source))
        return kind

    def days_with_source(self, date):
        return [d for d in DAYS if self.resolve_source(date, d)[0] is not None]

    def known_dates(self):
        """Every date that has any override, source or published file."""
        dates = set()
        overrides = os.path.join(self.root, "content", "overrides")
        cdn = os.path.join(self.root, "cdn", "star")
        for base in (overrides, cdn):
            if os.path.isdir(base):
                for name in os.listdir(base):
                    if DATE_RE.match(name) and os.path.isdir(os.path.join(base, name)):
                        dates.add(name)
        scripts = os.path.join(self.root, "content", "scripts")
        if os.path.isdir(scripts):
            for name in os.listdir(scripts):
                m = re.match(r"^claude_(\d{4}-\d{2}-\d{2})_([a-z]{3})\.txt$", name)
                if m and m.group(2) in DAYS:
                    dates.add(m.group(1))
        return sorted(dates)

    def load_manifest(self):
        path = self.manifest_path()
        if not os.path.isfile(path):
            raise ApiError(404, "manifest.json not found")
        try:
            data = json.loads(read_text(path))
        except (ValueError, OSError) as exc:
            raise ApiError(500, "manifest.json is not readable JSON: %s" % exc)
        if not isinstance(data, dict):
            raise ApiError(500, "manifest.json must contain a JSON object")
        return data

    # asset scans ------------------------------------------------------
    def script_files(self):
        """Every .txt file that actually exists under content/scripts."""
        base = os.path.join(self.root, "content", "scripts")
        found = []
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if name.lower().endswith(".txt"):
                    found.append(os.path.join(dirpath, name))
        return found

    def script_dates(self):
        """Distinct real calendar dates named by script files or their dirs."""
        base = os.path.join(self.root, "content", "scripts")
        dates = set()
        for path in self.script_files():
            rel = os.path.relpath(path, base)
            for found in FIND_DATE_RE.findall(rel.replace(os.sep, "/")):
                if is_real_date(found):
                    dates.add(found)
        return sorted(dates)

    def media_files(self, extensions):
        """Every file under output/ whose extension is in `extensions`."""
        base = os.path.join(self.root, "output")
        found = []
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if os.path.splitext(name)[1].lower() in extensions:
                    found.append(os.path.join(dirpath, name))
        return found

    # reports ---------------------------------------------------------
    def stats(self):
        dates = self.known_dates()
        published = overrides = sources = complete = 0
        for date in dates:
            published_days = 0
            for day in DAYS:
                if os.path.isfile(self.override_path(date, day)):
                    overrides += 1
                if os.path.isfile(self.source_path(date, day)):
                    sources += 1
                if os.path.isfile(self.cdn_path(date, day)):
                    published += 1
                    published_days += 1
            if published_days == len(DAYS):
                complete += 1

        status_counts = {}
        manifest_days = 0
        manifest_ok = True
        try:
            manifest = self.load_manifest()
            days = manifest.get("days")
            if isinstance(days, list):
                manifest_days = len(days)
                for entry in days:
                    if isinstance(entry, dict):
                        key = str(entry.get("status", "unknown"))
                        status_counts[key] = status_counts.get(key, 0) + 1
        except ApiError:
            manifest_ok = False

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        upcoming = [d for d in dates if d >= today]
        script_files = self.script_files()
        return {
            # compatibility aliases for older dashboard clients
            "scripts": len(script_files),
            "days": len(self.script_dates()),
            "audio": len(self.media_files(AUDIO_EXTS)),
            "videos": len(self.media_files(VIDEO_EXTS)),
            "generated_at": utcnow(),
            "dates_total": len(dates),
            "dates_complete": complete,
            "dates_upcoming": len(upcoming),
            "scripts_total": sources,
            "overrides_total": overrides,
            "published_total": published,
            "manifest_days": manifest_days,
            "manifest_ok": manifest_ok,
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "next_date": upcoming[0] if upcoming else None,
            "status_counts": status_counts,
        }

    def health(self):
        checks = {}

        try:
            manifest = self.load_manifest()
            days = manifest.get("days")
            if isinstance(days, list):
                checks["manifest"] = {
                    "status": "ok",
                    "detail": "%d days indexed" % len(days),
                }
            else:
                checks["manifest"] = {
                    "status": "warn",
                    "detail": "valid JSON but no 'days' list",
                }
        except ApiError as exc:
            checks["manifest"] = {"status": "fail", "detail": exc.message}

        for rel in CONTENT_DIRS:
            checks[rel] = self._writable(rel)

        if any(c["status"] == "fail" for c in checks.values()):
            overall = "fail"
        elif any(c["status"] == "warn" for c in checks.values()):
            overall = "warn"
        else:
            overall = "ok"

        return {
            "status": overall,
            "version": VERSION,
            "checked_at": utcnow(),
            "root": self.root,
            "checks": checks,
        }

    def _writable(self, rel):
        path = os.path.join(self.root, rel)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return {"status": "fail", "detail": "cannot create: %s" % exc.strerror}
        try:
            fd, tmp = tempfile.mkstemp(dir=path, prefix=".healthcheck-")
            os.close(fd)
            os.unlink(tmp)
        except OSError as exc:
            return {"status": "fail", "detail": "not writable: %s" % exc.strerror}
        return {"status": "ok", "detail": "writable"}


# ── route handlers ────────────────────────────────────────────────────

def route_health(store, _body):
    report = store.health()
    return (200 if report["status"] != "fail" else 503), report


def route_manifest(store, _body):
    return 200, store.load_manifest()


def route_stats(store, _body):
    return 200, store.stats()


def require_object(body):
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    return body


def route_save_script(store, body):
    body = require_object(body)
    date = valid_date(body.get("date"))
    day = valid_day(body.get("day"))
    script = valid_script(body.get("script"))

    try:
        written, backup = store.save_script(date, day, script)
    except OSError as exc:
        raise ApiError(500, "write failed: %s" % exc.strerror)

    return 200, {
        "ok": True,
        "date": date,
        "day": day,
        "bytes": len(script.encode("utf-8")),
        "written": written,
        "backup": backup,
        "saved_at": utcnow(),
    }


def route_regenerate(store, body):
    """Republish CDN script files from existing override/source files.

    No text is created here: every published file is a byte copy of a script
    that already exists under content/. A day with no source is reported, not
    filled in.
    """
    body = require_object(body)
    date = valid_date(body.get("date"))

    raw_day = body.get("day", "all")
    if raw_day in (None, "", "all"):
        requested = list(DAYS)
        single = False
    else:
        requested = [valid_day(raw_day)]
        single = True

    available = [d for d in requested if store.resolve_source(date, d)[0] is not None]
    missing = [d for d in requested if d not in available]

    if not available:
        raise ApiError(
            404,
            "no source script on disk for %s%s" % (date, "/" + requested[0] if single else ""),
            date=date,
            missing=missing,
        )
    if missing:
        # Refuse a half-rebuild rather than publish an inconsistent week.
        raise ApiError(
            409,
            "sources missing for %d of %d days — nothing was published"
            % (len(missing), len(requested)),
            date=date,
            missing=missing,
            available=available,
        )

    published = []
    for day in available:
        try:
            kind = store.publish(date, day)
        except OSError as exc:
            raise ApiError(500, "publish failed for %s: %s" % (day, exc.strerror))
        published.append({"day": day, "source": kind})

    return 200, {
        "ok": True,
        "date": date,
        "published": published,
        "count": len(published),
        "message": "เผยแพร่สคริปต์จากไฟล์ต้นทางแล้ว %d วัน" % len(published),
        "note": "republished existing script files; no text was generated",
        "finished_at": utcnow(),
    }


ROUTES = {
    ("GET", "/api/health"): route_health,
    ("GET", "/api/manifest"): route_manifest,
    ("GET", "/api/stats"): route_stats,
    ("POST", "/api/save-script"): route_save_script,
    ("POST", "/api/regenerate"): route_regenerate,
}

PATHS = {}
for _method, _path in ROUTES:
    PATHS.setdefault(_path, set()).add(_method)
    PATHS[_path].add("OPTIONS")


# ── automation control plane ──────────────────────────────────────────
#
# These routes are kept in their own table because they need three things the
# original five do not: path parameters, a query string, and a CSRF intent
# header. The original table and its handler signature are untouched so the
# existing endpoints and their tests keep behaving exactly as before.

INTENT_HEADER = "X-Star-Intent"
INTENT_VALUE = "automation-control"
MAX_QUERY_PARAMS = 20
MAX_QUERY_VALUE = 2048

# Imported lazily inside the handlers' module scope: these are pure-Python
# stdlib-only modules in this repo, so the import is cheap and safe at module
# load, but a broken optional dependency must never take the whole API down.
try:
    import star_assets
    import star_automation
    import star_jobs
    import star_providers
    import star_redact
    from star_state import StateError
    AUTOMATION_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - only on a broken deployment
    star_assets = star_automation = star_jobs = star_providers = star_redact = None
    StateError = OSError
    AUTOMATION_IMPORT_ERROR = repr(_exc)


# Routes whose body is bytes rather than JSON. They are read by their own
# reader, with their own limit: MAX_BODY stays at 256 KiB for every JSON route,
# because widening it for an image would widen it for the whole control plane.
RAW_BODY_PATHS = frozenset(("/api/assets/background",))
MAX_UPLOAD_BODY = (star_assets.MAX_UPLOAD_BYTES if star_assets is not None
                   else 12 * 1024 * 1024)


class RawUpload:
    """A binary request body plus the type its sender claimed it was.

    The claim is kept separate from the bytes on purpose: a handler has to be
    able to compare what the caller said against what the file turned out to
    be, which is impossible once the two are conflated.
    """

    __slots__ = ("data", "content_type")

    def __init__(self, data, content_type):
        self.data = data
        self.content_type = content_type


# Job artifacts are deliberately narrower than "any file under the project".
# These are the only roots the production pipeline promotes generated output
# into, and the only formats the control centre knows how to preview safely.
ARTIFACT_ROOTS = (
    ("output",),
    ("content", "raw_astro"),
    ("content", "horoscope"),
    ("content", "scripts"),
)
ARTIFACT_TYPES = {
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".png": ("image/png", "image"),
    ".webp": ("image/webp", "image"),
    ".mp4": ("video/mp4", "video"),
    ".webm": ("video/webm", "video"),
    ".mp3": ("audio/mpeg", "audio"),
    ".wav": ("audio/wav", "audio"),
    ".ogg": ("audio/ogg", "audio"),
    ".m4a": ("audio/mp4", "audio"),
    ".txt": ("text/plain; charset=utf-8", "text"),
    ".json": ("application/json; charset=utf-8", "json"),
}
MAX_JOB_ARTIFACTS = 200


def _artifact_name(basename):
    """A display/download name that cannot carry header or path syntax."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", basename)[:180] or "artifact"


class JobArtifact:
    """A validated job-owned file, represented without exposing its path."""

    __slots__ = ("root", "parts", "content_type", "media_type", "name", "size")

    def __init__(self, root, parts, content_type, media_type, size):
        self.root = root
        self.parts = parts
        self.content_type = content_type
        self.media_type = media_type
        self.name = _artifact_name(parts[-1])
        self.size = size


def _artifact_parts(value):
    """Validate one stored project-relative artifact path lexically.

    The open below also refuses symlinks component-by-component. Keeping the
    lexical check separate makes traversal, Windows paths and unsupported
    project locations fail before the filesystem is touched.
    """
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ApiError(404, "artifact is unavailable")
    if os.path.isabs(value) or "\\" in value or "\x00" in value:
        raise ApiError(404, "artifact is unavailable")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ApiError(404, "artifact is unavailable")
    parts = tuple(value.split("/"))
    if (not parts or any(not part or part in (".", "..") for part in parts)
            or not any(parts[:len(prefix)] == prefix for prefix in ARTIFACT_ROOTS)):
        raise ApiError(404, "artifact is unavailable")
    extension = os.path.splitext(parts[-1])[1].lower()
    if extension not in ARTIFACT_TYPES:
        raise ApiError(404, "artifact is unavailable")
    return parts, ARTIFACT_TYPES[extension]


def _open_artifact(root, parts):
    """Open a regular file below root without following any symlink.

    Walking with directory file descriptors closes the usual realpath/open
    race: an intermediate directory cannot be swapped for a symlink between a
    validation check and the final open. Generated artifacts never need to be
    symlinks, so failing closed is the simplest contract.
    """
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    file_flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_fd = None
    try:
        directory_fd = os.open(os.path.realpath(root), directory_flags | cloexec)
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags | nofollow | cloexec,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], file_flags | nofollow | cloexec,
                          dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            raise ApiError(404, "artifact is unavailable")
        return file_fd, info
    except ApiError:
        raise
    except (OSError, ValueError):
        raise ApiError(404, "artifact is unavailable")
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def resolve_job_artifact(service, job, index):
    """Resolve an artifact index only through the selected job's result."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise ApiError(404, "artifact is unavailable")
    result = job.get("result") if isinstance(job, dict) else None
    artifacts = result.get("artifacts") if isinstance(result, dict) else None
    if (not isinstance(artifacts, list) or index < 0
            or index >= min(len(artifacts), MAX_JOB_ARTIFACTS)):
        raise ApiError(404, "artifact is unavailable")
    entry = artifacts[index]
    if not isinstance(entry, dict):
        raise ApiError(404, "artifact is unavailable")
    parts, (content_type, media_type) = _artifact_parts(entry.get("path"))
    file_fd, info = _open_artifact(service.root, parts)
    os.close(file_fd)
    return JobArtifact(service.root, parts, content_type, media_type, info.st_size)


def _artifact_views(service, job, verify=False):
    """Safe render contract for artifacts; stored filesystem paths never ship.

    `verify` opens every artifact, which is what makes `bytes` truthful and
    drops entries whose file has since been deleted. Only the single-job
    detail route pays that: the history list renders twenty jobs at a time and
    would otherwise stat several thousand files to answer one poll. Redaction
    is not conditional — no branch here can emit a stored path.
    """
    result = job.get("result") if isinstance(job, dict) else None
    stored = result.get("artifacts") if isinstance(result, dict) else None
    if not isinstance(stored, list):
        return []
    views = []
    for index, entry in enumerate(stored[:MAX_JOB_ARTIFACTS]):
        if not isinstance(entry, dict):
            continue
        try:
            parts, (content_type, media_type) = _artifact_parts(entry.get("path"))
        except ApiError:
            continue
        view = {
            "id": str(index),
            "name": _artifact_name(parts[-1]),
            "kind": media_type,
            "content_type": content_type,
            "url": "/api/jobs/%s/artifacts/%d" % (job["id"], index),
        }
        if verify:
            try:
                view["bytes"] = resolve_job_artifact(service, job, index).size
            except ApiError:
                continue
        # These are useful production labels, not path material. Keep them
        # tightly bounded and let the central redactor inspect the values too.
        for key in ("date", "day"):
            value = entry.get(key)
            if (isinstance(value, str) and len(value) <= 32
                    and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                view[key] = value
        views.append(view)
    return views


class RequestContext:
    """Everything a control-plane handler is given. No handler touches self."""

    def __init__(self, server, method, path, params, query, body, host, scheme):
        self.server = server
        self.method = method
        self.path = path
        self.params = params
        self.query = query
        self.body = body
        self.host = host
        self.scheme = scheme

    @property
    def store(self):
        return self.server.store

    @property
    def automation(self):
        return self.server.automation_service()


def _as_object(body):
    if body is None:
        raise ApiError(400, "request body is required")
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    return body


def _translate(exc):
    """Map a domain exception onto the API's error contract."""
    if star_jobs is not None and isinstance(exc, star_jobs.JobValidationError):
        return ApiError(400, exc.message, **({"field": exc.field} if exc.field else {}))
    if star_assets is not None and isinstance(exc, star_assets.AssetError):
        # AssetError messages are written to be shown to an operator: they name
        # the rule that was broken and never a path, a byte or a file name.
        status = exc.status if isinstance(exc.status, int) and 400 <= exc.status <= 599 else 400
        return ApiError(status, exc.message,
                        **({"field": exc.field} if exc.field else {}))
    if star_jobs is not None and isinstance(exc, star_jobs.JobConflict):
        return ApiError(409, exc.message, active_job=exc.active)
    if star_providers is not None and isinstance(exc, star_providers.ProviderError):
        return ApiError(400, exc.message, **({"field": exc.field} if exc.field else {}))
    return None


def _job_view(service, job, events=0, after_id=0, verify_artifacts=False):
    """A job as the API returns it: input echoed, credentials impossible."""
    view = dict(job)
    result = view.get("result")
    if isinstance(result, dict) and "artifacts" in result:
        result = dict(result)
        result["artifacts"] = _artifact_views(service, job, verify=verify_artifacts)
        view["result"] = result
    if events:
        view["events"] = service.store.list_events(job["id"], limit=events,
                                                   after_id=after_id)
    return star_redact.redact_obj(view)


def route_overview(ctx):
    return 200, ctx.automation.overview()


def route_prediction_guide(ctx):
    """The canonical prediction style guide, exactly as the prompt sees it.

    Read-only, and nothing here is confidential: it is editorial style. Serving
    it through the same loader the script stage uses is what keeps the rendered
    page and the generated prompt from drifting apart.
    """
    return 200, ctx.automation.prediction_guide_view()


def route_providers(ctx):
    service = ctx.automation
    return 200, {
        "generated_at": star_jobs.utcnow(),
        "providers": service.providers.statuses(),
        "note": "stored credentials are never returned by this API",
    }


def route_provider_configure(ctx):
    body = _as_object(ctx.body)
    key = body.get("provider")
    service = ctx.automation
    provider = service.providers.get(key)
    config = body.get("config")
    if config is None:
        config = {k: v for k, v in body.items() if k != "provider"}
    if not isinstance(config, dict):
        raise ApiError(400, "config must be a JSON object", field="config")
    status = provider.configure(config)
    problems = service.state.audit()
    if problems:
        # Surfaced rather than swallowed: a world-readable credential is an
        # operator-visible problem, not something to fix silently.
        status = dict(status)
        status["permission_problems"] = problems
    return 200, status


def route_provider_test(ctx):
    body = _as_object(ctx.body)
    service = ctx.automation
    provider = service.providers.get(body.get("provider"))
    live = body.get("live", False)
    if not isinstance(live, bool):
        raise ApiError(400, "live must be true or false", field="live")
    return 200, provider.test(live=live)


def route_jobs_list(ctx):
    service = ctx.automation
    limit = _int_param(ctx.query, "limit", default=25, low=1, high=star_jobs.MAX_JOBS_RETURNED)
    status = ctx.query.get("status")
    if status is not None and status not in star_jobs.ALL_STATES:
        raise ApiError(400, "unknown status filter; allowed: %s"
                       % " ".join(star_jobs.ALL_STATES), field="status")
    jobs = service.store.list_jobs(limit=limit, status=status)
    return 200, {
        "jobs": [_job_view(service, job) for job in jobs],
        "active_job": service.store.active_job(),
        "count": len(jobs),
        "generated_at": star_jobs.utcnow(),
    }


def _require_background_asset(service, job_input):
    """A syntactically valid asset id that names nothing is still a bad request.

    Validation in star_jobs can only say the id is 32 hex characters. Whether
    those characters name an image this server actually holds is a question
    only the asset store can answer, and it has to be answered before the job
    is created: a job that references a missing background would sit in the
    queue only to block at the video stage.
    """
    asset_id = job_input.get("background_asset_id")
    if not asset_id:
        return job_input
    if not service.background_exists(asset_id):
        raise ApiError(400, "no uploaded background image has that id; upload "
                            "the image again and use the id from that response",
                       field="background_asset_id")
    return job_input


def route_jobs_create(ctx):
    service = ctx.automation
    job_input = _require_background_asset(
        service, star_jobs.validate_job_input(_as_object(ctx.body)))
    job = service.store.create_job(job_input, origin="manual")
    return 201, _job_view(service, job)


def route_asset_background(ctx):
    """Accept one image as a raw body and return the id the job will carry.

    The body is bytes, not JSON, and it is read by its own reader with its own
    much larger limit — the JSON limit stays where it is for every other route.
    Nothing the caller said about those bytes is believed: the declared
    Content-Type only has to be one of the three we accept, and it is then
    checked against the type the stored file actually turned out to be, which
    was decided by magic bytes, ffprobe and a real decode.
    """
    upload = ctx.body
    if not isinstance(upload, RawUpload) or not upload.data:
        raise ApiError(400, "send the image bytes as the request body")

    declared = upload.content_type
    if not declared:
        raise ApiError(415, "Content-Type is required and must be one of: %s"
                       % ", ".join(star_assets.ACCEPTED_CONTENT_TYPES),
                       field="content_type")
    if declared not in star_assets.ACCEPTED_CONTENT_TYPES:
        raise ApiError(415, "Content-Type must be one of: %s"
                       % ", ".join(star_assets.ACCEPTED_CONTENT_TYPES),
                       field="content_type")

    service = ctx.automation
    meta = service.store_background(upload.data)
    if meta.get("content_type") != declared:
        # The header and the file disagree, so the upload is a mistake at best.
        # The bytes were already validated as an image, but they are not the
        # image the caller said they were sending, and keeping them would leave
        # an asset nobody asked for.
        star_assets.delete_background(service.state, meta["id"])
        raise ApiError(400, "the file is a %s image but the request declared "
                            "%s; send the file with its own type"
                       % (meta.get("content_type"), declared),
                       field="content_type")

    return 201, {
        "ok": True,
        # public_meta is the whole response body on purpose: id, type, size and
        # dimensions. No path, no file name, nothing that reveals the layout of
        # the state directory.
        "asset": star_assets.public_meta(meta),
        "note": "the image is stored outside the project tree and is never "
                "served back; a job references it by id only",
        "uploaded_at": star_jobs.utcnow(),
    }


def route_job_detail(ctx):
    service = ctx.automation
    job_id = star_jobs.valid_job_id(ctx.params.get("id"))
    job = service.store.get_job(job_id)
    if job is None:
        raise ApiError(404, "unknown job")
    limit = _int_param(ctx.query, "events", default=200, low=0,
                       high=star_jobs.MAX_EVENTS_RETURNED)
    after = _int_param(ctx.query, "after_id", default=0, low=0, high=2 ** 31)
    return 200, _job_view(service, job, events=limit, after_id=after,
                          verify_artifacts=True)


def route_job_artifact(ctx):
    """Return one validated artifact that belongs to exactly one job result."""
    service = ctx.automation
    job_id = star_jobs.valid_job_id(ctx.params.get("id"))
    job = service.store.get_job(job_id)
    if job is None:
        raise ApiError(404, "unknown job")
    try:
        index = int(ctx.params.get("artifact"))
    except (TypeError, ValueError):
        raise ApiError(404, "artifact is unavailable")
    return 200, resolve_job_artifact(service, job, index)


def route_job_cancel(ctx):
    service = ctx.automation
    job_id = star_jobs.valid_job_id(ctx.params.get("id"))
    changed, job = service.store.request_cancel(job_id)
    if job is None:
        raise ApiError(404, "unknown job")
    if not changed:
        raise ApiError(409, "job is already %s" % job["status"], job=_job_view(service, job))
    service.store.add_event(job_id, "warn", "cancellation requested by the operator")
    return 200, _job_view(service, service.store.get_job(job_id))


def route_job_retry(ctx):
    """Create a *new* job that references the parent.

    Deliberately manual: nothing in this service ever retries on its own, so a
    failing provider cannot turn into a loop of paid calls.
    """
    service = ctx.automation
    job_id = star_jobs.valid_job_id(ctx.params.get("id"))
    parent = service.store.get_job(job_id)
    if parent is None:
        raise ApiError(404, "unknown job")
    if parent["status"] in star_jobs.ACTIVE_STATES:
        raise ApiError(409, "job is still %s; cancel it before retrying" % parent["status"],
                       job=_job_view(service, parent))

    overrides = ctx.body if isinstance(ctx.body, dict) else {}
    merged = dict(parent["input"])
    merged.pop("dates", None)
    # The video customisation travels with the retry: a retried job renders the
    # same overlay over the same background as the job it repeats, unless the
    # caller overrides one of them here. The asset id is re-checked below
    # because retention may have removed the image since the parent ran.
    for key in ("from_date", "to_date", "days", "stages", "platforms", "dry_run",
                "force", "note", "overlay_text_mode", "custom_overlay_text",
                "background_asset_id"):
        if key in overrides:
            merged[key] = overrides[key]
    job_input = _require_background_asset(service, star_jobs.validate_job_input(merged))
    job = service.store.create_job(job_input, parent_id=parent["id"], origin="retry")
    service.store.add_event(job["id"], "info", "manual retry of job %s" % parent["id"])
    return 201, _job_view(service, job)


def route_schedule_get(ctx):
    service = ctx.automation
    config = service.store.get_schedule()
    config["note"] = ("one run per day in Asia/Bangkok; disabled by default and "
                      "never auto-retried")
    return 200, config


def route_schedule_put(ctx):
    service = ctx.automation
    config = star_jobs.validate_schedule_input(_as_object(ctx.body))
    stored = service.store.set_schedule(config)
    return 200, stored


def route_oauth_youtube_start(ctx):
    service = ctx.automation
    return 200, service.oauth.start(host=ctx.host, scheme=ctx.scheme)


def route_oauth_youtube_callback(ctx):
    """Completes the flow from Google's browser redirect.

    No intent header is required here and that is intentional: this request is
    a top-level navigation initiated by Google, so it cannot carry a custom
    header. The CSRF defence is the single-use, unguessable, short-lived state
    parameter that `consume_oauth_state` burns before the code is exchanged.
    """
    service = ctx.automation
    status = service.oauth.callback(ctx.query)
    return 200, {
        "ok": True,
        "provider": "youtube",
        "status": status,
        "message": "YouTube authorisation stored. You can close this tab and "
                   "return to the automation page.",
    }


def _int_param(query, name, default, low, high):
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "%s must be an integer" % name, field=name)
    if value < low or value > high:
        raise ApiError(400, "%s must be between %d and %d" % (name, low, high), field=name)
    return value


HEX32 = r"(?P<id>[0-9a-f]{32})"
ARTIFACT_INDEX = r"(?P<artifact>0|[1-9][0-9]{0,2})"

# (method, compiled path pattern, handler, requires_intent_header)
AUTOMATION_ROUTES = (
    ("GET", r"/api/automation/overview", route_overview, False),
    ("GET", r"/api/automation/prediction-guide", route_prediction_guide, False),
    ("GET", r"/api/providers", route_providers, False),
    ("POST", r"/api/providers/configure", route_provider_configure, True),
    ("POST", r"/api/providers/test", route_provider_test, True),
    ("GET", r"/api/jobs", route_jobs_list, False),
    ("POST", r"/api/jobs", route_jobs_create, True),
    ("POST", r"/api/assets/background", route_asset_background, True),
    ("GET", r"/api/jobs/" + HEX32, route_job_detail, False),
    ("GET", r"/api/jobs/" + HEX32 + r"/artifacts/" + ARTIFACT_INDEX,
     route_job_artifact, False),
    ("POST", r"/api/jobs/" + HEX32 + r"/cancel", route_job_cancel, True),
    ("POST", r"/api/jobs/" + HEX32 + r"/retry", route_job_retry, True),
    ("GET", r"/api/schedule", route_schedule_get, False),
    ("PUT", r"/api/schedule", route_schedule_put, True),
    ("GET", r"/api/oauth/youtube/start", route_oauth_youtube_start, True),
    ("GET", r"/api/oauth/youtube/callback", route_oauth_youtube_callback, False),
)

COMPILED_AUTOMATION = tuple(
    (method, re.compile("^" + pattern + "$"), handler, intent)
    for method, pattern, handler, intent in AUTOMATION_ROUTES
)

# Paths that exist but where an unmatched id should still 404 rather than
# falling through to "unknown endpoint" — purely cosmetic, but it makes a
# mistyped job id obvious in the response.
JOB_PATH_RE = re.compile(
    r"^/api/jobs/[^/]+(?:/(?:cancel|retry)|/artifacts/[^/]+)?$")


def match_automation(method, path):
    """Return (handler, params, requires_intent, allowed_methods_for_path)."""
    allowed = set()
    found = None
    for route_method, pattern, handler, intent in COMPILED_AUTOMATION:
        match = pattern.match(path)
        if match is None:
            continue
        allowed.add(route_method)
        if route_method == method:
            found = (handler, match.groupdict(), intent)
    if allowed:
        allowed.add("OPTIONS")
    return found, allowed


# ── HTTP layer ────────────────────────────────────────────────────────

class StarHandler(BaseHTTPRequestHandler):
    server_version = "star-api/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------
    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status, payload, extra_headers=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def send_error_json(self, status, message, **extra):
        payload = {"error": message, "status": status}
        payload.update(extra)
        self.send_json(status, payload)

    def _send_artifact_headers(self, artifact, status, length, content_range=None):
        self.send_response(status)
        self.send_header("Content-Type", artifact.content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", 'inline; filename="%s"' % artifact.name)
        self.send_header("Accept-Ranges", "bytes")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; frame-ancestors 'none'; sandbox")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        self.end_headers()

    def _requested_byte_range(self, size):
        raw = self.headers.get("Range")
        if not raw:
            return None
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw.strip())
        if match is None or (not match.group(1) and not match.group(2)) or size <= 0:
            return False
        if not match.group(1):
            suffix = int(match.group(2))
            if suffix <= 0:
                return False
            return max(0, size - suffix), size - 1
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        if start >= size or end < start:
            return False
        return start, min(end, size - 1)

    def send_artifact(self, artifact):
        """Stream a validated file, including byte ranges for native media."""
        try:
            file_fd, info = _open_artifact(artifact.root, artifact.parts)
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)
            return

        requested = self._requested_byte_range(info.st_size)
        if requested is False:
            os.close(file_fd)
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % info.st_size)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return

        start, end = requested if requested is not None else (0, info.st_size - 1)
        length = max(0, end - start + 1)
        status = 206 if requested is not None else 200
        content_range = ("bytes %d-%d/%d" % (start, end, info.st_size)
                         if requested is not None else None)
        self._send_artifact_headers(artifact, status, length, content_range)
        try:
            with os.fdopen(file_fd, "rb") as source:
                if start:
                    source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- security ---------------------------------------------------
    def same_origin_ok(self):
        """Only enforced when the browser actually sent an Origin."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        return parsed.netloc == host

    def host_ok(self):
        """Reject a forged Host when the operator pinned an allowlist.

        Applied to the automation control plane only; the original endpoints
        keep their previous, more permissive behaviour so nothing that already
        works against this API breaks.
        """
        host = self.headers.get("Host", "")
        if not host or "\n" in host or "\r" in host:
            return False
        allowed = os.environ.get("STAR_ALLOWED_HOSTS", "").strip()
        if not allowed:
            return True
        return host in {item.strip() for item in allowed.split(",") if item.strip()}

    def intent_ok(self):
        """CSRF guard: a custom header a cross-origin form simply cannot send."""
        return self.headers.get(INTENT_HEADER, "").strip() == INTENT_VALUE

    def parse_query(self):
        raw = urlsplit(self.path).query
        if not raw:
            return {}
        pairs = parse_qsl(raw, keep_blank_values=True)
        if len(pairs) > MAX_QUERY_PARAMS:
            raise ApiError(400, "too many query parameters")
        query = {}
        for key, value in pairs:
            if len(value) > MAX_QUERY_VALUE:
                raise ApiError(400, "query parameter %s is too long" % key[:32])
            query.setdefault(key, value)
        return query

    def request_scheme(self):
        """Honour the proxy's scheme header; default to https in production."""
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if forwarded in ("http", "https"):
            return forwarded
        host = self.headers.get("Host", "")
        return "http" if host.startswith("127.0.0.1") or host.startswith("localhost") else "https"

    def read_body(self, allow_empty=False):
        # Any failure below leaves unread bytes on the socket, which would
        # desync a keep-alive connection — close it instead of reusing it.
        # The flag is restored only once the whole body has been consumed.
        keep_alive = self.close_connection
        self.close_connection = True
        if self.headers.get("Transfer-Encoding", "").lower().strip() == "chunked":
            raise ApiError(411, "Content-Length is required")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            if allow_empty:
                self.close_connection = keep_alive
                return None
            raise ApiError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(400, "invalid Content-Length")
        if length < 0:
            raise ApiError(400, "invalid Content-Length")
        if length > MAX_BODY:
            raise ApiError(413, "request body exceeds %d bytes" % MAX_BODY)
        if length == 0:
            if allow_empty:
                self.close_connection = keep_alive
                return None
            raise ApiError(400, "request body is required")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "truncated request body")
        self.close_connection = keep_alive
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body must be valid UTF-8 JSON")

    def read_binary_body(self, limit=MAX_UPLOAD_BODY):
        """Read a raw body up to `limit` bytes. Used only by upload routes.

        Deliberately a second reader rather than a flag on read_body: the JSON
        limit is a security property of every other endpoint and must not
        become a parameter that an upload route can widen for everyone. The
        oversize check happens on Content-Length, so an over-limit upload is
        refused before a single byte of it is read.
        """
        keep_alive = self.close_connection
        self.close_connection = True
        if self.headers.get("Transfer-Encoding", "").lower().strip() == "chunked":
            raise ApiError(411, "Content-Length is required")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(400, "invalid Content-Length")
        if length < 0:
            raise ApiError(400, "invalid Content-Length")
        if length > limit:
            raise ApiError(413, "the upload exceeds the %d MiB limit"
                           % (limit // (1024 * 1024)))
        if length == 0:
            raise ApiError(400, "request body is required")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "truncated request body")
        self.close_connection = keep_alive
        return RawUpload(data, self.declared_content_type())

    def declared_content_type(self):
        """The bare media type the caller sent, lowercased, parameters dropped."""
        raw = self.headers.get("Content-Type") or ""
        return raw.split(";")[0].strip().lower()

    def drop_unread_body(self):
        """Refuse a request without reading its body, and without desyncing.

        Every guard in the dispatcher answers before the body is read, which on
        a keep-alive connection would leave the next request to be parsed out
        of the middle of this one's payload. Draining instead would mean
        reading up to 12 MiB that a rejected caller chose for us, so the
        connection is closed: correct for the client, free for the server.
        """
        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            self.close_connection = True

    # -- dispatch ---------------------------------------------------
    def dispatch(self, method):
        path = urlsplit(self.path).path
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        allowed = PATHS.get(path)
        if allowed is not None:
            self.dispatch_legacy(method, path, allowed)
            return
        self.dispatch_automation(method, path)

    def dispatch_legacy(self, method, path, allowed):
        """The original five endpoints, with their original semantics."""
        if method not in allowed:
            self.send_json(405, {"error": "method not allowed", "status": 405,
                                 "allow": sorted(allowed)},
                           {"Allow": ", ".join(sorted(allowed))})
            return
        if not self.same_origin_ok():
            self.send_error_json(403, "cross-origin request rejected")
            return
        if method == "OPTIONS":
            self.send_json(204, {}, {"Allow": ", ".join(sorted(allowed))})
            return

        try:
            body = self.read_body() if method == "POST" else None
            status, payload = ROUTES[(method, path)](self.store, body)
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message, **exc.extra)
            return
        except Exception as exc:  # never leak a traceback to the client
            self.log_message("unhandled error on %s %s: %r", method, path, exc)
            self.send_error_json(500, "internal server error")
            return
        self.send_json(status, payload)

    def dispatch_automation(self, method, path):
        found, allowed = match_automation(method, path)
        if not allowed:
            self.drop_unread_body()
            if JOB_PATH_RE.match(path):
                self.send_error_json(404, "unknown job")
                return
            self.send_error_json(404, "unknown endpoint: %s" % path)
            return
        if method != "OPTIONS" and found is None:
            self.drop_unread_body()
            self.send_json(405, {"error": "method not allowed", "status": 405,
                                 "allow": sorted(allowed)},
                           {"Allow": ", ".join(sorted(allowed))})
            return
        if not self.same_origin_ok():
            self.drop_unread_body()
            self.send_error_json(403, "cross-origin request rejected")
            return
        if not self.host_ok():
            self.drop_unread_body()
            self.send_error_json(403, "host not allowed")
            return
        if method == "OPTIONS":
            # An OPTIONS carrying a body is malformed rather than dangerous,
            # but the body is still never read, so the socket cannot be reused.
            self.drop_unread_body()
            self.send_json(204, {}, {"Allow": ", ".join(sorted(allowed))})
            return

        handler, params, requires_intent = found
        if requires_intent and not self.intent_ok():
            self.drop_unread_body()
            self.send_error_json(
                403, "missing or wrong %s header" % INTENT_HEADER,
                required_header={INTENT_HEADER: INTENT_VALUE})
            return
        if AUTOMATION_IMPORT_ERROR is not None:
            self.drop_unread_body()
            self.send_error_json(503, "automation modules failed to load on this server")
            return

        try:
            body = None
            if method in ("POST", "PUT"):
                if path in RAW_BODY_PATHS:
                    # Bytes, not JSON, and read by the upload reader with the
                    # upload limit. Nothing else on the control plane can reach
                    # this branch: the set is a literal, not a prefix match.
                    body = self.read_binary_body()
                else:
                    # Cancel and retry are intentionally body-optional: an empty
                    # POST is the natural thing for a button to send.
                    body = self.read_body(allow_empty=True)
            ctx = RequestContext(self.server, method, path, params,
                                 self.parse_query(), body,
                                 self.headers.get("Host", ""), self.request_scheme())
            status, payload = handler(ctx)
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message, **exc.extra)
            return
        except Exception as exc:
            translated = _translate(exc)
            if translated is not None:
                self.send_error_json(translated.status, translated.message,
                                     **translated.extra)
                return
            if star_automation is not None and isinstance(exc, StateError):
                self.send_error_json(503, "state directory is unavailable: %s" % exc)
                return
            self.log_message("unhandled error on %s %s: %r", method, path, exc)
            self.send_error_json(500, "internal server error")
            return
        if isinstance(payload, JobArtifact):
            self.send_artifact(payload)
        else:
            self.send_json(status, payload)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")


class StarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, state_dir=None, automation=None,
                 start_threads=True):
        super().__init__(address, StarHandler)
        self.store = store
        self.state_dir = state_dir
        self.start_automation_threads = start_threads
        self._automation = automation
        self._automation_lock = threading.Lock()

    def automation_service(self):
        """Built on first use, never at import or at server construction.

        That laziness is what keeps the original endpoints (and their tests)
        from ever touching /var/lib/star: a server that only serves
        /api/stats never creates a state directory at all.
        """
        if self._automation is not None:
            return self._automation
        with self._automation_lock:
            if self._automation is None:
                if AUTOMATION_IMPORT_ERROR is not None:
                    raise ApiError(503, "automation modules are unavailable")
                self._automation = star_automation.AutomationService(
                    self.store.root, state_dir=self.state_dir,
                    start_threads=self.start_automation_threads)
        return self._automation

    def server_close(self):
        if self._automation is not None:
            try:
                self._automation.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        super().server_close()


def create_server(root, host=HOST, port=PORT, state_dir=None, automation=None,
                  start_threads=True):
    return StarServer((host, port), Store(root), state_dir=state_dir,
                      automation=automation, start_threads=start_threads)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0] if argv else os.environ.get("STAR_ROOT", "/var/www/star")
    port = int(os.environ.get("STAR_API_PORT", PORT))
    server = create_server(root, HOST, port)
    sys.stderr.write("star-api %s listening on %s:%d (root=%s)\n"
                     % (VERSION, HOST, port, server.store.root))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
