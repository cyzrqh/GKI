"""Every kernel worker can use the authenticated official KPM API fallback."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from combined_manager_workflow import add_combined
from generate_workflow import create_workflow
from oplus_workflow import create_workflow as create_oplus_workflow


class KpmTransportWorkflowTests(unittest.TestCase):
    def test_both_platforms_and_overflow_workers_pass_api_token_to_kpm_preparation(self):
        gki = create_workflow(ROOT)
        workflows = ((False, gki), (True, create_oplus_workflow(ROOT, gki)))
        for oplus, base in workflows:
            workflow, _ = add_combined(base, oplus=oplus)
            kernel = 'build-oplus-kernel' if oplus else 'build-kernel'
            for name in (kernel, kernel + '-overflow'):
                with self.subTest(platform='oplus' if oplus else 'gki', job=name):
                    job = workflow['jobs'][name]
                    preparations = [step for step in job['steps']
                                    if 'apply_kpm_patch.py' in step.get('run', '')
                                    and ' prepare ' in step['run']]
                    self.assertEqual(1, len(preparations))
                    step = preparations[0]
                    self.assertEqual('${{ github.token }}', step['env']['GH_TOKEN'])
                    self.assertIn('inputs.use_kpm', step['if'])
                    self.assertIn('--ksu-source', step['run'])
                    self.assertNotIn('GH_TOKEN', job.get('env', {}))
                    patches = [step for step in job['steps']
                               if 'apply_kpm_patch.py' in step.get('run', '')
                               and ' patch ' in step['run']]
                    self.assertEqual(1, len(patches))
                    self.assertNotIn('GH_TOKEN', patches[0].get('env', {}))


if __name__ == '__main__':
    unittest.main()
