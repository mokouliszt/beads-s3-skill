#!/usr/bin/env python3
"""Package a skill folder as a .skill file: a zip with the skill folder at its root.

Release package (what GitHub Actions attaches to each release):

    python3 .github/scripts/package_skill.py skills/beads-s3-skill

Private package with your bucket settings inside, to upload to your AI app yourself:

    python3 .github/scripts/package_skill.py skills/beads-s3-skill \\
        --credentials skills/beads-s3-skill/auth/credentials.json

Without --credentials the package never holds credentials: from auth/ only example
files (*.example, *.example.*) are packed, even when a real credentials.json sits
next to them. --credentials adds the given file as auth/credentials.json and names
the output *-private.skill. Never attach a package built that way to a release.

The SKILL.md checks and the files left out follow skill-creator's
quick_validate.py and package_skill.py. Files that are executable or start with
"#!" are stored as 0755, everything else as 0644. Files that start with "#!" are
stored with LF line endings: with CRLF, the interpreter named on their first line
is not found. Entry times come from SOURCE_DATE_EPOCH when it is set. Standard
library only; PyYAML is used to read the frontmatter when it is installed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
import zipfile
from pathlib import Path

CREDENTIALS_NAME = "credentials.json"
EXAMPLE_NAME = "credentials.example.json"
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git"}
EXCLUDE_FILES = {".DS_Store", ".gitignore"}
EXCLUDE_SUFFIXES = (".pyc",)
ROOT_EXCLUDE_DIRS = {"evals"}
ALLOWED_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ZIP_MIN_TIME = (1980, 1, 1, 0, 0, 0)


class PackageError(Exception):
    pass


# ---------------------------------------------------------------- SKILL.md

def read_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    if not m:
        raise PackageError("SKILL.md has no YAML frontmatter")
    try:
        import yaml
    except ImportError:
        return simple_frontmatter(m.group(1))
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise PackageError(f"SKILL.md frontmatter is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise PackageError("SKILL.md frontmatter must be a mapping")
    return data


def simple_frontmatter(block: str) -> dict:
    """Top-level keys only, for when PyYAML is not installed."""
    data: dict = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z][\w-]*):(?:[ \t]+(.*))?$", lines[i])
        i += 1
        if not m:
            continue
        key, value = m.group(1), (m.group(2) or "").strip()
        if value[:1] in ("|", ">"):
            # Block scalar: the indented lines that follow.
            body = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
                body.append(lines[i].strip())
                i += 1
            value = ("\n" if value[0] == "|" else " ").join(body).strip()
        elif value[:1] == '"':
            if len(value) < 2 or not value.endswith('"'):
                raise PackageError(f"SKILL.md: unterminated quoted value for '{key}'")
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        elif value[:1] == "'":
            if len(value) < 2 or not value.endswith("'"):
                raise PackageError(f"SKILL.md: unterminated quoted value for '{key}'")
            value = value[1:-1].replace("''", "'")
        elif ": " in value or " #" in value:
            # Same rule as YAML: such a value has to be quoted.
            raise PackageError(f"SKILL.md: quote the value of '{key}' (an unquoted value cannot contain ': ' or ' #')")
        data[key] = value
    return data


def is_excluded(rel: Path) -> bool:
    """Files skill-creator's package_skill.py leaves out, plus git metadata."""
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return True
    if len(parts) > 1 and parts[0] in ROOT_EXCLUDE_DIRS:
        return True
    return parts[-1] in EXCLUDE_FILES or parts[-1].endswith(EXCLUDE_SUFFIXES)


def is_example(filename: str) -> bool:
    """Files in auth/ that are safe to publish: foo.example, foo.example.json."""
    return filename.endswith(".example") or ".example." in filename


def validate(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise PackageError(f"{skill_dir}: SKILL.md not found")
    nested = sorted(
        p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("SKILL.md")
        if p != skill_md and not is_excluded(p.relative_to(skill_dir))
    )
    if nested:
        raise PackageError("a skill must contain exactly one SKILL.md; also found: " + ", ".join(nested))

    fm = read_frontmatter(skill_md)
    unexpected = sorted(set(fm) - ALLOWED_KEYS)
    if unexpected:
        raise PackageError(f"SKILL.md: unexpected frontmatter key(s): {', '.join(unexpected)}")

    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PackageError("SKILL.md: 'name' is missing")
    name = name.strip()
    if not NAME_RE.match(name) or len(name) > 64:
        raise PackageError(f"SKILL.md: name '{name}' must be kebab-case, at most 64 characters")
    if name != skill_dir.name:
        raise PackageError(f"SKILL.md: name '{name}' does not match the folder name '{skill_dir.name}'")

    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PackageError("SKILL.md: 'description' is missing")
    if "<" in description or ">" in description:
        raise PackageError("SKILL.md: the description cannot contain angle brackets")
    if len(description.strip()) > 1024:
        raise PackageError(f"SKILL.md: the description is {len(description.strip())} characters (max 1024)")

    compatibility = fm.get("compatibility")
    if compatibility is not None and (not isinstance(compatibility, str) or len(compatibility) > 500):
        raise PackageError("SKILL.md: 'compatibility' must be a string of at most 500 characters")
    return name


# ---------------------------------------------------------------- zip

def entry_time(path: Path | None) -> tuple:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        t = time.gmtime(int(epoch))[:6]
    else:
        t = time.localtime(path.stat().st_mtime if path else time.time())[:6]
    return max(t, ZIP_MIN_TIME)


def add(zf: zipfile.ZipFile, arcname: str, data: bytes, mode: int, when: tuple) -> None:
    info = zipfile.ZipInfo(arcname, when)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # Unix, so that the permission bits below are honoured
    info.external_attr = (stat.S_IFREG | mode) << 16
    zf.writestr(info, data)


def read_credentials(path: Path, example: Path) -> bytes:
    data = path.read_bytes()
    try:
        cfg = json.loads(data)
    except ValueError as e:
        raise PackageError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(cfg, dict) or not isinstance(cfg.get("bucket"), str) or not cfg["bucket"].strip():
        raise PackageError(f"{path} has no \"bucket\" (see auth/{EXAMPLE_NAME})")
    try:
        sample = json.loads(example.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sample = {}
    unchanged = sorted(k for k in ("bucket", "access_key_id", "secret_access_key")
                       if k in cfg and cfg[k] == sample.get(k))
    if unchanged:
        raise PackageError(f"{path} still has the example value for: {', '.join(unchanged)}")
    return data


def build(skill_dir: Path, out: Path, credentials: Path | None) -> None:
    name = validate(skill_dir)
    if out.resolve().is_relative_to(skill_dir.resolve()):
        raise PackageError("the output file must be outside the skill folder")
    credentials_data = read_credentials(credentials, skill_dir / "auth" / EXAMPLE_NAME) if credentials else None

    files: list[tuple[str, Path]] = []
    held_back: list[str] = []
    for path in skill_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir)
        if is_excluded(rel):
            continue
        if rel.parts[0] == "auth" and not is_example(rel.name):
            held_back.append(rel.as_posix())  # a real credential: only ever packed through --credentials
            continue
        files.append((rel.as_posix(), path))
    files.sort()

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    converted: list[str] = []
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            for rel, path in files:
                data = path.read_bytes()
                script = data.startswith(b"#!")
                if script and b"\r\n" in data:
                    data = data.replace(b"\r\n", b"\n")
                    converted.append(rel)
                executable = path.stat().st_mode & 0o111 or script
                add(zf, f"{name}/{rel}", data, 0o755 if executable else 0o644, entry_time(path))
            if credentials_data is not None:
                add(zf, f"{name}/auth/{CREDENTIALS_NAME}", credentials_data, 0o644, entry_time(None))
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)

    with zipfile.ZipFile(out) as zf:
        if zf.testzip() is not None:
            raise PackageError(f"{out}: archive check failed")
        infos = zf.infolist()
    names = [i.filename for i in infos]
    if f"{name}/SKILL.md" not in names:
        raise PackageError(f"{out}: SKILL.md missing from the archive")
    secrets = [n for n in names if n.startswith(f"{name}/auth/") and not is_example(n.rsplit("/", 1)[-1])]
    if credentials is None and secrets:
        raise PackageError(f"{out}: credentials in a release package: {', '.join(secrets)}")

    print(f"{out} ({len(infos)} files, {out.stat().st_size / 1024:.1f} KB)")
    for i in infos:
        note = "  <- your bucket settings" if i.filename in secrets else ""
        print(f"  {(i.external_attr >> 16) & 0o777:o}  {i.file_size:>6}  {i.filename}{note}")
    for rel in converted:
        print(f"  line endings changed from CRLF to LF: {rel}")
    for rel in held_back:
        if credentials is None or (skill_dir / rel).resolve() != credentials.resolve():
            print(f"  left out (credential): {rel}")
    if credentials is not None:
        print("This package contains your bucket settings. Keep it private; never attach it to a release.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Package a skill folder as a .skill file.")
    ap.add_argument("skill_dir", type=Path, help="the skill folder, e.g. skills/beads-s3-skill")
    ap.add_argument("--out", type=Path, help="output file (default: dist/<name>.skill, or dist/<name>-private.skill with --credentials)")
    ap.add_argument("--credentials", type=Path, metavar="FILE",
                    help=f"add FILE as auth/{CREDENTIALS_NAME}, for a private package you upload yourself")
    args = ap.parse_args(argv)
    skill_dir = args.skill_dir.resolve()
    out = args.out or Path("dist") / f"{skill_dir.name}{'-private' if args.credentials else ''}.skill"
    try:
        build(skill_dir, out, args.credentials)
    except (PackageError, OSError) as e:
        print(f"package_skill: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
