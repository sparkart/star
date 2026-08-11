#!/usr/bin/env python3
"""Operator-supplied background images: validation, owner-only storage, pruning.

This is the only module in the tree that accepts raw bytes from a browser, so
every rule that keeps that safe lives here rather than being spread across the
HTTP layer:

* **Nothing the caller says is believed.** No filename, MIME type, extension or
  path ever reaches this module — the API hands over bytes and nothing else. The
  content type is decided by magic bytes, then confirmed by ffprobe, then proven
  by an actual ffmpeg decode. A PNG header glued to a JPEG body fails the second
  check; a truncated file fails the third.
* **The id is ours.** 32 lowercase hex characters from `secrets`, used as the
  only handle the rest of the system ever sees. A job stores that id, never a
  path and never the bytes.
* **Bounded before decoded.** Byte size is capped before anything runs, and the
  declared pixel dimensions are capped before the decode, so a 50000x50000 PNG
  header is rejected without ever allocating for it.
* **Owner-only.** 0700 directories, 0600 files, atomic writes, under the state
  directory — never inside the project tree and never served back out.
"""

import errno
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone

from star_state import StateError

# The upload limit is deliberately its own constant: the JSON body limit in the
# API is 256 KiB and must stay there, because widening it for images would widen
# it for every control-plane endpoint too.
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MIN_UPLOAD_BYTES = 64

ASSET_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# magic prefix -> (kind, content type, stored extension, ffprobe codec name)
JPEG = ("jpeg", "image/jpeg", "jpg", "mjpeg")
PNG = ("png", "image/png", "png", "png")
WEBP = ("webp", "image/webp", "webp", "webp")
KINDS = {kind[0]: kind for kind in (JPEG, PNG, WEBP)}
ACCEPTED_CONTENT_TYPES = tuple(kind[1] for kind in (JPEG, PNG, WEBP))

# A background is composited over 1080x1920, so anything smaller than this would
# be upscaled past recognition, and anything larger is a decompression bomb
# risk rather than a useful photograph.
MIN_DIMENSION = 64
MAX_DIMENSION = 10000
MAX_PIXELS = 40_000_000

PROBE_TIMEOUT = 20
DECODE_TIMEOUT = 30

# Retention. Conservative on purpose: an asset is only ever a candidate for
# removal once it is older than RETAIN_MIN_AGE_SECONDS *and* not referenced by a
# job the caller told us to keep. Nothing here can delete the image a running
# job is about to render with.
RETAIN_MIN_AGE_SECONDS = 6 * 3600
RETAIN_MAX_AGE_SECONDS = 14 * 24 * 3600
RETAIN_MAX_COUNT = 40

SUBDIR = ("assets", "backgrounds")


class AssetError(Exception):
    """An upload was refused. Carries the HTTP status the API should return."""

    def __init__(self, message, status=400, field=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── validation ────────────────────────────────────────────────────────

def valid_asset_id(value, field="background_asset_id"):
    """Exactly 32 lowercase hex characters, or raise. Never a path fragment."""
    if not isinstance(value, str) or not ASSET_ID_RE.fullmatch(value):
        raise AssetError("asset id must be 32 lowercase hex characters", 400, field)
    return value


def new_asset_id():
    return secrets.token_hex(16)


def sniff(data):
    """Content type from magic bytes alone: ('jpeg'|'png'|'webp') or None.

    GIF and SVG are not merely absent from the allowlist, they are the two
    formats most often used to smuggle something else past an image check, so
    they fail here before any decoder sees them.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 16:
        return None
    head = bytes(data[:32])
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        chunk = head[12:16]
        if chunk not in (b"VP8 ", b"VP8L", b"VP8X"):
            return None
        # VP8X carries a feature flag byte; bit 1 means "animated", which is a
        # video wearing an image's extension.
        if chunk == b"VP8X" and len(data) > 20 and (data[20] & 0x02):
            return None
        return "webp"
    return None


def _binary(name):
    found = shutil.which(name)
    if found is None:
        raise AssetError("%s is not installed on this server, so an uploaded "
                         "image cannot be verified" % name, 503)
    return found


def _run(argv, timeout):
    """argv only — this module never builds a shell command."""
    try:
        return subprocess.run(  # noqa: S603 - argv list, shell is never used
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise AssetError("the uploaded image took too long to inspect", 400)
    except OSError as exc:
        raise AssetError("could not inspect the uploaded image: %s"
                         % (exc.strerror or exc.errno), 500)


def probe_image(path, kind):
    """Confirm with ffprobe that the bytes really are one still image.

    Returns (width, height). Raises AssetError on anything that is not a single
    video stream whose codec matches the magic bytes we already sniffed.
    """
    expected_codec = KINDS[kind][3]
    result = _run([
        _binary("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", "--", path], PROBE_TIMEOUT)
    if result.returncode != 0:
        raise AssetError("the uploaded file is not a readable image", 400)
    try:
        probed = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise AssetError("the uploaded file could not be inspected", 400)

    streams = probed.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise AssetError("the upload must contain exactly one image stream, "
                         "not audio, video or multiple streams", 400)
    stream = streams[0]
    if stream.get("codec_type") != "video":
        raise AssetError("the upload is not an image", 400)
    if stream.get("codec_name") != expected_codec:
        raise AssetError("the file contents do not match a %s image" % kind, 400)

    frames = stream.get("nb_frames")
    if frames not in (None, "", "N/A", "0", "1"):
        raise AssetError("animated files are not accepted; upload a single "
                         "still image", 400)

    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (TypeError, ValueError):
        raise AssetError("the image dimensions could not be read", 400)
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise AssetError("the image must be at least %dx%d pixels"
                         % (MIN_DIMENSION, MIN_DIMENSION), 400)
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise AssetError("the image must not exceed %d pixels on a side"
                         % MAX_DIMENSION, 400)
    if width * height > MAX_PIXELS:
        raise AssetError("the image exceeds %d megapixels"
                         % (MAX_PIXELS // 1_000_000), 400)
    return width, height


def verify_decodes(path):
    """Actually decode one frame. A header-only forgery dies here."""
    result = _run([
        _binary("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error",
        "-i", path, "-frames:v", "1", "-f", "null", "-"], DECODE_TIMEOUT)
    if result.returncode != 0:
        raise AssetError("the uploaded image could not be decoded; it may be "
                         "truncated or corrupt", 400)


# ── storage ───────────────────────────────────────────────────────────

def backgrounds_dir(state):
    """Where assets live. Resolved, never created.

    Reading must not have side effects: a dry run asks whether a job's
    background still exists, and answering that question must not create a
    directory in the state tree. `StateDir.subdir` makes directories, so it is
    reserved for `ensure_backgrounds_dir` on the upload path.
    """
    return os.path.join(state.path, *SUBDIR)


def ensure_backgrounds_dir(state):
    """The same directory, created 0700. Called only when bytes arrive."""
    return state.subdir(*SUBDIR)


def _asset_paths(state, asset_id, kind=None):
    base = os.path.join(backgrounds_dir(state), asset_id)
    if kind is None:
        return None, base + ".json"
    return base + "." + KINDS[kind][2], base + ".json"


def background_path(state, asset_id):
    """Absolute path of a stored asset, resolved from its id alone, or None.

    The id is validated as 32 hex characters before it is joined, and the join
    is then confirmed to be a direct child of the backgrounds directory, so a
    caller cannot reach any file the format check somehow let through.
    """
    try:
        valid_asset_id(asset_id)
    except AssetError:
        return None
    meta = read_meta(state, asset_id)
    if meta is None:
        return None
    kind = meta.get("kind")
    if kind not in KINDS:
        return None
    directory = backgrounds_dir(state)
    path = os.path.join(directory, "%s.%s" % (asset_id, KINDS[kind][2]))
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(directory):
        return None
    return path if os.path.isfile(path) else None


def read_meta(state, asset_id):
    """Stored metadata for an asset id, or None. Never raises on bad input."""
    try:
        valid_asset_id(asset_id)
    except AssetError:
        return None
    _binary_path, meta_path = _asset_paths(state, asset_id)
    meta = state.read_json(meta_path)
    if not isinstance(meta, dict) or meta.get("id") != asset_id:
        return None
    return meta


def public_meta(meta):
    """The subset an HTTP response may contain: no paths, ever."""
    if not isinstance(meta, dict):
        return None
    return {
        "id": meta.get("id"),
        "content_type": meta.get("content_type"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "bytes": meta.get("bytes"),
        "created_at": meta.get("created_at"),
    }


def exists(state, asset_id):
    return background_path(state, asset_id) is not None


def store_background(state, data):
    """Validate raw bytes and store them owner-only. Returns safe metadata.

    The bytes are written to a temporary file inside the assets directory (not
    /tmp: the state directory is the only place with the permissions we want)
    so ffprobe and ffmpeg can inspect a real file, and the file is only given
    its final name once both have passed.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AssetError("no image data was received", 400)
    if len(data) < MIN_UPLOAD_BYTES:
        raise AssetError("the upload is too small to be an image", 400)
    if len(data) > MAX_UPLOAD_BYTES:
        raise AssetError("the image exceeds the %d MiB limit"
                         % (MAX_UPLOAD_BYTES // (1024 * 1024)), 413)

    kind = sniff(data)
    if kind is None:
        raise AssetError("only JPEG, PNG or WebP images are accepted", 400)

    directory = ensure_backgrounds_dir(state)
    asset_id = new_asset_id()
    binary_path, meta_path = _asset_paths(state, asset_id, kind)
    staging = os.path.join(directory, ".staging-%s" % asset_id)
    try:
        state.write_bytes(staging, bytes(data))
        width, height = probe_image(staging, kind)
        verify_decodes(staging)
        os.replace(staging, binary_path)
    except BaseException:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise

    meta = {
        "id": asset_id,
        "kind": kind,
        "content_type": KINDS[kind][1],
        "width": width,
        "height": height,
        "bytes": len(data),
        "created_at": utcnow(),
    }
    try:
        state.write_json(meta_path, meta)
    except (OSError, StateError):
        try:
            os.unlink(binary_path)
        except OSError:
            pass
        raise AssetError("the image could not be stored", 500)
    return meta


def list_assets(state):
    """(id, mtime) for every stored asset, newest last. Missing dir is empty."""
    directory = backgrounds_dir(state)
    found = []
    try:
        names = os.listdir(directory)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.EACCES):
            return []
        raise
    for name in names:
        if not name.endswith(".json"):
            continue
        asset_id = name[:-len(".json")]
        if not ASSET_ID_RE.fullmatch(asset_id):
            continue
        try:
            mtime = os.stat(os.path.join(directory, name)).st_mtime
        except OSError:
            continue
        found.append((asset_id, mtime))
    found.sort(key=lambda item: item[1])
    return found


def delete_background(state, asset_id):
    """Remove an asset's bytes and metadata. True if anything was removed."""
    try:
        valid_asset_id(asset_id)
    except AssetError:
        return False
    directory = backgrounds_dir(state)
    removed = False
    for suffix in [".json"] + ["." + KINDS[k][2] for k in KINDS]:
        path = os.path.join(directory, asset_id + suffix)
        try:
            os.unlink(path)
            removed = True
        except OSError:
            continue
    return removed


def prune_backgrounds(state, keep_ids=(), now=None,
                      min_age_seconds=RETAIN_MIN_AGE_SECONDS,
                      max_age_seconds=RETAIN_MAX_AGE_SECONDS,
                      max_count=RETAIN_MAX_COUNT):
    """Bounded retention: age first, then count. Returns the ids removed.

    Three rules, in order, and every one of them is a refusal to delete:

      1. Anything in `keep_ids` is untouchable. The caller passes the assets
         referenced by queued and running jobs, so the image a job is about to
         render with can never be pruned out from under it.
      2. Anything younger than `min_age_seconds` is untouchable, which covers
         the gap between "the operator uploaded an image" and "the operator
         pressed run" without needing to know about jobs at all.
      3. Only then: drop what is older than `max_age_seconds`, and if more than
         `max_count` remain, drop the oldest until the count fits.
    """
    keep = {item for item in keep_ids if isinstance(item, str)}
    now = time.time() if now is None else now
    assets = list_assets(state)
    prunable = [(asset_id, mtime) for asset_id, mtime in assets
                if asset_id not in keep and (now - mtime) > min_age_seconds]

    removed = []
    for asset_id, mtime in list(prunable):
        if (now - mtime) > max_age_seconds:
            if delete_background(state, asset_id):
                removed.append(asset_id)
            prunable.remove((asset_id, mtime))

    surviving = len(assets) - len(removed)
    for asset_id, _mtime in prunable:  # oldest first
        if surviving <= max_count:
            break
        if delete_background(state, asset_id):
            removed.append(asset_id)
            surviving -= 1
    return removed


def audit(state):
    """Any stored asset readable beyond its owner, for the health report."""
    directory = backgrounds_dir(state)
    problems = []
    if not os.path.isdir(directory):
        return problems
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        mode = stat.S_IMODE(os.stat(full).st_mode)
        if mode & 0o077:
            problems.append({"path": "assets/backgrounds/" + name,
                             "mode": oct(mode)})
    return problems
