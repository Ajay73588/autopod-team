#!/usr/bin/env python3
"""
AutoPod Cloud -- Terraform Configuration Validation Utility
===========================================================

Read-only, offline static checks for the Terraform root module in
``infra/terraform/`` (and any directory that looks like a Terraform
configuration).

What this script does NOT do
----------------------------
No AWS credentials are read, no network calls are made, ``terraform`` is
never executed, and no file is created, edited or deleted. It only reads the
configuration directory and prints a report. Every check is a regex/structure
heuristic over the ``.tf`` sources -- it is a fast pre-commit smoke test, not
a replacement for ``terraform validate`` / ``terraform plan``, which remain
the authority on whether the configuration is actually valid.

Checks performed
----------------
  T001  required_files      Directory is a Terraform configuration (has *.tf
                            files); reports canonical file presence.
  T002  terraform_block     ``terraform {}`` block exists and declares a
                            ``required_version`` constraint.
  T003  required_providers  ``required_providers`` exists, every provider is
                            pinned to a version, and configured providers are
                            declared there.
  T004  provider_blocks     At least one ``provider`` block is configured.
  T005  variables           Duplicate names, missing ``type``/``description``,
                            non-conventional names.
  T006  outputs             Duplicate names, missing ``value``/``description``.
  T007  tfvars_example      ``terraform.tfvars.example`` keys match declared
                            variables and document required (no-default) ones.
  T008  forbidden_artifacts Real ``terraform.tfvars`` / state / plan files that
                            are tracked (or unignored) in git.
  T009  gitignore_hygiene   ``.gitignore`` ignores state, tfvars and
                            ``.terraform/`` while un-ignoring the example file.
  T010  file_hygiene        Empty ``.tf`` files, byte-order marks, missing
                            trailing newline, unexpected files in the module
                            root.
  T011  secret_scan         AWS access-key IDs, 40-character secret keys and
                            PEM private-key blocks committed in sources.
  T012  variable_usage      ``var.X`` references resolve to declared
                            variables; declared variables are actually used.

Usage
-----
    python validate_terraform_config.py
        Validate the current directory if it contains *.tf files, otherwise
        the directory this script lives in.

    python validate_terraform_config.py infra/terraform
        Validate a specific configuration directory.

    python validate_terraform_config.py --strict --verbose
        Treat warnings as failures and print informational findings.

    python validate_terraform_config.py --list-checks
        Print the check registry and exit.

Exit codes
----------
    0   no errors found (warnings/info may still be reported)
    1   one or more errors (or warnings when --strict is used)
    2   invalid usage, or the target path is not a directory

Requires Python 3.8+ and the standard library only.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
import sys
from typing import Dict, Iterator, List, Optional, Sequence, Tuple


# ==============================================================================
# Constants
# ==============================================================================

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"
SEVERITY_INFO = "INFO"

#: Canonical file names, by concern. Terraform allows splitting declarations
#: across any *.tf file, so these are reported (INFO) rather than required.
CANONICAL_FILES: Dict[str, Tuple[str, ...]] = {
    "provider/version configuration": ("providers.tf", "versions.tf", "terraform.tf"),
    "variables": ("variables.tf",),
    "outputs": ("outputs.tf",),
}

#: Top-level directory entries that are expected in a Terraform root module.
#: ``.py`` covers local helper scripts such as this validator itself.
ALLOWED_ROOT_SUFFIXES = (".tf", ".example", ".md", ".py")
ALLOWED_ROOT_NAMES = {".gitignore", ".git", "templates", "modules", ".terraform.lock.hcl"}
ALLOWED_ROOT_DIRS = {"templates", "modules", ".terraform", "__pycache__", ".pytest_cache"}

#: Terraform state, plan and variable-value artifacts that must never be
#: committed. (glob, severity if tracked, description)
ARTIFACT_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    ("terraform.tfvars", SEVERITY_ERROR, "real variable values (may contain secrets)"),
    ("terraform.tfvars.json", SEVERITY_ERROR, "real variable values (may contain secrets)"),
    ("*.tfstate", SEVERITY_ERROR, "Terraform state (contains resource state and sometimes secrets)"),
    ("*.tfstate.*", SEVERITY_ERROR, "Terraform state backup"),
    ("*.tfplan", SEVERITY_ERROR, "saved plan output"),
    ("tfplan", SEVERITY_ERROR, "saved plan output"),
    ("crash.log", SEVERITY_WARNING, "Terraform crash log"),
    ("*.backup", SEVERITY_WARNING, "editor/apply backup file"),
)

#: Processed (non-comment, non-blank) lines of .gitignore that protect the
#: repository from state and secret leakage.
GITIGNORE_REQUIREMENTS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("state files", ("tfstate",), r"*.tfstate and *.tfstate.*"),
    ("variable values", ("tfvars",), r"*.tfvars"),
    ("provider plugins / init cache", (".terraform",), ".terraform/"),
)

#: Credential shapes that should never appear in a committed Terraform source.
SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = (
    (
        "aws_access_key_id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "looks like a real AWS access key ID",
    ),
    (
        "aws_secret_access_key",
        re.compile(r"aws_secret_access_key\s*=\s*\"[A-Za-z0-9/+=]{40}\""),
        "looks like a real AWS secret access key value",
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
        "contains an embedded private key",
    ),
)

CHECKS: Tuple[Tuple[str, str, str], ...] = (
    ("T001", "required_files", "Directory contains Terraform configuration files."),
    ("T002", "terraform_block", "terraform block declares required_version."),
    ("T003", "required_providers", "required_providers pins provider versions."),
    ("T004", "provider_blocks", "At least one provider block is configured."),
    ("T005", "variables", "Variable blocks are well-formed and documented."),
    ("T006", "outputs", "Output blocks declare value and description."),
    ("T007", "tfvars_example", "Example tfvars matches the declared variables."),
    ("T008", "forbidden_artifacts", "No tracked state, plan or real tfvars files."),
    ("T009", "gitignore_hygiene", ".gitignore protects state and secrets."),
    ("T010", "file_hygiene", "Module directory is clean and non-empty."),
    ("T011", "secret_scan", "No credentials or private keys in sources."),
    ("T012", "variable_usage", "Variable references resolve; no dead variables."),
)


# ==============================================================================
# Terminal presentation
# ==============================================================================

class Colors:
    """ANSI color helpers, disabled for non-TTY output or NO_COLOR."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    DIM = "\033[2m"

    @classmethod
    def disable(cls) -> None:
        cls.RESET = cls.BOLD = cls.RED = cls.GREEN = ""
        cls.YELLOW = cls.CYAN = cls.DIM = ""


SEVERITY_COLORS = {
    SEVERITY_ERROR: lambda: Colors.RED,
    SEVERITY_WARNING: lambda: Colors.YELLOW,
    SEVERITY_INFO: lambda: Colors.CYAN,
}
SEVERITY_RANK = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}


# ==============================================================================
# Minimal HCL structure scanning
# ==============================================================================

def _skip_string(text: str, start: int) -> int:
    """Return the index just past the closing quote of the string at *start*.

    Handles backslash escapes and ``${...}`` / ``%{...}`` interpolation, which
    may itself contain quoted strings or braces.
    """
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        if ch in "$%" and text.startswith("{", i + 1):
            i = _skip_braced_expression(text, i + 1)
            continue
        i += 1
    return n


def _skip_braced_expression(text: str, start: int) -> int:
    """Skip a balanced ``{...}`` group, respecting nested strings and braces."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_heredoc(text: str, start: int) -> int:
    """Skip ``<<EOT ... EOT`` (or ``<<-EOT``) heredoc content starting at *start*."""
    match = re.compile(r'<<-?\s*"?([A-Za-z_][A-Za-z0-9_]*)"?').match(text, start)
    if not match:
        return start + 2
    marker = match.group(1)
    newline = text.find("\n", match.end())
    if newline < 0:
        return len(text)
    i = newline + 1
    n = len(text)
    while i < n:
        line_end = text.find("\n", i)
        if line_end < 0:
            line_end = n
        if text[i:line_end].strip() == marker:
            return min(line_end + 1, n)
        i = line_end + 1
    return n


def _match_brace(text: str, start: int) -> int:
    """Return the index of the ``}`` closing the ``{`` at *start*, or -1."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "#" or text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if text.startswith("<<", i):
            i = _skip_heredoc(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


_BLOCK_RE = re.compile(
    r"^[ \t]*(?P<kw>terraform|variable|output|provider|module|resource|data|locals)\b",
    re.MULTILINE,
)


@dataclass(frozen=True)
class HclBlock:
    """A top-level HCL block: keyword, quoted labels, body text, line number."""

    keyword: str
    labels: Tuple[str, ...]
    body: str
    line: int


def iter_blocks(text: str) -> Iterator[HclBlock]:
    """Yield top-level blocks in document order, skipping nested content.

    Nested blocks are skipped, which also keeps keyword-looking lines inside
    heredocs or nested maps from being mistaken for top-level declarations.
    """
    covered_until = -1
    for match in _BLOCK_RE.finditer(text):
        if match.start() < covered_until:
            continue
        cursor = match.end()
        n = len(text)
        while cursor < n and text[cursor] not in "{=\n":
            cursor += 1
        if cursor >= n or text[cursor] != "{":
            continue
        close = _match_brace(text, cursor)
        if close < 0:
            close = n - 1
        labels = tuple(re.findall(r'"([^"]*)"', text[match.end():cursor]))
        yield HclBlock(
            keyword=match.group("kw"),
            labels=labels,
            body=text[cursor + 1:close],
            line=text.count("\n", 0, match.start()) + 1,
        )
        covered_until = close


def strip_hcl_comments(text: str) -> str:
    """Blank out ``#``, ``//`` and ``/* */`` comments while preserving offsets.

    String literals are left untouched so that ``"#"`` or ``"//"`` inside a
    value is not mistaken for a comment.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
            continue
        if ch == "#" or text.startswith("//", i):
            end = text.find("\n", i)
            if end < 0:
                end = n
            out.append(" " * (end - i))
            i = end
            continue
        if text.startswith("<<", i):
            end = _skip_heredoc(text, i)
            out.append(text[i:end])
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_tfvars_assignments(text: str) -> Dict[str, Tuple[str, int, bool]]:
    """Parse ``key = value`` lines from a tfvars file.

    Returns ``{key: (value, line_number, is_active)}``. Commented-out
    assignments are included (with ``is_active=False``) so documentation such
    as ``# app_node_ami_id = ""`` still counts as covering a variable.
    """
    entries: Dict[str, Tuple[str, int, bool]] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        active = True
        if stripped.startswith("#") or stripped.startswith("//"):
            active = False
            stripped = stripped.lstrip("#/").strip()
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$", stripped)
        if not match:
            continue
        entries[match.group(1)] = (match.group(2).strip(), line_number, active)
    return entries


# ==============================================================================
# Report model
# ==============================================================================

@dataclass(frozen=True)
class Finding:
    """A single observation produced by a check."""

    check: str
    severity: str
    message: str
    path: Optional[Path] = None
    line: Optional[int] = None


@dataclass
class ValidationReport:
    """Collected findings plus the inputs that produced them."""

    config_dir: Path
    findings: List[Finding] = field(default_factory=list)
    tf_files: List[Path] = field(default_factory=list)
    checks_run: List[str] = field(default_factory=list)

    def add(
        self,
        check: str,
        severity: str,
        message: str,
        path: Optional[Path] = None,
        line: Optional[int] = None,
    ) -> None:
        self.findings.append(Finding(check, severity, message, path, line))

    def of(self, severity: str) -> List[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def errors(self) -> List[Finding]:
        return self.of(SEVERITY_ERROR)

    @property
    def warnings(self) -> List[Finding]:
        return self.of(SEVERITY_WARNING)

    @property
    def infos(self) -> List[Finding]:
        return self.of(SEVERITY_INFO)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def sorted_findings(self) -> List[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (
                SEVERITY_RANK.get(f.severity, 9),
                f.check,
                str(f.path or ""),
                f.line or 0,
            ),
        )


# ==============================================================================
# Validator
# ==============================================================================

class TerraformConfigValidator:
    """Runs every static check against one Terraform configuration directory."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir.resolve()
        self.report = ValidationReport(config_dir=self.config_dir)
        self.files: Dict[Path, str] = {}
        self._git_ok: Optional[bool] = None

    # -- input -----------------------------------------------------------------

    def load(self) -> None:
        """Read every *.tf file in the directory (sorted for stable output)."""
        try:
            paths = sorted(
                (p for p in self.config_dir.glob("*.tf") if p.is_file()),
                key=lambda p: p.name,
            )
        except OSError as exc:
            self.report.add("T001", SEVERITY_ERROR, f"cannot list directory: {exc}")
            return
        for path in paths:
            try:
                self.files[path] = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                self.report.add(
                    "T001", SEVERITY_ERROR, f"cannot read file: {exc}", path=path
                )
        self.report.tf_files = list(self.files)

    # -- helpers ---------------------------------------------------------------

    def _rel(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.config_dir)
        except ValueError:
            return path

    def _blocks(self, keyword: str) -> List[Tuple[Path, HclBlock]]:
        found: List[Tuple[Path, HclBlock]] = []
        for path, text in self.files.items():
            for block in iter_blocks(text):
                if block.keyword == keyword:
                    found.append((path, block))
        return found

    def _line_of(self, text: str, offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    def _has_utf8_bom(self, path: Path) -> bool:
        """Detect a UTF-8 BOM from the raw bytes (text is read as utf-8-sig)."""
        try:
            with path.open("rb") as handle:
                return handle.read(3) == b"\xef\xbb\xbf"
        except OSError:
            return False

    def _git(self, *args: str) -> Optional[subprocess.CompletedProcess]:
        """Run a read-only git query, or return None when git is unavailable."""
        if self._git_ok is False:
            return None
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.config_dir), *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self._git_ok = False
            return None
        if completed.returncode != 0 and "not a git repository" in (completed.stderr or ""):
            self._git_ok = False
            return None
        self._git_ok = True
        return completed

    def _is_tracked(self, path: Path) -> Optional[bool]:
        """True/False when git can answer, None when it cannot."""
        if self._git("rev-parse", "--is-inside-work-tree") is None:
            return None
        completed = self._git("ls-files", "--error-unmatch", "--", str(self._rel(path)))
        if completed is None:
            return None
        return completed.returncode == 0

    def _is_ignored(self, path: Path) -> Optional[bool]:
        if self._git("rev-parse", "--is-inside-work-tree") is None:
            return None
        completed = self._git("check-ignore", "-q", "--", str(self._rel(path)))
        if completed is None:
            return None
        return completed.returncode == 0

    # -- T001 ------------------------------------------------------------------

    def check_required_files(self) -> None:
        """Is this a Terraform directory, and which canonical files exist?"""
        if not self.files:
            self.report.add(
                "T001",
                SEVERITY_ERROR,
                "no *.tf files found -- this directory is not a Terraform "
                "configuration module (or the wrong path was given).",
            )
            return

        names = {p.name for p in self.files}
        for concern, candidates in CANONICAL_FILES.items():
            if not names.intersection(candidates):
                self.report.add(
                    "T001",
                    SEVERITY_INFO,
                    f"no dedicated {concern} file ({' / '.join(candidates)}); "
                    "declarations may live in a combined file.",
                )

        self.report.add(
            "T001",
            SEVERITY_INFO,
            f"{len(self.files)} Terraform source file(s) scanned: "
            + ", ".join(sorted(p.name for p in self.files)),
        )

    # -- T002 ------------------------------------------------------------------

    def check_terraform_block(self) -> None:
        """The terraform block must exist and pin a required_version."""
        blocks = self._blocks("terraform")
        if not blocks:
            self.report.add(
                "T002",
                SEVERITY_ERROR,
                "no terraform {} block found in any *.tf file; the provider and "
                "required_version constraints are undeclared.",
            )
            return

        for path, block in blocks:
            version_match = re.search(
                r'^\s*required_version\s*=\s*"(?P<value>[^"]*)"',
                block.body,
                re.MULTILINE,
            )
            if version_match is None:
                if re.search(r"^\s*required_version\s*=", block.body, re.MULTILINE):
                    self.report.add(
                        "T002",
                        SEVERITY_WARNING,
                        "required_version is not a quoted string literal; use "
                        'required_version = ">= 1.5.0".',
                        path=path,
                        line=block.line,
                    )
                else:
                    self.report.add(
                        "T002",
                        SEVERITY_ERROR,
                        "terraform block does not declare required_version; "
                        "Terraform will otherwise run on any version.",
                        path=path,
                        line=block.line,
                    )
                continue

            value = version_match.group("value").strip()
            if not value:
                self.report.add(
                    "T002",
                    SEVERITY_WARNING,
                    'required_version is empty; use a real constraint such as ">= 1.5.0".',
                    path=path,
                    line=block.line,
                )
            elif re.fullmatch(r">=\s*0(\.0)*", value):
                self.report.add(
                    "T002",
                    SEVERITY_WARNING,
                    f'required_version "{value}" does not exclude anything; '
                    'use a meaningful minimum such as ">= 1.5.0".',
                    path=path,
                    line=block.line,
                )
            else:
                self.report.add(
                    "T002",
                    SEVERITY_INFO,
                    f'required_version = "{value}"',
                    path=path,
                    line=block.line + block.body.count("\n", 0, version_match.start()),
                )

    # -- T003 ------------------------------------------------------------------

    def check_required_providers(self) -> None:
        """required_providers must exist, and each provider must be pinned."""
        terraform_blocks = self._blocks("terraform")
        if not terraform_blocks:
            return  # T002 already reported the missing block

        declared: Dict[str, bool] = {}
        found_any = False

        for path, block in terraform_blocks:
            rp_match = re.search(r"required_providers\s*\{", block.body)
            if rp_match is None:
                continue
            found_any = True
            brace_index = block.body.find("{", rp_match.start())
            close = _match_brace(block.body, brace_index)
            rp_body = block.body[brace_index + 1: (close if close >= 0 else len(block.body))]
            base_line = block.line + block.body.count("\n", 0, brace_index)

            for entry in re.finditer(
                r"^[ \t]*(?P<name>[A-Za-z0-9_-]+)[ \t]*=[ \t]*\{", rp_body, re.MULTILINE
            ):
                name = entry.group("name")
                entry_open = rp_body.find("{", entry.start())
                entry_close = _match_brace(rp_body, entry_open)
                entry_body = rp_body[entry_open + 1: (entry_close if entry_close >= 0 else len(rp_body))]
                entry_line = base_line + rp_body.count("\n", 0, entry.start())
                declared[name] = True

                source_match = re.search(r'\bsource\s*=\s*"([^"]*)"', entry_body)
                version_match = re.search(r'\bversion\s*=\s*"([^"]*)"', entry_body)

                if source_match is None:
                    self.report.add(
                        "T003",
                        SEVERITY_WARNING,
                        f'provider "{name}" has no source attribute; Terraform '
                        "assumes registry.terraform.io/hashicorp/" + name + ".",
                        path=path,
                        line=entry_line,
                    )
                if version_match is None:
                    self.report.add(
                        "T003",
                        SEVERITY_WARNING,
                        f'provider "{name}" is not version-pinned; add a version '
                        'constraint (e.g. "~> 5.0") for reproducible plans.',
                        path=path,
                        line=entry_line,
                    )

        if not found_any:
            self.report.add(
                "T003",
                SEVERITY_ERROR,
                "no required_providers block found; provider versions are "
                "unpinned and `terraform init` may install a different major "
                "version than the team tested with.",
            )
            return

        # Configured provider blocks should be declared in required_providers.
        for path, block in self._blocks("provider"):
            name = block.labels[0] if block.labels else "<unnamed>"
            if name not in declared:
                self.report.add(
                    "T003",
                    SEVERITY_WARNING,
                    f'provider "{name}" is configured but missing from '
                    "required_providers; its version is not pinned.",
                    path=path,
                    line=block.line,
                )

    # -- T004 ------------------------------------------------------------------

    def check_provider_blocks(self) -> None:
        """At least one provider configuration block should exist."""
        blocks = self._blocks("provider")
        if not blocks:
            self.report.add(
                "T004",
                SEVERITY_ERROR,
                "no provider block found; Terraform has no configuration for "
                "the cloud it should target.",
            )
            return
        for path, block in blocks:
            name = block.labels[0] if block.labels else "<unnamed>"
            if not block.body.strip():
                self.report.add(
                    "T004",
                    SEVERITY_WARNING,
                    f'provider "{name}" block is empty.',
                    path=path,
                    line=block.line,
                )
            else:
                self.report.add(
                    "T004",
                    SEVERITY_INFO,
                    f'provider "{name}" configured',
                    path=path,
                    line=block.line,
                )

    # -- T005 ------------------------------------------------------------------

    def check_variables(self) -> None:
        """Variable blocks: unique names, type and description present."""
        blocks = self._blocks("variable")
        if not blocks:
            self.report.add(
                "T005",
                SEVERITY_WARNING,
                "no variable blocks declared; every value is hardcoded, which "
                "makes the module hard to reuse across accounts/regions.",
            )
            return

        seen: Dict[str, Path] = {}
        for path, block in blocks:
            name = block.labels[0] if block.labels else "<unnamed>"
            if name in seen:
                self.report.add(
                    "T005",
                    SEVERITY_ERROR,
                    f'variable "{name}" is declared more than once '
                    f"(first in {seen[name].name}).",
                    path=path,
                    line=block.line,
                )
                continue
            seen[name] = path

            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                self.report.add(
                    "T005",
                    SEVERITY_WARNING,
                    f'variable "{name}" does not follow the Terraform naming '
                    "convention (lowercase letters, digits and underscores).",
                    path=path,
                    line=block.line,
                )
            if re.search(r"^\s*type\s*=", block.body, re.MULTILINE) is None:
                self.report.add(
                    "T005",
                    SEVERITY_WARNING,
                    f'variable "{name}" has no type; it will be inferred as a '
                    "string, which breaks list/map/object callers.",
                    path=path,
                    line=block.line,
                )
            if re.search(r"^\s*description\s*=", block.body, re.MULTILINE) is None:
                self.report.add(
                    "T005",
                    SEVERITY_WARNING,
                    f'variable "{name}" has no description; `terraform plan` '
                    "prompts will not explain what to supply.",
                    path=path,
                    line=block.line,
                )

        summary = (
            f"{len(seen)} unique variable(s) from {len(blocks)} declaration(s)"
            if len(seen) != len(blocks)
            else f"{len(seen)} variable(s) declared"
        )
        self.report.add(
            "T005",
            SEVERITY_INFO,
            f"{summary}: " + ", ".join(sorted(seen)),
        )

    # -- T006 ------------------------------------------------------------------

    def check_outputs(self) -> None:
        """Output blocks: unique names, value and description present."""
        blocks = self._blocks("output")
        if not blocks:
            self.report.add(
                "T006",
                SEVERITY_WARNING,
                "no output blocks declared; consumers (e.g. the Ansible "
                "inventory step documented in infra/ansible/) cannot read the "
                "generated IPs and DNS names via `terraform output`.",
            )
            return

        seen: Dict[str, Path] = {}
        for path, block in blocks:
            name = block.labels[0] if block.labels else "<unnamed>"
            if name in seen:
                self.report.add(
                    "T006",
                    SEVERITY_ERROR,
                    f'output "{name}" is declared more than once '
                    f"(first in {seen[name].name}).",
                    path=path,
                    line=block.line,
                )
                continue
            seen[name] = path

            if re.search(r"^\s*value\s*=", block.body, re.MULTILINE) is None:
                self.report.add(
                    "T006",
                    SEVERITY_ERROR,
                    f'output "{name}" has no value expression.',
                    path=path,
                    line=block.line,
                )
            if re.search(r"^\s*description\s*=", block.body, re.MULTILINE) is None:
                self.report.add(
                    "T006",
                    SEVERITY_WARNING,
                    f'output "{name}" has no description.',
                    path=path,
                    line=block.line,
                )

        self.report.add(
            "T006",
            SEVERITY_INFO,
            f"{len(seen)} output(s) declared: " + ", ".join(sorted(seen)),
        )

    # -- T007 ------------------------------------------------------------------

    def check_tfvars_example(self) -> None:
        """The example tfvars file should mirror the declared variables."""
        examples = sorted(self.config_dir.glob("*.tfvars.example")) + sorted(
            self.config_dir.glob("*.tfvars.sample")
        )
        examples = [p for p in examples if p.is_file()]
        if not examples:
            self.report.add(
                "T007",
                SEVERITY_INFO,
                "no *.tfvars.example file; there is no committed template "
                "showing which values a teammate must supply.",
            )
            return

        declared = {
            block.labels[0]
            for _, block in self._blocks("variable")
            if block.labels
        }
        required = {
            block.labels[0]
            for _, block in self._blocks("variable")
            if block.labels
            and re.search(r"^\s*default\s*=", block.body, re.MULTILINE) is None
        }

        for example in examples:
            try:
                text = example.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                self.report.add(
                    "T007", SEVERITY_WARNING, f"cannot read example file: {exc}", path=example
                )
                continue

            entries = parse_tfvars_assignments(text)

            for key, (value, line, active) in sorted(entries.items()):
                if key not in declared:
                    self.report.add(
                        "T007",
                        SEVERITY_ERROR,
                        f'example sets "{key}", which is not a declared variable; '
                        "`terraform plan` would reject this file.",
                        path=example,
                        line=line,
                    )
                elif active and value and not value.startswith(("{", "[")):
                    self.report.add(
                        "T007",
                        SEVERITY_INFO,
                        f'{key} = {value}',
                        path=example,
                        line=line,
                    )

            for name in sorted(required):
                if name not in entries:
                    self.report.add(
                        "T007",
                        SEVERITY_WARNING,
                        f'variable "{name}" has no default but is not documented '
                        f"in {example.name}; a teammate would not know to set it.",
                        path=example,
                    )

        self.report.add(
            "T007",
            SEVERITY_INFO,
            "example file(s): " + ", ".join(p.name for p in examples),
        )

    # -- T008 ------------------------------------------------------------------

    def check_forbidden_artifacts(self) -> None:
        """Flag state, plan and real tfvars files that could be committed."""
        for pattern, tracked_severity, description in ARTIFACT_PATTERNS:
            for path in sorted(self.config_dir.glob(pattern)):
                if not path.is_file():
                    continue
                tracked = self._is_tracked(path)
                rel = self._rel(path)
                if tracked is True:
                    self.report.add(
                        "T008",
                        tracked_severity,
                        f"{rel} is tracked by git -- {description}. Remove it "
                        "from the index and add it to .gitignore.",
                        path=path,
                    )
                elif tracked is False:
                    ignored = self._is_ignored(path)
                    if ignored is False:
                        self.report.add(
                            "T008",
                            SEVERITY_WARNING,
                            f"{rel} exists locally, is untracked, and is NOT "
                            f"ignored by git -- {description}. Add it to "
                            ".gitignore before it is committed by accident.",
                            path=path,
                        )
                    else:
                        self.report.add(
                            "T008",
                            SEVERITY_INFO,
                            f"{rel} exists locally but is untracked and ignored "
                            "(expected for local runs); keep it out of commits.",
                            path=path,
                        )
                else:
                    self.report.add(
                        "T008",
                        tracked_severity,
                        f"{rel} exists in the module directory -- {description}. "
                        "Confirm it is git-ignored and never committed.",
                        path=path,
                    )

        terraform_dir = self.config_dir / ".terraform"
        if terraform_dir.is_dir():
            self.report.add(
                "T008",
                SEVERITY_INFO,
                ".terraform/ exists (local `terraform init` cache); it must "
                "stay untracked and is normally machine-specific.",
                path=terraform_dir,
            )

        lock_file = self.config_dir / ".terraform.lock.hcl"
        if lock_file.is_file():
            self.report.add(
                "T008",
                SEVERITY_INFO,
                ".terraform.lock.hcl exists; this repository ignores it, so "
                "providers are resolved fresh on each machine.",
                path=lock_file,
            )

    # -- T009 ------------------------------------------------------------------

    def check_gitignore_hygiene(self) -> None:
        """The module .gitignore must block state, tfvars and .terraform/."""
        gitignore = self.config_dir / ".gitignore"
        if not gitignore.is_file():
            self.report.add(
                "T009",
                SEVERITY_WARNING,
                "no .gitignore in the Terraform directory; state files and "
                "real terraform.tfvars could be committed by accident.",
            )
            return

        try:
            text = gitignore.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            self.report.add(
                "T009", SEVERITY_WARNING, f"cannot read .gitignore: {exc}", path=gitignore
            )
            return

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        for label, needles, example_pattern in GITIGNORE_REQUIREMENTS:
            if not any(any(n in line for n in needles) for line in lines):
                self.report.add(
                    "T009",
                    SEVERITY_WARNING,
                    f".gitignore does not ignore {label}; add {example_pattern}.",
                    path=gitignore,
                )

        ignores_tfvars = any("tfvars" in line and not line.startswith("!") for line in lines)
        un_ignores_example = any(
            line.startswith("!") and "tfvars.example" in line for line in lines
        )
        if ignores_tfvars and not un_ignores_example:
            self.report.add(
                "T009",
                SEVERITY_WARNING,
                "*.tfvars is ignored but the committed example is not "
                "un-ignored; add `!*.tfvars.example` so the template can be "
                "committed.",
                path=gitignore,
            )

    # -- T010 ------------------------------------------------------------------

    def check_file_hygiene(self) -> None:
        """Empty files, BOMs, missing trailing newlines, stray root entries."""
        for path, text in self.files.items():
            if not text.strip():
                self.report.add(
                    "T010",
                    SEVERITY_WARNING,
                    f"{path.name} is empty; delete it or add the declarations "
                    "it is meant to hold.",
                    path=path,
                )
                continue
            if self._has_utf8_bom(path):
                self.report.add(
                    "T010",
                    SEVERITY_WARNING,
                    f"{path.name} starts with a UTF-8 byte-order mark, which "
                    "some Terraform versions and diff tooling mishandle.",
                    path=path,
                )
            if not text.endswith("\n"):
                self.report.add(
                    "T010",
                    SEVERITY_INFO,
                    f"{path.name} does not end with a newline.",
                    path=path,
                )

        artifact_names = set()
        for pattern, _, _ in ARTIFACT_PATTERNS:
            artifact_names.update(p.name for p in self.config_dir.glob(pattern))

        for entry in sorted(self.config_dir.iterdir(), key=lambda p: p.name):
            name = entry.name
            if name in ALLOWED_ROOT_NAMES or name in artifact_names:
                continue
            if entry.is_dir() and name in ALLOWED_ROOT_DIRS:
                continue
            if entry.is_file() and name.endswith(ALLOWED_ROOT_SUFFIXES):
                continue
            self.report.add(
                "T010",
                SEVERITY_WARNING,
                f"unexpected {'directory' if entry.is_dir() else 'file'} "
                f"'{name}' in the Terraform module root; module sources, "
                "backups and editor files usually belong elsewhere.",
                path=entry,
            )

    # -- T011 ------------------------------------------------------------------

    def check_secret_scan(self) -> None:
        """Scan sources and the example file for real credential material."""
        targets = list(self.files.items())
        for pattern in ("*.tfvars.example", "*.tfvars.sample"):
            for example in sorted(self.config_dir.glob(pattern)):
                if example.is_file():
                    targets.append(
                        (example, example.read_text(encoding="utf-8-sig", errors="replace"))
                    )

        for path, text in targets:
            for label, regex, description in SECRET_PATTERNS:
                for match in regex.finditer(text):
                    self.report.add(
                        "T011",
                        SEVERITY_ERROR,
                        f"possible {label}: {description}. Remove the value and "
                        "supply it at apply time via environment variables or an "
                        "ignored terraform.tfvars.",
                        path=path,
                        line=self._line_of(text, match.start()),
                    )

    # -- T012 ------------------------------------------------------------------

    def check_variable_usage(self) -> None:
        """var.X references must resolve, and declared variables should be used."""
        declared = {
            block.labels[0]
            for _, block in self._blocks("variable")
            if block.labels
        }
        referenced: Dict[str, Tuple[Path, int]] = {}
        for path, text in self.files.items():
            scrubbed = strip_hcl_comments(text)
            for match in re.finditer(r"\bvar\.([A-Za-z_][A-Za-z0-9_]*)", scrubbed):
                name = match.group(1)
                referenced.setdefault(name, (path, self._line_of(scrubbed, match.start())))

        for name in sorted(referenced):
            if name not in declared:
                path, line = referenced[name]
                self.report.add(
                    "T012",
                    SEVERITY_ERROR,
                    f"var.{name} is referenced but never declared; "
                    "`terraform validate` would fail with an undeclared "
                    "variable error.",
                    path=path,
                    line=line,
                )

        for name in sorted(declared - set(referenced)):
            self.report.add(
                "T012",
                SEVERITY_INFO,
                f'variable "{name}" is declared but never referenced; consider '
                "removing it or wiring it into a resource.",
            )

    # -- orchestration ---------------------------------------------------------

    def validate(self) -> ValidationReport:
        """Run every check, isolating failures so one bug cannot hide the rest."""
        self.load()
        self.report.checks_run = [check_id for check_id, _, _ in CHECKS]
        checks = (
            self.check_required_files,
            self.check_terraform_block,
            self.check_required_providers,
            self.check_provider_blocks,
            self.check_variables,
            self.check_outputs,
            self.check_tfvars_example,
            self.check_forbidden_artifacts,
            self.check_gitignore_hygiene,
            self.check_file_hygiene,
            self.check_secret_scan,
            self.check_variable_usage,
        )
        for check in checks:
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - defensive: keep other checks running
                self.report.add(
                    "T999",
                    SEVERITY_WARNING,
                    f"{check.__name__} could not complete: {exc.__class__.__name__}: {exc}",
                )
        return self.report


# ==============================================================================
# Output formatting
# ==============================================================================

def _label(finding: Finding) -> str:
    if finding.path is None:
        return ""
    location = f"{finding.path.name}"
    if finding.line:
        location += f":{finding.line}"
    return location


def format_report(report: ValidationReport, verbose: bool = False, quiet: bool = False) -> str:
    """Render the report for the terminal."""
    lines: List[str] = []
    sep = "=" * 72
    subsep = "-" * 72

    lines.append(sep)
    lines.append(f"{Colors.BOLD}AutoPod Terraform Configuration Validation{Colors.RESET}")
    lines.append(f"Directory: {Colors.CYAN}{report.config_dir}{Colors.RESET}")
    lines.append(f"Files:     {len(report.tf_files)} Terraform source file(s)")
    lines.append(sep)

    if verbose:
        lines.append(f"Checks run: {', '.join(report.checks_run)}")

    findings = report.sorted_findings()
    if not findings:
        lines.append(f"{Colors.GREEN}No findings.{Colors.RESET}")

    if not quiet:
        for severity, heading in (
            (SEVERITY_ERROR, "Errors"),
            (SEVERITY_WARNING, "Warnings"),
            (SEVERITY_INFO, "Information"),
        ):
            bucket = [f for f in findings if f.severity == severity]
            if not bucket:
                continue
            if severity == SEVERITY_INFO and not verbose:
                continue
            color = SEVERITY_COLORS[severity]()
            lines.append("")
            lines.append(f"{color}{Colors.BOLD}{heading} ({len(bucket)}):{Colors.RESET}")
            lines.append(subsep)
            for finding in bucket:
                location = _label(finding)
                suffix = f" {Colors.DIM}[{location}]{Colors.RESET}" if location else ""
                lines.append(
                    f"  {color}[{finding.severity:<7}] {finding.check}{Colors.RESET} "
                    f"{finding.message}{suffix}"
                )

    lines.append("")
    lines.append(sep)
    if report.has_errors:
        status = f"{Colors.RED}{Colors.BOLD}FAILED{Colors.RESET}"
        detail = f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
    else:
        status = f"{Colors.GREEN}{Colors.BOLD}PASSED{Colors.RESET}"
        detail = f"0 error(s), {len(report.warnings)} warning(s)"
        if report.warnings:
            detail += " -- review the warnings above"
    lines.append(f"Validation Status: {status} - {detail}.")
    lines.append(f"{Colors.DIM}Static checks only; `terraform validate` and "
                 f"`terraform plan` remain the authoritative checks.{Colors.RESET}")
    lines.append(sep)

    return "\n".join(lines)


# ==============================================================================
# CLI
# ==============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="validate_terraform_config.py",
        description=(
            "Offline static checks for an AutoPod Terraform configuration "
            "directory: required files, provider/version pinning, variables, "
            "outputs, example tfvars, committed state/secrets, and directory "
            "hygiene. Never contacts AWS and never modifies files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python validate_terraform_config.py
    python validate_terraform_config.py infra/terraform
    python validate_terraform_config.py --strict --verbose
    python validate_terraform_config.py infra/terraform --no-color
    python validate_terraform_config.py --list-checks

Exit codes:
    0  no errors (warnings may still be present)
    1  errors found (or warnings present with --strict)
    2  usage error / target is not a directory
        """,
    )
    parser.add_argument(
        "config_dir",
        nargs="?",
        default=None,
        metavar="CONFIG_DIR",
        help=(
            "Terraform configuration directory to inspect. Defaults to the "
            "current directory when it contains *.tf files, otherwise to the "
            "directory containing this script."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures (exit code 1).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show informational findings and the checks that were run.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress the findings sections; print only the final status.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output.",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="List the check registry (id, name, purpose) and exit.",
    )
    return parser


def resolve_target(explicit: Optional[str]) -> Optional[Path]:
    """Work out which directory to validate, or None when it cannot be found."""
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate.resolve() if candidate.is_dir() else None

    cwd = Path.cwd()
    if any(cwd.glob("*.tf")):
        return cwd

    script_dir = Path(__file__).resolve().parent
    if any(script_dir.glob("*.tf")):
        return script_dir

    return script_dir if script_dir.is_dir() else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_checks:
        width = max(len(check_id) for check_id, _, _ in CHECKS)
        for check_id, name, purpose in CHECKS:
            print(f"{check_id:<{width}}  {name:<20} {purpose}")
        return EXIT_OK

    if args.no_color or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        Colors.disable()

    target = resolve_target(args.config_dir)
    if target is None:
        sys.stderr.write(
            f"{Colors.RED}Error:{Colors.RESET} "
            f"'{args.config_dir}' is not an existing directory.\n"
            "Usage: python validate_terraform_config.py [CONFIG_DIR] [--strict] [--verbose]\n"
        )
        return EXIT_USAGE

    report = TerraformConfigValidator(target).validate()
    print(format_report(report, verbose=args.verbose, quiet=args.quiet))

    if report.has_errors:
        return EXIT_FINDINGS
    if args.strict and report.has_warnings:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
