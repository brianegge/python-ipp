"""Check the installed dependency tree against Home Assistant's hassfest rules.

Home Assistant's hassfest validates every integration requirement and its
transitive dependencies: forbidden packages, forbidden files and directories,
and overly strict version ranges. This script applies the same rules to an
installed distribution so problems are caught here instead of in a Home
Assistant pull request. The rule tables are fetched from Home Assistant core
at runtime so they stay current.
"""

from __future__ import annotations

import ast
import sys
import urllib.request
from importlib import metadata
from typing import Any

from awesomeversion import AwesomeVersion
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

HASSFEST_REQUIREMENTS_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/dev/"
    "script/hassfest/requirements.py"
)
RULE_NAMES = (
    "FORBIDDEN_PACKAGES",
    "FORBIDDEN_FILE_NAMES",
    "FORBIDDEN_PACKAGE_NAMES",
    "PACKAGE_CHECK_VERSION_RANGE",
    "PACKAGE_CHECK_PREPARE_UPDATE",
)


def load_rules() -> dict[str, Any]:
    """Fetch hassfest's requirements module and extract its rule tables."""
    with urllib.request.urlopen(HASSFEST_REQUIREMENTS_URL, timeout=30) as response:
        source = response.read().decode()
    rules: dict[str, Any] = {}
    for node in ast.parse(source).body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id in RULE_NAMES and node.value:
            rules[target.id] = ast.literal_eval(node.value)
    missing = set(RULE_NAMES) - rules.keys()
    if missing:
        msg = f"Could not find {sorted(missing)} in hassfest requirements.py"
        raise RuntimeError(msg)
    return rules


def dependency_tree(root: str) -> dict[str, dict[str, str]]:
    """Return {package: {dependency: version_spec}} for root and its dependencies."""
    tree: dict[str, dict[str, str]] = {}
    to_check = [canonicalize_name(root)]
    while to_check:
        package = to_check.pop()
        if package in tree:
            continue
        dependencies: dict[str, str] = {}
        for line in metadata.requires(package) or ():
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            dependencies[canonicalize_name(requirement.name)] = str(
                requirement.specifier,
            )
        tree[package] = dependencies
        to_check.extend(dependencies)
    return tree


def version_part_valid(  # noqa: PLR0911 - mirrors hassfest branch for branch
    version_part: str,
    convention: str,
    prepare_update: int | None,
) -> bool:
    """Mirror hassfest's _is_dependency_version_range_valid."""
    stripped = version_part.strip()
    operator = ""
    for candidate in ("===", "==", ">=", "<=", "~=", "!=", "<", ">"):
        if stripped.startswith(candidate):
            operator = candidate
            break
    version = stripped[len(operator) :]
    awesome = AwesomeVersion(version)

    if operator in (">", ">=", "!="):
        return True
    if prepare_update is not None:
        if operator in ("==", "~="):
            return False
        if operator == "<" and awesome.section(0) < prepare_update + 1:
            return False
        if operator == "<=" and awesome.section(0) < prepare_update:
            return False
    if convention == "SemVer":
        if operator == "==":
            return version.endswith(".*") and version.count(".") == 1
        if operator in ("<", "<="):
            return awesome.section(1) == 0 and awesome.section(2) == 0
        if operator == "~=":
            return awesome.section(2) == 0
    return False


def check_files(package: str, rules: dict[str, Any]) -> list[str]:
    """Mirror hassfest's check_dependency_files."""
    errors: list[str] = []
    top_level: set[str] = set()
    for file in metadata.files(package) or ():
        top = file.parts[0].lower()
        if not top.endswith((".dist-info", ".py")):
            top_level.add(top)
        name = str(file).lower()
        if name in rules["FORBIDDEN_FILE_NAMES"] or (
            name.endswith((".pth", ".start")) and len(file.parts) == 1
        ):
            errors.append(f"Package {package} has a forbidden file '{file}'")
    errors.extend(
        f"Package {package} has a forbidden top level directory '{directory}'"
        for directory in sorted(rules["FORBIDDEN_PACKAGE_NAMES"] & top_level)
    )
    return errors


def check(root: str) -> list[str]:
    """Return every hassfest violation in root's dependency tree."""
    rules = load_rules()
    errors: list[str] = []
    for package, dependencies in dependency_tree(root).items():
        requires_python = metadata.metadata(package).get("Requires-Python")
        if requires_python and not all(
            version_part_valid(part, "SemVer", None)
            for part in requires_python.split(",")
        ):
            errors.append(
                f"Version restrictions for Python are too strict "
                f"({requires_python}) in {package}",
            )
        errors.extend(check_files(package, rules))
        for dependency, specifier in dependencies.items():
            if (
                dependency.startswith("types-")
                or dependency in rules["FORBIDDEN_PACKAGES"]
            ):
                reason = rules["FORBIDDEN_PACKAGES"].get(
                    dependency,
                    "not be a runtime dependency",
                )
                errors.append(f"Package {dependency} should {reason} in {package}")
            convention = rules["PACKAGE_CHECK_VERSION_RANGE"].get(dependency)
            if convention and specifier:
                prepare_update = rules["PACKAGE_CHECK_PREPARE_UPDATE"].get(dependency)
                if not all(
                    version_part_valid(part, convention, prepare_update)
                    for part in specifier.split(",")
                ):
                    errors.append(
                        f"Version restrictions for {dependency} are too strict "
                        f"({specifier}) in {package}",
                    )
    return errors


def main(argv: list[str]) -> int:
    """Run the check for the distribution named on the command line."""
    root = argv[1] if len(argv) > 1 else "aioipp"
    errors = check(root)
    for error in errors:
        sys.stdout.write(f"ERROR: {error}\n")
    if errors:
        return 1
    sys.stdout.write(f"{root}: dependency tree passes Home Assistant hassfest rules\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
