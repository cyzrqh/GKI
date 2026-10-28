"""Audit exact native source closure, download scope and restoration transactions."""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_vendor_locking as locking

FIXTURE = ROOT / 'tests/fixtures/oplus-vendor-locking'


class VendorLockingTests(unittest.TestCase):
    def setUp(self):
        self.lock = locking.load_locking_lock()
        self.profiles = {p['id']: p for p in json.loads((ROOT/'config/oplus-kernel-profiles.json').read_text('utf-8'))['profiles']}
        self.profile = self.profiles['sm8650-6.1.141']
        self.selected = self.lock['profiles'][self.profile['id']]
        self.sources = self.fixture_sources(self.selected)
        self.root = ROOT/'tests'/('.vendor-locking-'+uuid.uuid4().hex)
        self.common = self.root/'common'
        (self.common/'kernel/locking').mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.root)

    def fixture_sources(self, selected):
        return {path:(FIXTURE/(spec['sha256']+'.source')).read_bytes() for path,spec in selected['files'].items()}

    def transport(self, profile_id, destination, size):
        self.assertEqual(self.profile['id'],profile_id)
        self.assertEqual(self.selected['files'][destination]['size'],size)
        return self.sources[destination]

    def simulation(self):
        # File publication is real. Only original dangling-link / native Git
        # preflight observations are mocked so Windows can exercise atomic
        # replacement and rollback without symlink creation privileges.
        return (patch.object(locking,'_check_common',return_value=self.selected),
                patch.object(locking,'_check_links'),patch.object(locking,'_check_link'),
                patch.object(locking,'download_locking_source',side_effect=self.transport))

    def test_all_three_matching_official_source_closures_pass_full_locks(self):
        for name, selected in self.lock['profiles'].items():
            with self.subTest(profile=name):
                locking.verify_source_bytes(self.fixture_sources(selected),selected)
                self.assertEqual(selected,locking._profile(self.profiles[name],self.lock))
        self.assertEqual(7,len(list(FIXTURE.iterdir())))

    def test_not_in_scope_is_noop_and_cannot_carry_foreign_provenance(self):
        profile=self.profiles['sm8650-6.1.57']
        with patch.object(locking,'download_locking_source') as fetch:
            self.assertEqual({},locking.restore_vendor_locking(self.common,profile))
            self.assertEqual({},locking.verify_vendor_locking_report(self.common,profile,{}))
            fetch.assert_not_called()
        with self.assertRaises(ValueError):
            locking.validate_vendor_locking_provenance(profile,{'commit':'unexpected'})

    def test_profile_pairing_rejects_branch_mixing_or_unfrozen_common_ref(self):
        for key,value in [('platform','mt6897'),('kernel','6.1.118'),('kernel_repo','other/common'),
                          ('kernel_ref','main'),('verified_kernel_ref','a'*40)]:
            profile={**self.profile,key:value}
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'pairing'):
                locking._profile(profile,self.lock)

    def test_native_c_header_wrong_version_or_any_byte_tamper_rejected(self):
        for path in self.sources:
            data={**self.sources,path:self.sources[path]+b'\n'}
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,'verification'):
                locking.verify_source_bytes(data,self.selected)
        with self.assertRaisesRegex(ValueError,'closure'):
            locking.verify_source_bytes({key:value for key,value in self.sources.items() if key.endswith('.c')},self.selected)
        other=self.fixture_sources(self.lock['profiles']['sm8650-6.1.75'])
        with self.assertRaisesRegex(ValueError,'verification'):
            locking.verify_source_bytes(other,self.selected)

    def test_restore_is_complete_idempotent_and_does_not_change_makefile_or_options(self):
        native=self.common/'kernel/locking/Makefile'
        native.write_bytes((FIXTURE/'native-Makefile.source').read_bytes())
        config=self.common/'native-config'
        config.write_bytes(b'CONFIG_OPLUS_SCHED_TUNE=y\nCONFIG_VENDOR_NATIVE=y\n')
        a,b,c,d=self.simulation()
        with a,b,c,d as fetch:
            report=locking.restore_vendor_locking(self.common,self.profile)
            self.assertEqual(3,fetch.call_count)
            self.assertEqual(report,locking.restore_vendor_locking(self.common,self.profile))
            self.assertEqual(3,fetch.call_count)
            self.assertEqual(report,locking.verify_vendor_locking_report(self.common,self.profile,report))
        self.assertEqual((FIXTURE/'native-Makefile.source').read_bytes(),native.read_bytes())
        self.assertEqual(b'CONFIG_OPLUS_SCHED_TUNE=y\nCONFIG_VENDOR_NATIVE=y\n',config.read_bytes())
        self.assertFalse(list((self.common/'kernel/locking').glob('.gki-locking-*')))
        self.assertEqual(self.selected['vendor_commit'],report['commit'])
        self.assertEqual(self.selected['common_commit'],report['common_commit'])
        self.assertEqual(locking.DESTINATIONS,set(report['files']))

    def test_every_download_is_verified_before_first_file_publication(self):
        def altered(profile_id,path,size):
            data=self.transport(profile_id,path,size)
            return data+b'X' if path.endswith('sa_common_struct.h') else data
        a,b,c,d=self.simulation()
        with a,b,c,d,patch.object(locking,'download_locking_source',side_effect=altered),patch.object(locking.os,'replace') as replace:
            with self.assertRaisesRegex(ValueError,'verification'):
                locking.restore_vendor_locking(self.common,self.profile)
            replace.assert_not_called()
        self.assertEqual([],list((self.common/'kernel/locking').iterdir()))

    def test_partial_or_changed_original_links_fail_before_download(self):
        (self.common/'kernel/locking/locking_main.h').write_bytes(self.sources['kernel/locking/locking_main.h'])
        with patch.object(locking,'_check_common',return_value=self.selected),patch.object(locking,'download_locking_source') as fetch:
            with self.assertRaisesRegex(ValueError,'dangling'):
                locking.restore_vendor_locking(self.common,self.profile)
            fetch.assert_not_called()

    def test_late_replace_failure_restores_only_this_calls_original_links(self):
        real_replace=os.replace
        calls=0
        def replace(source,destination):
            nonlocal calls
            calls+=1
            if calls==2:
                raise OSError('injected local publication failure')
            return real_replace(source,destination)
        def symbolic(target,path):
            # This host-only transport simulation encodes an original link in
            # a file; production uses os.symlink without this replacement.
            Path(path).write_bytes(target.encode())
        a,b,c,d=self.simulation()
        with a,b,c,d,patch.object(locking.os,'replace',side_effect=replace),patch.object(locking.os,'symlink',side_effect=symbolic):
            with self.assertRaises(OSError):
                locking.restore_vendor_locking(self.common,self.profile)
        first=next(iter(self.sources))
        self.assertEqual(self.selected['files'][first]['symlink_target'].encode(),(self.common/first).read_bytes())
        self.assertFalse(list((self.common/'kernel/locking').glob('.gki-locking-*')))

    def test_real_dangling_symlinks_are_replaced_without_populating_external_vendor(self):
        for path,spec in self.selected['files'].items():
            try:
                (self.common/path).symlink_to(spec['symlink_target'])
            except OSError:
                self.skipTest('This host cannot create filesystem symlinks; Linux runs the native replacement test')
        with patch.object(locking,'_check_common',return_value=self.selected), \
                patch.object(locking,'download_locking_source',side_effect=self.transport):
            report=locking.restore_vendor_locking(self.common,self.profile)
            self.assertEqual(report,locking.restore_vendor_locking(self.common,self.profile))
        for path,data in self.sources.items():
            self.assertFalse((self.common/path).is_symlink())
            self.assertEqual(data,(self.common/path).read_bytes())
        self.assertEqual({'common'},{path.name for path in self.root.iterdir()})

    def test_installed_tamper_is_rejected_without_redownloading_or_replacing_it(self):
        a,b,c,d=self.simulation()
        with a,b,c,d:
            report=locking.restore_vendor_locking(self.common,self.profile)
        target=self.common/'kernel/locking/locking_main.h'
        target.write_bytes(b'changed independently')
        with patch.object(locking,'_check_common',return_value=self.selected),patch.object(locking,'download_locking_source') as fetch:
            with self.assertRaisesRegex(ValueError,'verification'):
                locking.restore_vendor_locking(self.common,self.profile)
            with self.assertRaisesRegex(ValueError,'verification'):
                locking.verify_vendor_locking_report(self.common,self.profile,report)
            fetch.assert_not_called()
        self.assertEqual(b'changed independently',target.read_bytes())

    def test_provenance_rejects_missing_profile_file_or_modified_hash(self):
        report=locking._provenance(self.lock,self.selected)
        self.assertEqual(report,locking.validate_vendor_locking_provenance(self.profile,report))
        for value in ({},{**report,'commit':'a'*40},{**report,'schema_version':True}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                locking.validate_vendor_locking_provenance(self.profile,value)
        altered=copy.deepcopy(report)
        altered['files']['kernel/locking/oplus_locking.c']['sha256']='a'*64
        with self.assertRaises(ValueError):
            locking.validate_vendor_locking_provenance(self.profile,altered)

    def response(self, *, data=None, url=None):
        path='kernel/locking/oplus_locking.c'
        spec=self.selected['files'][path]
        expected=('https://raw.githubusercontent.com/'+self.lock['vendor_repository']+'/'
                  +self.selected['vendor_commit']+'/'+spec['source_path'])
        response=MagicMock()
        response.__enter__.return_value=response
        response.status=200
        response.geturl.return_value=url or expected
        response.read.return_value=self.sources[path] if data is None else data
        opener=MagicMock()
        opener.open.return_value=response
        return path,spec,response,opener,expected

    def test_download_is_fixed_public_get_bounded_and_never_sends_github_token(self):
        path,spec,response,opener,url=self.response()
        with patch.dict(os.environ,{'GH_TOKEN':'private-value'}),patch.object(locking,'build_opener',return_value=opener):
            self.assertEqual(self.sources[path],locking.download_locking_source(self.profile['id'],path,spec['size']))
        request=opener.open.call_args.args[0]
        self.assertEqual(url,request.full_url)
        self.assertEqual('GET',request.get_method())
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(spec['size']+1,response.read.call_args.args[0])
        self.assertIsNone(locking._NoRedirect().redirect_request(None,None,302,'',{},'https://untrusted/'))
        for name,source,size in [('unknown',path,spec['size']),(self.profile['id'],'../../outside',1),(self.profile['id'],path,True)]:
            with self.assertRaises(ValueError):
                locking.download_locking_source(name,source,size)

    def test_download_retries_network_only_and_rejects_redirect_or_truncation(self):
        path,spec,response,opener,_=self.response()
        opener.open.side_effect=[URLError('private-response'),response]
        with patch.object(locking,'build_opener',return_value=opener),patch.object(locking.time,'sleep') as wait:
            locking.download_locking_source(self.profile['id'],path,spec['size'])
            self.assertEqual(1,wait.call_count)
        for data,url in [(b'bad',None),(None,'https://untrusted/')]:
            path,spec,response,opener,_=self.response(data=data,url=url)
            with patch.object(locking,'build_opener',return_value=opener),patch.object(locking.time,'sleep') as wait:
                with self.assertRaises(ValueError):
                    locking.download_locking_source(self.profile['id'],path,spec['size'])
                wait.assert_not_called()

    def prepare_native_preflight_fixture(self):
        makefile=(FIXTURE/'native-Makefile.source').read_bytes()
        core=b'#include "../locking/locking_main.h"\nvoid test_switch(void) { locking_record_switch_in_cs(prev); }\n'
        (self.common/'kernel/locking/Makefile').write_bytes(makefile)
        (self.common/'kernel/sched').mkdir()
        (self.common/'kernel/sched/core.c').write_bytes(core)
        generated_rule=(locking.VERSION_HEADER_DECLARATION+b'archprepare: headers $(version_h)\n'
                        +b'define filechk_version.h\nendef\n'+locking.VERSION_HEADER_PRODUCER)
        (self.common/'Makefile').write_bytes(generated_rule)
        for name in locking.COMMON_HEADERS-locking.GENERATED_HEADERS:
            target=self.common/'include'/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(b'/* availability-only native preflight fixture; not compiled */\n')
        def git(common,*arguments):
            self.assertEqual(self.common,common)
            if arguments==('rev-parse','--verify','HEAD'):
                return (self.selected['common_commit']+'\n').encode()
            if arguments==('rev-parse','--show-toplevel'):
                return (str(self.common)+'\n').encode()
            path=arguments[-1]
            spec=self.selected['files'][path]
            return (b'120000 blob '+locking._blob(spec['symlink_target'].encode()).encode()+b'\t'+path.encode()+b'\0')
        def blob(common,commit,path):
            self.assertEqual(self.common,common)
            self.assertEqual(self.selected['common_commit'],commit)
            if path=='kernel/locking/Makefile':return makefile
            if path=='kernel/sched/core.c':return core
            if path=='Makefile':return generated_rule
            return self.selected['files'][path]['symlink_target'].encode()
        return patch.object(locking,'_git',side_effect=git),patch.object(locking,'_native_blob',side_effect=blob)

    def test_common_preflight_checks_fixed_git_link_modes_native_call_and_header_closure(self):
        a,b=self.prepare_native_preflight_fixture()
        with a,b:
            self.assertFalse((self.common/'include/linux/version.h').exists())
            self.assertFalse((self.common/'include/generated/uapi/linux/version.h').exists())
            self.assertEqual(self.selected,locking._check_common(self.common,self.profile,self.lock))
            required=self.common/'include'/next(iter(locking.COMMON_HEADERS-locking.GENERATED_HEADERS))
            required.unlink()
            with self.assertRaisesRegex(ValueError,'header is missing'):
                locking._check_common(self.common,self.profile,self.lock)

    def test_generated_version_header_is_only_allowed_with_its_native_makefile_producer(self):
        a,b=self.prepare_native_preflight_fixture()
        path=self.common/'Makefile'
        path.write_bytes(path.read_bytes().replace(locking.VERSION_HEADER_PRODUCER,b''))
        with a,b,self.assertRaisesRegex(ValueError,'build-generated linux/version.h producer'):
            locking._check_common(self.common,self.profile,self.lock)

    def test_common_makefile_or_scheduler_cannot_be_disabled_to_hide_the_dependency(self):
        a,b=self.prepare_native_preflight_fixture()
        with a,b:
            path=self.common/'kernel/locking/Makefile'
            original=path.read_bytes()
            path.write_bytes(original.replace(b' oplus_locking.o',b''))
            with self.assertRaisesRegex(ValueError,'mandatory locking Makefile'):
                locking._check_common(self.common,self.profile,self.lock)
            path.write_bytes(original)
            core=self.common/'kernel/sched/core.c'
            core.write_bytes(core.read_bytes().replace(b'locking_record_switch_in_cs(prev);',b''))
            with self.assertRaisesRegex(ValueError,'scheduler locking integration'):
                locking._check_common(self.common,self.profile,self.lock)

    def test_real_native_headers_preprocess_the_complete_local_include_closure(self):
        compiler=os.environ.get('OPLUS_LOCKING_CC') or shutil.which('clang') or shutil.which('cc')
        if not compiler:
            self.skipTest('A C preprocessor is required for the actual native header closure')
        is_zig=Path(compiler).stem.lower()=='zig'
        command=[compiler]+(['cc','-target','aarch64-linux-gnu'] if is_zig else [])
        for profile,selected in self.lock['profiles'].items():
            inputs=self.fixture_sources(selected)
            for path,data in inputs.items():
                (self.common/'kernel/locking'/Path(path).name).write_bytes(data)
            for conditional in ('0','1'):
                with self.subTest(profile=profile,conditional=conditional):
                    environment={**os.environ,'ZIG_GLOBAL_CACHE_DIR':str(self.root/'zig-global'),
                                 'ZIG_LOCAL_CACHE_DIR':str(self.root/'zig-local')}
                    result=subprocess.run([*command,'-E','-P','-x','c','-DIS_ENABLED(option)='+conditional,
                                           '-DCONFIG_OPLUS_LOCKING_MONITOR=1','-I',str(self.common/'kernel/locking'),'-'],
                                          input='#include "locking_main.h"\n',capture_output=True,text=True,
                                          encoding='utf-8',timeout=30,env=environment)
                    self.assertEqual(0,result.returncode,result.stderr)
                    self.assertIn('struct locking_info',result.stdout)
                    self.assertIn('locking_record_switch_in_cs',result.stdout)
                    self.assertNotIn('#include',result.stdout)


if __name__=='__main__':
    unittest.main()
