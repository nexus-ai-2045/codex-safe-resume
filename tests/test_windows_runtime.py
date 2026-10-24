import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import windows_runtime


class RuntimeStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root/'package'
        self.resources = self.package/'app'/'resources'
        self.resources.mkdir(parents=True)
        self.cache = self.root/'cache'
        self.cache.mkdir()
        self.home = self.root/'state'
        for name in windows_runtime.REQUIRED_FILES:
            payload = ('verified '+name).encode()
            (self.resources/name).write_bytes(payload)
            (self.cache/name).write_bytes(payload)

    def stage(self):
        with patch('windows_runtime.validate_location', side_effect=Path):
            return windows_runtime.stage_cli(self.package, self.cache/'codex.exe', self.home)

    def test_only_identical_known_package_files_are_staged(self):
        (self.cache/'unknown.exe').write_bytes(b'never adopted')
        staged = self.stage()
        self.assertEqual(staged.name, 'codex.exe')
        self.assertEqual(set(p.name for p in staged.parent.iterdir()),
                         set(windows_runtime.REQUIRED_FILES) | {windows_runtime.MARKER})
        self.assertEqual(staged.read_bytes(), (self.resources/'codex.exe').read_bytes())
        self.assertEqual(self.stage(), staged)

    def test_mismatched_cached_cli_or_helper_never_creates_runtime(self):
        for name in ('codex.exe', 'codex-code-mode-host.exe'):
            source = self.cache/name
            original = source.read_bytes()
            source.write_bytes(b'unverified replacement')
            with self.assertRaises(ValueError):
                self.stage()
            self.assertFalse((self.home/'runtime').exists())
            source.write_bytes(original)

    def test_existing_unknown_or_modified_payload_is_not_overwritten(self):
        staged = self.stage()
        staged.write_bytes(b'keep changed file')
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(staged.read_bytes(), b'keep changed file')

    def test_staged_executable_must_pass_unchanged_location_validator(self):
        with patch('windows_runtime.validate_location', side_effect=ValueError('private location')):
            with self.assertRaises(ValueError) as error:
                windows_runtime.stage_cli(self.package, self.cache/'codex.exe', self.home)
        self.assertNotIn('private', str(error.exception))

    def test_changed_owner_marker_is_not_adopted_or_overwritten(self):
        staged = self.stage()
        marker = staged.parent/windows_runtime.MARKER
        changed = json.loads(marker.read_text(encoding='utf-8'))
        changed['schema'] = 'unrecognized'
        marker.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8')), changed)
