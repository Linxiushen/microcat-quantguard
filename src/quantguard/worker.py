"""Isolated candidate bootstrap; invoked directly with Python -I.

Linux uses kernel Landlock file policy plus seccomp network/process denial.
macOS receives a sandbox-exec profile from the parent. Absence of kernel
support is a failure, not a switch to an insecure Python-only sandbox.
"""
import ctypes
import errno
import json
import os
import platform
import resource
import runpy
import sys


def landlock(read_paths, write_paths, list_paths=()):
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise RuntimeError("Landlock is required")
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class Rule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13
    if abi >= 3:
        handled |= 1 << 14
    attr = Ruleset(handled)
    ruleset = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        raise RuntimeError("Landlock ruleset creation failed")
    try:
        rules = [(p, "read") for p in read_paths] + [(p, "write") for p in write_paths] + [(p, "list") for p in list_paths]
        for path, mode in rules:
            if not os.path.exists(path):
                continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                allowed = (1 << 2) | ((1 << 3) if os.path.isdir(path) else 0)
                if mode == "list":
                    allowed = 1 << 3
                if mode == "write":
                    # No mknod, socket creation or executable files are granted.
                    allowed |= (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 12)
                    if abi >= 2:
                        allowed |= 1 << 13
                    if abi >= 3:
                        allowed |= 1 << 14
                rule = Rule(allowed & handled, fd)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0) != 0:
                    raise RuntimeError("Landlock path rule failed")
            finally:
                os.close(fd)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, ruleset, 0) != 0:
            raise RuntimeError("Landlock enforcement failed")
    finally:
        os.close(ruleset)


def seccomp():
    machine = platform.machine()
    if machine == "x86_64":
        arch = 0xC000003E
        denied = [41, 42, 43, 49, 50, 53, 57, 58, 59, 62, 90, 91, 92, 93, 94, 101, 129, 165, 166, 200, 234, 260, 268, 272, 280, 288, 297, 298, 308, 310, 311, 321, 322, 323, 424, 452]
        clone, clone3 = 56, 435
    elif machine in {"aarch64", "arm64"}:
        arch = 0xC00000B7
        denied = [40, 41, 52, 53, 54, 55, 88, 97, 117, 129, 130, 131, 138, 198, 199, 200, 201, 202, 203, 221, 240, 241, 242, 268, 270, 271, 280, 281, 282, 424, 452]
        clone, clone3 = 220, 435
    else:
        raise RuntimeError("unsupported seccomp architecture")
    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]
    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]
    allow, deny = 0x7FFF0000, 0x00050000 | errno.EPERM
    items = [(0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    if machine == "x86_64":
        # Refuse the x32 syscall ABI, whose flagged syscall numbers would
        # otherwise miss the native x86_64 denial list.
        items += [(0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    for number in denied:
        items += [(0x15, 0, 1, number), (0x06, 0, 0, deny)]
    # clone3 falls back to clone for libc threads; clone only permits threads.
    items += [(0x15, 0, 1, clone3), (0x06, 0, 0, 0x00050000 | errno.ENOSYS),
              (0x15, 0, 4, clone), (0x20, 0, 0, 16), (0x45, 1, 0, 0x10000),
              (0x06, 0, 0, deny), (0x06, 0, 0, allow), (0x06, 0, 0, allow)]
    arr = (Filter * len(items))(*(Filter(*item) for item in items))
    prog = Program(len(items), arr)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(prog)) != 0:
        raise RuntimeError("seccomp enforcement failed")


def main():
    with open(sys.argv[1]) as f:
        config = json.load(f)
    # Open this controller-visible result before restriction. It is outside
    # candidate writable paths and closed before generated code starts.
    mode_file = open(os.path.join(config["work"], "isolation-mode.json"), "w")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (config["file_limit"], config["file_limit"]))
    resource.setrlimit(resource.RLIMIT_CPU, (config["cpu_seconds"], config["cpu_seconds"] + 1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    isolation = "macos_sandbox"
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (config["memory_bytes"], config["memory_bytes"]))
        try:
            landlock(config["read_paths"], config["write_paths"], config["list_paths"])
            seccomp()
            isolation = "linux_kernel"
        except RuntimeError:
            if not config.get("allow_container_guard"):
                raise
            isolation = "container_guarded"
    elif sys.platform != "darwin" or os.environ.get("QUANTGUARD_OS_SANDBOX") != "macos":
        raise RuntimeError("no supported OS sandbox")
    os.chdir(config["work"])
    sys.argv = [config["program"]]
    json.dump(isolation, mode_file)
    mode_file.close()
    # Resolve the trusted allowlist before installing hooks. In compatibility
    # mode these are error-prevention guards, NOT a hostile-code sandbox.
    reads = [(os.path.realpath(p), os.path.isdir(p)) for p in config["read_paths"]]
    writes = [os.path.realpath(p) for p in config["write_paths"]]
    lists = {os.path.realpath(p) for p in config["list_paths"]}
    def permitted(path, writing=False):
        if isinstance(path, int):
            return True  # only intentionally inherited stdio/opened safe FDs
        resolved = os.path.realpath(os.fsdecode(path))
        if writing:
            return any(resolved == root or resolved.startswith(root + os.sep) for root in writes)
        return any(resolved == root or (directory and resolved.startswith(root + os.sep)) for root, directory in reads) or any(resolved == root or resolved.startswith(root + os.sep) for root in writes)
    # Audit hooks add policy to the OS boundary, e.g. for ctypes dlopen. They
    # are not relied on as a substitute for Landlock/seccomp/sandbox-exec.
    def audit(event, args):
        if event.startswith(("socket.", "subprocess.")) or event in {"os.system", "os.fork", "os.exec", "os.posix_spawn", "os.kill", "os.killpg"}:
            raise PermissionError("candidate operation disallowed")
        if event == "open":
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if not permitted(args[0], writing):
                raise PermissionError("candidate path disallowed")
        elif event in {"os.listdir", "os.scandir"}:
            path = args[0] if args and args[0] is not None else os.getcwd()
            if not permitted(path) and os.path.realpath(os.fsdecode(path)) not in lists:
                raise PermissionError("candidate directory listing disallowed")
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.rename"}:
            paths = args[:2] if event == "os.rename" else args[:1]
            if any(not permitted(path, True) for path in paths):
                raise PermissionError("candidate write outside workspace")
        elif event in {"os.symlink", "os.link", "os.chmod", "os.chown", "os.utime", "os.truncate"}:
            raise PermissionError("candidate filesystem mutation disallowed")
        elif event == "ctypes.dlopen" and (args[0] is None or not permitted(args[0])):
            raise PermissionError("candidate native library disallowed")
    sys.addaudithook(audit)
    runpy.run_path(config["program"], run_name="__main__")


if __name__ == "__main__":
    main()
