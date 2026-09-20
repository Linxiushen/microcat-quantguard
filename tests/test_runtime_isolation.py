"""Isolation selection: exercise policy decisions without restricting pytest."""
from types import SimpleNamespace

import pytest

from quantguard import runtime, worker
from quantguard.errors import SandboxError


def config(compatible=True):
    return {"read_paths": [], "write_paths": [], "list_paths": [], "allow_container_guard": compatible}


def unsupported(message):
    def operation(*args):
        raise worker.KernelFeatureUnavailable(message)
    return operation


def test_worker_sets_nnp_and_attempts_seccomp_when_landlock_is_unavailable(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "no_new_privileges", lambda: calls.append("nnp"))
    monkeypatch.setattr(worker, "landlock", unsupported("Landlock unsupported"))
    monkeypatch.setattr(worker, "seccomp", lambda: calls.append("seccomp"))
    result = worker.linux_isolation(config())
    assert calls == ["nnp", "seccomp"]
    assert result["mode"] == "container_guarded"
    assert result["applied"] == {"no_new_privileges": True, "landlock": False, "seccomp": True}
    assert result["unavailable"] == ["Landlock unsupported"]


def test_partial_landlock_protection_is_preserved_and_reported(monkeypatch):
    monkeypatch.setattr(worker, "no_new_privileges", lambda: None)
    monkeypatch.setattr(worker, "landlock", lambda *args: None)
    monkeypatch.setattr(worker, "seccomp", unsupported("seccomp unavailable"))
    result = worker.linux_isolation(config())
    assert result["mode"] == "container_guarded"
    assert result["applied"]["landlock"] is True
    assert result["applied"]["seccomp"] is False


def test_every_successful_layer_is_required_for_kernel_label(monkeypatch):
    monkeypatch.setattr(worker, "no_new_privileges", lambda: None)
    monkeypatch.setattr(worker, "landlock", lambda *args: None)
    monkeypatch.setattr(worker, "seccomp", lambda: None)
    result = worker.linux_isolation(config(False))
    assert result["mode"] == "linux_kernel"
    assert all(result["applied"].values())


def test_host_never_silently_uses_compatibility(monkeypatch):
    monkeypatch.setattr(worker, "no_new_privileges", lambda: None)
    monkeypatch.setattr(worker, "landlock", unsupported("Landlock absent"))
    monkeypatch.setattr(worker, "seccomp", lambda: None)
    with pytest.raises(RuntimeError, match="required Linux kernel isolation"):
        worker.linux_isolation(config(False))


def test_nnp_unavailable_does_not_skip_other_layers(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "no_new_privileges", unsupported("nnp unavailable"))
    monkeypatch.setattr(worker, "landlock", lambda *args: calls.append("landlock"))
    monkeypatch.setattr(worker, "seccomp", lambda: calls.append("seccomp"))
    result = worker.linux_isolation(config())
    assert calls == ["landlock", "seccomp"]
    assert result["mode"] == "container_guarded"
    assert result["applied"]["no_new_privileges"] is False


def test_ruleset_configuration_error_is_not_hidden_as_fallback(monkeypatch):
    monkeypatch.setattr(worker, "no_new_privileges", lambda: None)
    def invalid_rules(*args):
        raise RuntimeError("Landlock bad rule")
    monkeypatch.setattr(worker, "landlock", invalid_rules)
    with pytest.raises(RuntimeError, match="bad rule"):
        worker.linux_isolation(config())


def test_invalid_seccomp_filter_fails_after_landlock_was_applied(monkeypatch):
    monkeypatch.setattr(worker, "no_new_privileges", lambda: None)
    monkeypatch.setattr(worker, "landlock", lambda *args: None)
    def invalid_filter():
        raise RuntimeError("invalid BPF filter")
    monkeypatch.setattr(worker, "seccomp", invalid_filter)
    with pytest.raises(RuntimeError, match="invalid BPF"):
        worker.linux_isolation(config())


def mock_container(monkeypatch, root_mode="ro", task_mode=None, uid=65534, marker=True):
    monkeypatch.setenv("QUANTGUARD_ISOLATION", "container")
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: uid)
    monkeypatch.setattr(runtime.Path, "exists", lambda p: marker)
    def read_mounts(path):
        assert str(path) == "/proc/self/mountinfo", "controller must not require inherited NNP"
        return f"1 0 0:1 / / {root_mode},relatime - overlay overlay {root_mode}\n"
    monkeypatch.setattr(runtime.Path, "read_text", read_mounts)
    flags = runtime.os.ST_RDONLY if task_mode is None else task_mode
    monkeypatch.setattr(runtime.os, "statvfs", lambda p: SimpleNamespace(f_flag=flags))


def test_official_readonly_launch_does_not_need_host_nnp(monkeypatch):
    mock_container(monkeypatch)
    assert runtime.container_guard_allowed(runtime.Path("/input")) is True


@pytest.mark.parametrize("changes,reason", [
    ({"root_mode": "rw"}, "read-only root"),
    ({"task_mode": 0}, "read-only task"),
    ({"uid": 0}, "non-root"),
    ({"marker": False}, "non-root"),
])
def test_container_prerequisites_remain_required(monkeypatch, changes, reason):
    mock_container(monkeypatch, **changes)
    with pytest.raises(SandboxError, match=reason):
        runtime.container_guard_allowed(runtime.Path("/input"))


def test_compatibility_requires_explicit_mode(monkeypatch):
    monkeypatch.delenv("QUANTGUARD_ISOLATION", raising=False)
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    assert runtime.container_guard_allowed(runtime.Path("/input")) is False
