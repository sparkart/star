#!/usr/bin/env python3
"""Provider registry: configuration, status and connectivity tests.

Three rules hold for every provider in here:

1. A stored secret is never returned by any code path. Status responses carry
   only booleans, labels and a masked tail hint.
2. Nothing touches the network unless the operator explicitly asked for a live
   test (`live: true`) *and* the process is not running with
   `STAR_DISABLE_NETWORK=1`. Automated tests set that variable, so a regression
   that adds an unguarded call fails loudly instead of hitting a real API.
3. A provider that cannot be fully automated (TikTok, Shopee) reports
   `manual`/`semi_auto` with its exact prerequisites. It never reports `ready`.
"""

import json
import os
import re
import shutil
import subprocess

import star_redact
from star_state import StateError

# Status vocabulary shared with the frontend.
NOT_CONFIGURED = "not_configured"
CONFIGURED = "configured"
READY = "ready"
ERROR = "error"
MANUAL = "manual"

AUTOMATION_FULL = "full_auto"
AUTOMATION_SEMI = "semi_auto"
AUTOMATION_MANUAL = "manual"

CLI_TIMEOUT = 20
HTTP_TIMEOUT = 15


class ProviderError(Exception):
    """Provider configuration was rejected. Maps to HTTP 400."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


def network_disabled():
    return os.environ.get("STAR_DISABLE_NETWORK") == "1"


def _require_network(action):
    if network_disabled():
        raise ProviderError("network access is disabled in this process (%s)" % action)


def _str_field(payload, name, required=True, max_len=4096, pattern=None):
    value = payload.get(name)
    if value is None or value == "":
        if required:
            raise ProviderError("%s is required" % name, name)
        return None
    if not isinstance(value, str):
        raise ProviderError("%s must be a string" % name, name)
    value = value.strip()
    if len(value) > max_len:
        raise ProviderError("%s exceeds %d characters" % (name, max_len), name)
    if pattern and not re.match(pattern, value):
        raise ProviderError("%s has an unexpected format" % name, name)
    return value


# ── base ──────────────────────────────────────────────────────────────

class Provider:
    key = ""
    label = ""
    automation = AUTOMATION_FULL
    # Declarative form description for the frontend. `secret` fields are write
    # only: the UI must never prefill them and the API never returns them.
    fields = ()
    prerequisites = ()
    docs = ""

    def __init__(self, state):
        self.state = state

    # -- storage --------------------------------------------------------
    @property
    def credential_name(self):
        return "provider_" + self.key

    def stored(self):
        return self.state.read_credential(self.credential_name) or {}

    def save(self, payload):
        self.state.write_credential(self.credential_name, payload)

    def clear(self):
        return self.state.delete_credential(self.credential_name)

    def is_configured(self):
        return bool(self.stored())

    # -- contract -------------------------------------------------------
    def configure(self, payload):
        """Validate and persist. Returns a safe summary, never a secret."""
        raise ProviderError("%s cannot be configured through this API" % self.key)

    def status(self):
        """Safe status dict. Must not raise for a missing credential."""
        configured = self.is_configured()
        return self._status(CONFIGURED if configured else NOT_CONFIGURED,
                            "configured" if configured else "not configured yet")

    def test(self, live=False):
        """Offline validation by default; a live call only when asked."""
        return self.status()

    def prerequisite_error(self):
        """Reason this provider cannot run a publish, or None when it can."""
        state = self.status()
        if state["status"] == READY:
            return None
        return state.get("detail") or "provider is not ready"

    # -- helpers --------------------------------------------------------
    def _status(self, status, detail, **extra):
        payload = {
            "provider": self.key,
            "label": self.label,
            "automation": self.automation,
            "status": status,
            "detail": star_redact.redact_text(detail, limit=400),
            "configured": self.is_configured(),
            "fields": [dict(f) for f in self.fields],
            "prerequisites": list(self.prerequisites),
            "docs": self.docs,
        }
        payload.update(extra)
        return star_redact.redact_obj(payload)


# ── claude (CLI login, never a browser-supplied token) ────────────────

class ClaudeProvider(Provider):
    key = "claude"
    label = "Claude CLI"
    automation = AUTOMATION_FULL
    fields = ()
    prerequisites = (
        "Run `claude login` once on the server as the service user.",
        "Set CLAUDE_CONFIG_DIR if the service user's home is not writable.",
    )
    docs = "Script generation uses the logged-in Claude CLI subscription, not a per-call API key."

    #: Injectable so tests never shell out.
    runner = None

    def config_dir(self):
        return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")

    def binary(self):
        return shutil.which("claude")

    def configure(self, payload):
        # Deliberate: accepting a session token here would put a long-lived
        # credential in the browser, which the contract forbids.
        raise ProviderError(
            "Claude is authenticated by running `claude login` on the server; "
            "this API never accepts Claude session tokens.")

    def is_configured(self):
        return self.binary() is not None

    def status(self):
        binary = self.binary()
        if binary is None:
            return self._status(NOT_CONFIGURED, "claude CLI is not on PATH",
                                cost="subscription (no per-call API key)")
        config = self.config_dir()
        has_config = os.path.isdir(config)
        return self._status(
            READY if has_config else CONFIGURED,
            "claude CLI found; login state confirmed only by an explicit test"
            if has_config else "claude CLI found but no config directory yet",
            cost="subscription (no per-call API key)",
            config_dir_present=has_config)

    def test(self, live=False):
        binary = self.binary()
        if binary is None:
            return self._status(ERROR, "claude CLI is not on PATH")
        run = self.runner or _run_command
        try:
            code, out, err = run([binary, "auth", "status"], timeout=CLI_TIMEOUT)
        except FileNotFoundError:
            return self._status(ERROR, "claude CLI disappeared from PATH")
        except subprocess.TimeoutExpired:
            return self._status(ERROR, "claude auth status timed out after %ds" % CLI_TIMEOUT)
        except OSError as exc:
            return self._status(ERROR, "could not run claude CLI: %s" % (exc.strerror or "error"))
        detail = star_redact.redact_text((out or err or "").strip(), limit=300)
        if code == 0:
            return self._status(READY, detail or "claude CLI reports an active login",
                                cost="subscription (no per-call API key)")
        return self._status(ERROR, detail or "claude auth status exited %d" % code)


# ── google cloud text-to-speech ───────────────────────────────────────

SERVICE_ACCOUNT_REQUIRED = ("type", "project_id", "private_key", "client_email")
GOOGLE_TTS_API_KEY_MAX_LENGTH = 512

GOOGLE_TTS_LANGUAGE_CODE = "th-TH"
GOOGLE_TTS_DEFAULT_VOICE = "th-TH-Chirp3-HD-Kore"

# Quality tiers, most natural first. Chirp3-HD is the generative tier; Neural2
# and Standard are the older, flatter ones kept only as an escape hatch.
VOICE_TIER_CHIRP3_HD = "chirp3_hd"
VOICE_TIER_NEURAL2 = "neural2"
VOICE_TIER_STANDARD = "standard"

VOICE_TIER_NAMES = {
    VOICE_TIER_CHIRP3_HD: "Chirp3-HD",
    VOICE_TIER_NEURAL2: "Neural2",
    VOICE_TIER_STANDARD: "Standard",
}
VOICE_GENDER_TH = {"FEMALE": "หญิง", "MALE": "ชาย"}

# The allowlist. Every entry is a voice Google currently returns from
# ListVoices for th-TH, written out with its exact full name so synthesis can
# ask for it verbatim. Gender is taken from the same metadata and is the only
# gender the pipeline ever sends — a client-supplied one is never trusted.
# The first seven are the ones worth using for Star; the rest follow so the
# operator can still pick anything Google offers.
_CHIRP3_HD_RECOMMENDED = (
    ("Kore", "FEMALE", "สดใส สาว เป็นธรรมชาติ"),
    ("Aoede", "FEMALE", "โปร่งสบาย ฟังลื่น"),
    ("Despina", "FEMALE", "นุ่มลื่น"),
    ("Charon", "MALE", "ชัดเจน เล่าข้อมูลดี"),
    ("Rasalgethi", "MALE", "ให้ข้อมูล ชัดถ้อยชัดคำ"),
    ("Schedar", "MALE", "สม่ำเสมอ นิ่ง"),
    ("Puck", "MALE", "กระฉับกระเฉง สดใส"),
)
_CHIRP3_HD_OTHERS = (
    ("Achernar", "FEMALE", "นุ่มเบา"),
    ("Achird", "MALE", "เป็นมิตร"),
    ("Algenib", "MALE", "แหบต่ำ"),
    ("Algieba", "MALE", "นุ่มลื่น"),
    ("Alnilam", "MALE", "หนักแน่น"),
    ("Autonoe", "FEMALE", "สดใส"),
    ("Callirrhoe", "FEMALE", "สบาย ๆ"),
    ("Enceladus", "MALE", "เสียงลม นุ่ม"),
    ("Erinome", "FEMALE", "ชัดเจน"),
    ("Fenrir", "MALE", "ตื่นเต้น เร้าใจ"),
    ("Gacrux", "FEMALE", "ผู้ใหญ่ สุขุม"),
    ("Iapetus", "MALE", "ชัดเจน"),
    ("Laomedeia", "FEMALE", "สดใส กระฉับกระเฉง"),
    ("Leda", "FEMALE", "วัยรุ่น สดใส"),
    ("Orus", "MALE", "หนักแน่น"),
    ("Pulcherrima", "FEMALE", "กระตือรือร้น"),
    ("Sadachbia", "MALE", "มีชีวิตชีวา"),
    ("Sadaltager", "MALE", "รอบรู้ น่าเชื่อถือ"),
    ("Sulafat", "FEMALE", "อบอุ่น"),
    ("Umbriel", "MALE", "สบาย ๆ"),
    ("Vindemiatrix", "FEMALE", "อ่อนโยน"),
    ("Zephyr", "FEMALE", "สดใส"),
    ("Zubenelgenubi", "MALE", "เป็นกันเอง"),
)
_LEGACY_TIER_VOICES = (
    ("th-TH-Neural2-C", "FEMALE", VOICE_TIER_NEURAL2, "โทนกลาง ฟังง่าย"),
    ("th-TH-Standard-A", "FEMALE", VOICE_TIER_STANDARD, "พื้นฐาน"),
)


def _voice_entry(name, gender, tier, character, recommended=False):
    short = name.split("-")[-1] if tier == VOICE_TIER_CHIRP3_HD else name[len("th-TH-"):]
    label = "%s — %s %s · %s" % (short, VOICE_GENDER_TH[gender], character,
                                 VOICE_TIER_NAMES[tier])
    if recommended:
        label += " · แนะนำสำหรับ Star"
    elif tier != VOICE_TIER_CHIRP3_HD:
        label += " (เป็นธรรมชาติน้อยกว่า)"
    return {
        "name": name,
        "gender": gender,
        "tier": tier,
        "label": label,
        "recommended": recommended,
    }


GOOGLE_TTS_VOICES = tuple(
    [_voice_entry("th-TH-Chirp3-HD-" + short, gender, VOICE_TIER_CHIRP3_HD,
                  character, recommended=True)
     for short, gender, character in _CHIRP3_HD_RECOMMENDED]
    + [_voice_entry("th-TH-Chirp3-HD-" + short, gender, VOICE_TIER_CHIRP3_HD, character)
       for short, gender, character in _CHIRP3_HD_OTHERS]
    + [_voice_entry(name, gender, tier, character)
       for name, gender, tier, character in _LEGACY_TIER_VOICES])

GOOGLE_TTS_VOICE_BY_NAME = {voice["name"]: voice for voice in GOOGLE_TTS_VOICES}

# What the form renderer receives. Non-secret by construction: a voice name is
# public metadata, so it is the one Google TTS field that is safe to echo back.
GOOGLE_TTS_VOICE_OPTIONS = tuple(
    {"value": voice["name"], "label": voice["label"],
     "gender": voice["gender"], "tier": voice["tier"],
     "recommended": voice["recommended"]}
    for voice in GOOGLE_TTS_VOICES)


class GoogleTtsProvider(Provider):
    key = "google_tts"
    label = "Google Cloud Text-to-Speech"
    automation = AUTOMATION_FULL
    BASE_FIELDS = (
        {"name": "api_key", "type": "password", "write_only": True, "required": False,
         "label": "Google Cloud API key"},
        {"name": "service_account_json", "type": "json", "write_only": True, "required": False,
         "label": "Service account JSON"},
        {"name": "credentials_path", "type": "text", "write_only": False, "required": False,
         "label": "Path to an already-uploaded key (inside the state directory)"},
        {"name": "voice_name", "type": "select", "write_only": False, "required": False,
         "label": "เสียงพากย์ภาษาไทย",
         "hint": "เปลี่ยนเสียงได้โดยไม่ต้องกรอกข้อมูลรับรองใหม่ "
                 "ระบบอ่านข้อความล้วน ไม่ปรับระดับเสียงหรือความเร็ว",
         "default": GOOGLE_TTS_DEFAULT_VOICE,
         "options": GOOGLE_TTS_VOICE_OPTIONS},
    )
    CREDENTIAL_FIELDS = ("api_key", "service_account_json", "credentials_path")
    prerequisites = (
        "A Google Cloud project with the Text-to-Speech API enabled.",
        "An API key permitted to use Text-to-Speech, or a service-account key "
        "with the Cloud Text-to-Speech User role.",
    )
    docs = ("Configure exactly one API key or service-account credential. "
            "The Thai voice is a separate, non-secret setting that can be changed "
            "on its own. gTTS remains an explicit free fallback and needs no credentials.")
    VOICES_URL = "https://texttospeech.googleapis.com/v1/voices?languageCode=th-TH"
    http_get = None  # injectable for tests

    # -- voice ----------------------------------------------------------
    @property
    def fields(self):
        """Static descriptors plus the current selection.

        The renderer stays generic: it preselects `selected` for any select
        field instead of knowing what a Thai voice is.
        """
        selected = self.selected_voice()["name"]
        out = []
        for field in self.BASE_FIELDS:
            field = dict(field)
            if field["name"] == "voice_name":
                field["selected"] = selected
            out.append(field)
        return tuple(out)

    def selected_voice(self):
        """Resolved catalogue entry, never None.

        A config written before the picker existed has no `voice_name`, and a
        hand-edited one may name a voice that is no longer in the allowlist.
        Both resolve to the recommended default rather than turning a working
        provider into an unready one.
        """
        try:
            stored = self.stored() or {}
        except StateError:
            stored = {}
        return (GOOGLE_TTS_VOICE_BY_NAME.get(stored.get("voice_name"))
                or GOOGLE_TTS_VOICE_BY_NAME[GOOGLE_TTS_DEFAULT_VOICE])

    def _status(self, status, detail, **extra):
        """Every google_tts status carries the resolved voice.

        Voice name, label, gender and tier are public metadata, so they are
        safe on a `not_configured` card as well as a ready one.
        """
        voice = self.selected_voice()
        payload = {
            "selected_voice_name": voice["name"],
            "selected_voice_label": voice["label"],
            "selected_voice_gender": voice["gender"],
            "selected_voice_tier": voice["tier"],
        }
        payload.update(extra)
        return Provider._status(self, status, detail, **payload)

    @staticmethod
    def _voice_field(payload):
        value = payload.get("voice_name")
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProviderError("voice_name must be a string", "voice_name")
        value = value.strip()
        if not value:
            return None
        if value not in GOOGLE_TTS_VOICE_BY_NAME:
            raise ProviderError(
                "unknown voice_name; choose one of the %d supported %s voices"
                % (len(GOOGLE_TTS_VOICES), GOOGLE_TTS_LANGUAGE_CODE), "voice_name")
        return value

    def _supplied_credentials(self, payload):
        """Credential fields the caller actually set.

        Once a credential is stored, a blank string means "leave it alone", so
        an untouched password box cannot wipe the stored key while the operator
        is only changing the voice. On a first configure a blank value is still
        a per-field error rather than a silent no-op.
        """
        configured = self.is_configured()
        supplied = []
        for name in self.CREDENTIAL_FIELDS:
            value = payload.get(name)
            if value is None:
                continue
            if configured and isinstance(value, str) and not value.strip():
                continue
            supplied.append(name)
        return supplied

    # -- configuration --------------------------------------------------
    def configure(self, payload):
        voice_name = self._voice_field(payload)
        supplied = self._supplied_credentials(payload)
        stored = self.stored()

        if not supplied:
            # Voice-only update: the stored credential is copied across
            # untouched, so changing the voice never re-asks for the key.
            if not stored:
                raise ProviderError(
                    "provide exactly one of api_key, service_account_json, or credentials_path")
            if voice_name is None:
                raise ProviderError(
                    "nothing to update: supply a credential or a voice_name", "voice_name")
            updated = dict(stored)
            updated["voice_name"] = voice_name
            self.save(updated)
            return self.status()

        if len(supplied) != 1:
            raise ProviderError(
                "provide exactly one of api_key, service_account_json, or credentials_path")

        # A credential change keeps whatever voice was already chosen.
        if voice_name is None:
            voice_name = stored.get("voice_name")

        selected_mode = supplied[0]
        if selected_mode == "api_key":
            api_key = _str_field(payload, "api_key", max_len=GOOGLE_TTS_API_KEY_MAX_LENGTH)
            # `_str_field` strips strings, so reject a whitespace-only value too.
            if not api_key:
                raise ProviderError("api_key must not be empty", "api_key")
            self.save(self._with_voice({"mode": "api_key", "api_key": api_key}, voice_name))
            return self.status()

        raw = payload.get("service_account_json")
        if selected_mode == "service_account_json":
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raise ProviderError("service_account_json is not valid JSON",
                                        "service_account_json")
            if not isinstance(raw, dict):
                raise ProviderError("service_account_json must be a JSON object",
                                    "service_account_json")
            missing = [k for k in SERVICE_ACCOUNT_REQUIRED if not raw.get(k)]
            if missing:
                raise ProviderError(
                    "service account JSON is missing: %s" % ", ".join(missing),
                    "service_account_json")
            if raw.get("type") != "service_account":
                raise ProviderError('service account JSON "type" must be "service_account"',
                                    "service_account_json")
            key_path = os.path.join(self.state.subdir("credentials"),
                                    "google_tts_service_account.json")
            self.state.write_json(key_path, raw)
            self.save(self._with_voice({
                "mode": "service_account_json",
                "credentials_path": key_path,
                "project_id": raw.get("project_id"),
                "client_email": raw.get("client_email"),
            }, voice_name))
            return self.status()

        # Path mode: only ever inside the state directory, so this endpoint can
        # never be used to point the service at an arbitrary file on the host.
        path = _str_field(payload, "credentials_path", max_len=512)
        if not path:
            raise ProviderError("credentials_path must not be empty", "credentials_path")
        resolved = os.path.realpath(path)
        allowed_root = os.path.realpath(self.state.path)
        if not (resolved == allowed_root or resolved.startswith(allowed_root + os.sep)):
            raise ProviderError(
                "credentials_path must be inside the configured state directory",
                "credentials_path")
        if not os.path.isfile(resolved):
            raise ProviderError("credentials_path does not exist", "credentials_path")
        data = self.state.read_json(resolved) or {}
        missing = [k for k in SERVICE_ACCOUNT_REQUIRED if not data.get(k)]
        if missing:
            raise ProviderError(
                "file at credentials_path is missing: %s" % ", ".join(missing),
                "credentials_path")
        self.save(self._with_voice({
            "mode": "credentials_path",
            "credentials_path": resolved,
            "project_id": data.get("project_id"),
            "client_email": data.get("client_email"),
        }, voice_name))
        return self.status()

    @staticmethod
    def _with_voice(record, voice_name):
        """Persist the voice beside the credential, and only when there is one.

        A record without `voice_name` stays valid: it resolves to the
        recommended default on read.
        """
        if voice_name:
            record["voice_name"] = voice_name
        return record

    def credentials_path(self):
        return (self.stored() or {}).get("credentials_path")

    def status(self):
        stored = self.stored()
        if not stored:
            return self._status(NOT_CONFIGURED,
                                "no Google Cloud credential stored; "
                                "gTTS fallback is still available",
                                fallback="gTTS (free, lower quality)")
        if stored.get("mode") == "api_key":
            schema_error = self._api_key_schema_error(stored)
            if schema_error:
                return self._status(ERROR, schema_error, key_mode="api_key")
            return self._status(
                READY, "Google Cloud API key stored",
                key_mode="api_key",
                masked_hint=star_redact.mask_tail(stored["api_key"]),
                fallback="gTTS (free, lower quality)")
        path = stored.get("credentials_path")
        if not path or not os.path.isfile(path):
            return self._status(ERROR, "stored credential file is missing")
        mode = self.state.credential_mode("google_tts_service_account") \
            if stored.get("mode") == "service_account_json" else None
        return self._status(
            READY, "service account stored for project %s" % (stored.get("project_id") or "?"),
            project_id=stored.get("project_id"),
            client_email_masked=star_redact.mask_tail(stored.get("client_email") or "", 12),
            key_mode=stored.get("mode"),
            key_file_mode=oct(mode) if mode else None,
            fallback="gTTS (free, lower quality)")

    def test(self, live=False):
        stored = self.stored()
        if not stored:
            return self._status(NOT_CONFIGURED, "no Google Cloud credential stored")
        if stored.get("mode") == "api_key":
            return self._test_api_key(stored, live)
        path = stored.get("credentials_path")
        data = self.state.read_json(path) if path else None
        if not isinstance(data, dict):
            return self._status(ERROR, "stored credential file is unreadable")
        missing = [k for k in SERVICE_ACCOUNT_REQUIRED if not data.get(k)]
        if missing:
            return self._status(ERROR, "credential is missing: %s" % ", ".join(missing))
        if not live:
            # Schema-only by default. Never synthesises speech.
            return self._status(READY, "service account schema is valid (offline check only)",
                                live_test=False)
        _require_network("google_tts live test")
        try:
            # ListVoices is a free metadata call; synthesis is never invoked.
            from google.cloud import texttospeech  # noqa: WPS433
            client = texttospeech.TextToSpeechClient.from_service_account_file(path)
            voices = client.list_voices(timeout=HTTP_TIMEOUT)
            thai = [v.name for v in voices.voices if any(
                code.startswith("th") for code in v.language_codes)]
            available = self.selected_voice()["name"] in {v.name for v in voices.voices}
            return self._status(
                *self._voice_listing_result(len(voices.voices), len(thai), available),
                selected_voice_available=available, live_test=True)
        except ImportError:
            return self._status(CONFIGURED,
                                "google-cloud-texttospeech is not installed; "
                                "schema check passed", live_test=False)
        except Exception as exc:  # noqa: BLE001 - surface a safe summary only
            return self._status(ERROR, "live voice listing failed: %s"
                                % star_redact.redact_text(str(exc), limit=200), live_test=True)

    def _voice_listing_result(self, total, thai_count, available):
        """Status + detail for a metadata-only listing. Never synthesises.

        A missing voice is an error, not a footnote: the stage would otherwise
        fail later with Google's own message in the middle of a paid run.
        """
        selected = self.selected_voice()["name"]
        if not available:
            return (ERROR,
                    "listed %d voices (%d Thai) but the selected voice %s is not among "
                    "them; choose another voice" % (total, thai_count, selected))
        return (READY,
                "listed %d voices (%d Thai); selected voice %s is available; "
                "no speech synthesised" % (total, thai_count, selected))

    @staticmethod
    def _api_key_schema_error(stored):
        api_key = stored.get("api_key")
        if not isinstance(api_key, str):
            return "stored API key is not a string"
        if not api_key.strip():
            return "stored API key is empty"
        if len(api_key) > GOOGLE_TTS_API_KEY_MAX_LENGTH:
            return "stored API key exceeds %d characters" % GOOGLE_TTS_API_KEY_MAX_LENGTH
        return None

    def _test_api_key(self, stored, live):
        schema_error = self._api_key_schema_error(stored)
        if schema_error:
            return self._status(ERROR, schema_error, key_mode="api_key",
                                live_test=False)
        api_key = stored["api_key"]
        if not live:
            # Schema-only by default. The injectable transport is not touched.
            return self._status(
                READY, "API key schema is valid (offline check only)",
                key_mode="api_key", masked_hint=star_redact.mask_tail(api_key),
                live_test=False)

        _require_network("google_tts API-key live test")
        getter = self.http_get or _http_get_json
        try:
            # This verification is metadata-only. It never calls synthesis.
            data = getter(self.VOICES_URL, headers={"X-Goog-Api-Key": api_key})
            voices = data.get("voices") if isinstance(data, dict) else None
            if not isinstance(voices, list):
                raise ValueError("response did not contain a voices list")
            thai_count = 0
            names = set()
            for voice in voices:
                if not isinstance(voice, dict):
                    continue
                if isinstance(voice.get("name"), str):
                    names.add(voice["name"])
                language_codes = voice.get("languageCodes")
                if isinstance(language_codes, list) and any(
                        isinstance(code, str) and code.lower().startswith("th")
                        for code in language_codes):
                    thai_count += 1
            available = self.selected_voice()["name"] in names
            return self._status(
                *self._voice_listing_result(len(voices), thai_count, available),
                key_mode="api_key", masked_hint=star_redact.mask_tail(api_key),
                voice_count=len(voices), thai_voice_count=thai_count,
                selected_voice_available=available, live_test=True)
        except Exception as exc:  # noqa: BLE001 - return only a redacted summary
            try:
                error = str(exc)
            except Exception:  # pragma: no cover - pathological exception object
                error = exc.__class__.__name__
            # Generic pattern redaction cannot identify every possible API-key
            # shape, so remove the exact stored value before handling the text.
            error = error.replace(api_key, star_redact.MASK)
            return self._status(
                ERROR, "live voice listing failed: %s"
                % star_redact.redact_text(error, limit=200),
                key_mode="api_key", live_test=True)


# ── youtube (OAuth) ───────────────────────────────────────────────────

class YouTubeProvider(Provider):
    key = "youtube"
    label = "YouTube"
    automation = AUTOMATION_FULL
    fields = (
        {"name": "client_json", "type": "json", "write_only": True, "required": True,
         "label": "OAuth client JSON (Desktop or Web application)"},
        {"name": "redirect_uri", "type": "text", "write_only": False, "required": False,
         "label": "Redirect URI registered in the Google Cloud console"},
    )
    prerequisites = (
        "Google Cloud project with the YouTube Data API v3 enabled.",
        "OAuth consent screen configured with the youtube.upload scope.",
        "The redirect URI below registered as an authorised redirect URI.",
    )
    docs = "Uploads are quota-limited by the YouTube Data API (about 6 uploads per day per project)."

    SCOPES = ("https://www.googleapis.com/auth/youtube.upload",)
    AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

    def configure(self, payload):
        raw = payload.get("client_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise ProviderError("client_json is not valid JSON", "client_json")
        if not isinstance(raw, dict):
            raise ProviderError("client_json must be a JSON object", "client_json")
        # Google exports the client under "installed" or "web".
        block = raw.get("installed") or raw.get("web") or raw
        client_id = block.get("client_id")
        client_secret = block.get("client_secret")
        if not client_id or not client_secret:
            raise ProviderError("client_json needs client_id and client_secret", "client_json")

        redirect_uri = _str_field(payload, "redirect_uri", required=False, max_len=512)
        if redirect_uri and not redirect_uri.startswith(("http://", "https://")):
            raise ProviderError("redirect_uri must be an http(s) URL", "redirect_uri")

        stored = self.stored()
        stored.update({
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri or stored.get("redirect_uri"),
        })
        self.save(stored)
        return self.status()

    def has_refresh_token(self):
        return bool((self.stored() or {}).get("refresh_token"))

    def save_tokens(self, token_payload):
        stored = self.stored()
        if not stored:
            raise ProviderError("configure the OAuth client before completing the flow")
        refresh = token_payload.get("refresh_token")
        if refresh:
            stored["refresh_token"] = refresh
        stored["token_type"] = token_payload.get("token_type")
        stored["scope"] = token_payload.get("scope")
        stored["connected_at"] = token_payload.get("connected_at")
        self.save(stored)
        return stored

    def status(self):
        stored = self.stored()
        if not stored.get("client_id"):
            return self._status(NOT_CONFIGURED, "OAuth client JSON not uploaded yet",
                                redirect_uri=stored.get("redirect_uri"))
        if not stored.get("refresh_token"):
            return self._status(
                CONFIGURED,
                "OAuth client stored; open the connect flow to authorise the channel",
                redirect_uri=stored.get("redirect_uri"),
                client_id_masked=star_redact.mask_tail(stored.get("client_id"), 8),
                needs_authorisation=True)
        return self._status(
            READY, "channel authorised; refresh credential stored",
            redirect_uri=stored.get("redirect_uri"),
            client_id_masked=star_redact.mask_tail(stored.get("client_id"), 8),
            scope=stored.get("scope"),
            connected_at=stored.get("connected_at"),
            needs_authorisation=False)

    def test(self, live=False):
        stored = self.stored()
        if not stored.get("client_id"):
            return self._status(NOT_CONFIGURED, "OAuth client JSON not uploaded yet")
        if not stored.get("refresh_token"):
            return self._status(CONFIGURED, "no refresh credential yet; run the connect flow",
                                needs_authorisation=True)
        if not live:
            return self._status(READY, "refresh credential present (offline check only)",
                                live_test=False)
        _require_network("youtube live test")
        return self._status(
            CONFIGURED,
            "live YouTube verification is not performed automatically; "
            "run a dry-run publish to confirm readiness", live_test=False)


# ── facebook page ─────────────────────────────────────────────────────

class FacebookProvider(Provider):
    key = "facebook"
    label = "Facebook Page"
    automation = AUTOMATION_FULL
    fields = (
        {"name": "page_id", "type": "text", "write_only": False, "required": True,
         "label": "Page ID"},
        {"name": "page_access_token", "type": "password", "write_only": True, "required": True,
         "label": "Page access token (long-lived)"},
    )
    prerequisites = (
        "A Facebook Page you administer.",
        "A long-lived Page access token with pages_manage_posts and pages_read_engagement.",
        "App review is required before the token works for a Page you do not own.",
    )
    docs = "Reels publishing uses the Page video API; availability depends on the Page's eligibility."

    GRAPH = "https://graph.facebook.com/v21.0"
    http_get = None  # injectable for tests

    def configure(self, payload):
        page_id = _str_field(payload, "page_id", max_len=64, pattern=r"^[0-9A-Za-z._\-]+$")
        token = _str_field(payload, "page_access_token", max_len=1024)
        self.save({"page_id": page_id, "page_access_token": token})
        return self.status()

    def status(self):
        stored = self.stored()
        if not stored.get("page_access_token"):
            return self._status(NOT_CONFIGURED, "no page access token stored")
        return self._status(READY, "page token stored for page %s" % stored.get("page_id"),
                            page_id=stored.get("page_id"),
                            token_masked=star_redact.mask_tail(stored.get("page_access_token")))

    def test(self, live=False):
        stored = self.stored()
        if not stored.get("page_access_token"):
            return self._status(NOT_CONFIGURED, "no page access token stored")
        if not live:
            return self._status(READY, "token stored (offline check only)", live_test=False)
        _require_network("facebook live test")
        getter = self.http_get or _http_get_json
        try:
            data = getter(self.GRAPH + "/me?fields=id,name",
                          headers={"Authorization": "Bearer " + stored["page_access_token"]})
        except Exception as exc:  # noqa: BLE001
            return self._status(ERROR, "graph /me failed: %s"
                                % star_redact.redact_text(str(exc), limit=200), live_test=True)
        return self._status(READY, "graph /me returned %s" % (data.get("name") or data.get("id")),
                            live_test=True)


# ── line messaging api ────────────────────────────────────────────────

class LineProvider(Provider):
    key = "line"
    label = "LINE Messaging API"
    automation = AUTOMATION_FULL
    fields = (
        {"name": "channel_access_token", "type": "password", "write_only": True, "required": True,
         "label": "Channel access token (long-lived)"},
        {"name": "broadcast", "type": "boolean", "write_only": False, "required": False,
         "label": "Broadcast to all friends instead of pushing to a user"},
    )
    prerequisites = (
        "A LINE Official Account with the Messaging API enabled.",
        "A long-lived channel access token.",
        "Broadcast messages consume the plan's monthly message quota.",
    )
    docs = "Broadcast is quota-metered; check the remaining quota before a production run."

    INFO_URL = "https://api.line.me/v2/bot/info"
    http_get = None

    def configure(self, payload):
        token = _str_field(payload, "channel_access_token", max_len=2048)
        broadcast = payload.get("broadcast", True)
        if not isinstance(broadcast, bool):
            raise ProviderError("broadcast must be true or false", "broadcast")
        self.save({"channel_access_token": token, "broadcast": broadcast})
        return self.status()

    def status(self):
        stored = self.stored()
        if not stored.get("channel_access_token"):
            return self._status(NOT_CONFIGURED, "no channel access token stored")
        return self._status(READY, "channel access token stored",
                            broadcast=bool(stored.get("broadcast", True)),
                            token_masked=star_redact.mask_tail(stored["channel_access_token"]))

    def test(self, live=False):
        stored = self.stored()
        if not stored.get("channel_access_token"):
            return self._status(NOT_CONFIGURED, "no channel access token stored")
        if not live:
            return self._status(READY, "token stored (offline check only)", live_test=False)
        _require_network("line live test")
        getter = self.http_get or _http_get_json
        try:
            data = getter(self.INFO_URL,
                          headers={"Authorization": "Bearer " + stored["channel_access_token"]})
        except Exception as exc:  # noqa: BLE001
            return self._status(ERROR, "bot info failed: %s"
                                % star_redact.redact_text(str(exc), limit=200), live_test=True)
        return self._status(READY, "bot info returned %s"
                            % (data.get("displayName") or data.get("basicId") or "ok"),
                            live_test=True)


# ── cloudflare r2 ─────────────────────────────────────────────────────

class R2Provider(Provider):
    key = "r2"
    label = "Cloudflare R2"
    automation = AUTOMATION_FULL
    fields = (
        {"name": "account_id", "type": "text", "write_only": False, "required": True,
         "label": "Account ID"},
        {"name": "access_key_id", "type": "text", "write_only": True, "required": True,
         "label": "Access key ID"},
        {"name": "secret_access_key", "type": "password", "write_only": True, "required": True,
         "label": "Secret access key"},
        {"name": "bucket", "type": "text", "write_only": False, "required": True,
         "label": "Bucket name"},
        {"name": "public_base_url", "type": "text", "write_only": False, "required": True,
         "label": "Public base URL"},
    )
    prerequisites = (
        "An R2 bucket with an S3-compatible API token (read/write).",
        "A public base URL (custom domain or r2.dev) if the media must be reachable.",
    )
    docs = "Used to host rendered media; also the handoff location for manual platforms."

    head_bucket = None  # injectable for tests

    def configure(self, payload):
        account_id = _str_field(payload, "account_id", max_len=64,
                                pattern=r"^[0-9A-Za-z_\-]+$")
        access_key_id = _str_field(payload, "access_key_id", max_len=128)
        secret_access_key = _str_field(payload, "secret_access_key", max_len=256)
        bucket = _str_field(payload, "bucket", max_len=64,
                            pattern=r"^[0-9a-z][0-9a-z._\-]{2,62}$")
        public_base_url = _str_field(payload, "public_base_url", max_len=512)
        if not public_base_url.startswith(("http://", "https://")):
            raise ProviderError("public_base_url must be an http(s) URL", "public_base_url")
        self.save({
            "account_id": account_id,
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
            "bucket": bucket,
            "public_base_url": public_base_url.rstrip("/"),
        })
        return self.status()

    def endpoint(self):
        stored = self.stored()
        if not stored.get("account_id"):
            return None
        return "https://%s.r2.cloudflarestorage.com" % stored["account_id"]

    def status(self):
        stored = self.stored()
        if not stored.get("secret_access_key"):
            return self._status(NOT_CONFIGURED, "no R2 credentials stored")
        return self._status(READY, "bucket %s configured" % stored.get("bucket"),
                            bucket=stored.get("bucket"),
                            public_base_url=stored.get("public_base_url"),
                            endpoint=self.endpoint(),
                            access_key_masked=star_redact.mask_tail(stored["access_key_id"]))

    def test(self, live=False):
        stored = self.stored()
        if not stored.get("secret_access_key"):
            return self._status(NOT_CONFIGURED, "no R2 credentials stored")
        if not live:
            return self._status(READY, "credentials stored (offline check only)",
                                live_test=False)
        _require_network("r2 live test")
        head = self.head_bucket or _r2_head_bucket
        try:
            head(stored, self.endpoint())
        except Exception as exc:  # noqa: BLE001
            return self._status(ERROR, "HeadBucket failed: %s"
                                % star_redact.redact_text(str(exc), limit=200), live_test=True)
        return self._status(READY, "HeadBucket succeeded for %s" % stored.get("bucket"),
                            live_test=True)


# ── manual platforms ──────────────────────────────────────────────────

class ManualProvider(Provider):
    """A platform with no officially usable unattended publishing path.

    These never report `ready`: the pipeline produces a handoff package and the
    operator finishes the upload by hand.
    """

    automation = AUTOMATION_MANUAL
    fields = ()

    def configure(self, payload):
        raise ProviderError(
            "%s cannot be automated from this server; the pipeline produces a "
            "manual handoff package instead" % self.label)

    def is_configured(self):
        return False

    def status(self):
        return self._status(MANUAL, "manual upload required; the pipeline prepares a "
                                    "handoff package with caption and media",
                            handoff=True)

    def test(self, live=False):
        return self.status()

    def prerequisite_error(self):
        return ("%s has no unattended publishing path; the job produces a handoff "
                "package instead of publishing" % self.label)


class TikTokProvider(ManualProvider):
    key = "tiktok"
    label = "TikTok"
    automation = AUTOMATION_SEMI
    prerequisites = (
        "TikTok Content Posting API access requires an approved developer app.",
        "Unaudited apps can only post to private/self-only drafts.",
        "Until the app is approved, upload the rendered MP4 by hand from the handoff package.",
    )
    docs = "Reported as semi-automatic on purpose: this server never claims a TikTok post succeeded."


class ShopeeProvider(ManualProvider):
    key = "shopee"
    label = "Shopee"
    automation = AUTOMATION_MANUAL
    prerequisites = (
        "Shopee has no public video-publishing API for affiliate content.",
        "Upload the rendered MP4 and caption manually in Shopee Video / Affiliate.",
    )
    docs = "Handoff only. This server never claims a Shopee post succeeded."


PROVIDER_CLASSES = (
    ClaudeProvider, GoogleTtsProvider, YouTubeProvider,
    FacebookProvider, LineProvider, R2Provider,
    TikTokProvider, ShopeeProvider,
)
PROVIDER_KEYS = tuple(cls.key for cls in PROVIDER_CLASSES)


class ProviderRegistry:
    def __init__(self, state):
        self.state = state
        self._providers = {cls.key: cls(state) for cls in PROVIDER_CLASSES}

    def get(self, key):
        if not isinstance(key, str) or key not in self._providers:
            raise ProviderError("unknown provider; known providers: %s"
                                % " ".join(PROVIDER_KEYS), "provider")
        return self._providers[key]

    def all(self):
        return [self._providers[key] for key in PROVIDER_KEYS]

    def statuses(self):
        out = []
        for provider in self.all():
            try:
                out.append(provider.status())
            except StateError as exc:
                out.append(provider._status(ERROR, str(exc)))
            except Exception as exc:  # noqa: BLE001 - one bad provider must not 500 the page
                out.append(provider._status(
                    ERROR, "status check failed: %s"
                    % star_redact.redact_text(str(exc), limit=200)))
        return out


# ── transport helpers (only reached on an explicit live test) ─────────

def _run_command(argv, timeout=CLI_TIMEOUT, env=None):
    """Run a command with an argv list. shell=True is never used anywhere."""
    proc = subprocess.run(argv, capture_output=True, timeout=timeout,
                          env=env, check=False, text=True, errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def _http_get_json(url, headers=None):
    _require_network(url.split("?")[0])
    import urllib.request  # local import keeps the critical path light
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _r2_head_bucket(stored, endpoint):
    _require_network("r2 HeadBucket")
    import boto3  # optional dependency, only needed for a live R2 test
    client = boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=stored["access_key_id"],
        aws_secret_access_key=stored["secret_access_key"],
        region_name="auto")
    return client.head_bucket(Bucket=stored["bucket"])
