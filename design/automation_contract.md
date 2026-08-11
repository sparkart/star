# Automation Contract Design Document

## 1. Overview
This document defines the design for the Star Content Ops automation control center. It provides:
- API contract for provider integration and job management
- Job lifecycle model and state machine
- Pipeline adapter requirements and implementation guidelines
- Scheduler configuration and behavior
- Security and credential handling specifications
- Frontend redesign requirements

All work must be performed in `/tmp/star-auto` only. No modifications to live services, credentials, or existing files in `/var/www/star` or `/home/ubuntu/affiliate` are permitted.

## 2. API Contract

### 1.1 Endpoints

| Method | Path | Description | Auth | Notes |
|--------|------|-------------|------|-------|
| GET | `/api/automation/overview` | Summary dashboard data | Same-origin + CSRF | Returns status cards, provider connections, job count |
| GET | `/api/providers` | List all configured providers | Same-origin + CSRF | Returns provider status (configured/ready/error) |
| POST | `/api/providers/configure` | Configure provider with credentials (JSON) | Same-origin + CSRF | Validates input, stores securely, returns status |
| POST | `/api/providers/test` | Test provider connectivity | Same-origin + CSRF | Returns connectivity status without exposing secrets |
| GET | `/api/jobs` | List active jobs | Same-origin + CSRF | Pagination, sorting |
| POST | `/api/jobs` | Create new job | Same-origin + CSRF | Validates input, returns job ID |
| GET | `/api/jobs/{id}` | Job details | Same-origin + CSRF | Returns full state |
| POST | `/api/jobs/{id}/cancel` | Cancel job | Same-origin + CSRF | Graceful termination |
| POST | `/api/jobs/{id}/retry` | Retry job | Same-origin + CSRF | Creates new job referencing parent |
| GET | `/api/schedule` | Get current schedule | Same-origin + CSRF | Returns schedule config |
| PUT | `/api/schedule` | Update schedule | Same-origin + CSRF | New config replaces old; validates |
| GET | `/api/oauth/youtube/start` | Start YouTube OAuth flow | Same-origin + CSRF | Returns URL and state |
| GET | `/api/oauth/youtube/callback` | OAuth callback (query params) | Same-origin + CSRF | Completes authentication, stores refresh token securely |

### 1.2 Request/Response Format
All requests must include:
- `Content-Type: application/json`
- `X-Star-Intent: automation-control` header
- CSRF token in header `X-CSRF-Token`

Responses:
- Success: HTTP 200 with JSON body
- Error: HTTP status with JSON `{ "error": "message", "status": <code>, "details": { ... } }`
- Secrets must never appear in response bodies or logs

### 1.3 Input Validation
- All string inputs must be validated for type, length, format
- Dates: YYYY-MM-DD, within 31-day window
- Days: must be from `DAYS` constant (mon-sun)
- Stages: `astro`, `script`, `audio`, `video`, `publish`
- Platforms: YouTube, Facebook, LINE, TikTok, Shopee, R2 (others as needed)
- JSON bodies must be parsed with strict schema validation

## 2. Job Lifecycle Model

### 2.1 States
- `queued`: waiting for resources
- `running`: active execution
- `succeeded`: completed successfully
- `failed`: permanent failure (e.g., missing provider)
- `cancelled`: operator requested cancellation
- `blocked`: prerequisite not met (credential, permission, etc.)

### 2.2 State Machine
```
queued -> running -> (succeeded|cancelled|failed|blocked)
running -> (succeeded|cancelled|failed|blocked)
failed -> (blocked)  # may transition to blocked if recoverable
blocked -> queued   # after fixing prerequisite
```

### 2.3 Job Attributes
- `id`: UUID
- `from_date`: YYYY-MM-DD
- `to_date`: YYYY-MM-DD (≤ 31 days from from_date)
- `days`: list of birth-day abbreviations (mon-sun)
- `stages`: ordered list of stage names
- `platforms`: subset of supported platforms
- `dry_run`: boolean
- `status`: current state
- `progress`: 0-100 integer
- `current_stage`: stage name or None
- `logs`: list of log entries (timestamp, level, message)
- `input`: JSON of input parameters
- `safe_error`: sanitized error message for display
- `created_at`, `updated_at`: timestamps

### 2.3 Concurrency Rules
- Only one active job at a time; create returns 409 with active job details
- Cancel/Retry creates new job, does not modify current running job

## 3. Pipeline Adapter Requirements

Each pipeline stage must implement a class with:
- `prerequisite_met(job_config, state) -> bool`
- `dry_run_check(job_config) -> List[str]` (list of warnings)
- `execute(job_config, state_dir) -> dict` (returns progress, logs, artifacts)
- `validate(job_config) -> List[str]` (validation errors)

### 3.1 Stage Implementations

| Stage | Requirements |
|-------|--------------|
| **astro** | Use existing deterministic Swiss Ephemeris scripts. Output per-day JSON under `content/horoscope` and raw data under `content/raw_astro`. Must produce 7 day entries (one per birth-day) with scores. |
| **script** | Use Claude CLI with `--print` and `--config-dir`. One script per date/day at `content/scripts/claude_{date}_{day}.txt`. Skip if exists unless `force=true`. Must handle retries, fallback to rule-based generator if API fails, and enforce max 300 chars. |
| **audio** | Google Cloud TTS when configured; gTTS as fallback. Output MP3 under `output/{date}/audio/`. Must accept an API key, service_account_json, or a service-account credentials path. |
| **video** | FFmpeg only, 1080x1920 MP4. Must use installed Thai font (discovered via safe search). No shell=True. Output under `output/{date}/video/`. |
| **publish** | Implement real adapters for YouTube, Facebook, LINE, R2. TikTok/Shopee must generate manual handoff package (ZIP with instructions) and never claim published. |

### 2.4 Adapter Contract (Python)

```python
class PipelineAdapter:
    def prerequisite_met(self, job_config: dict, state: dict) -> bool:
        """Check if prerequisites are satisfied."""
        ...

    def dry_run_check(self, job_config: dict) -> List[str]:
        """Return list of warnings if dry_run."""
        ...

    def execute(self, job_config: dict, state_dir: Path) -> dict:
        """Execute job, return progress dict."""
        ...

    def validate(self, job_config: dict) -> List[str]:
        """Return list of validation errors."""
        ...
```

## 4. Scheduler

### 4.1 Schedule Structure
- Persisted in `STAR_STATE_DIR/automation.db` (SQLite, WAL mode)
- Table `schedules` with columns:
  - `id` (PK)
  - `enabled` (bool)
  - `time` (HH:MM, 24h)
  - `date_offset_days` (int, 0 = today)
  - `days` (list of birth-day abbreviations)
  - `stages` (comma-separated list)
  - `platforms` (comma-separated list)
  - `dry_run` (bool)
  - `created_at`, `updated_at`

### 4.2 Scheduler Behavior
- Runs in background thread checking every 60 seconds
- Prevents duplicate runs using `last_run_date` and DB constraint
- Reads schedule config, validates, then executes jobs
- Supports cron-like expression via `time` + `date_offset_days`

## 5. Security & Credential Handling

### 5.1 Credential State Directory
- Configurable via `STAR_STATE_DIR` env var, default `/var/lib/star`
- Must be 0700, contents 0600
- All secrets stored as files with 0600 permissions
- Never expose secret values in API responses or logs

### 5.2 OAuth & Token Handling
- OAuth state stored as JSON file with `expires_at` timestamp
- PKCE flow for YouTube, Facebook, LINE
- Tokens never appear in query parameters or logs
- Refresh tokens auto-renewed when near expiry

### 5.3 Input Sanitization
- All user inputs must be sanitized
- Path traversal blocked via safe_join (already used in API)
- No shell=True in subprocess calls
- All subprocesses run with limited environment and timeout

## 6. Frontend Redesign Requirements

### 6.1 Design System
- Dark navy background (`#0A0A0A`) with gold accents (`#FFD700`)
- Material Symbols for icons (no emoji)
- Responsive layout for mobile and desktop

### 6.2 Automation Control Center UI
- **Overview**: Status cards for each provider, job count, schedule next run
- **Providers**: Cards with Connect/Configure/Test buttons; masked credential display
- **Date Range**: Input fields for from/to dates, day selector, stage selector
- **Pipeline**: Visual progress bar, current stage indicator, log stream, cancel button
- **Schedule**: Editor with time picker, date selector, stage/platform selectors, dry-run toggle
- **Publish**: Buttons for each platform, showing status (configured/ready/blocked)
- **TikTok/Shopee**: Show as "Manual Setup Required" with prerequisite list
- **Error States**: Clear messages for blocked, failed, timeout, etc.

### 6.3 Redesign `automation/index.html`
- Replace existing page with new control center UI
- Use existing CSS framework or create minimal custom CSS
- Ensure no horizontal overflow, proper mobile scaling
- All interactive elements must be keyboard accessible

### 6.4 Redesign `howto/index.html`
- Operator runbook matching actual implementation
- Sections: Quick Start, Provider Setup, Pipeline Stages, Dry Run, Production, Scheduling, Publishing Constraints, Troubleshooting, Security
- Remove stale `/shared.css` reference and DeepSeek primary claims
- Use Material Symbols for visual cues

## 7. Testing & Verification

### 7.1 Unit/Integration Tests
- Test each endpoint with temp root
- Mock provider adapters
- Verify secret redaction
- Test permission enforcement (0600/0700)
- Test invalid inputs (dates, days, stages, platforms)
- Test CSRF protection
- Test concurrency (409 on active job)
- Test schedule duplicate prevention
- Test OAuth state/PKCE flow with mocks
- Test dry-run mode (no provider calls)

### 7.2 Existing Tests
- `pytest` or `unittest` must keep all existing tests passing
- Add new tests for automation-specific features

## 8. Implementation Steps

1. Create `star_automation.py` module with adapter base class and concrete implementations
2. Implement API routes in `app.py` (Flask or FastAPI) extending existing routes
3. Create SQLite schema in `STAR_STATE_DIR/automation.db`
4. Implement scheduler loop in `scheduler.py`
5. Build frontend redesign in `/tmp/star-auto/static/automation/` with HTML/CSS/JS
6. Write comprehensive tests covering all contract aspects
7. Run full test suite, fix failures
8. Document implementation in `IMPLEMENTATION_REPORT.md`
9. Commit checkpoint and prepare for review

## 9. Checkpoint Commit

Create initial checkpoint to preserve current state before major changes:

```bash
git add .
git commit -m "chore: create automation control center worktree and design contract"
```

This checkpoint marks the baseline before implementing the automation contract.
