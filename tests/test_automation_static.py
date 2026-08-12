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

    def test_a_select_keeps_its_stored_choice_when_secrets_are_cleared(self):
        """Blanking a select would leave the form with no valid value, and a
        voice name is not a secret in the first place."""
        clearer = js_function_body(self.js, "clearSecretInputs")
        self.assertIn("input.dataset.kind === 'select'", clearer)
        self.assertIn("return;", clearer)

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


class TestProviderFormRenderer(unittest.TestCase):
    """The configure form is built from field descriptors, so the voice picker
    must arrive as data rather than as a branch in the page."""

    @classmethod
    def setUpClass(cls):
        cls.js = source_of(os.path.join("automation", "pages-providers.js"))
        cls.css = source_of(os.path.join("automation", "automation.css"))

    def test_select_fields_are_built_from_the_backend_options(self):
        build = js_function_body(self.js, "buildConfigForm")
        self.assertIn("field.type === 'select'", build)
        self.assertIn("createElement('select')", build)
        self.assertIn("ac-select", build)
        self.assertIn("field.options", build)
        self.assertIn("option.label || option.value", build)

    def test_the_current_choice_is_marked_on_its_option(self):
        """Preselecting by marking an option keeps the "nothing is ever
        prefilled" rule literally true for every field on the form."""
        build = js_function_body(self.js, "buildConfigForm")
        self.assertIn("field.selected || field.default", build)
        self.assertIn("choice.selected = true;", build)
        self.assertNotIn("input.value =", build)

    def test_the_renderer_hard_codes_no_provider_and_no_voice(self):
        build = js_function_body(self.js, "buildConfigForm")
        for term in ("google_tts", "voice_name", "th-TH", "Chirp3", "Kore"):
            self.assertNotIn(term, build,
                             "%s is hard coded in the provider form" % term)

    def test_no_voice_name_is_duplicated_into_the_page(self):
        for voice in star_providers.GOOGLE_TTS_VOICES:
            self.assertNotIn(voice["name"], self.js, voice["name"])

    def test_every_offered_voice_is_a_safe_select_option(self):
        field = next(f for f in star_providers.GoogleTtsProvider.BASE_FIELDS
                     if f["name"] == "voice_name")
        self.assertEqual(field["type"], "select")
        self.assertFalse(field["write_only"])
        self.assertEqual(len(field["options"]), 32)
        for option in field["options"]:
            self.assertEqual(set(option),
                             {"value", "label", "gender", "tier", "recommended"})
            self.assertIn(option["value"], star_providers.GOOGLE_TTS_VOICE_BY_NAME)

    def test_the_select_is_styled_for_the_dark_surface(self):
        self.assertIn(".ac-select", self.css)
        self.assertIn('.ac-select[aria-invalid="true"]', self.css)


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


class TestGeneratedArtifactPreview(unittest.TestCase):
    """Static accessibility, URL-safety, and responsive preview contracts."""

    @classmethod
    def setUpClass(cls):
        cls.html = source_of(os.path.join("automation", "jobs", "index.html"))
        cls.flat_html = re.sub(r"\s+", " ", cls.html)
        cls.js = source_of(os.path.join("automation", "pages-jobs.js"))
        cls.css = source_of(os.path.join("automation", "automation.css"))

    def tag_with_id(self, element_id):
        match = re.search(r'<[a-z]+[^>]*\bid="%s"[^>]*>'
                          % re.escape(element_id), self.flat_html)
        self.assertIsNotNone(match, "no element carries id=%r" % element_id)
        return match.group(0)

    def css_block(self, header):
        start = self.css.index(header)
        open_brace = self.css.index("{", start)
        depth = 0
        for index in range(open_brace, len(self.css)):
            if self.css[index] == "{":
                depth += 1
            elif self.css[index] == "}":
                depth -= 1
                if depth == 0:
                    return self.css[open_brace:index + 1]
        raise AssertionError("unbalanced braces after %r" % header)

    def test_the_page_uses_a_labelled_native_preview_dialog(self):
        dialog = self.tag_with_id("ac-preview-dialog")
        self.assertTrue(dialog.startswith("<dialog "), dialog)
        self.assertIn('aria-modal="true"', dialog)
        self.assertIn('aria-labelledby="ac-preview-title"', dialog)
        self.assertIn('aria-describedby="ac-preview-meta"', dialog)
        self.assertIn('id="ac-preview-title"', self.html)
        self.assertIn('id="ac-preview-meta"', self.html)

        close = self.tag_with_id("ac-preview-close")
        self.assertIn('type="button"', close)
        self.assertRegex(close, r'aria-label="[^"]*ปิด[^"]*"')
        self.assertIn('id="ac-preview-body"', self.html)

        original = self.tag_with_id("ac-preview-original")
        self.assertTrue(original.startswith("<a "), original)
        self.assertIn('target="_blank"', original)
        self.assertIn('rel="noopener"', original)
        self.assertIn("เปิดไฟล์ต้นฉบับ", self.html)

    def test_artifact_urls_are_exactly_job_scoped_and_bounded(self):
        """Exercise the two regexes that form the validator's static shape.

        Browser URL parsing is deliberately left to the source assertions
        below; the accepted and rejected examples exercise the path contract
        without needing a DOM-capable JavaScript harness.
        """
        body = js_function_body(self.js, "isSafeArtifactUrl")
        job_match = re.search(r"!/([^/]+)/\.test\(jobId\)", body)
        index_match = re.search(
            r"!/([^/]+)/\.test\(value\.slice\(prefix\.length\)\)", body)
        self.assertIsNotNone(job_match, "the job id has no anchored validator")
        self.assertIsNotNone(index_match, "the artifact index has no validator")
        job_pattern = re.compile(job_match.group(1))
        index_pattern = re.compile(index_match.group(1))

        def accepts(value, job_id):
            if not isinstance(value, str) or not job_pattern.fullmatch(job_id):
                return False
            prefix = "/api/jobs/%s/artifacts/" % job_id
            return value.startswith(prefix) and bool(
                index_pattern.fullmatch(value[len(prefix):]))

        job_id = "0123456789abcdef0123456789abcdef"
        for index in ("0", "1", "42", "999"):
            self.assertTrue(accepts("/api/jobs/%s/artifacts/%s" % (job_id, index),
                                    job_id), index)

        other_job = "fedcba9876543210fedcba9876543210"
        rejected = (
            "https://example.com/api/jobs/%s/artifacts/0" % job_id,
            "data:text/plain,hello",
            "javascript:alert(1)",
            "/api/jobs/%s/artifacts/0?download=1" % job_id,
            "/api/jobs/%s/artifacts/0#preview" % job_id,
            "/api/jobs/%s/artifacts/0" % other_job,
            "/api/jobs/%s/artifacts/../0" % job_id,
            "/api/jobs/%s/artifacts/%%2e%%2e/0" % job_id,
            "/api/jobs/%s/artifacts/" % job_id,
            "/api/jobs/%s/artifacts/-1" % job_id,
            "/api/jobs/%s/artifacts/00" % job_id,
            "/api/jobs/%s/artifacts/1.0" % job_id,
            "/api/jobs/%s/artifacts/1000" % job_id,
        )
        for value in rejected:
            self.assertFalse(accepts(value, job_id), value)

        self.assertIn("var prefix = '/api/jobs/' + jobId + '/artifacts/';", body)
        self.assertIn("value.indexOf(prefix) !== 0", body)
        self.assertIn("parsed.origin === window.location.origin", body)
        self.assertIn("parsed.pathname === value", body)
        self.assertIn("!parsed.search && !parsed.hash", body)

    def test_every_supported_artifact_kind_has_a_preview_branch(self):
        labels = self.js[self.js.index("var ARTIFACT_LABEL = {"):
                         self.js.index("var ARTIFACT_ICON = {")]
        self.assertEqual(set(re.findall(r"\b(image|video|audio|text|json):", labels)),
                         {"image", "video", "audio", "text", "json"})

        preview = js_function_body(self.js, "openPreview")
        for kind, element in (("image", "img"), ("video", "video"),
                              ("audio", "audio")):
            self.assertIn("artifact.kind === '%s'" % kind, preview)
            self.assertIn("document.createElement('%s')" % element, preview)
        self.assertRegex(preview, r"else\s*\{\s*previewText\(artifact\);")

        text_preview = js_function_body(self.js, "previewText")
        self.assertIn("artifact.kind === 'json'", text_preview)
        self.assertIn("JSON.stringify(JSON.parse(text), null, 2)", text_preview)
        self.assertIn("artifact.kind === 'json' ? 'application/json' : 'text/plain'",
                      text_preview)
        self.assertIn("el('pre', 'ac-preview-text')", text_preview)

    def test_dialog_close_paths_trap_and_restore_focus_and_stop_media(self):
        card = js_function_body(self.js, "artifactCard")
        self.assertIn("event.currentTarget", card,
                      "focus must return to the clicked preview control, not body")
        self.assertIn("openPreview(artifact, trigger);", card)

        wire = js_function_body(self.js, "wirePreviewDialog")
        self.assertIn("event.key === 'Escape'", wire)
        self.assertIn("event.target === dialog", wire,
                      "clicking the native backdrop must close the dialog")
        self.assertIn("addEventListener('cancel'", wire)
        self.assertGreaterEqual(wire.count("closePreview();"), 3)
        self.assertIn("trapPreviewFocus(event);", wire)
        self.assertIn("var returnTarget = previewReturnFocus;", wire)
        self.assertIn("previewReturnFocus = null;", wire)
        self.assertIn("returnTarget.isConnected", wire)
        self.assertIn("window.requestAnimationFrame", wire)
        self.assertIn("returnTarget.focus();", wire)

        trap = js_function_body(self.js, "trapPreviewFocus")
        self.assertIn("event.key !== 'Tab'", trap)
        self.assertIn("event.shiftKey", trap)
        self.assertIn("document.activeElement === first", trap)
        self.assertIn("document.activeElement === last", trap)
        self.assertIn("last.focus();", trap)
        self.assertIn("first.focus();", trap)

        close = js_function_body(self.js, "closePreview")
        self.assertIn("previewAbort.abort();", close)
        self.assertIn("AC.$$('audio, video', dialog)", close)
        self.assertIn("media.pause();", close)
        self.assertIn("dialog.close();", close)

    def test_a_superseded_text_fetch_can_neither_repaint_nor_disarm_the_dialog(self):
        """Switching previews while a text fetch is in flight: the old fetch's
        late callbacks (its abort rejection runs as a microtask after the new
        preview has installed its own controller) must not null the shared
        abort slot, and its stale response must never overwrite the body of
        whichever preview is on screen by then."""
        text_preview = js_function_body(self.js, "previewText")
        self.assertIn("var controller = new AbortController();", text_preview)
        self.assertIn("previewAbort = controller;", text_preview)
        self.assertIn("signal: controller.signal", text_preview)
        self.assertEqual(
            text_preview.count("if (previewAbort !== controller) return;"), 2,
            "both the success and the failure path must check that this "
            "fetch still owns the dialog before touching it")
        self.assertIn("if (previewAbort === controller) previewAbort = null;",
                      text_preview)
        self.assertNotIn("previewAbort = new AbortController()", text_preview)

    def test_a_superseded_close_event_cannot_clear_the_reopened_preview(self):
        """Switching straight from one artifact card to another: dialog.close()
        fires its close event from a queued task, and openPreview reopens the
        dialog synchronously before that task runs, so the stale close event
        arrives while the next preview is already on screen. The handler must
        recognise this — the dialog it finds is open again — and leave the new
        body, href, and focus target untouched, while a genuine close (dialog
        still closed when the event fires) keeps its full cleanup."""
        preview = js_function_body(self.js, "openPreview")
        reopen = preview.index("if (dialog.open) closePreview();")
        self.assertLess(reopen, preview.index("previewReturnFocus = trigger"),
                        "the old lifecycle must be closed before the new "
                        "focus target is recorded")
        self.assertLess(reopen, preview.index("dialog.showModal();"))

        wire = js_function_body(self.js, "wirePreviewDialog")
        handler = wire[wire.index("addEventListener('close'"):]
        guard = handler.index("if (dialog.open) return;")
        for cleanup in ("clear($('#ac-preview-body'));",
                        "$('#ac-preview-original').removeAttribute('href');",
                        "var returnTarget = previewReturnFocus;",
                        "previewReturnFocus = null;",
                        "returnTarget.focus();"):
            self.assertLess(guard, handler.index(cleanup),
                            "a stale close event must return before %r"
                            % cleanup)

    def test_an_unsupported_browser_never_starts_a_preview_fetch(self):
        """The showModal capability check must run before any state change:
        no fetch in flight, no focus target recorded, no body rebuilt for a
        dialog that will never open."""
        preview = js_function_body(self.js, "openPreview")
        gate = preview.index("typeof dialog.showModal !== 'function'")
        self.assertLess(gate, preview.index("previewReturnFocus = trigger"))
        self.assertLess(gate, preview.index("previewText(artifact)"))
        self.assertLess(gate, preview.index("dialog.showModal();"))

    def test_artifacts_and_dialog_fit_a_375px_viewport(self):
        self.assertIn("@media (max-width: 400px)", self.css)
        narrow = self.css_block("@media (max-width: 380px)")
        self.assertIn(".ac-artifact-grid", narrow)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", narrow)
        self.assertIn(".ac-artifact-card", narrow)
        self.assertIn("width: 100%", narrow)
        self.assertIn(".ac-preview-dialog", narrow)
        self.assertIn("width: calc(100vw - 1rem)", narrow)
        self.assertIn("max-height: calc(100dvh - .75rem)", narrow)
        self.assertIn(".ac-preview-body", narrow)
        self.assertIn("max-height: calc(100dvh - 9.5rem)", narrow)

    def test_reduced_motion_stops_preview_and_card_motion(self):
        reduced = self.css_block("@media (prefers-reduced-motion: reduce)")
        self.assertIn(".ac-artifact-card", reduced)
        self.assertIn(".ac-artifact-cue", reduced)
        self.assertIn("transition: none", reduced)
        self.assertIn(".ac-preview-dialog[open]", reduced)
        self.assertIn(".ac-preview-dialog::backdrop", reduced)
        self.assertIn("animation: none", reduced)


class TestVideoCustomisationForm(unittest.TestCase):
    """The run form's background image and overlay text controls.

    Three things have to stay true together, and only the first is checked by
    the page-aware tests above: the markup exists, the module talks to the
    real upload route with the shape that route accepts, and a dry run never
    puts a byte on the wire.  The copy is checked too, because the whole point
    of the auto mode is that the operator can tell — from the page, before
    pressing anything — that each clip gets its own generated script and that
    a custom string does not.
    """

    ACCEPTED = ("image/jpeg", "image/png", "image/webp")

    @classmethod
    def setUpClass(cls):
        cls.html = source_of(os.path.join("automation", "run", "index.html"))
        # Attributes wrap across lines in the markup; collapse before matching.
        cls.flat_html = re.sub(r"\s+", " ", cls.html)
        cls.js = source_of(os.path.join("automation", "pages-run.js"))
        cls.core = source_of(SHARED_JS)
        cls.css = source_of(os.path.join("automation", "automation.css"))

    # helpers ────────────────────────────────────────────────

    def tag_with_id(self, element_id):
        """The single opening tag carrying `id="…"`, whitespace collapsed."""
        match = re.search(r'<[a-z]+[^>]*\bid="%s"[^>]*>' % re.escape(element_id),
                          self.flat_html)
        self.assertIsNotNone(match, "no element carries id=%r" % element_id)
        return match.group(0)

    def visible_text(self, fragment):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()

    def flat(self, fragment):
        return re.sub(r"\s+", " ", fragment)

    def css_block(self, header):
        """The body of an at-rule, by brace matching from its header."""
        start = self.css.index(header)
        open_brace = self.css.index("{", start)
        depth = 0
        for index in range(open_brace, len(self.css)):
            if self.css[index] == "{":
                depth += 1
            elif self.css[index] == "}":
                depth -= 1
                if depth == 0:
                    return self.css[open_brace:index + 1]
        raise AssertionError("unbalanced braces after %r" % header)

    # markup ─────────────────────────────────────────────────

    def test_the_run_page_carries_every_customisation_control(self):
        for element_id in ("ac-bg-drop", "ac-bg-file", "ac-bg-preview",
                           "ac-bg-change", "ac-bg-remove", "ac-bg-status",
                           "ac-overlay-text"):
            self.assertIn('id="%s"' % element_id, self.html, element_id)
        # And nowhere else: an upload control on a second page would post to a
        # route that page cannot then reference in a job.
        for key, path, _module, _url in PAGES:
            if key == "run":
                continue
            self.assertNotIn('id="ac-bg-file"', source_of(path), path)

    def test_the_file_input_offers_exactly_the_three_accepted_types(self):
        """Anything wider is an upload the server will refuse after the bytes
        have already been sent."""
        import star_assets
        tag = self.tag_with_id("ac-bg-file")
        self.assertIn('type="file"', tag)
        accept = re.search(r'accept="([^"]+)"', tag)
        self.assertIsNotNone(accept, "the file input declares no accept list")
        offered = [item.strip() for item in accept.group(1).split(",")]
        self.assertEqual(offered, list(self.ACCEPTED), accept.group(1))
        self.assertEqual(tuple(offered), star_assets.ACCEPTED_CONTENT_TYPES,
                         "the picker and star_assets disagree on the types")
        for wider in ("image/*", "image/gif", "image/svg+xml", "image/heic", ".jpg"):
            self.assertNotIn(wider, accept.group(1), wider)

    def test_the_preview_controls_hang_off_the_preview_not_the_dropzone(self):
        preview = self.flat_html.index('id="ac-bg-preview"')
        closing = self.flat_html.index("</div> </div>", preview)
        panel = self.flat_html[preview:closing]
        for element_id in ("ac-bg-change", "ac-bg-remove"):
            self.assertIn('id="%s"' % element_id, panel,
                          "#%s must live inside the preview" % element_id)
        self.assertIn("hidden", self.tag_with_id("ac-bg-preview"),
                      "the preview must start hidden, before any file exists")
        self.assertNotIn(" hidden", self.tag_with_id("ac-bg-drop"))

    def test_the_two_status_regions_are_announced(self):
        self.assertIn('aria-live="polite"', self.tag_with_id("ac-bg-status"))
        self.assertIn('role="alert"', self.tag_with_id("ac-bg-error"))

    def test_the_overlay_radios_offer_exactly_the_backend_modes(self):
        group = re.findall(
            r'<input[^>]*name="overlay_text_mode"[^>]*>', self.flat_html)
        values = [re.search(r'value="([a-z]+)"', tag).group(1) for tag in group]
        self.assertEqual(values, ["auto", "custom"], values)
        self.assertEqual(tuple(values), star_jobs.OVERLAY_TEXT_MODES,
                         "the radios and star_jobs disagree on the modes")
        checked = [tag for tag in group if re.search(r"\bchecked\b", tag)]
        self.assertEqual(len(checked), 1, "exactly one mode may be preselected")
        self.assertIn('value="%s"' % star_jobs.DEFAULT_OVERLAY_TEXT_MODE, checked[0])

    def test_the_custom_textarea_stops_where_the_backend_stops(self):
        tag = self.tag_with_id("ac-overlay-text")
        limit = re.search(r'maxlength="(\d+)"', tag)
        self.assertIsNotNone(limit, "the textarea declares no maxlength")
        self.assertEqual(int(limit.group(1)), 220)
        self.assertEqual(int(limit.group(1)), star_jobs.MAX_CUSTOM_OVERLAY_TEXT,
                         "the textarea and star_jobs disagree on the ceiling")
        # A counter the field points at, and a separate polite live region so
        # the count is spoken without hijacking the typing.
        described = re.search(r'aria-describedby="([^"]+)"', tag)
        self.assertIsNotNone(described)
        self.assertIn("ac-overlay-count", described.group(1).split())
        counter = self.flat_html[self.flat_html.index('id="ac-overlay-count"'):]
        self.assertRegex(counter[:120], r">\s*0\s*/\s*220\s*<",
                         "the counter must start at 0 / 220")
        self.assertIn('role="status"', self.tag_with_id("ac-overlay-count-sr"))
        self.assertIn("hidden", self.tag_with_id("ac-overlay-custom"),
                      "the custom field must start hidden in auto mode")

    def test_the_page_says_what_each_mode_actually_draws(self):
        """The distinction this feature exists for, in the operator's words:
        auto uses each clip's own generated script, custom uses one string for
        all of them."""
        auto_at = self.flat_html.index('id="ac-overlay-mode-auto"')
        custom_at = self.flat_html.index('id="ac-overlay-mode-custom"')
        self.assertLess(auto_at, custom_at)
        auto = self.visible_text(self.flat_html[auto_at:custom_at])
        custom = self.visible_text(
            self.flat_html[custom_at:self.flat_html.index("</fieldset>", custom_at)])

        self.assertIn("บทที่ระบบสร้างขึ้นจริง", auto,
                      "auto mode must say it uses the actual generated script")
        self.assertIn("ไม่ใช่ข้อความ", auto,
                      "auto mode must say what it is not")
        self.assertIn("ทุกคลิปในงานนี้", custom,
                      "custom mode must say it applies to every clip")

        note = self.visible_text(
            self.flat_html[self.flat_html.index('id="ac-overlay-note"'):
                           self.flat_html.index("</span>",
                                                self.flat_html.index('id="ac-overlay-note"'))])
        self.assertIn("ทุกคลิปในงานนี้", note)

    def test_the_page_says_when_the_image_leaves_the_browser(self):
        card = self.visible_text(
            self.flat_html[self.flat_html.index('id="artwork"'):
                           self.flat_html.index('id="ac-bg-drop"')])
        self.assertIn("อัปโหลด", card)
        self.assertIn("เริ่มงานจริง", card,
                      "the copy must name the live run as the upload trigger")
        self.assertIn("ทุกคลิปในงานนี้", card)
        self.assertIn("JPEG", self.html)
        self.assertIn("WebP", self.html)

    # upload ─────────────────────────────────────────────────

    def test_the_upload_targets_the_registered_route(self):
        self.assertIn("'/api/assets/background'", self.js)
        self.assertIn(("POST", "/api/assets/background", True),
                      [(method, path, intent) for method, path, _h, intent
                       in star_api.AUTOMATION_ROUTES],
                      "the page posts to a route the dispatcher does not carry")

    def test_the_raw_upload_sends_the_file_as_the_body(self):
        upload = self.flat(js_function_body(self.js, "uploadBackground"))
        self.assertRegex(upload, r"fetch\(\s*(UPLOAD_PATH|'/api/assets/background')",
                         "the upload must go through fetch on the same path")
        self.assertIn("method: 'POST'", upload)
        self.assertRegex(upload, r"body:\s*file\b",
                         "the body is the file itself, not a wrapper")
        self.assertRegex(upload, r"'Content-Type':\s*file\.type",
                         "the server sniffs the declared type; it must be the file's")
        self.assertNotIn("FormData", upload)

    def test_the_raw_upload_carries_the_same_guards_as_api(self):
        """It bypasses api(), so every protection api() applies is re-applied."""
        upload = self.flat(js_function_body(self.js, "uploadBackground"))
        self.assertRegex(upload, r"headers\[AC\.INTENT_HEADER\]\s*=",
                         "a state-changing request without the intent header")
        self.assertIn("credentials: 'same-origin'", upload)
        self.assertIn("AC.ApiError", upload)
        # The header the module reads has to be something the core exports.
        self.assertRegex(self.core, r"INTENT_HEADER\s*:\s*INTENT_HEADER")

    def test_every_preview_url_is_handed_back(self):
        self.assertIn("URL.createObjectURL", self.js)
        release = js_function_body(self.js, "releaseSelection")
        self.assertIn("URL.revokeObjectURL", release,
                      "clearing a selection must revoke its object URL")
        self.assertRegex(self.js, r"addEventListener\('pagehide'[\s\S]{0,300}?"
                                  r"URL\.revokeObjectURL",
                         "an object URL outlives the document unless released")
        self.assertGreaterEqual(self.js.count("URL.revokeObjectURL"),
                                self.js.count("URL.createObjectURL"),
                                "more object URLs are created than revoked")

    def test_a_stored_asset_id_cannot_outlive_the_file_it_came_from(self):
        cached = self.flat(js_function_body(self.js, "cachedAssetId"))
        self.assertRegex(cached, r"assetFile\s*!==\s*\w+\.file",
                         "the cached id must be checked against the exact File")
        self.assertIn("return null;", cached)
        submit = self.flat(js_function_body(self.js, "submitJob"))
        self.assertRegex(submit, r"assetFile\s*=\s*\w+\.file",
                         "a successful upload must be cached against its File")
        self.assertIn("cachedAssetId()", submit,
                      "a retry must reuse the upload instead of repeating it")

    # payload ────────────────────────────────────────────────

    def test_the_job_body_carries_the_customisation_fields(self):
        body = self.flat(js_function_body(self.js, "buildBody"))
        self.assertRegex(body, r"overlay_text_mode:\s*\w+",
                         "the mode is always sent")
        self.assertRegex(body, r"if \(\w+ === 'custom'\) \w+\.custom_overlay_text = \w+;",
                         "custom text is only sent in custom mode")
        self.assertRegex(body, r"if \(!\w+ && \w+\) \w+\.background_asset_id = \w+;",
                         "the asset id must be guarded by the dry-run flag")
        assignments = re.findall(r"\.background_asset_id\s*=", body)
        self.assertEqual(len(assignments), 1,
                         "the job body must assign background_asset_id once")

    def test_a_dry_run_uploads_nothing_and_names_no_asset(self):
        """The explicit branch: with a file chosen and dry run ticked, no
        request may carry the bytes and no job may reference an id."""
        submit = self.flat(js_function_body(self.js, "submitJob"))
        self.assertRegex(submit, r"var \w+ = !\w+ && !!\w+\.file && !\w+\(\);",
                         "the upload flag must depend on the dry-run flag")
        self.assertRegex(submit, r"\w+\s*\?\s*uploadBackground\(",
                         "the upload may only run behind that flag")
        self.assertRegex(submit, r"Promise\.resolve\(\w+ \? null : \w+\(\)\)",
                         "a dry run must resolve to no asset id at all")
        calls = re.findall(r"(?<!function )uploadBackground\(", self.js)
        self.assertEqual(len(calls), 1, "exactly one call site may upload")

    def test_the_page_repeats_the_dry_run_rule_next_to_the_image(self):
        note = js_function_body(self.js, "updateBackgroundNote")
        self.assertIn("$('#ac-dry-run')", note)
        self.assertIn("ซ้อม", note)
        self.assertIn("อัปโหลด", note)

    def test_the_module_reads_the_radio_group_the_page_draws(self):
        """The defect the schedule form once shipped, in its other form: a
        group read by name that no markup declares."""
        for name in set(re.findall(r'input\[name="([a-z_]+)"\]', self.js)):
            self.assertIn('name="%s"' % name, self.html,
                          "pages-run.js reads %r, which the page never draws" % name)
        self.assertIn("overlay_text_mode", self.js)

    # styling ────────────────────────────────────────────────

    def test_the_stylesheet_covers_the_new_surfaces(self):
        for selector in (".ac-drop", ".ac-drop.is-dragover", ".ac-drop.is-locked",
                         ".ac-bg-preview", ".ac-bg-thumb", ".ac-bg-info",
                         ".ac-choices", ".ac-choice", ".ac-choice-note",
                         ".ac-overlay-custom", ".ac-overlay-textarea",
                         ".ac-counter", ".ac-count-row"):
            self.assertIn(selector, self.css, selector)

    def test_everything_toggled_by_hidden_is_actually_hidden(self):
        """The module shows and hides these with the hidden attribute; a
        display of their own would win against it."""
        for selector in (".ac-drop[hidden]", ".ac-bg-preview[hidden]",
                         ".ac-overlay-custom[hidden]"):
            self.assertIn(selector, self.css, selector)
            rule = self.css[self.css.index(selector):]
            self.assertIn("display: none", rule[:rule.index("}")], selector)

    def test_the_new_surfaces_reflow_on_a_phone(self):
        mobile = self.css_block("@media (max-width: 720px)")
        for selector in (".ac-bg-preview", ".ac-bg-thumb", ".ac-drop"):
            self.assertIn(selector, mobile,
                          "%s is not adapted for a narrow screen" % selector)
        self.assertIn("flex-direction: column", mobile)
        # Neither the thumbnail nor the dropzone may push the viewport sideways.
        self.assertIn(".ac-bg-thumb, .ac-drop { max-width: 100%; }", self.css)

    def test_the_busy_spinner_stops_for_reduced_motion(self):
        reduced = self.css_block("@media (prefers-reduced-motion: reduce)")
        self.assertIn(".ac-spin", reduced)
        self.assertIn("animation: none", reduced)
        for selector in (".ac-drop", ".ac-choice"):
            self.assertIn(selector, reduced, selector)


if __name__ == "__main__":
    unittest.main()
