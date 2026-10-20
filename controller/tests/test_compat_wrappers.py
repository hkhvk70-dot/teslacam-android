"""Exercise real shell wrappers through private copies and fake leaf tools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT.parent/'usb'

@unittest.skipUnless(os.name=='posix','real Android shell cases require POSIX')
class CompatibilityWrappers(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='teslacam-wrapper-private-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.runtime=self.base/'runtime with spaces'
        (self.runtime/'bin').mkdir(parents=True)
        self.python=self.runtime/'bin/python3'
        self.called=self.base/'called.json'
        self.python.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\nPath('+repr(str(self.called))+').write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(37)\n')
        self.python.chmod(0o700)
        self.cli=self.base/'private controller with spaces.py'
        self.cli.write_text('# private placeholder; never executed\n')
        self.fake_id=self.base/'fake-id'
        self.set_uid(0)

    def set_uid(self,uid):
        self.fake_id.write_text('#!/system/bin/sh\necho '+str(uid)+'\n')
        self.fake_id.chmod(0o700)

    def private_copy(self,name):
        raw=(SCRIPTS/name).read_text()
        commands='\n'.join(line for line in raw.splitlines() if not line.lstrip().startswith('#'))
        for forbidden in ('su -c','adb ','input tap','msd.sh','setprop','recover.lock','exec ','id -u','rm '):
            self.assertNotIn(forbidden,commands)
        self.assertIn('exit 78',commands)
        path=self.base/name
        path.write_text(raw)
        return path

    def invoke(self,name,*args):
        self.called.unlink(missing_ok=True)
        return subprocess.run(['/system/bin/sh',str(self.private_copy(name)),*args],capture_output=True,timeout=5)

    def test_real_wrapper_syntax(self):
        for name in ('maint.sh','recover.sh','self_recover.sh'):
            with self.subTest(name=name):
                p=subprocess.run(['/system/bin/sh','-n',str(SCRIPTS/name)],capture_output=True,timeout=5)
                self.assertEqual(p.returncode,0,p.stderr)

    def test_retired_entry_gives_instruction_without_calling_controller(self):
        for name,action in (('maint.sh','maintain'),('recover.sh','start'),('self_recover.sh','start')):
            with self.subTest(name=name):
                p=self.invoke(name)
                self.assertEqual(p.returncode,78,p.stderr)
                self.assertIn(('/data/teslacam/bin/teslacam.sh '+action).encode(),p.stderr)
                self.assertFalse(self.called.exists())

    def test_extra_arguments_refused_before_leaf_call(self):
        for name in ('maint.sh','recover.sh','self_recover.sh'):
            with self.subTest(name=name):
                p=self.invoke(name,'--force')
                self.assertEqual(p.returncode,64,p.stderr)
                self.assertFalse(self.called.exists())

    def test_legacy_force_environment_cannot_enable_entry(self):
        self.set_uid(2000)
        for name in ('maint.sh','recover.sh','self_recover.sh'):
            with self.subTest(name=name):
                env=dict(os.environ,FORCE='1',MAX_TRY='999')
                p=subprocess.run(['/system/bin/sh',str(self.private_copy(name))],capture_output=True,timeout=5,env=env)
                self.assertEqual(p.returncode,78,p.stderr)
                self.assertFalse(self.called.exists())

    def test_repeated_calls_do_not_change_private_state(self):
        self.python.unlink()
        state=self.base/'active_op.json'
        state.write_text('{"status":"finished","exit_code":0}')
        before=state.read_bytes()
        for name in ('maint.sh','recover.sh','self_recover.sh'):
            with self.subTest(name=name):
                for unused in range(3):
                    p=self.invoke(name)
                    self.assertEqual(p.returncode,78,p.stderr)
                    self.assertFalse(self.called.exists())
                    self.assertEqual(state.read_bytes(),before)

if __name__=='__main__': unittest.main()
