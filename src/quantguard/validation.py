"""Local structural validation, not a substitute for the official scorer."""
from __future__ import annotations
import csv
import json
import math
import os
from pathlib import Path
from .errors import CandidateError
from .task import safe_relative

MAX_OUTPUT_BYTES = 63 * 1024**2  # reserve space below the official 64 MiB tree cap


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise CandidateError("non-finite numeric JSON value")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def validate_outputs(directory: Path, outputs: list[str], *, canaries=()) -> list[Path]:
    root = directory.resolve()
    all_files = []
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CandidateError("symlink output refused")
        if path.is_file():
            total += path.stat().st_size
            all_files.append(path)
        elif not path.is_dir():
            raise CandidateError("non-regular output refused")
    if total > MAX_OUTPUT_BYTES or len(all_files) > 128:
        raise CandidateError("output tree exceeded byte or file cap")
    declared = set(outputs)
    actual = {p.relative_to(root).as_posix() for p in all_files}
    if actual != declared:
        raise CandidateError("output tree does not match declared deliverables")
    for name in outputs:
        safe_relative(name)
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            raise CandidateError("required deliverable absent or empty: " + name)
        if not path.resolve().is_relative_to(root):
            raise CandidateError("output path escaped working directory")
        # Do not copy contamination sentinels or raw instructions into outputs.
        with path.open("rb") as stream:
            overlap = b""
            while block := stream.read(1024**2):
                chunk = overlap + block
                if any(value.encode() in chunk for value in canaries if value):
                    raise CandidateError("task contamination sentinel in deliverable")
                overlap = chunk[-128:]
        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                with path.open() as f:
                    value = json.load(f, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
                _finite(value)
            elif suffix in {".csv", ".tsv"}:
                with path.open(newline="") as f:
                    rows = csv.reader(f, delimiter="\t" if suffix == ".tsv" else ",")
                    header = next(rows, [])
                    if not header or any(not col for col in header) or len(set(header)) != len(header):
                        raise ValueError("invalid table header")
                    for row in rows:
                        if len(row) != len(header):
                            raise ValueError("inconsistent column count")
            elif suffix in {".parquet", ".pqt"}:
                try:
                    import pyarrow.parquet as pq
                except ImportError:
                    raise CandidateError("pyarrow is required to validate parquet output") from None
                pq.ParquetFile(path).schema
            elif suffix == ".jsonl":
                with path.open() as f:
                    for line in f:
                        if line.strip():
                            _finite(json.loads(line))
            elif suffix == ".py":
                compile(path.read_text(), name, "exec")
        except (ValueError, SyntaxError, UnicodeError, csv.Error):
            raise CandidateError("malformed output file: " + name) from None
    return [root / name for name in outputs]


def publish_outputs(files: list[Path], staging: Path, destination: Path):
    """Each validated file is atomically replaced; completion is recorded last."""
    for path in files:
        name = path.relative_to(staging)
        target = destination / name
        if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != destination.parent):
            raise CandidateError("output destination symlink refused")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)
