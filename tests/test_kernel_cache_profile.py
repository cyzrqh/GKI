import hashlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import kernel_cache_profile as profile


class KernelCacheProfileTests(unittest.TestCase):
    def test_runtime_namespace_uses_the_exact_github_file_hash(self):
        files = ('config/kernel-cache-tools.json', 'tools/kernel_compile_cache.py')
        digest = hashlib.sha256(b''.join(hashlib.sha256((ROOT / p).read_bytes()).digest()
                                        for p in files)).hexdigest()
        expression = profile.profile_expression()[3:-3].replace(profile.HASH, repr(digest))
        result = eval(expression.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}})
        self.assertEqual(profile.OLD_PROFILE if digest == profile.DIAGNOSTIC_PROFILE else digest, result)

    def test_only_exact_reviewed_diagnostics_retain_the_old_namespace(self):
        expression = profile.profile_expression()[3:-3]
        for actual in (profile.DIAGNOSTIC_PROFILE, 'a' * 64, 'b' * 64):
            value = expression.replace(profile.HASH, repr(actual))
            result = eval(value.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}})
            self.assertEqual(profile.OLD_PROFILE if actual == profile.DIAGNOSTIC_PROFILE else actual, result)

    def test_gki_and_oplus_keep_complete_identity_and_alias_both_cache_directions(self):
        from generate_workflow import create_workflow
        from oplus_workflow import create_workflow as create_oplus
        gki = create_workflow(ROOT)
        for document, job_name in ((gki, 'build-kernel'),
                                   (create_oplus(ROOT, gki), 'build-oplus-kernel')):
            steps = document['jobs'][job_name]['steps']
            indexed = {step['id']: step for step in steps if 'id' in step}
            for kind in ('ccache', 'thinlto'):
                before = indexed['persistent-' + kind + '-restore']['env']['OBJECT_CACHE_KEY']
                after = indexed['persistent-' + kind + '-save']['env']['OBJECT_CACHE_KEY']
                self.assertEqual(before, after)
                self.assertIn(profile.profile_expression(), before)
                self.assertIn('inputs.kernelsu_variant', before)
                if kind == 'thinlto':
                    self.assertIn('ksu_ref', before)
                    self.assertIn('gki_defconfig', before)


if __name__ == '__main__':
    unittest.main()
