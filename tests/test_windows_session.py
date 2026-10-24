from pathlib import Path
import unittest
from unittest.mock import patch

import desktop_binding


class WindowsSessionTests(unittest.TestCase):
    def test_windows_binding_delegates_every_native_identity(self):
        with patch.object(desktop_binding.sys, 'platform', 'win32'), \
                patch('windows_platform.process_identity', return_value={'pid': 7}) as process, \
                patch('windows_platform.desktop_bundle', return_value='desktop') as bundle, \
                patch('windows_platform.endpoint_identity', return_value={'inode': 3}) as endpoint:
            self.assertEqual(desktop_binding.process_identity(7), {'pid': 7})
            self.assertEqual(desktop_binding.desktop_bundle(7), 'desktop')
            self.assertEqual(desktop_binding.endpoint_identity(Path('/socket')), {'inode': 3})
        process.assert_called_once_with(7)
        bundle.assert_called_once_with(7)
        endpoint.assert_called_once_with(Path('/socket'))
