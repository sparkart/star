#!/usr/bin/env python3
"""Unit tests for star_assets: magic-byte sniffing, asset id validation,
owner-only storage and bounded retention.

Every test runs against a throwaway StateDir under a temp directory, and the
two subprocess helpers (ffprobe, ffmpeg) are either mocked out or replaced by a
guard that fails the test if anything tries to spawn them. Nothing here reads
or writes real state, and nothing touches the network.
"""

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import star_assets  # noqa: E402
import star_state  # noqa: E402


# ── signature-shaped fixtures ─────────────────────────────────────────
# Just enough bytes to be sniffable and to clear MIN_UPLOAD_BYTES; the real
# decoders never see these because probe/decode are mocked.

def jpeg_bytes(size=256):
    return (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * size)[:size]


def png_bytes(size=256):
    return (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * size)[:size]


def webp_bytes(chunk=b"VP8 ", flags=0x00, size=256):
    """RIFF....WEBP<chunk><chunk size><flags byte>, padded to `size`."""
    body = (b"RIFF" + (size - 8).to_bytes(4, "little") + b"WEBP" + chunk
            + b"\x0a\x00\x00\x00" + bytes([flags]))
    return (body + b"\x00" * size)[:size]


def gif_bytes(size=256):
    return (b"GIF89a\x01\x00\x01\x00\x00\x00\x00," + b"\x00" * size)[:size]


def svg_bytes(size=256):
    return (b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
            + b" " * size)[:size]


class TestSniff(unittest.TestCase):
    """Content type comes from magic bytes and nothing else."""

    def test_accepts_jpeg_signature(self):
        self.assertEqual(star_assets.sniff(jpeg_bytes()), "jpeg")

    def test_accepts_png_signature(self):
        self.assertEqual(star_assets.sniff(png_bytes()), "png")

    def test_accepts_static_webp_variants(self):
        for chunk in (b"VP8 ", b"VP8L", b"VP8X"):
            with self.subTest(chunk=chunk):
                self.assertEqual(star_assets.sniff(webp_bytes(chunk)), "webp")

    def test_accepts_bytearray(self):
        self.assertEqual(star_assets.sniff(bytearray(png_bytes())), "png")

    def test_rejects_animated_vp8x(self):
        """Bit 1 of the VP8X feature flags means animation: a video in an
        image's clothing, refused before any decoder is involved."""
        animated = webp_bytes(b"VP8X", flags=0x02)
        self.assertIsNone(star_assets.sniff(animated))
        # ...while the same container without the flag is a still image.
        self.assertEqual(star_assets.sniff(webp_bytes(b"VP8X", flags=0x00)),
                         "webp")

    def test_rejects_gif(self):
        self.assertIsNone(star_assets.sniff(gif_bytes()))
        self.assertIsNone(star_assets.sniff(b"GIF87a" + b"\x00" * 64))

    def test_rejects_svg(self):
        self.assertIsNone(star_assets.sniff(svg_bytes()))

    def test_rejects_random_and_malformed_input(self):
        for data in (b"\x00" * 128,
                     b"not an image at all, just text" * 4,
                     b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 64,
                     b"RIFF" + b"\x00" * 4 + b"WEBP" + b"JUNK" + b"\x00" * 64,
                     b"\xff\xd8" + b"\x00" * 64,          # truncated SOI
                     b"\x89PNG\r\n\x1a" + b"\x00" * 64):  # near-miss PNG
            with self.subTest(prefix=data[:8]):
                self.assertIsNone(star_assets.sniff(data))

    def test_rejects_short_and_non_bytes(self):
        for data in (b"", b"\xff\xd8\xff", None, "\xff\xd8\xff" * 8, 42, []):
            with self.subTest(data=repr(data)[:24]):
                self.assertIsNone(star_assets.sniff(data))


class TestValidAssetId(unittest.TestCase):
    """The id is ours: exactly 32 lowercase hex characters, never a path."""

    def test_accepts_minted_ids(self):
        for _ in range(5):
            asset_id = star_assets.new_asset_id()
            self.assertEqual(star_assets.valid_asset_id(asset_id), asset_id)

    def test_accepts_literal_lowercase_hex(self):
        asset_id = "0123456789abcdef" * 2
        self.assertEqual(star_assets.valid_asset_id(asset_id), asset_id)

    def test_rejects_uppercase(self):
        for value in ("A" * 32, "0123456789ABCDEF" * 2,
                      "0123456789abcdeF" + "0123456789abcdef"):
            with self.subTest(value=value):
                with self.assertRaises(star_assets.AssetError):
                    star_assets.valid_asset_id(value)

    def test_rejects_embedded_and_leading_newlines(self):
        base = "a" * 32
        for value in (base + "\r\n", "\n" + base, base[:-1] + "\n",
                      "a" * 16 + "\n" + "a" * 15):
            with self.subTest(value=repr(value)):
                with self.assertRaises(star_assets.AssetError):
                    star_assets.valid_asset_id(value)

    def test_rejects_trailing_newline(self):
        """KNOWN GAP: ASSET_ID_RE is applied with `match` and ends in `$`, and
        `$` also matches just before a final newline, so "<32 hex>\\n" is
        accepted and returned verbatim. star_jobs._valid_background_asset_id
        uses `fullmatch` on the same pattern and does reject it, so the two
        validators disagree about the same id. Switching this call to
        `fullmatch` (or ending the pattern in `\\Z`) closes it; this test then
        reports an unexpected success.
        """
        with self.assertRaises(star_assets.AssetError):
            star_assets.valid_asset_id("a" * 32 + "\n")

    def test_rejects_path_fragments(self):
        for value in ("../" + "a" * 29, "a" * 16 + "/" + "b" * 15,
                      "/etc/passwd", "..", "a" * 31 + "/", "./" + "a" * 30,
                      "a" * 30 + "/..", "\x00" + "a" * 31):
            with self.subTest(value=value):
                with self.assertRaises(star_assets.AssetError):
                    star_assets.valid_asset_id(value)

    def test_rejects_wrong_length_and_non_strings(self):
        for value in ("", "a" * 31, "a" * 33, "g" * 32, None, 12345,
                      b"a" * 32, ["a" * 32]):
            with self.subTest(value=repr(value)[:24]):
                with self.assertRaises(star_assets.AssetError):
                    star_assets.valid_asset_id(value)

    def test_error_carries_status_and_field(self):
        with self.assertRaises(star_assets.AssetError) as caught:
            star_assets.valid_asset_id("nope", field="thumb_asset_id")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.field, "thumb_asset_id")


class TestPublicMeta(unittest.TestCase):
    """What may cross the HTTP boundary: no paths, no internals."""

    SAFE_KEYS = {"id", "content_type", "width", "height", "bytes", "created_at"}

    def stored_meta(self):
        return {
            "id": "b" * 32,
            "kind": "png",
            "content_type": "image/png",
            "width": 1080,
            "height": 1920,
            "bytes": 4096,
            "created_at": "2026-08-12T00:00:00Z",
            "path": "/var/lib/star/assets/backgrounds/bbbb.png",
            "staging_path": "/var/lib/star/assets/backgrounds/.staging-bbbb",
            "state_dir": "/var/lib/star",
            "owner": "operator@example.com",
        }

    def test_exposes_only_the_safe_subset(self):
        public = star_assets.public_meta(self.stored_meta())
        self.assertEqual(set(public), self.SAFE_KEYS)

    def test_drops_kind_paths_and_other_internals(self):
        public = star_assets.public_meta(self.stored_meta())
        for key in ("kind", "path", "staging_path", "state_dir", "owner"):
            self.assertNotIn(key, public)

    def test_no_value_leaks_a_filesystem_path(self):
        public = star_assets.public_meta(self.stored_meta())
        for key, value in public.items():
            if isinstance(value, str):
                self.assertFalse(value.startswith("/"),
                                 "%s leaked an absolute path" % key)
                self.assertNotIn("/var/lib/star", value,
                                 "%s leaked a state path" % key)

    def test_carries_the_values_the_ui_needs(self):
        public = star_assets.public_meta(self.stored_meta())
        self.assertEqual(public["id"], "b" * 32)
        self.assertEqual(public["content_type"], "image/png")
        self.assertEqual((public["width"], public["height"]), (1080, 1920))
        self.assertEqual(public["bytes"], 4096)

    def test_returns_none_for_non_dict(self):
        for value in (None, "meta", 7, ["id"]):
            self.assertIsNone(star_assets.public_meta(value))

    def test_missing_fields_become_none_rather_than_raising(self):
        public = star_assets.public_meta({"id": "c" * 32})
        self.assertEqual(set(public), self.SAFE_KEYS)
        self.assertIsNone(public["content_type"])


class StateTemp(unittest.TestCase):
    """Temp StateDir with the image subprocesses barred from running."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="star-assets-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = star_state.StateDir(os.path.join(self.tmp, "state"))
        guard = mock.patch.object(
            star_assets, "_run",
            side_effect=AssertionError("no subprocess may run in these tests"))
        guard.start()
        self.addCleanup(guard.stop)

    # -- helpers --------------------------------------------------------
    @property
    def backgrounds(self):
        return os.path.join(self.state.path, "assets", "backgrounds")

    def store(self, data=None, dimensions=(1080, 1920)):
        """store_background with ffprobe/ffmpeg mocked out."""
        payload = jpeg_bytes() if data is None else data
        with mock.patch.object(star_assets, "probe_image",
                               return_value=dimensions) as probe, \
             mock.patch.object(star_assets, "verify_decodes",
                               return_value=None) as decode:
            meta = star_assets.store_background(self.state, payload)
        self.probe_calls = probe.call_args_list
        self.decode_calls = decode.call_args_list
        return meta

    def age(self, asset_id, seconds):
        """Backdate an asset's metadata mtime, which is what pruning reads."""
        when = time.time() - seconds
        os.utime(os.path.join(self.backgrounds, asset_id + ".json"),
                 (when, when))

    def listing(self):
        return sorted(os.listdir(self.backgrounds))


class TestStorage(StateTemp):
    """Bytes land owner-only inside the state directory, or not at all."""

    def test_stores_binary_and_metadata_under_the_state_dir(self):
        meta = self.store(png_bytes(512))
        asset_id = meta["id"]

        binary = os.path.join(self.backgrounds, asset_id + ".png")
        metadata = os.path.join(self.backgrounds, asset_id + ".json")
        self.assertTrue(os.path.isfile(binary))
        self.assertTrue(os.path.isfile(metadata))
        # Inside the state tree, and nowhere near the project tree.
        self.assertTrue(binary.startswith(self.state.path + os.sep))
        self.assertEqual(self.listing(), sorted([asset_id + ".png",
                                                 asset_id + ".json"]))

    def test_files_are_0600_and_directories_are_0700(self):
        meta = self.store()
        for name in self.listing():
            mode = stat.S_IMODE(os.stat(os.path.join(self.backgrounds,
                                                     name)).st_mode)
            self.assertEqual(mode, 0o600, "%s is not owner-only" % name)
        self.assertEqual(stat.S_IMODE(os.stat(self.backgrounds).st_mode),
                         0o700, self.backgrounds)
        self.assertEqual(star_assets.audit(self.state), [],
                         "audit found world/group-readable assets")
        self.assertEqual(meta["kind"], "jpeg")

    def test_intermediate_assets_directory_is_also_0700(self):
        """KNOWN GAP: os.makedirs applies `mode` to the leaf directory only,
        and StateDir._harden_dir chmods only the leaf, so the intermediate
        "assets" directory is left at 0777 & ~umask (0775 here, 0755 under the
        usual deployment umask) rather than the 0700 this module documents.
        The images themselves stay unreadable because backgrounds/ is 0700, so
        this is a weakened outer layer rather than an exposure; hardening every
        component in StateDir.subdir closes it. audit() only inspects files, so
        it cannot see this.
        """
        self.store()
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.state.path,
                                              "assets")).st_mode), 0o700)

    def test_metadata_records_probed_dimensions_and_byte_count(self):
        data = jpeg_bytes(400)
        meta = self.store(data, dimensions=(1280, 720))
        self.assertEqual(star_assets.valid_asset_id(meta["id"]), meta["id"])
        self.assertEqual(meta["content_type"], "image/jpeg")
        self.assertEqual((meta["width"], meta["height"]), (1280, 720))
        self.assertEqual(meta["bytes"], len(data))
        self.assertTrue(meta["created_at"].endswith("Z"))
        # Both checks ran against the staging file, inside the assets dir.
        staged = self.probe_calls[0].args[0]
        self.assertTrue(os.path.basename(staged).startswith(".staging-"))
        self.assertEqual(os.path.dirname(staged), self.backgrounds)
        self.assertEqual(self.decode_calls[0].args[0], staged)

    def test_stored_metadata_round_trips_and_publishes_safely(self):
        meta = self.store()
        stored = star_assets.read_meta(self.state, meta["id"])
        self.assertEqual(stored, meta)
        public = star_assets.public_meta(stored)
        self.assertEqual(set(public), TestPublicMeta.SAFE_KEYS)
        self.assertNotIn("kind", public)
        self.assertNotIn(self.state.path, repr(public))

    def test_background_path_resolves_only_a_valid_stored_id(self):
        meta = self.store(webp_bytes())
        asset_id = meta["id"]

        resolved = star_assets.background_path(self.state, asset_id)
        self.assertEqual(resolved,
                         os.path.join(self.backgrounds, asset_id + ".webp"))
        self.assertTrue(os.path.isfile(resolved))
        self.assertTrue(star_assets.exists(self.state, asset_id))

        for bogus in (asset_id.upper(), asset_id + "\n", "../" + asset_id,
                      "/etc/passwd", asset_id[:-1] + "/", "f" * 32, "", None,
                      os.path.join(self.backgrounds, asset_id + ".webp")):
            with self.subTest(asset_id=repr(bogus)[:32]):
                self.assertIsNone(star_assets.background_path(self.state, bogus))
                self.assertFalse(star_assets.exists(self.state, bogus))

    def test_reading_does_not_create_the_assets_directory(self):
        """A dry run asks whether a background exists; asking must not write."""
        fresh = star_state.StateDir(os.path.join(self.tmp, "fresh"))
        directory = star_assets.backgrounds_dir(fresh)
        self.assertFalse(star_assets.exists(fresh, "d" * 32))
        self.assertIsNone(star_assets.read_meta(fresh, "d" * 32))
        self.assertEqual(star_assets.list_assets(fresh), [])
        self.assertEqual(star_assets.audit(fresh), [])
        self.assertFalse(os.path.exists(directory))


class TestStorageRejection(StateTemp):
    """Refusals happen before storage, and leave nothing behind."""

    def assertNothingStored(self):
        self.assertTrue(not os.path.isdir(self.backgrounds)
                        or self.listing() == [], "files were left behind")

    def test_rejects_empty_and_undersized_uploads(self):
        for data in (b"", b"\xff\xd8\xff" + b"\x00" * 8,
                     jpeg_bytes(star_assets.MIN_UPLOAD_BYTES - 1)):
            with self.subTest(size=len(data)):
                with self.assertRaises(star_assets.AssetError) as caught:
                    star_assets.store_background(self.state, data)
                self.assertEqual(caught.exception.status, 400)
        self.assertNothingStored()

    def test_rejects_non_bytes(self):
        for data in (None, "\xff\xd8\xff" * 64, 42, ["\xff\xd8\xff"]):
            with self.subTest(data=repr(data)[:24]):
                with self.assertRaises(star_assets.AssetError):
                    star_assets.store_background(self.state, data)
        self.assertNothingStored()

    def test_oversized_upload_is_refused_with_413(self):
        oversized = jpeg_bytes(star_assets.MAX_UPLOAD_BYTES + 1)
        with self.assertRaises(star_assets.AssetError) as caught:
            star_assets.store_background(self.state, oversized)
        self.assertEqual(caught.exception.status, 413)
        self.assertNothingStored()

    def test_rejects_unsupported_formats_before_any_decoder_runs(self):
        for name, data in (("gif", gif_bytes()), ("svg", svg_bytes()),
                           ("animated_webp", webp_bytes(b"VP8X", flags=0x02)),
                           ("random", b"\x00" * 256),
                           ("text", b"just some text, not an image" * 8)):
            with self.subTest(format=name):
                with self.assertRaises(star_assets.AssetError) as caught:
                    star_assets.store_background(self.state, data)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("JPEG", caught.exception.message)
        # The _run guard in setUp proves no ffprobe/ffmpeg was spawned.
        self.assertNothingStored()

    def test_failed_probe_leaves_no_staging_or_final_files(self):
        boom = star_assets.AssetError("the uploaded file is not a readable "
                                      "image", 400)
        with mock.patch.object(star_assets, "probe_image", side_effect=boom), \
             mock.patch.object(star_assets, "verify_decodes") as decode:
            with self.assertRaises(star_assets.AssetError) as caught:
                star_assets.store_background(self.state, png_bytes())
        self.assertEqual(caught.exception.status, 400)
        decode.assert_not_called()
        self.assertEqual(self.listing(), [])

    def test_failed_decode_leaves_no_staging_or_final_files(self):
        boom = star_assets.AssetError("could not be decoded", 400)
        with mock.patch.object(star_assets, "probe_image",
                               return_value=(1080, 1920)), \
             mock.patch.object(star_assets, "verify_decodes", side_effect=boom):
            with self.assertRaises(star_assets.AssetError):
                star_assets.store_background(self.state, jpeg_bytes())
        self.assertEqual(self.listing(), [])

    def test_probe_timeout_leaves_no_staging_file(self):
        with mock.patch.object(star_assets, "probe_image",
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                star_assets.store_background(self.state, jpeg_bytes())
        self.assertEqual(self.listing(), [])


class TestPruning(StateTemp):
    """Retention is three refusals to delete, applied in order."""

    def test_keeps_recent_and_pinned_while_dropping_expired_and_excess(self):
        now = time.time()
        recent = self.store()["id"]                     # younger than min age
        pinned = self.store()["id"]
        expired = self.store()["id"]
        old_a = self.store()["id"]
        old_b = self.store()["id"]
        self.age(pinned, 30 * 24 * 3600)                # old, but referenced
        self.age(expired, 30 * 24 * 3600)               # past max age
        self.age(old_a, 24 * 3600)
        self.age(old_b, 2 * 24 * 3600)

        removed = star_assets.prune_backgrounds(
            self.state, keep_ids=[pinned], now=now, max_count=2)

        self.assertEqual(set(removed), {expired, old_a, old_b})
        for asset_id in (recent, pinned):
            self.assertTrue(star_assets.exists(self.state, asset_id))
            self.assertIsNotNone(star_assets.read_meta(self.state, asset_id))
        for asset_id in removed:
            self.assertFalse(star_assets.exists(self.state, asset_id))
            self.assertIsNone(star_assets.read_meta(self.state, asset_id))
            self.assertIsNone(star_assets.background_path(self.state, asset_id))
        self.assertEqual(self.listing(),
                         sorted([recent + ".jpg", recent + ".json",
                                 pinned + ".jpg", pinned + ".json"]))

    def test_nothing_younger_than_min_age_is_ever_removed(self):
        now = time.time()
        ids = [self.store()["id"] for _ in range(4)]
        self.age(ids[0], star_assets.RETAIN_MIN_AGE_SECONDS - 60)

        removed = star_assets.prune_backgrounds(
            self.state, now=now, max_count=1, max_age_seconds=1)

        self.assertEqual(removed, [])
        for asset_id in ids:
            self.assertTrue(star_assets.exists(self.state, asset_id))

    def test_pinned_ids_survive_even_when_long_expired(self):
        now = time.time()
        pinned = [self.store()["id"] for _ in range(3)]
        for asset_id in pinned:
            self.age(asset_id, 90 * 24 * 3600)

        removed = star_assets.prune_backgrounds(
            self.state, keep_ids=tuple(pinned), now=now, max_count=0)

        self.assertEqual(removed, [])
        self.assertEqual(len(star_assets.list_assets(self.state)), 3)

    def test_count_pruning_drops_the_oldest_first(self):
        now = time.time()
        ordered = []
        for age_days in (5, 4, 3, 2, 1):
            asset_id = self.store()["id"]
            self.age(asset_id, age_days * 24 * 3600)
            ordered.append(asset_id)  # oldest first

        removed = star_assets.prune_backgrounds(
            self.state, now=now, max_count=2, max_age_seconds=365 * 24 * 3600)

        self.assertEqual(removed, ordered[:3])
        survivors = [asset_id for asset_id, _ in
                     star_assets.list_assets(self.state)]
        self.assertEqual(sorted(survivors), sorted(ordered[3:]))

    def test_expired_assets_go_even_when_the_count_is_within_bounds(self):
        now = time.time()
        fresh = self.store()["id"]
        stale = self.store()["id"]
        self.age(stale, star_assets.RETAIN_MAX_AGE_SECONDS + 3600)

        removed = star_assets.prune_backgrounds(self.state, now=now)

        self.assertEqual(removed, [stale])
        self.assertTrue(star_assets.exists(self.state, fresh))

    def test_pruning_an_empty_or_missing_directory_is_a_no_op(self):
        fresh = star_state.StateDir(os.path.join(self.tmp, "empty"))
        self.assertEqual(star_assets.prune_backgrounds(fresh), [])
        self.assertFalse(os.path.exists(star_assets.backgrounds_dir(fresh)))
        self.assertEqual(star_assets.prune_backgrounds(self.state), [])

    def test_list_assets_ignores_foreign_names_and_orders_by_mtime(self):
        older = self.store()["id"]
        newer = self.store()["id"]
        self.age(older, 3600)
        trailing_newline = ("a" * 32) + "\n"
        for junk in ("notes.txt", "readme.json", "../escape.json",
                     ("z" * 33) + ".json", ("Z" * 32) + ".json",
                     trailing_newline + ".json"):
            path = os.path.join(self.backgrounds, os.path.basename(junk))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")

        listed = star_assets.list_assets(self.state)

        self.assertEqual([asset_id for asset_id, _ in listed], [older, newer])
        self.assertNotIn(trailing_newline, [asset_id for asset_id, _ in listed])

    def test_delete_background_refuses_invalid_ids(self):
        stored = self.store()["id"]
        for bogus in (stored.upper(), stored + "\n", "../" + stored, "", None):
            with self.subTest(asset_id=repr(bogus)[:32]):
                self.assertFalse(
                    star_assets.delete_background(self.state, bogus))
        self.assertTrue(star_assets.exists(self.state, stored))
        self.assertTrue(star_assets.delete_background(self.state, stored))
        self.assertEqual(self.listing(), [])


if __name__ == "__main__":
    unittest.main()
