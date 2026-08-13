#!/usr/bin/env python3
"""Unit tests for the automation core: redaction, state permissions, the job
store, validation, the pipeline runner, the scheduler and the OAuth flow.

Every test runs against a temp project root and a temp state directory, with
STAR_DISABLE_NETWORK=1 set so any accidental provider call raises instead of
reaching a real API.
"""

import base64
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_automation  # noqa: E402
import star_jobs  # noqa: E402
import star_providers  # noqa: E402
import star_redact  # noqa: E402
import star_state  # noqa: E402

FAKE_TOKEN = "EAAG" + "Zx9Qk4tPl2mNvR7sBd3fH6jU8wY1aC5eT0gI4oL7pS2" + "vX"
FAKE_REFRESH = "1//0gTESTrefreshTOKENvalue123456789"
FAKE_GOOGLE_API_KEY = "AIzaSyD0-not-a-real-key-7pQ3vW9xL2mN6cB"


class TempEnv(unittest.TestCase):
    """Temp root + temp state dir + network hard-disabled."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="star-auto-root-")
        self.state_dir = tempfile.mkdtemp(prefix="star-auto-state-")
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


# ── redaction ─────────────────────────────────────────────────────────

class TestRedaction(unittest.TestCase):
    def test_bearer_header_is_masked(self):
        text = "GET /me\nAuthorization: Bearer %s\n" % FAKE_TOKEN
        out = star_redact.redact_text(text)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertIn(star_redact.MASK, out)

    def test_key_value_pairs_are_masked(self):
        for line in ('access_token=%s' % FAKE_TOKEN,
                     '"client_secret": "GOCSPX-abcdef123456"',
                     "api_key = sk-abcdefghijklmnopqrstuvwx",
                     'password: "hunter2hunter2"'):
            out = star_redact.redact_text(line)
            self.assertIn(star_redact.MASK, out, line)

    def test_google_refresh_token_shape_is_masked(self):
        out = star_redact.redact_text("refreshed with %s ok" % FAKE_REFRESH)
        self.assertNotIn(FAKE_REFRESH, out)

    def test_private_key_block_is_masked(self):
        pem = ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\nsecretline\n"
               "-----END PRIVATE KEY-----")
        self.assertNotIn("secretline", star_redact.redact_text(pem))

    def test_redact_obj_masks_secret_keys_recursively(self):
        payload = {"page_id": "123", "page_access_token": FAKE_TOKEN,
                   "nested": [{"refresh_token": FAKE_REFRESH, "name": "ok"}]}
        out = star_redact.redact_obj(payload)
        self.assertEqual(out["page_id"], "123")
        self.assertEqual(out["page_access_token"], star_redact.MASK)
        self.assertEqual(out["nested"][0]["refresh_token"], star_redact.MASK)
        self.assertEqual(out["nested"][0]["name"], "ok")
        self.assertNotIn(FAKE_TOKEN, json.dumps(out))

    def test_masked_hint_keys_survive(self):
        # *_masked values are produced by mask_tail and must stay readable.
        out = star_redact.redact_obj({"token_masked": "****cdef"})
        self.assertEqual(out["token_masked"], "****cdef")

    def test_mask_tail_never_reveals_the_secret(self):
        hint = star_redact.mask_tail(FAKE_TOKEN)
        self.assertTrue(hint.startswith("****"))
        self.assertEqual(len(hint), 8)
        self.assertNotIn(hint[4:], FAKE_TOKEN[:-4])

    def test_text_is_bounded(self):
        out = star_redact.redact_text("x" * 5000, limit=100)
        self.assertLess(len(out), 200)
        self.assertIn("[truncated]", out)


# ── state directory permissions ───────────────────────────────────────

class TestStateDir(TempEnv):
    def test_directory_is_0700_and_files_are_0600(self):
        state = star_state.StateDir(os.path.join(self.state_dir, "nested"))
        self.assertEqual(state.dir_mode(), 0o700)
        state.write_credential("provider_facebook", {"page_access_token": FAKE_TOKEN})
        self.assertEqual(state.credential_mode("provider_facebook"), 0o600)
        self.assertEqual(state.audit(), [])

    def test_audit_reports_a_widened_credential(self):
        state = star_state.StateDir(os.path.join(self.state_dir, "audit"))
        state.write_credential("provider_line", {"channel_access_token": FAKE_TOKEN})
        os.chmod(state.credential_path("provider_line"), 0o644)
        problems = state.audit()
        self.assertEqual(len(problems), 1)
        self.assertIn("provider_line", problems[0]["path"])

    def test_credential_name_cannot_traverse(self):
        state = star_state.StateDir(os.path.join(self.state_dir, "traverse"))
        for bad in ("../escape", "a/b", "..", ".hidden", "x" * 200, ""):
            with self.assertRaises(star_state.StateError):
                state.credential_path(bad)

    def test_job_dir_is_created_under_the_state_dir(self):
        state = star_state.StateDir(os.path.join(self.state_dir, "jobs-test"))
        path = state.job_dir("a" * 32)
        self.assertTrue(path.startswith(state.path + os.sep))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)

    def test_db_sidecars_are_owner_only(self):
        state = star_state.StateDir(os.path.join(self.state_dir, "db"))
        store = star_jobs.JobStore(state.db_path)
        store.get_schedule()
        self.assertEqual(stat.S_IMODE(os.stat(state.db_path).st_mode) & 0o077, 0)


# ── job input validation ──────────────────────────────────────────────

class TestJobValidation(unittest.TestCase):
    def valid(self, **overrides):
        payload = {"from_date": "2026-08-11", "to_date": "2026-08-12"}
        payload.update(overrides)
        return star_jobs.validate_job_input(payload)

    def test_happy_path_normalises(self):
        out = self.valid(days=["fri", "mon"], stages=["video", "astro"])
        self.assertEqual(out["dates"], ["2026-08-11", "2026-08-12"])
        # Canonical order, regardless of how the caller listed them.
        self.assertEqual(out["days"], ["mon", "fri"])
        self.assertEqual(out["stages"], ["astro", "video"])
        self.assertTrue(out["dry_run"], "dry_run must default to true")

    def test_all_expands(self):
        out = self.valid(days="all", stages="all", platforms=["r2"])
        self.assertEqual(out["days"], list(star_jobs.DAYS))
        self.assertEqual(out["stages"], list(star_jobs.STAGES))

    def test_publish_is_opt_in_by_default(self):
        out = self.valid()
        self.assertEqual(out["stages"], list(star_jobs.DEFAULT_STAGES))
        self.assertNotIn("publish", out["stages"])
        self.assertEqual(out["platforms"], [])

    def test_bad_dates(self):
        for payload in ({"from_date": "11-08-2026"},
                        {"from_date": "2026-02-30"},
                        {"from_date": "2026-08-11", "to_date": "2026-08-10"},
                        {"from_date": None},
                        {"from_date": "2026-08-11/../etc"}):
            with self.assertRaises(star_jobs.JobValidationError):
                star_jobs.validate_job_input(payload)

    def test_range_is_capped_at_31_days(self):
        star_jobs.validate_job_input({"from_date": "2026-08-01", "to_date": "2026-08-31"})
        with self.assertRaises(star_jobs.JobValidationError) as ctx:
            star_jobs.validate_job_input({"from_date": "2026-08-01",
                                          "to_date": "2026-09-01"})
        self.assertEqual(ctx.exception.field, "to_date")

    def test_unknown_and_duplicate_members_are_rejected(self):
        for kwargs in ({"days": ["funday"]}, {"days": ["mon", "mon"]},
                       {"stages": ["deploy"]}, {"platforms": ["myspace"]},
                       {"days": []}, {"days": "some"}):
            with self.assertRaises(star_jobs.JobValidationError):
                self.valid(**kwargs)

    def test_publish_requires_a_platform(self):
        with self.assertRaises(star_jobs.JobValidationError) as ctx:
            self.valid(stages=["publish"], platforms=[])
        self.assertEqual(ctx.exception.field, "platforms")

    def test_shell_metacharacters_cannot_survive_validation(self):
        for value in ("2026-08-11; rm -rf /", "$(whoami)", "2026-08-11 && ls"):
            with self.assertRaises(star_jobs.JobValidationError):
                star_jobs.validate_job_input({"from_date": value})

    def test_job_id_validation(self):
        star_jobs.valid_job_id("a" * 32)
        for bad in ("../../etc/passwd", "A" * 32, "abc", 42, None):
            with self.assertRaises(star_jobs.JobValidationError):
                star_jobs.valid_job_id(bad)

    # ── video customisation: overlay text and background image ───────
    #
    # Both fields are optional, and a job that mentions neither must come out
    # exactly as it did before the feature existed. Everything below is about
    # what reaches the video stage, so a value that survives validation is a
    # value the renderer will draw.

    def test_customisation_defaults_to_the_untouched_template(self):
        out = self.valid()
        self.assertEqual(out["overlay_text_mode"],
                         star_jobs.DEFAULT_OVERLAY_TEXT_MODE)
        self.assertEqual(out["overlay_text_mode"], "auto")
        self.assertIsNone(out["custom_overlay_text"],
                          "auto mode must carry no text of its own")
        self.assertIsNone(out["background_asset_id"])

    def test_auto_mode_accepts_an_absent_or_blank_text(self):
        """Blank is not a custom line, so it is not treated as one."""
        for value in (None, "", "   ", "\t\n"):
            out = self.valid(overlay_text_mode="auto", custom_overlay_text=value)
            self.assertIsNone(out["custom_overlay_text"], repr(value))

    def test_auto_mode_rejects_a_real_custom_text(self):
        """Text in auto mode is an instruction the renderer would ignore; it is
        refused rather than silently dropped."""
        with self.assertRaises(star_jobs.JobValidationError) as ctx:
            self.valid(overlay_text_mode="auto",
                       custom_overlay_text="ดวงประจำสัปดาห์นี้")
        self.assertEqual(ctx.exception.field, "custom_overlay_text")

    def test_custom_mode_requires_a_text(self):
        for value in (None, "", "   ", "\t\r\n"):
            with self.assertRaises(star_jobs.JobValidationError) as ctx:
                self.valid(overlay_text_mode="custom", custom_overlay_text=value)
            self.assertEqual(ctx.exception.field, "custom_overlay_text", repr(value))

    def test_custom_text_is_trimmed(self):
        out = self.valid(overlay_text_mode="custom",
                         custom_overlay_text="  ดวงประจำสัปดาห์นี้  \n")
        self.assertEqual(out["custom_overlay_text"], "ดวงประจำสัปดาห์นี้")
        self.assertEqual(out["overlay_text_mode"], "custom")

    def test_custom_text_length_boundary(self):
        limit = star_jobs.MAX_CUSTOM_OVERLAY_TEXT
        self.assertEqual(limit, 220)
        # Exactly at the limit passes, and the surrounding whitespace is not
        # counted against it: the stored value is what gets measured.
        out = self.valid(overlay_text_mode="custom",
                         custom_overlay_text="  " + "ก" * limit + "  ")
        self.assertEqual(len(out["custom_overlay_text"]), limit)
        with self.assertRaises(star_jobs.JobValidationError) as ctx:
            self.valid(overlay_text_mode="custom",
                       custom_overlay_text="ก" * (limit + 1))
        self.assertEqual(ctx.exception.field, "custom_overlay_text")

    def test_custom_text_rejects_control_characters(self):
        """The text is drawn by ffmpeg; a NUL or an escape has no glyph and no
        business travelling this far. Embedded mid-string so a strip() cannot
        be what rejects them."""
        for bad in ("before\x00after", "esc\x1bhere", "del\x7fhere",
                    "bell\x07here", "vert\x0btab", "c1\x9fhere"):
            with self.assertRaises(star_jobs.JobValidationError) as ctx:
                self.valid(overlay_text_mode="custom", custom_overlay_text=bad)
            self.assertEqual(ctx.exception.field, "custom_overlay_text", repr(bad))

    def test_custom_text_keeps_the_whitespace_a_human_types(self):
        out = self.valid(overlay_text_mode="custom",
                         custom_overlay_text="บรรทัดแรก\nบรรทัดสอง\tเว้น")
        self.assertEqual(out["custom_overlay_text"], "บรรทัดแรก\nบรรทัดสอง\tเว้น")

    def test_custom_text_must_be_a_string(self):
        for bad in (42, 3.5, True, ["ข้อความ"], {"text": "x"}):
            with self.assertRaises(star_jobs.JobValidationError) as ctx:
                self.valid(overlay_text_mode="custom", custom_overlay_text=bad)
            self.assertEqual(ctx.exception.field, "custom_overlay_text", repr(bad))

    def test_overlay_mode_must_be_a_known_mode(self):
        for bad in ("AUTO", "Custom", "random", "auto ", "", "manual",
                    42, True, ["custom"], {"mode": "custom"}):
            with self.assertRaises(star_jobs.JobValidationError) as ctx:
                self.valid(overlay_text_mode=bad)
            self.assertEqual(ctx.exception.field, "overlay_text_mode", repr(bad))

    def test_every_declared_overlay_mode_is_accepted(self):
        for mode in star_jobs.OVERLAY_TEXT_MODES:
            text = "ข้อความกลาง" if mode == "custom" else None
            out = self.valid(overlay_text_mode=mode, custom_overlay_text=text)
            self.assertEqual(out["overlay_text_mode"], mode)

    def test_background_asset_id_accepts_a_server_minted_id(self):
        asset_id = "0123456789abcdef" * 2
        self.assertEqual(len(asset_id), 32)
        out = self.valid(background_asset_id=asset_id)
        self.assertEqual(out["background_asset_id"], asset_id)

    def test_background_asset_id_is_optional(self):
        for value in (None, ""):
            self.assertIsNone(self.valid(background_asset_id=value)["background_asset_id"])

    def test_background_asset_id_rejects_anything_but_32_lowercase_hex(self):
        """The id is a lookup key on a server-side path, so nothing that could
        walk out of the asset directory may pass — including the uppercase form
        of an otherwise real id."""
        for bad in ("A" * 32, "0123456789ABCDEF" * 2, "abc", "a" * 31, "a" * 33,
                    "../../etc/passwd", "a" * 30 + "/x", "a" * 31 + "g",
                    "  " + "a" * 32, "a" * 32 + "\n", "a" * 16 + "-" + "a" * 15,
                    42, True, ["a" * 32], {"id": "a" * 32}):
            with self.assertRaises(star_jobs.JobValidationError) as ctx:
                self.valid(background_asset_id=bad)
            self.assertEqual(ctx.exception.field, "background_asset_id", repr(bad))

    def test_customisation_survives_a_full_job_body(self):
        """The new fields do not disturb the ones that were already there."""
        asset_id = "f" * 32
        out = self.valid(days=["mon"], stages=["astro", "video"],
                         overlay_text_mode="custom",
                         custom_overlay_text="  ดวงรายสัปดาห์  ",
                         background_asset_id=asset_id)
        self.assertEqual(out["days"], ["mon"])
        self.assertEqual(out["stages"], ["astro", "video"])
        self.assertEqual(out["custom_overlay_text"], "ดวงรายสัปดาห์")
        self.assertEqual(out["background_asset_id"], asset_id)
        self.assertTrue(out["dry_run"])


class TestScheduleValidation(unittest.TestCase):
    def test_defaults_are_disabled_and_dry(self):
        out = star_jobs.validate_schedule_input({})
        self.assertFalse(out["enabled"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["timezone"], "Asia/Bangkok")

    def test_bad_time_rejected(self):
        for bad in ("24:00", "5:30", "05:60", "0530", "05:30:00", 530):
            with self.assertRaises(star_jobs.JobValidationError):
                star_jobs.validate_schedule_input({"time": bad})

    def test_offset_bounds(self):
        with self.assertRaises(star_jobs.JobValidationError):
            star_jobs.validate_schedule_input({"date_offset_days": 999})
        with self.assertRaises(star_jobs.JobValidationError):
            star_jobs.validate_schedule_input({"date_offset_days": True})


# ── job store ─────────────────────────────────────────────────────────

class TestJobStore(TempEnv):
    def store(self):
        return star_jobs.JobStore(os.path.join(self.state_dir, "automation.db"))

    def job_input(self, **kw):
        payload = {"from_date": "2026-08-11", "stages": ["astro"]}
        payload.update(kw)
        return star_jobs.validate_job_input(payload)

    def test_only_one_active_job(self):
        store = self.store()
        first = store.create_job(self.job_input())
        with self.assertRaises(star_jobs.JobConflict) as ctx:
            store.create_job(self.job_input())
        self.assertEqual(ctx.exception.active["id"], first["id"])

    def test_a_finished_job_frees_the_queue(self):
        store = self.store()
        first = store.create_job(self.job_input())
        store.finish_job(first["id"], "succeeded")
        second = store.create_job(self.job_input())
        self.assertNotEqual(first["id"], second["id"])

    def test_cancel_queued_job_is_immediate(self):
        store = self.store()
        job = store.create_job(self.job_input())
        changed, updated = store.request_cancel(job["id"])
        self.assertTrue(changed)
        self.assertEqual(updated["status"], "cancelled")

    def test_cancel_running_job_only_sets_the_flag(self):
        store = self.store()
        store.create_job(self.job_input())
        running = store.claim_next()
        self.assertEqual(running["status"], "running")
        changed, updated = store.request_cancel(running["id"])
        self.assertTrue(changed)
        self.assertEqual(updated["status"], "running")
        self.assertTrue(store.cancel_requested(running["id"]))

    def test_cancelling_a_finished_job_reports_no_change(self):
        store = self.store()
        job = store.create_job(self.job_input())
        store.finish_job(job["id"], "succeeded")
        changed, updated = store.request_cancel(job["id"])
        self.assertFalse(changed)
        self.assertEqual(updated["status"], "succeeded")

    def test_orphan_recovery_marks_running_jobs_failed(self):
        store = self.store()
        store.create_job(self.job_input())
        running = store.claim_next()
        # A fresh store stands in for a service restart.
        recovered = self.store().recover_orphans()
        self.assertEqual(recovered, [running["id"]])
        after = store.get_job(running["id"])
        self.assertEqual(after["status"], "failed")
        self.assertIn("restart", after["safe_error"])

    def test_events_are_bounded_and_paginated(self):
        store = self.store()
        job = store.create_job(self.job_input())
        for i in range(10):
            store.add_event(job["id"], "info", "line %d" % i)
        events = store.list_events(job["id"], limit=5)
        self.assertEqual(len(events), 5)
        rest = store.list_events(job["id"], after_id=events[-1]["id"])
        self.assertEqual(len(rest), 6)  # 5 remaining + the "job queued" line
        store.add_event(job["id"], "info", "z" * 99999)
        self.assertLess(len(store.list_events(job["id"])[-1]["message"]),
                        star_jobs.MAX_EVENT_MESSAGE + 100)

    def test_schedule_round_trips(self):
        store = self.store()
        self.assertFalse(store.get_schedule()["enabled"])
        config = star_jobs.validate_schedule_input(
            {"enabled": True, "time": "06:15", "days": ["mon"], "stages": ["astro"]})
        stored = store.set_schedule(config)
        self.assertTrue(stored["enabled"])
        self.assertEqual(stored["time"], "06:15")
        self.assertEqual(stored["days"], ["mon"])
        self.assertEqual(store.get_schedule()["stages"], ["astro"])

    def test_unsaved_default_schedule_is_valid_input(self):
        """The default the store advertises must survive validate_schedule_input.

        Regression for the default that shipped `publish` with no platforms and
        therefore could not be PUT back unchanged.
        """
        default = self.store().get_schedule()
        self.assertNotIn("publish", default["stages"])
        self.assertEqual(default["platforms"], [])
        config = star_jobs.validate_schedule_input(
            {key: default[key] for key in
             ("enabled", "time", "date_offset_days", "days", "stages",
              "platforms", "dry_run")})
        self.assertFalse(config["enabled"])
        self.assertTrue(config["dry_run"])
        self.assertEqual(config["stages"], default["stages"])
        self.assertEqual(config["platforms"], [])

    def test_schedule_run_can_only_be_claimed_once(self):
        store = self.store()
        self.assertTrue(store.claim_schedule_run("2026-08-11"))
        self.assertFalse(store.claim_schedule_run("2026-08-11"))
        self.assertTrue(store.claim_schedule_run("2026-08-12"))

    def test_claiming_a_run_stamps_last_run_date(self):
        store = self.store()
        store.set_schedule(star_jobs.validate_schedule_input(
            {"enabled": True, "time": "05:30", "stages": ["astro"]}))
        self.assertIsNone(store.get_schedule()["last_run_date"])
        self.assertTrue(store.claim_schedule_run("2026-08-11"))
        self.assertEqual(store.get_schedule()["last_run_date"], "2026-08-11")

    def test_releasing_a_run_undoes_both_halves_of_the_claim(self):
        """A release has to clear last_run_date, not only the reservation row.

        Regression: the reservation row was deleted but last_run_date was left
        pointing at the released day, and Scheduler.tick returns early on
        exactly that value — so a "released" day was still burned until
        midnight and the run was lost instead of retried.
        """
        store = self.store()
        store.set_schedule(star_jobs.validate_schedule_input(
            {"enabled": True, "time": "05:30", "stages": ["astro"]}))
        self.assertTrue(store.claim_schedule_run("2026-08-11"))
        store.release_schedule_run("2026-08-11")
        self.assertIsNone(store.get_schedule()["last_run_date"])
        self.assertTrue(store.claim_schedule_run("2026-08-11"),
                        "a released day must be claimable again")

    def test_releasing_one_day_leaves_another_days_stamp_alone(self):
        store = self.store()
        store.set_schedule(star_jobs.validate_schedule_input(
            {"enabled": True, "time": "05:30", "stages": ["astro"]}))
        self.assertTrue(store.claim_schedule_run("2026-08-11"))
        store.release_schedule_run("2026-08-10")
        self.assertEqual(store.get_schedule()["last_run_date"], "2026-08-11")

    def test_finish_job_will_not_overwrite_a_terminal_job(self):
        """The first terminal write wins.

        Two paths can try to finish the same job — execute_job finishing it
        normally and JobRunner catching whatever escaped that — and a second
        write would replace a real result with a generic failure.
        """
        store = self.store()
        job = store.create_job(self.job_input())
        store.claim_next()
        self.assertTrue(store.finish_job(job["id"], "succeeded",
                                         result={"stages": []}))
        self.assertFalse(store.finish_job(job["id"], "failed",
                                          safe_error="internal error"))
        after = store.get_job(job["id"])
        self.assertEqual(after["status"], "succeeded")
        self.assertIsNone(after["safe_error"])
        self.assertEqual(after["result"], {"stages": []})

    def test_finish_job_still_rejects_a_non_terminal_status(self):
        store = self.store()
        job = store.create_job(self.job_input())
        with self.assertRaises(ValueError):
            store.finish_job(job["id"], "running")

    def test_schema_is_migration_safe(self):
        path = os.path.join(self.state_dir, "again.db")
        star_jobs.JobStore(path)
        star_jobs.JobStore(path)  # must not raise on an existing schema
        conn = sqlite3.connect(path)
        try:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue({"jobs", "job_events", "schedule", "schedule_runs",
                         "oauth_state"} <= names)


# ── oauth ─────────────────────────────────────────────────────────────

class TestOAuthState(TempEnv):
    def store(self):
        return star_jobs.JobStore(os.path.join(self.state_dir, "oauth.db"))

    def test_state_is_single_use(self):
        store = self.store()
        store.put_oauth_state("st1", "youtube", "verifier", "https://x/cb")
        record, reason = store.consume_oauth_state("st1", "youtube")
        self.assertEqual(reason, "ok")
        self.assertEqual(record["code_verifier"], "verifier")
        record, reason = store.consume_oauth_state("st1", "youtube")
        self.assertIsNone(record)
        self.assertEqual(reason, "already used")

    def test_expired_state_is_rejected(self):
        store = self.store()
        store.put_oauth_state("st2", "youtube", "v", "https://x/cb", ttl_seconds=-5)
        record, reason = store.consume_oauth_state("st2", "youtube")
        self.assertIsNone(record)
        self.assertEqual(reason, "expired")

    def test_unknown_state_and_provider_mismatch_rejected(self):
        store = self.store()
        store.put_oauth_state("st3", "youtube", "v", "https://x/cb")
        self.assertIsNone(store.consume_oauth_state("nope", "youtube")[0])
        self.assertIsNone(store.consume_oauth_state("st3", "facebook")[0])


class TestYouTubeOAuthFlow(TempEnv):
    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self.svc.providers.get("youtube").configure({
            "client_json": {"web": {"client_id": "cid.apps.googleusercontent.com",
                                    "client_secret": "GOCSPX-testsecret"}},
            "redirect_uri": "https://star.example/api/oauth/youtube/callback",
        })

    def test_start_builds_a_pkce_url(self):
        out = self.svc.oauth.start(host="star.example")
        self.assertIn("code_challenge=", out["authorization_url"])
        self.assertIn("code_challenge_method=S256", out["authorization_url"])
        self.assertIn("access_type=offline", out["authorization_url"])
        self.assertIn("state=", out["authorization_url"])
        self.assertNotIn("GOCSPX", out["authorization_url"],
                         "the client secret must never appear in the auth URL")

    def test_start_requires_a_configured_client(self):
        self.svc.providers.get("youtube").clear()
        with self.assertRaises(star_providers.ProviderError):
            self.svc.oauth.start(host="star.example")

    def _state_from(self, url):
        import urllib.parse
        return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]

    def test_callback_stores_the_refresh_token_and_never_returns_it(self):
        start = self.svc.oauth.start(host="star.example")
        state = self._state_from(start["authorization_url"])

        seen = {}

        def fake_exchange(form, endpoint):
            seen.update(form)
            return {"refresh_token": FAKE_REFRESH, "token_type": "Bearer",
                    "scope": "https://www.googleapis.com/auth/youtube.upload"}

        self.svc.oauth.token_exchange = fake_exchange
        status = self.svc.oauth.callback({"code": "auth-code-1", "state": state})

        self.assertIn("code_verifier", seen, "PKCE verifier must be sent on exchange")
        self.assertEqual(seen["grant_type"], "authorization_code")
        self.assertEqual(status["status"], star_providers.READY)
        self.assertNotIn(FAKE_REFRESH, json.dumps(status))
        self.assertTrue(self.svc.providers.get("youtube").has_refresh_token())

    def test_callback_state_is_single_use(self):
        start = self.svc.oauth.start(host="star.example")
        state = self._state_from(start["authorization_url"])
        self.svc.oauth.token_exchange = lambda form, endpoint: {
            "refresh_token": FAKE_REFRESH}
        self.svc.oauth.callback({"code": "c", "state": state})
        with self.assertRaises(star_providers.ProviderError) as ctx:
            self.svc.oauth.callback({"code": "c", "state": state})
        self.assertIn("already used", ctx.exception.message)

    def test_callback_rejects_a_forged_state_before_exchanging(self):
        called = []
        self.svc.oauth.token_exchange = lambda form, endpoint: called.append(1) or {}
        with self.assertRaises(star_providers.ProviderError):
            self.svc.oauth.callback({"code": "c", "state": "forged"})
        self.assertEqual(called, [], "a bad state must short-circuit the exchange")

    def test_callback_without_refresh_token_is_an_error(self):
        start = self.svc.oauth.start(host="star.example")
        state = self._state_from(start["authorization_url"])
        self.svc.oauth.token_exchange = lambda form, endpoint: {"access_token": "x"}
        with self.assertRaises(star_providers.ProviderError) as ctx:
            self.svc.oauth.callback({"code": "c", "state": state})
        self.assertIn("refresh token", ctx.exception.message)

    def test_error_callback_still_burns_the_state(self):
        start = self.svc.oauth.start(host="star.example")
        state = self._state_from(start["authorization_url"])
        with self.assertRaises(star_providers.ProviderError):
            self.svc.oauth.callback({"error": "access_denied", "state": state})
        record, reason = self.svc.store.consume_oauth_state(state, "youtube")
        self.assertIsNone(record)
        self.assertEqual(reason, "already used")


# ── providers ─────────────────────────────────────────────────────────

class TestProviders(TempEnv):
    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self.registry = self.svc.providers

    def test_unknown_provider_rejected(self):
        with self.assertRaises(star_providers.ProviderError):
            self.registry.get("myspace")

    def test_claude_never_accepts_a_token(self):
        with self.assertRaises(star_providers.ProviderError) as ctx:
            self.registry.get("claude").configure({"session_token": "abc"})
        self.assertIn("never accepts", ctx.exception.message)

    def test_claude_test_uses_an_argv_list_and_never_a_shell(self):
        calls = []

        def fake_runner(argv, timeout=None, env=None):
            calls.append(argv)
            return 0, "Logged in as ops@example.com", ""

        provider = self.registry.get("claude")
        provider.runner = fake_runner
        result = provider.test()
        self.assertTrue(calls, "the runner should have been invoked")
        self.assertIsInstance(calls[0], list)
        self.assertEqual(calls[0][1:], ["auth", "status"])
        self.assertIn(result["status"], (star_providers.READY, star_providers.ERROR))

    def test_facebook_configure_stores_0600_and_returns_no_secret(self):
        status = self.registry.get("facebook").configure(
            {"page_id": "123456", "page_access_token": FAKE_TOKEN})
        self.assertEqual(status["status"], star_providers.READY)
        self.assertNotIn(FAKE_TOKEN, json.dumps(status))
        self.assertEqual(self.svc.state.credential_mode("provider_facebook"), 0o600)

    def test_facebook_rejects_bad_input(self):
        for payload in ({"page_id": "12", "page_access_token": ""},
                        {"page_id": "../etc", "page_access_token": FAKE_TOKEN},
                        {"page_access_token": FAKE_TOKEN},
                        {"page_id": "1", "page_access_token": 42}):
            with self.assertRaises(star_providers.ProviderError):
                self.registry.get("facebook").configure(payload)

    def test_offline_test_makes_no_network_call(self):
        self.registry.get("facebook").configure(
            {"page_id": "123456", "page_access_token": FAKE_TOKEN})
        calls = []
        self.registry.get("facebook").http_get = lambda *a, **k: calls.append(a)
        result = self.registry.get("facebook").test(live=False)
        self.assertEqual(calls, [])
        self.assertFalse(result["live_test"])

    def test_live_test_is_blocked_when_the_network_is_disabled(self):
        self.registry.get("line").configure({"channel_access_token": FAKE_TOKEN})
        with self.assertRaises(star_providers.ProviderError) as ctx:
            self.registry.get("line").test(live=True)
        self.assertIn("network access is disabled", ctx.exception.message)

    def test_google_tts_validates_the_service_account_schema(self):
        provider = self.registry.get("google_tts")
        with self.assertRaises(star_providers.ProviderError):
            provider.configure({"service_account_json": {"type": "service_account"}})
        with self.assertRaises(star_providers.ProviderError):
            provider.configure({"service_account_json": {
                "type": "user", "project_id": "p", "private_key": "k",
                "client_email": "e@x"}})
        status = provider.configure({"service_account_json": {
            "type": "service_account", "project_id": "star-proj",
            "private_key": "-----BEGIN PRIVATE KEY-----\nzzz\n-----END PRIVATE KEY-----",
            "client_email": "tts@star-proj.iam.gserviceaccount.com"}})
        self.assertEqual(status["status"], star_providers.READY)
        self.assertNotIn("BEGIN PRIVATE KEY", json.dumps(status))
        key_file = os.path.join(self.svc.state.path, "credentials",
                                "google_tts_service_account.json")
        self.assertEqual(stat.S_IMODE(os.stat(key_file).st_mode), 0o600)

    def test_google_tts_api_key_configure_status_and_storage_are_safe(self):
        provider = self.registry.get("google_tts")
        api_field = next(field for field in provider.fields
                         if field["name"] == "api_key")
        self.assertEqual(api_field["type"], "password")
        self.assertTrue(api_field["write_only"])
        self.assertFalse(api_field["required"])

        status = provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        self.assertEqual(status["status"], star_providers.READY)
        self.assertEqual(status["key_mode"], "api_key")
        self.assertEqual(status["masked_hint"],
                         star_redact.mask_tail(FAKE_GOOGLE_API_KEY))
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(status))
        self.assertEqual(provider.stored(), {
            "mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY})
        self.assertEqual(self.svc.state.credential_mode("provider_google_tts"), 0o600)
        service_account_file = os.path.join(
            self.svc.state.path, "credentials", "google_tts_service_account.json")
        self.assertFalse(os.path.exists(service_account_file))

        reread = provider.status()
        self.assertEqual(reread["status"], star_providers.READY)
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(reread))

    def test_google_tts_configure_requires_exactly_one_bounded_mode(self):
        provider = self.registry.get("google_tts")
        for payload in ({},
                        {"api_key": FAKE_GOOGLE_API_KEY,
                         "service_account_json": {}},
                        {"api_key": FAKE_GOOGLE_API_KEY,
                         "credentials_path": "/tmp/not-used"}):
            with self.assertRaises(star_providers.ProviderError) as ctx:
                provider.configure(payload)
            self.assertIn("exactly one", ctx.exception.message)

        for bad_key in ("", "   ", 42,
                        "x" * (star_providers.GOOGLE_TTS_API_KEY_MAX_LENGTH + 1)):
            with self.assertRaises(star_providers.ProviderError) as ctx:
                provider.configure({"api_key": bad_key})
            self.assertEqual(ctx.exception.field, "api_key")

    def test_google_tts_api_key_offline_test_validates_schema_without_network(self):
        provider = self.registry.get("google_tts")
        provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        calls = []
        provider.http_get = lambda *args, **kwargs: calls.append((args, kwargs))

        result = provider.test(live=False)
        self.assertEqual(result["status"], star_providers.READY)
        self.assertFalse(result["live_test"])
        self.assertEqual(result["key_mode"], "api_key")
        self.assertEqual(calls, [])
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(result))

        self.svc.state.write_credential(
            "provider_google_tts", {"mode": "api_key", "api_key": "   "})
        invalid = provider.test(live=False)
        self.assertEqual(invalid["status"], star_providers.ERROR)
        self.assertFalse(invalid["live_test"])
        self.assertEqual(calls, [])

    def test_google_tts_api_key_live_test_lists_thai_voices_with_get_only(self):
        provider = self.registry.get("google_tts")
        provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        calls = []

        def fake_get(url, headers=None):
            calls.append((url, headers))
            return {"voices": [
                {"name": star_providers.GOOGLE_TTS_DEFAULT_VOICE,
                 "languageCodes": ["th-TH"]},
                {"name": "en-US-Standard-A", "languageCodes": ["en-US"]},
                {"name": "th-TH-Standard-A", "languageCodes": ["th-TH", "th"]},
            ]}

        provider.http_get = fake_get
        with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            result = provider.test(live=True)

        self.assertEqual(calls, [(provider.VOICES_URL,
                                  {"X-Goog-Api-Key": FAKE_GOOGLE_API_KEY})])
        self.assertEqual(result["status"], star_providers.READY)
        self.assertTrue(result["live_test"])
        self.assertEqual(result["voice_count"], 3)
        self.assertEqual(result["thai_voice_count"], 2)
        self.assertTrue(result["selected_voice_available"])
        self.assertIn("no speech synthesised", result["detail"])
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(result))

    def test_google_tts_api_key_live_error_cannot_echo_the_key(self):
        provider = self.registry.get("google_tts")
        provider.configure({"api_key": FAKE_GOOGLE_API_KEY})

        def failing_get(url, headers=None):
            raise RuntimeError("Google rejected api key " + FAKE_GOOGLE_API_KEY)

        provider.http_get = failing_get
        with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            result = provider.test(live=True)

        self.assertEqual(result["status"], star_providers.ERROR)
        self.assertTrue(result["live_test"])
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(result))
        self.assertIn(star_redact.MASK, result["detail"])

    def test_google_tts_path_mode_refuses_paths_outside_the_state_dir(self):
        outside = os.path.join(self.root, "key.json")
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump({"type": "service_account", "project_id": "p",
                       "private_key": "k", "client_email": "e@x"}, fh)
        with self.assertRaises(star_providers.ProviderError) as ctx:
            self.registry.get("google_tts").configure({"credentials_path": outside})
        self.assertEqual(ctx.exception.field, "credentials_path")

        for traversal in ("/etc/passwd", self.state_dir + "/../../etc/passwd"):
            with self.assertRaises(star_providers.ProviderError):
                self.registry.get("google_tts").configure(
                    {"credentials_path": traversal})

    # -- Thai voice picker ------------------------------------------------

    def test_google_tts_voice_catalogue_is_a_complete_unique_allowlist(self):
        voices = star_providers.GOOGLE_TTS_VOICES
        names = [voice["name"] for voice in voices]
        self.assertEqual(len(names), 32)
        self.assertEqual(len(set(names)), 32, "a voice is listed twice")
        for name in names:
            self.assertTrue(name.startswith("th-TH-"), name)
        for voice in voices:
            self.assertIn(voice["gender"], ("FEMALE", "MALE"), voice["name"])
            self.assertIn(voice["tier"], (star_providers.VOICE_TIER_CHIRP3_HD,
                                          star_providers.VOICE_TIER_NEURAL2,
                                          star_providers.VOICE_TIER_STANDARD))
            self.assertTrue(voice["label"].strip(), voice["name"])

        # The natural picks lead, in the order the operator was promised.
        self.assertEqual(names[:7], [
            "th-TH-Chirp3-HD-" + short for short in
            ("Kore", "Aoede", "Despina", "Charon", "Rasalgethi", "Schedar", "Puck")])
        self.assertTrue(all(voice["recommended"] for voice in voices[:7]))
        self.assertFalse(any(voice["recommended"] for voice in voices[7:]))

        default = star_providers.GOOGLE_TTS_VOICE_BY_NAME[
            star_providers.GOOGLE_TTS_DEFAULT_VOICE]
        self.assertEqual(names[0], star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        self.assertEqual(default["gender"], "FEMALE")
        self.assertEqual(default["tier"], star_providers.VOICE_TIER_CHIRP3_HD)
        self.assertIn("แนะนำ", default["label"])

        # Chirp3-HD is the natural tier; the two survivors are the older ones.
        legacy = {voice["name"]: voice["tier"] for voice in voices
                  if voice["tier"] != star_providers.VOICE_TIER_CHIRP3_HD}
        self.assertEqual(legacy, {
            "th-TH-Neural2-C": star_providers.VOICE_TIER_NEURAL2,
            "th-TH-Standard-A": star_providers.VOICE_TIER_STANDARD})

    def test_google_tts_voice_field_offers_every_voice_without_a_secret(self):
        provider = self.registry.get("google_tts")
        field = next(f for f in provider.fields if f["name"] == "voice_name")
        self.assertEqual(field["type"], "select")
        self.assertFalse(field["write_only"])
        self.assertFalse(field["required"])
        self.assertEqual(field["default"], star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        self.assertEqual(field["selected"], star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        values = [option["value"] for option in field["options"]]
        self.assertEqual(values,
                         [voice["name"] for voice in star_providers.GOOGLE_TTS_VOICES])
        for option in field["options"]:
            self.assertIn(option["gender"], ("FEMALE", "MALE"))
            self.assertTrue(option["label"])

    def test_google_tts_defaults_a_legacy_config_to_the_recommended_voice(self):
        """A credential stored before the picker existed keeps working."""
        provider = self.registry.get("google_tts")
        self.svc.state.write_credential(
            "provider_google_tts", {"mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY})
        self.assertNotIn("voice_name", provider.stored())

        status = provider.status()
        self.assertEqual(status["status"], star_providers.READY)
        self.assertEqual(status["selected_voice_name"],
                         star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        self.assertEqual(status["selected_voice_gender"], "FEMALE")
        self.assertEqual(status["selected_voice_tier"],
                         star_providers.VOICE_TIER_CHIRP3_HD)
        self.assertTrue(status["selected_voice_label"])
        self.assertEqual(provider.selected_voice()["name"],
                         star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(status))

        # A voice that Google no longer offers must not strand the provider.
        self.svc.state.write_credential(
            "provider_google_tts",
            {"mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY,
             "voice_name": "th-TH-Retired-Z"})
        recovered = provider.status()
        self.assertEqual(recovered["status"], star_providers.READY)
        self.assertEqual(recovered["selected_voice_name"],
                         star_providers.GOOGLE_TTS_DEFAULT_VOICE)

    def test_google_tts_rejects_a_voice_outside_the_allowlist(self):
        provider = self.registry.get("google_tts")
        for bad in ("th-TH-Chirp3-HD-Nope", "en-US-Chirp3-HD-Kore", "../etc/passwd",
                    "th-TH-Chirp3-HD-Kore ; rm -rf /", 7, ["th-TH-Standard-A"]):
            with self.assertRaises(star_providers.ProviderError) as ctx:
                provider.configure({"api_key": FAKE_GOOGLE_API_KEY, "voice_name": bad})
            self.assertEqual(ctx.exception.field, "voice_name")
        self.assertFalse(provider.is_configured(),
                         "a rejected voice must not store a credential")

    def test_google_tts_initial_configuration_takes_credential_and_voice(self):
        provider = self.registry.get("google_tts")
        chosen = "th-TH-Chirp3-HD-Charon"
        status = provider.configure(
            {"api_key": FAKE_GOOGLE_API_KEY, "voice_name": chosen})
        self.assertEqual(status["status"], star_providers.READY)
        self.assertEqual(status["selected_voice_name"], chosen)
        self.assertEqual(status["selected_voice_gender"], "MALE")
        self.assertEqual(provider.stored(), {
            "mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY, "voice_name": chosen})
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(status))

    def test_google_tts_voice_only_update_keeps_the_stored_key_byte_for_byte(self):
        provider = self.registry.get("google_tts")
        provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        before = provider.stored()

        status = provider.configure({"voice_name": "th-TH-Chirp3-HD-Aoede"})
        self.assertEqual(status["status"], star_providers.READY)
        self.assertEqual(status["selected_voice_name"], "th-TH-Chirp3-HD-Aoede")
        self.assertEqual(provider.stored(), {
            "mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY,
            "voice_name": "th-TH-Chirp3-HD-Aoede"})
        self.assertEqual(provider.stored()["api_key"], before["api_key"])
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(status))

        # An untouched credential box arrives as an empty string; it must be
        # ignored rather than wiping the stored key.
        provider.configure({"api_key": "", "service_account_json": "",
                            "credentials_path": "",
                            "voice_name": "th-TH-Chirp3-HD-Puck"})
        self.assertEqual(provider.stored(), {
            "mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY,
            "voice_name": "th-TH-Chirp3-HD-Puck"})

        # And a credential change keeps the voice that was already chosen.
        provider.configure({"api_key": FAKE_GOOGLE_API_KEY + "2"})
        self.assertEqual(provider.stored(), {
            "mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY + "2",
            "voice_name": "th-TH-Chirp3-HD-Puck"})

    def test_google_tts_voice_update_needs_a_credential_first(self):
        provider = self.registry.get("google_tts")
        with self.assertRaises(star_providers.ProviderError) as ctx:
            provider.configure({"voice_name": "th-TH-Chirp3-HD-Aoede"})
        self.assertIn("exactly one", ctx.exception.message)
        self.assertFalse(provider.is_configured())

        provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        with self.assertRaises(star_providers.ProviderError) as ctx:
            provider.configure({})
        self.assertEqual(ctx.exception.field, "voice_name")

    def test_google_tts_voice_survives_a_service_account_configuration(self):
        provider = self.registry.get("google_tts")
        provider.configure({"api_key": FAKE_GOOGLE_API_KEY,
                            "voice_name": "th-TH-Chirp3-HD-Despina"})
        status = provider.configure({"service_account_json": {
            "type": "service_account", "project_id": "star-proj",
            "private_key": "-----BEGIN PRIVATE KEY-----\nzzz\n-----END PRIVATE KEY-----",
            "client_email": "tts@star-proj.iam.gserviceaccount.com"}})
        self.assertEqual(status["selected_voice_name"], "th-TH-Chirp3-HD-Despina")
        stored = provider.stored()
        self.assertEqual(stored["mode"], "service_account_json")
        self.assertEqual(stored["voice_name"], "th-TH-Chirp3-HD-Despina")
        self.assertNotIn("api_key", stored, "the previous credential mode must go")

    def test_google_tts_live_test_fails_when_the_selected_voice_is_gone(self):
        provider = self.registry.get("google_tts")
        provider.configure({"api_key": FAKE_GOOGLE_API_KEY,
                            "voice_name": "th-TH-Chirp3-HD-Kore"})
        provider.http_get = lambda url, headers=None: {"voices": [
            {"name": "th-TH-Standard-A", "languageCodes": ["th-TH"]}]}
        with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            result = provider.test(live=True)

        self.assertEqual(result["status"], star_providers.ERROR)
        self.assertFalse(result["selected_voice_available"])
        self.assertIn("th-TH-Chirp3-HD-Kore", result["detail"])
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(result))

    def test_r2_validates_bucket_and_url(self):
        good = {"account_id": "acc1", "access_key_id": "AKIAEXAMPLEKEYID",
                "secret_access_key": "s" * 40, "bucket": "star-media",
                "public_base_url": "https://cdn.example.com/"}
        status = self.registry.get("r2").configure(good)
        self.assertEqual(status["status"], star_providers.READY)
        self.assertNotIn("s" * 40, json.dumps(status))
        self.assertEqual(status["public_base_url"], "https://cdn.example.com")

        for key, bad in (("bucket", "A_BAD_Bucket"), ("public_base_url", "ftp://x"),
                         ("account_id", "acc/../1")):
            payload = dict(good)
            payload[key] = bad
            with self.assertRaises(star_providers.ProviderError):
                self.registry.get("r2").configure(payload)

    def test_manual_platforms_never_report_ready(self):
        for key in ("tiktok", "shopee"):
            provider = self.registry.get(key)
            status = provider.status()
            self.assertEqual(status["status"], star_providers.MANUAL)
            self.assertNotEqual(status["automation"], star_providers.AUTOMATION_FULL)
            self.assertTrue(status["prerequisites"])
            self.assertIsNotNone(provider.prerequisite_error())
            with self.assertRaises(star_providers.ProviderError):
                provider.configure({"anything": "x"})

    def test_registry_reports_every_provider(self):
        keys = [s["provider"] for s in self.registry.statuses()]
        self.assertEqual(keys, list(star_providers.PROVIDER_KEYS))


# ── audio synthesis ---------------------------------------------------

class TestAudioStage(TempEnv):
    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self.provider = self.svc.providers.get("google_tts")
        self.stage = star_automation.AudioStage()

    def context(self, dry_run=False):
        job_input = star_jobs.validate_job_input({
            "from_date": "2026-08-11",
            "days": ["mon"],
            "stages": ["audio"],
            "dry_run": dry_run,
        })
        job = self.svc.store.create_job(job_input)
        return star_automation.JobContext(
            self.svc, job, self.svc.state.job_dir(job["id"]))

    def write_script(self):
        path = os.path.join(
            self.root, "content", "scripts", "claude_2026-08-11_mon.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("เสียงทดสอบภาษาไทย")

    def test_api_key_mode_posts_exact_request_and_writes_mp3(self):
        self.provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        self.write_script()
        ctx = self.context()
        mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x08test-mp3"
        calls = []

        def fake_post(url, payload, headers=None):
            calls.append((url, payload, headers))
            return {"audioContent": base64.b64encode(mp3).decode("ascii")}

        self.stage.http_post = fake_post
        self.assertEqual(self.stage.engine(ctx), "google_tts")
        with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            result = self.stage.execute(ctx)

        self.assertEqual(calls, [(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            {
                "input": {"text": "เสียงทดสอบภาษาไทย"},
                "voice": {
                    "languageCode": "th-TH",
                    "name": star_providers.GOOGLE_TTS_DEFAULT_VOICE,
                    "ssmlGender": "FEMALE",
                },
                "audioConfig": {"audioEncoding": "MP3"},
            },
            {
                "X-Goog-Api-Key": FAKE_GOOGLE_API_KEY,
                "Content-Type": "application/json",
            },
        )])
        target = os.path.join(self.root, "output", "2026-08-11", "audio", "mon.mp3")
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), mp3)
        self.assertEqual(result, {
            "audio_files": 1, "engine": "google_tts",
            "voice": star_providers.GOOGLE_TTS_DEFAULT_VOICE})
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(ctx.artifacts))

    def test_default_transport_is_a_stdlib_json_post_without_network(self):
        payload = {"input": {"text": "ไทย"}}
        headers = {
            "X-Goog-Api-Key": FAKE_GOOGLE_API_KEY,
            "Content-Type": "application/json",
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"ok":true}'

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as opener, \
                mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            result = star_automation._http_post_json(
                self.stage.GOOGLE_SYNTHESIZE_URL, payload, headers=headers)

        request = opener.call_args.args[0]
        request_headers = {
            name.lower(): value for name, value in request.header_items()}
        self.assertEqual(request.full_url, self.stage.GOOGLE_SYNTHESIZE_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{"input":{"text":"\\u0e44\\u0e17\\u0e22"}}')
        self.assertEqual(request_headers, {
            "x-goog-api-key": FAKE_GOOGLE_API_KEY,
            "content-type": "application/json",
        })
        self.assertEqual(opener.call_args.kwargs["timeout"], star_providers.HTTP_TIMEOUT)
        self.assertEqual(result, {"ok": True})

    def test_service_account_mode_keeps_client_library_path(self):
        self.provider.configure({"service_account_json": {
            "type": "service_account",
            "project_id": "star-proj",
            "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
            "client_email": "tts@star-proj.iam.gserviceaccount.com",
        }})
        ctx = self.context()
        out_path = os.path.join(ctx.stage_dir("audio"), "service-account.mp3")
        mp3 = b"ID3-service-account"
        calls = {}

        texttospeech = types.ModuleType("google.cloud.texttospeech")

        class FakeClient:
            @classmethod
            def from_service_account_file(cls, path):
                calls["credentials_path"] = path
                return cls()

            def synthesize_speech(self, **kwargs):
                calls["synthesize"] = kwargs
                return types.SimpleNamespace(audio_content=mp3)

        texttospeech.TextToSpeechClient = FakeClient
        texttospeech.SynthesisInput = lambda **kw: ("input", kw)
        texttospeech.VoiceSelectionParams = lambda **kw: ("voice", kw)
        texttospeech.AudioConfig = lambda **kw: ("config", kw)
        texttospeech.SsmlVoiceGender = types.SimpleNamespace(
            FEMALE="FEMALE", MALE="MALE")
        texttospeech.AudioEncoding = types.SimpleNamespace(MP3="MP3")
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        google.cloud = cloud
        cloud.texttospeech = texttospeech
        modules = {
            "google": google,
            "google.cloud": cloud,
            "google.cloud.texttospeech": texttospeech,
        }
        self.stage.http_post = mock.Mock(
            side_effect=AssertionError("API-key transport must not be used"))

        with mock.patch.dict(sys.modules, modules), \
                mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            self.stage._google(ctx, "service account text", out_path)

        self.stage.http_post.assert_not_called()
        self.assertEqual(calls["credentials_path"], self.provider.credentials_path())
        self.assertEqual(calls["synthesize"], {
            "input": ("input", {"text": "service account text"}),
            "voice": ("voice", {
                "language_code": "th-TH",
                "name": star_providers.GOOGLE_TTS_DEFAULT_VOICE,
                "ssml_gender": "FEMALE"}),
            "audio_config": ("config", {"audio_encoding": "MP3"}),
        })
        with open(out_path, "rb") as fh:
            self.assertEqual(fh.read(), mp3)

    def test_api_key_mode_asks_for_the_selected_voice_by_exact_name(self):
        self.provider.configure({"api_key": FAKE_GOOGLE_API_KEY,
                                 "voice_name": "th-TH-Chirp3-HD-Charon"})
        ctx = self.context()
        out_path = os.path.join(ctx.stage_dir("audio"), "selected.mp3")
        calls = []

        def fake_post(url, payload, headers=None):
            calls.append(payload)
            return {"audioContent": base64.b64encode(b"ID3-selected").decode("ascii")}

        self.stage.http_post = fake_post
        with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            self.stage._google(ctx, "ข้อความ", out_path)

        # Exact name, Thai language code, and the gender the allowlist holds —
        # never one a caller could have supplied.
        self.assertEqual(calls, [{
            "input": {"text": "ข้อความ"},
            "voice": {"languageCode": "th-TH",
                      "name": "th-TH-Chirp3-HD-Charon",
                      "ssmlGender": "MALE"},
            "audioConfig": {"audioEncoding": "MP3"},
        }])

    def test_service_account_mode_asks_for_the_selected_voice_by_exact_name(self):
        self.provider.configure({"service_account_json": {
            "type": "service_account",
            "project_id": "star-proj",
            "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
            "client_email": "tts@star-proj.iam.gserviceaccount.com",
        }})
        self.provider.configure({"voice_name": "th-TH-Chirp3-HD-Aoede"})
        ctx = self.context()
        out_path = os.path.join(ctx.stage_dir("audio"), "selected-sa.mp3")
        calls = {}

        texttospeech = types.ModuleType("google.cloud.texttospeech")

        class FakeClient:
            @classmethod
            def from_service_account_file(cls, path):
                return cls()

            def synthesize_speech(self, **kwargs):
                calls.update(kwargs)
                return types.SimpleNamespace(audio_content=b"ID3-selected-sa")

        texttospeech.TextToSpeechClient = FakeClient
        texttospeech.SynthesisInput = lambda **kw: ("input", kw)
        texttospeech.VoiceSelectionParams = lambda **kw: ("voice", kw)
        texttospeech.AudioConfig = lambda **kw: ("config", kw)
        texttospeech.SsmlVoiceGender = types.SimpleNamespace(
            FEMALE="FEMALE", MALE="MALE")
        texttospeech.AudioEncoding = types.SimpleNamespace(MP3="MP3")
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        google.cloud = cloud
        cloud.texttospeech = texttospeech
        modules = {"google": google, "google.cloud": cloud,
                   "google.cloud.texttospeech": texttospeech}

        with mock.patch.dict(sys.modules, modules), \
                mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            self.stage._google(ctx, "ข้อความ", out_path)

        self.assertEqual(calls["voice"], ("voice", {
            "language_code": "th-TH",
            "name": "th-TH-Chirp3-HD-Aoede",
            "ssml_gender": "FEMALE"}))
        self.assertEqual(calls["input"], ("input", {"text": "ข้อความ"}))
        self.assertEqual(calls["audio_config"], ("config", {"audio_encoding": "MP3"}))

    def test_dry_run_names_the_voice_without_synthesising(self):
        self.provider.configure({"api_key": FAKE_GOOGLE_API_KEY,
                                 "voice_name": "th-TH-Chirp3-HD-Despina"})
        self.write_script()
        ctx = self.context(dry_run=True)
        self.stage.http_post = mock.Mock(
            side_effect=AssertionError("a dry run must not synthesise"))

        planned = self.stage.plan(ctx)

        self.stage.http_post.assert_not_called()
        self.assertTrue(any("th-TH-Chirp3-HD-Despina" in item["description"]
                            for item in planned), planned)
        target = os.path.join(self.root, "output", "2026-08-11", "audio", "mon.mp3")
        self.assertFalse(os.path.exists(target))

    def test_api_key_mode_rejects_malformed_or_empty_audio_safely(self):
        self.provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        ctx = self.context()
        responses = (
            None,
            {},
            {"audioContent": None},
            {"audioContent": ""},
            {"audioContent": "not-valid-base64!"},
        )
        for index, response in enumerate(responses):
            with self.subTest(response=response):
                out_path = os.path.join(ctx.stage_dir("audio"), "%d.mp3" % index)
                self.stage.http_post = lambda *args, _response=response, **kwargs: _response
                with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}), \
                        self.assertRaises(star_automation.StageFailed) as caught:
                    self.stage._google(ctx, "text", out_path)
                self.assertNotIn(FAKE_GOOGLE_API_KEY, str(caught.exception))
                self.assertFalse(os.path.exists(out_path))

    def test_api_key_transport_error_is_scrubbed_from_exception_and_job_output(self):
        self.provider.configure({"api_key": FAKE_GOOGLE_API_KEY})

        def failing_post(url, payload, headers=None):
            raise RuntimeError("request rejected for " + FAKE_GOOGLE_API_KEY)

        self.stage.http_post = failing_post
        ctx = self.context()
        out_path = os.path.join(ctx.stage_dir("audio"), "failed.mp3")
        with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}), \
                self.assertRaises(star_automation.StageFailed) as caught:
            self.stage._google(ctx, "text", out_path)
        self.assertNotIn(FAKE_GOOGLE_API_KEY, str(caught.exception))
        self.assertIn(star_redact.MASK, str(caught.exception))
        self.assertFalse(os.path.exists(out_path))

        self.write_script()
        original = star_automation.STAGE_ADAPTERS["audio"]
        star_automation.STAGE_ADAPTERS["audio"] = self.stage
        self.addCleanup(star_automation.STAGE_ADAPTERS.__setitem__, "audio", original)
        job = ctx.job
        with mock.patch.dict(os.environ, {"STAR_DISABLE_NETWORK": "0"}):
            done = self.svc.execute_job(job)
        exposed = json.dumps({
            "job": done,
            "events": self.svc.store.list_events(job["id"]),
        })
        self.assertEqual(done["status"], "failed")
        self.assertNotIn(FAKE_GOOGLE_API_KEY, exposed)
        self.assertIn(star_redact.MASK, done["safe_error"])

    def test_api_key_audio_dry_run_never_synthesizes(self):
        self.provider.configure({"api_key": FAKE_GOOGLE_API_KEY})
        calls = []
        self.stage.http_post = lambda *args, **kwargs: calls.append((args, kwargs))
        original = star_automation.STAGE_ADAPTERS["audio"]
        star_automation.STAGE_ADAPTERS["audio"] = self.stage
        self.addCleanup(star_automation.STAGE_ADAPTERS.__setitem__, "audio", original)

        ctx = self.context(dry_run=True)
        done = self.svc.execute_job(ctx.job)

        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"]["provider_calls_made"], 0)
        self.assertEqual(calls, [])
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(done))
        self.assertFalse(os.path.exists(os.path.join(self.root, "output")))


# ── pipeline / runner ─────────────────────────────────────────────────

class RecordingStage(star_automation.StageAdapter):
    """A stage that records what it was asked to do, and can be told to fail."""

    name = "astro"
    label = "recording"

    def __init__(self, behaviour="ok", missing=()):
        self.behaviour = behaviour
        self.missing = list(missing)
        self.executed = 0
        self.planned = 0

    def prerequisites(self, ctx):
        return list(self.missing)

    def plan(self, ctx):
        self.planned += 1
        ctx.plan("recording stage would run")
        return ctx.planned

    def execute(self, ctx):
        self.executed += 1
        ctx.log("recording stage executing")
        if self.behaviour == "fail":
            raise star_automation.StageFailed("stage exploded")
        if self.behaviour == "block":
            raise star_automation.StageBlocked("needs a credential")
        if self.behaviour == "slow":
            for _ in range(200):
                ctx.check_cancelled()
                time.sleep(0.02)
        return {"ok": True}


class ArtifactRecordingStage(RecordingStage):
    """A production-only stage that records one promoted text artifact."""

    label = "artifact recording"

    def execute(self, ctx):
        self.executed += 1
        target = ctx.project_path("output", "2026-08-11", "report.txt")
        artifact = ctx.record("text", target, {
            "date": "2026-08-11",
            "day": "mon",
        })
        return {"recorded": artifact}


class TestPipeline(TempEnv):
    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self._original = dict(star_automation.STAGE_ADAPTERS)
        self.addCleanup(star_automation.STAGE_ADAPTERS.update, self._original)

    def make_job(self, **kw):
        payload = {"from_date": "2026-08-11", "stages": ["astro"]}
        payload.update(kw)
        return self.svc.store.create_job(star_jobs.validate_job_input(payload))

    def test_dry_run_never_calls_execute(self):
        stage = RecordingStage()
        star_automation.STAGE_ADAPTERS["astro"] = stage
        job = self.make_job(dry_run=True)
        done = self.svc.execute_job(job)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(stage.executed, 0, "dry run must not execute a stage")
        self.assertEqual(stage.planned, 1)
        self.assertEqual(done["result"]["provider_calls_made"], 0)
        self.assertTrue(done["result"]["dry_run"])

    def test_context_record_keeps_a_project_relative_artifact_contract(self):
        job = self.make_job(dry_run=False)
        ctx = star_automation.JobContext(
            self.svc, job, self.svc.state.job_dir(job["id"]))
        target = os.path.join(self.root, "content", "scripts", "daily.txt")

        entry = ctx.record("text", target, {
            "date": "2026-08-11",
            "day": "mon",
        })

        self.assertEqual(entry, {
            "kind": "text",
            "path": os.path.join("content", "scripts", "daily.txt"),
            "date": "2026-08-11",
            "day": "mon",
        })
        self.assertEqual(ctx.artifacts, [entry])
        self.assertIs(ctx.artifacts[0], entry)

    def test_production_result_carries_context_record_artifacts(self):
        stage = ArtifactRecordingStage()
        star_automation.STAGE_ADAPTERS["astro"] = stage

        done = self.svc.execute_job(self.make_job(dry_run=False))

        expected = {
            "kind": "text",
            "path": os.path.join("output", "2026-08-11", "report.txt"),
            "date": "2026-08-11",
            "day": "mon",
        }
        self.assertEqual(done["status"], "succeeded")
        self.assertFalse(done["result"]["dry_run"])
        self.assertEqual(done["result"]["artifacts"], [expected])
        self.assertEqual(done["result"]["stages"][0]["result"],
                         {"recorded": expected})

    def test_dry_run_never_executes_or_reports_recorded_artifacts(self):
        stage = ArtifactRecordingStage()
        star_automation.STAGE_ADAPTERS["astro"] = stage

        done = self.svc.execute_job(self.make_job(dry_run=True))

        self.assertEqual(done["status"], "succeeded")
        self.assertTrue(done["result"]["dry_run"])
        self.assertEqual(stage.executed, 0)
        self.assertEqual(done["result"].get("artifacts", []), [])
        self.assertNotIn("report.txt", json.dumps(done["result"]))

    def test_dry_run_writes_nothing_into_the_project_tree(self):
        before = sorted(os.listdir(self.root))
        job = self.make_job(dry_run=True, stages=["astro", "script", "audio",
                                                  "video", "publish"],
                            platforms=["tiktok"])
        done = self.svc.execute_job(job)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_dry_run_reports_unmet_prerequisites_without_blocking(self):
        star_automation.STAGE_ADAPTERS["astro"] = RecordingStage(
            missing=["swisseph is not installed"])
        job = self.make_job(dry_run=True)
        done = self.svc.execute_job(job)
        self.assertEqual(done["status"], "succeeded")
        self.assertIn("astro: swisseph is not installed",
                      done["result"]["unmet_prerequisites"])

    def test_production_run_executes_and_reports_progress(self):
        stage = RecordingStage()
        star_automation.STAGE_ADAPTERS["astro"] = stage
        job = self.make_job(dry_run=False)
        done = self.svc.execute_job(job)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(stage.executed, 1)
        self.assertEqual(done["progress"], 100)
        messages = [e["message"] for e in self.svc.store.list_events(job["id"])]
        self.assertIn("recording stage executing", messages)

    def test_missing_prerequisite_blocks_rather_than_fails(self):
        star_automation.STAGE_ADAPTERS["astro"] = RecordingStage(
            missing=["Cloudflare R2: no R2 credentials stored"])
        job = self.make_job(dry_run=False)
        done = self.svc.execute_job(job)
        self.assertEqual(done["status"], "blocked")
        self.assertIn("no R2 credentials stored", done["safe_error"])

    def test_stage_failure_is_reported_safely(self):
        star_automation.STAGE_ADAPTERS["astro"] = RecordingStage("fail")
        done = self.svc.execute_job(self.make_job(dry_run=False))
        self.assertEqual(done["status"], "failed")
        self.assertIn("stage exploded", done["safe_error"])

    def test_stage_block_at_runtime(self):
        star_automation.STAGE_ADAPTERS["astro"] = RecordingStage("block")
        done = self.svc.execute_job(self.make_job(dry_run=False))
        self.assertEqual(done["status"], "blocked")

    def test_secrets_in_logs_are_redacted(self):
        job = self.make_job(dry_run=True)
        ctx = star_automation.JobContext(self.svc, job, self.svc.state.job_dir(job["id"]))
        ctx.log("calling api with Authorization: Bearer %s" % FAKE_TOKEN)
        messages = [e["message"] for e in self.svc.store.list_events(job["id"])]
        self.assertTrue(any(star_redact.MASK in m for m in messages))
        self.assertFalse(any(FAKE_TOKEN in m for m in messages))

    def test_project_path_refuses_to_escape_the_root(self):
        job = self.make_job()
        ctx = star_automation.JobContext(self.svc, job, self.svc.state.job_dir(job["id"]))
        with self.assertRaises(star_automation.StageFailed):
            ctx.project_path("..", "..", "etc", "passwd")
        self.assertTrue(ctx.project_path("content", "scripts").startswith(self.root))


class TestRunnerThread(TempEnv):
    def setUp(self):
        super().setUp()
        self._original = dict(star_automation.STAGE_ADAPTERS)
        self.addCleanup(star_automation.STAGE_ADAPTERS.update, self._original)

    def test_background_runner_picks_the_job_up(self):
        star_automation.STAGE_ADAPTERS["astro"] = RecordingStage()
        svc = self.service(start_threads=True)
        job = svc.store.create_job(star_jobs.validate_job_input(
            {"from_date": "2026-08-11", "stages": ["astro"], "dry_run": False}))
        self.assertTrue(svc.runner.wait_idle(20))
        self.assertEqual(svc.store.get_job(job["id"])["status"], "succeeded")

    def test_cancel_stops_a_running_job(self):
        star_automation.STAGE_ADAPTERS["astro"] = RecordingStage("slow")
        svc = self.service(start_threads=True)
        job = svc.store.create_job(star_jobs.validate_job_input(
            {"from_date": "2026-08-11", "stages": ["astro"], "dry_run": False}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if svc.store.get_job(job["id"])["status"] == "running":
                break
            time.sleep(0.05)
        self.assertEqual(svc.store.get_job(job["id"])["status"], "running")
        svc.store.request_cancel(job["id"])
        self.assertTrue(svc.runner.wait_idle(20))
        self.assertEqual(svc.store.get_job(job["id"])["status"], "cancelled")


# ── subprocess helper ─────────────────────────────────────────────────

class TestRunCommand(unittest.TestCase):
    def test_runs_argv_and_captures_output(self):
        code, lines = star_automation.run_command(
            [sys.executable, "-c", "print('hello')"], timeout=20)
        self.assertEqual(code, 0)
        self.assertIn("hello", lines)

    def test_output_is_redacted_as_it_streams(self):
        code, lines = star_automation.run_command(
            [sys.executable, "-c", "print('token=%s')" % FAKE_TOKEN], timeout=20)
        self.assertEqual(code, 0)
        self.assertFalse(any(FAKE_TOKEN in line for line in lines))

    def test_metacharacters_are_literal_arguments_not_shell(self):
        # If this ever went through a shell the semicolon would run `echo`.
        code, lines = star_automation.run_command(
            [sys.executable, "-c", "import sys; print(sys.argv[1])",
             "; echo pwned"], timeout=20)
        self.assertEqual(code, 0)
        self.assertIn("; echo pwned", lines)
        self.assertNotIn("pwned", [line.strip() for line in lines])

    def test_cancellation_kills_the_process_group(self):
        started = time.monotonic()
        with self.assertRaises(star_automation.JobCancelled):
            star_automation.run_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=60, is_cancelled=lambda: True)
        self.assertLess(time.monotonic() - started, 20)

    def test_timeout_is_enforced(self):
        import subprocess as sp
        with self.assertRaises(sp.TimeoutExpired):
            star_automation.run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)

    def test_argv_must_be_a_list_of_strings(self):
        for bad in ("echo hi", [], [1, 2]):
            with self.assertRaises(ValueError):
                star_automation.run_command(bad, timeout=5)


class TestVideoCommand(TempEnv):
    def test_ffmpeg_command_is_argv_and_sized_1080x1920(self):
        stage = star_automation.VideoStage()
        argv = stage.build_command("/tmp/a.mp3", "/tmp/a.mp4", "ดวงของชาววันจันทร์",
                                   "/usr/share/fonts/x.ttf")
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("1080x1920", " ".join(argv))
        self.assertNotIn("shell", " ".join(argv))

    def test_drawtext_escapes_filter_metacharacters(self):
        out = star_automation._ffmpeg_escape("a:b'c,d[e]")
        for ch in (":", "'", ",", "[", "]"):
            self.assertIn("\\" + ch, out)

    def test_thai_font_lookup_only_uses_the_allowlist(self):
        font = star_automation.find_thai_font()
        if font is not None:
            self.assertIn(font, star_automation.THAI_FONT_CANDIDATES)


# ── scheduler ─────────────────────────────────────────────────────────

# ── real adapters, exercised locally with no network and no paid call ─

def _has_swisseph():
    try:
        import swisseph  # noqa: F401
    except ImportError:
        return False
    return True


class TestRealAdapters(TempEnv):
    """The two stages that can be verified offline for real.

    astro is pure local computation and video is pure local ffmpeg, so both run
    here against the actual scripts and binaries rather than a mock. script,
    audio and publish are not exercised: they would need a Claude call, a TTS
    call and a live platform respectively.
    """

    def setUp(self):
        super().setUp()
        shutil.copytree(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts"),
            os.path.join(self.root, "scripts"))
        self.svc = self.service()

    @unittest.skipUnless(_has_swisseph(), "swisseph is not installed")
    def test_astro_produces_real_ephemeris_backed_predictions(self):
        job = self.svc.store.create_job(star_jobs.validate_job_input(
            {"from_date": "2026-08-11", "days": ["mon", "fri"], "stages": ["astro"],
             "dry_run": False}))
        done = self.svc.execute_job(job)
        self.assertEqual(done["status"], "succeeded", done["safe_error"])

        raw = os.path.join(self.root, "content", "raw_astro", "2026-08-11.json")
        self.assertTrue(os.path.isfile(raw))
        with open(raw, encoding="utf-8") as fh:
            ephem = json.load(fh)
        # A real Swiss Ephemeris run: the Sun is in Leo in mid-August.
        self.assertIn("planets", ephem)
        self.assertTrue(120.0 <= ephem["planets"]["sun"]["longitude"] < 150.0)

        for day in ("mon", "fri"):
            path = os.path.join(self.root, "content", "horoscope", "2026-08-11",
                                "%s.json" % day)
            self.assertTrue(os.path.isfile(path), path)
            with open(path, encoding="utf-8") as fh:
                pred = json.load(fh)
            self.assertEqual(pred["day"], day)
            self.assertIn("score", pred)
            self.assertIn("aspects", pred)

        # Only the requested days were promoted into the project tree.
        produced = os.listdir(os.path.join(self.root, "content", "horoscope",
                                           "2026-08-11"))
        self.assertEqual(sorted(produced), ["fri.json", "mon.json"])

    @unittest.skipUnless(shutil.which("ffmpeg") and star_automation.find_thai_font(),
                         "ffmpeg or a Thai font is unavailable")
    def test_video_command_renders_a_real_1080x1920_sample(self):
        audio = os.path.join(self.root, "sample.mp3")
        code, _lines = star_automation.run_command(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "anullsrc=r=44100:cl=mono", "-t", "1", "-c:a", "libmp3lame",
             audio, "-y"], timeout=60)
        self.assertEqual(code, 0, "could not build the synthetic audio fixture")

        out = os.path.join(self.root, "sample.mp4")
        argv = star_automation.VideoStage().build_command(
            audio, out, "ดวงของชาววันจันทร์", star_automation.find_thai_font())
        code, lines = star_automation.run_command(argv, timeout=180)
        self.assertEqual(code, 0, " | ".join(lines[-3:]))
        self.assertTrue(os.path.getsize(out) > 1000)

        probe = shutil.which("ffprobe")
        if probe:
            code, lines = star_automation.run_command(
                [probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0", out], timeout=60)
            self.assertEqual(code, 0)
            self.assertIn("1080,1920", " ".join(lines))


class TestScheduler(TempEnv):
    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self.sched = self.svc.scheduler

    def enable(self, **kw):
        config = star_jobs.validate_schedule_input(dict(
            {"enabled": True, "time": "00:00", "stages": ["astro"],
             "dry_run": True}, **kw))
        return self.svc.store.set_schedule(config)

    def now(self, hour=12, minute=0):
        from datetime import datetime
        return datetime(2026, 8, 11, hour, minute, tzinfo=star_automation.BANGKOK)

    def test_disabled_schedule_never_runs(self):
        self.assertIsNone(self.sched.tick(now=self.now()))

    def test_runs_once_and_only_once_per_day(self):
        self.enable()
        job = self.sched.tick(now=self.now())
        self.assertIsNotNone(job)
        self.assertEqual(job["origin"], "schedule")
        self.assertIsNone(self.sched.tick(now=self.now(13)),
                          "a second tick on the same day must not queue another job")

    def test_does_not_run_before_the_configured_time(self):
        self.enable(time="23:30")
        self.assertIsNone(self.sched.tick(now=self.now(23, 29)))
        self.assertIsNotNone(self.sched.tick(now=self.now(23, 30)))

    def test_date_offset_is_applied(self):
        self.enable(date_offset_days=1)
        job = self.sched.tick(now=self.now())
        self.assertEqual(job["input"]["from_date"], "2026-08-12")

    def test_a_busy_queue_skips_the_day_instead_of_stacking_up(self):
        self.svc.store.create_job(star_jobs.validate_job_input(
            {"from_date": "2026-08-11", "stages": ["astro"]}))
        self.enable()
        self.assertIsNone(self.sched.tick(now=self.now()))
        # The claim was released, so the day is not silently burned: once the
        # manual job finishes, a later tick can still run it.
        self.assertTrue(self.svc.store.claim_schedule_run("2026-08-11"))

    def test_a_released_day_is_actually_retried_once_the_queue_drains(self):
        """The end-to-end form of the release bug, through tick() itself.

        Releasing the reservation row was not enough: last_run_date still named
        the released day, so every later tick returned early and the scheduled
        run was lost for the rest of the day even though nothing was wrong any
        more.
        """
        manual = self.svc.store.create_job(star_jobs.validate_job_input(
            {"from_date": "2026-08-11", "stages": ["astro"]}))
        self.enable()
        self.assertIsNone(self.sched.tick(now=self.now(6)))
        self.assertIsNone(self.svc.store.get_schedule()["last_run_date"])

        self.svc.store.finish_job(manual["id"], "succeeded")
        job = self.sched.tick(now=self.now(7))
        self.assertIsNotNone(job, "the released day must run once the queue is free")
        self.assertEqual(job["origin"], "schedule")
        self.assertIsNone(self.sched.tick(now=self.now(8)),
                          "and it still runs only once that day")

    def test_a_schedule_that_no_longer_validates_releases_the_day(self):
        """A stored row that validate_job_input rejects must not burn the day.

        Everything after the claim runs under the release, so a bad row costs a
        log line and a retry rather than the whole day's run.
        """
        self.enable()
        with mock.patch.object(
                star_jobs, "validate_job_input",
                side_effect=star_jobs.JobValidationError("stages must not be empty",
                                                         "stages")):
            self.assertIsNone(self.sched.tick(now=self.now(6)))
        self.assertIsNone(self.svc.store.get_schedule()["last_run_date"])
        self.assertIsNotNone(self.sched.tick(now=self.now(7)),
                             "the next tick must be free to try again")

    def test_an_unexpected_failure_releases_the_day_and_still_raises(self):
        self.enable()
        with mock.patch.object(self.svc.store, "create_job",
                               side_effect=RuntimeError("database is on fire")):
            with self.assertRaises(RuntimeError):
                self.sched.tick(now=self.now(6))
        self.assertIsNone(self.svc.store.get_schedule()["last_run_date"])
        self.assertIsNotNone(self.sched.tick(now=self.now(7)))


if __name__ == "__main__":
    unittest.main()
