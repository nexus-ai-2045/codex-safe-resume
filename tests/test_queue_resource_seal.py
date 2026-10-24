import hashlib
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import types
import unittest
from unittest import mock

import queue_resource_seal as seal


class ResourceSealTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.contents = self.root / 'Codex.app' / 'Contents'
        self.target = self.contents / 'Resources' / 'codex-cli' / 'bin' / 'codex'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b'official unsigned resource executable')
        self.relative = self.target.relative_to(self.contents).as_posix()
        self.path = self.contents / '_CodeSignature' / 'CodeResources'
        self.path.parent.mkdir()
        self.digest = hashlib.sha256(self.target.read_bytes()).digest()
        self.write_entry({'hash2': self.digest})

    def write_entry(self, entry, *, relative=None):
        value = {'files2': {self.relative if relative is None else relative: entry}}
        self.path.write_bytes(plistlib.dumps(value))

    def test_direct_sha256_resource_proves_exact_unsigned_file_without_execution(self):
        source = seal.ResourceSeal(self.contents)
        self.assertTrue(source.resource(self.target))
        self.assertTrue(source.unchanged())

    def test_nested_signed_code_requires_separate_code_verification(self):
        self.write_entry({'cdhash': b'x' * 20, 'requirement': 'anchor apple generic'})
        self.assertFalse(seal.ResourceSeal(self.contents).resource(self.target))

    def test_legacy_sha1_alone_and_unknown_entry_shapes_are_rejected(self):
        entries = ({'hash': b'x' * 20}, b'x' * 20, [], {},
                   {'hash2': self.digest, 'optional': True},
                   {'hash2': self.digest, 'optional': False},
                   {'hash2': self.digest, 'symlink': 'elsewhere'},
                   {'hash2': self.digest, 'cdhash': b'x' * 20},
                   {'hash2': self.digest, 'unknown': True},
                   {'hash2': self.digest, 'hash': b'short'},
                   {'cdhash': b'x' * 20},
                   {'cdhash': b'x' * 20, 'requirement': ''},
                   {'cdhash': b'x' * 20, 'requirement': 'anchor apple', 'optional': False})
        for entry in entries:
            with self.subTest(entry_kind=type(entry).__name__, keys=list(entry) if isinstance(entry, dict) else None):
                self.write_entry(entry)
                with self.assertRaises(ValueError):
                    seal.ResourceSeal(self.contents).resource(self.target)

    def test_sha256_mismatch_wrong_length_or_type_are_rejected(self):
        for digest in (b'x' * 32, b'x' * 20, 'x' * 32, 1):
            with self.subTest(digest_kind=type(digest).__name__):
                self.write_entry({'hash2': digest})
                with self.assertRaises(ValueError):
                    seal.ResourceSeal(self.contents).resource(self.target)

    def test_digest_for_other_path_and_path_alias_are_not_accepted(self):
        for relative in ('Resources/other', '../' + self.relative,
                         'Resources/codex-cli/bin/../bin/codex', str(self.target)):
            with self.subTest(relative_kind=relative.split('/')[0]):
                self.write_entry({'hash2': self.digest}, relative=relative)
                with self.assertRaises(ValueError):
                    seal.ResourceSeal(self.contents).resource(self.target)

    def test_resource_envelope_must_exist_and_contain_bounded_files2_dictionary(self):
        for value in ({}, {'files': {self.relative: self.digest}}, {'files2': []}, {'files2': {}}):
            self.path.write_bytes(plistlib.dumps(value))
            with self.assertRaises(ValueError):
                seal.ResourceSeal(self.contents)
        self.path.unlink()
        with self.assertRaises(OSError):
            seal.ResourceSeal(self.contents)

    def test_target_modified_after_envelope_read_is_rejected(self):
        source = seal.ResourceSeal(self.contents)
        self.target.write_bytes(b'altered executable')
        with self.assertRaises(ValueError):
            source.resource(self.target)

    def test_envelope_modified_during_bundle_verification_is_rejected(self):
        source = seal.ResourceSeal(self.contents)
        self.write_entry({'hash2': b'x' * 32})
        self.assertFalse(source.unchanged())

    def test_identical_envelope_replacement_has_different_file_identity(self):
        source = seal.ResourceSeal(self.contents)
        replacement = self.path.parent / 'replacement'
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.assertFalse(source.unchanged())

    def test_hardlinked_envelope_is_rejected(self):
        os.link(self.path, self.path.parent / 'alias')
        with self.assertRaises(ValueError):
            seal.ResourceSeal(self.contents)

    def test_envelope_size_and_file_count_are_bounded(self):
        with mock.patch.object(seal, '_MAX_ENVELOPE', 8):
            with self.assertRaises(ValueError):
                seal.ResourceSeal(self.contents)
        with mock.patch.object(seal, '_MAX_FILES', 0):
            with self.assertRaises(ValueError):
                seal.ResourceSeal(self.contents)

    def test_linked_components_are_rejected_before_reading(self):
        with mock.patch.object(seal, 'reject_links', side_effect=RuntimeError('linked component')):
            with self.assertRaises(RuntimeError):
                seal.ResourceSeal(self.contents)

    def test_foreign_owned_envelope_is_rejected(self):
        info = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1,
                                     st_uid=12345, st_size=1)
        with mock.patch.object(seal.os, 'getuid', return_value=1000, create=True), mock.patch.object(seal, 'reject_links'), mock.patch.object(Path, 'lstat', return_value=info):
            with self.assertRaises(ValueError):
                seal._regular(self.path, 1024)

    def test_target_cannot_escape_contents_or_use_parent_segments(self):
        source = seal.ResourceSeal(self.contents)
        for target in (self.root / 'codex', self.contents / 'Resources' / '..' / 'codex'):
            with self.assertRaises(ValueError):
                source.resource(target)


if __name__ == '__main__':
    unittest.main()
