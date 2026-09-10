"""Cross-platform regressions for the Windows package; no accounts or network."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from monitor.codex_usage import executable, CodexUnavailable
from monitor.mail_service import set_enabled, read_session

class WindowsCompatibilityTests(unittest.TestCase):
    def test_npm_shim_resolves_native_exe_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim = root/'codex.cmd'; shim.write_text('do not run this wrapper')
            native = root/'node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/codex/codex.exe'
            native.parent.mkdir(parents=True); native.touch(); native.chmod(0o700)
            with patch('monitor.codex_usage.sys.platform', 'win32'), patch('monitor.codex_usage.shutil.which', return_value=str(shim)):
                self.assertEqual(executable({}), str(native))
                native.unlink()
                with patch.dict(os.environ, {'LOCALAPPDATA':tmp}):
                    with self.assertRaises(CodexUnavailable): executable({})

    def test_weekly_preference_closes_file_before_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = {'serviceUrl':'https://mail.example.com','token':'a'*64,'subscriptionStatus':'active','weeklyEnabled':False}
            (root/'mail-session.json').write_text(json.dumps(session))
            original = Path.replace
            def replace(path, target):
                # Windows itself enforces this in CI; portable regression checks complete data.
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['weeklyEnabled'], True)
                return original(path, target)
            with patch.object(Path, 'replace', replace): self.assertTrue(set_enabled(root, True))
            self.assertTrue(read_session(root)['weeklyEnabled'])
            self.assertFalse(list(root.glob('.mail-*')))
