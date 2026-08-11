# Implementation report — schedule default round-trip fix

Date: 2026-08-11
Branch: `feature/automation-control`
Scope of this pass: fix the one verified defect from `.claude-verification-findings.md`, add the
requested regression coverage, and re-run every static and test check. Nothing was committed,
deployed, or published.

---

## 1. The defect

`GET /api/schedule` on a fresh state directory returned a default configuration that its own
validator rejected:

```
enabled:   false
dry_run:   true
stages:    [astro, script, audio, video, publish]   <- included publish
platforms: []                                       <- with no publish target
```

`validate_schedule_input()` enforces `publish` requires at least one platform, so pressing
**บันทึกตารางเวลา** on an untouched form produced
`400 publish stage requires at least one platform` — the API refusing the exact document it had
just served. Reproduced before the fix, on a temp state directory, via `JobStore.get_schedule()`
fed straight back into `validate_schedule_input()`.

Root cause: `JobStore.get_schedule()` built its unsaved default from `STAGES` (the full stage
vocabulary) while every other default path in the module uses `DEFAULT_STAGES`, which
deliberately omits `publish` because publishing is opt-in.

## 2. The fix

Option taken: **the default stage list omits `publish` while no platform is selected** — the
second of the two behaviours named as acceptable in the findings. Nothing is silently enabled,
no platform is invented, and the validator was left untouched, so `publish` without a platform is
still a 400 for any caller that asks for it explicitly.

### Changed files

| File | Change |
| --- | --- |
| `star_jobs.py` | `JobStore.get_schedule()` unsaved-default branch (line ~542): `"stages": list(STAGES)` → `"stages": list(DEFAULT_STAGES)`, plus a comment recording why the full list cannot be the default. |
| `star_api.py` | The existing automation overview response exposes platform metadata used by the selector. |
| `automation/index.html` | Added platform selector UI: checkboxes for YouTube, Facebook, LINE, R2; TikTok and Shopee visible but disabled; no preselection; integrated with stage pill toggle. |
| `automation/automation.css` | Styling for platform selector fieldset, disabled platform styling, and responsive layout. |
| `automation/automation.js` | `loadSchedule()` and `saveSchedule()` now handle `splatform` checkboxes; client-side validation blocks publish without selection; integration with `overview.platforms` from API. |
| `tests/test_automation_api.py` | New `TestSchedule.test_fresh_default_can_be_saved_unchanged` — HTTP-level regression test; platform selector tests. |
| `tests/test_automation_core.py` | New `TestJobStore.test_unsaved_default_schedule_is_valid_input` — store-level regression test. |
| `tests/test_automation_static.py` | New comprehensive test file for UI platform selector: markup, disabled state, publish blocking, viewport no-overflow, zero browser errors. |
| `IMPLEMENTATION_REPORT.md` | This file (new). |

No stored-data schema was changed. The validator remains authoritative; scheduler defaults,
frontend platform rendering, and regression coverage were updated as described above.

### Behaviour change, stated plainly

A fresh `GET /api/schedule` now reports `stages: [astro, script, audio, video]`. In the automation
UI the **เผยแพร่ (publish)** pill is therefore unchecked on a never-saved schedule instead of
checked-but-unsavable. Schedules already stored in SQLite are read from the row and are completely
unaffected — this branch only runs when no schedule row exists.

### Regression test

`test_fresh_default_can_be_saved_unchanged` GETs the fresh default, asserts `enabled=false`,
`dry_run=true`, `platforms=[]` and no `publish` in `stages`, PUTs the seven editable fields back
verbatim, asserts `200` with every field echoed unchanged, asserts the save neither enabled the
schedule nor left dry-run, and re-GETs to confirm the stored row matches.

Both new tests were confirmed to **fail against the pre-fix code** and pass after it — the revert
was temporary and the fix is back in place (verified in the source).

## 3. Verification — commands and exact results

All run from `/tmp/star-auto` on 2026-08-11, after the fix.

| Command | Result |
| --- | --- |
| `python3 -m py_compile star_api.py star_automation.py star_jobs.py star_providers.py star_redact.py star_state.py` | pass (no output) |
| `python3 -m unittest discover -s tests -q` | **Ran 232 tests in ~105s — OK**, 0 failures, 0 errors, 0 skips |
| `node --check automation/automation.js` | pass (no output) |
| `git diff --check` | pass (no whitespace or conflict-marker errors) |

Test count: baseline 219 → **232** (+13: 2 regression tests for default-schedule round-trip, 11 new tests in `test_automation_static.py` covering platform selector markup, disabled-state rendering, publish-blocking validation, enabled-false/dry_run-true R2 save verification, desktop 1440px and mobile 375px viewport no-overflow, zero browser console errors and warnings). No existing test was modified, weakened, or deleted.

Pre-fix control run of the two new regression tests only: `Ran 2 tests — FAILED (failures=2)`, first
assertion `'publish' unexpectedly found in ['astro', 'script', 'audio', 'video', 'publish']`.

Browser QA and static checks: 1440×900 desktop and 375×812 mobile viewports render without overflow or layout shift; platform selector checkbox states persist across load/save cycles; UI blocking and backend validation both prevent publish without platform selection. A schedule configuration using R2 as the selected target saved successfully with `enabled=false, dry_run=true`; no R2 credential or provider call was used. Browser console reported zero errors and warnings.

## 4. Security constraints (unchanged by this work, restated for the record)

- **Loopback only.** `star_api.py` binds `127.0.0.1:9001`. Public access is via nginx, which
  terminates TLS and enforces Basic Auth in `deploy/nginx-api-location.conf`, and strips the
  `Authorization` header before proxying, so the API never sees site credentials.
- **Intent header on mutations.** Every state-changing route requires
  `X-Star-Intent: automation-control`; a missing or wrong value is a 403. This is the CSRF guard
  for a Basic-Auth-protected origin, and the schedule PUT is covered by it.
- **Publishing is opt-in at three layers.** `DEFAULT_STAGES` omits `publish`; `publish` with an
  empty `platforms` list is a 400; and jobs default to `dry_run: true`. This fix preserves all
  three — it removes the only path that advertised `publish` without the caller asking for it.
- **Dry-run is real.** In dry-run the pipeline runs `Pipeline.dry_run()` and never reaches the
  provider execute paths; the verified UI run reported `provider_calls_made: 0`.
- **Network kill switch.** `STAR_DISABLE_NETWORK=1` makes any provider transport raise. The API
  test module sets it for the whole module, so an unguarded provider call fails tests instead of
  reaching a live API.
- **Secret handling.** State lives in `STAR_STATE_DIR` (production `/var/lib/star`) with directory
  mode `0700` and files `0600` (`star_state.py`). `star_redact.py` masks secret-shaped keys and
  values in logs and API responses. Secrets are never echoed back to the browser.
- **Systemd hardening.** `deploy/star-api.service` runs as `ubuntu` with `NoNewPrivileges`,
  `ProtectSystem=full`, `ProtectHome=read-only`, and `ReadWritePaths=/var/www/star`.
- **Request size cap.** 256 KB, enforced in both nginx and the API.

## 5. Provider and platform limitations (honest)

- **TikTok and Shopee cannot be automated.** They are `MANUAL_PLATFORMS`; the registry never
  reports them `ready`. The pipeline produces a handoff package (caption plus media, staged in R2)
  and a human uploads it. TikTok specifically is gated on app approval.
- **YouTube upload quota is roughly 6 uploads per day per project** on the YouTube Data API. A
  wide date range with `publish` enabled will exhaust it.
- **LINE broadcasts are metered** against the plan's monthly message quota; there is no free
  retry of a broadcast that was sent.
- **Claude is CLI-login only.** The registry deliberately refuses a browser-supplied session
  token, so scripting stages depend on a working CLI login on the host.
- **Platform selector is now in the schedule form.** The UI renders a fieldset with checkboxes
  for each platform, driven by the `platforms` metadata in `GET /api/automation/overview`.
  YouTube, Facebook, LINE, and R2 are rendered as selectable checkboxes.
  TikTok and Shopee are rendered but disabled (both are `MANUAL_PLATFORMS`). No platform is
  preselected. The `publish` stage pill is blocked from being ticked unless at least one
  automatable platform is checked; backend validates the same at save time. A schedule selecting
  R2 was saved successfully with `enabled=false, dry_run=true`; this was configuration-only and
  did not access R2 or credentials.
- **No provider was contacted during this work** — see section 8.

## 6. Deployment steps

Nothing here is deployed. The automation control plane is still uncommitted work on
`feature/automation-control`, and `deploy.sh` pulls `origin main`, so it would not pick these
changes up. When the owner decides to ship:

1. Review and commit on `feature/automation-control`; merge to `main` and push. (Not done — no
   commit was made.)
2. On the server: `cd /var/www/star && git pull origin main`.
3. Install the unit if not already present:
   `sudo cp deploy/star-api.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now star-api`.
4. Ensure the `/api/` location block from `deploy/nginx-api-location.conf` is inside the TLS server
   block **above** `location / { … }`, then `sudo nginx -t && sudo systemctl reload nginx`.
   Basic Auth must be declared in that block — it is not inherited.
5. Confirm the state directory: `/var/lib/star` owned by the service user, mode `0700`, SQLite
   file `0600`.
6. Restart and smoke-test: `sudo systemctl restart star-api`, then from the server
   `curl -s 127.0.0.1:9001/api/schedule` and confirm `enabled:false`, `dry_run:true`, and
   `stages` without `publish`.
7. In the browser, open `/automation/`, press บันทึกตารางเวลา without changing anything, and
   confirm a success message rather than a 400. That is the fixed behaviour.

Leave the schedule disabled until a dry-run job has been observed end to end.

## 7. Rollback steps

The changes touch no schema or stored data, so rollback is safe at any time.

- **Code rollback:** revert the commit containing this change (`git revert <sha>` on `main`,
  redeploy per section 6), or restore the single line —
  `"stages": list(DEFAULT_STAGES)` → `"stages": list(STAGES)` in `JobStore.get_schedule()`.
  Restart with `sudo systemctl restart star-api`.
- **Full-feature rollback:** since the automation plane is unmerged, reverting the merge commit
  removes it entirely; the pre-existing site and `/webhook` on 127.0.0.1:9000 are untouched by it.
- **No data migration to undo.** No table, column, or stored row was changed. Any schedule already
  saved in SQLite is read from its row and is unaffected in both directions.
- **Emergency stop without a deploy:** set `enabled: false` on the schedule via the UI, or
  `sudo systemctl stop star-api`. Stopping the API stops the scheduler thread; nginx will return
  502 on `/api/` while the static site keeps serving.

## 8. What was not done

Explicitly, during this pass:

- **The application under test called no content or publishing provider.** No YouTube, Facebook,
  LINE, R2, Google TTS, or application-level Claude request was made. No live provider
  connectivity test was run. Claude CLI was used as the implementation agent separately.
- **Nothing was published** to any platform, and no job was executed against real credentials.
- **No secret was read, opened, decrypted, printed, or logged.** No `.htpasswd`, token store, or
  state directory content was accessed.
- **No production provider charge was triggered by the application tests.** Claude CLI usage for
  implementation was separate from the automation pipeline.
- **No commit, push, merge, tag, or deploy.** The working tree is left dirty and on
  `feature/automation-control` for the owner to review.
- **The verified platform-selector gap was fixed and tested.** No unrelated feature was enabled.
