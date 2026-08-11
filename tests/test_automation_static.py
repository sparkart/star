#!/usr/bin/env python3
"""Static checks over the tracked backend source.

These are cheap, fast guards for the mistakes that are easy to make and hard to
notice in review: a `shell=True` creeping into a subprocess call, a real-looking
credential pasted into a source file, or a route that exists in the contract but
not in the dispatcher.
"""

import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_api  # noqa: E402
import star_jobs  # noqa: E402
import star_providers  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BACKEND_MODULES = (
    "star_api.py", "star_automation.py", "star_jobs.py",
    "star_providers.py", "star_redact.py", "star_state.py",
)

# Shapes that would be an actual credential rather than the word for one.
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}"),
    re.compile(r"\bya29\.[A-Za-z0-9._\-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\.[A-Za-z0-9._\-]{20,}\."),
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}"),
)


def source_of(name):
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as fh:
        return fh.read()


class TestNoShellTrue(unittest.TestCase):
    def test_no_module_uses_shell_true(self):
        """Parsed, not grepped: a comment saying shell=True must not fail this,
        and an actual keyword argument must not slip past a formatting change."""
        for name in BACKEND_MODULES:
            tree = ast.parse(source_of(name), filename=name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "shell":
                        continue
                    value = keyword.value
                    is_true = isinstance(value, ast.Constant) and value.value is True
                    self.assertFalse(is_true, "shell=True in %s line %d"
                                     % (name, node.lineno))

    def test_no_os_system_or_popen_shell(self):
        for name in BACKEND_MODULES:
            source = source_of(name)
            self.assertNotIn("os.system(", source, name)
            self.assertNotIn("os.popen(", source, name)
            self.assertNotIn("subprocess.getoutput", source, name)

    def test_subprocess_calls_pass_a_list(self):
        """Every subprocess entry point in the backend takes an argv list."""
        tree = ast.parse(source_of("star_automation.py"))
        found = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("Popen", "run", "call", "check_output")):
                found = True
                first = node.args[0] if node.args else None
                self.assertIsNotNone(first, "subprocess call with no argv")
                self.assertNotIsInstance(first, ast.Constant,
                                         "subprocess called with a string command")
        self.assertTrue(found, "expected at least one subprocess call to inspect")


class TestNoHardcodedSecrets(unittest.TestCase):
    def test_backend_sources_contain_no_credential_shaped_values(self):
        for name in BACKEND_MODULES:
            source = source_of(name)
            for pattern in SECRET_VALUE_PATTERNS:
                match = pattern.search(source)
                self.assertIsNone(
                    match, "%s looks like a real credential in %s"
                    % (match.group(0)[:12] if match else "", name))

    def test_state_dir_default_is_not_inside_the_repo(self):
        import star_state
        self.assertEqual(star_state.DEFAULT_STATE_DIR, "/var/lib/star")
        self.assertFalse(star_state.DEFAULT_STATE_DIR.startswith(ROOT))

    def test_systemd_unit_provisions_and_allows_the_state_directory(self):
        unit = source_of(os.path.join("deploy", "star-api.service"))
        self.assertIn("Environment=STAR_STATE_DIR=/var/lib/star", unit)
        self.assertIn("Environment=CLAUDE_CONFIG_DIR=/home/ubuntu/.claude-b", unit)
        self.assertIn("StateDirectory=star", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        writable = next(line for line in unit.splitlines()
                        if line.startswith("ReadWritePaths="))
        self.assertIn("/var/lib/star", writable)


def js_function_body(source, name):
    """Return the source of `function name(...) { ... }` by brace matching."""
    start = source.index("function %s(" % name)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace:index + 1]
    raise AssertionError("unbalanced braces in %s" % name)


class TestScheduleFormPlatformSelector(unittest.TestCase):
    """Regression for the schedule form that read a checkbox group nobody drew.

    `saveSchedule()` read `input[name="splatform"]`, the markup contained none
    and no code built any, so a scheduled run could never select a platform and
    ticking `publish` in that form was unsatisfiable from the UI.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = source_of(os.path.join("automation", "index.html"))
        cls.js = source_of(os.path.join("automation", "automation.js"))
        cls.render = js_function_body(cls.js, "syncSchedulePlatforms")
        cls.save = js_function_body(cls.js, "saveSchedule")

    def render_halves(self):
        """The automatable loop and the manual loop, separately."""
        auto_at = self.render.index("meta.automatable.forEach")
        manual_at = self.render.index("meta.manual.forEach")
        self.assertLess(auto_at, manual_at)
        return self.render[auto_at:manual_at], self.render[manual_at:]

    def test_schedule_section_has_a_selector_container(self):
        self.assertIn('id="ac-sched-platforms"', self.html)
        section = self.html.index('id="schedule"')
        container = self.html.index('id="ac-sched-platforms"')
        save_button = self.html.index('id="ac-sched-save"')
        self.assertLess(section, container,
                        "the selector must live in the schedule section")
        self.assertLess(container, save_button)

    def test_every_checkbox_group_read_back_is_actually_rendered(self):
        """The exact shape of the original defect: a name read but never drawn."""
        read = set(re.findall(r"selectedValues\('([a-z\-]+)'\)", self.js))
        read |= set(re.findall(r"applySelection\('([a-z\-]+)'", self.js))
        rendered = set(re.findall(r"box\.name = '([a-z\-]+)';", self.js))
        rendered |= set(re.findall(r"buildPills\([^;]*?, '([a-z\-]+)',", self.js))
        self.assertIn("splatform", read, "saveSchedule must still read the group")
        self.assertEqual(read - rendered, set(),
                         "these checkbox groups are read but never rendered")

    def test_targets_are_derived_from_overview_metadata(self):
        meta = js_function_body(self.js, "schedulePlatformMeta")
        self.assertIn("state.overview", meta)
        self.assertIn("platforms", meta)
        self.assertIn("meta.automatable", self.render)
        self.assertIn("meta.manual", self.render)
        # Nothing may be hard coded: the backend's own lists are the only source.
        for platform in star_jobs.PLATFORMS:
            self.assertNotIn("'%s'" % platform, self.render,
                             "%s is hard coded in the selector" % platform)

    def test_only_automatable_targets_carry_the_splatform_name(self):
        automatable, manual = self.render_halves()
        self.assertIn("box.name = 'splatform';", automatable)
        self.assertNotIn("box.name = 'splatform';", manual)

    def test_manual_targets_are_disabled_and_unselectable(self):
        _automatable, manual = self.render_halves()
        self.assertIn("box.disabled = true;", manual)
        self.assertIn("box.checked = false;", manual)
        self.assertIn("aria-disabled", manual)
        # A different name means selectedValues('splatform') cannot see them
        # even if the disabled attribute were lost in a future edit.
        self.assertIn("box.name = 'splatform-manual';", manual)

    def test_no_target_is_preselected(self):
        self.assertNotIn('name="splatform"', self.html,
                         "the markup must not ship a preselected target")
        automatable, _manual = self.render_halves()
        self.assertIn("box.checked = selected.indexOf(key) !== -1;", automatable)
        # `selected` only ever comes from the live form or the stored config.
        self.assertIn("state.schedule && state.schedule.platforms", self.render)
        for call in re.findall(r"applySelection\('splatform', ([^)]*)\)", self.js):
            self.assertIn("config.platforms", call,
                          "splatform selection must come from the server config")

    def test_schedule_defaults_stay_safe_in_the_markup(self):
        enabled = self.html.index('id="ac-sched-enabled"')
        self.assertNotIn("checked", self.html[enabled:self.html.index(">", enabled)])
        dry = self.html.index('id="ac-sched-dry"')
        self.assertIn("checked", self.html[dry:self.html.index(">", dry)])

    def test_publish_without_a_target_is_blocked_before_the_request(self):
        reason = js_function_body(self.js, "scheduleBlockReason")
        self.assertIn("selectedValues('sstage')", reason)
        self.assertIn("selectedValues('splatform')", reason)
        self.assertIn("'publish'", reason)
        guard = self.save.index("scheduleBlockReason()")
        request = self.save.index("api('PUT', '/api/schedule'")
        self.assertLess(guard, request, "the guard must run before the PUT")
        self.assertIn("return;", self.save[guard:request])

    def test_submitted_targets_are_filtered_to_automatable_keys(self):
        self.assertIn("schedulePlatformMeta().automatable", self.save)
        self.assertIn("selectedValues('splatform').filter", self.save)


class TestContractSurface(unittest.TestCase):
    def test_original_routes_are_all_still_registered(self):
        for key in (("GET", "/api/health"), ("GET", "/api/manifest"),
                    ("GET", "/api/stats"), ("POST", "/api/save-script"),
                    ("POST", "/api/regenerate")):
            self.assertIn(key, star_api.ROUTES, key)

    def test_days_constant_has_not_drifted(self):
        self.assertEqual(star_api.DAYS, star_jobs.DAYS)

    def test_every_platform_maps_to_a_provider(self):
        for platform in star_jobs.PLATFORMS:
            self.assertIn(platform, star_providers.PROVIDER_KEYS, platform)

    def test_manual_platforms_are_not_listed_as_automatable(self):
        overlap = set(star_jobs.MANUAL_PLATFORMS) & set(star_jobs.AUTOMATABLE_PLATFORMS)
        self.assertEqual(overlap, set())

    def test_state_changing_automation_routes_require_intent(self):
        for method, path, _handler, intent in star_api.AUTOMATION_ROUTES:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self.assertTrue(intent, "%s %s must require the intent header"
                                % (method, path))

    def test_modules_compile(self):
        import py_compile
        for name in BACKEND_MODULES:
            py_compile.compile(os.path.join(ROOT, name), doraise=True)


if __name__ == "__main__":
    unittest.main()
