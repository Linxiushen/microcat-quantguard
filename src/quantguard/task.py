"""Read the per-unit public contract; never import or run task files."""
from __future__ import annotations
import ast
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from .errors import TaskError

MAX_TASK_TEXT = 100_000
MAX_INPUT_FILES = 2048
_OUTPUT = re.compile(r"/(?:app/)?output/([A-Za-z0-9_][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]+)")
_GUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
_VERIFIER_FILES = {"reward.json", "reward.txt", "pytest_report.json", "diagnostic.json", "reward_summary.json"}


def safe_relative(name: str) -> str:
    p = Path(name)
    if (not name or p.is_absolute() or ".." in p.parts or "\\" in name or
            any(ord(c) < 32 for c in name) or name.startswith(".quantguard") or
            p.name in _VERIFIER_FILES | {"quantguard-audit.json"}):
        raise TaskError("unsafe or reserved output path")
    return p.as_posix()


def redact(text: str, secrets=()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = _GUID.sub("[guid-redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", text)
    return text


@dataclass
class Task:
    root: Path
    instruction: str
    card: dict
    files: list[Path]
    expected_files: list[str]
    canaries: list[str]
    check_context: str = ""

    @property
    def timeout(self) -> float:
        value = self.card.get("agent", {}).get("timeout_sec", 1800)
        if not isinstance(value, (int, float)) or not 0 < value <= 86_400:
            raise TaskError("invalid card agent.timeout_sec")
        return float(value)

    @classmethod
    def load(cls, root: Path, *, public_checks: bool = False):
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise TaskError("task directory is not a directory")
        required = [root / "instruction.md", root / "card.toml"]
        for p in required:
            if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_TASK_TEXT:
                raise TaskError("instruction.md and card.toml must be bounded regular files")
        instruction = required[0].read_text()
        try:
            card = tomllib.loads(required[1].read_text())
        except (tomllib.TOMLDecodeError, UnicodeError):
            raise TaskError("invalid task card") from None
        if card.get("task", {}).get("track") not in (None, "coding"):
            raise TaskError("only Track 1 coding tasks are supported")
        if card.get("environment", {}).get("network", "restricted") != "restricted":
            raise TaskError("card does not authorize House access")
        excluded = {".git", "checks", "tests", "solutions", "solution", "__pycache__"}
        files = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in excluded or part.startswith(".") for part in rel.parts):
                continue
            if p.is_symlink():
                raise TaskError("symlinks in task input are not accepted")
            if p.is_file() and p.name not in {"solve.sh", "reward.json", "pytest_report.json"}:
                files.append(p)
            if len(files) > MAX_INPUT_FILES:
                raise TaskError("task input file count exceeds safety cap")
        names = set()
        for match in _OUTPUT.finditer(instruction):
            name = match.group(1).rstrip(".")
            if name not in _VERIFIER_FILES:
                names.add(safe_relative(name))
        # Explicitly public-dev checks may aid output-contract discovery. This
        # option never searches beyond this task or assumes sealed checks exist.
        check_context = ""
        if public_checks and card.get("task", {}).get("split") == "public-dev":
            for p in sorted((root / "checks").glob("*.py")):
                if p.is_symlink() or p.stat().st_size > MAX_TASK_TEXT:
                    continue
                text = p.read_text()
                check_context += "\n" + p.name + ":\n" + redact(text)
                for match in _OUTPUT.finditer(text):
                    name = match.group(1).rstrip(".")
                    if name not in _VERIFIER_FILES:
                        names.add(safe_relative(name))
            check_context = check_context[:MAX_TASK_TEXT]
        canaries = list(set(_GUID.findall(instruction)))
        canary = card.get("contamination", {}).get("canary_guid")
        if isinstance(canary, str):
            canaries.append(canary)
        return cls(root, redact(instruction), card, files, sorted(names), canaries, check_context)

    def context(self) -> str:
        inventory = []
        for p in self.files:
            if p.name in {"instruction.md", "card.toml", "manifest.json"} or p.suffix in {".sh"}:
                continue
            item = {"path": str(p), "bytes": p.stat().st_size}
            # Read only short structure previews, never try to import data code.
            if p.suffix.lower() in {".csv", ".tsv", ".json", ".jsonl", ".xml", ".txt"}:
                with p.open("rb") as f:
                    item["preview"] = redact(f.read(2048).decode("utf-8", errors="replace"))
            inventory.append(item)
        context = {"instruction": self.instruction, "task_root": str(self.root),
                   "expected_files_from_explicit_output_paths": self.expected_files,
                   "input_inventory": inventory,
                   "category": self.card.get("metadata", {}).get("category", "unknown"),
                   "public_contract_checks": self.check_context}
        text = json.dumps(context, ensure_ascii=False)
        if len(text.encode()) > 180_000:
            # Do not truncate instructions. Drop data previews before failing.
            for item in inventory:
                item.pop("preview", None)
            text = json.dumps(context, ensure_ascii=False)
        if len(text.encode()) > 180_000:
            raise TaskError("task context exceeds the bounded prompt size")
        return text


@dataclass
class Program:
    code: str
    outputs: list[str]

    @classmethod
    def parse(cls, content: str, expected_files: list[str]):
        raw = content.strip()
        if raw.startswith("```json"):
            raw = raw.removeprefix("```json").removesuffix("```").strip()
        if raw.startswith("```python") and raw.endswith("```"):
            obj = {"code": raw[len("```python"):-3].strip(), "outputs": expected_files}
        else:
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raise TaskError("model response is not a program JSON object") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("code"), str):
            raise TaskError("program JSON must contain code")
        outputs = obj.get("outputs")
        if not isinstance(outputs, list) or not outputs or not all(isinstance(x, str) for x in outputs):
            raise TaskError("program must declare nonempty output filenames")
        outputs = list(dict.fromkeys(safe_relative(x) for x in outputs))
        if len(outputs) > 128 or len(obj["code"].encode()) > 100_000:
            raise TaskError("program exceeds size limits")
        if not set(expected_files).issubset(outputs):
            raise TaskError("program omitted a filename required by instruction.md")
        try:
            tree = ast.parse(obj["code"])
        except SyntaxError as exc:
            raise TaskError(f"candidate syntax error on line {exc.lineno}") from None
        forbidden = {"subprocess", "socket", "ctypes", "multiprocessing", "http", "urllib", "requests",
                     "httpx", "openai", "torch", "transformers", "tensorflow", "jax", "pickle", "marshal"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {n.name.split(".")[0] for n in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            if forbidden & roots:
                raise TaskError("candidate imported a forbidden network, process or neural module")
        # This lint is only defense in depth. OS policy provides the isolation.
        return cls(obj["code"], outputs)
