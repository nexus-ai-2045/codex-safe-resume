"""CI接続の合成fixture。製品テスト全件は起動しない。"""
from contextlib import redirect_stderr
import fnmatch
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("safe_ci_runner", ROOT / "scripts" / "run_ci_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def plan(paths=(), checks=()):
    return {"schema": "test-selection/v1", "status": "selected" if paths else "no_tests",
            "selected_tests": list(paths), "checks": list(checks), "unresolved": []}


class RunnerFixtures(unittest.TestCase):
    def test_empty_never_starts_unittest(self):
        with patch.object(runner.unittest, "TestLoader", side_effect=AssertionError("must not load")):
            self.assertEqual(runner.run_tests(ROOT, []), 0)

    def test_only_explicit_filename_is_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_safe_ci_selected_fixture.py").write_text(
                "import unittest\nclass Selected(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
            (root / "tests/test_safe_ci_forbidden_fixture.py").write_text("raise RuntimeError('unrelated test imported')\n")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(runner.run_tests(root, ["tests/test_safe_ci_selected_fixture.py"]), 0)

    def test_zero_cases_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_safe_ci_empty_fixture.py").write_text("VALUE = 1\n")
            with self.assertRaisesRegex(ValueError, "zero test cases"):
                runner.run_tests(root, ["tests/test_safe_ci_empty_fixture.py"])

    def test_import_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_safe_ci_broken_fixture.py").write_text("import missing_safe_ci_fixture_dependency\n")
            with self.assertRaisesRegex(ValueError, "failed to load"):
                runner.run_tests(root, ["tests/test_safe_ci_broken_fixture.py"])

    def test_unsupported_filename_is_rejected(self):
        for name in ("tests/test_*.py", "tests/../test_escape.py", "tests/test-hyphen.py", "/tmp/test_outside.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runner.run_tests(ROOT, [name])

    def test_missing_selected_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            runner.run_tests(ROOT, ["tests/test_safe_ci_missing_fixture.py"])

    def test_unresolved_and_unknown_check_stop(self):
        bad = plan(); bad["unresolved"] = [{"file": "unknown.fixture"}]
        with self.assertRaises(ValueError):
            runner.validate_plan(bad)
        for checks in (["arbitrary-command"], [{}]):
            with self.assertRaises(ValueError):
                runner.validate_plan(plan(checks=checks))

    def test_wrong_selector_source_does_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            selector = Path(directory) / "local_check.py"
            selector.write_text("raise RuntimeError('must not execute')\n")
            with patch.object(runner.subprocess, "run", side_effect=AssertionError("must not execute")):
                with self.assertRaisesRegex(ValueError, "fixed source"):
                    runner.select(ROOT, selector, "origin/main")


class PolicyWorkflowFixtures(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((ROOT / ".test-selection.json").read_text(encoding="utf-8"))

    def test_product_inventory_and_ci_component_have_different_owners(self):
        declared = set(self.policy["source_roots"])
        self.assertEqual(declared, {p.name for p in ROOT.glob("*.py")})
        self.assertTrue(all((ROOT / name).is_file() for name in declared))
        self.assertNotIn("scripts/run_ci_tests.py", declared)
        self.assertTrue(any({"scripts/run_ci_tests.py", "tests/test_ci_selection.py"} <= set(rule["changes"])
                            and rule.get("dependency_mode") == "mapped"
                            and rule["tests"] == ["tests/test_ci_selection.py"]
                            for rule in self.policy["rules"]))

    def test_product_packaging_uses_the_same_declared_source_roots(self):
        product = plan(); product["changed_files"] = ["usage_client.py"]
        self.assertEqual(runner.owner_checks(product, self.policy), runner.CHECKS)
        component = plan(["tests/test_ci_selection.py"])
        component["changed_files"] = ["scripts/run_ci_tests.py"]
        self.assertEqual(runner.owner_checks(component, self.policy), set())
        reduced = dict(self.policy, source_roots=[])
        self.assertEqual(runner.owner_checks(product, reduced), set())

    def test_startup_contract_keeps_both_stdio_integration_tests(self):
        for changed in ("app.py", "usage_client.py", "queue_transport.py", "desktop_launcher.py"):
            mapped = {test for rule in self.policy["rules"]
                      if any(fnmatch.fnmatchcase(changed, pattern) for pattern in rule["changes"])
                      for test in rule.get("tests", [])}
            self.assertTrue({"tests/test_desktop_startup_stdio.py", "tests/test_desktop_stdio.py"} <= mapped)
            self.assertNotIn("tests/test_windows_shortcuts.py", mapped)

    def test_selected_stdio_contract_keeps_actual_native_pipe_coverage(self):
        selected = plan(["tests/test_desktop_startup_stdio.py", "tests/test_desktop_stdio.py"])
        for environment in ("windows", "macos"):
            with self.subTest(environment=environment):
                self.assertEqual(runner.environment_tests(selected, self.policy, environment),
                                 selected["selected_tests"])
        cells = runner.matrix_for(selected, self.policy)["include"]
        self.assertEqual([(c["os"], c["python-version"]) for c in cells],
                         [("ubuntu-latest", "3.11"), ("ubuntu-latest", "3.13"),
                          ("macos-latest", "3.13"), ("windows-latest", "3.13")])
        self.assertTrue(all(c["runtime_required"] for c in cells))

    def test_coordinator_keeps_native_runner_coverage_without_app_dependencies(self):
        selected = plan(["tests/test_ci_selection.py"])
        cells = runner.matrix_for(selected, self.policy)["include"]
        self.assertEqual([(c["os"], c["python-version"]) for c in cells],
                         [("ubuntu-latest", "3.11"), ("ubuntu-latest", "3.13"),
                          ("macos-latest", "3.13"), ("windows-latest", "3.13")])
        self.assertFalse(any(c["runtime_required"] or c["package_required"] for c in cells))

    def test_native_scope_is_an_intersection_not_a_whole_suite(self):
        selected = plan(["tests/test_usage_client.py", "tests/test_windows_platform.py"])
        self.assertEqual(runner.environment_tests(selected, self.policy, "windows"), ["tests/test_windows_platform.py"])
        self.assertEqual(runner.environment_tests(selected, self.policy, "macos"), [])

    def test_package_only_has_one_correct_native_version(self):
        cells = runner.matrix_for(plan(checks=["package-windows"]), self.policy)["include"]
        self.assertEqual(cells, [{"os": "windows-latest", "python-version": "3.13",
                                 "environment": "windows", "runtime_required": False, "package_required": True}])

    def test_no_tests_has_no_matrix(self):
        self.assertEqual(runner.matrix_for(plan(), self.policy), {"include": []})

    def test_workflow_plans_before_dependencies_and_keeps_hash_locks(self):
        text = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        self.assertLess(text.index("id: plan"), text.index("pip install"))
        self.assertIn("if: matrix.runtime_required", text)
        self.assertIn("if: matrix.package_required", text)
        self.assertIn("--require-hashes -r packaging/requirements-runtime.lock", text)
        self.assertIn("--require-hashes -r packaging/requirements-build.lock", text)
        self.assertIn("fromJSON(needs.selection.outputs.matrix)", text)
        self.assertNotIn("unittest discover -s tests", text)

    def test_all_policy_targets_and_native_paths_exist(self):
        for rule in self.policy["rules"]:
            for name in rule.get("tests", []):
                self.assertTrue((ROOT / name).is_file(), name)
        for values in self.policy["native_tests"].values():
            for name in values:
                self.assertTrue((ROOT / name).is_file(), name)


class SemanticBoundaryFixtures(unittest.TestCase):
    def selector(self):
        path = Path(os.environ["SAFE_SELECTOR_SCRIPT"])
        spec = importlib.util.spec_from_file_location("safe_fixed_selector", path)
        selector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selector)
        return selector

    @unittest.skipUnless(os.environ.get("SAFE_SELECTOR_SCRIPT"), "fixed semantic selector required")
    def test_usage_change_does_not_select_platform_parser_or_installer(self):
        selector = self.selector()
        policy = json.loads((ROOT / ".test-selection.json").read_text(encoding="utf-8"))
        selected = selector.select_tests(ROOT, ["usage_client.py"], policy)
        self.assertFalse(selected["unresolved"], selected["unresolved"])
        self.assertIn("tests/test_usage_client.py", selected["selected_tests"])
        self.assertEqual(runner.owner_checks(selected, policy), runner.CHECKS)
        for unrelated in ("tests/test_windows_shortcuts.py", "tests/test_windows_runtime_cleanup.py",
                          "tests/test_windows_platform.py", "tests/test_portable_paths.py"):
            self.assertNotIn(unrelated, selected["selected_tests"])
        component = selector.select_tests(ROOT, ["scripts/run_ci_tests.py"], policy)
        self.assertFalse(component["unresolved"])
        self.assertEqual(component["selected_tests"], ["tests/test_ci_selection.py"])
        self.assertEqual(runner.owner_checks(component, policy), set())
        self.assertFalse(any(c["package_required"] or c["runtime_required"]
                             for c in runner.matrix_for(component, policy)["include"]))
        workflow = selector.select_tests(ROOT, [".github/workflows/test.yml"], policy)
        self.assertFalse(workflow["unresolved"])
        self.assertEqual(workflow["selected_tests"], ["tests/test_ci_selection.py"])
        self.assertEqual(set(workflow["checks"]), runner.CHECKS)
        self.assertEqual({c["os"] for c in runner.matrix_for(workflow, policy)["include"]
                          if c["package_required"]}, {"macos-latest", "windows-latest"})

    @unittest.skipUnless(os.environ.get("SAFE_SELECTOR_SCRIPT"), "fixed semantic selector required")
    def test_new_product_requires_an_owner_declaration_even_with_a_test_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "declared_product_fixture.py").write_text("VALUE = 1\n")
            (root / "new_product_fixture.py").write_text("VALUE = 2\n")
            (root / "tests/test_future_product_fixture.py").write_text("import new_product_fixture\n")
            policy = {"source_roots": ["declared_product_fixture.py"], "test_roots": ["tests"],
                      "test_patterns": ["test_*.py"], "code_suffixes": [".py"], "rules": []}
            selected = self.selector().select_tests(root, ["new_product_fixture.py"], policy)
            self.assertTrue(selected["unresolved"])


if __name__ == "__main__":
    unittest.main()
