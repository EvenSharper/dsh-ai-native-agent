"""An isolated Git candidate and a deliberately small, trusted verification runner.

Isolation protects the source checkout from ordinary edits; it is not an OS or
network sandbox. Execution permission must only be granted to trusted commands.
"""
from __future__ import annotations

import fnmatch
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time

from .interfaces import Workspace
from .cancellation import check_cancelled
from .schema import CommandResult, Proposal, VerificationCommand, VerificationResult


class WorkspaceError(RuntimeError):
    """A candidate cannot be safely prepared, changed, or verified."""


CONTEXT_BYTES = 120_000
FILE_BYTES = 500_000
OUTPUT_BYTES = 120_000


class _WindowsJob:
    """Keep the trusted command's descendants alive no longer than its parent."""
    def __init__(self, process: subprocess.Popen):
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", wintypes.DWORD), ("MinSet", ctypes.c_size_t),
                        ("MaxSet", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                        ("Scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("Basic", Limits), ("IO", ctypes.c_uint64 * 6),
                        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                        ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("Times", ctypes.c_int64 * 4), ("Faults", wintypes.DWORD),
                        ("Total", wintypes.DWORD), ("Active", wintypes.DWORD),
                        ("Terminated", wintypes.DWORD)]

        self._accounting = Accounting
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Could not create a verification process job")
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle))):
            self.close()
            raise OSError("Could not contain verification descendants in a Windows Job Object")

    def has_running_processes(self) -> bool:
        info = self._accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError("Could not inspect verification descendants")
        return info.Active > 0

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _matches(path: str, patterns: list[str]) -> bool:
    path = path.casefold()
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").casefold()
        if fnmatch.fnmatchcase(path, pattern):
            return True
        # '**/*.py' also includes a Python file at the repository root.
        while pattern.startswith("**/"):
            pattern = pattern[3:]
            if fnmatch.fnmatchcase(path, pattern):
                return True
    return False


def _sensitive(path: str) -> bool:
    for part in PurePosixPath(path.casefold()).parts:
        if part in {".git", ".ssh", ".aws", ".azure", ".gnupg", ".codex", ".agents"}:
            return True
        if part == ".env" or part.startswith(".env."):
            return True
        if part.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")):
            return True
        if part.endswith((".pem", ".key", ".p12", ".pfx")):
            return True
        if part in {"credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml"}:
            return True
    return False


def _always_protected(path: str) -> bool:
    return _sensitive(path) or any(
        part.startswith("system_record") or part in {
            ".agent", "agent.json", "agent.yaml", "agent.yml", "agent.toml",
            "agent_config.json", "agent-config.json", "agent.config.json", "agents.md",
        }
        for part in PurePosixPath(path.casefold()).parts
    )


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _snapshot(root: Path) -> dict[str, tuple[str, str, int]]:
    """Hash files (including .git) without following links or Windows junctions."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            item = parent / name
            if _is_link(item):
                dirs.remove(name)
                result[item.relative_to(root).as_posix()] = ("link", os.readlink(item), 0)
        for name in files:
            item = parent / name
            info = item.lstat()
            relative = item.relative_to(root).as_posix()
            if _is_link(item):
                result[relative] = ("link", os.readlink(item), 0)
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(128 * 1024), b""):
                        digest.update(chunk)
                result[relative] = ("file", digest.hexdigest(), stat.S_IMODE(info.st_mode))
            else:
                result[relative] = ("special", str(info.st_mode), 0)
    return result


class GitWorkspace(Workspace):
    def __init__(self, repo: Path, run_dir: Path, editable_paths: list[str],
                 protected_paths: list[str], commands: list[VerificationCommand],
                 allow_execution: bool = False, cancel_event: threading.Event | None = None):
        self.repo = Path(repo).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.root = self.run_dir / "worktree"
        self.worktree_path = self.root
        self.editable_paths = list(editable_paths)
        self.protected_paths = list(protected_paths)
        self.commands = [VerificationCommand(c.name, list(c.argv), c.timeout_seconds) for c in commands]
        self.allow_execution = allow_execution
        self.cancel_event = cancel_event
        self.base_commit = ""
        self._prepared = False
        self._expected = {}
        self._baseline = {}
        self._tracked: set[str] = set()
        self._non_regular: set[str] = set()
        self._candidate_paths: set[str] = set()
        self._verification_sequence = 0
        self._git_path = shutil.which("git")
        if not self._git_path:
            raise WorkspaceError("Git executable was not found")
        if not self.editable_paths:
            raise WorkspaceError("An explicit editable_paths allowlist is required")
        for command in self.commands:
            if (not isinstance(command.name, str) or not command.name.strip()
                    or not command.argv or any(not isinstance(a, str) or "\0" in a for a in command.argv)
                    or not isinstance(command.timeout_seconds, (int, float))
                    or not math.isfinite(command.timeout_seconds) or command.timeout_seconds <= 0):
                raise WorkspaceError("Invalid trusted verification command")

    def _environment(self) -> dict[str, str]:
        keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMDRIVE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        isolated_home = str(self.run_dir / "runtime-home")
        env.update({
            "HOME": isolated_home, "USERPROFILE": isolated_home,
            "APPDATA": isolated_home, "LOCALAPPDATA": isolated_home,
            "XDG_CONFIG_HOME": isolated_home, "XDG_CACHE_HOME": isolated_home,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        })
        return env

    def _git_process(self, argv, **kwargs):
        check_cancelled(self.cancel_event)
        if self.cancel_event is None:
            return subprocess.run(argv, **kwargs)
        timeout = kwargs.pop("timeout")
        deadline = time.monotonic() + timeout
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with subprocess.Popen(argv, **kwargs, **options) as process:
            try:
                while True:
                    check_cancelled(self.cancel_event)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    self._terminate(process)
                    process.wait(timeout=10)

    def _git(self, *args: str, cwd: Path | None = None,
             extra_env: dict[str, str] | None = None,
             extra_config: dict[str, str] | None = None) -> bytes:
        env = self._environment()
        env.update(extra_env or {})
        config_args = [arg for key, value in (extra_config or {}).items()
                       for arg in ("-c", f"{key}={value}")]
        try:
            result = self._git_process(
                [self._git_path, "-c", "core.hooksPath=" + str(self.run_dir / "empty-hooks"),
                 "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false", *config_args, *args],
                cwd=cwd or self.root, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"Git operation failed: {exc}") from exc
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace")[-4000:]
            raise WorkspaceError(f"Git operation failed ({args[0]}): {error.strip()}")
        return result.stdout

    def _source_line_endings(self) -> dict[str, str]:
        """Read effective text settings without importing executable Git settings.

        Windows installations commonly set autocrlf in the system Git config.
        Hiding that setting during status makes an unchanged CRLF checkout look
        dirty. Only the three text-conversion values cross into our Git runner.
        """
        env = self._environment()
        for key in ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME",
                    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
            env.pop(key, None)
            if key in os.environ:
                env[key] = os.environ[key]
        try:
            result = self._git_process(
                [self._git_path, "config", "--null", "--get-regexp", r"^core\.(autocrlf|eol|safecrlf)$"],
                cwd=self.repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=30, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"Cannot read repository line-ending settings: {exc}") from exc
        if result.returncode not in {0, 1}:
            raise WorkspaceError("Cannot read repository line-ending settings")
        config = {}
        for entry in result.stdout.decode("utf-8").split("\0"):
            if entry:
                key, _, value = entry.partition("\n")
                if key in {"core.autocrlf", "core.eol", "core.safecrlf"}:
                    config[key] = value
        return config

    def prepare(self) -> None:
        check_cancelled(self.cancel_event)
        if self._prepared:
            raise WorkspaceError("This workspace is already prepared")
        if self.run_dir == self.repo or self.run_dir.is_relative_to(self.repo):
            raise WorkspaceError("run_dir must be outside the original repository")
        if self.root.exists() or self.root.is_symlink():
            raise WorkspaceError("Candidate worktree already exists; choose a new run directory")
        if not self.repo.is_dir():
            raise WorkspaceError("Repository directory does not exist")
        top = self._git("rev-parse", "--show-toplevel", cwd=self.repo).decode().strip()
        if Path(top).resolve() != self.repo:
            raise WorkspaceError("repo must be the exact root of a Git working tree")
        self.base_commit = self._git("rev-parse", "--verify", "HEAD^{commit}", cwd=self.repo).decode().strip()
        if self._git("status", "--porcelain=v1", "--untracked-files=all", cwd=self.repo,
                     extra_config=self._source_line_endings()).strip():
            raise WorkspaceError("Original repository must be clean, including untracked files")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "runtime-home").mkdir(exist_ok=True)
        (self.run_dir / "empty-hooks").mkdir(exist_ok=True)
        # A clone avoids sharing indexes, objects, config, or refs with the source.
        self._git("clone", "--quiet", "--no-hardlinks", "--no-checkout", "--",
                  str(self.repo), str(self.root), cwd=self.run_dir)
        self._git("remote", "remove", "origin")
        self._git("checkout", "--quiet", "--detach", self.base_commit)
        self._tracked = set(self._git("ls-files", "-z").decode("utf-8").split("\0")) - {""}
        for entry in self._git("ls-files", "--stage", "-z").decode("utf-8").split("\0"):
            if entry:
                metadata, name = entry.split("\t", 1)
                if metadata.split()[0] not in {"100644", "100755"}:
                    self._non_regular.add(name)
        self._baseline = _snapshot(self.root)
        self._expected = dict(self._baseline)
        self._prepared = True

    def _require_prepared(self) -> None:
        check_cancelled(self.cancel_event)
        if not self._prepared:
            raise WorkspaceError("Call prepare() before using the candidate workspace")

    def _path(self, raw: str) -> tuple[str, Path]:
        if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw or "\0" in raw:
            raise WorkspaceError(f"Invalid repository-relative path: {raw!r}")
        parts = raw.split("/")
        reserved = {"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)],
                    *[f"lpt{i}" for i in range(1, 10)]}
        if any(part in {"", ".", ".."} or part.rstrip(" .") != part
               or part.split(".")[0].casefold() in reserved
               or any(ord(char) < 32 or char in '<>"|?*' for char in part) for part in parts):
            raise WorkspaceError(f"Unsafe repository-relative path: {raw!r}")
        path = self.root.joinpath(*parts)
        if any(raw == name or raw.startswith(name + "/") for name in self._non_regular):
            raise WorkspaceError(f"Tracked symlinks and submodules cannot be accessed: {raw}")
        for item in [self.root, *[self.root.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)]]:
            if item.exists() or item.is_symlink():
                if _is_link(item):
                    raise WorkspaceError(f"Symlink or junction access is forbidden: {raw}")
                if item != path and not item.is_dir():
                    raise WorkspaceError(f"A parent component is not a directory: {raw}")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise WorkspaceError(f"Path escapes candidate: {raw}")
        return raw, path

    def context(self) -> str:
        self._require_prepared()
        self.check_integrity()
        result = {
            "base_commit": self.base_commit,
            "proposal_semantics": "Each proposal is a complete candidate relative to base_commit; repeat every intended edit on revision.",
            "editable_paths": self.editable_paths,
            "protected_paths": self.protected_paths,
            "files": {}, "omitted": [],
        }
        used = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        for name in sorted(self._tracked):
            if _sensitive(name) or not (_matches(name, self.editable_paths)
                    or _matches(name, ["tests/**", "test/**", "**/test_*.py", "**/*_test.py"])):
                continue
            try:
                _, path = self._path(name)
                if not path.is_file() or path.stat().st_size > min(FILE_BYTES, CONTEXT_BYTES):
                    continue
                content = path.read_bytes()
                if b"\0" in content:
                    continue
                text = content.decode("utf-8")
            except (UnicodeError, OSError, WorkspaceError):
                continue
            cost = len(json.dumps({name: text}, ensure_ascii=False).encode("utf-8")) + 2
            if used + cost > CONTEXT_BYTES - 2000:
                if len(result["omitted"]) < 20:
                    result["omitted"].append(name)
                continue
            result["files"][name] = text
            used += cost
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    def stage(self, proposal: Proposal) -> None:
        self._require_prepared()
        self.check_integrity()
        changes = []
        seen: set[str] = set()
        if not proposal.changes or len(proposal.changes) > 50:
            raise WorkspaceError("A proposal must contain between 1 and 50 file changes")
        for change in proposal.changes:
            name, path = self._path(change.path)
            folded = name.casefold()
            if any(folded == previous or folded.startswith(previous + "/")
                   or previous.startswith(folded + "/") for previous in seen):
                raise WorkspaceError(f"Duplicate or overlapping change paths: {name}")
            seen.add(folded)
            if _always_protected(name) or _matches(name, self.protected_paths):
                raise WorkspaceError(f"Protected file cannot be changed: {name}")
            if not _matches(name, self.editable_paths):
                raise WorkspaceError(f"File is outside editable_paths: {name}")
            if path.exists() and not path.is_file():
                raise WorkspaceError(f"Only regular files may be changed: {name}")
            if change.content is not None and (not isinstance(change.content, str)
                    or len(change.content.encode("utf-8")) > FILE_BYTES or "\0" in change.content):
                raise WorkspaceError(f"Replacement must be bounded UTF-8 text: {name}")
            if change.content is None and name not in self._tracked:
                raise WorkspaceError(f"Cannot delete a file absent from the baseline: {name}")
            changes.append((name, path, change.content))
        # All requested paths passed validation. Reset only this independent clone.
        self._git("reset", "--quiet", "--hard", self.base_commit)
        for name in self._candidate_paths - self._tracked:
            _, path = self._path(name)
            if path.exists():
                path.unlink()
        for name, path, content in changes:
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            self._path(name)  # Recheck after creating parent directories.
            old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
            fd, temporary = tempfile.mkstemp(prefix=".candidate-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                if old_mode is not None:
                    os.chmod(temporary, old_mode)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        self._candidate_paths = {name for name, _, _ in changes}
        self._expected = _snapshot(self.root)

    def check_integrity(self) -> None:
        check_cancelled(self.cancel_event)
        self._require_prepared()
        current = _snapshot(self.root)
        changed = sorted(name for name in current.keys() | self._expected.keys()
                         if current.get(name) != self._expected.get(name))
        if changed:
            raise WorkspaceError("Candidate integrity violation: " + ", ".join(changed[:12]))

    def _patch_bytes(self) -> bytes:
        self._require_prepared()
        self.check_integrity()
        fd, index_path = tempfile.mkstemp(prefix="patch-index-", dir=self.run_dir)
        os.close(fd)
        os.unlink(index_path)  # read-tree requires an absent or valid index.
        try:
            with tempfile.TemporaryDirectory(prefix="patch-objects-", dir=self.run_dir) as objects:
                env = {"GIT_INDEX_FILE": index_path, "GIT_OBJECT_DIRECTORY": objects,
                       "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.root / ".git" / "objects")}
                self._git("read-tree", self.base_commit, extra_env=env)
                self._git("add", "--all", "--", ".", extra_env=env)
                added = [name for name in self._candidate_paths if (self.root / name).is_file()]
                if added:
                    self._git("add", "--force", "--", *sorted(added), extra_env=env)
                return self._git("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                                 "--no-renames", self.base_commit, "--", extra_env=env)
        finally:
            for temporary in (index_path, index_path + ".lock"):
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def diff(self) -> str:
        return self._patch_bytes().decode("utf-8", errors="replace")

    def export_patch(self, path: Path) -> None:
        path = Path(path).resolve()
        if path == self.repo or path.is_relative_to(self.repo) or path.is_relative_to(self.root):
            raise WorkspaceError("Export patch outside the original and candidate repositories")
        data = self._patch_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run([str(Path(os.environ.get("SYSTEMROOT", "C:\\Windows")) / "System32" / "taskkill.exe"),
                            "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()

    def _run(self, command: VerificationCommand, evidence_id: str) -> CommandResult:
        captured = bytearray()
        truncated = False
        process = None
        job = None
        timed_out = False

        def consume() -> None:
            nonlocal truncated
            try:
                while chunk := process.stdout.read(8192):
                    remaining = OUTPUT_BYTES - len(captured)
                    captured.extend(chunk[:remaining])
                    truncated |= len(chunk) > remaining
            except (OSError, ValueError):
                pass

        try:
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(command.argv, cwd=self.root, env=self._environment(),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, shell=False, **kwargs)
            if os.name == "nt":
                job = _WindowsJob(process)
            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            try:
                deadline = time.monotonic() + command.timeout_seconds
                while True:
                    check_cancelled(self.cancel_event)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command.argv, command.timeout_seconds)
                    try:
                        process.wait(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except subprocess.TimeoutExpired:
                timed_out = True
                if job:
                    job.close()
                else:
                    self._terminate(process)
                process.wait(timeout=10)
            descendants = job.has_running_processes() if job and job.handle else False
            if job:
                job.close()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, 0)
                    descendants = True
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            reader.join(timeout=2)
            if not reader.is_alive():
                process.stdout.close()
            output = captured.decode("utf-8", errors="replace")
            if truncated:
                output += "\n[output truncated]"
            if timed_out:
                output += f"\n[timeout after {command.timeout_seconds:g}s]"
            if descendants and not timed_out:
                output += "\n[verification left running descendants; terminated]"
            return CommandResult(command.name, list(command.argv), -1 if descendants else process.returncode,
                                 output, timed_out, evidence_id)
        except (OSError, subprocess.SubprocessError) as exc:
            if process and process.poll() is None:
                self._terminate(process)
            return CommandResult(command.name, list(command.argv), None, str(exc), timed_out, evidence_id)
        finally:
            if job:
                job.close()
            if process is not None:
                if process.poll() is None:
                    self._terminate(process)
                    process.wait(timeout=10)
                if process.stdout is not None:
                    process.stdout.close()

    def verify(self) -> VerificationResult:
        self._require_prepared()
        self.check_integrity()
        if not self.allow_execution:
            raise WorkspaceError("Trusted command execution requires explicit authorization")
        if not self.commands:
            raise WorkspaceError("At least one trusted verification command is required")
        self._verification_sequence += 1
        results = []
        integrity_error = ""
        for command in self.commands:
            evidence_id = f"verification:{self.run_dir.name}:{self._verification_sequence}:{command.name}"
            results.append(self._run(command, evidence_id))
            try:
                self.check_integrity()
            except WorkspaceError as exc:
                integrity_error = str(exc)
                break
        output = "\n".join(f"[{result.name}]\n{result.output}" for result in results)
        if integrity_error:
            output += "\n" + integrity_error
        if len(output.encode("utf-8")) > OUTPUT_BYTES:
            output = output.encode("utf-8")[:OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n[combined output truncated]"
            if integrity_error:
                output += "\n" + integrity_error
        ok = not integrity_error and all(result.returncode == 0 and not result.timed_out for result in results)
        return VerificationResult(ok, [command.name for command in self.commands], output, results)
