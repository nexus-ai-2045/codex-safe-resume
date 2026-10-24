from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_package as package
import windows_platform


class PackageVerificationTests(unittest.TestCase):
    def setUp(self):
        fixture_root = Path(__file__).resolve().parents[2] / 'owned-fixture-temp'
        fixture_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixture_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.desktop = self.root / 'app' / 'ChatGPT.exe'
        self.desktop.parent.mkdir()
        self.desktop.write_bytes(b'inert fixture')
        self.identity = dict(pid=3, ppid=1, start=[1, 2], executable=str(self.desktop))
        self.native = dict(root=str(self.root), full_name='installed-package', family=package.FAMILY)
        self.record = dict(self.native, **dict.fromkeys(package._REQUIRED, True))

    @contextmanager
    def verified_fixture(self):
        @contextmanager
        def process(pid):
            yield 7, self.identity

        with patch.object(windows_platform, '_process', side_effect=process), \
                patch.object(windows_platform, 'process_identity', return_value=self.identity), \
                patch.object(package, '_native_package', return_value=self.native), \
                patch.object(package, '_package_record', return_value=self.record), \
                patch.object(package.sys, 'platform', 'win32'):
            yield

    def test_verified_package_returns_only_native_identity(self):
        with self.verified_fixture():
            self.assertEqual(package.verify_package(3, self.desktop), self.native)

    def test_every_protection_must_be_explicitly_true(self):
        for key in package._REQUIRED:
            for invalid in (False, None, 1, 'true'):
                with self.subTest(protection=key, invalid=invalid):
                    self.record[key] = invalid
                    with self.verified_fixture(), self.assertRaises(ValueError):
                        package.verify_package(3, self.desktop)
            self.record[key] = True

    def test_missing_protection_fails_closed(self):
        for key in package._REQUIRED:
            self.record.pop(key)
            with self.subTest(protection=key), self.verified_fixture(), self.assertRaises(ValueError):
                package.verify_package(3, self.desktop)
            self.record[key] = True

    def test_unrelated_registered_package_is_rejected(self):
        self.native['family'] = 'untrusted-family'
        self.record['family'] = 'untrusted-family'
        with self.verified_fixture(), self.assertRaises(ValueError):
            package.verify_package(3, self.desktop)

    def test_os_and_winrt_package_records_must_match(self):
        for key in ('root', 'full_name', 'family'):
            original = self.record[key]
            self.record[key] = str(self.root / 'other') if key == 'root' else 'different'
            with self.subTest(field=key), self.verified_fixture(), self.assertRaises(ValueError):
                package.verify_package(3, self.desktop)
            self.record[key] = original

    def test_executable_outside_registered_root_is_rejected(self):
        self.native['root'] = str(self.root / 'other')
        self.record['root'] = self.native['root']
        with self.verified_fixture(), self.assertRaises(ValueError):
            package.verify_package(3, self.desktop)

    def test_process_creation_identity_must_survive_verification(self):
        with self.verified_fixture(), \
                patch.object(windows_platform, 'process_identity',
                             return_value=dict(self.identity, start=[9, 9])), self.assertRaises(ValueError):
            package.verify_package(3, self.desktop)

    def test_subprocess_failure_is_sanitized(self):
        with self.verified_fixture(), \
                patch.object(package, '_package_record', side_effect=subprocess.TimeoutExpired('private', 1)):
            with self.assertRaises(ValueError) as error:
                package.verify_package(3, self.desktop)
            self.assertNotIn('private', str(error.exception))

    def test_relative_path_and_unknown_os_are_rejected(self):
        with self.assertRaises(ValueError):
            package.verify_package(3, 'ChatGPT.exe')
        with patch.object(package.sys, 'platform', 'unknown'), self.assertRaises(ValueError):
            package.verify_package(3, self.desktop)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native package identity')
    def test_unpacked_current_test_process_has_no_package(self):
        with windows_platform._process(os.getpid()) as (handle, _):
            with self.assertRaises(ValueError):
                package._native_package(handle)


if __name__ == '__main__':
    unittest.main()
