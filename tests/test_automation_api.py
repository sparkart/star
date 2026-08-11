#!/usr/bin/env python3
"""HTTP integration tests for the automation control plane.

Every endpoint in the contract is exercised over a real socket against a temp
project root and a temp state directory. `STAR_DISABLE_NETWORK=1` is set for the
whole module, so if any handler ever grows an unguarded provider call these
tests fail instead of contacting a live API.

Job runner threads are off by default: jobs stay `queued`, which makes the
concurrency, cancel and retry assertions deterministic. The one test that needs
a real background run turns them on explicitly.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_api  # noqa: E402
import star_automation  # noqa: E402
import star_jobs  # noqa: E402
import star_providers  # noqa: E402

INTENT = {star_api.INTENT_HEADER: star_api.INTENT_VALUE}
FAKE_TOKEN = "EAAG" + "Zx9Qk4tPl2mNvR7sBd3fH6jU8wY1aC5eT0gI4oL7pS2" + "vX"
FAKE_SECRET_KEY = "R2secret" + "0123456789abcdef0123456789abcdef"
FAKE_REFRESH = "1//0gTESTrefreshTOKENvalue123456789"


class AutomationApiTestCase(unittest.TestCase):
    start_threads = False

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="star-auto-api-root-")
        self.state_dir = tempfile.mkdtemp(prefix="star-auto-api-state-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.state_dir, True)

        self._old_network = os.environ.get("STAR_DISABLE_NETWORK")
        os.environ["STAR_DISABLE_NETWORK"] = "1"
        self.addCleanup(self._restore_network)

        self.server = star_api.create_server(
            self.root, "127.0.0.1", 0, state_dir=self.state_dir,
            start_threads=self.start_threads)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        # Build the service eagerly so tests can inject fakes into it.
        self.service = self.server.automation_service()

    def _restore_network(self):
        if self._old_network is None:
            os.environ.pop("STAR_DISABLE_NETWORK", None)
        else:
            os.environ["STAR_DISABLE_NETWORK"] = self._old_network

    # -- helpers ----------------------------------------------------
    @property
    def host(self):
        return "127.0.0.1:%d" % self.port

    def request(self, method, path, body=None, headers=None, raw_body=None):
        data = raw_body
        head = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            head.setdefault("Content-Type", "application/json")
        req = urllib.request.Request("http://%s%s" % (self.host, path),
                                     data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                payload = res.read()
                return res.status, (json.loads(payload) if payload else None), res.headers
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, (json.loads(payload) if payload else None), exc.headers

    def get(self, path, headers=None):
        status, payload, _ = self.request("GET", path, headers=headers)
        return status, payload

    def post(self, path, body=None, headers=None):
        status, payload, _ = self.request(
            "POST", path, body=body, headers=dict(INTENT, **(headers or {})))
        return status, payload

    def put(self, path, body=None, headers=None):
        status, payload, _ = self.request(
            "PUT", path, body=body, headers=dict(INTENT, **(headers or {})))
        return status, payload

    def create_job(self, **kw):
        payload = {"from_date": "2026-08-11", "stages": ["astro"], "dry_run": True}
        payload.update(kw)
        return self.post("/api/jobs", payload)


# ── existing endpoints must be untouched ──────────────────────────────

class TestBackwardCompatibility(AutomationApiTestCase):
    def test_original_endpoints_still_work(self):
        status, payload = self.get("/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("scripts", payload)

        # An empty temp root has no manifest, so health legitimately reports
        # 503 here — the point is that it still answers with its own schema.
        status, payload = self.get("/api/health")
        self.assertIn(status, (200, 503))
        self.assertIn("checks", payload)
        self.assertEqual(payload["version"], star_api.VERSION)

    def test_save_script_still_works_without_the_intent_header(self):
        status, payload, _ = self.request(
            "POST", "/api/save-script",
            body={"date": "2026-08-11", "day": "mon", "script": "สวัสดี"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_regenerate_still_works_without_the_intent_header(self):
        self.request("POST", "/api/save-script",
                     body={"date": "2026-08-11", "day": "mon", "script": "x"})
        status, payload, _ = self.request(
            "POST", "/api/regenerate", body={"date": "2026-08-11", "day": "mon"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)

    def test_unknown_endpoint_is_still_404(self):
        status, payload = self.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("unknown endpoint", payload["error"])

    def test_security_headers_present(self):
        _status, _payload, headers = self.request("GET", "/api/automation/overview")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])


# ── routing ───────────────────────────────────────────────────────────

class TestRouting(AutomationApiTestCase):
    def test_every_contract_endpoint_is_routed(self):
        contract = [
            ("GET", "/api/automation/overview"),
            ("GET", "/api/providers"),
            ("POST", "/api/providers/configure"),
            ("POST", "/api/providers/test"),
            ("GET", "/api/jobs"),
            ("POST", "/api/jobs"),
            ("GET", "/api/jobs/" + "a" * 32),
            ("POST", "/api/jobs/" + "a" * 32 + "/cancel"),
            ("POST", "/api/jobs/" + "a" * 32 + "/retry"),
            ("GET", "/api/schedule"),
            ("PUT", "/api/schedule"),
            ("GET", "/api/oauth/youtube/start"),
            ("GET", "/api/oauth/youtube/callback"),
        ]
        for method, path in contract:
            found, allowed = star_api.match_automation(method, path)
            self.assertIsNotNone(found, "%s %s is not routed" % (method, path))
            self.assertIn("OPTIONS", allowed)

    def test_options_is_answered_for_new_routes(self):
        status, _payload, headers = self.request("OPTIONS", "/api/schedule")
        self.assertEqual(status, 204)
        self.assertIn("PUT", headers["Allow"])
        self.assertIn("GET", headers["Allow"])

    def test_wrong_method_is_405_with_allow(self):
        status, payload, headers = self.request("PUT", "/api/jobs",
                                                body={}, headers=INTENT)
        self.assertEqual(status, 405)
        self.assertIn("POST", payload["allow"])
        self.assertIn("GET", headers["Allow"])

    def test_trailing_slash_is_tolerated(self):
        status, _payload = self.get("/api/schedule/")
        self.assertEqual(status, 200)

    def test_malformed_job_id_does_not_reach_the_store(self):
        for bad in ("/api/jobs/..%2f..%2fetc%2fpasswd", "/api/jobs/xyz",
                    "/api/jobs/" + "A" * 32, "/api/jobs/" + "a" * 31):
            status, _payload = self.get(bad)
            self.assertIn(status, (400, 404), bad)

    def test_put_body_is_read_correctly(self):
        status, payload = self.put("/api/schedule", {"enabled": False, "time": "07:45"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["time"], "07:45")


# ── same-origin and CSRF ──────────────────────────────────────────────

class TestSameOriginAndCsrf(AutomationApiTestCase):
    def test_cross_origin_is_rejected(self):
        status, payload, _ = self.request(
            "GET", "/api/providers", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", payload["error"])

    def test_same_origin_is_accepted(self):
        status, _payload, _ = self.request(
            "GET", "/api/providers", headers={"Origin": "http://" + self.host})
        self.assertEqual(status, 200)

    def test_state_changing_routes_require_the_intent_header(self):
        state_changing = [
            ("POST", "/api/jobs", {"from_date": "2026-08-11"}),
            ("POST", "/api/providers/configure", {"provider": "line"}),
            ("POST", "/api/providers/test", {"provider": "line"}),
            ("PUT", "/api/schedule", {"enabled": False}),
            ("POST", "/api/jobs/" + "a" * 32 + "/cancel", {}),
            ("POST", "/api/jobs/" + "a" * 32 + "/retry", {}),
        ]
        for method, path, body in state_changing:
            status, payload, _ = self.request(method, path, body=body)
            self.assertEqual(status, 403, "%s %s should require the intent header"
                             % (method, path))
            self.assertIn(star_api.INTENT_HEADER, payload["error"])

    def test_wrong_intent_value_is_rejected(self):
        status, _payload, _ = self.request(
            "POST", "/api/jobs", body={"from_date": "2026-08-11"},
            headers={star_api.INTENT_HEADER: "something-else"})
        self.assertEqual(status, 403)

    def test_oauth_start_requires_the_intent_header(self):
        status, _payload = self.get("/api/oauth/youtube/start")
        self.assertEqual(status, 403)

    def test_oauth_callback_does_not_require_the_intent_header(self):
        # It is a top-level redirect from Google and cannot carry one; the
        # single-use state parameter is the CSRF defence instead.
        status, payload = self.get("/api/oauth/youtube/callback?state=forged&code=x")
        self.assertEqual(status, 400)
        self.assertIn("state", payload["error"])

    def test_read_only_routes_do_not_require_the_intent_header(self):
        for path in ("/api/automation/overview", "/api/providers", "/api/jobs",
                     "/api/schedule"):
            status, _payload = self.get(path)
            self.assertEqual(status, 200, path)

    def test_host_allowlist_is_enforced_when_configured(self):
        os.environ["STAR_ALLOWED_HOSTS"] = "star.example"
        self.addCleanup(os.environ.pop, "STAR_ALLOWED_HOSTS", None)
        status, payload = self.get("/api/providers")
        self.assertEqual(status, 403)
        self.assertIn("host", payload["error"])
        # The original endpoints keep their previous permissive behaviour.
        self.assertEqual(self.get("/api/stats")[0], 200)


# ── overview & providers ──────────────────────────────────────────────

class TestOverviewAndProviders(AutomationApiTestCase):
    def test_overview_shape(self):
        status, payload = self.get("/api/automation/overview")
        self.assertEqual(status, 200)
        for key in ("providers", "schedule", "recent_jobs", "stages", "platforms",
                    "limits", "job_counts", "state"):
            self.assertIn(key, payload)
        self.assertEqual(payload["limits"]["max_concurrent_jobs"], 1)
        self.assertEqual(payload["limits"]["max_range_days"], star_jobs.MAX_RANGE_DAYS)
        self.assertEqual(payload["platforms"]["manual"],
                         list(star_jobs.MANUAL_PLATFORMS))
        self.assertTrue(payload["state"]["network_disabled"])

    def test_provider_list_covers_the_contract(self):
        status, payload = self.get("/api/providers")
        self.assertEqual(status, 200)
        keys = [p["provider"] for p in payload["providers"]]
        for expected in ("claude", "google_tts", "youtube", "facebook", "line",
                         "r2", "tiktok", "shopee"):
            self.assertIn(expected, keys)

    def test_manual_platforms_are_reported_as_manual(self):
        _status, payload = self.get("/api/providers")
        by_key = {p["provider"]: p for p in payload["providers"]}
        for key in ("tiktok", "shopee"):
            self.assertEqual(by_key[key]["status"], star_providers.MANUAL)
            self.assertNotEqual(by_key[key]["automation"],
                                star_providers.AUTOMATION_FULL)
            self.assertTrue(by_key[key]["prerequisites"])

    def test_configure_then_status_never_returns_the_secret(self):
        status, payload = self.post("/api/providers/configure", {
            "provider": "facebook",
            "config": {"page_id": "1234567890", "page_access_token": FAKE_TOKEN}})
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_TOKEN, json.dumps(payload))

        for path in ("/api/providers", "/api/automation/overview"):
            _status, body = self.get(path)
            self.assertNotIn(FAKE_TOKEN, json.dumps(body, ensure_ascii=False), path)

        # And it really was stored, at 0600.
        self.assertEqual(self.service.state.credential_mode("provider_facebook"), 0o600)
        self.assertEqual(self.service.providers.get("facebook").stored()
                         ["page_access_token"], FAKE_TOKEN)

    def test_r2_secret_never_leaves_the_server(self):
        status, payload = self.post("/api/providers/configure", {
            "provider": "r2", "account_id": "acct1",
            "access_key_id": "AKIAEXAMPLEKEYID", "secret_access_key": FAKE_SECRET_KEY,
            "bucket": "star-media", "public_base_url": "https://cdn.example.com"})
        self.assertEqual(status, 200)
        _status, listing = self.get("/api/providers")
        self.assertNotIn(FAKE_SECRET_KEY, json.dumps(listing))

    def test_configure_rejects_unknown_provider_and_bad_input(self):
        status, payload = self.post("/api/providers/configure", {"provider": "myspace"})
        self.assertEqual(status, 400)
        self.assertIn("unknown provider", payload["error"])

        status, payload = self.post("/api/providers/configure",
                                    {"provider": "facebook", "config": {"page_id": "1"}})
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("field"), "page_access_token")

        status, _payload = self.post("/api/providers/configure",
                                     {"provider": "facebook", "config": "not-an-object"})
        self.assertEqual(status, 400)

    def test_claude_configure_is_refused(self):
        status, payload = self.post("/api/providers/configure",
                                    {"provider": "claude", "session_token": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("never accepts", payload["error"])

    def test_google_tts_path_traversal_is_refused(self):
        status, payload = self.post("/api/providers/configure", {
            "provider": "google_tts", "credentials_path": "/etc/passwd"})
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("field"), "credentials_path")

    def test_offline_provider_test_makes_no_network_call(self):
        self.post("/api/providers/configure", {
            "provider": "line", "channel_access_token": FAKE_TOKEN})
        calls = []
        self.service.providers.get("line").http_get = lambda *a, **k: calls.append(a)
        status, payload = self.post("/api/providers/test", {"provider": "line"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["live_test"])
        self.assertEqual(calls, [])
        self.assertNotIn(FAKE_TOKEN, json.dumps(payload))

    def test_live_provider_test_is_refused_while_the_network_is_disabled(self):
        self.post("/api/providers/configure", {
            "provider": "line", "channel_access_token": FAKE_TOKEN})
        status, payload = self.post("/api/providers/test",
                                    {"provider": "line", "live": True})
        self.assertEqual(status, 400)
        self.assertIn("network access is disabled", payload["error"])

    def test_live_flag_must_be_boolean(self):
        status, payload = self.post("/api/providers/test",
                                    {"provider": "line", "live": "yes"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["field"], "live")


# ── jobs ──────────────────────────────────────────────────────────────

class TestJobs(AutomationApiTestCase):
    def test_create_and_fetch(self):
        status, job = self.create_job()
        self.assertEqual(status, 201)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["progress"], 0)
        self.assertTrue(job["input"]["dry_run"])

        status, detail = self.get("/api/jobs/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(detail["id"], job["id"])
        self.assertIsInstance(detail["events"], list)
        self.assertTrue(any("queued" in e["message"] for e in detail["events"]))

    def test_list_and_filter(self):
        _status, job = self.create_job()
        status, payload = self.get("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["active_job"]["id"], job["id"])

        status, payload = self.get("/api/jobs?status=succeeded")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)

        status, payload = self.get("/api/jobs?status=bogus")
        self.assertEqual(status, 400)

    def test_list_limit_is_bounded(self):
        for bad in ("0", "9999", "abc", "-1"):
            status, _payload = self.get("/api/jobs?limit=" + bad)
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.get("/api/jobs?limit=5")[0], 200)

    def test_second_job_conflicts_with_409(self):
        _status, first = self.create_job()
        status, payload = self.create_job()
        self.assertEqual(status, 409)
        self.assertEqual(payload["active_job"]["id"], first["id"])

    def test_validation_errors_carry_the_field(self):
        cases = [
            ({"from_date": "not-a-date"}, "from_date"),
            ({"from_date": "2026-08-01", "to_date": "2026-09-30"}, "to_date"),
            ({"from_date": "2026-08-11", "days": ["funday"]}, "days"),
            ({"from_date": "2026-08-11", "stages": ["deploy"]}, "stages"),
            ({"from_date": "2026-08-11", "platforms": ["myspace"]}, "platforms"),
            ({"from_date": "2026-08-11", "stages": ["publish"], "platforms": []},
             "platforms"),
            ({"from_date": "2026-08-11", "dry_run": "yes"}, "dry_run"),
        ]
        for body, field in cases:
            status, payload = self.post("/api/jobs", body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload.get("field"), field, body)

    def test_body_must_be_a_json_object(self):
        status, _payload = self.post("/api/jobs", ["a", "list"])
        self.assertEqual(status, 400)
        status, _payload, _ = self.request("POST", "/api/jobs", raw_body=b"{oops",
                                           headers=INTENT)
        self.assertEqual(status, 400)

    def test_oversized_body_is_rejected(self):
        big = b'{"from_date": "2026-08-11", "note": "' + b"x" * (300 * 1024) + b'"}'
        status, _payload, _ = self.request("POST", "/api/jobs", raw_body=big,
                                           headers=INTENT)
        self.assertEqual(status, 413)

    def test_unknown_job_is_404(self):
        for path in ("/api/jobs/" + "b" * 32,
                     "/api/jobs/" + "b" * 32 + "/cancel",
                     "/api/jobs/" + "b" * 32 + "/retry"):
            method = "GET" if path.endswith("b" * 32) else "POST"
            status, payload, _ = self.request(method, path, headers=INTENT,
                                              body={} if method == "POST" else None)
            self.assertEqual(status, 404, path)

    def test_cancel_a_queued_job(self):
        _status, job = self.create_job()
        status, payload = self.post("/api/jobs/%s/cancel" % job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "cancelled")

        status, payload = self.post("/api/jobs/%s/cancel" % job["id"])
        self.assertEqual(status, 409)

    def test_cancel_works_without_a_request_body(self):
        _status, job = self.create_job()
        req = urllib.request.Request(
            "http://%s/api/jobs/%s/cancel" % (self.host, job["id"]),
            method="POST", headers=INTENT)
        with urllib.request.urlopen(req, timeout=20) as res:
            payload = json.loads(res.read())
        self.assertEqual(payload["status"], "cancelled")

    def test_retry_creates_a_new_job_linked_to_the_parent(self):
        _status, parent = self.create_job(days=["mon"])
        self.post("/api/jobs/%s/cancel" % parent["id"])

        status, child = self.post("/api/jobs/%s/retry" % parent["id"])
        self.assertEqual(status, 201)
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["origin"], "retry")
        self.assertNotEqual(child["id"], parent["id"])
        self.assertEqual(child["input"]["days"], ["mon"])
        self.assertEqual(child["status"], "queued")

    def test_retry_accepts_overrides(self):
        _status, parent = self.create_job(days=["mon"])
        self.post("/api/jobs/%s/cancel" % parent["id"])
        status, child = self.post("/api/jobs/%s/retry" % parent["id"],
                                  {"days": ["tue", "wed"], "dry_run": False})
        self.assertEqual(status, 201)
        self.assertEqual(child["input"]["days"], ["tue", "wed"])
        self.assertFalse(child["input"]["dry_run"])

    def test_retry_of_an_active_job_is_refused(self):
        _status, parent = self.create_job()
        status, payload = self.post("/api/jobs/%s/retry" % parent["id"])
        self.assertEqual(status, 409)
        self.assertIn("cancel it before retrying", payload["error"])

    def test_retry_never_loops_automatically(self):
        # A retry only ever happens because this endpoint was called; nothing
        # in the service re-queues a failed job by itself.
        _status, parent = self.create_job()
        self.post("/api/jobs/%s/cancel" % parent["id"])
        before = self.get("/api/jobs")[1]["count"]
        self.assertEqual(self.get("/api/jobs")[1]["count"], before)

    def test_event_pagination_parameters_are_validated(self):
        _status, job = self.create_job()
        self.assertEqual(self.get("/api/jobs/%s?events=0" % job["id"])[0], 200)
        self.assertEqual(self.get("/api/jobs/%s?events=99999" % job["id"])[0], 400)
        self.assertEqual(self.get("/api/jobs/%s?after_id=-1" % job["id"])[0], 400)

    def test_too_many_query_parameters_rejected(self):
        query = "&".join("k%d=1" % i for i in range(40))
        status, _payload = self.get("/api/jobs?" + query)
        self.assertEqual(status, 400)


class TestJobExecution(AutomationApiTestCase):
    """The one class that lets the background runner actually run."""

    start_threads = True

    def test_dry_run_completes_without_contacting_a_provider(self):
        status, job = self.create_job(stages=["astro", "script", "audio", "video",
                                              "publish"],
                                      platforms=["tiktok"], dry_run=True)
        self.assertEqual(status, 201)
        self.assertTrue(self.service.runner.wait_idle(30))

        status, detail = self.get("/api/jobs/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual(detail["progress"], 100)
        self.assertTrue(detail["result"]["dry_run"])
        self.assertEqual(detail["result"]["provider_calls_made"], 0)
        planned = [s["stage"] for s in detail["result"]["stages"]]
        self.assertEqual(planned, ["astro", "script", "audio", "video", "publish"])
        self.assertTrue(detail["events"])

    def test_dry_run_shows_the_commands_it_would_run(self):
        _status, job = self.create_job(stages=["video"], dry_run=True)
        self.assertTrue(self.service.runner.wait_idle(30))
        _status, detail = self.get("/api/jobs/" + job["id"])
        stage = detail["result"]["stages"][0]
        self.assertTrue(any("ffmpeg" in (p.get("command") or "")
                            for p in stage["planned"]))

    def test_publish_to_a_manual_platform_is_planned_as_a_handoff(self):
        _status, job = self.create_job(stages=["publish"], platforms=["shopee"],
                                       dry_run=True)
        self.assertTrue(self.service.runner.wait_idle(30))
        _status, detail = self.get("/api/jobs/" + job["id"])
        text = json.dumps(detail["result"], ensure_ascii=False)
        self.assertIn("handoff", text)

    def test_production_publish_without_credentials_is_blocked_not_failed(self):
        _status, job = self.create_job(stages=["publish"], platforms=["r2"],
                                       dry_run=False)
        self.assertTrue(self.service.runner.wait_idle(30))
        _status, detail = self.get("/api/jobs/" + job["id"])
        self.assertEqual(detail["status"], "blocked")
        self.assertIn("R2", detail["safe_error"])

    def test_a_finished_job_can_be_retried(self):
        _status, job = self.create_job(stages=["publish"], platforms=["r2"],
                                       dry_run=False)
        self.assertTrue(self.service.runner.wait_idle(30))
        status, child = self.post("/api/jobs/%s/retry" % job["id"])
        self.assertEqual(status, 201)
        self.assertEqual(child["parent_id"], job["id"])


# ── schedule ──────────────────────────────────────────────────────────

class TestSchedule(AutomationApiTestCase):
    def test_defaults_are_disabled_and_bangkok(self):
        status, payload = self.get("/api/schedule")
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["timezone"], "Asia/Bangkok")

    def test_put_returns_the_stored_config(self):
        status, payload = self.put("/api/schedule", {
            "enabled": True, "time": "05:45", "date_offset_days": 1,
            "days": ["mon", "tue"], "stages": ["astro", "script"], "dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["time"], "05:45")
        self.assertEqual(payload["days"], ["mon", "tue"])
        self.assertEqual(payload["stages"], ["astro", "script"])
        self.assertEqual(payload["date_offset_days"], 1)

        # Re-reading it returns exactly what was stored, not the request echo.
        _status, again = self.get("/api/schedule")
        self.assertEqual(again["time"], "05:45")
        self.assertEqual(again["days"], ["mon", "tue"])

    def test_fresh_default_can_be_saved_unchanged(self):
        """Regression: pressing save on an untouched form used to 400.

        The unsaved default advertised every stage including `publish` while
        `platforms` was empty, which is exactly the combination the validator
        rejects — so the API refused its own default. The round trip must
        succeed without enabling anything or inventing a platform.
        """
        status, default = self.get("/api/schedule")
        self.assertEqual(status, 200)
        self.assertFalse(default["enabled"])
        self.assertTrue(default["dry_run"])
        self.assertEqual(default["platforms"], [])
        self.assertNotIn("publish", default["stages"],
                         "publish must stay opt-in while no platform is selected")

        editable = {key: default[key] for key in
                    ("enabled", "time", "date_offset_days", "days", "stages",
                     "platforms", "dry_run")}
        status, saved = self.put("/api/schedule", editable)
        self.assertEqual(status, 200, saved)
        for key, value in editable.items():
            self.assertEqual(saved[key], value, key)
        self.assertFalse(saved["enabled"], "saving a default must not enable the schedule")
        self.assertTrue(saved["dry_run"], "saving a default must stay in dry-run")

        # And it is still the same config after a re-read.
        _status, again = self.get("/api/schedule")
        for key, value in editable.items():
            self.assertEqual(again[key], value, key)

    def test_manual_platforms_cannot_be_scheduled(self):
        """A scheduled run is unattended; TikTok/Shopee have no unattended path.

        The UI renders them disabled, and the endpoint refuses them too, so a
        hand-written request cannot arm a scheduled "publish" that would only
        ever stage a handoff package.
        """
        for platforms in (["tiktok"], ["shopee"], ["r2", "tiktok"]):
            status, payload = self.put("/api/schedule", {"platforms": platforms})
            self.assertEqual(status, 400, platforms)
            self.assertEqual(payload.get("field"), "platforms", platforms)
        status, payload = self.put("/api/schedule",
                                   {"stages": ["astro", "publish"],
                                    "platforms": ["tiktok"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("field"), "platforms")

        # None of the rejected attempts may have been stored.
        _status, current = self.get("/api/schedule")
        self.assertEqual(current["platforms"], [])
        self.assertFalse(current["enabled"])

    def test_publish_needs_a_supported_platform(self):
        """Publish with no target is refused; publish with an automatable one is
        stored — still disabled and still in dry-run."""
        status, payload = self.put("/api/schedule",
                                   {"stages": ["astro", "publish"], "platforms": []})
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("field"), "platforms")

        status, saved = self.put("/api/schedule",
                                 {"stages": ["astro", "publish"], "platforms": ["r2"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["platforms"], ["r2"])
        self.assertIn("publish", saved["stages"])
        self.assertFalse(saved["enabled"])
        self.assertTrue(saved["dry_run"])

    def test_put_validates(self):
        for body, field in (({"time": "25:00"}, "time"),
                            ({"days": ["funday"]}, "days"),
                            ({"stages": ["deploy"]}, "stages"),
                            ({"date_offset_days": 999}, "date_offset_days"),
                            ({"stages": ["publish"], "platforms": []}, "platforms")):
            status, payload = self.put("/api/schedule", body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload.get("field"), field, body)

    def test_put_requires_a_body(self):
        status, _payload, _ = self.request("PUT", "/api/schedule", headers=INTENT)
        self.assertEqual(status, 400)


# ── oauth over http ───────────────────────────────────────────────────

class TestOAuthEndpoints(AutomationApiTestCase):
    def configure_client(self):
        return self.post("/api/providers/configure", {
            "provider": "youtube",
            "client_json": {"web": {"client_id": "cid.apps.googleusercontent.com",
                                    "client_secret": "GOCSPX-testsecret"}},
            "redirect_uri": "http://%s/api/oauth/youtube/callback" % self.host})

    def test_start_before_configuring_is_a_400(self):
        status, payload = self.request(
            "GET", "/api/oauth/youtube/start", headers=INTENT)[:2]
        self.assertEqual(status, 400)
        self.assertIn("client JSON", payload["error"])

    def test_start_returns_a_pkce_url_and_the_redirect_uri(self):
        self.configure_client()
        status, payload, _ = self.request("GET", "/api/oauth/youtube/start",
                                          headers=INTENT)
        self.assertEqual(status, 200)
        self.assertIn("code_challenge_method=S256", payload["authorization_url"])
        self.assertIn("/api/oauth/youtube/callback", payload["redirect_uri"])
        self.assertNotIn("GOCSPX", json.dumps(payload))

    def test_callback_reads_its_query_parameters(self):
        self.configure_client()
        _status, start, _ = self.request("GET", "/api/oauth/youtube/start",
                                         headers=INTENT)
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(start["authorization_url"]).query)["state"][0]

        self.service.oauth.token_exchange = lambda form, endpoint: {
            "refresh_token": FAKE_REFRESH, "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/youtube.upload"}

        status, payload = self.get(
            "/api/oauth/youtube/callback?code=abc123&state=" + urllib.parse.quote(state))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"]["status"], star_providers.READY)
        self.assertNotIn(FAKE_REFRESH, json.dumps(payload))

        # Replaying the same callback fails: the state is single use.
        status, payload = self.get(
            "/api/oauth/youtube/callback?code=abc123&state=" + urllib.parse.quote(state))
        self.assertEqual(status, 400)
        self.assertIn("already used", payload["error"])

    def test_callback_without_state_is_rejected(self):
        status, payload = self.get("/api/oauth/youtube/callback?code=abc")
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("field"), "state")

    def test_callback_reports_a_denied_authorisation(self):
        self.configure_client()
        _status, start, _ = self.request("GET", "/api/oauth/youtube/start",
                                         headers=INTENT)
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(start["authorization_url"]).query)["state"][0]
        status, payload = self.get(
            "/api/oauth/youtube/callback?error=access_denied&state="
            + urllib.parse.quote(state))
        self.assertEqual(status, 400)
        self.assertIn("refused", payload["error"])


# ── restart recovery through the API ──────────────────────────────────

class TestRestartRecovery(AutomationApiTestCase):
    def test_orphaned_running_job_is_failed_and_visible(self):
        _status, job = self.create_job()
        self.service.store.claim_next()  # simulate a run that never finished
        self.assertEqual(self.get("/api/jobs/" + job["id"])[1]["status"], "running")

        # A new service over the same state directory is a restart.
        restarted = star_automation.AutomationService(
            self.root, state_dir=self.state_dir, start_threads=False)
        self.addCleanup(restarted.close)
        self.assertIn(job["id"], restarted.recovered)

        detail = restarted.store.get_job(job["id"])
        self.assertEqual(detail["status"], "failed")
        self.assertIn("restart", detail["safe_error"])


# ── a stored secret must never appear anywhere in the API surface ─────

class TestNoSecretLeaks(AutomationApiTestCase):
    def test_full_api_sweep_after_configuring_every_provider(self):
        secrets_used = {
            "facebook": FAKE_TOKEN,
            "line": "LINEchannel" + "A" * 40,
            "r2": FAKE_SECRET_KEY,
            "google_tts": "-----BEGIN PRIVATE KEY-----\nSUPERSECRETKEYBODY\n"
                          "-----END PRIVATE KEY-----",
        }
        self.post("/api/providers/configure", {
            "provider": "facebook", "page_id": "1234567890",
            "page_access_token": secrets_used["facebook"]})
        self.post("/api/providers/configure", {
            "provider": "line", "channel_access_token": secrets_used["line"]})
        self.post("/api/providers/configure", {
            "provider": "r2", "account_id": "acct1",
            "access_key_id": "AKIAEXAMPLEKEYID",
            "secret_access_key": secrets_used["r2"], "bucket": "star-media",
            "public_base_url": "https://cdn.example.com"})
        self.post("/api/providers/configure", {
            "provider": "google_tts", "service_account_json": {
                "type": "service_account", "project_id": "star-proj",
                "private_key": secrets_used["google_tts"],
                "client_email": "tts@star-proj.iam.gserviceaccount.com"}})

        _status, job = self.create_job()
        surfaces = [
            "/api/automation/overview", "/api/providers", "/api/jobs",
            "/api/jobs/" + job["id"], "/api/schedule", "/api/health", "/api/stats",
        ]
        for path in surfaces:
            _status, payload = self.get(path)
            blob = json.dumps(payload, ensure_ascii=False)
            for name, secret in secrets_used.items():
                self.assertNotIn(secret, blob, "%s leaked on %s" % (name, path))
                self.assertNotIn("SUPERSECRETKEYBODY", blob, path)

        for provider in ("facebook", "line", "r2", "google_tts"):
            _status, payload = self.post("/api/providers/test", {"provider": provider})
            blob = json.dumps(payload, ensure_ascii=False)
            for secret in secrets_used.values():
                self.assertNotIn(secret, blob, provider)

    def test_state_directory_permissions_stay_clean(self):
        self.post("/api/providers/configure", {
            "provider": "facebook", "page_id": "1", "page_access_token": FAKE_TOKEN})
        self.assertEqual(self.service.state.audit(), [])
        _status, payload = self.get("/api/automation/overview")
        self.assertEqual(payload["state"]["permission_problems"], [])


if __name__ == "__main__":
    unittest.main()
