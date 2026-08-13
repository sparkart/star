#!/usr/bin/env python3
"""Pipeline adapters, background job runner, scheduler and OAuth scaffolding.

Design notes that matter for review:

* **No shell.** Every subprocess is an argv list with `start_new_session=True`,
  so cancelling a job can signal the whole process group and no operator input
  is ever parsed by a shell.
* **Dry run is a hard boundary, not a flag that stages politely honour.** In
  dry-run the stage's `execute` is never called at all; only `plan()` runs, and
  `plan()` is pure — it builds strings and checks prerequisites.
* **Every write outside the job workspace is deliberate.** Stages write into
  `workdir` and the pipeline promotes finished artifacts into the project tree
  with an atomic rename.
* **Blocked beats failed.** A missing credential is a `blocked` job carrying the
  exact prerequisite text from the provider, not a stack trace.
"""

import base64
import errno
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import star_assets
import star_jobs
import star_providers
import star_redact
from star_jobs import JobConflict, JobStore, JobValidationError
from star_state import StateDir, StateError, resolve_state_dir

try:  # stdlib since 3.9, but tzdata may be absent on a minimal image
    from zoneinfo import ZoneInfo
    BANGKOK = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover - exercised only on hosts without tzdata
    BANGKOK = timezone(timedelta(hours=7), "Asia/Bangkok")

DAY_TH = {"sun": "อาทิตย์", "mon": "จันทร์", "tue": "อังคาร", "wed": "พุธ",
          "thu": "พฤหัสบดี", "fri": "ศุกร์", "sat": "เสาร์"}

STAGE_TIMEOUT = {
    "astro": 300,
    "script": 300,
    "audio": 300,
    "video": 900,
    "publish": 900,
}
MAX_CAPTURED_LINES = 400
MAX_LINE = 2000
SCRIPT_MAX_CHARS = 1200

THAI_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerifThai-Regular.ttf",
    "/usr/share/fonts/truetype/tlwg/Sarabun-Regular.ttf",
    "/usr/share/fonts/truetype/thai/Garuda.ttf",
)



# ── prediction style guide ────────────────────────────────────────────
#
# automation/prediction-guide.json is the single canonical source for how a
# prediction is allowed to sound. The frontend renders that same file and the
# script prompt is built from it here — there is deliberately no second copy of
# the editorial rules anywhere in this repository.
#
# The guide governs *style only*. Planetary facts come from the astro stage's
# prediction JSON and are passed through untouched; nothing below can add,
# remove or reinterpret an astronomical value.

PREDICTION_GUIDE_RELPATH = "automation/prediction-guide.json"
PREDICTION_GUIDE_SUPPORTED_MAJOR = 1

# The caption heading is a hard contract shared with the caption tooling; a
# guide that changes it silently would drift every downstream file, so the
# loader refuses the guide instead.
PREDICTION_GUIDE_HEADING = "ดวงของชาววัน{วัน} ประจำวันที่ {วันที่} {เดือนเต็ม} พ.ศ. {ปี}"
PREDICTION_GUIDE_HASHTAG_COUNT = 5

# Bounds for the prompt block. A guide that grows without limit would quietly
# push the day's real astronomy data out of the model's attention, so the block
# is capped item-by-item and as a whole.
GUIDE_MAX_ITEMS = 12
GUIDE_MAX_ITEM_CHARS = 220
GUIDE_MAX_CHARS = 6000

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_GUIDE_CACHE = {}
_GUIDE_CACHE_LOCK = threading.Lock()


class GuideError(Exception):
    """The prediction guide is missing, unparseable or does not meet contract.

    Carries only the repo-relative path and a description of the problem: no
    file system layout beyond the project, and never any credential material.
    """


def guide_path(root):
    return os.path.join(root, "automation", "prediction-guide.json")


def _dig(data, path):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _is_text(value):
    return isinstance(value, str) and value.strip() != ""


def _is_text_list(value):
    return (isinstance(value, list) and len(value) > 0
            and all(_is_text(item) for item in value))


# (dotted path, kind) — checked in this order so the reported problem list is
# deterministic for a given guide.
_GUIDE_TEXT_FIELDS = (
    ("title",),
    ("purpose",),
    ("core_voice", "definition"),
    ("mood_tone", "rhythm"),
    ("structure", "caption_contract", "body_length"),
    ("factual_rules", "source_of_truth"),
    ("factual_rules", "uncertainty_language"),
    ("factual_rules", "missing_data_behavior"),
)

_GUIDE_LIST_FIELDS = (
    ("core_voice", "principles"),
    ("mood_tone", "target"),
    ("structure", "caption_contract", "heading_rules"),
    ("structure", "caption_contract", "body_order"),
    ("structure", "caption_contract", "hashtags", "rules"),
    ("vocabulary", "preferred"),
    ("vocabulary", "language_rules"),
    ("factual_rules", "astronomy_and_astrology"),
    ("factual_rules", "high_stakes"),
    ("consistency_checklist",),
)


def validate_prediction_guide(data):
    """Return the list of contract problems. Empty means the guide is usable."""
    problems = []
    if not isinstance(data, dict):
        return ["the guide must be a JSON object"]

    version = data.get("version")
    match = _VERSION_RE.match(version) if isinstance(version, str) else None
    if match is None:
        problems.append("version is missing or is not MAJOR.MINOR.PATCH")
    elif int(match.group(1)) != PREDICTION_GUIDE_SUPPORTED_MAJOR:
        problems.append("version %s is not supported by this build (expected %d.x.x)"
                        % (version, PREDICTION_GUIDE_SUPPORTED_MAJOR))

    for path in _GUIDE_TEXT_FIELDS:
        if not _is_text(_dig(data, path)):
            problems.append("%s is missing or empty" % ".".join(path))
    for path in _GUIDE_LIST_FIELDS:
        if not _is_text_list(_dig(data, path)):
            problems.append("%s must be a non-empty list of strings" % ".".join(path))

    heading = _dig(data, ("structure", "caption_contract", "required_heading"))
    if heading != PREDICTION_GUIDE_HEADING:
        problems.append("structure.caption_contract.required_heading does not match "
                        "the caption contract this build enforces")

    hashtags = _dig(data, ("structure", "caption_contract", "hashtags"))
    if not isinstance(hashtags, dict):
        problems.append("structure.caption_contract.hashtags is missing")
    else:
        if hashtags.get("count") != PREDICTION_GUIDE_HASHTAG_COUNT:
            problems.append("structure.caption_contract.hashtags.count must be %d"
                            % PREDICTION_GUIDE_HASHTAG_COUNT)
        order = hashtags.get("order")
        if not _is_text_list(order) or len(order) != PREDICTION_GUIDE_HASHTAG_COUNT:
            problems.append("structure.caption_contract.hashtags.order must list "
                            "exactly %d hashtags" % PREDICTION_GUIDE_HASHTAG_COUNT)

    prohibited = data.get("prohibited_patterns")
    if not isinstance(prohibited, list) or not prohibited:
        problems.append("prohibited_patterns must be a non-empty list")
    elif not all(isinstance(item, dict) and _is_text(item.get("pattern"))
                 for item in prohibited):
        problems.append("every prohibited_patterns entry needs a pattern string")

    return problems


def load_prediction_guide(root, refresh=False):
    """Read, validate and cache the canonical guide. Raises GuideError.

    Cached on (mtime_ns, size) so an edited guide is picked up on the next job
    without a restart, while a long run does not re-read the file per script.
    """
    path = guide_path(root)
    try:
        info = os.stat(path)
    except OSError:
        raise GuideError("prediction guide %s is missing" % PREDICTION_GUIDE_RELPATH)
    stamp = (info.st_mtime_ns, info.st_size)

    if not refresh:
        with _GUIDE_CACHE_LOCK:
            cached = _GUIDE_CACHE.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError:
        raise GuideError("prediction guide %s is not valid JSON"
                         % PREDICTION_GUIDE_RELPATH)
    except OSError:
        raise GuideError("prediction guide %s could not be read"
                         % PREDICTION_GUIDE_RELPATH)

    problems = validate_prediction_guide(data)
    if problems:
        raise GuideError("prediction guide %s does not meet contract: %s"
                         % (PREDICTION_GUIDE_RELPATH, "; ".join(problems[:5])))

    with _GUIDE_CACHE_LOCK:
        _GUIDE_CACHE[path] = (stamp, data)
    return data


def _guide_lines(label, items, bullet="- "):
    out = []
    if not items:
        return out
    out.append(label)
    for item in list(items)[:GUIDE_MAX_ITEMS]:
        out.append(bullet + str(item).replace("\n", " ").strip()[:GUIDE_MAX_ITEM_CHARS])
    return out


def prediction_guide_prompt_block(guide):
    """A deterministic, bounded rendering of the guide for a Claude prompt.

    Deterministic: the traversal order is written out here rather than taken
    from dict iteration, so the same guide file always produces byte-identical
    text. Bounded: per-item and whole-block caps, applied last.
    """
    contract = _dig(guide, ("structure", "caption_contract")) or {}
    hashtags = contract.get("hashtags") or {}
    vocabulary = guide.get("vocabulary") or {}
    factual = guide.get("factual_rules") or {}

    lines = ["[คู่มือภาษาและน้ำเสียง เวอร์ชัน %s — บังคับใช้ทุกข้อ]" % guide.get("version", "?")]
    lines.append("น้ำเสียงหลัก: " + str(_dig(guide, ("core_voice", "definition")) or "")[:GUIDE_MAX_ITEM_CHARS])
    lines += _guide_lines("หลักการเขียน:", _dig(guide, ("core_voice", "principles")))
    lines += _guide_lines("อารมณ์และโทนที่ต้องการ:", _dig(guide, ("mood_tone", "target")))
    lines.append("จังหวะประโยค: " + str(_dig(guide, ("mood_tone", "rhythm")) or "")[:GUIDE_MAX_ITEM_CHARS])

    lines.append("บรรทัดแรกต้องเป็น: " + str(contract.get("required_heading") or ""))
    lines += _guide_lines("กฎของบรรทัดแรก:", contract.get("heading_rules"))
    lines += _guide_lines("ลำดับเนื้อหา:", contract.get("body_order"))
    lines.append("ความยาวเนื้อหา: " + str(contract.get("body_length") or "")[:GUIDE_MAX_ITEM_CHARS])
    if hashtags:
        lines.append("แฮชแท็ก: ต้องมี %s ตัวพอดี ตามลำดับ %s"
                     % (hashtags.get("count"), " ".join(
                         str(item) for item in (hashtags.get("order") or []))))
        lines += _guide_lines("กฎแฮชแท็ก:", hashtags.get("rules"))

    lines += _guide_lines("คำที่ควรใช้:", vocabulary.get("preferred"))
    lines += _guide_lines("กฎภาษา:", vocabulary.get("language_rules"))

    prohibited = []
    for item in (guide.get("prohibited_patterns") or [])[:GUIDE_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        examples = [str(ex) for ex in (item.get("examples") or [])[:2]]
        text = str(item.get("pattern", ""))
        if examples:
            text += " (เช่น " + " / ".join(examples) + ")"
        prohibited.append(text)
    lines += _guide_lines("ห้ามเด็ดขาด:", prohibited)

    lines.append("แหล่งข้อมูลที่ใช้ได้: " + str(factual.get("source_of_truth") or "")[:GUIDE_MAX_ITEM_CHARS])
    lines += _guide_lines("กฎข้อเท็จจริงทางดาราศาสตร์:", factual.get("astronomy_and_astrology"))
    lines.append("ภาษาแสดงความไม่แน่นอน: " + str(factual.get("uncertainty_language") or "")[:GUIDE_MAX_ITEM_CHARS])
    lines += _guide_lines("เรื่องละเอียดอ่อน:", factual.get("high_stakes"))
    lines += _guide_lines("ตรวจก่อนส่ง:", guide.get("consistency_checklist"))
    lines.append("[จบคู่มือ]")

    block = "\n".join(line for line in lines if line.strip())
    if len(block) > GUIDE_MAX_CHARS:
        block = block[:GUIDE_MAX_CHARS].rsplit("\n", 1)[0] + "\n[คู่มือถูกตัดตามขีดจำกัดความยาว]"
    return block


class StageBlocked(Exception):
    """A prerequisite is missing. The job becomes `blocked`, not `failed`."""


class StageFailed(Exception):
    """The stage ran and did not succeed. The job becomes `failed`."""


class JobCancelled(Exception):
    """Cancellation was requested; unwind and mark the job `cancelled`."""


# ── subprocess plumbing ───────────────────────────────────────────────

def find_thai_font():
    """First readable Thai font from a fixed allowlist. Never operator input."""
    for path in THAI_FONT_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    return None


def run_command(argv, timeout, cwd=None, env=None, on_line=None, is_cancelled=None):
    """Run argv, streaming output lines, honouring cooperative cancellation.

    Returns (returncode, captured_lines). Raises JobCancelled after tearing the
    process group down, or subprocess.TimeoutExpired on the deadline.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("argv must be a non-empty list")
    for part in argv:
        if not isinstance(part, str):
            raise ValueError("argv entries must be strings")

    proc = subprocess.Popen(  # noqa: S603 - argv list, shell is never used
        list(argv), cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, errors="replace", bufsize=1, start_new_session=True)

    captured = []
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    cancelled = False
    timed_out = False
    streaming = True
    try:
        while True:
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            if streaming:
                for _key, _mask in selector.select(timeout=0.5):
                    line = proc.stdout.readline()
                    if not line:
                        # End of output. A closed pipe selects ready
                        # immediately and forever, so a child that closes
                        # stdout and keeps working (ffmpeg finishing a long
                        # encode) would spin this loop at full CPU until its
                        # timeout. Stop polling the pipe instead.
                        selector.unregister(proc.stdout)
                        streaming = False
                        break
                    line = star_redact.redact_text(line.rstrip("\n")[:MAX_LINE],
                                                   limit=MAX_LINE)
                    if len(captured) < MAX_CAPTURED_LINES:
                        captured.append(line)
                    if on_line is not None and line:
                        on_line(line)
            else:
                # Wait for the exit at the same cadence the selector polled at,
                # so cancellation and the deadline stay just as responsive.
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is not None:
                # Drain whatever is still buffered before giving up the loop.
                if streaming:
                    for line in proc.stdout:
                        line = star_redact.redact_text(line.rstrip("\n")[:MAX_LINE],
                                                       limit=MAX_LINE)
                        if len(captured) < MAX_CAPTURED_LINES:
                            captured.append(line)
                        if on_line is not None and line:
                            on_line(line)
                break
    finally:
        selector.close()
        if proc.poll() is None:
            _terminate_group(proc)
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait(timeout=10)

    if cancelled:
        raise JobCancelled("cancelled during: %s" % os.path.basename(argv[0]))
    if timed_out:
        raise subprocess.TimeoutExpired(argv[0], timeout)
    return proc.returncode, captured


def _terminate_group(proc):
    """SIGTERM the process group, then SIGKILL what survives."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def atomic_replace(src, dst):
    """Move a finished artifact into the project tree without a partial state."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        tmp = dst + ".part"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    return dst


# ── job context ───────────────────────────────────────────────────────

class JobContext:
    """Everything a stage is allowed to touch, and nothing else."""

    def __init__(self, service, job, workdir):
        self.service = service
        self.job = job
        self.job_id = job["id"]
        self.input = job["input"]
        self.workdir = workdir
        self.root = service.root
        self.state = service.state
        self.store = service.store
        self.providers = service.providers
        self.artifacts = []
        self.planned = []

    # -- reporting ------------------------------------------------------
    def log(self, message, level="info", stage=None):
        self.store.add_event(self.job_id, level,
                             star_redact.redact_text(message, limit=star_jobs.MAX_EVENT_MESSAGE),
                             stage=stage)

    def progress(self, value, stage=None):
        self.store.update_progress(self.job_id, progress=value, current_stage=stage)

    def cancelled(self):
        return self.store.cancel_requested(self.job_id)

    def check_cancelled(self):
        if self.cancelled():
            raise JobCancelled("cancellation requested by the operator")

    # -- paths ----------------------------------------------------------
    def stage_dir(self, name):
        path = os.path.join(self.workdir, name)
        os.makedirs(path, exist_ok=True)
        return path

    def project_path(self, *parts):
        """Join under the project root and refuse anything that escapes it."""
        root = os.path.realpath(self.root)
        target = os.path.realpath(os.path.join(root, *parts))
        if target != root and not target.startswith(root + os.sep):
            raise StageFailed("refused a path outside the project root")
        return target

    def record(self, kind, path, extra=None):
        entry = {"kind": kind, "path": os.path.relpath(path, self.root)
                 if path.startswith(self.root) else path}
        if extra:
            entry.update(extra)
        self.artifacts.append(entry)
        return entry

    def plan(self, description, command=None, output=None):
        entry = {"description": description}
        if command:
            entry["command"] = " ".join(command)
        if output:
            entry["output"] = output
        self.planned.append(entry)
        return entry

    @property
    def dates(self):
        return list(self.input.get("dates") or [])

    @property
    def days(self):
        return list(self.input.get("days") or [])

    @property
    def force(self):
        return bool(self.input.get("force"))


# ── stage adapters ────────────────────────────────────────────────────

class StageAdapter:
    name = ""
    label = ""

    def validate(self, job_input):
        """Return a list of validation errors for this stage's inputs."""
        return []

    def prerequisites(self, ctx):
        """Return a list of unmet prerequisites (empty means runnable)."""
        return []

    def plan(self, ctx):
        """Describe what execute() would do. Must be side-effect free."""
        raise NotImplementedError

    def execute(self, ctx):
        """Do the work. Only ever called when dry_run is false."""
        raise NotImplementedError

    # helpers
    def _python(self):
        return shutil.which("python3") or "python3"

    def _script(self, ctx, name):
        path = os.path.join(ctx.root, "scripts", name)
        return path if os.path.isfile(path) else None


class AstroStage(StageAdapter):
    """Swiss Ephemeris via the repository's own deterministic scripts.

    Nothing here computes astrology itself; if the scripts or the ephemeris are
    unavailable the stage blocks rather than inventing numbers.
    """

    name = "astro"
    label = "Astronomy & predictions"

    def prerequisites(self, ctx):
        missing = []
        if self._script(ctx, "pull_ephem.py") is None:
            missing.append("scripts/pull_ephem.py is missing from the project")
        if self._script(ctx, "generate_predictions.py") is None:
            missing.append("scripts/generate_predictions.py is missing from the project")
        try:
            import swisseph  # noqa: F401
        except ImportError:
            missing.append("the swisseph Python module is not installed "
                           "(pip install pyswisseph)")
        return missing

    def plan(self, ctx):
        for date in ctx.dates:
            ephem = os.path.join(ctx.stage_dir(self.name), "ephem_%s.json" % date)
            ctx.plan("pull ephemeris for %s" % date,
                     command=[self._python(), "scripts/pull_ephem.py", "--date", date,
                              "--run", "1", "--output", ephem],
                     output="content/raw_astro/%s.json" % date)
            ctx.plan("derive per-day predictions for %s" % date,
                     command=[self._python(), "scripts/generate_predictions.py",
                              "--input", ephem, "--output",
                              os.path.join(ctx.stage_dir(self.name), date)],
                     output="content/horoscope/%s/<day>.json" % date)
        return ctx.planned

    def execute(self, ctx):
        stage_dir = ctx.stage_dir(self.name)
        produced = 0
        for date in ctx.dates:
            ctx.check_cancelled()
            ephem = os.path.join(stage_dir, "ephem_%s.json" % date)
            code, lines = run_command(
                [self._python(), self._script(ctx, "pull_ephem.py"),
                 "--date", date, "--run", "1", "--output", ephem],
                timeout=STAGE_TIMEOUT[self.name], cwd=ctx.root,
                on_line=lambda line: ctx.log(line, stage=self.name),
                is_cancelled=ctx.cancelled)
            if code != 0 or not os.path.isfile(ephem):
                raise StageFailed("pull_ephem.py failed for %s (exit %s): %s"
                                  % (date, code, " | ".join(lines[-3:])))

            out_dir = os.path.join(stage_dir, date)
            os.makedirs(out_dir, exist_ok=True)
            code, lines = run_command(
                [self._python(), self._script(ctx, "generate_predictions.py"),
                 "--input", ephem, "--output", out_dir],
                timeout=STAGE_TIMEOUT[self.name], cwd=ctx.root,
                on_line=lambda line: ctx.log(line, stage=self.name),
                is_cancelled=ctx.cancelled)
            if code != 0:
                raise StageFailed("generate_predictions.py failed for %s (exit %s): %s"
                                  % (date, code, " | ".join(lines[-3:])))

            atomic_replace(ephem, ctx.project_path("content", "raw_astro", "%s.json" % date))
            ctx.record("raw_astro", ctx.project_path("content", "raw_astro",
                                                     "%s.json" % date))
            for day in ctx.days:
                src = os.path.join(out_dir, "%s.json" % day)
                if not os.path.isfile(src):
                    raise StageFailed("no prediction produced for %s/%s" % (date, day))
                dst = ctx.project_path("content", "horoscope", date, "%s.json" % day)
                atomic_replace(src, dst)
                ctx.record("horoscope", dst, {"date": date, "day": day})
                produced += 1
            ctx.log("astro complete for %s (%d days)" % (date, len(ctx.days)),
                    stage=self.name)
        return {"predictions": produced}


class ScriptStage(StageAdapter):
    """Claude CLI, authenticated by the operator's subscription login."""

    name = "script"
    label = "Script generation"

    def prerequisites(self, ctx):
        missing = []
        provider = ctx.providers.get("claude")
        status = provider.status()
        if status["status"] == star_providers.NOT_CONFIGURED:
            missing.append("Claude CLI is not available: " + status["detail"])
        # Fail closed: without a valid guide the tone would drift silently,
        # which is worse than not generating at all. A dry run surfaces this as
        # an unmet prerequisite; a production run blocks on it below.
        try:
            load_prediction_guide(ctx.root)
        except GuideError as exc:
            missing.append(str(exc))
        return missing

    def _prompt(self, date, day, prediction, guide_block):
        """Deterministic prompt built from the day's real prediction data.

        Two inputs, two roles: `prediction` is the authoritative astronomy and
        is never restated as a rule, `guide_block` is style and never adds a
        fact. The guide is placed first so the facts stay closest to the task.
        """
        th = DAY_TH.get(day, day)
        facts = json.dumps(prediction, ensure_ascii=False, sort_keys=True)[:4000]
        return (
            "เขียนสคริปต์พากย์ดวงรายวันภาษาไทย สำหรับชาววันเกิด" + th + " "
            "ประจำวันที่ " + date + "\n\n"
            + guide_block + "\n\n"
            "ใช้ข้อมูลโหราศาสตร์ต่อไปนี้เท่านั้น ห้ามแต่งข้อมูลดาวเพิ่ม "
            "และห้ามให้คู่มือด้านบนเปลี่ยนค่าหรือความหมายของข้อมูลนี้:\n"
            + facts + "\n\n"
            "ข้อกำหนด: ขึ้นต้นด้วย \"ดวงของชาววัน" + th + "\" "
            "ความยาว 3-5 ประโยค น้ำเสียงเป็นมิตร ไม่ใช้อิโมจิ "
            "ไม่ต้องมีหัวข้อหรือคำอธิบายเพิ่ม ตอบเฉพาะตัวสคริปต์"
        )

    def _prediction(self, ctx, date, day):
        path = os.path.join(ctx.root, "content", "horoscope", date, "%s.json" % day)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (ValueError, OSError):
                return None
        return None

    def _target(self, ctx, date, day):
        return ctx.project_path("content", "scripts", "claude_%s_%s.txt" % (date, day))

    def plan(self, ctx):
        binary = shutil.which("claude") or "claude"
        try:
            guide = load_prediction_guide(ctx.root)
            ctx.plan("style guide %s version %s would be embedded in every prompt"
                     % (PREDICTION_GUIDE_RELPATH, guide.get("version")),
                     output="%d characters of style rules"
                            % len(prediction_guide_prompt_block(guide)))
        except GuideError as exc:
            ctx.plan("script stage would refuse to run: %s" % exc)
        pending = skipped = 0
        for date in ctx.dates:
            for day in ctx.days:
                target = self._target(ctx, date, day)
                if os.path.isfile(target) and not ctx.force:
                    skipped += 1
                    continue
                pending += 1
                ctx.plan("generate script for %s/%s" % (date, day),
                         command=[binary, "--print", "<prompt built from the day's "
                                                     "prediction JSON>"],
                         output=os.path.relpath(target, ctx.root))
        ctx.plan("%d script(s) would be generated, %d skipped because they already exist "
                 "(set force to overwrite)" % (pending, skipped))
        return ctx.planned

    def execute(self, ctx):
        provider = ctx.providers.get("claude")
        binary = provider.binary()
        if binary is None:
            raise StageBlocked("Claude CLI is not on PATH")
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = provider.config_dir()

        # Loaded once per run, before any paid call: a broken guide must stop
        # the stage rather than let the first script through with a drifted
        # tone. The message names the file and the fault, nothing else.
        try:
            guide = load_prediction_guide(ctx.root)
        except GuideError as exc:
            raise StageBlocked(str(exc))
        guide_block = prediction_guide_prompt_block(guide)
        ctx.log("style guide %s version %s loaded (%d chars in prompt)"
                % (PREDICTION_GUIDE_RELPATH, guide.get("version"), len(guide_block)),
                stage=self.name)

        written = skipped = 0
        for date in ctx.dates:
            for day in ctx.days:
                ctx.check_cancelled()
                target = self._target(ctx, date, day)
                if os.path.isfile(target) and not ctx.force:
                    skipped += 1
                    continue
                prediction = self._prediction(ctx, date, day)
                if prediction is None:
                    raise StageBlocked(
                        "no prediction for %s/%s — run the astro stage first" % (date, day))
                prompt = self._prompt(date, day, prediction, guide_block)
                code, lines = run_command(
                    [binary, "--print", prompt],
                    timeout=STAGE_TIMEOUT[self.name], cwd=ctx.root, env=env,
                    is_cancelled=ctx.cancelled)
                text = "\n".join(lines).strip()
                if code != 0 or not text:
                    raise StageFailed("claude CLI failed for %s/%s (exit %s)"
                                      % (date, day, code))
                if len(text) > SCRIPT_MAX_CHARS:
                    text = text[:SCRIPT_MAX_CHARS].rstrip()
                tmp = os.path.join(ctx.stage_dir(self.name),
                                   "claude_%s_%s.txt" % (date, day))
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                atomic_replace(tmp, target)
                ctx.record("script", target, {"date": date, "day": day})
                written += 1
                ctx.log("script written for %s/%s (%d chars)" % (date, day, len(text)),
                        stage=self.name)
        return {"scripts_written": written, "scripts_skipped": skipped}


class AudioStage(StageAdapter):
    """Google Cloud TTS when configured; gTTS only as an explicit free fallback."""

    name = "audio"
    label = "Voice-over"
    GOOGLE_SYNTHESIZE_URL = (
        "https://texttospeech.googleapis.com/v1/text:synthesize")
    http_post = None  # injectable JSON POST transport for tests

    def engine(self, ctx):
        google = ctx.providers.get("google_tts")
        if google.is_configured() and google.status()["status"] == star_providers.READY:
            return "google_tts"
        return "gtts"

    def prerequisites(self, ctx):
        if self.engine(ctx) == "google_tts":
            # An API key is synthesised over plain HTTP and needs no library;
            # a service account goes through the Google client. That import is
            # checked here rather than at synthesis time because a package that
            # is missing halfway through execute() surfaces to the operator as
            # "internal error while running the job" instead of a reason.
            if ctx.providers.get("google_tts").stored().get("mode") == "api_key":
                return []
            try:
                from google.cloud import texttospeech  # noqa: F401
            except ImportError:
                return ["the stored Google Cloud TTS credential is a service "
                        "account, but the google-cloud-texttospeech package is "
                        "not installed (pip install google-cloud-texttospeech)"]
            return []
        try:
            import gtts  # noqa: F401
        except ImportError:
            return ["neither a Google Cloud TTS credential nor the gTTS "
                    "fallback is available"]
        return []

    def _target(self, ctx, date, day):
        return ctx.project_path("output", date, "audio", "%s.mp3" % day)

    def voice(self, ctx):
        """The exact voice synthesis will ask for, straight from the allowlist."""
        return ctx.providers.get("google_tts").selected_voice()

    def plan(self, ctx):
        engine = self.engine(ctx)
        if engine == "google_tts":
            voice = self.voice(ctx)
            ctx.plan("voice-over engine: Google Cloud TTS (paid, per character); "
                     "voice %s (%s, %s)" % (voice["name"], voice["gender"], voice["tier"]))
        else:
            ctx.plan("voice-over engine: gTTS free fallback "
                     "(no credential, lower quality)")
        for date in ctx.dates:
            for day in ctx.days:
                ctx.plan("synthesise %s/%s" % (date, day),
                         output=os.path.relpath(self._target(ctx, date, day), ctx.root))
        return ctx.planned

    def _script_text(self, ctx, date, day):
        for rel in (("content", "overrides", date, "%s.txt" % day),
                    ("content", "scripts", "claude_%s_%s.txt" % (date, day))):
            path = os.path.join(ctx.root, *rel)
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
        return None

    def execute(self, ctx):
        engine = self.engine(ctx)
        voice = self.voice(ctx) if engine == "google_tts" else None
        rendered = 0
        for date in ctx.dates:
            for day in ctx.days:
                ctx.check_cancelled()
                text = self._script_text(ctx, date, day)
                if not text:
                    raise StageBlocked(
                        "no script for %s/%s — run the script stage first" % (date, day))
                tmp = os.path.join(ctx.stage_dir(self.name), "%s_%s.mp3" % (date, day))
                if engine == "google_tts":
                    self._google(ctx, text, tmp)
                else:
                    self._gtts(text, tmp)
                # Checked before the promote, not after: atomic_replace would
                # happily move a zero-byte file into output/ and the video
                # stage would then render a silent clip from it.
                if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                    raise StageFailed(
                        "the %s engine produced no audio for %s/%s" % (engine, date, day))
                target = self._target(ctx, date, day)
                atomic_replace(tmp, target)
                meta = {"date": date, "day": day, "engine": engine}
                if voice:
                    meta["voice"] = voice["name"]
                ctx.record("audio", target, meta)
                rendered += 1
                ctx.log("audio rendered for %s/%s via %s%s"
                        % (date, day, engine,
                           " (%s)" % voice["name"] if voice else ""),
                        stage=self.name)
        result = {"audio_files": rendered, "engine": engine}
        if voice:
            result["voice"] = voice["name"]
        return result

    def _google(self, ctx, text, out_path):
        star_providers._require_network("google tts synthesis")
        provider = ctx.providers.get("google_tts")
        stored = provider.stored()
        # Name and gender both come from the backend allowlist; nothing a
        # browser sent is trusted at synthesis time.
        voice = provider.selected_voice()
        if stored.get("mode") == "api_key":
            api_key = stored.get("api_key")
            if not isinstance(api_key, str) or not api_key.strip():
                raise StageFailed("Google Cloud TTS API-key credential is unavailable")
            self._google_api_key(text, out_path, api_key, voice)
            return

        try:
            from google.cloud import texttospeech
        except ImportError:
            # Blocked, not failed: nothing is wrong with the job or the
            # credential, the server is simply missing a package.
            raise StageBlocked(
                "the stored Google Cloud TTS credential is a service account, but "
                "the google-cloud-texttospeech package is not installed on this "
                "server") from None
        path = provider.credentials_path()
        if not path:
            raise StageFailed("the stored Google Cloud TTS service-account "
                              "credential has no key file path")
        try:
            client = texttospeech.TextToSpeechClient.from_service_account_file(path)
            response = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(
                    language_code=star_providers.GOOGLE_TTS_LANGUAGE_CODE,
                    name=voice["name"],
                    ssml_gender=getattr(texttospeech.SsmlVoiceGender, voice["gender"])),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.MP3))
            audio = getattr(response, "audio_content", None)
        except Exception as exc:  # noqa: BLE001 - expose only a scrubbed summary
            # The client's own errors quote request bodies and headers, so the
            # text is redacted for the same reason the API-key path redacts it.
            raise StageFailed(
                "Google Cloud TTS service-account synthesis failed: %s"
                % (star_redact.redact_text(str(exc), limit=300)
                   or exc.__class__.__name__)) from None
        if not audio:
            raise StageFailed("Google Cloud TTS returned no audio for this script")
        with open(out_path, "wb") as fh:
            fh.write(audio)

    def _google_api_key(self, text, out_path, api_key, voice):
        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": star_providers.GOOGLE_TTS_LANGUAGE_CODE,
                "name": voice["name"],
                "ssmlGender": voice["gender"],
            },
            "audioConfig": {"audioEncoding": "MP3"},
        }
        headers = {
            "X-Goog-Api-Key": api_key,
            "Content-Type": "application/json",
        }
        poster = self.http_post or _http_post_json
        try:
            response = poster(self.GOOGLE_SYNTHESIZE_URL, payload, headers=headers)
            encoded = response.get("audioContent") if isinstance(response, dict) else None
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("response did not contain audioContent")
            audio = base64.b64decode(encoded, validate=True)
            if not audio:
                raise ValueError("response audioContent decoded to empty audio")
            with open(out_path, "wb") as fh:
                fh.write(audio)
        except Exception as exc:  # noqa: BLE001 - expose only a scrubbed summary
            try:
                error = str(exc)
            except Exception:  # pragma: no cover - pathological exception object
                error = exc.__class__.__name__
            error = error.replace(api_key, star_redact.MASK)
            error = star_redact.redact_text(error, limit=300)
            raise StageFailed(
                "Google Cloud TTS API-key synthesis failed: %s" %
                (error or exc.__class__.__name__)) from None

    def _gtts(self, text, out_path):
        star_providers._require_network("gtts synthesis")
        try:
            from gtts import gTTS
        except ImportError:
            raise StageBlocked("the gTTS fallback is not installed on this "
                               "server and no Google Cloud TTS credential is "
                               "configured") from None
        try:
            gTTS(text=text, lang="th").save(out_path)
        except Exception as exc:  # noqa: BLE001 - a network summary, not a traceback
            raise StageFailed(
                "gTTS synthesis failed: %s"
                % (star_redact.redact_text(str(exc), limit=300)
                   or exc.__class__.__name__)) from None


# ── overlay text ──────────────────────────────────────────────────────
#
# The line burned into a clip has exactly two possible sources and no third:
#
#   auto    the first real hook of the script that was actually generated for
#           that date and birth-day, read from disk at render time. Empty
#           lines, hashtag lines and the contract's boilerplate heading are
#           skipped because none of them is the hook. If the day has no script
#           text at all the clip does not get a generic caption — the stage
#           blocks and says why.
#   custom  the one line the operator typed, already trimmed, length-checked
#           and stripped of control characters by star_jobs. It is used
#           verbatim for every clip in the job; nothing here rewrites it.
#
# Everything below is pure and deterministic: the same script bytes always
# produce the same overlay, which is what makes the dry-run preview honest.

OVERLAY_MIN_CHARS = 8
OVERLAY_MAX_CHARS = 90

# Layout. The safe width is 1080 minus a 96px margin on each side; the glyph
# ratio is the average advance of the Thai faces in THAI_FONT_CANDIDATES, which
# is what turns a font size into a wrap width. MAX_LINES is a hard cap, not a
# working limit: the longest text star_jobs will accept (220 characters) wraps
# to roughly nine lines at the smallest size, so validated input never reaches
# it and no operator text is ever silently cut.
OVERLAY_SAFE_WIDTH = 888
OVERLAY_GLYPH_RATIO = 0.58
OVERLAY_MAX_LINES = 20

# Longest text -> smallest type, in fixed steps so the same string always
# renders at the same size.
OVERLAY_SIZE_STEPS = ((40, 84), (90, 66), (150, 54))
OVERLAY_MIN_SIZE = 44

_SENTENCE_END_RE = re.compile(r"[.!?…。！？]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _heading_fragments():
    """The literal parts of the contract's caption heading, no second copy."""
    parts = [part.strip() for part in re.split(r"\{[^}]*\}", PREDICTION_GUIDE_HEADING)]
    return [part for part in parts if part]


def is_boilerplate_heading(line):
    """True for the dated caption heading every script opens with.

    It is a title, not a hook: it says which day and which date, which the
    viewer can already see. Recognised from PREDICTION_GUIDE_HEADING itself so
    a change to the contract cannot leave this behind.
    """
    fragments = _heading_fragments()
    if len(fragments) < 2:
        return False
    return line.startswith(fragments[0]) and fragments[1] in line


def _clean_overlay_line(raw):
    """One script line reduced to drawable text: no hashtags, no control bytes."""
    line = _CONTROL_RE.sub(" ", raw)
    tokens = [token for token in line.split() if not token.startswith("#")]
    return " ".join(tokens).strip()


def _first_sentence(line):
    """The opening sentence, when the line runs on past one."""
    for match in _SENTENCE_END_RE.finditer(line):
        if match.start() >= OVERLAY_MIN_CHARS:
            return line[:match.start()].strip()
    return line


def bound_overlay_text(text, limit=OVERLAY_MAX_CHARS):
    """Cut to `limit` on a word boundary where there is one. Deterministic."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def auto_overlay_text(script):
    """The clip's own hook, taken from its own script. None when there is none.

    Returning None is a real answer and the caller must treat it as one: there
    is deliberately no fallback string here, because a caption the script never
    said would be a claim this system did not make.
    """
    for raw in (script or "").splitlines():
        line = _clean_overlay_line(raw)
        if not line or len(line) < OVERLAY_MIN_CHARS:
            continue
        if is_boilerplate_heading(line):
            continue
        return bound_overlay_text(_first_sentence(line))
    return None


def overlay_layout(text):
    """(font size, wrap width) for one overlay string. Same input, same layout."""
    length = len(text)
    size = OVERLAY_MIN_SIZE
    for limit, step in OVERLAY_SIZE_STEPS:
        if length <= limit:
            size = step
            break
    width = max(12, int(OVERLAY_SAFE_WIDTH / (OVERLAY_GLYPH_RATIO * size)))
    return size, width


def wrap_overlay_text(text, width, max_lines=OVERLAY_MAX_LINES):
    """Greedy word wrap, hard-splitting any single run longer than a line."""
    lines = []
    current = ""
    for word in text.split():
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        lines[-1] = (last[:width - 1].rstrip() if len(last) >= width else last) + "…"
    return "\n".join(lines)


# The size a caller gets when it does not choose one. Every render in this
# module picks a size from overlay_layout(); this is only the fallback for the
# literal-title form of build_command, and it is the size that form has always
# used, so an existing caller's argv does not change.
DEFAULT_OVERLAY_SIZE = 72

# Symbolic stand-ins used by plan() and nowhere else.
#
# A plan is shown in the browser, so it must not contain a path inside the
# state directory — not the job workdir the overlay file would be staged in,
# and not the assets directory the uploaded image lives in — nor an absolute
# path inside the project itself. These tokens keep the planned argv readable
# and complete while naming nothing an operator could use to locate a file on
# disk. They are never passed to ffmpeg: plan() builds a description,
# execute() builds the command that actually runs.
PLAN_TEXTFILE = "<job-workspace>/video/overlay_%s_%s.txt"
PLAN_BACKGROUND = "<uploaded-background-image>"
PLAN_AUDIO = "<project-output>/%s/audio/%s.mp3"
PLAN_VIDEO = "<project-output>/%s/video/%s.mp4"


class VideoStage(StageAdapter):
    """Deterministic 1080x1920 MP4 built by ffmpeg. No shell, ever."""

    name = "video"
    label = "Video render"

    WIDTH, HEIGHT = 1080, 1920
    BACKGROUND = "#0B1220"
    ACCENT = "#E8C36B"
    # Opacity of the black scrim laid over an uploaded photo. Text legibility
    # is not negotiable, so the scrim is applied to every image regardless of
    # how dark the operator's own picture already is.
    SCRIM = "0.45"

    def prerequisites(self, ctx):
        missing = []
        if shutil.which("ffmpeg") is None:
            missing.append("ffmpeg is not installed on the server")
        if find_thai_font() is None:
            missing.append("no Thai font found (install fonts-noto-core or fonts-thai-tlwg)")
        asset_id = ctx.input.get("background_asset_id")
        if asset_id and not star_assets.exists(ctx.state, asset_id):
            missing.append("the background image this job references is no longer "
                           "stored on the server; upload it again")
        return missing

    def _target(self, ctx, date, day):
        return ctx.project_path("output", date, "video", "%s.mp4" % day)

    # -- overlay ---------------------------------------------------------
    def script_text(self, ctx, date, day):
        """The day's real script: hand-edited override first, then the source."""
        for rel in (("content", "overrides", date, "%s.txt" % day),
                    ("content", "scripts", "claude_%s_%s.txt" % (date, day))):
            path = os.path.join(ctx.root, *rel)
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        return fh.read()
                except OSError:
                    return None
        return None

    def overlay_mode(self, ctx):
        mode = ctx.input.get("overlay_text_mode")
        if mode in star_jobs.OVERLAY_TEXT_MODES:
            return mode
        return star_jobs.DEFAULT_OVERLAY_TEXT_MODE

    def overlay_text(self, ctx, date, day):
        """Exactly what this clip will draw, or StageBlocked explaining why not."""
        if self.overlay_mode(ctx) == "custom":
            text = (ctx.input.get("custom_overlay_text") or "").strip()
            if not text:
                raise StageBlocked("this job asks for custom overlay text but "
                                   "carries none")
            return text
        text = auto_overlay_text(self.script_text(ctx, date, day))
        if not text:
            raise StageBlocked(
                "no script text to caption %s/%s with — the automatic overlay is "
                "the opening line of that day's own script, and this server does "
                "not draw a generic caption instead; run the script stage first "
                "or type a custom line" % (date, day))
        return text

    def overlay_file(self, ctx, date, day):
        """Where the drawn text is staged. Inside the job workdir, 0600."""
        return os.path.join(ctx.workdir, self.name,
                            "overlay_%s_%s.txt" % (date, day))

    def _write_overlay(self, ctx, date, day, text, width):
        """Hand the text to ffmpeg as a file, never as a filter argument.

        drawtext's `text=` value is parsed by the filter's own mini-syntax, so
        a colon or a backslash in an operator's line would change the filter
        rather than the caption. `textfile=` is read as bytes and cannot, which
        is why the escaping below only ever applies to paths we chose.
        """
        ctx.stage_dir(self.name)
        path = self.overlay_file(ctx, date, day)
        ctx.state.write_bytes(path, wrap_overlay_text(text, width).encode("utf-8"))
        return path

    # -- background ------------------------------------------------------
    def background_path(self, ctx):
        """The uploaded image for this job, or None. Never a caller's path."""
        asset_id = ctx.input.get("background_asset_id")
        if not asset_id:
            return None
        path = star_assets.background_path(ctx.state, asset_id)
        if path is None:
            raise StageBlocked("the background image this job references is no "
                               "longer stored on the server; upload it again and "
                               "start a new job")
        return path

    def build_command(self, audio_path, out_path, title=None, font=None,
                      fontsize=DEFAULT_OVERLAY_SIZE, background=None,
                      textfile=None):
        """The exact ffmpeg argv. Split out so tests can assert it without rendering.

        argv only: every value below is either a constant, a path this module
        chose, or a number — nothing here is ever handed to a shell.

        The caption can arrive two ways and the pipeline only ever uses one of
        them. `textfile` is a path ffmpeg reads as raw bytes, and it is what
        every render in this module passes, because operator text put into
        `text=` would be parsed by drawtext's own mini-syntax rather than drawn.
        `title` is the older literal-caption form, escaped into the filter and
        kept for callers that have a string and no file to point at; it is
        never how job text reaches ffmpeg. Passing both is a programming error.
        """
        if textfile is not None and title is not None:
            raise ValueError("pass either a title or a textfile, not both")
        if textfile is None and title is None:
            raise ValueError("the overlay needs either a title or a textfile")
        caption = ("textfile=%s" % _ffmpeg_escape(textfile) if textfile is not None
                   else "text=%s" % _ffmpeg_escape(title))
        drawtext = ":".join([
            "fontfile=%s" % _ffmpeg_escape(font or ""),
            caption,
            "fontcolor=%s" % self.ACCENT,
            "fontsize=%d" % fontsize,
            "line_spacing=%d" % max(8, fontsize // 5),
            "borderw=3",
            "bordercolor=black@0.85",
            "x=(w-text_w)/2",
            "y=(h-text_h)/2",
        ])
        if background:
            # Cover, then crop: the photo fills 1080x1920 with its aspect ratio
            # intact and the overflow trimmed, so nothing is ever stretched.
            # The scrim goes under the text and over the picture.
            source = ["-loop", "1", "-framerate", "30", "-i", background]
            video_filter = ",".join([
                "scale=%d:%d:force_original_aspect_ratio=increase"
                % (self.WIDTH, self.HEIGHT),
                "crop=%d:%d" % (self.WIDTH, self.HEIGHT),
                "setsar=1",
                "drawbox=x=0:y=0:w=iw:h=ih:color=black@%s:t=fill" % self.SCRIM,
                "drawtext=" + drawtext,
            ])
        else:
            source = ["-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=30"
                      % (self.BACKGROUND, self.WIDTH, self.HEIGHT)]
            video_filter = "drawtext=" + drawtext
        return [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
        ] + source + [
            "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            out_path,
        ]

    def plan(self, ctx):
        """Describe the render. Writes nothing: no text file, no asset, no dir.

        A dry run is a preview, so every clip gets the ffmpeg argv it would be
        rendered with — including the clips whose overlay text does not exist
        yet. That is not a promise the render would succeed: where the text is
        missing the description says so in the operator's own terms, and the
        argv carries a symbolic textfile rather than an invented caption. The
        real execute() still refuses to run that clip.

        Nothing here touches disk beyond the read-only existence check the
        background needs, and no absolute path reaches the plan: the overlay
        file, the uploaded image, the audio input and the .mp4 output all
        appear as tokens. The entry's `output` field still carries the real
        project-relative path, which is what the operator needs to find the
        clip afterwards.
        """
        font = find_thai_font()
        mode = self.overlay_mode(ctx)
        asset_id = ctx.input.get("background_asset_id")
        has_background = bool(asset_id) and star_assets.exists(ctx.state, asset_id)
        ctx.plan("render %dx%d MP4 with font %s"
                 % (self.WIDTH, self.HEIGHT, font or "<missing>"))
        if asset_id:
            if has_background:
                ctx.plan("composite the uploaded background image behind the text: "
                         "scaled to cover %dx%d, centre-cropped, darkened by a "
                         "black@%s scrim" % (self.WIDTH, self.HEIGHT, self.SCRIM))
            else:
                ctx.plan("the background image this job references is no longer "
                         "stored on the server, so the render would block")
        else:
            ctx.plan("solid %s background; no image was uploaded with this job"
                     % self.BACKGROUND)
        if mode == "custom":
            ctx.plan("overlay text: the operator's custom line, used unchanged on "
                     "every clip in this job")
        else:
            ctx.plan("overlay text: taken from each date and birth-day's own "
                     "script (hand-edited override first, then the generated "
                     "script); no placeholder is ever drawn")

        for date in ctx.dates:
            for day in ctx.days:
                # `target` is only ever used for the operator-facing `output`
                # field, which is project-relative. The argv gets the tokens.
                target = self._target(ctx, date, day)
                audio_token = PLAN_AUDIO % (date, day)
                video_token = PLAN_VIDEO % (date, day)
                try:
                    text = self.overlay_text(ctx, date, day)
                    blocked = None
                except StageBlocked as exc:
                    text, blocked = None, str(exc)
                fontsize, _width = overlay_layout(text or "")
                description = (
                    "render %s/%s with the overlay %r" % (date, day, text)
                    if text is not None else
                    "render %s/%s would block: %s — the command below is what "
                    "would run once that text exists" % (date, day, blocked))
                ctx.plan(
                    description,
                    command=self.build_command(
                        audio_token, video_token, font=font or "<missing>",
                        fontsize=fontsize,
                        background=PLAN_BACKGROUND if has_background else None,
                        textfile=PLAN_TEXTFILE % (date, day)),
                    output=os.path.relpath(target, ctx.root))
        return ctx.planned

    def execute(self, ctx):
        font = find_thai_font()
        if font is None:
            raise StageBlocked("no Thai font found on the server")
        background = self.background_path(ctx)
        mode = self.overlay_mode(ctx)
        rendered = 0
        for date in ctx.dates:
            for day in ctx.days:
                ctx.check_cancelled()
                audio = os.path.join(ctx.root, "output", date, "audio", "%s.mp3" % day)
                if not os.path.isfile(audio):
                    raise StageBlocked(
                        "no audio for %s/%s — run the audio stage first" % (date, day))
                text = self.overlay_text(ctx, date, day)
                fontsize, width = overlay_layout(text)
                textfile = self._write_overlay(ctx, date, day, text, width)
                tmp = os.path.join(ctx.stage_dir(self.name), "%s_%s.mp4" % (date, day))
                argv = self.build_command(audio, tmp, font=font, fontsize=fontsize,
                                          background=background,
                                          textfile=textfile)
                code, lines = run_command(
                    argv, timeout=STAGE_TIMEOUT[self.name], cwd=ctx.root,
                    is_cancelled=ctx.cancelled)
                if code != 0 or not os.path.isfile(tmp):
                    raise StageFailed("ffmpeg failed for %s/%s (exit %s): %s"
                                      % (date, day, code, " | ".join(lines[-3:])))
                target = self._target(ctx, date, day)
                atomic_replace(tmp, target)
                ctx.record("video", target, {"date": date, "day": day,
                                             "overlay_text_mode": mode})
                rendered += 1
                ctx.log("video rendered for %s/%s (%s overlay%s)"
                        % (date, day, mode,
                           ", uploaded background" if background else ""),
                        stage=self.name)
        return {"videos": rendered, "overlay_text_mode": mode,
                "background_image": bool(background)}


def _ffmpeg_escape(text):
    """Escape for the drawtext filter's own mini-syntax (not a shell)."""
    for old, new in (("\\", "\\\\"), (":", "\\:"), ("'", "\\'"),
                     ("%", "\\%"), (",", "\\,"), ("[", "\\["), ("]", "\\]")):
        text = text.replace(old, new)
    return text


# Publishing order is a dependency order, not the order the boxes were ticked
# in. A LINE message carries no bytes: it links to the .mp4 that the R2 upload
# put on the public base URL, so R2 has to finish before the broadcast goes
# out. star_jobs' canonical platform order is the vocabulary order and puts
# "line" before "r2", which meant a job selecting both broadcast a link to a
# file that had not been uploaded yet. A LINE broadcast is metered and cannot
# be recalled, so this ordering is load-bearing rather than cosmetic.
PUBLISH_ORDER = ("r2", "youtube", "facebook", "line", "tiktok", "shopee")


def publish_sequence(platforms):
    """The selected platforms in dependency order; anything unknown goes last."""
    selected = list(platforms or [])
    ordered = [name for name in PUBLISH_ORDER if name in selected]
    return ordered + [name for name in selected if name not in PUBLISH_ORDER]


class PublishStage(StageAdapter):
    """Per-platform publishing, with a handoff package for manual platforms."""

    name = "publish"
    label = "Publish"

    # LINE links to media hosted elsewhere, so it cannot be the only target.
    LINE_NEEDS_R2 = ("LINE: a LINE message links to the video hosted on Cloudflare "
                     "R2, so R2 must be published by the same job. Add r2 to the "
                     "platform list, or the broadcast would advertise a URL this "
                     "job never uploaded.")

    def prerequisites(self, ctx):
        missing = []
        platforms = ctx.input.get("platforms") or []
        for platform in publish_sequence(platforms):
            provider = ctx.providers.get(platform)
            if provider.automation == star_providers.AUTOMATION_FULL:
                reason = provider.prerequisite_error()
                if reason:
                    missing.append("%s: %s" % (provider.label, reason))
        # Checked here, before a single platform is contacted: discovering this
        # halfway through execute() would mean YouTube and Facebook had already
        # published while the broadcast the operator actually wanted blocks.
        if "line" in platforms and "r2" not in platforms:
            missing.append(self.LINE_NEEDS_R2)
        return missing

    def plan(self, ctx):
        platforms = ctx.input.get("platforms") or []
        if "line" in platforms and "r2" not in platforms:
            ctx.plan("publish would block: " + self.LINE_NEEDS_R2)
        for platform in publish_sequence(platforms):
            provider = ctx.providers.get(platform)
            if provider.automation == star_providers.AUTOMATION_FULL:
                reason = provider.prerequisite_error()
                ctx.plan("publish to %s%s" % (
                    provider.label, "" if reason is None else " — BLOCKED: " + reason))
            else:
                ctx.plan("prepare a manual handoff package for %s (this server never "
                         "claims a %s post succeeded)" % (provider.label, provider.label),
                         output="output/<date>/handoff/%s/" % platform)
        return ctx.planned

    def execute(self, ctx):
        results = {}
        platforms = ctx.input.get("platforms") or []
        if "line" in platforms and "r2" not in platforms:
            raise StageBlocked(self.LINE_NEEDS_R2)
        # Dependency order, so the R2 object exists before LINE links to it.
        for platform in publish_sequence(platforms):
            ctx.check_cancelled()
            provider = ctx.providers.get(platform)
            if provider.automation != star_providers.AUTOMATION_FULL:
                results[platform] = self._handoff(ctx, provider)
                continue
            reason = provider.prerequisite_error()
            if reason:
                raise StageBlocked("%s: %s" % (provider.label, reason))
            results[platform] = self._publish(ctx, platform, provider)
        return {"publish": results}

    # -- manual --------------------------------------------------------
    def _handoff(self, ctx, provider):
        """Copy media plus written instructions somewhere the operator can grab."""
        prepared = []
        for date in ctx.dates:
            for day in ctx.days:
                video = os.path.join(ctx.root, "output", date, "video", "%s.mp4" % day)
                if not os.path.isfile(video):
                    continue
                target_dir = ctx.project_path("output", date, "handoff", provider.key)
                os.makedirs(target_dir, exist_ok=True)
                shutil.copy2(video, os.path.join(target_dir, "%s.mp4" % day))
                caption = self._caption(ctx, date, day)
                with open(os.path.join(target_dir, "%s.txt" % day), "w",
                          encoding="utf-8") as fh:
                    fh.write(caption or "")
                with open(os.path.join(target_dir, "README.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write("%s manual upload\n\n%s\n" % (
                        provider.label, "\n".join("- " + p for p in provider.prerequisites)))
                prepared.append("%s/%s" % (date, day))
        ctx.log("prepared %d handoff item(s) for %s" % (len(prepared), provider.label),
                stage=self.name)
        return {"status": "manual_handoff", "published": False,
                "items": prepared,
                "note": "not published; upload manually from output/<date>/handoff/%s"
                        % provider.key}

    def _caption(self, ctx, date, day):
        for rel in (("content", "overrides", date, "%s.txt" % day),
                    ("content", "scripts", "claude_%s_%s.txt" % (date, day))):
            path = os.path.join(ctx.root, *rel)
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
        return None

    # -- automated -----------------------------------------------------
    def _publish(self, ctx, platform, provider):
        handler = {
            "r2": self._publish_r2,
            "youtube": self._publish_youtube,
            "facebook": self._publish_facebook,
            "line": self._publish_line,
        }.get(platform)
        if handler is None:
            raise StageBlocked("no publishing adapter for %s" % platform)
        done = []
        for date in ctx.dates:
            for day in ctx.days:
                ctx.check_cancelled()
                video = os.path.join(ctx.root, "output", date, "video", "%s.mp4" % day)
                if not os.path.isfile(video):
                    raise StageBlocked(
                        "no video for %s/%s — run the video stage first" % (date, day))
                done.append(handler(ctx, provider, date, day, video))
        return {"status": "published", "published": True, "items": done}

    def _publish_r2(self, ctx, provider, date, day, video):
        star_providers._require_network("r2 upload")
        import boto3
        stored = provider.stored()
        client = boto3.client("s3", endpoint_url=provider.endpoint(),
                              aws_access_key_id=stored["access_key_id"],
                              aws_secret_access_key=stored["secret_access_key"],
                              region_name="auto")
        key = "star/%s/%s.mp4" % (date, day)
        with open(video, "rb") as fh:
            client.put_object(Bucket=stored["bucket"], Key=key, Body=fh,
                              ContentType="video/mp4")
        return {"date": date, "day": day,
                "url": "%s/%s" % (stored["public_base_url"], key)}

    def _publish_youtube(self, ctx, provider, date, day, video):
        star_providers._require_network("youtube upload")
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        stored = provider.stored()
        creds = Credentials(
            None, refresh_token=stored["refresh_token"],
            token_uri=provider.TOKEN_ENDPOINT,
            client_id=stored["client_id"], client_secret=stored["client_secret"],
            scopes=list(provider.SCOPES))
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        title = "ดวงของชาววัน%s %s" % (DAY_TH.get(day, day), date)
        request = youtube.videos().insert(
            part="snippet,status",
            body={"snippet": {"title": title[:100],
                              "description": (self._caption(ctx, date, day) or "")[:4900]},
                  "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}},
            media_body=MediaFileUpload(video, chunksize=-1, resumable=True))
        response = request.execute()
        return {"date": date, "day": day, "video_id": response.get("id"),
                "privacy": "private"}

    def _publish_facebook(self, ctx, provider, date, day, video):
        star_providers._require_network("facebook upload")
        import requests
        stored = provider.stored()
        url = "%s/%s/videos" % (provider.GRAPH, stored["page_id"])
        with open(video, "rb") as fh:
            response = requests.post(
                url,
                data={"description": (self._caption(ctx, date, day) or "")[:2000]},
                files={"source": fh},
                headers={"Authorization": "Bearer " + stored["page_access_token"]},
                timeout=600)
        if response.status_code >= 400:
            raise StageFailed("facebook upload failed with HTTP %d" % response.status_code)
        return {"date": date, "day": day, "post_id": response.json().get("id")}

    def _publish_line(self, ctx, provider, date, day, video):
        star_providers._require_network("line broadcast")
        import urllib.request
        stored = provider.stored()
        r2 = ctx.providers.get("r2")
        if not r2.is_configured():
            raise StageBlocked(
                "LINE needs a public media URL; configure Cloudflare R2 and include "
                "it in the platform list so the video is hosted first")
        # Belt and braces behind the preflight check. A broadcast cannot be
        # recalled, so the URL is only ever built for a job that also uploaded
        # the object it points at.
        if "r2" not in (ctx.input.get("platforms") or []):
            raise StageBlocked(self.LINE_NEEDS_R2)
        media_url = "%s/star/%s/%s.mp4" % (r2.stored()["public_base_url"], date, day)
        endpoint = ("https://api.line.me/v2/bot/message/broadcast"
                    if stored.get("broadcast", True)
                    else "https://api.line.me/v2/bot/message/push")
        payload = json.dumps({"messages": [
            {"type": "text", "text": (self._caption(ctx, date, day) or "")[:1000]},
            {"type": "video", "originalContentUrl": media_url,
             "previewImageUrl": media_url.replace(".mp4", ".jpg")},
        ]}).encode("utf-8")
        request = urllib.request.Request(
            endpoint, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + stored["channel_access_token"]})
        with urllib.request.urlopen(request, timeout=60) as response:
            code = response.status
        return {"date": date, "day": day, "http_status": code, "endpoint": endpoint}


STAGE_ADAPTERS = {
    "astro": AstroStage(),
    "script": ScriptStage(),
    "audio": AudioStage(),
    "video": VideoStage(),
    "publish": PublishStage(),
}


# ── youtube oauth (PKCE) ──────────────────────────────────────────────

class YouTubeOAuth:
    """Authorisation-code flow with PKCE.

    The exchange is factored behind `token_exchange` so the callback path is
    unit-testable without contacting Google.
    """

    def __init__(self, service):
        self.service = service
        self.token_exchange = None  # injectable

    def _provider(self):
        return self.service.providers.get("youtube")

    def redirect_uri(self, host=None, scheme="https"):
        stored = self._provider().stored()
        configured = stored.get("redirect_uri")
        if configured:
            return configured
        if host:
            return "%s://%s/api/oauth/youtube/callback" % (scheme, host)
        return None

    def start(self, host=None, scheme="https"):
        provider = self._provider()
        stored = provider.stored()
        if not stored.get("client_id"):
            raise star_providers.ProviderError(
                "upload the YouTube OAuth client JSON before starting the flow")
        redirect_uri = self.redirect_uri(host, scheme)
        if not redirect_uri:
            raise star_providers.ProviderError(
                "no redirect URI configured and the request carried no Host header")

        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(32)
        expires = self.service.store.put_oauth_state(
            state, "youtube", verifier, redirect_uri, ttl_seconds=600)

        params = {
            "client_id": stored["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(provider.SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return {
            "authorization_url": provider.AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params),
            "redirect_uri": redirect_uri,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "state_issued": True,
            "note": "Register this exact redirect URI in the Google Cloud console "
                    "before authorising.",
        }

    def callback(self, query):
        """Complete the flow. `query` is the parsed callback query dict."""
        error = query.get("error")
        state = query.get("state")
        code = query.get("code")

        if not isinstance(state, str) or not state:
            raise star_providers.ProviderError("callback is missing the state parameter",
                                               "state")
        record, reason = self.service.store.consume_oauth_state(state, "youtube")
        if record is None:
            # Consumed first, so a replayed callback fails even if it carries a
            # valid-looking code.
            raise star_providers.ProviderError("oauth state is %s" % reason, "state")
        if error:
            raise star_providers.ProviderError(
                "authorisation was refused: %s" % star_redact.redact_text(str(error), 200))
        if not isinstance(code, str) or not code:
            raise star_providers.ProviderError("callback is missing the code parameter",
                                               "code")

        provider = self._provider()
        stored = provider.stored()
        exchange = self.token_exchange or _exchange_code
        payload = exchange({
            "code": code,
            "client_id": stored["client_id"],
            "client_secret": stored["client_secret"],
            "redirect_uri": record["redirect_uri"],
            "grant_type": "authorization_code",
            "code_verifier": record["code_verifier"],
        }, provider.TOKEN_ENDPOINT)

        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            raise star_providers.ProviderError(
                "the token response contained no refresh token; re-run the flow with "
                "prompt=consent and confirm offline access is granted")
        payload = dict(payload)
        payload["connected_at"] = star_jobs.utcnow()
        provider.save_tokens(payload)
        # Return status only — never the tokens.
        return provider.status()


def _exchange_code(form, endpoint):
    star_providers._require_network("youtube token exchange")
    import urllib.request
    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post_json(url, payload, headers=None):
    """POST a JSON object with urllib and return a decoded JSON object."""
    star_providers._require_network("HTTP JSON POST")
    import urllib.request
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(
            request, timeout=star_providers.HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


# ── pipeline execution ────────────────────────────────────────────────

class Pipeline:
    def __init__(self, service):
        self.service = service

    def preflight(self, ctx):
        """Prerequisite check for every requested stage, before anything runs."""
        blocked = []
        for name in ctx.input.get("stages") or []:
            adapter = STAGE_ADAPTERS.get(name)
            if adapter is None:
                blocked.append("unknown stage: %s" % name)
                continue
            blocked.extend("%s: %s" % (name, item) for item in adapter.prerequisites(ctx))
        return blocked

    def dry_run(self, ctx):
        """Build the plan without calling execute() on any adapter."""
        report = {"dry_run": True, "stages": []}
        blocked = self.preflight(ctx)
        for name in ctx.input.get("stages") or []:
            adapter = STAGE_ADAPTERS[name]
            ctx.planned = []
            adapter.plan(ctx)
            report["stages"].append({
                "stage": name,
                "label": adapter.label,
                "unmet_prerequisites": adapter.prerequisites(ctx),
                "planned": list(ctx.planned),
            })
            ctx.log("dry run planned %d action(s) for stage %s"
                    % (len(ctx.planned), name), stage=name)
        report["unmet_prerequisites"] = blocked
        report["provider_calls_made"] = 0
        report["note"] = ("dry run only: no provider was contacted, no paid call was "
                          "made and nothing was written outside the job workspace")
        return report

    def run(self, ctx):
        stages = ctx.input.get("stages") or []
        blocked = self.preflight(ctx)
        if blocked:
            raise StageBlocked("; ".join(blocked[:6]))

        results = {"dry_run": False, "stages": []}
        total = len(stages)
        for index, name in enumerate(stages):
            ctx.check_cancelled()
            adapter = STAGE_ADAPTERS[name]
            ctx.progress(int(index * 100 / total), stage=name)
            ctx.log("stage %s starting" % name, stage=name)
            outcome = adapter.execute(ctx)
            results["stages"].append({"stage": name, "label": adapter.label,
                                      "result": star_redact.redact_obj(outcome)})
            ctx.log("stage %s finished" % name, stage=name)
        ctx.progress(100, stage=None)
        results["artifacts"] = ctx.artifacts[:200]
        return results


# ── background runner ─────────────────────────────────────────────────

class JobRunner(threading.Thread):
    """One thread, one job at a time. The DB is the source of truth."""

    POLL_SECONDS = 1.0

    def __init__(self, service):
        super().__init__(name="star-job-runner", daemon=True)
        self.service = service
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                job = self.service.store.claim_next()
            except Exception as exc:  # noqa: BLE001 - a runner must never die
                self.service.log_internal("claim failed: %r" % exc)
                job = None
            if job is None:
                self._idle.set()
                self._stop.wait(self.POLL_SECONDS)
                continue
            self._idle.clear()
            try:
                self.service.execute_job(job)
            except Exception as exc:  # noqa: BLE001
                self.service.log_internal("job %s crashed: %r" % (job["id"], exc))
                try:
                    self.service.store.finish_job(
                        job["id"], "failed",
                        safe_error="internal error while running the job")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._idle.set()

    def wait_idle(self, timeout=30):
        """Test helper: block until the queue has been drained."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.service.store.active_job() is None and self._idle.is_set():
                return True
            time.sleep(0.05)
        return False


class Scheduler(threading.Thread):
    """One daily run in Asia/Bangkok. Never retries, never runs twice a day."""

    POLL_SECONDS = 30.0

    def __init__(self, service, poll_seconds=None):
        super().__init__(name="star-scheduler", daemon=True)
        self.service = service
        self.poll_seconds = poll_seconds or self.POLL_SECONDS
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.service.log_internal("scheduler tick failed: %r" % exc)
            self._stop.wait(self.poll_seconds)

    def tick(self, now=None):
        """Return the created job, or None. Safe to call repeatedly."""
        config = self.service.store.get_schedule()
        if not config.get("enabled"):
            return None
        now = now or datetime.now(BANGKOK)
        run_date = now.date().isoformat()
        if config.get("last_run_date") == run_date:
            return None
        hour, minute = (int(part) for part in config["time"].split(":"))
        if (now.hour, now.minute) < (hour, minute):
            return None

        if not self.service.store.claim_schedule_run(run_date):
            return None  # another tick already won the race

        # Everything past the claim runs under the release: the claim is what
        # stops a second tick, so anything that stops *this* tick from queueing
        # a job has to hand the day back. A stored schedule that no longer
        # validates (an old row, a hand-edited database) used to raise straight
        # out of tick() with the day already claimed, which silently skipped the
        # run and left no job to explain why.
        try:
            target = now.date() + timedelta(days=config["date_offset_days"])
            job_input = star_jobs.validate_job_input({
                "from_date": target.isoformat(),
                "to_date": target.isoformat(),
                "days": config["days"],
                "stages": config["stages"],
                "platforms": config["platforms"] or None,
                "dry_run": config["dry_run"],
            })
            job = self.service.store.create_job(job_input, origin="schedule")
        except JobConflict:
            # A manual job is running. Hand the day back so a later tick can
            # still run it once the queue drains, rather than losing the run.
            self.service.store.release_schedule_run(run_date)
            return None
        except JobValidationError as exc:
            self.service.store.release_schedule_run(run_date)
            self.service.log_internal(
                "scheduled run for %s rejected by validation: %s" % (run_date, exc))
            return None
        except Exception:
            self.service.store.release_schedule_run(run_date)
            raise
        self.service.store.attach_schedule_job(run_date, job["id"])
        return job


# ── service facade used by the API layer ──────────────────────────────

class AutomationService:
    def __init__(self, root, state_dir=None, start_threads=True, scheduler_poll=None):
        self.root = os.path.realpath(root)
        # resolve_state_dir applies the STAR_STATE_DIR env var and the
        # /var/lib/star production default; tests always pass an explicit path.
        self.state = StateDir(resolve_state_dir(state_dir))
        self.store = JobStore(self.state.db_path)
        self.providers = star_providers.ProviderRegistry(self.state)
        self.pipeline = Pipeline(self)
        self.oauth = YouTubeOAuth(self)
        self.started_at = star_jobs.utcnow()
        self._log_lock = threading.Lock()

        self.recovered = self.store.recover_orphans()
        self.store.purge_oauth_states()

        self.runner = JobRunner(self)
        self.scheduler = Scheduler(self, poll_seconds=scheduler_poll)
        if start_threads:
            self.runner.start()
            self.scheduler.start()

    def close(self):
        self.runner.stop()
        self.scheduler.stop()

    # -- prediction style guide -----------------------------------------
    def prediction_guide_view(self):
        """The canonical guide plus this build's verdict on it.

        The page that renders the guide and the prompt that consumes it read
        the same file through the same validator, so an operator can never be
        looking at rules the script stage has already rejected.
        """
        view = {
            "source": PREDICTION_GUIDE_RELPATH,
            "supported_major": PREDICTION_GUIDE_SUPPORTED_MAJOR,
            "required_heading": PREDICTION_GUIDE_HEADING,
            "hashtag_count": PREDICTION_GUIDE_HASHTAG_COUNT,
            "max_prompt_chars": GUIDE_MAX_CHARS,
        }
        try:
            guide = load_prediction_guide(self.root)
        except GuideError as exc:
            view.update({"valid": False, "error": str(exc), "guide": None,
                         "version": None, "prompt_chars": 0})
            return view
        view.update({
            "valid": True,
            "error": None,
            "version": guide.get("version"),
            "prompt_chars": len(prediction_guide_prompt_block(guide)),
            "guide": guide,
        })
        return view

    def log_internal(self, message):
        import sys
        with self._log_lock:
            sys.stderr.write("star-automation: %s\n"
                             % star_redact.redact_text(message, limit=1000))

    # -- background images ----------------------------------------------
    def protected_asset_ids(self):
        """Assets a job still needs. Retention may never touch these.

        "Still needs" means referenced by a job that has not finished — queued
        or running. An image uploaded for a job that is waiting its turn is as
        protected as one being rendered right now.
        """
        keep = set()
        for status in star_jobs.ACTIVE_STATES:
            for job in self.store.list_jobs(limit=star_jobs.MAX_JOBS_RETURNED,
                                            status=status):
                value = (job.get("input") or {}).get("background_asset_id")
                if isinstance(value, str) and value:
                    keep.add(value)
        return keep

    def store_background(self, data):
        """Validate and store uploaded image bytes, then trim old ones.

        Pruning is deliberately part of the upload path rather than a timer: it
        runs when the directory has just grown, and it can only ever remove
        images that no unfinished job references.
        """
        meta = star_assets.store_background(self.state, data)
        try:
            self.prune_backgrounds()
        except (OSError, StateError) as exc:  # retention must not fail an upload
            self.log_internal("background prune failed: %r" % exc)
        return meta

    def prune_backgrounds(self):
        return star_assets.prune_backgrounds(
            self.state, keep_ids=self.protected_asset_ids())

    def background_exists(self, asset_id):
        return star_assets.exists(self.state, asset_id)

    # -- job execution --------------------------------------------------
    def execute_job(self, job):
        ctx = JobContext(self, job, self.state.job_dir(job["id"]))
        dry = bool(job["input"].get("dry_run"))
        ctx.log("job started (%s)" % ("dry run" if dry else "production run"))
        try:
            if ctx.cancelled():
                raise JobCancelled("cancelled before the first stage")
            if dry:
                result = self.pipeline.dry_run(ctx)
                self.store.finish_job(job["id"], "succeeded", result=result, progress=100)
                ctx.log("dry run complete; no provider was contacted")
            else:
                result = self.pipeline.run(ctx)
                self.store.finish_job(job["id"], "succeeded", result=result, progress=100)
                ctx.log("job succeeded")
        except JobCancelled as exc:
            self.store.finish_job(job["id"], "cancelled",
                                  safe_error=star_redact.redact_text(str(exc), 400))
            ctx.log("job cancelled: %s" % exc, level="warn")
        except StageBlocked as exc:
            self.store.finish_job(job["id"], "blocked",
                                  safe_error=star_redact.redact_text(str(exc), 800))
            ctx.log("job blocked: %s" % exc, level="error")
        except subprocess.TimeoutExpired as exc:
            self.store.finish_job(job["id"], "failed",
                                  safe_error="a stage exceeded its %ss timeout"
                                             % getattr(exc, "timeout", "?"))
            ctx.log("job timed out", level="error")
        except StageFailed as exc:
            self.store.finish_job(job["id"], "failed",
                                  safe_error=star_redact.redact_text(str(exc), 800))
            ctx.log("job failed: %s" % exc, level="error")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            self.log_internal("job %s error: %r" % (job["id"], exc))
            self.store.finish_job(job["id"], "failed",
                                  safe_error="internal error while running the job")
            ctx.log("job failed with an internal error", level="error")
        finally:
            if not dry:
                self._cleanup_workspace(job["id"])
        return self.store.get_job(job["id"])

    def _cleanup_workspace(self, job_id):
        try:
            shutil.rmtree(self.state.job_dir(job_id), ignore_errors=True)
        except OSError:
            pass

    # -- overview -------------------------------------------------------
    def overview(self):
        jobs = self.store.list_jobs(limit=10)
        active = self.store.active_job()
        schedule = self.store.get_schedule()
        statuses = self.providers.statuses()
        ready = [s for s in statuses if s["status"] == star_providers.READY]
        needs = [s for s in statuses
                 if s["status"] in (star_providers.NOT_CONFIGURED, star_providers.ERROR)]
        counts = {}
        for job in self.store.list_jobs(limit=star_jobs.MAX_JOBS_RETURNED):
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        return {
            "generated_at": star_jobs.utcnow(),
            "service_started_at": self.started_at,
            "recovered_jobs": len(self.recovered),
            "active_job": active,
            "recent_jobs": jobs,
            "job_counts": counts,
            "providers": statuses,
            "providers_ready": len(ready),
            "providers_pending": len(needs),
            "schedule": schedule,
            "stages": [{"stage": name, "label": STAGE_ADAPTERS[name].label}
                       for name in star_jobs.STAGES],
            "platforms": {
                "automatable": list(star_jobs.AUTOMATABLE_PLATFORMS),
                "manual": list(star_jobs.MANUAL_PLATFORMS),
            },
            "limits": {
                "max_range_days": star_jobs.MAX_RANGE_DAYS,
                "max_concurrent_jobs": 1,
                "stage_timeouts_seconds": dict(STAGE_TIMEOUT),
                # The run form reads these rather than carrying its own copy,
                # so the client-side check and the server's limit cannot drift.
                "video": {
                    "overlay_text_modes": list(star_jobs.OVERLAY_TEXT_MODES),
                    "default_overlay_text_mode": star_jobs.DEFAULT_OVERLAY_TEXT_MODE,
                    "max_custom_overlay_text": star_jobs.MAX_CUSTOM_OVERLAY_TEXT,
                    "background_max_bytes": star_assets.MAX_UPLOAD_BYTES,
                    "background_content_types": list(
                        star_assets.ACCEPTED_CONTENT_TYPES),
                },
            },
            "state": {
                # Uploaded images are operator content living in the same
                # owner-only tree as the credentials, so the health report
                # checks their modes too rather than only the ones written by
                # star_state itself.
                "permission_problems": self.state.audit() + star_assets.audit(self.state),
                "network_disabled": star_providers.network_disabled(),
            },
        }
