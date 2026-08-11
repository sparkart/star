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
import secrets
import selectors
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import star_jobs
import star_providers
import star_redact
from star_jobs import JobConflict, JobStore, JobValidationError
from star_state import StateDir, resolve_state_dir

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
    try:
        while True:
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            for _key, _mask in selector.select(timeout=0.5):
                line = proc.stdout.readline()
                if not line:
                    break
                line = star_redact.redact_text(line.rstrip("\n")[:MAX_LINE], limit=MAX_LINE)
                if len(captured) < MAX_CAPTURED_LINES:
                    captured.append(line)
                if on_line is not None and line:
                    on_line(line)
            if proc.poll() is not None:
                # Drain whatever is still buffered before giving up the loop.
                for line in proc.stdout:
                    line = star_redact.redact_text(line.rstrip("\n")[:MAX_LINE], limit=MAX_LINE)
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
        provider = ctx.providers.get("claude")
        status = provider.status()
        if status["status"] == star_providers.NOT_CONFIGURED:
            return ["Claude CLI is not available: " + status["detail"]]
        return []

    def _prompt(self, date, day, prediction):
        """Deterministic prompt built from the day's real prediction data."""
        th = DAY_TH.get(day, day)
        facts = json.dumps(prediction, ensure_ascii=False, sort_keys=True)[:4000]
        return (
            "เขียนสคริปต์พากย์ดวงรายวันภาษาไทย สำหรับชาววันเกิด" + th + " "
            "ประจำวันที่ " + date + "\n"
            "ใช้ข้อมูลโหราศาสตร์ต่อไปนี้เท่านั้น ห้ามแต่งข้อมูลดาวเพิ่ม:\n"
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
                prompt = self._prompt(date, day, prediction)
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

    def engine(self, ctx):
        google = ctx.providers.get("google_tts")
        if google.is_configured() and google.status()["status"] == star_providers.READY:
            return "google_tts"
        return "gtts"

    def prerequisites(self, ctx):
        if self.engine(ctx) == "google_tts":
            return []
        try:
            import gtts  # noqa: F401
        except ImportError:
            return ["neither a Google Cloud TTS service account nor the gTTS "
                    "fallback is available"]
        return []

    def _target(self, ctx, date, day):
        return ctx.project_path("output", date, "audio", "%s.mp3" % day)

    def plan(self, ctx):
        engine = self.engine(ctx)
        ctx.plan("voice-over engine: %s" % (
            "Google Cloud TTS (paid, per character)" if engine == "google_tts"
            else "gTTS free fallback (no credential, lower quality)"))
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
                target = self._target(ctx, date, day)
                atomic_replace(tmp, target)
                ctx.record("audio", target, {"date": date, "day": day, "engine": engine})
                rendered += 1
                ctx.log("audio rendered for %s/%s via %s" % (date, day, engine),
                        stage=self.name)
        return {"audio_files": rendered, "engine": engine}

    def _google(self, ctx, text, out_path):
        star_providers._require_network("google tts synthesis")
        from google.cloud import texttospeech
        path = ctx.providers.get("google_tts").credentials_path()
        client = texttospeech.TextToSpeechClient.from_service_account_file(path)
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code="th-TH",
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3))
        with open(out_path, "wb") as fh:
            fh.write(response.audio_content)

    def _gtts(self, text, out_path):
        star_providers._require_network("gtts synthesis")
        from gtts import gTTS
        gTTS(text=text, lang="th").save(out_path)


class VideoStage(StageAdapter):
    """Deterministic 1080x1920 MP4 built by ffmpeg. No shell, ever."""

    name = "video"
    label = "Video render"

    WIDTH, HEIGHT = 1080, 1920
    BACKGROUND = "#0B1220"
    ACCENT = "#E8C36B"

    def prerequisites(self, ctx):
        missing = []
        if shutil.which("ffmpeg") is None:
            missing.append("ffmpeg is not installed on the server")
        if find_thai_font() is None:
            missing.append("no Thai font found (install fonts-noto-core or fonts-thai-tlwg)")
        return missing

    def _target(self, ctx, date, day):
        return ctx.project_path("output", date, "video", "%s.mp4" % day)

    def build_command(self, audio_path, out_path, title, font):
        """The exact ffmpeg argv. Split out so tests can assert it without rendering."""
        drawtext = ":".join([
            "fontfile=%s" % font,
            "text=%s" % _ffmpeg_escape(title),
            "fontcolor=%s" % self.ACCENT,
            "fontsize=72",
            "x=(w-text_w)/2",
            "y=(h-text_h)/2",
            "line_spacing=18",
        ])
        return [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=30" % (
                self.BACKGROUND, self.WIDTH, self.HEIGHT),
            "-i", audio_path,
            "-vf", "drawtext=" + drawtext,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            out_path,
        ]

    def plan(self, ctx):
        font = find_thai_font()
        ctx.plan("render %dx%d MP4 with font %s" % (self.WIDTH, self.HEIGHT, font or "<missing>"))
        for date in ctx.dates:
            for day in ctx.days:
                audio = os.path.join(ctx.root, "output", date, "audio", "%s.mp3" % day)
                ctx.plan("render %s/%s" % (date, day),
                         command=self.build_command(audio, self._target(ctx, date, day),
                                                    "ดวงของชาววัน" + DAY_TH.get(day, day),
                                                    font or "<missing>"),
                         output=os.path.relpath(self._target(ctx, date, day), ctx.root))
        return ctx.planned

    def execute(self, ctx):
        font = find_thai_font()
        if font is None:
            raise StageBlocked("no Thai font found on the server")
        rendered = 0
        for date in ctx.dates:
            for day in ctx.days:
                ctx.check_cancelled()
                audio = os.path.join(ctx.root, "output", date, "audio", "%s.mp3" % day)
                if not os.path.isfile(audio):
                    raise StageBlocked(
                        "no audio for %s/%s — run the audio stage first" % (date, day))
                tmp = os.path.join(ctx.stage_dir(self.name), "%s_%s.mp4" % (date, day))
                argv = self.build_command(audio, tmp,
                                          "ดวงของชาววัน" + DAY_TH.get(day, day), font)
                code, lines = run_command(
                    argv, timeout=STAGE_TIMEOUT[self.name], cwd=ctx.root,
                    is_cancelled=ctx.cancelled)
                if code != 0 or not os.path.isfile(tmp):
                    raise StageFailed("ffmpeg failed for %s/%s (exit %s): %s"
                                      % (date, day, code, " | ".join(lines[-3:])))
                target = self._target(ctx, date, day)
                atomic_replace(tmp, target)
                ctx.record("video", target, {"date": date, "day": day})
                rendered += 1
                ctx.log("video rendered for %s/%s" % (date, day), stage=self.name)
        return {"videos": rendered}


def _ffmpeg_escape(text):
    """Escape for the drawtext filter's own mini-syntax (not a shell)."""
    for old, new in (("\\", "\\\\"), (":", "\\:"), ("'", "\\'"),
                     ("%", "\\%"), (",", "\\,"), ("[", "\\["), ("]", "\\]")):
        text = text.replace(old, new)
    return text


class PublishStage(StageAdapter):
    """Per-platform publishing, with a handoff package for manual platforms."""

    name = "publish"
    label = "Publish"

    def prerequisites(self, ctx):
        missing = []
        for platform in ctx.input.get("platforms") or []:
            provider = ctx.providers.get(platform)
            if provider.automation == star_providers.AUTOMATION_FULL:
                reason = provider.prerequisite_error()
                if reason:
                    missing.append("%s: %s" % (provider.label, reason))
        return missing

    def plan(self, ctx):
        for platform in ctx.input.get("platforms") or []:
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
        for platform in ctx.input.get("platforms") or []:
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
        target = now.date() + timedelta(days=config["date_offset_days"])
        job_input = star_jobs.validate_job_input({
            "from_date": target.isoformat(),
            "to_date": target.isoformat(),
            "days": config["days"],
            "stages": config["stages"],
            "platforms": config["platforms"] or None,
            "dry_run": config["dry_run"],
        })
        try:
            job = self.service.store.create_job(job_input, origin="schedule")
        except JobConflict:
            # A manual job is running; skip today rather than queueing behind it.
            self.service.store.release_schedule_run(run_date)
            return None
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

    def log_internal(self, message):
        import sys
        with self._log_lock:
            sys.stderr.write("star-automation: %s\n"
                             % star_redact.redact_text(message, limit=1000))

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
            },
            "state": {
                "permission_problems": self.state.audit(),
                "network_disabled": star_providers.network_disabled(),
            },
        }
