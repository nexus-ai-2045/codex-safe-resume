"""repo-preflight 導入契約を退行防止するテスト。"""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class PreflightContract(unittest.TestCase):
    def test_documents(self):
        for name in ('README.md','SECURITY.md','CONTRIBUTING.md','PREFLIGHT.md','docs/開発と検証.md'):
            data=(ROOT/name).read_text(encoding='utf-8')
            self.assertGreater(len(data),100,name)
        self.assertTrue((ROOT/'PREFLIGHT.md').read_text(encoding='utf-8').startswith('<!-- repo-preflight:review-record -->'))
    def test_pinned_preflight_gate(self):
        data=(ROOT/'.github/workflows/preflight.yml').read_text(encoding='utf-8')
        self.assertIn('nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml@',data)
        self.assertIn('preflight-ref:',data)
        self.assertNotIn('@main',data)
    def test_no_baseline_relaxation(self):
        data=(ROOT/'.repo-preflight/ratchet-baseline.json').read_text(encoding='utf-8')
        self.assertGreater(len(data),5)
if __name__=='__main__':unittest.main()