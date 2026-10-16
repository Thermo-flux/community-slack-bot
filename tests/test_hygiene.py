"""Repository hygiene: dashes, the voice separation guarantee, clock and randomness rules, and the Docker rules."""
from __future__ import annotations

import ast
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
SKIP_DIRS = {".git", "data", "backups", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv", "build", "dist"}
VOICE_FORBIDDEN = ("goose.core", "goose.gateway", "sqlite3", "slack", "slack_bolt")
TIME_ALLOWED = {"goose/core/clock.py", "goose/gateway/voice_client.py"}
GLOBAL_RANDOM = re.compile(r"\brandom\.(random|choice|randint|shuffle)\(")


def rel(path):
    return path.relative_to(ROOT).as_posix()


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = pathlib.Path(dirpath) / name
            if path.suffix != ".pyc":
                yield path


def read_text(path):
    raw = path.read_bytes()
    if b"\x00" in raw:
        return None   # binary file
    return raw.decode("utf-8", errors="replace")


def py_files(subdir):
    return sorted((ROOT / subdir).rglob("*.py"))


def test_no_em_or_en_dashes_in_any_text_file():
    offenders = []
    for path in iter_files():
        text = read_text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if EM_DASH in line or EN_DASH in line:
                offenders.append("%s:%d" % (rel(path), lineno))
    assert not offenders, "em or en dashes found: " + ", ".join(offenders)


def _imported_names(path, tree):
    """Yield (absolute module name, line) for every import in a file under goose/voice."""
    package = ["goose", "voice"] if path.parent.name == "voice" else ["goose"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            yield module, node.lineno
            if module == "goose":
                for alias in node.names:
                    yield "goose." + alias.name, node.lineno


def test_voice_never_imports_core_gateway_database_or_slack():
    files = py_files("goose/voice")
    assert files, "goose/voice must exist"
    offenders = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lineno in _imported_names(path, tree):
            for banned in VOICE_FORBIDDEN:
                if name == banned or name.startswith(banned + "."):
                    offenders.append("%s:%d imports %s" % (rel(path), lineno, name))
    assert not offenders, "voice separation broken: " + "; ".join(offenders)


def test_time_time_is_only_called_from_the_clock():
    offenders = []
    for path in py_files("goose"):
        name = rel(path)
        if name in TIME_ALLOWED or name.startswith("goose/voice/"):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "time.time()" in line:
                offenders.append("%s:%d" % (name, lineno))
    assert not offenders, "time.time() outside the clock: " + ", ".join(offenders)


def test_core_uses_the_passed_rng_not_the_global_random_module():
    offenders = []
    for path in py_files("goose/core"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if GLOBAL_RANDOM.search(line):
                offenders.append("%s:%d" % (rel(path), lineno))
    assert not offenders, "global random used in core: " + ", ".join(offenders)


def test_voice_image_has_no_pip_install_and_copies_only_the_voice():
    text = (ROOT / "Dockerfile.voice").read_text(encoding="utf-8")
    assert "pip install" not in text.lower()
    sources = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "COPY":
            args = [p for p in parts[1:] if not p.startswith("--")]
            sources.extend(args[:-1])
    assert sorted(sources) == ["goose/__init__.py", "goose/voice/"]


def test_gateway_image_has_no_curl_and_env_files_are_not_copied():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "curl" not in text.lower()
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore and "voice.env" in ignore and "data/" in ignore
