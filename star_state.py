#!/usr/bin/env python3
"""Credential and runtime state directory.

Everything the automation layer persists outside the project tree lives here:
provider credentials, the SQLite job database and per-job working directories.

Permissions are enforced on every write rather than only at creation, because
an operator who fixes a file by hand should not silently widen it.
"""

import errno
import json
import os
import stat
import tempfile

DEFAULT_STATE_DIR = "/var/lib/star"
DIR_MODE = 0o700
FILE_MODE = 0o600

# Credential file names are chosen by us, never by the caller, but the guard
# stays so a future provider key cannot become a path.
SAFE_NAME = set("abcdefghijklmnopqrstuvwxyz0123456789_-.")


class StateError(Exception):
    """State directory could not be prepared or used."""


def resolve_state_dir(explicit=None):
    """Pick the state directory: explicit argument, env, then production default."""
    if explicit:
        return os.path.abspath(explicit)
    from_env = os.environ.get("STAR_STATE_DIR")
    if from_env:
        return os.path.abspath(from_env)
    return DEFAULT_STATE_DIR


def _check_name(name):
    if not isinstance(name, str) or not name or len(name) > 96:
        raise StateError("invalid state file name")
    if any(ch not in SAFE_NAME for ch in name):
        raise StateError("invalid state file name")
    if name.startswith(".") or ".." in name:
        raise StateError("invalid state file name")
    return name


class StateDir:
    """A 0700 directory of 0600 files, with JSON helpers."""

    def __init__(self, path, create=True):
        self.path = os.path.abspath(path)
        if create:
            self.ensure()

    # -- directories ----------------------------------------------------
    def ensure(self):
        try:
            os.makedirs(self.path, mode=DIR_MODE, exist_ok=True)
        except OSError as exc:
            raise StateError("cannot create state directory: %s"
                             % (exc.strerror or exc.errno))
        self._harden_dir(self.path)
        for sub in ("credentials", "jobs", "oauth"):
            self.subdir(sub)
        return self.path

    def _harden_dir(self, path):
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode != DIR_MODE:
                os.chmod(path, DIR_MODE)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOENT):
                raise StateError("cannot secure state directory: %s"
                                 % (exc.strerror or exc.errno))

    def _within_root(self, path):
        """True if `path` really lives under the state root.

        Checked against realpath on both sides so a pre-existing symlink in the
        tree cannot turn a chmod of ours into a chmod of something outside it.
        """
        root = os.path.realpath(self.path)
        real = os.path.realpath(path)
        return real == root or real.startswith(root + os.sep)

    def subdir(self, *parts):
        for part in parts:
            _check_name(part)
        path = os.path.join(self.path, *parts)
        # One level at a time: os.makedirs applies `mode` to the leaf only, so
        # an intermediate directory ("assets" under assets/backgrounds) would
        # otherwise be left at 0777 & ~umask. Every component we create or pass
        # through is hardened, and nothing outside the root is touched.
        current = self.path
        for part in parts:
            current = os.path.join(current, part)
            try:
                os.makedirs(current, mode=DIR_MODE, exist_ok=True)
            except OSError as exc:
                raise StateError("cannot create %s: %s" % ("/".join(parts),
                                                           exc.strerror or exc.errno))
            if self._within_root(current):
                self._harden_dir(current)
        return path

    def job_dir(self, job_id):
        _check_name(job_id)
        return self.subdir("jobs", job_id)

    @property
    def db_path(self):
        return os.path.join(self.path, "automation.db")

    # -- secret files ---------------------------------------------------
    def credential_path(self, name):
        return os.path.join(self.subdir("credentials"), _check_name(name) + ".json")

    def has_credential(self, name):
        return os.path.isfile(self.credential_path(name))

    def write_json(self, path, payload):
        """Atomic 0600 write. The temp file is created 0600 from the start."""
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            os.fchmod(fd, FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        try:
            os.chmod(path, FILE_MODE)
        except OSError:
            pass
        return path

    def write_bytes(self, path, data):
        """Atomic 0600 write of raw bytes, same guarantees as write_json.

        Used for operator-supplied binary assets (background images), which is
        exactly why the temp file is created 0600 before a single byte of it is
        written: the file is never briefly readable by anyone else.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise StateError("state bytes must be a bytes object")
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
        try:
            os.fchmod(fd, FILE_MODE)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        try:
            os.chmod(path, FILE_MODE)
        except OSError:
            pass
        return path

    def read_json(self, path, default=None):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except (ValueError, OSError):
            return default

    def write_credential(self, name, payload):
        return self.write_json(self.credential_path(name), payload)

    def read_credential(self, name, default=None):
        return self.read_json(self.credential_path(name), default)

    def delete_credential(self, name):
        try:
            os.unlink(self.credential_path(name))
            return True
        except FileNotFoundError:
            return False

    def credential_mode(self, name):
        """Octal permission bits of a stored credential, for tests and health."""
        try:
            return stat.S_IMODE(os.stat(self.credential_path(name)).st_mode)
        except OSError:
            return None

    def dir_mode(self):
        try:
            return stat.S_IMODE(os.stat(self.path).st_mode)
        except OSError:
            return None

    def audit(self):
        """Report any state file that is readable beyond the owner."""
        problems = []
        mode = self.dir_mode()
        if mode is not None and mode & 0o077:
            problems.append({"path": "<state dir>", "mode": oct(mode)})
        creds = os.path.join(self.path, "credentials")
        if os.path.isdir(creds):
            for name in sorted(os.listdir(creds)):
                full = os.path.join(creds, name)
                if not os.path.isfile(full):
                    continue
                fmode = stat.S_IMODE(os.stat(full).st_mode)
                if fmode & 0o077:
                    problems.append({"path": "credentials/" + name, "mode": oct(fmode)})
        return problems
