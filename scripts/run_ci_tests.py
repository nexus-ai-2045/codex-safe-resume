"""固定版の共通selectorを、既存unittestとOS別配布検査へ接続する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SELECTOR_REF = "92fed10d1dbba659b71436796b0e681222b37a19"
SELECTOR_HASHES = {
    "local_check.py": "445d27d84d6e963d27895b42b55d1847d34a33ff2a7356e8eb5190050accc67b",
    "local_identity_guard.py": "3915411258b286414d62d52444a6ddb803ffe1669aedc44ada546e45be7d516a",
}
CHECKS = {"package-macos", "package-windows"}
ENVIRONMENTS = {"portable": "ubuntu-latest", "macos": "macos-latest", "windows": "windows-latest"}


def validate_plan(plan: dict) -> None:
    if (plan.get("schema") != "test-selection/v1"
            or plan.get("status") not in {"selected", "no_tests"}
            or plan.get("unresolved")):
        raise ValueError("unknown selection; no whole-suite fallback")
    paths = plan.get("selected_tests")
    if not isinstance(paths, list) or any(
        not isinstance(p, str) or not re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", p)
        for p in paths
    ):
        raise ValueError("selected tests must be owned flat unittest paths")
    checks = plan.get("checks")
    if not isinstance(checks, list) or any(not isinstance(c, str) or c not in CHECKS for c in checks):
        raise ValueError("unknown consumer check")


def environment_tests(plan: dict, policy: dict, environment: str) -> list[str]:
    validate_plan(plan)
    if environment == "portable":
        return plan["selected_tests"]
    owned = policy["native_tests"][environment]
    if not isinstance(owned, list) or any(
        not isinstance(p, str) or not re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", p)
        for p in owned
    ):
        raise ValueError("invalid native test scope")
    return [p for p in plan["selected_tests"] if p in owned]


def owner_checks(plan: dict, policy: dict, repo: Path = ROOT) -> set[str]:
    """配布入力の正本はproduct source_roots。別のmodule一覧を持たない。"""
    validate_plan(plan)
    checks = set(plan["checks"])
    for name in plan.get("changed_files", []):
        if any(name == root or ((repo / root).is_dir() and name.startswith(root.rstrip("/") + "/"))
               for root in policy["source_roots"]):
            checks.update(CHECKS)
    return checks


def matrix_for(plan: dict, policy: dict, repo: Path = ROOT) -> dict:
    cells = []
    checks = owner_checks(plan, policy, repo)
    for environment, os_name in ENVIRONMENTS.items():
        paths = environment_tests(plan, policy, environment)
        packaging = "package-" + environment in checks
        # Language compatibility is portable; real OS APIs use the native primary.
        versions = ("3.11", "3.13") if environment == "portable" else ("3.13",)
        for version in versions:
            package_required = packaging and version == "3.13"
            if paths or package_required:
                cells.append({"os": os_name, "python-version": version,
                              "environment": environment,
                              "runtime_required": any(p not in policy["stdlib_only_tests"] for p in paths),
                              "package_required": package_required})
    return {"include": cells}


def select(repo: Path, selector: Path, base: str) -> dict:
    for name, digest in SELECTOR_HASHES.items():
        if hashlib.sha256((selector.parent / name).read_bytes()).hexdigest() != digest:
            raise ValueError("selector must match the adopted fixed source")
    result = subprocess.run(
        [sys.executable, str(selector), "--repo", str(repo), "--base-ref", base,
         "--selection-config", ".test-selection.json", "--plan-tests", "--json"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise ValueError("selector stopped: unresolved change or unavailable base")
    plan = json.loads(result.stdout)
    validate_plan(plan)
    return plan


def run_tests(repo: Path, paths: list[str]) -> int:
    # unittest receives only explicit selected file paths; no discover default.
    if not paths:
        return 0
    if any(not isinstance(p, str) or not re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", p) for p in paths):
        raise ValueError("unsupported unittest filename")
    if any(not (repo / p).is_file() or not (repo / p).resolve().is_relative_to(repo.resolve()) for p in paths):
        raise ValueError("selected test file missing or outside repository")
    previous_path, previous_cwd = sys.path[:], Path.cwd()
    try:
        os.chdir(repo)
        sys.path.insert(0, str(repo))
        suite = unittest.TestSuite()
        for path in paths:
            loader = unittest.TestLoader()
            selected = loader.discover(str(repo / "tests"), pattern=Path(path).name)
            if loader.errors:
                raise ValueError("selected unittest failed to load")
            if selected.countTestCases() == 0:
                raise ValueError("selected unittest contains zero test cases")
            suite.addTests(selected)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    finally:
        sys.path[:] = previous_path
        os.chdir(previous_cwd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "run"))
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--selector-script", type=Path, required=True)
    parser.add_argument("--environment", choices=tuple(ENVIRONMENTS), default="portable")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    receipt = {"executed": False, "environment": args.environment, "selector_ref": SELECTOR_REF}
    code = 2
    try:
        repo = args.repo.resolve()
        policy = json.loads((repo / ".test-selection.json").read_text(encoding="utf-8"))
        plan = select(repo, args.selector_script.resolve(), args.base_ref)
        paths = environment_tests(plan, policy, args.environment)
        matrix = matrix_for(plan, policy, repo)
        receipt.update(plan=plan, selected_tests=paths, matrix=matrix,
                       consumer_checks=sorted(owner_checks(plan, policy, repo)))
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write("matrix=" + json.dumps(matrix, separators=(",", ":")) + "\n")
                output.write("required=" + str(bool(matrix["include"])).lower() + "\n")
        code = 0
        if args.mode == "run":
            native = {"macos": "darwin", "windows": "win32"}.get(args.environment)
            if paths and native and sys.platform != native:
                raise ValueError("native tests require the actual OS")
            code = run_tests(repo, paths)
            receipt.update(executed=bool(paths), exit_code=code)
    except (ValueError, OSError, KeyError, TypeError) as error:
        receipt["error"] = str(error)
        print("停止: " + str(error), file=sys.stderr)
        code = 2
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
