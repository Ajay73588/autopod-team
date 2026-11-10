#!/usr/bin/env python3
"""
Ansible Inventory Validation Utility
====================================
Validates Ansible inventory files for structural correctness, presence of
host groups and host entries, malformed syntax, and security risks such as
hardcoded passwords or private key paths.

Designed for AutoPod Cloud infrastructure automation.
Compatible with standard Python libraries (Python 3.8+).

Usage:
    python validate_inventory.py [path/to/inventory.ini]
    python validate_inventory.py -i [path/to/inventory.ini] [--strict] [--verbose]
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Dict, List, Optional, Set, Tuple


# ==============================================================================
# Security & Pattern Definitions
# ==============================================================================

# Variable names that should never store plaintext secrets
SENSITIVE_VAR_PATTERNS = [
    re.compile(r"^ansible_(?:ssh_)?pass(?:word)?$", re.IGNORECASE),
    re.compile(r"^ansible_become_pass(?:word)?$", re.IGNORECASE),
    re.compile(r"^ansible_sudo_pass(?:word)?$", re.IGNORECASE),
    re.compile(r"^(?:db_|admin_|root_|app_)?password$", re.IGNORECASE),
    re.compile(r"^(?:api_|auth_|access_|secret_)?token$", re.IGNORECASE),
    re.compile(r"^(?:api_|access_)?secret(?:_key)?$", re.IGNORECASE),
]

# Variable names that specify private key paths
PRIVATE_KEY_VAR_PATTERNS = [
    re.compile(r"^ansible_(?:ssh_)?private_key_file$", re.IGNORECASE),
    re.compile(r"^(?:ssh_)?private_key_file$", re.IGNORECASE),
    re.compile(r"^(?:ssl_|tls_)?key_file$", re.IGNORECASE),
]

# Inline private key banner detection
INLINE_KEY_PATTERN = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|OPENSSH\s+|EC\s+|DSA\s+)?PRIVATE\s+KEY-----",
    re.IGNORECASE,
)

# Valid group identifier: alphanumeric, underscores, hyphens
GROUP_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")

# Valid host name or Ansible host range (e.g., node[01:10], web-1.local)
HOSTNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.\[\]:]+$")

# Valid variable name identifier
VAR_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Standard known Ansible connection plugins
STANDARD_CONNECTIONS = {
    "ssh", "local", "docker", "podman", "winrm", "paramiko",
    "smart", "kubectl", "chroot", "buildah", "jail", "zone"
}


# ==============================================================================
# Helper Functions
# ==============================================================================

def is_templated_or_vault(value: str) -> bool:
    """Check if a variable value is templated, a vault reference, or an env lookup."""
    v = value.strip().strip("'\"")
    if not v:
        return True
    if (v.startswith("{{") and v.endswith("}}")) or (v.startswith("{%") and v.endswith("%}")):
        return True
    if v.startswith("!vault") or "$ANSIBLE_VAULT" in v:
        return True
    if v.startswith("lookup(") or v.startswith("env("):
        return True
    return False


def is_sensitive_variable(key: str) -> bool:
    """Check if variable name matches sensitive credential patterns."""
    return any(pattern.match(key) for pattern in SENSITIVE_VAR_PATTERNS)


def is_private_key_variable(key: str) -> bool:
    """Check if variable name specifies a private key file."""
    return any(pattern.match(key) for pattern in PRIVATE_KEY_VAR_PATTERNS)


def mask_credential(value: str) -> str:
    """Safely mask credential strings to prevent accidental exposure."""
    return "******** [REDACTED]"


def sanitize_path(path_str: str) -> str:
    """Return a sanitized representation of a file path."""
    clean = path_str.strip().strip("'\"")
    # Mask username in /home/<user> or C:\\Users\\<user>
    clean = re.sub(r"([/\\]home[/\\])[^/\\]+", r"\1<user>", clean)
    clean = re.sub(r"([/\\]Users[/\\])[^/\\]+", r"\1<user>", clean, flags=re.IGNORECASE)
    return clean


# ==============================================================================
# Validation Result Model
# ==============================================================================

class InventoryReport:
    """Collects and organizes inventory analysis results, warnings, and errors."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.format_detected = "INI"
        self.errors: List[Tuple[Optional[int], str]] = []
        self.warnings: List[Tuple[Optional[int], str]] = []
        self.groups: Dict[str, List[str]] = {}
        self.children: Dict[str, List[str]] = {}
        self.group_vars: Dict[str, Dict[str, str]] = {}
        self.host_vars: Dict[str, Dict[str, str]] = {}
        self.all_hosts: Set[str] = set()

    def add_error(self, message: str, line_no: Optional[int] = None) -> None:
        self.errors.append((line_no, message))

    def add_warning(self, message: str, line_no: Optional[int] = None) -> None:
        self.warnings.append((line_no, message))

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ==============================================================================
# INI Inventory Parser & Validator
# ==============================================================================

class AnsibleInventoryValidator:
    """Parses and validates Ansible inventory files."""

    def __init__(self, file_path: Path, strict: bool = False, verbose: bool = False):
        self.file_path = file_path
        self.strict = strict
        self.verbose = verbose
        self.report = InventoryReport(file_path)

    def validate(self) -> InventoryReport:
        """Run all structural, grammatical, and security validations."""
        if not self._check_file_access():
            return self.report

        self._parse_ini_content()
        self._validate_cross_references()
        self._validate_minimum_requirements()

        return self.report

    def _check_file_access(self) -> bool:
        """Verify the inventory file exists, is readable, and is not empty."""
        if not self.file_path.exists():
            self.report.add_error(f"File not found: '{self.file_path}'")
            return False

        if not self.file_path.is_file():
            self.report.add_error(f"Path is not a regular file: '{self.file_path}'")
            return False

        try:
            stat_info = self.file_path.stat()
            if stat_info.st_size == 0:
                self.report.add_error("Inventory file is completely empty (0 bytes).")
                return False
        except OSError as err:
            self.report.add_error(f"Unable to read file metadata: {err}")
            return False

        return True

    def _parse_ini_content(self) -> None:
        """Line-by-line parsing of INI-formatted inventory."""
        try:
            with open(self.file_path, "r", encoding="utf-8", errors="replace") as fh:
                raw_lines = fh.readlines()
        except OSError as err:
            self.report.add_error(f"Failed to open inventory file: {err}")
            return

        current_group: Optional[str] = None
        current_section_type = "hosts"  # "hosts", "vars", or "children"
        non_empty_lines = 0

        for line_no, raw_line in enumerate(raw_lines, start=1):
            line = raw_line.strip()

            # Security check: detect inline private keys anywhere in the line
            if INLINE_KEY_PATTERN.search(raw_line):
                self.report.add_error(
                    "Inline private key block detected! Private keys must NEVER be stored in inventory files.",
                    line_no=line_no,
                )
                continue

            # Skip empty lines and full comment lines (# or ;)
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            non_empty_lines += 1

            # Check for section header [group] or [group:vars] or [group:children]
            if line.startswith("["):
                header_info = self._parse_section_header(line, line_no)
                if header_info:
                    current_group, current_section_type = header_info
                continue

            # Process entries inside section
            if current_section_type == "hosts":
                self._parse_host_line(line, line_no, current_group)
            elif current_section_type == "vars":
                self._parse_var_line(line, line_no, current_group)
            elif current_section_type == "children":
                self._parse_child_line(line, line_no, current_group)

        if non_empty_lines == 0:
            self.report.add_error("Inventory file contains only whitespace and comments.")

    def _parse_section_header(
        self, line: str, line_no: int
    ) -> Optional[Tuple[str, str]]:
        """Validate and parse a section header [group_name]."""
        if not line.endswith("]"):
            self.report.add_error(
                f"Malformed group header (missing closing bracket ']'): '{line}'",
                line_no=line_no,
            )
            return None

        inner = line[1:-1].strip()
        if not inner:
            self.report.add_error("Empty group header '[]'", line_no=line_no)
            return None

        parts = inner.split(":")
        if len(parts) == 1:
            group_name = parts[0].strip()
            section_type = "hosts"
        elif len(parts) == 2:
            group_name = parts[0].strip()
            suffix = parts[1].strip()
            if suffix not in ("vars", "children"):
                self.report.add_error(
                    f"Invalid group header suffix ':{suffix}' in '{line}'. "
                    "Only ':vars' and ':children' are supported by Ansible.",
                    line_no=line_no,
                )
                return None
            section_type = suffix
        else:
            self.report.add_error(
                f"Malformed group header '{line}' with multiple colons.",
                line_no=line_no,
            )
            return None

        # Validate base group name (or 'all')
        if not GROUP_NAME_PATTERN.match(group_name) and group_name != "all":
            self.report.add_error(
                f"Invalid group name '{group_name}'. Group names should contain only "
                "alphanumeric characters, underscores, and hyphens.",
                line_no=line_no,
            )
            return None

        # Initialize group tracking
        if section_type == "hosts":
            if group_name not in self.report.groups:
                self.report.groups[group_name] = []
        elif section_type == "vars":
            if group_name not in self.report.group_vars:
                self.report.group_vars[group_name] = {}
        elif section_type == "children":
            if group_name not in self.report.children:
                self.report.children[group_name] = []

        return group_name, section_type

    def _parse_host_line(
        self, line: str, line_no: int, current_group: Optional[str]
    ) -> None:
        """Parse a host definition line with host-specific variables."""
        if current_group is None:
            # Ungrouped host before any header
            current_group = "ungrouped"
            if current_group not in self.report.groups:
                self.report.groups[current_group] = []
            self.report.add_warning(
                "Host defined before any group header; assigned to implicit 'ungrouped' group.",
                line_no=line_no,
            )

        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as err:
            self.report.add_error(
                f"Malformed line syntax (quote error): {err}",
                line_no=line_no,
            )
            return

        if not tokens:
            return

        host_name = tokens[0]

        # Hostname validation
        if "=" in host_name:
            self.report.add_error(
                f"Malformed host entry: missing hostname before variable assignment '{host_name}'.",
                line_no=line_no,
            )
            return

        if not HOSTNAME_PATTERN.match(host_name):
            self.report.add_error(
                f"Malformed hostname '{host_name}'. Hostnames should contain valid characters or ranges.",
                line_no=line_no,
            )
            return

        # Check duplicate hosts within the same group
        if host_name in self.report.groups.get(current_group, []):
            self.report.add_warning(
                f"Duplicate host '{host_name}' declared in group '[{current_group}]'.",
                line_no=line_no,
            )
        else:
            self.report.groups[current_group].append(host_name)

        self.report.all_hosts.add(host_name)

        # Parse host variables (tokens 1..N)
        if host_name not in self.report.host_vars:
            self.report.host_vars[host_name] = {}

        for token in tokens[1:]:
            if "=" not in token:
                self.report.add_error(
                    f"Malformed host variable token '{token}' (expected 'key=value').",
                    line_no=line_no,
                )
                continue

            var_name, sep, var_val = token.partition("=")
            var_name = var_name.strip()
            var_val = var_val.strip()

            if not var_name:
                self.report.add_error(
                    f"Malformed variable assignment '{token}' (missing variable name before '=').",
                    line_no=line_no,
                )
                continue

            if not VAR_NAME_PATTERN.match(var_name):
                self.report.add_warning(
                    f"Non-standard variable name '{var_name}' in host '{host_name}'.",
                    line_no=line_no,
                )

            self._validate_variable_value(var_name, var_val, line_no, context=f"host '{host_name}'")
            self.report.host_vars[host_name][var_name] = var_val

    def _parse_var_line(
        self, line: str, line_no: int, current_group: Optional[str]
    ) -> None:
        """Parse a variable assignment inside a [:vars] section."""
        if current_group is None:
            self.report.add_error(
                "Variable defined outside of any group section.",
                line_no=line_no,
            )
            return

        # Strip inline comments if present outside quotes
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as err:
            self.report.add_error(f"Quote syntax error in variable definition: {err}", line_no=line_no)
            return

        if not tokens:
            return

        reconstructed = " ".join(tokens)

        # In INI vars sections, variables can be key=val or key: val
        if "=" in reconstructed:
            var_name, _, var_val = reconstructed.partition("=")
        elif ":" in reconstructed:
            var_name, _, var_val = reconstructed.partition(":")
        else:
            self.report.add_error(
                f"Malformed variable assignment '{reconstructed}' in '[{current_group}:vars]'. Expected 'key=value'.",
                line_no=line_no,
            )
            return

        var_name = var_name.strip()
        var_val = var_val.strip()

        if not var_name:
            self.report.add_error(
                f"Missing variable name in '[{current_group}:vars]' assignment: '{line}'.",
                line_no=line_no,
            )
            return

        if not VAR_NAME_PATTERN.match(var_name):
            self.report.add_warning(
                f"Non-standard variable name '{var_name}' in '[{current_group}:vars]'.",
                line_no=line_no,
            )

        self._validate_variable_value(
            var_name, var_val, line_no, context=f"group '[{current_group}:vars]'"
        )
        self.report.group_vars[current_group][var_name] = var_val

    def _parse_child_line(
        self, line: str, line_no: int, current_group: Optional[str]
    ) -> None:
        """Parse a child group reference inside a [:children] section."""
        if current_group is None:
            self.report.add_error(
                "Child group defined outside of any group header.",
                line_no=line_no,
            )
            return

        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as err:
            self.report.add_error(f"Syntax error in child group line: {err}", line_no=line_no)
            return

        if not tokens:
            return

        if len(tokens) > 1 or "=" in tokens[0]:
            self.report.add_error(
                f"Malformed child entry '{line}' in '[{current_group}:children]'. "
                "Child groups must contain only single group names without variables.",
                line_no=line_no,
            )
            return

        child_name = tokens[0].strip()

        if child_name == current_group:
            self.report.add_error(
                f"Self-referential child group: '[{current_group}:children]' cannot include itself.",
                line_no=line_no,
            )
            return

        if not GROUP_NAME_PATTERN.match(child_name):
            self.report.add_error(
                f"Malformed child group name '{child_name}' in '[{current_group}:children]'.",
                line_no=line_no,
            )
            return

        self.report.children[current_group].append(child_name)

    def _validate_variable_value(
        self, key: str, value: str, line_no: int, context: str
    ) -> None:
        """Validate variable parameters and perform security checks."""
        # 1. Parameter: ansible_host
        if key == "ansible_host":
            val_clean = value.strip("'\"")
            # If it resembles an IP address (contains digits and dots), validate strictly
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", val_clean):
                try:
                    ipaddress.IPv4Address(val_clean)
                except ipaddress.AddressValueError:
                    self.report.add_error(
                        f"Invalid IPv4 address '{val_clean}' for 'ansible_host' in {context}.",
                        line_no=line_no,
                    )
            elif ":" in val_clean and not is_templated_or_vault(val_clean):
                # Potential IPv6
                try:
                    ipaddress.IPv6Address(val_clean)
                except ipaddress.AddressValueError:
                    self.report.add_warning(
                        f"Unrecognized IPv6 format '{val_clean}' for 'ansible_host' in {context}.",
                        line_no=line_no,
                    )

        # 2. Parameter: ansible_port
        elif key == "ansible_port":
            val_clean = value.strip("'\"")
            if not is_templated_or_vault(val_clean):
                try:
                    port = int(val_clean)
                    if not (1 <= port <= 65535):
                        self.report.add_error(
                            f"Invalid port number '{port}' for 'ansible_port' in {context} (must be 1-65535).",
                            line_no=line_no,
                        )
                except ValueError:
                    self.report.add_error(
                        f"Non-integer value '{val_clean}' for 'ansible_port' in {context}.",
                        line_no=line_no,
                    )

        # 3. Parameter: ansible_connection
        elif key == "ansible_connection":
            val_clean = value.strip("'\"").lower()
            if not is_templated_or_vault(val_clean) and val_clean not in STANDARD_CONNECTIONS:
                self.report.add_warning(
                    f"Non-standard connection plugin '{val_clean}' in {context}.",
                    line_no=line_no,
                )

        # 4. Security Check: Hardcoded Passwords
        if is_sensitive_variable(key):
            if not is_templated_or_vault(value):
                # Emit warning without printing or exposing the secret
                self.report.add_warning(
                    f"Potential hardcoded password/credential detected in variable '{key}' in {context}. "
                    f"Use Ansible Vault or environment variables instead. Value: {mask_credential(value)}",
                    line_no=line_no,
                )

        # 5. Security Check: Hardcoded Private Key Paths
        if is_private_key_variable(key):
            if not is_templated_or_vault(value):
                sanitized_val = sanitize_path(value)
                self.report.add_warning(
                    f"Hardcoded private-key path detected in variable '{key}' in {context} "
                    f"({sanitized_val}). Consider using SSH agents or external credentials.",
                    line_no=line_no,
                )

    def _validate_cross_references(self) -> None:
        """Validate relationships between groups, children, and detect cycles."""
        # Check that children refer to defined groups
        all_known_groups = set(self.report.groups.keys()).union(set(self.report.children.keys()))

        for parent, child_list in self.report.children.items():
            for child in child_list:
                if child not in all_known_groups and child != "ungrouped":
                    self.report.add_warning(
                        f"Child group '[{child}]' referenced by '[{parent}:children]' "
                        "is not explicitly defined as a host group in this inventory."
                    )

        # Check for cycles in group hierarchy
        for root in self.report.children:
            visited: Set[str] = set()
            path: List[str] = [root]

            def detect_cycle(current: str) -> bool:
                visited.add(current)
                for neighbor in self.report.children.get(current, []):
                    if neighbor in path:
                        cycle_path = " -> ".join(path + [neighbor])
                        self.report.add_error(
                            f"Circular group dependency detected in inventory hierarchy: {cycle_path}"
                        )
                        return True
                    if neighbor not in visited:
                        path.append(neighbor)
                        if detect_cycle(neighbor):
                            return True
                        path.pop()
                return False

            detect_cycle(root)

    def _validate_minimum_requirements(self) -> None:
        """Ensure the inventory defines at least one host group and at least one host."""
        host_groups = [g for g in self.report.groups.keys() if g != "ungrouped"]

        if not host_groups and "ungrouped" not in self.report.groups:
            self.report.add_error(
                "No host groups defined in inventory. At least one group must be present (e.g., [control_plane])."
            )

        if not self.report.all_hosts:
            self.report.add_error(
                "No host entries found in inventory. At least one host must be defined."
            )

        # Warn about empty host groups (groups defined with no hosts and no children)
        for group, hosts in self.report.groups.items():
            if not hosts and group not in self.report.children:
                self.report.add_warning(
                    f"Host group '[{group}]' is defined but contains no hosts or child groups."
                )


# ==============================================================================
# Terminal Presentation
# ==============================================================================

class Colors:
    """ANSI color formatting helpers."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"

    @classmethod
    def disable(cls) -> None:
        cls.RESET = ""
        cls.BOLD = ""
        cls.RED = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.BLUE = ""
        cls.CYAN = ""


def format_report(report: InventoryReport, verbose: bool = False, quiet: bool = False) -> str:
    """Format inventory validation results for command-line output."""
    lines: List[str] = []
    sep = "=" * 72
    subsep = "-" * 72

    lines.append(sep)
    lines.append(f"{Colors.BOLD}AutoPod Ansible Inventory Validation Report{Colors.RESET}")
    lines.append(f"Target:  {Colors.CYAN}{report.file_path}{Colors.RESET}")
    lines.append(f"Format:  {report.format_detected}")
    lines.append(sep)

    # Overview Metrics
    total_groups = len(report.groups)
    total_hosts = len(report.all_hosts)
    lines.append(
        f"Inventory Summary: {Colors.BOLD}{total_groups}{Colors.RESET} group(s), "
        f"{Colors.BOLD}{total_hosts}{Colors.RESET} host(s) discovered."
    )

    if not quiet:
        for group, hosts in sorted(report.groups.items()):
            child_info = ""
            if group in report.children and report.children[group]:
                child_info = f" (children: {', '.join(report.children[group])})"
            lines.append(
                f"  • [{Colors.BOLD}{group}{Colors.RESET}] - {len(hosts)} host(s){child_info}"
            )
            if verbose:
                for h in hosts:
                    h_vars = report.host_vars.get(h, {})
                    ip_str = f" (ansible_host={h_vars.get('ansible_host')})" if "ansible_host" in h_vars else ""
                    lines.append(f"      - {h}{ip_str}")

    # Display Warnings
    if report.warnings:
        lines.append("")
        lines.append(f"{Colors.YELLOW}{Colors.BOLD}Warnings ({len(report.warnings)}):{Colors.RESET}")
        lines.append(subsep)
        for line_no, msg in report.warnings:
            prefix = f"  [WARNING line {line_no:3d}]" if line_no else "  [WARNING]         "
            lines.append(f"{Colors.YELLOW}{prefix}{Colors.RESET} {msg}")

    # Display Errors
    if report.errors:
        lines.append("")
        lines.append(f"{Colors.RED}{Colors.BOLD}Errors ({len(report.errors)}):{Colors.RESET}")
        lines.append(subsep)
        for line_no, msg in report.errors:
            prefix = f"  [ERROR   line {line_no:3d}]" if line_no else "  [ERROR]           "
            lines.append(f"{Colors.RED}{prefix}{Colors.RESET} {msg}")

    # Final Conclusion Banner
    lines.append("")
    lines.append(sep)
    if report.has_errors:
        status_text = f"{Colors.RED}{Colors.BOLD}FAILED{Colors.RESET}"
        lines.append(
            f"Validation Status: {status_text} - "
            f"{len(report.errors)} error(s), {len(report.warnings)} warning(s) found."
        )
    else:
        status_text = f"{Colors.GREEN}{Colors.BOLD}PASSED{Colors.RESET}"
        warn_note = f" (with {len(report.warnings)} warning(s))" if report.warnings else ""
        lines.append(
            f"Validation Status: {status_text} - "
            f"Inventory structure is valid and ready for automation{warn_note}."
        )
    lines.append(sep)

    return "\n".join(lines)


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Validate Ansible inventory files for structure, syntax, and security risks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python validate_inventory.py inventory.ini
    python validate_inventory.py -i inventory.ini --strict
    python validate_inventory.py infra/ansible/inventory.ini --verbose
        """,
    )

    parser.add_argument(
        "inventory_path",
        nargs="?",
        default=None,
        help="Path to the Ansible inventory file (INI format).",
    )
    parser.add_argument(
        "-i",
        "--inventory",
        dest="inventory_flag",
        default=None,
        help="Alternative flag to specify inventory file path.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Strict mode: treat warnings as errors (exits with non-zero code).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Display detailed breakdown of hosts, IPs, and variables.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Quiet mode: suppress summary details, output only warnings and errors.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color codes in output.",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Main execution function."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Determine color output
    if args.no_color or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        Colors.disable()

    # Determine target inventory file
    target = args.inventory_flag or args.inventory_path
    if not target:
        # Default search: inventory.ini in current directory or script's directory
        default_candidates = [
            Path("inventory.ini"),
            Path("infra/ansible/inventory.ini"),
            Path(__file__).parent / "inventory.ini",
        ]
        for candidate in default_candidates:
            if candidate.exists() and candidate.is_file():
                target = str(candidate)
                break

    if not target:
        sys.stderr.write(
            f"{Colors.RED}Error:{Colors.RESET} No inventory file specified and no default 'inventory.ini' found.\n"
            "Usage: python validate_inventory.py <path/to/inventory.ini>\n"
        )
        return 2

    target_path = Path(target).resolve()
    validator = AnsibleInventoryValidator(
        target_path,
        strict=args.strict,
        verbose=args.verbose,
    )

    report = validator.validate()
    output = format_report(report, verbose=args.verbose, quiet=args.quiet)
    print(output)

    # Return codes:
    # 0 = clean pass
    # 1 = validation errors (or warnings in strict mode)
    if report.has_errors:
        return 1
    if args.strict and report.has_warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
