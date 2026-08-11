#!/usr/bin/env python3
"""Unit tests for the automation core: redaction, state permissions, the job
store, validation, the pipeline runner, the scheduler and the OAuth flow.

Every test runs against a temp project root and a temp state directory, with
STAR_DISABLE_NETWORK=1 set so any accidental provider call raises instead of
reaching a real API.
"""

import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_automation  # noqa: E402
import star_jobs  # noqa: E402
import star_providers  # noqa: E402
import star_redact  # noqa: E402
import star_state  # noqa: E402

FAKE_TOKEN = "EAAG" + "Zx9Qk4tPl2mNvR7sBd3fH6jU8wY1aC5eT0gI4oL7pS2" + "vX"
FAKE_REFRESH = "1//0gTESTrefreshTOKENvalue123456789"


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


if __name__ == "__main__":
    unittest.main()
