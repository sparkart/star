#!/usr/bin/env python3
"""Static checks over the tracked backend and control-center sources.

These are cheap, fast guards for the mistakes that are easy to make and hard to
notice in review: a `shell=True` creeping into a subprocess call, a real-looking
credential pasted into a source file, a route that exists in the contract but
not in the dispatcher, or — since the control center was split into six pages —
a page module that reaches for an element its own page does not contain.
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

# The six pages, in nav order: page key -> (document, page module).
PAGES = (
    ("dashboard", os.path.join("automation", "index.html"),
     os.path.join("automation", "pages-dashboard.js"), "/automation/"),
    ("run", os.path.join("automation", "run", "index.html"),
     os.path.join("automation", "pages-run.js"), "/automation/run/"),
    ("jobs", os.path.join("automation", "jobs", "index.html"),
     os.path.join("automation", "pages-jobs.js"), "/automation/jobs/"),
    ("schedule", os.path.join("automation", "schedule", "index.html"),
     os.path.join("automation", "pages-schedule.js"), "/automation/schedule/"),
    ("providers", os.path.join("automation", "providers", "index.html"),
     os.path.join("automation", "pages-providers.js"), "/automation/providers/"),
    ("guide", os.path.join("automation", "guide", "index.html"),
     os.path.join("automation", "pages-guide.js"), "/automation/guide/"),
)

SHARED_JS = os.path.join("automation", "automation.js")
NAV_JS = os.path.join("automation", "automation-nav.js")
PAGE_MODULES = tuple(module for _key, _html, module, _url in PAGES)
FRONTEND_SOURCES = (SHARED_JS, NAV_JS) + PAGE_MODULES + tuple(
    html for _key, html, _module, _url in PAGES)

# A misspelling that once shipped as an identifier; it must never come back.
FORBIDDEN_TERM = "qrg"

# Star Automation owns its own R2 bucket.  The legacy CDN name belongs to a
# different surface and must never become an Automation upload target again.
FORBIDDEN_AUTOMATION_R2_NAME = "qrf"


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

    def test_frontend_sources_contain_no_credential_shaped_values(self):
        """The control center writes credentials; it must never carry one."""
        for name in FRONTEND_SOURCES:
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


# ── the six pages ──────────────────────────────────────────


class TestSixPages(unittest.TestCase):
    """The split itself: six real documents, each wired to its own module."""

    @classmethod
    def setUpClass(cls):
        cls.html = {key: source_of(path) for key, path, _m, _u in PAGES}

    def test_every_page_exists_and_is_not_empty(self):
        """The interrupted write left these at zero bytes; they must be real."""
        for key, path, _module, _url in PAGES:
            full = os.path.join(ROOT, path)
            self.assertTrue(os.path.isfile(full), "%s is missing" % path)
            self.assertGreater(os.path.getsize(full), 2000,
                               "%s is empty or a stub" % path)
            self.assertIn("<!DOCTYPE html>", self.html[key], path)
            self.assertIn('<html lang="th">', self.html[key], path)
            self.assertIn("</html>", self.html[key], path)

    def test_every_page_declares_its_own_page_key(self):
        for key, path, _module, _url in PAGES:
            self.assertIn('data-ac-page="%s"' % key, self.html[key], path)

    def test_every_page_loads_the_shared_core_nav_and_stylesheet(self):
        for key, path, _module, _url in PAGES:
            page = self.html[key]
            self.assertIn('href="/automation/automation.css"', page, path)
            self.assertIn('src="/automation/automation.js"', page, path)
            self.assertIn('src="/automation/automation-nav.js"', page, path)
            self.assertIn('src="/workspace.js"', page, path)
            # The core must be parsed before the module that registers against it.
            self.assertLess(page.index("/automation/automation.js"),
                            page.index("/automation/pages-"), path)

    def test_every_page_loads_exactly_its_own_module(self):
        """A page that loaded a sibling's module would query a foreign DOM."""
        for key, path, module, _url in PAGES:
            page = self.html[key]
            wanted = "/" + module.replace(os.sep, "/")
            self.assertIn('src="%s"' % wanted, page, path)
            for _other, _p, other_module, _u in PAGES:
                if other_module == module:
                    continue
                self.assertNotIn('src="/%s"' % other_module.replace(os.sep, "/"),
                                 page, "%s also loads %s" % (path, other_module))

    def test_every_page_carries_the_same_six_link_nav(self):
        for key, path, _module, _url in PAGES:
            page = self.html[key]
            self.assertIn('id="ac-nav"', page, path)
            nav = page[page.index('id="ac-nav"'):page.index("</nav>",
                                                            page.index('id="ac-nav"'))]
            for _k, _p, _m, url in PAGES:
                self.assertIn('href="%s"' % url, nav,
                              "%s nav is missing %s" % (path, url))
            self.assertEqual(len(re.findall(r'data-ac-nav="', nav)), len(PAGES), path)

    def test_exactly_one_nav_link_is_marked_current(self):
        for key, path, _module, url in PAGES:
            page = self.html[key]
            start = page.index('id="ac-nav"')
            nav = page[start:page.index("</nav>", start)]
            current = re.findall(r'<a class="ac-nav-link is-active" href="([^"]+)"', nav)
            self.assertEqual(current, [url], "%s marks the wrong active link" % path)
            self.assertEqual(nav.count('aria-current="page"'), 1, path)
            self.assertIn('data-ac-nav="%s" aria-current="page"' % key, nav, path)

    def test_every_page_prefetches_its_five_siblings(self):
        """Moving between the six is the common case, so warm them on arrival."""
        for key, path, _module, url in PAGES:
            page = self.html[key]
            head = page[:page.index("</head>")]
            for _k, _p, _m, other in PAGES:
                if other == url:
                    continue
                self.assertIn('<link rel="prefetch" href="%s">' % other, head,
                              "%s does not prefetch %s" % (path, other))

    def test_only_the_run_page_can_create_a_job(self):
        """Controls live on exactly one page: no second run button anywhere."""
        for key, path, _module, _url in PAGES:
            if key == "run":
                continue
            self.assertNotIn('id="ac-run"', self.html[key], path)
        self.assertIn('id="ac-run"', self.html["run"])

    def test_only_the_schedule_page_can_change_the_schedule(self):
        for key, path, _module, _url in PAGES:
            if key == "schedule":
                continue
            self.assertNotIn('id="ac-sched-save"', self.html[key], path)
        self.assertIn('id="ac-sched-save"', self.html["schedule"])

    def test_the_dashboard_is_read_only(self):
        """It summarises and links; every mutating control lives elsewhere."""
        page = self.html["dashboard"]
        for control in ('id="ac-run"', 'id="ac-sched-save"', 'id="ac-cancel"',
                        'id="ac-retry"', 'id="ac-providers"'):
            self.assertNotIn(control, page, control)
        self.assertIn('id="ac-current-panel"', page)


class TestPageAwareInitialisation(unittest.TestCase):
    """No module may run on a document that does not own its markup."""

    @classmethod
    def setUpClass(cls):
        cls.core = source_of(SHARED_JS)
        cls.modules = {module: source_of(module) for module in PAGE_MODULES}
        cls.html = {key: source_of(path) for key, path, _m, _u in PAGES}

    def test_the_core_dispatches_on_the_body_page_key(self):
        boot = js_function_body(self.core, "boot")
        self.assertIn("currentPage()", boot)
        self.assertIn("pages[name]", boot)
        # An unknown or absent key starts nothing at all.
        self.assertIn("if (typeof init !== 'function') return;", boot)
        current = js_function_body(self.core, "currentPage")
        self.assertIn("dataset", current)
        self.assertIn("acPage", current)

    def test_every_module_registers_exactly_one_page(self):
        for key, _path, module, _url in PAGES:
            source = self.modules[module]
            registered = re.findall(r"AC\.page\('([a-z]+)'", source)
            self.assertEqual(registered, [key],
                             "%s registers %r, expected [%r]"
                             % (module, registered, key))

    def test_every_module_bails_out_without_the_shared_core(self):
        """Load order or a failed core must degrade, never throw."""
        for module in PAGE_MODULES:
            source = self.modules[module]
            self.assertIn("var AC = window.StarAC;", source, module)
            self.assertIn("if (!AC) return;", source, module)

    def test_registered_page_keys_match_the_documents(self):
        registered = set()
        for source in self.modules.values():
            registered.update(re.findall(r"AC\.page\('([a-z]+)'", source))
        declared = set(re.findall(r'data-ac-page="([a-z]+)"',
                                  "".join(self.html.values())))
        self.assertEqual(registered, declared)
        self.assertEqual(len(registered), len(PAGES))

    def test_every_element_a_module_looks_up_exists_on_its_own_page(self):
        """The defect this architecture is meant to make impossible: a module
        querying an id that its page does not contain."""
        for key, path, module, _url in PAGES:
            page = self.html[key]
            wanted = set(re.findall(r"\$\('#([A-Za-z0-9\-]+)'\)",
                                    self.modules[module]))
            self.assertTrue(wanted, "%s looks up no elements at all" % module)
            for element_id in sorted(wanted):
                self.assertIn('id="%s"' % element_id, page,
                              "%s reads #%s, which %s does not contain"
                              % (module, element_id, path))

    def test_the_shared_core_owns_no_page_specific_element(self):
        """Anything the core touched by id would have to exist on all six."""
        core_ids = set(re.findall(r"\$\('#([A-Za-z0-9\-]+)'\)", self.core))
        self.assertEqual(core_ids, {"toast-region", "ac-refresh"}, sorted(core_ids))
        for key, path, _module, _url in PAGES:
            for element_id in sorted(core_ids):
                self.assertIn('id="%s"' % element_id, self.html[key],
                              "%s is missing the shared #%s" % (path, element_id))


class TestNavBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nav = source_of(NAV_JS)

    def test_active_state_is_re_asserted_from_the_page_key(self):
        self.assertIn("dataset.acPage", self.nav)
        self.assertIn("data-ac-nav", self.nav)
        self.assertIn("aria-current", self.nav)

    def test_prefetch_is_skipped_on_metered_connections(self):
        self.assertIn("saveData", self.nav)
        self.assertIn("rel = 'prefetch'", self.nav)

    def test_the_nav_never_prefetches_the_page_it_is_on(self):
        self.assertIn("if (link.dataset.acNav === current) return;", self.nav)

    def test_the_nav_is_a_no_op_without_its_markup(self):
        self.assertIn("if (!nav) return;", self.nav)


# ── schedule guards (adapted from the monolith's regressions) ──


class TestScheduleFormPlatformSelector(unittest.TestCase):
    """Regression for the schedule form that read a checkbox group nobody drew.

    `saveSchedule()` read `input[name="splatform"]`, the markup contained none
    and no code built any, so a scheduled run could never select a platform and
    ticking `publish` in that form was unsatisfiable from the UI.

    The code moved to `automation/pages-schedule.js` when the control center was
    split into six pages; the contract did not move with it.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = source_of(os.path.join("automation", "schedule", "index.html"))
        cls.js = source_of(os.path.join("automation", "pages-schedule.js"))
        # Checkbox groups are read and rendered across several modules now.
        cls.all_js = "\n".join(source_of(name) for name in
                               (SHARED_JS,) + PAGE_MODULES)
        cls.render = js_function_body(cls.js, "syncSchedulePlatforms")
        cls.save = js_function_body(cls.js, "saveSchedule")

    def render_halves(self):
        """The automatable loop and the manual loop, separately."""
        auto_at = self.render.index("meta.automatable.forEach")
        manual_at = self.render.index("meta.manual.forEach")
        self.assertLess(auto_at, manual_at)
        return self.render[auto_at:manual_at], self.render[manual_at:]

    def test_schedule_page_has_a_selector_container(self):
        self.assertIn('id="ac-sched-platforms"', self.html)
        section = self.html.index('id="schedule"')
        container = self.html.index('id="ac-sched-platforms"')
        save_button = self.html.index('id="ac-sched-save"')
        self.assertLess(section, container,
                        "the selector must live in the schedule section")
        self.assertLess(container, save_button)

    def test_every_checkbox_group_read_back_is_actually_rendered(self):
        """The exact shape of the original defect: a name read but never drawn."""
        read = set(re.findall(r"selectedValues\('([a-z\-]+)'\)", self.all_js))
        read |= set(re.findall(r"applySelection\('([a-z\-]+)'", self.all_js))
        rendered = set(re.findall(r"box\.name = '([a-z\-]+)';", self.all_js))
        rendered |= set(re.findall(r"buildPills\([^;]*?, '([a-z\-]+)',", self.all_js))
        self.assertIn("splatform", read, "saveSchedule must still read the group")
        self.assertEqual(read - rendered, set(),
                         "these checkbox groups are read but never rendered")

    def test_the_groups_a_page_reads_are_built_by_that_same_page(self):
        """Cross-page leakage would resurrect the original defect one module
        further out: the run form's groups must not be what satisfies the
        schedule form's reads."""
        for _key, _path, module, _url in PAGES:
            source = source_of(module)
            read = set(re.findall(r"selectedValues\('([a-z\-]+)'\)", source))
            read |= set(re.findall(r"applySelection\('([a-z\-]+)'", source))
            if not read:
                continue
            rendered = set(re.findall(r"box\.name = '([a-z\-]+)';", source))
            rendered |= set(re.findall(r"buildPills\([^;]*?, '([a-z\-]+)',", source))
            self.assertEqual(read - rendered, set(),
                             "%s reads groups it does not render" % module)

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

    def test_manual_platforms_can_never_be_scheduled(self):
        """TikTok and Shopee have no working publish channel, so the schedule
        form must not be able to submit them however the render is reached."""
        self.assertTrue(star_jobs.MANUAL_PLATFORMS, "expected manual platforms")
        for platform in star_jobs.MANUAL_PLATFORMS:
            self.assertNotIn(platform, star_jobs.AUTOMATABLE_PLATFORMS)
        _automatable, manual = self.render_halves()
        self.assertIn("box.disabled = true;", manual)
        self.assertIn("schedulePlatformMeta().automatable", self.save)

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

    def test_the_form_is_repopulated_from_the_response(self):
        """What the server stored, not what was submitted."""
        self.assertIn("applyConfig(config);", self.save)


class TestRunFormGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = source_of(os.path.join("automation", "pages-run.js"))
        cls.html = source_of(os.path.join("automation", "run", "index.html"))

    def test_dry_run_is_the_default_in_the_markup(self):
        dry = self.html.index('id="ac-dry-run"')
        self.assertIn("checked", self.html[dry:self.html.index(">", dry)])
        force = self.html.index('id="ac-force"')
        self.assertNotIn("checked", self.html[force:self.html.index(">", force)])

    def test_publish_without_a_target_is_blocked_before_the_request(self):
        """Symmetrical with the schedule form: the backend rejects it, so the
        page says so instead of spending a round trip on a certain 400."""
        reason = js_function_body(self.js, "runBlockReason")
        self.assertIn("selectedValues('stage')", reason)
        self.assertIn("selectedValues('platform')", reason)
        self.assertIn("'publish'", reason)
        submit = js_function_body(self.js, "submitJob")
        guard = submit.index("runBlockReason()")
        request = submit.index("api('POST', '/api/jobs'")
        self.assertLess(guard, request, "the guard must run before the POST")
        self.assertIn("return;", submit[guard:request])

    def test_publish_targets_come_from_the_provider_list(self):
        render = js_function_body(self.js, "syncPlatformAvailability")
        self.assertIn("state.providers", render)
        for platform in star_jobs.PLATFORMS:
            self.assertNotIn("'%s'" % platform, render,
                             "%s is hard coded in the run form" % platform)

    def test_manual_targets_are_never_drawn_as_published(self):
        """A one-off run may prepare a handoff bundle for a manual platform,
        but the form must label it as manual, not as an upload."""
        render = js_function_body(self.js, "syncPlatformAvailability")
        self.assertIn("provider.automation !== 'full_auto'", render)
        self.assertIn("front_hand", render)
        self.assertIn("handoff/", render)

    def test_the_job_is_handed_over_rather_than_followed_here(self):
        submit = js_function_body(self.js, "submitJob")
        self.assertIn("AC.jobHref(job.id)", submit)


class TestSecretHandling(unittest.TestCase):
    """Credentials are write-only and must not outlive their request."""

    @classmethod
    def setUpClass(cls):
        cls.js = source_of(os.path.join("automation", "pages-providers.js"))
        cls.core = source_of(SHARED_JS)

    def test_entered_values_are_cleared_from_the_dom_after_a_save(self):
        submit = js_function_body(self.js, "submitProviderConfig")
        self.assertIn("clearSecretInputs(formHost);", submit)
        clearer = js_function_body(self.js, "clearSecretInputs")
        self.assertIn("input.value = '';", clearer)
        # The clear happens on success, before anything reloads the list.
        self.assertLess(submit.index("clearSecretInputs(formHost);"),
                        submit.index("return loadProviders();"))

    def test_write_only_fields_are_never_prefilled(self):
        build = js_function_body(self.js, "buildConfigForm")
        self.assertIn("field.write_only", build)
        self.assertIn("input.placeholder", build)
        self.assertNotIn("input.value =", build,
                         "a configure form must not prefill any value")

    def test_only_the_providers_page_can_write_a_credential(self):
        for _key, _path, module, _url in PAGES:
            if module.endswith("pages-providers.js"):
                continue
            self.assertNotIn("/api/providers/configure", source_of(module), module)

    def test_state_changing_requests_carry_the_intent_header(self):
        api = js_function_body(self.core, "api")
        self.assertIn("INTENT_HEADER", api)
        self.assertIn("method !== 'GET'", api)
        self.assertIn("credentials: 'same-origin'", api)
        self.assertIn("X-Star-Intent", self.core)


class TestGuidePage(unittest.TestCase):
    """The reader must read the API, never a copy of the guide."""

    @classmethod
    def setUpClass(cls):
        cls.js = source_of(os.path.join("automation", "pages-guide.js"))
        cls.html = source_of(os.path.join("automation", "guide", "index.html"))

    def test_the_page_reads_the_canonical_endpoint(self):
        self.assertIn("'/api/automation/prediction-guide'", self.js)
        self.assertIn("/api/automation/prediction-guide", self.html)
        self.assertIn(("GET", "/api/automation/prediction-guide"),
                      [(method, path) for method, path, _h, _i
                       in star_api.AUTOMATION_ROUTES])

    def test_the_guide_endpoint_is_read_only_and_needs_no_intent(self):
        for method, path, _handler, intent in star_api.AUTOMATION_ROUTES:
            if path.endswith("prediction-guide"):
                self.assertEqual(method, "GET")
                self.assertFalse(intent)

    def test_the_reader_duplicates_no_guide_prose(self):
        """Only the API may supply the rules; the page renders shapes."""
        import json
        with open(os.path.join(ROOT, "automation", "prediction-guide.json"),
                  "r", encoding="utf-8") as fh:
            guide = json.load(fh)

        def strings(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, list):
                for item in node:
                    for found in strings(item):
                        yield found
            elif isinstance(node, dict):
                for item in node.values():
                    for found in strings(item):
                        yield found

        for text in strings(guide):
            if len(text) < 25:
                continue
            self.assertNotIn(text, self.js,
                             "guide prose is duplicated in pages-guide.js")
            self.assertNotIn(text, self.html,
                             "guide prose is duplicated in the guide page")

    def test_an_invalid_guide_shows_the_reason_instead_of_content(self):
        render = js_function_body(self.js, "renderGuide")
        self.assertIn("!view.valid", render)
        self.assertIn("view.error", render)
        self.assertIn("host.hidden = true;", render)

    def test_the_renderer_handles_every_shape_the_payload_uses(self):
        render = js_function_body(self.js, "renderValue")
        self.assertIn("Array.isArray", render)
        self.assertIn("isPlainObject", render)
        self.assertIn("typeof value === 'string'", render)


class TestFrontendHygiene(unittest.TestCase):
    def test_no_module_uses_a_blocking_dialog(self):
        """Status lines and toasts only: a modal dialog would block the poll
        loop and cannot be styled or read consistently."""
        for name in (SHARED_JS, NAV_JS) + PAGE_MODULES:
            source = source_of(name)
            for banned in ("alert(", "confirm(", "prompt("):
                self.assertNotIn(banned, source, "%s uses %s" % (name, banned))

    def test_the_forbidden_typo_term_is_absent(self):
        for name in FRONTEND_SOURCES + (os.path.join("automation", "automation.css"),):
            self.assertNotIn(FORBIDDEN_TERM, source_of(name).lower(),
                             "%r reappeared in %s" % (FORBIDDEN_TERM, name))

    def test_automation_never_targets_the_legacy_r2_name(self):
        names = FRONTEND_SOURCES + (
            os.path.join("automation", "automation.css"),
            os.path.join("automation", "upload_manifest.py"),
        )
        for name in names:
            self.assertNotIn(FORBIDDEN_AUTOMATION_R2_NAME, source_of(name).lower(),
                             "%r reappeared in %s"
                             % (FORBIDDEN_AUTOMATION_R2_NAME, name))

    def test_every_request_is_same_origin(self):
        """No page may invent a base URL; paths are relative to this origin."""
        for name in (SHARED_JS,) + PAGE_MODULES:
            source = source_of(name)
            for match in re.findall(r"api\('[A-Z]+', '([^']+)'", source):
                self.assertTrue(match.startswith("/"),
                                "%s calls a non-relative path %r" % (name, match))

    def test_the_stylesheet_covers_the_new_surfaces(self):
        css = source_of(os.path.join("automation", "automation.css"))
        for selector in (".ac-nav", ".ac-nav-link", ".ac-door", ".ac-guide-key",
                         ".ac-guide-list", ".ac-sk-row", ".ac-sk-pill",
                         ".ac-inline-link"):
            self.assertIn(selector, css, selector)

    def test_the_stylesheet_respects_reduced_motion_and_small_screens(self):
        css = source_of(os.path.join("automation", "automation.css"))
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("max-width: 380px", css)
        # Every animated property must be turned off, not merely shortened.
        reduced = css[css.index("prefers-reduced-motion"):]
        self.assertIn("transition: none;", reduced)

    def test_focus_is_always_visible_on_the_new_controls(self):
        css = source_of(os.path.join("automation", "automation.css"))
        for selector in (".ac-nav-link:focus-visible", ".ac-door:focus-visible",
                         ".ac-guide-toc-link:focus-visible"):
            self.assertIn(selector, css, selector)


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
