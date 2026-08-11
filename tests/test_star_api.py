#!/usr/bin/env python3
"""Integration tests for star_api — every route is exercised over real HTTP.

Each test gets its own temp root, so nothing here touches /var/www/star data.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_api  # noqa: E402

DAYS = star_api.DAYS


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="star-api-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel in ("content/overrides", "content/scripts", "content/backups", "cdn/star"):
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)

        self.server = star_api.create_server(self.root, "127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

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
            with urllib.request.urlopen(req, timeout=10) as res:
                payload = res.read()
                return res.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, (json.loads(payload) if payload else None)

    def write_manifest(self, days=None):
        manifest = {"updated": "2026-08-11T00:00:00Z",
                    "days": days if days is not None else
                    [{"date": "2026-08-11", "status": "done"},
                     {"date": "2026-08-12", "status": "draft"}]}
        with open(os.path.join(self.root, "cdn/star/manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh)
        return manifest

    def write_source(self, date, day, text="source text"):
        path = os.path.join(self.root, "content/scripts",
                            "claude_%s_%s.txt" % (date, day))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def write_override(self, date, day, text="override text"):
        directory = os.path.join(self.root, "content/overrides", date)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, day + ".txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as fh:
            return fh.read()


class TestRouting(ApiTestCase):
    def test_unknown_path_is_404_json(self):
        status, body = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_method_not_allowed(self):
        status, body = self.request("POST", "/api/stats", body={})
        self.assertEqual(status, 405)
        self.assertIn("GET", body["allow"])

    def test_get_on_post_route_is_405(self):
        status, _ = self.request("GET", "/api/save-script")
        self.assertEqual(status, 405)

    def test_options_lists_allowed_methods(self):
        for path, method in (("/api/stats", "GET"), ("/api/save-script", "POST")):
            status, _ = self.request("OPTIONS", path)
            self.assertEqual(status, 204)

    def test_query_string_and_trailing_slash_still_route(self):
        self.write_manifest()
        status, _ = self.request("GET", "/api/stats/?fresh=1")
        self.assertEqual(status, 200)

    def test_json_content_type(self):
        req = urllib.request.Request("http://%s/api/stats" % self.host)
        with urllib.request.urlopen(req, timeout=10) as res:
            self.assertIn("application/json", res.headers["Content-Type"])
            self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")


class TestSecurity(ApiTestCase):
    def test_cross_origin_rejected(self):
        status, body = self.request("GET", "/api/stats",
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", body["error"])

    def test_same_origin_allowed(self):
        status, _ = self.request("GET", "/api/stats",
                                 headers={"Origin": "http://%s" % self.host})
        self.assertEqual(status, 200)

    def test_missing_origin_allowed(self):
        status, _ = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)

    def test_body_limit_rejected(self):
        oversized = json.dumps({
            "date": "2026-08-11", "day": "mon",
            "script": "x" * (star_api.MAX_BODY + 10),
        }).encode("utf-8")
        status, body = self.request("POST", "/api/save-script", raw_body=oversized)
        self.assertEqual(status, 413)
        self.assertIn("exceeds", body["error"])
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "content/overrides/2026-08-11/mon.txt")))

    def test_empty_body_rejected(self):
        status, _ = self.request("POST", "/api/regenerate", raw_body=b"")
        self.assertEqual(status, 400)

    def test_invalid_json_rejected(self):
        status, body = self.request("POST", "/api/regenerate", raw_body=b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

    def test_post_without_content_length_is_411(self):
        # urllib always sends Content-Length, so drive the socket directly.
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /api/regenerate HTTP/1.1\r\nHost: %s\r\n"
                     b"Connection: close\r\n\r\n" % self.host.encode())
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        self.assertIn(b" 411 ", response.split(b"\r\n")[0])

    def test_oversized_content_length_rejected_before_reading(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /api/save-script HTTP/1.1\r\nHost: %s\r\n"
                     b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                     % (self.host.encode(), star_api.MAX_BODY + 1))
        status_line = sock.recv(4096).split(b"\r\n")[0]
        self.assertIn(b" 413 ", status_line)

    def test_non_object_body_rejected(self):
        status, _ = self.request("POST", "/api/regenerate", raw_body=b"[1, 2]")
        self.assertEqual(status, 400)


class TestValidation(ApiTestCase):
    def bad_save(self, payload):
        return self.request("POST", "/api/save-script", body=payload)

    def test_missing_fields(self):
        for payload in ({}, {"date": "2026-08-11"}, {"day": "mon"},
                        {"date": "2026-08-11", "day": "mon"}):
            status, _ = self.bad_save(payload)
            self.assertEqual(status, 400, payload)

    def test_bad_date_formats(self):
        for date in ("2026-8-11", "11-08-2026", "2026-08-11T00:00", "", "2026-13-01",
                     "2026-02-30", 20260811, None):
            status, _ = self.bad_save({"date": date, "day": "mon", "script": "hi"})
            self.assertEqual(status, 400, date)

    def test_bad_days(self):
        for day in ("monday", "MON", "xyz", "", None, 3):
            status, _ = self.bad_save({"date": "2026-08-11", "day": day, "script": "hi"})
            self.assertEqual(status, 400, day)

    def test_all_valid_days_accepted(self):
        for day in DAYS:
            status, _ = self.bad_save({"date": "2026-08-11", "day": day, "script": "hi"})
            self.assertEqual(status, 200, day)

    def test_empty_script_rejected(self):
        status, _ = self.bad_save({"date": "2026-08-11", "day": "mon", "script": "   "})
        self.assertEqual(status, 400)

    def test_non_string_script_rejected(self):
        status, _ = self.bad_save({"date": "2026-08-11", "day": "mon", "script": {"a": 1}})
        self.assertEqual(status, 400)


class TestTraversal(ApiTestCase):
    ATTACKS = ("../../etc/passwd", "..", ".", "2026-08-11/../../x",
               "/etc/passwd", "2026-08-11\x00", "....//....//etc")

    def test_traversal_in_date_rejected(self):
        for attack in self.ATTACKS:
            status, _ = self.request("POST", "/api/save-script",
                                     body={"date": attack, "day": "mon", "script": "x"})
            self.assertEqual(status, 400, attack)

    def test_traversal_in_day_rejected(self):
        for attack in self.ATTACKS:
            status, _ = self.request("POST", "/api/save-script",
                                     body={"date": "2026-08-11", "day": attack,
                                           "script": "x"})
            self.assertEqual(status, 400, attack)

    def test_traversal_in_regenerate_rejected(self):
        status, _ = self.request("POST", "/api/regenerate",
                                 body={"date": "../../etc", "day": "mon"})
        self.assertEqual(status, 400)

    def test_nothing_written_outside_root(self):
        self.request("POST", "/api/save-script",
                     body={"date": "../escape", "day": "mon", "script": "x"})
        parent = os.path.dirname(self.root)
        self.assertFalse(os.path.exists(os.path.join(parent, "escape")))

    def test_safe_join_blocks_symlink_escape(self):
        outside = tempfile.mkdtemp(prefix="star-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        link = os.path.join(self.root, "content/overrides/2026-08-11")
        os.symlink(outside, link)
        store = star_api.Store(self.root)
        with self.assertRaises(star_api.ApiError):
            store.override_path("2026-08-11", "mon")


class TestSaveScript(ApiTestCase):
    def test_save_writes_all_three_locations(self):
        text = "สคริปต์วันจันทร์\nบรรทัดสอง"
        status, body = self.request("POST", "/api/save-script",
                                    body={"date": "2026-08-11", "day": "mon",
                                          "script": text})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.read("content/overrides/2026-08-11/mon.txt"), text)
        self.assertEqual(self.read("content/scripts/claude_2026-08-11_mon.txt"), text)
        self.assertEqual(self.read("cdn/star/2026-08-11/mon.txt"), text)
        self.assertEqual(len(body["written"]), 3)

    def test_save_creates_missing_directories(self):
        shutil.rmtree(os.path.join(self.root, "content/overrides"))
        status, _ = self.request("POST", "/api/save-script",
                                 body={"date": "2026-09-01", "day": "sat",
                                       "script": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(self.read("content/overrides/2026-09-01/sat.txt"), "hello")

    def test_overwrite_backs_up_previous_script(self):
        self.request("POST", "/api/save-script",
                     body={"date": "2026-08-11", "day": "tue", "script": "first"})
        status, body = self.request("POST", "/api/save-script",
                                    body={"date": "2026-08-11", "day": "tue",
                                          "script": "second"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(body["backup"])
        self.assertEqual(self.read("content/overrides/2026-08-11/tue.txt"), "second")
        self.assertEqual(self.read(body["backup"]), "first")

    def test_first_save_has_no_backup(self):
        _, body = self.request("POST", "/api/save-script",
                               body={"date": "2026-08-11", "day": "wed",
                                     "script": "only"})
        self.assertIsNone(body["backup"])

    def test_no_temp_files_left_behind(self):
        self.request("POST", "/api/save-script",
                     body={"date": "2026-08-11", "day": "thu", "script": "x"})
        leftovers = [n for n in os.listdir(
            os.path.join(self.root, "content/overrides/2026-08-11"))
            if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_large_but_allowed_script_saved(self):
        text = "ก" * 1000
        status, body = self.request("POST", "/api/save-script",
                                    body={"date": "2026-08-11", "day": "fri",
                                          "script": text})
        self.assertEqual(status, 200)
        self.assertEqual(body["bytes"], len(text.encode("utf-8")))
        self.assertEqual(self.read("content/overrides/2026-08-11/fri.txt"), text)


class TestRegenerate(ApiTestCase):
    def test_single_day_from_source(self):
        self.write_source("2026-08-11", "mon", "from source")
        status, body = self.request("POST", "/api/regenerate",
                                    body={"date": "2026-08-11", "day": "mon"})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["published"][0]["source"], "script")
        self.assertEqual(self.read("cdn/star/2026-08-11/mon.txt"), "from source")

    def test_override_wins_over_source(self):
        self.write_source("2026-08-11", "mon", "from source")
        self.write_override("2026-08-11", "mon", "from override")
        _, body = self.request("POST", "/api/regenerate",
                               body={"date": "2026-08-11", "day": "mon"})
        self.assertEqual(body["published"][0]["source"], "override")
        self.assertEqual(self.read("cdn/star/2026-08-11/mon.txt"), "from override")

    def test_all_days(self):
        for day in DAYS:
            self.write_source("2026-08-12", day, "text-" + day)
        status, body = self.request("POST", "/api/regenerate",
                                    body={"date": "2026-08-12"})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], len(DAYS))
        for day in DAYS:
            self.assertEqual(self.read("cdn/star/2026-08-12/%s.txt" % day),
                             "text-" + day)

    def test_explicit_all_keyword(self):
        for day in DAYS:
            self.write_source("2026-08-12", day)
        status, body = self.request("POST", "/api/regenerate",
                                    body={"date": "2026-08-12", "day": "all"})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], len(DAYS))

    def test_missing_single_day_source_is_404(self):
        status, body = self.request("POST", "/api/regenerate",
                                    body={"date": "2026-08-11", "day": "sun"})
        self.assertEqual(status, 404)
        self.assertEqual(body["missing"], ["sun"])
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "cdn/star/2026-08-11/sun.txt")))

    def test_no_sources_at_all_is_404(self):
        status, body = self.request("POST", "/api/regenerate",
                                    body={"date": "2026-08-20"})
        self.assertEqual(status, 404)
        self.assertEqual(len(body["missing"]), len(DAYS))

    def test_partial_sources_is_409_and_publishes_nothing(self):
        self.write_source("2026-08-13", "mon")
        self.write_source("2026-08-13", "tue")
        status, body = self.request("POST", "/api/regenerate",
                                    body={"date": "2026-08-13"})
        self.assertEqual(status, 409)
        self.assertEqual(sorted(body["available"]), ["mon", "tue"])
        self.assertEqual(len(body["missing"]), len(DAYS) - 2)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "cdn/star/2026-08-13")))

    def test_response_does_not_claim_generation(self):
        self.write_source("2026-08-11", "mon")
        _, body = self.request("POST", "/api/regenerate",
                               body={"date": "2026-08-11", "day": "mon"})
        blob = json.dumps(body, ensure_ascii=False).lower()
        for word in ("claude", "ai ", "generated new", "llm"):
            self.assertNotIn(word, blob)
        self.assertIn("no text was generated", body["note"])

    def test_republish_is_idempotent(self):
        self.write_source("2026-08-11", "mon", "same")
        self.request("POST", "/api/regenerate", body={"date": "2026-08-11", "day": "mon"})
        status, _ = self.request("POST", "/api/regenerate",
                                 body={"date": "2026-08-11", "day": "mon"})
        self.assertEqual(status, 200)
        self.assertEqual(self.read("cdn/star/2026-08-11/mon.txt"), "same")

    def test_saved_script_can_be_republished(self):
        self.request("POST", "/api/save-script",
                     body={"date": "2026-08-14", "day": "sat", "script": "edited"})
        os.unlink(os.path.join(self.root, "cdn/star/2026-08-14/sat.txt"))
        status, _ = self.request("POST", "/api/regenerate",
                                 body={"date": "2026-08-14", "day": "sat"})
        self.assertEqual(status, 200)
        self.assertEqual(self.read("cdn/star/2026-08-14/sat.txt"), "edited")


class TestManifest(ApiTestCase):
    def test_manifest_returned(self):
        expected = self.write_manifest()
        status, body = self.request("GET", "/api/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)

    def test_missing_manifest_is_404(self):
        status, body = self.request("GET", "/api/manifest")
        self.assertEqual(status, 404)
        self.assertIn("manifest.json", body["error"])

    def test_broken_manifest_is_500(self):
        with open(os.path.join(self.root, "cdn/star/manifest.json"), "w") as fh:
            fh.write("{oops")
        status, body = self.request("GET", "/api/manifest")
        self.assertEqual(status, 500)
        self.assertIn("JSON", body["error"])


class TestStats(ApiTestCase):
    def test_empty_root_reports_zeros(self):
        status, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(body["dates_total"], 0)
        self.assertEqual(body["published_total"], 0)
        self.assertIsNone(body["first_date"])
        self.assertFalse(body["manifest_ok"])

    def test_counts_come_from_real_files(self):
        self.write_manifest()
        for day in DAYS:
            self.write_source("2026-08-11", day)
        self.write_override("2026-08-11", "mon")
        self.write_source("2026-08-12", "tue")
        self.request("POST", "/api/regenerate", body={"date": "2026-08-11"})

        _, body = self.request("GET", "/api/stats")
        self.assertEqual(body["dates_total"], 2)
        self.assertEqual(body["scripts_total"], len(DAYS) + 1)
        self.assertEqual(body["overrides_total"], 1)
        self.assertEqual(body["published_total"], len(DAYS))
        self.assertEqual(body["dates_complete"], 1)
        self.assertEqual(body["first_date"], "2026-08-11")
        self.assertEqual(body["last_date"], "2026-08-12")
        self.assertEqual(body["manifest_days"], 2)
        self.assertEqual(body["status_counts"], {"done": 1, "draft": 1})

    def test_stats_track_new_saves(self):
        before = self.request("GET", "/api/stats")[1]
        self.request("POST", "/api/save-script",
                     body={"date": "2026-08-30", "day": "sun", "script": "x"})
        after = self.request("GET", "/api/stats")[1]
        self.assertEqual(after["overrides_total"], before["overrides_total"] + 1)
        self.assertEqual(after["published_total"], before["published_total"] + 1)
        self.assertEqual(after["dates_total"], before["dates_total"] + 1)

    def test_stray_directories_ignored(self):
        os.makedirs(os.path.join(self.root, "cdn/star/templates"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "cdn/star/2026-08-11"), exist_ok=True)
        _, body = self.request("GET", "/api/stats")
        self.assertEqual(body["dates_total"], 1)

    def _put(self, rel, text="x"):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_compat_keys_present_and_zero_on_empty_root(self):
        _, body = self.request("GET", "/api/stats")
        for key in ("scripts", "days", "audio", "videos"):
            self.assertIn(key, body)
            self.assertEqual(body[key], 0)

    def test_compat_keys_count_real_files(self):
        # two naming variants plus a nested date directory
        self._put("content/scripts/claude_2026-08-11_mon.txt")
        self._put("content/scripts/claude_2026-08-11_tue.txt")
        self._put("content/scripts/script_2026-08-12_wed.txt")
        self._put("content/scripts/2026-08-13/sun.txt")
        self._put("content/scripts/notes.md")          # not a .txt
        self._put("content/scripts/script_2026-13-45_mon.txt")  # impossible date

        self._put("output/2026-08-11/audio/mon.mp3")
        self._put("output/2026-08-11/audio/tue.wav")
        self._put("output/2026-08-11/audio/wed.ogg")
        self._put("output/2026-08-11/video/mon.mp4")
        self._put("output/2026-08-11/video/tue.mov")
        self._put("output/2026-08-11/video/wed.webm")
        self._put("output/2026-08-11/final.json")

        _, body = self.request("GET", "/api/stats")
        self.assertEqual(body["scripts"], 5)
        self.assertEqual(body["days"], 3)
        self.assertEqual(body["audio"], 3)
        self.assertEqual(body["videos"], 3)

    def test_compat_keys_keep_existing_keys(self):
        _, body = self.request("GET", "/api/stats")
        for key in ("dates_total", "scripts_total", "overrides_total",
                    "published_total", "manifest_ok", "status_counts"):
            self.assertIn(key, body)

    def test_generated_at_present(self):
        _, body = self.request("GET", "/api/stats")
        self.assertTrue(body["generated_at"].endswith("Z"))


class TestHealth(ApiTestCase):
    def test_healthy_when_manifest_valid_and_dirs_writable(self):
        self.write_manifest()
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], star_api.VERSION)
        for rel in star_api.CONTENT_DIRS:
            self.assertEqual(body["checks"][rel]["status"], "ok")
        self.assertEqual(body["checks"]["manifest"]["status"], "ok")

    def test_missing_manifest_fails_health(self):
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "fail")
        self.assertEqual(body["checks"]["manifest"]["status"], "fail")

    def test_manifest_without_days_warns(self):
        with open(os.path.join(self.root, "cdn/star/manifest.json"), "w") as fh:
            json.dump({"updated": "now"}, fh)
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "warn")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_directory_fails_health(self):
        self.write_manifest()
        target = os.path.join(self.root, "content/overrides")
        os.chmod(target, 0o500)
        self.addCleanup(os.chmod, target, 0o755)
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["checks"]["content/overrides"]["status"], "fail")

    def test_health_leaves_no_temp_files(self):
        self.write_manifest()
        self.request("GET", "/api/health")
        for rel in star_api.CONTENT_DIRS:
            names = os.listdir(os.path.join(self.root, rel))
            self.assertEqual([n for n in names if n.startswith(".healthcheck-")], [])


class TestUnits(unittest.TestCase):
    def test_valid_date_roundtrip(self):
        self.assertEqual(star_api.valid_date("2026-02-28"), "2026-02-28")

    def test_leap_day_accepted(self):
        self.assertEqual(star_api.valid_date("2028-02-29"), "2028-02-29")

    def test_no_secrets_in_source(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "star_api.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read().lower()
        for marker in ("secret", "api_key", "password", "token", "sk-"):
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
