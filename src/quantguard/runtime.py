from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path
from .errors import SandboxError
from .task import redact


@dataclass
class RunResult:
    returncode: int
    diagnostic: str
    timed_out: bool
    elapsed: float
    isolation: str = "os_sandbox"


def container_guard_allowed(task_dir: Path | None = None) -> bool:
    """Explicit compatibility mode only inside a locked-down Linux container.

    This validates launch prerequisites, not resistance to hostile native code.
    The external container is its security boundary; Python audits are guards.
    """
    if os.environ.get("QUANTGUARD_ISOLATION") != "container" or not sys.platform.startswith("linux"):
        return False
    if os.geteuid() == 0 or not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()):
        raise SandboxError("container mode requires a non-root Linux container")
    try:
        rows = Path("/proc/self/mountinfo").read_text().splitlines()
        roots = [row.split() for row in rows if len(row.split()) > 5 and row.split()[4] == "/"]
        readonly = bool(roots) and "ro" in roots[0][5].split(",")
        status = Path("/proc/self/status").read_text()
        nnp = any(line.split() == ["NoNewPrivs:", "1"] for line in status.splitlines())
    except OSError:
        raise SandboxError("unable to verify container launch restrictions") from None
    if not readonly or not nnp:
        raise SandboxError("container mode requires read-only root and no-new-privileges")
    if task_dir is not None and not (os.statvfs(task_dir).f_flag & os.ST_RDONLY):
        raise SandboxError("container mode requires a read-only task mount")
    return True


def clean_environment(task_dir: Path, output_dir: Path, scratch: Path, seed: int) -> dict:
    # Deliberate allowlist: no API, proxy, OAuth, credential or parent env is
    # inherited. Everything numerical uses deterministic bounded threading.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(scratch), "TMPDIR": str(scratch),
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": str(seed % (2**32)),
            "TASK_DIR": str(task_dir), "OUT_DIR": str(output_dir), "QFBENCH_SEED": str(seed),
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1", "ARROW_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1",
            "MPLCONFIGDIR": str(scratch), "XDG_CACHE_HOME": str(scratch)}


def runtime_read_paths() -> list[str]:
    roots = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = sysconfig.get_path(key)
        if value:
            roots.add(str(Path(value).resolve()))
    for value in (Path(sys.base_prefix) / "lib", Path(sys.prefix) / "pyvenv.cfg",
                  Path(sys.executable).parent, Path(sys.executable).resolve().parent):
        if value.exists():
            roots.add(str(value.resolve()))
    for value in ("/usr/lib", "/usr/local/lib", "/lib", "/lib64", "/System/Library", "/Library/Apple", "/System/Volumes/Preboot/Cryptexes/OS", "/private/var/db/dyld",
                  "/usr/share/zoneinfo", "/etc/localtime", "/etc/ld.so.cache", "/dev/urandom", "/dev/null"):
        if Path(value).exists():
            roots.add(str(Path(value).resolve()))
    return sorted(roots)


def run_candidate(program_path: Path, task, output_dir: Path, *, timeout: float,
                  memory_bytes: int = 4 * 1024**3, seed: int = 0, secrets=()) -> RunResult:
    work = program_path.parent
    scratch = work / "scratch"
    scratch.mkdir()
    output_dir.mkdir()
    worker = Path(__file__).with_name("worker.py").resolve()
    read_paths = runtime_read_paths() + [str(p) for p in task.files] + [str(worker), str(work)]
    list_paths = {str(task.root)}
    for path in task.files:
        for parent in path.parents:
            if parent.is_relative_to(task.root):
                list_paths.add(str(parent))
    config = {"read_paths": read_paths, "list_paths": sorted(list_paths), "write_paths": [str(output_dir), str(scratch)],
              "work": str(work), "program": str(program_path), "cpu_seconds": max(1, int(timeout)),
              "memory_bytes": memory_bytes, "file_limit": 64 * 1024**2}
    config["allow_container_guard"] = container_guard_allowed(task.root)
    config_path = work / "policy.json"
    config_path.write_text(json.dumps(config))
    env = clean_environment(task.root, output_dir, scratch, seed)
    command = [sys.executable, "-I", str(worker), str(config_path)]
    if sys.platform == "darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.exists():
            raise SandboxError("macOS sandbox-exec unavailable")
        def quoted(path):
            # Sandbox profile strings use UTF-8 paths, not JSON \u escapes.
            return json.dumps(path, ensure_ascii=False)
        read_rules = " ".join(f"(subpath {quoted(p)})" if Path(p).is_dir() else f"(literal {quoted(p)})" for p in read_paths)
        profile = '(version 1) (deny default) (allow sysctl-read) (allow mach-lookup) (allow process-info*) (allow file-read-metadata) '
        profile += f'(allow file-read* {read_rules} (literal {quoted(sys.executable)}) (literal {quoted(str(Path(sys.executable).resolve()))})) '
        profile += '(allow file-read-data ' + ' '.join(f'(literal {quoted(p)})' for p in list_paths) + ') '
        # sandbox-exec resolves interpreter symlinks differently across macOS
        # releases; executable file reads are still restricted by read_rules.
        profile += '(allow process-exec*) '
        profile += f'(allow file-write* (subpath {quoted(str(output_dir))}) (subpath {quoted(str(scratch))}) (literal "/dev/null"))'
        policy = work / "sandbox.sb"
        policy.write_text(profile)
        command = [str(sandbox), "-f", str(policy)] + command
        env["QUANTGUARD_OS_SANDBOX"] = "macos"
    elif not sys.platform.startswith("linux"):
        raise SandboxError("Linux Landlock/seccomp or macOS sandbox-exec required")
    started = time.monotonic()
    log_path = work / "process.log"
    timed_out = False
    with log_path.open("wb") as log:
        proc = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        finally:
            # Kill any remaining child/thread-group processes even after normal
            # termination; macOS policy and seccomp also prohibit new processes.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    with log_path.open("rb") as log:
        log.seek(max(0, log_path.stat().st_size - 6000))
        diagnostic = log.read(6000).decode("utf-8", errors="replace")
    diagnostic = redact(diagnostic, secrets)
    mode_path = work / "isolation-mode.json"
    mode = "os_sandbox"
    if mode_path.is_file() and not mode_path.is_symlink():
        try:
            value = json.loads(mode_path.read_text())
            if value in {"linux_kernel", "container_guarded", "macos_sandbox"}:
                mode = value
        except (OSError, ValueError):
            pass
    return RunResult(proc.returncode, diagnostic, timed_out, time.monotonic() - started, mode)
