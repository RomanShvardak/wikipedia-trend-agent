"""Scaffold smoke tests: the skill directory is self-contained per agentskills.io.

Proves (verification block of the 01-02-02 plan): SKILL.md frontmatter `name`
equals the parent directory name; description <= 1024 chars; license and
compatibility intents; metadata.version present; requirements.txt declares
matplotlib with a floor pin and no exact `==` pin; the conftest.py sys.path
shim resolves `common`; .gitignore covers the generated dirs; pyproject.toml
exists. No network, no matplotlib/pandas/requests/numpy imports.
"""
from __future__ import annotations

from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REQUIREMENTS = SKILL_DIR / "requirements.txt"
SKILL_DOC = SKILL_DIR / "SKILL.md"
GITIGNORE = SKILL_DIR / ".gitignore"


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Manual split of the YAML frontmatter: top-level + nested `metadata.*` keys."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    assert len(parts) >= 3, "file must open with a YAML frontmatter block (--- ... ---)"
    block = parts[1]
    front: dict[str, str] = {}
    top: str | None = None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if raw[:1] in (" ", "\t"):
            if top is not None:
                name, _, value = raw.strip().partition(":")
                front[f"{top}.{name.strip()}"] = value.strip()
            continue
        top = None
        name, _, value = raw.partition(":")
        name, value = name.strip(), value.strip()
        if value == "":
            top = name
        else:
            front[name] = value
    return front


def test_skill_name_matches_directory() -> None:
    """agentskills.io: frontmatter `name` must equal the parent directory name."""
    front = _parse_frontmatter(SKILL_DOC)
    assert front["name"] == SKILL_DIR.name, (
        f"SKILL.md name must be '{SKILL_DIR.name}', got '{front['name']}'"
    )


def test_skill_frontmatter_fields() -> None:
    """Required frontmatter: description <= 1024 chars, license, compatibility, metadata."""
    front = _parse_frontmatter(SKILL_DOC)
    description = front["description"]
    assert len(description) <= 1024, f"description is {len(description)} chars (> 1024)"
    assert front["license"] == "Apache-2.0", front["license"]
    assert "Python >= 3.11" in front["compatibility"], front["compatibility"]
    # The version is read from `pyproject.toml` rather than pinned as a literal here.
    # It was a literal ("0.1.0") until Phase 9 bumped the skill to 0.2.0, and the bump
    # turned this line red — which is the test working, but it also showed the cost: two
    # places to change for one fact, and a reader who bumps one and not the other gets a
    # failure that says nothing about which is wrong. Now `pyproject.toml` is the single
    # source and this asserts the two agree.
    import tomllib

    declared = tomllib.loads(
        (SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    assert front["metadata.version"] == declared, (
        f"SKILL.md's frontmatter says {front['metadata.version']!r} while pyproject.toml "
        f"says {declared!r}; the skill's version is stated in two files and they must agree"
    )
    assert front["metadata.spec-url"] == "https://agentskills.io/specification"


def test_requirements_declares_matplotlib_only_floor() -> None:
    """requirements.txt: exactly the matplotlib floor pin, no exact `==` pin (D-06)."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(lines) >= 1, "requirements.txt must be non-empty"
    assert any(ln.strip().startswith("matplotlib>=3.11") for ln in lines), (
        f"matplotlib>=3.11 floor pin missing: {lines!r}"
    )
    assert "==" not in text, "no exact version pin (==) allowed in requirements.txt"


def test_import_common_via_conftest_shim() -> None:
    """The sys.path shim (conftest.py) makes `common` importable from tests."""
    import common

    assert callable(common.load_spec), "common.load_spec must be importable and callable"


def test_gitignore_covers_generated_dirs() -> None:
    """Generated/runtime artifacts are ignored: cache, output, venv, bytecode."""
    entries = {ln.strip() for ln in GITIGNORE.read_text(encoding="utf-8").splitlines() if ln.strip()}
    for required in ("__pycache__/", "*.pyc", ".cache/", "out/", ".venv/"):
        assert required in entries, f".gitignore is missing {required!r}"


def test_pyproject_exists() -> None:
    """The dev config (pytest/ruff/mypy) lives in pyproject.toml at the skill root."""
    assert (SKILL_DIR / "pyproject.toml").is_file(), "pyproject.toml missing at skill root"