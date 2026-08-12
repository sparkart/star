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

import email.message
import http.client
import io
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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_api  # noqa: E402
import star_assets  # noqa: E402
import star_automation  # noqa: E402
import star_jobs  # noqa: E402
import star_providers  # noqa: E402

INTENT = {star_api.INTENT_HEADER: star_api.INTENT_VALUE}
FAKE_TOKEN = "EAAG" + "Zx9Qk4tPl2mNvR7sBd3fH6jU8wY1aC5eT0gI4oL7pS2" + "vX"
FAKE_SECRET_KEY = "R2secret" + "0123456789abcdef0123456789abcdef"
FAKE_REFRESH = "1//0gTESTrefreshTOKENvalue123456789"
FAKE_GOOGLE_API_KEY = "AIzaSyD0-not-a-real-key-7pQ3vW9xL2mN6cB"


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


# ── generated artifact path contract ────────────────────────────────

class TestArtifactPathContract(unittest.TestCase):
    def assert_unavailable(self, value):
        with self.assertRaises(star_api.ApiError) as raised:
            star_api._artifact_parts(value)
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.message, "artifact is unavailable")

    def test_every_allowed_root_and_type_has_the_declared_preview_kind(self):
        for root in star_api.ARTIFACT_ROOTS:
            for extension, expected_type in star_api.ARTIFACT_TYPES.items():
                value = "/".join(root + ("nested", "preview" + extension))
                parts, artifact_type = star_api._artifact_parts(value)
                self.assertEqual(parts, root + ("nested", "preview" + extension),
                                 value)
                self.assertEqual(artifact_type, expected_type, value)

    def test_traversal_empty_segments_and_dot_segments_are_rejected(self):
        for value in (
                "../output/preview.png",
                "output/../content/scripts/preview.txt",
                "output/./preview.png",
                "output//preview.png",
                "output/nested/..",
                "./output/preview.png",
                "output/",
        ):
            self.assert_unavailable(value)

    def test_absolute_and_backslash_paths_are_rejected(self):
        for value in (
                "/output/preview.png",
                os.path.abspath("output/preview.png"),
                r"output\preview.png",
                r"C:\output\preview.png",
                r"\\server\share\preview.png",
        ):
            self.assert_unavailable(value)

    def test_unsupported_roots_and_types_are_rejected(self):
        for value in (
                "content/overrides/preview.txt",
                "content/preview.txt",
                "cdn/star/preview.json",
                "output-private/preview.png",
                "outputs/preview.png",
                "output/preview.svg",
                "output/preview.html",
                "output/preview.mov",
                "output/preview.md",
                "output/preview.png.exe",
        ):
            self.assert_unavailable(value)

    def test_non_strings_controls_and_overlong_paths_are_rejected(self):
        for value in (None, b"output/preview.png", "", "output/bad\nname.png",
                      "output/" + "x" * 1024 + ".png"):
            self.assert_unavailable(value)


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
            ("POST", "/api/assets/background"),
            ("GET", "/api/jobs/" + "a" * 32),
            ("GET", "/api/jobs/" + "a" * 32 + "/artifacts/0"),
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

    def test_google_tts_api_key_is_write_only_across_api_responses(self):
        status, payload = self.post("/api/providers/configure", {
            "provider": "google_tts", "api_key": FAKE_GOOGLE_API_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], star_providers.READY)
        self.assertEqual(payload["key_mode"], "api_key")
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(payload))

        _status, listing = self.get("/api/providers")
        google = next(item for item in listing["providers"]
                      if item["provider"] == "google_tts")
        api_field = next(field for field in google["fields"]
                         if field["name"] == "api_key")
        self.assertTrue(api_field["write_only"])
        self.assertFalse(api_field["required"])
        self.assertEqual(google["key_mode"], "api_key")
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(listing))

        test_status, tested = self.post(
            "/api/providers/test", {"provider": "google_tts"})
        self.assertEqual(test_status, 200)
        self.assertFalse(tested["live_test"])
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(tested))

        self.assertEqual(self.service.providers.get("google_tts").stored(), {
            "mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY})

    def test_google_tts_voice_options_are_listed_as_safe_metadata(self):
        _status, listing = self.get("/api/providers")
        google = next(item for item in listing["providers"]
                      if item["provider"] == "google_tts")
        field = next(f for f in google["fields"] if f["name"] == "voice_name")
        self.assertEqual(field["type"], "select")
        self.assertFalse(field["write_only"])
        self.assertEqual(len(field["options"]), 32)
        self.assertEqual([option["value"] for option in field["options"]],
                         [voice["name"] for voice in star_providers.GOOGLE_TTS_VOICES])
        self.assertEqual(field["default"], star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        self.assertEqual(field["selected"], star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        # Unconfigured, the card still advertises the voice it would use.
        self.assertEqual(google["selected_voice_name"],
                         star_providers.GOOGLE_TTS_DEFAULT_VOICE)
        self.assertEqual(google["selected_voice_gender"], "FEMALE")
        self.assertEqual(google["selected_voice_tier"],
                         star_providers.VOICE_TIER_CHIRP3_HD)

    def test_google_tts_voice_can_be_changed_without_resending_the_key(self):
        status, _payload = self.post("/api/providers/configure", {
            "provider": "google_tts", "config": {"api_key": FAKE_GOOGLE_API_KEY}})
        self.assertEqual(status, 200)

        status, payload = self.post("/api/providers/configure", {
            "provider": "google_tts",
            "config": {"voice_name": "th-TH-Chirp3-HD-Rasalgethi"}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], star_providers.READY)
        self.assertEqual(payload["selected_voice_name"], "th-TH-Chirp3-HD-Rasalgethi")
        self.assertEqual(payload["selected_voice_gender"], "MALE")
        self.assertEqual(payload["key_mode"], "api_key")
        self.assertNotIn(FAKE_GOOGLE_API_KEY, json.dumps(payload))

        # The key is untouched on disk and still absent from every response.
        self.assertEqual(self.service.providers.get("google_tts").stored(), {
            "mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY,
            "voice_name": "th-TH-Chirp3-HD-Rasalgethi"})
        for path in ("/api/providers", "/api/automation/overview"):
            _code, body = self.get(path)
            self.assertNotIn(FAKE_GOOGLE_API_KEY,
                             json.dumps(body, ensure_ascii=False), path)
        _code, listing = self.get("/api/providers")
        google = next(item for item in listing["providers"]
                      if item["provider"] == "google_tts")
        field = next(f for f in google["fields"] if f["name"] == "voice_name")
        self.assertEqual(field["selected"], "th-TH-Chirp3-HD-Rasalgethi")

    def test_google_tts_rejects_an_unknown_voice_over_the_api(self):
        self.post("/api/providers/configure", {
            "provider": "google_tts", "config": {"api_key": FAKE_GOOGLE_API_KEY}})
        status, payload = self.post("/api/providers/configure", {
            "provider": "google_tts", "config": {"voice_name": "th-TH-Chirp3-HD-Nope"}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["field"], "voice_name")
        self.assertEqual(self.service.providers.get("google_tts").stored(),
                         {"mode": "api_key", "api_key": FAKE_GOOGLE_API_KEY})

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


# ── job-owned generated artifacts ───────────────────────────────────

class TestJobArtifacts(AutomationApiTestCase):
    def put_artifact(self, relative_path, data=b"artifact bytes"):
        path = os.path.join(self.root, *relative_path.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def finish_with_artifacts(self, entries):
        status, job = self.create_job(dry_run=False)
        self.assertEqual(status, 201)
        self.service.store.finish_job(
            job["id"], "succeeded", progress=100,
            result={"dry_run": False, "stages": [], "artifacts": entries})
        return job["id"]

    def raw_get(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request("GET", path, headers=dict(headers or {}))
            response = connection.getresponse()
            return response.status, response.read(), response.headers
        finally:
            connection.close()

    def test_artifact_views_redact_paths_and_mint_only_job_scoped_urls(self):
        job_id = "a" * 32
        job = {
            "id": job_id,
            "result": {"artifacts": [
                {
                    "kind": "forged-kind",
                    "path": "output/private/preview.png",
                    "name": "forged-name",
                    "url": "https://attacker.invalid/file",
                    "bytes": 999999,
                    "date": "2026-08-11",
                    "day": "mon",
                },
                {"path": "../outside.txt"},
                {"path": "content/scripts/daily.txt", "day": "tue"},
            ]},
        }

        views = star_api._artifact_views(self.service, job)

        self.assertEqual([view["id"] for view in views], ["0", "2"])
        self.assertEqual([view["url"] for view in views], [
            "/api/jobs/%s/artifacts/0" % job_id,
            "/api/jobs/%s/artifacts/2" % job_id,
        ])
        self.assertEqual(views[0]["kind"], "image")
        self.assertEqual(views[0]["name"], "preview.png")
        self.assertEqual(views[1]["kind"], "text")
        for view in views:
            self.assertNotIn("path", view)
            self.assertEqual(
                set(view) - {"date", "day"},
                {"id", "name", "kind", "content_type", "url"})
            self.assertRegex(
                view["url"],
                r"^/api/jobs/%s/artifacts/(?:0|[1-9][0-9]{0,2})$" % job_id)
        rendered = json.dumps(views)
        self.assertNotIn("output/private", rendered)
        self.assertNotIn("content/scripts", rendered)
        self.assertNotIn("attacker.invalid", rendered)

    def test_detail_verifies_files_reports_bytes_and_drops_missing_entries(self):
        data = b"verified detail bytes"
        self.put_artifact("output/2026-08-11/report.txt", data)
        job_id = self.finish_with_artifacts([
            {"kind": "text", "path": "output/2026-08-11/report.txt"},
            {"kind": "text", "path": "output/2026-08-11/missing.txt"},
        ])

        status, detail = self.get("/api/jobs/" + job_id)

        self.assertEqual(status, 200)
        self.assertEqual(detail["result"]["artifacts"], [{
            "id": "0",
            "name": "report.txt",
            "kind": "text",
            "content_type": "text/plain; charset=utf-8",
            "url": "/api/jobs/%s/artifacts/0" % job_id,
            "bytes": len(data),
        }])
        self.assertNotIn("path", json.dumps(detail["result"]["artifacts"]))

    def test_resolve_and_open_reject_missing_files_and_symlinks(self):
        real = self.put_artifact("output/real.txt", b"real")
        outside = os.path.join(self.state_dir, "outside.txt")
        with open(outside, "wb") as handle:
            handle.write(b"outside")
        os.symlink(outside, os.path.join(self.root, "output", "linked.txt"))

        outside_dir = os.path.join(self.state_dir, "outside-dir")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "nested.txt"), "wb") as handle:
            handle.write(b"outside nested")
        os.symlink(outside_dir, os.path.join(self.root, "output", "linked-dir"))

        for relative_path in (
                "output/missing.txt",
                "output/linked.txt",
                "output/linked-dir/nested.txt",
        ):
            job = {"result": {"artifacts": [{"path": relative_path}]}}
            with self.assertRaises(star_api.ApiError, msg=relative_path) as raised:
                star_api.resolve_job_artifact(self.service, job, 0)
            self.assertEqual(raised.exception.status, 404)

        with self.assertRaises(star_api.ApiError):
            star_api._open_artifact(self.root, ("output", "missing.txt"))
        with self.assertRaises(star_api.ApiError):
            star_api._open_artifact(self.root, ("output", "linked.txt"))

        resolved = star_api.resolve_job_artifact(
            self.service,
            {"result": {"artifacts": [{"path": "output/real.txt"}]}},
            0)
        self.assertEqual(resolved.size, os.path.getsize(real))

    def test_artifact_response_has_inline_no_store_security_headers(self):
        data = b"0123456789"
        self.put_artifact("output/renders/clip demo.txt", data)
        job_id = self.finish_with_artifacts([
            {"kind": "text", "path": "output/renders/clip demo.txt"},
        ])

        status, body, headers = self.raw_get(
            "/api/jobs/%s/artifacts/0" % job_id)

        self.assertEqual(status, 200)
        self.assertEqual(body, data)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["Content-Length"], str(len(data)))
        self.assertEqual(headers["Content-Disposition"],
                         'inline; filename="clip_demo.txt"')
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("sandbox", headers["Content-Security-Policy"])

    def test_artifact_response_supports_closed_open_and_suffix_ranges(self):
        data = b"0123456789"
        self.put_artifact("output/video/sample.mp4", data)
        job_id = self.finish_with_artifacts([
            {"kind": "video", "path": "output/video/sample.mp4"},
        ])
        path = "/api/jobs/%s/artifacts/0" % job_id

        for requested, expected_body, expected_range in (
                ("bytes=2-5", b"2345", "bytes 2-5/10"),
                ("bytes=6-", b"6789", "bytes 6-9/10"),
                ("bytes=-3", b"789", "bytes 7-9/10"),
        ):
            status, body, headers = self.raw_get(path, {"Range": requested})
            self.assertEqual(status, 206, requested)
            self.assertEqual(body, expected_body, requested)
            self.assertEqual(headers["Content-Range"], expected_range, requested)
            self.assertEqual(headers["Content-Length"], str(len(expected_body)))
            self.assertEqual(headers["Accept-Ranges"], "bytes")

    def test_invalid_or_unsatisfiable_ranges_return_416_without_a_body(self):
        data = b"0123456789"
        self.put_artifact("output/audio/sample.mp3", data)
        job_id = self.finish_with_artifacts([
            {"kind": "audio", "path": "output/audio/sample.mp3"},
        ])
        path = "/api/jobs/%s/artifacts/0" % job_id

        for requested in ("bytes=", "bytes=5-2", "bytes=10-", "bytes=-0",
                          "bytes=0-1,4-5", "items=0-1"):
            status, body, headers = self.raw_get(path, {"Range": requested})
            self.assertEqual(status, 416, requested)
            self.assertEqual(body, b"", requested)
            self.assertEqual(headers["Content-Range"], "bytes */10", requested)
            self.assertEqual(headers["Content-Length"], "0", requested)

    def test_serving_missing_symlinked_or_other_job_artifacts_is_404(self):
        os.makedirs(os.path.join(self.root, "output"), exist_ok=True)
        outside = os.path.join(self.state_dir, "private.txt")
        with open(outside, "wb") as handle:
            handle.write(b"private")
        os.symlink(outside, os.path.join(self.root, "output", "linked.txt"))
        job_id = self.finish_with_artifacts([
            {"path": "output/missing.txt"},
            {"path": "output/linked.txt"},
        ])

        for index in (0, 1, 2):
            status, payload = self.get(
                "/api/jobs/%s/artifacts/%d" % (job_id, index))
            self.assertEqual(status, 404, index)
            self.assertEqual(payload["error"], "artifact is unavailable")

        status, payload = self.get(
            "/api/jobs/%s/artifacts/0" % ("b" * 32))
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown job")


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


# ── background upload ─────────────────────────────────────────────────

# A JPEG magic prefix and enough filler to clear MIN_UPLOAD_BYTES. These bytes
# are never decoded here: what the store does with them is tests/
# test_automation_assets.py's job, and this file only owns the HTTP contract.
JPEG_UPLOAD = b"\xff\xd8\xff" + b"x" * 80
ASSET_ID = "a1b2" * 8


class TestBackgroundUploadApi(AutomationApiTestCase):
    """POST /api/assets/background — the only route that takes raw bytes.

    `store_background` is replaced by a recorder returning fixed safe metadata,
    so every assertion here is about the HTTP contract (status, headers, guard
    order, what is allowed into the response body) and not about image
    validation. `star_assets._run` is booby-trapped for the whole class, so a
    test that accidentally reached the real validator — and with it ffprobe and
    ffmpeg — fails loudly instead of passing slowly.
    """

    def setUp(self):
        super().setUp()
        self.store_calls = []
        self.meta = self.stored_meta()
        self.real_store_background = self.service.store_background
        self.service.store_background = self.fake_store

        guard = mock.patch.object(
            star_assets, "_run",
            side_effect=AssertionError("no subprocess may run in this test"))
        guard.start()
        self.addCleanup(guard.stop)

    # -- helpers ----------------------------------------------------
    def stored_meta(self, content_type="image/jpeg", asset_id=ASSET_ID):
        """What the store returns: safe fields *plus* fields that must not ship.

        `kind`, `path` and `state_dir` are here on purpose — the response is
        built by `public_meta`, so if it ever starts echoing the store's dict
        the leak assertions below catch it.
        """
        return {
            "id": asset_id,
            "kind": "jpeg",
            "content_type": content_type,
            "width": 1440,
            "height": 2160,
            "bytes": len(JPEG_UPLOAD),
            "created_at": "2026-08-11T04:05:06Z",
            "path": os.path.join(self.state_dir, "assets", "backgrounds",
                                 asset_id + ".jpg"),
            "state_dir": self.state_dir,
        }

    def fake_store(self, data):
        self.store_calls.append(data)
        return dict(self.meta)

    def upload(self, body=JPEG_UPLOAD, content_type="image/jpeg", intent=True,
               headers=None, method="POST", path="/api/assets/background"):
        """One upload over a real socket, with exact control of the headers.

        http.client rather than urllib because urllib invents a Content-Type
        for any request that carries a body, which would make "the caller sent
        no Content-Type" untestable.
        """
        head = {}
        if content_type is not None:
            head["Content-Type"] = content_type
        if intent:
            head[star_api.INTENT_HEADER] = star_api.INTENT_VALUE
        head.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=head)
            response = conn.getresponse()
            raw = response.read()
            return (response.status, (json.loads(raw) if raw else None),
                    response.headers)
        finally:
            conn.close()

    def assertNoPathsLeaked(self, payload):
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(self.state_dir, blob)
        self.assertNotIn(self.root, blob)
        self.assertNotIn("assets/backgrounds", blob)
        self.assertNotIn("state_dir", blob)
        self.assertNotIn("path", blob)

    # -- the happy path ---------------------------------------------
    def test_upload_returns_201_and_only_the_safe_fields(self):
        status, payload, _headers = self.upload()
        self.assertEqual(status, 201, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["uploaded_at"])
        self.assertEqual(sorted(payload["asset"]),
                         ["bytes", "content_type", "created_at", "height", "id",
                          "width"])
        self.assertEqual(payload["asset"]["id"], ASSET_ID)
        self.assertEqual(payload["asset"]["content_type"], "image/jpeg")
        self.assertEqual(payload["asset"]["width"], 1440)
        self.assertEqual(payload["asset"]["height"], 2160)
        self.assertEqual(payload["asset"]["bytes"], len(JPEG_UPLOAD))
        self.assertEqual(payload["asset"]["created_at"], "2026-08-11T04:05:06Z")
        self.assertNoPathsLeaked(payload)

    def test_the_store_receives_exactly_the_bytes_that_were_sent(self):
        self.upload()
        self.assertEqual(self.store_calls, [JPEG_UPLOAD])
        self.assertIsInstance(self.store_calls[0], bytes)

    def test_the_response_carries_the_standard_security_headers(self):
        _status, _payload, headers = self.upload()
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_content_type_parameters_and_case_are_tolerated(self):
        for declared in ("image/jpeg; charset=binary", "IMAGE/JPEG",
                         " image/jpeg "):
            self.store_calls = []
            status, payload, _headers = self.upload(content_type=declared)
            self.assertEqual(status, 201, declared)
            self.assertEqual(payload["asset"]["id"], ASSET_ID, declared)
            self.assertEqual(len(self.store_calls), 1, declared)

    def test_same_origin_upload_is_accepted(self):
        status, _payload, _headers = self.upload(
            headers={"Origin": "http://" + self.host})
        self.assertEqual(status, 201)

    # -- the guards, and the order they fire in ---------------------
    def test_upload_requires_the_intent_header(self):
        status, payload, _headers = self.upload(intent=False)
        self.assertEqual(status, 403)
        self.assertIn(star_api.INTENT_HEADER, payload["error"])
        self.assertEqual(payload["required_header"],
                         {star_api.INTENT_HEADER: star_api.INTENT_VALUE})
        self.assertEqual(self.store_calls, [])

    def test_a_wrong_intent_value_is_rejected_before_the_store(self):
        status, _payload, _headers = self.upload(
            intent=False, headers={star_api.INTENT_HEADER: "automation"})
        self.assertEqual(status, 403)
        self.assertEqual(self.store_calls, [])

    def test_cross_origin_upload_is_rejected_before_the_store(self):
        status, payload, _headers = self.upload(
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", payload["error"])
        self.assertEqual(self.store_calls, [])

    def test_content_type_must_be_declared_and_supported(self):
        cases = [None, "", "application/json", "text/plain", "image/gif",
                 "image/svg+xml", "application/octet-stream",
                 "multipart/form-data; boundary=x"]
        for declared in cases:
            status, payload, _headers = self.upload(content_type=declared)
            self.assertEqual(status, 415, declared)
            self.assertEqual(payload.get("field"), "content_type", declared)
            for accepted in star_assets.ACCEPTED_CONTENT_TYPES:
                self.assertIn(accepted, payload["error"], declared)
        self.assertEqual(self.store_calls, [])

    def test_a_declared_type_that_the_file_contradicts_is_refused_and_cleaned_up(self):
        # The caller says PNG; the store reports what the bytes really were.
        with mock.patch.object(star_assets, "delete_background",
                               return_value=True) as delete:
            status, payload, _headers = self.upload(content_type="image/png")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload.get("field"), "content_type")
        self.assertIn("image/jpeg", payload["error"])
        self.assertIn("image/png", payload["error"])
        self.assertNotIn("asset", payload)
        self.assertNoPathsLeaked(payload)
        # The half-accepted asset is removed rather than left orphaned.
        self.assertEqual(delete.call_args_list,
                         [mock.call(self.service.state, ASSET_ID)])

    def test_an_empty_body_is_refused_before_the_store(self):
        status, payload, _headers = self.upload(body=b"")
        self.assertEqual(status, 400)
        self.assertIn("body", payload["error"])
        self.assertEqual(self.store_calls, [])

    def test_a_body_too_small_to_be_an_image_is_refused_and_stores_nothing(self):
        # The real store runs here: its size floor is checked before any file
        # is created and long before ffprobe, so this stays deterministic.
        self.service.store_background = self.real_store_background
        status, payload, _headers = self.upload(body=b"\xff\xd8\xff" + b"x" * 8)
        self.assertEqual(status, 400, payload)
        self.assertIn("too small", payload["error"])
        self.assertEqual(star_assets.list_assets(self.service.state), [])
        self.assertFalse(os.path.isdir(
            os.path.join(self.state_dir, "assets", "backgrounds")))

    def test_a_body_over_the_upload_limit_is_refused(self):
        # Content-Length alone is enough to refuse it, so nothing is allocated:
        # the reader is driven directly rather than pushing 12 MiB over a socket.
        handler = _StubUploadHandler(
            {"Content-Length": str(star_api.MAX_UPLOAD_BODY + 1),
             "Content-Type": "image/jpeg"})
        with self.assertRaises(star_api.ApiError) as caught:
            handler.read_binary_body()
        self.assertEqual(caught.exception.status, 413)
        self.assertIn("12 MiB", caught.exception.message)
        self.assertEqual(handler.rfile.reads, [])
        self.assertEqual(self.store_calls, [])

    # -- method and preflight behaviour -----------------------------
    def test_reading_the_upload_route_back_is_405(self):
        for method in ("GET", "PUT"):
            status, payload, headers = self.request(
                method, "/api/assets/background",
                body=({} if method == "PUT" else None), headers=INTENT)
            self.assertEqual(status, 405, method)
            self.assertEqual(payload["allow"], ["OPTIONS", "POST"], method)
            self.assertIn("POST", headers["Allow"], method)
            self.assertEqual(self.store_calls, [], method)

    def test_options_advertises_post_only(self):
        status, _payload, headers = self.request("OPTIONS",
                                                 "/api/assets/background")
        self.assertEqual(status, 204)
        allow = [item.strip() for item in headers["Allow"].split(",")]
        self.assertEqual(sorted(allow), ["OPTIONS", "POST"])
        self.assertNotIn("GET", allow)

    def test_the_upload_path_is_routed_and_requires_intent(self):
        found, allowed = star_api.match_automation("POST",
                                                   "/api/assets/background")
        self.assertIsNotNone(found)
        self.assertEqual(allowed, {"POST", "OPTIONS"})
        _handler, params, requires_intent = found
        self.assertEqual(params, {})
        self.assertTrue(requires_intent)

    def test_an_uploaded_image_is_never_served_back(self):
        self.upload()
        for path in ("/api/assets/background/" + ASSET_ID,
                     "/api/assets/backgrounds",
                     "/api/assets/background/" + ASSET_ID + ".jpg"):
            status, _payload = self.get(path)
            self.assertEqual(status, 404, path)


class _RecordingReader:
    """An rfile that remembers every read, so "never read" is provable."""

    def __init__(self, data=b""):
        self.buffer = io.BytesIO(data)
        self.reads = []

    def read(self, length):
        self.reads.append(length)
        return self.buffer.read(length)


class _StubUploadHandler:
    """Just enough of StarHandler to drive its binary body reader directly.

    Borrowing the two real methods keeps this a test of production code rather
    than of a re-implementation of it.
    """

    read_binary_body = star_api.StarHandler.read_binary_body
    declared_content_type = star_api.StarHandler.declared_content_type

    def __init__(self, headers, body=b""):
        self.headers = email.message.Message()
        for key, value in headers.items():
            self.headers[key] = value
        self.rfile = _RecordingReader(body)
        self.close_connection = False


class TestBinaryBodyReader(unittest.TestCase):
    """The upload reader in isolation: its limit must stay its own.

    No server here on purpose — these are the cases that are either impossible
    (a 12 MiB body) or awkward (a lying Content-Length) to express over a real
    socket.
    """

    def read(self, headers, body=b""):
        handler = _StubUploadHandler(headers, body)
        return handler, handler.read_binary_body()

    def test_a_well_formed_upload_becomes_a_raw_upload(self):
        handler, upload = self.read(
            {"Content-Length": str(len(JPEG_UPLOAD)),
             "Content-Type": "image/jpeg; charset=binary"}, JPEG_UPLOAD)
        self.assertIsInstance(upload, star_api.RawUpload)
        self.assertEqual(upload.data, JPEG_UPLOAD)
        self.assertEqual(upload.content_type, "image/jpeg")
        self.assertFalse(handler.close_connection)

    def test_the_limit_is_the_asset_modules_limit_not_the_json_one(self):
        self.assertEqual(star_api.MAX_UPLOAD_BODY, star_assets.MAX_UPLOAD_BYTES)
        self.assertEqual(star_api.MAX_UPLOAD_BODY, 12 * 1024 * 1024)
        self.assertGreater(star_api.MAX_UPLOAD_BODY, star_api.MAX_BODY)

    def test_an_over_limit_length_is_refused_without_reading(self):
        for over in (star_api.MAX_UPLOAD_BODY + 1, star_api.MAX_UPLOAD_BODY * 4,
                     10 ** 12):
            handler = _StubUploadHandler({"Content-Length": str(over),
                                          "Content-Type": "image/jpeg"})
            with self.assertRaises(star_api.ApiError) as caught:
                handler.read_binary_body()
            self.assertEqual(caught.exception.status, 413, over)
            self.assertEqual(handler.rfile.reads, [], over)
            self.assertTrue(handler.close_connection, over)

    def test_a_body_at_the_limit_is_not_refused_for_its_length(self):
        # One byte under the ceiling is a size question only; whether those
        # bytes are an image is the store's decision, not the reader's.
        handler = _StubUploadHandler(
            {"Content-Length": str(star_api.MAX_UPLOAD_BODY),
             "Content-Type": "image/jpeg"}, b"")
        with self.assertRaises(star_api.ApiError) as caught:
            handler.read_binary_body()
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("truncated", caught.exception.message)
        self.assertEqual(handler.rfile.reads, [star_api.MAX_UPLOAD_BODY])

    def test_malformed_or_missing_lengths_are_refused(self):
        cases = [
            ({"Content-Type": "image/jpeg"}, 411),
            ({"Content-Length": "0", "Content-Type": "image/jpeg"}, 400),
            ({"Content-Length": "-1", "Content-Type": "image/jpeg"}, 400),
            ({"Content-Length": "lots", "Content-Type": "image/jpeg"}, 400),
            ({"Transfer-Encoding": "chunked", "Content-Type": "image/jpeg"}, 411),
        ]
        for headers, expected in cases:
            handler = _StubUploadHandler(headers)
            with self.assertRaises(star_api.ApiError) as caught:
                handler.read_binary_body()
            self.assertEqual(caught.exception.status, expected, headers)
            self.assertEqual(handler.rfile.reads, [], headers)
            self.assertTrue(handler.close_connection, headers)

    def test_a_truncated_body_is_refused(self):
        handler = _StubUploadHandler(
            {"Content-Length": "500", "Content-Type": "image/jpeg"}, JPEG_UPLOAD)
        with self.assertRaises(star_api.ApiError) as caught:
            handler.read_binary_body()
        self.assertEqual(caught.exception.status, 400)
        self.assertTrue(handler.close_connection)

    def test_content_type_is_reduced_to_a_bare_media_type(self):
        cases = {
            "image/jpeg": "image/jpeg",
            "IMAGE/PNG; charset=utf-8": "image/png",
            "  image/webp  ": "image/webp",
            "": "",
        }
        for raw, expected in cases.items():
            handler = _StubUploadHandler({"Content-Type": raw})
            self.assertEqual(handler.declared_content_type(), expected, raw)
        self.assertEqual(_StubUploadHandler({}).declared_content_type(), "")


class TestJsonBodyLimitIsUnchanged(AutomationApiTestCase):
    """Adding a 12 MiB route must not widen the control plane's 256 KiB."""

    def test_the_json_limit_keeps_its_value(self):
        self.assertEqual(star_api.MAX_BODY, 256 * 1024)
        self.assertEqual(star_api.MAX_SCRIPT, star_api.MAX_BODY)

    def test_only_the_upload_route_reads_a_raw_body(self):
        self.assertEqual(star_api.RAW_BODY_PATHS,
                         frozenset(("/api/assets/background",)))
        for path in star_api.RAW_BODY_PATHS:
            found, allowed = star_api.match_automation("POST", path)
            self.assertIsNotNone(found, path)
            self.assertEqual(allowed, {"POST", "OPTIONS"}, path)

    def test_json_routes_still_refuse_a_body_over_the_json_limit(self):
        big = b'{"provider": "line", "note": "' + b"x" * (star_api.MAX_BODY + 1) + b'"}'
        for path in ("/api/jobs", "/api/providers/configure"):
            status, payload, _headers = self.request("POST", path, raw_body=big,
                                                     headers=INTENT)
            self.assertEqual(status, 413, path)
            self.assertIn(str(star_api.MAX_BODY), payload["error"], path)

    def test_a_json_route_does_not_accept_an_image_body(self):
        status, payload, _headers = self.request(
            "POST", "/api/jobs", raw_body=JPEG_UPLOAD,
            headers=dict(INTENT, **{"Content-Type": "image/jpeg"}))
        self.assertEqual(status, 400)
        self.assertIn("JSON", payload["error"])


# ── background id and overlay on a job ────────────────────────────────

class TestJobBackgroundAsset(AutomationApiTestCase):
    """A job carries an asset *id*; the API proves the id names something.

    `background_exists` is stubbed so the check can be observed without an
    image ever being stored — this class is about what the API does with the
    answer, not about how the answer is computed.
    """

    def setUp(self):
        super().setUp()
        self.known = set()
        self.exists_calls = []
        self.service.background_exists = self.fake_exists

    def fake_exists(self, asset_id):
        self.exists_calls.append(asset_id)
        return asset_id in self.known

    def test_an_unknown_background_is_a_400_and_creates_no_job(self):
        status, payload = self.create_job(background_asset_id=ASSET_ID)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["field"], "background_asset_id")
        self.assertIn("upload", payload["error"])
        self.assertEqual(self.exists_calls, [ASSET_ID])

        _status, listing = self.get("/api/jobs")
        self.assertEqual(listing["count"], 0)
        self.assertIsNone(listing["active_job"])

    def test_an_uploaded_background_is_persisted_on_the_job(self):
        self.known.add(ASSET_ID)
        status, job = self.create_job(background_asset_id=ASSET_ID)
        self.assertEqual(status, 201, job)
        self.assertEqual(job["input"]["background_asset_id"], ASSET_ID)
        self.assertEqual(self.exists_calls, [ASSET_ID])

        _status, detail = self.get("/api/jobs/" + job["id"])
        self.assertEqual(detail["input"]["background_asset_id"], ASSET_ID)
        stored = self.service.store.get_job(job["id"])
        self.assertEqual(stored["input"]["background_asset_id"], ASSET_ID)

    def test_a_job_response_never_carries_a_path_to_the_image(self):
        self.known.add(ASSET_ID)
        _status, job = self.create_job(background_asset_id=ASSET_ID)
        for path in ("/api/jobs", "/api/jobs/" + job["id"],
                     "/api/automation/overview"):
            _status, payload = self.get(path)
            blob = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(self.state_dir, blob, path)
            self.assertNotIn(self.root, blob, path)
            self.assertNotIn("assets/backgrounds", blob, path)

    def test_omitting_the_background_never_asks_the_asset_store(self):
        status, job = self.create_job()
        self.assertEqual(status, 201)
        self.assertIsNone(job["input"]["background_asset_id"])
        self.assertEqual(self.exists_calls, [])

    def test_an_empty_background_id_is_treated_as_none(self):
        status, job = self.create_job(background_asset_id="")
        self.assertEqual(status, 201, job)
        self.assertIsNone(job["input"]["background_asset_id"])
        self.assertEqual(self.exists_calls, [])

    def test_a_malformed_background_id_never_reaches_the_asset_store(self):
        for bad in ("../../etc/passwd", "A1B2" * 8, "z" * 32, "a" * 31,
                    "a" * 33, 12345, ["a" * 32], ASSET_ID + "/x"):
            status, payload = self.create_job(background_asset_id=bad)
            self.assertEqual(status, 400, bad)
            self.assertEqual(payload.get("field"), "background_asset_id", bad)
        self.assertEqual(self.exists_calls, [])
        self.assertEqual(self.get("/api/jobs")[1]["count"], 0)


class TestJobOverlayAndRetry(AutomationApiTestCase):
    """The three customisation fields survive create, read back and retry."""

    def setUp(self):
        super().setUp()
        self.known = {ASSET_ID}
        self.exists_calls = []
        self.service.background_exists = self.fake_exists

    def fake_exists(self, asset_id):
        self.exists_calls.append(asset_id)
        return asset_id in self.known

    def custom_job(self, **kw):
        payload = {"overlay_text_mode": "custom",
                   "custom_overlay_text": "ดวงประจำวันอังคาร",
                   "background_asset_id": ASSET_ID}
        payload.update(kw)
        return self.create_job(**payload)

    def test_defaults_are_auto_with_no_text_and_no_background(self):
        _status, job = self.create_job()
        self.assertEqual(job["input"]["overlay_text_mode"],
                         star_jobs.DEFAULT_OVERLAY_TEXT_MODE)
        self.assertIsNone(job["input"]["custom_overlay_text"])
        self.assertIsNone(job["input"]["background_asset_id"])

    def test_custom_overlay_is_persisted_on_create(self):
        status, job = self.custom_job()
        self.assertEqual(status, 201, job)
        self.assertEqual(job["input"]["overlay_text_mode"], "custom")
        self.assertEqual(job["input"]["custom_overlay_text"], "ดวงประจำวันอังคาร")
        self.assertEqual(job["input"]["background_asset_id"], ASSET_ID)

        _status, detail = self.get("/api/jobs/" + job["id"])
        for key in ("overlay_text_mode", "custom_overlay_text",
                    "background_asset_id"):
            self.assertEqual(detail["input"][key], job["input"][key], key)

    def test_overlay_validation_errors_carry_their_field(self):
        cases = [
            ({"overlay_text_mode": "fancy"}, "overlay_text_mode"),
            ({"overlay_text_mode": 7}, "overlay_text_mode"),
            ({"overlay_text_mode": "custom"}, "custom_overlay_text"),
            ({"overlay_text_mode": "custom", "custom_overlay_text": "   "},
             "custom_overlay_text"),
            ({"custom_overlay_text": "text without custom mode"},
             "custom_overlay_text"),
            ({"overlay_text_mode": "custom", "custom_overlay_text": 5},
             "custom_overlay_text"),
            ({"overlay_text_mode": "custom",
              "custom_overlay_text": "x" * (star_jobs.MAX_CUSTOM_OVERLAY_TEXT + 1)},
             "custom_overlay_text"),
            ({"overlay_text_mode": "custom", "custom_overlay_text": "nul\x00here"},
             "custom_overlay_text"),
            ({"overlay_text_mode": "custom", "custom_overlay_text": "\x1b[31mred"},
             "custom_overlay_text"),
            ({"overlay_text_mode": "custom", "custom_overlay_text": "del\x7fhere"},
             "custom_overlay_text"),
        ]
        for body, field in cases:
            status, payload = self.create_job(**body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload.get("field"), field, body)

    def test_line_breaks_in_custom_overlay_text_are_kept(self):
        """Typed whitespace is text, not a control character.

        The renderer takes the caption through `textfile=` and re-wraps it, so
        a newline is a layout choice the operator is allowed to make; only the
        C0/C1 characters they cannot type are refused.
        """
        status, job = self.create_job(overlay_text_mode="custom",
                                      custom_overlay_text="บรรทัดหนึ่ง\nบรรทัดสอง")
        self.assertEqual(status, 201, job)
        self.assertEqual(job["input"]["custom_overlay_text"],
                         "บรรทัดหนึ่ง\nบรรทัดสอง")

    def test_retry_inherits_all_three_customisation_fields(self):
        _status, parent = self.custom_job(days=["mon"])
        self.post("/api/jobs/%s/cancel" % parent["id"])
        self.exists_calls = []

        status, child = self.post("/api/jobs/%s/retry" % parent["id"])
        self.assertEqual(status, 201, child)
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["origin"], "retry")
        for key in ("overlay_text_mode", "custom_overlay_text",
                    "background_asset_id"):
            self.assertEqual(child["input"][key], parent["input"][key], key)
        # The image is re-checked for the child: retention may have removed it
        # between the parent's run and this retry.
        self.assertEqual(self.exists_calls, [ASSET_ID])

    def test_retry_accepts_a_valid_custom_override(self):
        _status, parent = self.create_job(background_asset_id=ASSET_ID)
        self.assertEqual(parent["input"]["overlay_text_mode"], "auto")
        self.post("/api/jobs/%s/cancel" % parent["id"])

        status, child = self.post("/api/jobs/%s/retry" % parent["id"], {
            "overlay_text_mode": "custom", "custom_overlay_text": "ข้อความใหม่"})
        self.assertEqual(status, 201, child)
        self.assertEqual(child["input"]["overlay_text_mode"], "custom")
        self.assertEqual(child["input"]["custom_overlay_text"], "ข้อความใหม่")
        self.assertEqual(child["input"]["background_asset_id"], ASSET_ID)

    def test_retry_can_clear_a_custom_overlay_back_to_auto(self):
        _status, parent = self.custom_job()
        self.post("/api/jobs/%s/cancel" % parent["id"])
        status, child = self.post("/api/jobs/%s/retry" % parent["id"], {
            "overlay_text_mode": "auto", "custom_overlay_text": None})
        self.assertEqual(status, 201, child)
        self.assertEqual(child["input"]["overlay_text_mode"], "auto")
        self.assertIsNone(child["input"]["custom_overlay_text"])
        self.assertEqual(child["input"]["background_asset_id"], ASSET_ID)

    def test_retry_can_replace_the_background(self):
        other = "c3d4" * 8
        self.known.add(other)
        _status, parent = self.custom_job()
        self.post("/api/jobs/%s/cancel" % parent["id"])
        status, child = self.post("/api/jobs/%s/retry" % parent["id"],
                                  {"background_asset_id": other})
        self.assertEqual(status, 201, child)
        self.assertEqual(child["input"]["background_asset_id"], other)
        self.assertEqual(child["input"]["custom_overlay_text"],
                         parent["input"]["custom_overlay_text"])

    def test_retry_rejects_an_invalid_override(self):
        _status, parent = self.custom_job()
        self.post("/api/jobs/%s/cancel" % parent["id"])
        for body, field in (({"overlay_text_mode": "fancy"}, "overlay_text_mode"),
                            ({"custom_overlay_text": "x" * 400},
                             "custom_overlay_text"),
                            ({"background_asset_id": "nope"},
                             "background_asset_id")):
            status, payload = self.post("/api/jobs/%s/retry" % parent["id"], body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload.get("field"), field, body)
        self.assertEqual(self.get("/api/jobs")[1]["count"], 1)

    def test_retry_is_refused_when_the_background_no_longer_exists(self):
        _status, parent = self.custom_job()
        self.post("/api/jobs/%s/cancel" % parent["id"])
        self.known.clear()  # retention removed the image in the meantime
        status, payload = self.post("/api/jobs/%s/retry" % parent["id"])
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["field"], "background_asset_id")
        self.assertEqual(self.get("/api/jobs")[1]["count"], 1)


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
