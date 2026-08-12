#!/usr/bin/env python3
"""Unit tests for the video stage's customisation surface.

Everything the operator can change about a clip — the overlay text and the
uploaded background image — arrives as job input and ends up inside an ffmpeg
argv. These tests own the path between those two points:

* the automatic caption is the day's own script and nothing else, so a clip
  never claims something the script did not say;
* the custom caption is used byte for byte and never goes near a shell or a
  drawtext filter argument;
* the overlay file is staged inside the job workspace under the state
  directory, owner-only, and nothing is written into the project tree;
* a dry run describes all of that without writing a file, spawning a process
  or printing a server path an operator could use to locate an upload.

Everything runs against a temp project root and a temp state directory with
STAR_DISABLE_NETWORK=1, and no production path, credential or provider is
touched. The only tests that spawn a process are the ones that render a real
clip, and they skip when ffmpeg or a Thai font is unavailable.
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_assets  # noqa: E402
import star_automation  # noqa: E402
import star_jobs  # noqa: E402

DATE = "2026-08-11"
DAY = "mon"

# The dated caption heading every generated script opens with. Built the same
# way the contract builds it, so this is the real thing and not a lookalike.
THAI_HEADING = "ดวงของชาววันจันทร์ ประจำวันที่ 11 สิงหาคม พ.ศ. 2569"

# A script shaped like the ones the script stage writes: a heading, a blank
# line, hashtags, a stray short line, then the actual prediction.
THAI_HOOK = "วันนี้การงานของชาววันจันทร์โดดเด่นมาก ผู้ใหญ่ให้การสนับสนุนเต็มที่"
THAI_SCRIPT = "\n".join([
    "",
    THAI_HEADING,
    "",
    "#ดูดวง #ดวงรายวัน",
    "สั้น",
    THAI_HOOK,
    "ส่วนการเงินให้ระวังรายจ่ายที่ไม่ได้วางแผนไว้",
    "",
])

# Captions this module must never produce on its own. If one of these ever
# shows up in an overlay or a plan, something invented a caption.
GENERIC_CAPTIONS = ("ดวงของชาววันจันทร์", "ดวงรายวัน", "horoscope", "Horoscope",
                    "ดวงวันนี้", "<caption>", "TODO")

# A JPEG magic prefix with enough filler to clear MIN_UPLOAD_BYTES. These bytes
# are never decoded: the tests that use them mock the probe out, because what
# star_assets does with real bytes is tests/test_automation_assets.py's job.
JPEG_UPLOAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200


class TempEnv(unittest.TestCase):
    """Temp root + temp state dir + network hard-disabled.

    Copied from tests/test_automation_core.py rather than imported: these
    tests must keep working if that file is reorganised, and a copy of six
    lines is cheaper than a dependency between test modules.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="star-video-root-")
        self.state_dir = tempfile.mkdtemp(prefix="star-video-state-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self._old_env = {}
        for key, value in (("STAR_DISABLE_NETWORK", "1"),
                           ("STAR_STATE_DIR", self.state_dir)):
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def service(self, start_threads=False):
        svc = star_automation.AutomationService(
            self.root, state_dir=self.state_dir, start_threads=start_threads)
        self.addCleanup(svc.close)
        return svc


class VideoStageEnv(TempEnv):
    """A real VideoStage and a real JobContext over the temp tree."""

    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self.stage = star_automation.VideoStage()

    # -- context --------------------------------------------------------
    def context(self, **overrides):
        payload = {"from_date": DATE, "days": [DAY], "stages": ["video"],
                   "dry_run": True}
        payload.update(overrides)
        job = self.svc.store.create_job(star_jobs.validate_job_input(payload))
        return star_automation.JobContext(
            self.svc, job, self.svc.state.job_dir(job["id"]))

    # -- fixtures -------------------------------------------------------
    def write_script(self, text, date=DATE, day=DAY, override=False):
        """Put a script where the stage looks for it. Temp root only."""
        rel = (("content", "overrides", date, "%s.txt" % day) if override else
               ("content", "scripts", "claude_%s_%s.txt" % (date, day)))
        path = os.path.join(self.root, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def store_asset(self, dimensions=(1080, 1920)):
        """A stored background, with the image subprocesses mocked out."""
        with mock.patch.object(star_assets, "probe_image",
                               return_value=dimensions), \
             mock.patch.object(star_assets, "verify_decodes", return_value=None):
            return star_assets.store_background(self.svc.state, JPEG_UPLOAD)

    # -- assertions -----------------------------------------------------
    def tree(self, path):
        found = []
        for base, dirs, files in os.walk(path):
            for name in dirs + files:
                found.append(os.path.relpath(os.path.join(base, name), path))
        return sorted(found)

    def assertProjectTreeUnchanged(self, before):
        self.assertEqual(self.tree(self.root), before,
                         "the project tree was written to")


# ── the automatic caption comes out of the day's own script ───────────

class TestAutoOverlayText(unittest.TestCase):
    """auto_overlay_text is the whole automatic-caption policy, so it is
    tested as policy: what it skips, what it picks, where it cuts, and what
    it refuses to invent."""

    def test_hook_is_the_scripts_own_first_meaningful_line(self):
        text = star_automation.auto_overlay_text(THAI_SCRIPT)
        self.assertEqual(text, THAI_HOOK)
        # Taken from the content, not from anywhere else.
        self.assertIn(text, THAI_SCRIPT)

    def test_hook_is_not_a_hard_coded_generic_title(self):
        """The point of the feature: the caption is this clip's own line.

        A second script must produce a second caption, and neither may be one
        of the generic strings a fallback would reach for.
        """
        other_hook = "ชาววันศุกร์ระวังเรื่องสุขภาพ พักผ่อนให้เพียงพอในสัปดาห์นี้"
        other = "\n".join([THAI_HEADING, "#ดูดวง", other_hook])

        first = star_automation.auto_overlay_text(THAI_SCRIPT)
        second = star_automation.auto_overlay_text(other)
        self.assertEqual(second, other_hook)
        self.assertNotEqual(first, second)
        for caption in (first, second):
            self.assertNotIn(caption, GENERIC_CAPTIONS)
            self.assertFalse(caption.startswith("ดวงของชาววัน"), caption)

    def test_blank_lines_are_skipped(self):
        hook = "ดวงการเงินของคุณกำลังมาแรงในช่วงนี้"
        self.assertEqual(
            star_automation.auto_overlay_text("\n\n   \n\t\n" + hook), hook)

    def test_the_dated_heading_is_skipped_not_captioned(self):
        """The heading says which day and which date; the viewer can already
        see both, so it is a title rather than a hook."""
        self.assertTrue(star_automation.is_boilerplate_heading(THAI_HEADING))
        hook = "ผู้ใหญ่ให้การสนับสนุนงานที่ค้างอยู่จนสำเร็จ"
        self.assertEqual(
            star_automation.auto_overlay_text(THAI_HEADING + "\n" + hook), hook)

    def test_a_hashtag_only_line_is_skipped(self):
        hook = "ความรักมีเกณฑ์สมหวังในช่วงกลางสัปดาห์"
        script = "#ดูดวง #ดวงรายวัน #โหราศาสตร์\n" + hook
        self.assertEqual(star_automation.auto_overlay_text(script), hook)

    def test_hashtags_are_stripped_from_a_line_that_also_has_words(self):
        script = "#ดวง วันนี้การงานรุ่งโรจน์ #โหราศาสตร์"
        text = star_automation.auto_overlay_text(script)
        self.assertEqual(text, "วันนี้การงานรุ่งโรจน์")
        self.assertNotIn("#", text)

    def test_control_characters_never_reach_the_caption(self):
        text = star_automation.auto_overlay_text("วันนี้\x00ดวงดีมากๆ\x1b นะ")
        self.assertEqual(text, "วันนี้ ดวงดีมากๆ นะ")
        for bad in ("\x00", "\x1b", "\x7f"):
            self.assertNotIn(bad, text)

    def test_a_line_shorter_than_the_minimum_is_not_a_hook(self):
        limit = star_automation.OVERLAY_MIN_CHARS
        short = "ก" * (limit - 1)
        exact = "การงานดี"
        self.assertEqual(len(exact), limit)
        self.assertIsNone(star_automation.auto_overlay_text(short))
        self.assertEqual(star_automation.auto_overlay_text(exact), exact)
        self.assertEqual(
            star_automation.auto_overlay_text(short + "\n" + exact), exact)

    def test_a_run_on_line_is_cut_at_its_first_sentence(self):
        script = "การเงินมาแรงมากในวันนี้. ส่วนความรักต้องระวังคำพูด"
        self.assertEqual(star_automation.auto_overlay_text(script),
                         "การเงินมาแรงมากในวันนี้")

    def test_a_terminator_inside_the_minimum_does_not_cut_the_line(self):
        """"สวัสดี!" is a greeting, not a sentence: cutting there would leave
        a caption with no content in it."""
        script = "สวัสดี! วันนี้ดวงการงานของคุณโดดเด่นมาก. ที่เหลือค่อยว่ากัน"
        self.assertEqual(star_automation.auto_overlay_text(script),
                         "สวัสดี! วันนี้ดวงการงานของคุณโดดเด่นมาก")

    def test_a_long_line_is_truncated_deterministically(self):
        line = "การเงิน" * 40
        text = star_automation.auto_overlay_text(line)
        self.assertEqual(len(text), star_automation.OVERLAY_MAX_CHARS + 1)
        self.assertTrue(text.endswith("…"))
        self.assertTrue(line.startswith(text[:-1]),
                        "the kept part must be a prefix of the script")
        self.assertEqual(text, star_automation.auto_overlay_text(line),
                         "the same script must always give the same caption")

    def test_truncation_prefers_a_word_boundary(self):
        line = " ".join(["ดวงชะตา", "การงาน", "การเงิน", "ความรัก", "สุขภาพ",
                         "โชคลาภ", "ผู้ใหญ่", "สนับสนุน", "เต็มที่", "ก้าวหน้า",
                         "มั่นคง", "ยั่งยืน"])
        self.assertGreater(len(line), star_automation.OVERLAY_MAX_CHARS)
        text = star_automation.auto_overlay_text(line)
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text), star_automation.OVERLAY_MAX_CHARS + 1)
        self.assertFalse(text[:-1].endswith(" "), "a dangling space was kept")
        self.assertTrue(line.startswith(text[:-1]))
        # The cut fell between words, so no word is left half-drawn.
        self.assertIn(text[:-1].split()[-1], line.split())

    def test_nothing_meaningful_gives_none_and_never_a_placeholder(self):
        for empty in (None, "", "   ", "\n\n\t\n", "#ดูดวง #ดวงรายวัน",
                      "สั้น\nสั้น\n", THAI_HEADING,
                      THAI_HEADING + "\n#ดูดวง\n\n"):
            self.assertIsNone(star_automation.auto_overlay_text(empty),
                              repr(empty))


# ── layout and wrapping ───────────────────────────────────────────────

class TestOverlayLayoutAndWrap(unittest.TestCase):
    def test_size_steps_are_fixed_and_repeatable(self):
        cases = ((1, 84), (40, 84), (41, 66), (90, 66), (91, 54), (150, 54),
                 (151, star_automation.OVERLAY_MIN_SIZE),
                 (star_jobs.MAX_CUSTOM_OVERLAY_TEXT,
                  star_automation.OVERLAY_MIN_SIZE))
        for length, expected in cases:
            text = "ก" * length
            size, width = star_automation.overlay_layout(text)
            self.assertEqual(size, expected, length)
            self.assertEqual((size, width),
                             star_automation.overlay_layout(text),
                             "layout must be a pure function of the text")

    def test_wrap_width_follows_the_safe_width_and_never_collapses(self):
        for length in (10, 60, 120, 220):
            size, width = star_automation.overlay_layout("ก" * length)
            self.assertEqual(width, max(12, int(star_automation.OVERLAY_SAFE_WIDTH
                                                / (star_automation.OVERLAY_GLYPH_RATIO
                                                   * size))))
            self.assertGreaterEqual(width, 12)

    def test_lines_are_bounded_and_thai_survives_the_wrap(self):
        text = " ".join(["ดวงชะตา", "การงาน", "การเงิน", "ความรัก", "สุขภาพ",
                         "โชคลาภ", "ผู้ใหญ่", "สนับสนุน", "เต็มที่"])
        size, width = star_automation.overlay_layout(text)
        wrapped = star_automation.wrap_overlay_text(text, width)
        lines = wrapped.split("\n")
        self.assertTrue(lines)
        for line in lines:
            self.assertLessEqual(len(line), width, line)
        self.assertLessEqual(len(lines), star_automation.OVERLAY_MAX_LINES)
        # Same characters, in the same order: only the spacing changed.
        self.assertEqual("".join(wrapped.split()), "".join(text.split()))
        self.assertNotIn("…", wrapped)
        self.assertEqual(wrapped, star_automation.wrap_overlay_text(text, width))

    def test_a_word_longer_than_a_line_is_hard_split_not_dropped(self):
        word = "ก" * 50
        wrapped = star_automation.wrap_overlay_text(word, 10)
        self.assertEqual("".join(wrapped.split("\n")), word)
        for line in wrapped.split("\n"):
            self.assertLessEqual(len(line), 10)

    def test_the_longest_accepted_custom_text_is_never_silently_cut(self):
        """MAX_LINES is a cap, not a working limit: the longest text
        validation accepts must wrap well inside it."""
        text = "ก" * star_jobs.MAX_CUSTOM_OVERLAY_TEXT
        size, width = star_automation.overlay_layout(text)
        wrapped = star_automation.wrap_overlay_text(text, width)
        self.assertLess(len(wrapped.split("\n")),
                        star_automation.OVERLAY_MAX_LINES)
        self.assertNotIn("…", wrapped)
        self.assertEqual("".join(wrapped.split("\n")), text)

    def test_beyond_the_line_cap_the_text_is_cut_with_an_ellipsis(self):
        wrapped = star_automation.wrap_overlay_text("ก" * 400, 10, max_lines=3)
        lines = wrapped.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("…"))


# ── which file the automatic caption is read from ─────────────────────

class TestScriptTextSource(VideoStageEnv):
    """content/overrides/{date}/{day}.txt wins over the generated script."""

    def test_hand_edited_override_beats_the_generated_script(self):
        self.write_script("สคริปต์ที่เครื่องสร้าง")
        self.write_script("สคริปต์ที่คนแก้เอง", override=True)
        ctx = self.context()
        self.assertEqual(self.stage.script_text(ctx, DATE, DAY),
                         "สคริปต์ที่คนแก้เอง")

    def test_the_generated_script_is_used_when_there_is_no_override(self):
        self.write_script(THAI_SCRIPT)
        ctx = self.context()
        self.assertEqual(self.stage.script_text(ctx, DATE, DAY), THAI_SCRIPT)

    def test_no_script_at_all_is_none(self):
        self.assertIsNone(self.stage.script_text(self.context(), DATE, DAY))

    def test_an_empty_override_wins_and_stays_empty(self):
        """A hand-edited file that has been emptied is an answer, not a miss:
        the override still wins, the caption comes out empty and the stage
        blocks rather than quietly falling back to the generated script."""
        self.write_script(THAI_SCRIPT)
        self.write_script("", override=True)
        ctx = self.context()
        self.assertEqual(self.stage.script_text(ctx, DATE, DAY), "")
        self.assertIsNone(star_automation.auto_overlay_text(""))
        with self.assertRaises(star_automation.StageBlocked):
            self.stage.overlay_text(ctx, DATE, DAY)

    def test_a_whitespace_only_script_yields_no_caption(self):
        self.write_script("\n\n   \n")
        ctx = self.context()
        self.assertEqual(self.stage.script_text(ctx, DATE, DAY), "\n\n   \n")
        with self.assertRaises(star_automation.StageBlocked):
            self.stage.overlay_text(ctx, DATE, DAY)

    def test_another_days_script_is_not_borrowed(self):
        self.write_script(THAI_SCRIPT, day="fri")
        self.write_script(THAI_SCRIPT, date="2026-08-12")
        ctx = self.context()
        self.assertIsNone(self.stage.script_text(ctx, DATE, DAY))

    def test_only_files_under_the_project_root_are_opened(self):
        """The stage builds its own paths from the job's date and day; nothing
        it reads may sit outside the root it was given."""
        self.write_script(THAI_SCRIPT)
        ctx = self.context()
        opened = []
        real_open = open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=recording_open):
            self.stage.script_text(ctx, DATE, DAY)
        self.assertTrue(opened)
        for path in opened:
            self.assertTrue(os.path.abspath(path).startswith(self.root + os.sep),
                            path)


# ── auto vs custom ────────────────────────────────────────────────────

class TestOverlayModeResolution(VideoStageEnv):
    def test_default_and_unknown_modes_fall_back_to_auto(self):
        ctx = self.context()
        self.assertEqual(self.stage.overlay_mode(ctx), "auto")
        for bad in (None, "", "AUTO", "manual", 42, ["custom"]):
            ctx.input["overlay_text_mode"] = bad
            self.assertEqual(self.stage.overlay_mode(ctx),
                             star_jobs.DEFAULT_OVERLAY_TEXT_MODE, repr(bad))

    def test_auto_reads_the_script_of_each_date_and_day(self):
        dates = ["2026-08-11", "2026-08-12"]
        days = ["mon", "fri"]
        expected = {}
        for date in dates:
            for day in days:
                hook = "ดวงของ %s วัน %s การงานเด่นชัดมาก" % (date, day)
                self.write_script("\n".join([THAI_HEADING, "#ดูดวง", hook]),
                                  date=date, day=day)
                expected[(date, day)] = hook
        ctx = self.context(from_date=dates[0], to_date=dates[1], days=days)

        asked = []
        real = self.stage.script_text

        def spy(context, date, day):
            asked.append((date, day))
            return real(context, date, day)

        with mock.patch.object(self.stage, "script_text", side_effect=spy):
            produced = {(date, day): self.stage.overlay_text(ctx, date, day)
                        for date in ctx.dates for day in ctx.days}
        self.assertEqual(produced, expected)
        self.assertEqual(sorted(asked), sorted(expected))

    def test_custom_uses_the_exact_text_on_every_clip_without_reading_a_script(self):
        text = "ดวงรายสัปดาห์ของชาวราศีสิงห์"
        self.write_script(THAI_SCRIPT)  # present, and must stay unread
        ctx = self.context(from_date="2026-08-11", to_date="2026-08-12",
                           days=["mon", "fri"], overlay_text_mode="custom",
                           custom_overlay_text="  %s  " % text)
        self.assertEqual(ctx.input["custom_overlay_text"], text,
                         "validation stores the trimmed text")

        with mock.patch.object(
                self.stage, "script_text",
                side_effect=AssertionError("custom mode must not read a script")):
            for date in ctx.dates:
                for day in ctx.days:
                    self.assertEqual(self.stage.overlay_text(ctx, date, day), text)

    def test_custom_text_is_trimmed_by_the_stage_as_well(self):
        """Validation trims, and so does the stage: a job that reached the
        database another way still draws the same glyphs."""
        ctx = self.context(overlay_text_mode="custom",
                           custom_overlay_text="ข้อความ")
        ctx.input["custom_overlay_text"] = "  \n ข้อความกลางจอ \t "
        self.assertEqual(self.stage.overlay_text(ctx, DATE, DAY), "ข้อความกลางจอ")

    def test_custom_mode_without_a_text_blocks(self):
        ctx = self.context(overlay_text_mode="custom",
                           custom_overlay_text="ข้อความ")
        for blank in (None, "", "   ", "\n\t"):
            ctx.input["custom_overlay_text"] = blank
            with self.assertRaises(star_automation.StageBlocked):
                self.stage.overlay_text(ctx, DATE, DAY)

    def test_auto_without_a_script_blocks_and_names_the_clip(self):
        ctx = self.context()
        with self.assertRaises(star_automation.StageBlocked) as caught:
            self.stage.overlay_text(ctx, DATE, DAY)
        message = str(caught.exception)
        self.assertIn(DATE, message)
        self.assertIn(DAY, message)
        for caption in GENERIC_CAPTIONS:
            self.assertNotIn(caption, message,
                             "the block message offered a caption instead")

    def test_auto_blocks_when_only_boilerplate_is_available(self):
        self.write_script(THAI_HEADING + "\n#ดูดวง #ดวงรายวัน\n")
        with self.assertRaises(star_automation.StageBlocked):
            self.stage.overlay_text(self.context(), DATE, DAY)


# ── where the drawn text is staged ────────────────────────────────────

class TestOverlayFile(VideoStageEnv):
    def test_the_overlay_file_lives_in_the_job_workspace(self):
        ctx = self.context()
        path = self.stage.overlay_file(ctx, DATE, DAY)
        self.assertTrue(path.startswith(ctx.workdir + os.sep), path)
        self.assertTrue(path.startswith(self.state_dir + os.sep), path)
        self.assertFalse(path.startswith(self.root + os.sep), path)
        self.assertEqual(os.path.dirname(path),
                         os.path.join(ctx.workdir, self.stage.name))

    def test_the_overlay_file_name_is_built_from_validated_input_only(self):
        """The only caller-supplied parts of the name are the date and the
        birth-day, and neither can be anything but its own canonical form."""
        ctx = self.context()
        name = os.path.basename(self.stage.overlay_file(ctx, DATE, DAY))
        self.assertRegex(name, r"^overlay_\d{4}-\d{2}-\d{2}_[a-z]{3}\.txt$")
        for hostile in ("../../etc/passwd", "2026-08-11/../..", "2026-08-11\x00"):
            with self.assertRaises(star_jobs.JobValidationError):
                star_jobs.validate_job_input({"from_date": hostile})
            with self.assertRaises(star_jobs.JobValidationError):
                star_jobs.validate_job_input({"from_date": DATE,
                                              "days": [hostile]})

    def test_writing_the_overlay_is_owner_only_and_touches_no_project_file(self):
        ctx = self.context()
        before = self.tree(self.root)
        text = "ดวงการงานของคุณกำลังไปได้สวยในสัปดาห์นี้"
        size, width = star_automation.overlay_layout(text)
        path = self.stage._write_overlay(ctx, DATE, DAY, text, width)

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        # The stage directory itself is created by JobContext.stage_dir with a
        # plain makedirs, so it carries the process umask (0775 here) rather
        # than 0700. Nothing is reachable through it: the job workspace above
        # it is 0700, which is what actually keeps the overlay unreadable.
        self.assertEqual(stat.S_IMODE(os.stat(ctx.workdir).st_mode), 0o700)
        self.assertTrue(path.startswith(ctx.workdir + os.sep), path)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(),
                             star_automation.wrap_overlay_text(text, width))
        self.assertProjectTreeUnchanged(before)

    def test_the_staged_bytes_are_the_wrapped_thai_text(self):
        ctx = self.context()
        text = "โชคลาภ วิ่งเข้าหา อย่างต่อเนื่อง ตลอดทั้งสัปดาห์นี้"
        size, width = star_automation.overlay_layout(text)
        path = self.stage._write_overlay(ctx, DATE, DAY, text, width)
        with open(path, "rb") as fh:
            staged = fh.read().decode("utf-8")
        self.assertEqual("".join(staged.split()), "".join(text.split()))
        for line in staged.split("\n"):
            self.assertLessEqual(len(line), width)


# ── the ffmpeg argv ───────────────────────────────────────────────────

class TestBuildCommand(unittest.TestCase):
    """build_command is pure, so it is asserted without rendering anything."""

    def setUp(self):
        self.stage = star_automation.VideoStage()
        self.font = "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf"

    def build(self, **kwargs):
        params = {"audio_path": "/srv/audio/mon.mp3", "out_path": "/srv/out/mon.mp4",
                  "font": self.font, "textfile": "/var/lib/star/jobs/j/video/o.txt"}
        params.update(kwargs)
        return self.stage.build_command(**params)

    def filter_of(self, argv):
        return argv[argv.index("-vf") + 1]

    # -- shape ----------------------------------------------------------
    def test_it_is_an_argv_list_of_strings_and_never_a_shell_line(self):
        argv = self.build()
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "ffmpeg")
        for item in argv:
            self.assertIsInstance(item, str)
        for shellish in ("sh", "bash", "-c", "&&", ";", "|"):
            self.assertNotIn(shellish, argv)
        self.assertIn("-nostdin", argv)

    def test_paths_stay_single_arguments_even_with_spaces(self):
        audio = "/srv/my audio/mon.mp3"
        out = "/srv/my out/mon.mp4"
        argv = self.build(audio_path=audio, out_path=out)
        self.assertIn(audio, argv)
        self.assertEqual(argv[-1], out)

    # -- background -----------------------------------------------------
    def test_without_a_background_the_source_is_the_lavfi_colour(self):
        argv = self.build()
        self.assertIn("-f", argv)
        self.assertIn("lavfi", argv)
        self.assertIn("color=c=%s:s=%dx%d:r=30"
                      % (self.stage.BACKGROUND, self.stage.WIDTH,
                         self.stage.HEIGHT), argv)
        self.assertNotIn("-loop", argv)
        video_filter = self.filter_of(argv)
        self.assertTrue(video_filter.startswith("drawtext="))
        for absent in ("scale=", "crop=", "drawbox="):
            self.assertNotIn(absent, video_filter)

    def test_a_background_is_looped_from_the_server_path(self):
        background = "/var/lib/star/assets/backgrounds/%s.jpg" % ("a" * 32)
        argv = self.build(background=background)
        self.assertIn("-loop", argv)
        loop = argv.index("-loop")
        self.assertEqual(argv[loop:loop + 6],
                         ["-loop", "1", "-framerate", "30", "-i", background])
        self.assertNotIn("lavfi", argv)

    def test_the_background_filter_covers_crops_and_darkens(self):
        argv = self.build(background="/var/lib/star/assets/backgrounds/x.jpg")
        video_filter = self.filter_of(argv)
        self.assertIn("scale=%d:%d:force_original_aspect_ratio=increase"
                      % (self.stage.WIDTH, self.stage.HEIGHT), video_filter)
        self.assertIn("crop=%d:%d" % (self.stage.WIDTH, self.stage.HEIGHT),
                      video_filter)
        self.assertIn("setsar=1", video_filter)
        self.assertIn("drawbox=", video_filter)
        self.assertIn("black@%s" % self.stage.SCRIM, video_filter)
        # Order matters: cover, crop, darken, then draw the text on top.
        self.assertLess(video_filter.index("scale="), video_filter.index("crop="))
        self.assertLess(video_filter.index("crop="), video_filter.index("drawbox="))
        self.assertLess(video_filter.index("drawbox="), video_filter.index("drawtext="))

    # -- encoding -------------------------------------------------------
    def test_the_output_is_h264_yuv420p_at_1080x1920(self):
        for background in (None, "/var/lib/star/assets/backgrounds/x.jpg"):
            argv = self.build(background=background)
            self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
            self.assertEqual(argv[argv.index("-pix_fmt") + 1], "yuv420p")
            self.assertEqual(argv[argv.index("-preset") + 1], "veryfast")
            self.assertIn("+faststart", argv)
            size = ("crop=%d:%d" % (self.stage.WIDTH, self.stage.HEIGHT)
                    if background else
                    "%dx%d" % (self.stage.WIDTH, self.stage.HEIGHT))
            self.assertIn(size, " ".join(argv), repr(background))

    def test_audio_is_mapped_encoded_and_the_clip_stops_with_it(self):
        argv = self.build()
        # The audio is the second input, and each stream is mapped explicitly
        # so a background image cannot end up supplying the audio stream.
        self.assertEqual(argv[argv.index("-map") + 1], "0:v:0")
        self.assertIn("1:a:0", argv)
        self.assertEqual(argv[argv.index("/srv/audio/mon.mp3") - 1], "-i")
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
        self.assertEqual(argv[argv.index("-b:a") + 1], "128k")
        self.assertIn("-shortest", argv)
        self.assertEqual(argv[-1], "/srv/out/mon.mp4")

    # -- how the caption gets in ----------------------------------------
    def test_the_caption_is_passed_as_a_file_not_as_filter_text(self):
        argv = self.build(textfile="/var/lib/star/jobs/j/video/overlay.txt")
        video_filter = self.filter_of(argv)
        self.assertIn("textfile=", video_filter)
        self.assertNotIn(":text=", video_filter)

    def test_a_title_and_a_textfile_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            self.stage.build_command("/a.mp3", "/a.mp4", title="ชื่อ",
                                     textfile="/t.txt")
        with self.assertRaises(ValueError):
            self.stage.build_command("/a.mp3", "/a.mp4")

    def test_hostile_custom_text_never_appears_in_the_argv(self):
        """The operator's line is drawn, not interpreted. It reaches ffmpeg as
        the bytes of a file, so nothing in it can become an argument, a filter
        option or a shell token — there is nothing to escape because the text
        is not on the command line at all.
        """
        hostile = ("'; rm -rf / #\":fontcolor=red:text=owned\\,x=0"
                   "$(whoami) `id` ${HOME} | tee /tmp/pwned")
        textfile = "/var/lib/star/jobs/j/video/overlay.txt"
        argv = self.build(textfile=textfile)
        joined = " ".join(argv)
        for fragment in ("rm -rf", "fontcolor=red", "owned", "$(whoami)",
                         "`id`", "${HOME}", "tee /tmp/pwned", hostile):
            self.assertNotIn(fragment, joined, fragment)
        self.assertIn("textfile=", joined)

    def test_the_hostile_text_is_what_lands_in_the_staged_file(self):
        """The other half of the guarantee: the operator still gets exactly
        the characters they typed, drawn as glyphs."""
        hostile = "ราคา 1:2 'พิเศษ' [ลด] 50%, ดูดวง\\ฟรี"
        tmp = tempfile.mkdtemp(prefix="star-video-overlay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "overlay.txt")
        size, width = star_automation.overlay_layout(hostile)
        with open(path, "wb") as fh:
            fh.write(star_automation.wrap_overlay_text(hostile, width).encode("utf-8"))
        with open(path, encoding="utf-8") as fh:
            staged = fh.read()
        self.assertEqual("".join(staged.split()), "".join(hostile.split()))
        self.assertNotIn(hostile, " ".join(self.build(textfile=path)))

    def test_a_literal_title_is_still_escaped_for_the_filter(self):
        """The legacy title form is not how job text travels, but when a
        caller uses it the drawtext mini-syntax must not be reachable."""
        argv = self.stage.build_command("/a.mp3", "/a.mp4", title="a:b'c,d[e]",
                                        font=self.font, textfile=None)
        video_filter = self.filter_of(argv)
        for ch in (":", "'", ",", "[", "]"):
            self.assertIn("\\" + ch, video_filter)


# ── the dry run ───────────────────────────────────────────────────────

class TestDryRunPlan(VideoStageEnv):
    """plan() is a description, not a rehearsal: nothing runs and nothing is
    written, and what it prints must be safe to show in a browser."""

    def setUp(self):
        super().setUp()
        for target, attr in ((star_automation, "run_command"),
                             (star_assets, "_run")):
            guard = mock.patch.object(
                target, attr,
                side_effect=AssertionError("a dry run must not spawn a process"))
            guard.start()
            self.addCleanup(guard.stop)
        execute = mock.patch.object(
            self.stage, "execute",
            side_effect=AssertionError("a dry run must not call execute()"))
        execute.start()
        self.addCleanup(execute.stop)

    def commands(self, planned):
        return [entry["command"] for entry in planned if entry.get("command")]

    def text_of(self, planned):
        return "\n".join(entry.get("description", "") + " "
                         + entry.get("command", "") + " "
                         + entry.get("output", "") for entry in planned)

    # -- side effects ---------------------------------------------------
    def test_the_plan_writes_nothing_anywhere(self):
        self.write_script(THAI_SCRIPT)
        ctx = self.context()
        project_before = self.tree(self.root)
        workdir_before = self.tree(ctx.workdir)

        planned = self.stage.plan(ctx)

        self.assertTrue(planned)
        self.assertProjectTreeUnchanged(project_before)
        self.assertEqual(self.tree(ctx.workdir), workdir_before,
                         "the plan created something in the job workspace")
        self.assertFalse(os.path.exists(self.stage.overlay_file(ctx, DATE, DAY)))
        self.assertEqual(ctx.artifacts, [], "a dry run must record no artifact")

    def test_checking_a_missing_background_creates_no_asset_directory(self):
        ctx = self.context(background_asset_id="a" * 32)
        planned = self.stage.plan(ctx)
        self.assertFalse(os.path.isdir(star_assets.backgrounds_dir(self.svc.state)))
        self.assertIn("no longer stored on the server", self.text_of(planned))

    # -- what it says ---------------------------------------------------
    def test_a_missing_script_still_gets_a_representative_command(self):
        ctx = self.context()
        planned = self.stage.plan(ctx)
        commands = self.commands(planned)
        self.assertEqual(len(commands), 1)
        argv = commands[0]
        self.assertIn("ffmpeg", argv)
        self.assertIn("libx264", argv)
        self.assertIn("1080x1920", argv)
        self.assertIn("would block", self.text_of(planned))

    def test_a_missing_script_is_never_papered_over_with_a_caption(self):
        ctx = self.context()
        text = self.text_of(self.stage.plan(ctx))
        self.assertNotIn(":text=", text, "a literal caption reached the plan")
        for caption in GENERIC_CAPTIONS:
            self.assertNotIn(caption, text, caption)
        self.assertIn(star_automation.PLAN_TEXTFILE % (DATE, DAY), text)

    def test_the_auto_caption_that_does_exist_is_shown(self):
        self.write_script(THAI_SCRIPT)
        text = self.text_of(self.stage.plan(self.context()))
        self.assertIn(THAI_HOOK, text)
        self.assertNotIn("would block", text)

    def test_the_custom_caption_is_described_exactly_and_stays_bounded(self):
        """Showing the operator their own line back is safe: validation has
        already bounded it and stripped every control character, and the
        description quotes it rather than pasting it into a command."""
        custom = "ดวงรายสัปดาห์ ราคา 1:2 'พิเศษ' 50%"
        ctx = self.context(overlay_text_mode="custom",
                           custom_overlay_text=custom)
        with mock.patch.object(
                self.stage, "script_text",
                side_effect=AssertionError("custom mode must not read a script")):
            planned = self.stage.plan(ctx)
        described = [entry["description"] for entry in planned
                     if custom in entry["description"]]
        self.assertTrue(described, "the custom line was not described")
        for description in described:
            self.assertLess(len(description),
                            star_jobs.MAX_CUSTOM_OVERLAY_TEXT + 200)
        # Described, never pasted into the argv.
        for command in self.commands(planned):
            self.assertNotIn(custom, command)

    def test_every_clip_in_the_job_is_planned(self):
        ctx = self.context(from_date="2026-08-11", to_date="2026-08-12",
                           days=["mon", "fri"])
        planned = self.stage.plan(ctx)
        self.assertEqual(len(self.commands(planned)), 4)
        outputs = [entry["output"] for entry in planned if entry.get("output")]
        self.assertEqual(sorted(outputs), sorted(
            "output/%s/video/%s.mp4" % (date, day)
            for date in ctx.dates for day in ctx.days))

    # -- what it must not say -------------------------------------------
    def test_the_plan_never_leaks_a_state_or_asset_path(self):
        """The plan is rendered in the browser. Neither the job workspace nor
        the directory an upload is stored in may be locatable from it."""
        meta = self.store_asset()
        self.write_script(THAI_SCRIPT)
        ctx = self.context(background_asset_id=meta["id"])
        planned = self.stage.plan(ctx)
        text = self.text_of(planned)

        self.assertNotIn(self.state_dir, text)
        self.assertNotIn(star_assets.backgrounds_dir(self.svc.state), text)
        self.assertNotIn(star_assets.background_path(self.svc.state, meta["id"]),
                         text)
        self.assertNotIn(ctx.workdir, text)
        # The symbolic stand-ins are there instead, so the argv is still whole.
        self.assertIn(star_automation.PLAN_BACKGROUND, text)
        self.assertIn(star_automation.PLAN_TEXTFILE % (DATE, DAY), text)
        self.assertIn("<job-workspace>", text)

    def test_the_plan_never_leaks_an_absolute_project_path(self):
        """FAILING — the audio input and the .mp4 output are joined against the
        project root, so every planned command prints an absolute server path
        (in production, /var/www/star/output/<date>/video/<day>.mp4). The
        `output` field of the same entry is already a project-relative path,
        and the overlay file and the uploaded image are already symbolic, so
        the argv is the only place a real server path still appears.
        """
        self.write_script(THAI_SCRIPT)
        ctx = self.context()
        text = self.text_of(self.stage.plan(ctx))
        self.assertNotIn(self.root, text)

    def test_the_plan_is_stable_across_runs(self):
        """Two dry runs of the same job describe the same render: the caption,
        the type size and the argv are all functions of the input."""
        self.write_script(THAI_SCRIPT)
        ctx = self.context()
        first = self.stage.plan(ctx)
        second = star_automation.VideoStage().plan(
            star_automation.JobContext(self.svc, ctx.job, ctx.workdir))
        self.assertEqual(first, second)


# ── one real render ───────────────────────────────────────────────────

@unittest.skipUnless(shutil.which("ffmpeg") and star_automation.find_thai_font(),
                     "ffmpeg or a Thai font is unavailable")
class TestRealRender(VideoStageEnv):
    """One second of real video, built by the real ffmpeg.

    The customised path is the one worth proving end to end: an uploaded
    background resolved from its asset id, a Thai caption staged as a file,
    and a clip that comes out 1080x1920. Everything is generated here; no
    fixture, no network and no production file is involved.
    """

    def ffmpeg(self, argv, timeout=120):
        code, lines = star_automation.run_command(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            + argv, timeout=timeout)
        self.assertEqual(code, 0, " | ".join(lines[-3:]))

    def make_audio(self):
        path = os.path.join(self.root, "silence.mp3")
        self.ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
                     "-c:a", "libmp3lame", path])
        return path

    def make_image(self):
        path = os.path.join(self.root, "background.png")
        self.ffmpeg(["-f", "lavfi", "-i", "color=c=#204060:s=640x640",
                     "-frames:v", "1", path])
        return path

    def test_a_custom_caption_over_an_uploaded_image_renders_1080x1920(self):
        audio = self.make_audio()
        with open(self.make_image(), "rb") as fh:
            image = fh.read()
        # The real validator: magic bytes, then ffprobe, then a real decode.
        meta = star_assets.store_background(self.svc.state, image)

        caption = "ดวงการงานวันนี้ ราคา 1:2 'พิเศษ'"
        ctx = self.context(overlay_text_mode="custom", custom_overlay_text=caption,
                           background_asset_id=meta["id"], dry_run=False)
        background = self.stage.background_path(ctx)
        self.assertTrue(background.startswith(self.state_dir + os.sep))

        text = self.stage.overlay_text(ctx, DATE, DAY)
        self.assertEqual(text, caption)
        fontsize, width = star_automation.overlay_layout(text)
        textfile = self.stage._write_overlay(ctx, DATE, DAY, text, width)

        out = os.path.join(ctx.stage_dir(self.stage.name), "sample.mp4")
        argv = self.stage.build_command(
            audio, out, font=star_automation.find_thai_font(), fontsize=fontsize,
            background=background, textfile=textfile)
        code, lines = star_automation.run_command(argv, timeout=180)
        self.assertEqual(code, 0, " | ".join(lines[-3:]))
        self.assertGreater(os.path.getsize(out), 1000)

        probe = shutil.which("ffprobe")
        if probe:
            code, lines = star_automation.run_command(
                [probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0", out], timeout=60)
            self.assertEqual(code, 0)
            self.assertIn("1080,1920", " ".join(lines))

        # The finished clip stayed in the job workspace: nothing was promoted
        # into the project tree, because only execute() does that.
        self.assertTrue(out.startswith(ctx.workdir + os.sep))
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "output", DATE, "video", "%s.mp4" % DAY)))


if __name__ == "__main__":
    unittest.main()
