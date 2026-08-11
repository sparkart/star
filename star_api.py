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
import sys
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

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
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def send_error_json(self, status, message, **extra):
        payload = {"error": message, "status": status}
        payload.update(extra)
        self.send_json(status, payload)

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

    def read_body(self):
        # Any failure below leaves unread bytes on the socket, which would
        # desync a keep-alive connection — close it instead of reusing it.
        # The flag is restored only once the whole body has been consumed.
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
        if length > MAX_BODY:
            raise ApiError(413, "request body exceeds %d bytes" % MAX_BODY)
        if length == 0:
            raise ApiError(400, "request body is required")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "truncated request body")
        self.close_connection = keep_alive
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body must be valid UTF-8 JSON")

    # -- dispatch ---------------------------------------------------
    def dispatch(self, method):
        path = urlsplit(self.path).path
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        allowed = PATHS.get(path)
        if allowed is None:
            self.send_error_json(404, "unknown endpoint: %s" % path)
            return
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

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")


class StarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store):
        super().__init__(address, StarHandler)
        self.store = store


def create_server(root, host=HOST, port=PORT):
    return StarServer((host, port), Store(root))


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
