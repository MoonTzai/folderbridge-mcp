from __future__ import annotations
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from folderbridge_mcp.config import CONFIG_NAME, ConfigError, _validate_task, load_config, approve_config, config_is_trusted
from folderbridge_mcp.node_task_registration import plan_node_task, install_node_task, NodeTaskRegistrationError

class TwoHourTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        (self.repo / 'a.cjs').write_text("console.log('local fixture')", encoding='utf8')
        (self.repo / 'b.cjs').write_text("console.log('different fixture')", encoding='utf8')
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.base/'state'), 'XDG_STATE_HOME': str(self.base/'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
    def existing(self, count=2):
        tasks = {'original': {'argv':['node','a.cjs'], 'timeout_seconds':600}}
        tasks.update({f'other{i}': {'argv':['node','b.cjs'], 'timeout_seconds':120} for i in range(count-1)})
        (self.repo/CONFIG_NAME).write_text(json.dumps({'version':1,'tasks':tasks}),encoding='utf8')
        return load_config(self.repo)
    def test_boundary_accepts_two_hours_and_rejects_invalid_values(self):
        for seconds in (1, 600, 601, 7200):
            self.assertEqual(_validate_task('fixed', {'argv':['node','a.cjs'],'timeout_seconds':seconds}).timeout_seconds, seconds)
        for seconds in (0, -1, 7201, True, 7200.0, '7200'):
            with self.subTest(seconds=seconds), self.assertRaises(ConfigError):
                _validate_task('fixed', {'argv':['node','a.cjs'],'timeout_seconds':seconds})
    def test_explicit_update_changes_only_timeout_and_does_not_inherit_trust(self):
        before = self.existing()
        approve_config(self.repo,before)
        plan = plan_node_task(self.repo,'original','a.cjs',7200,update_timeout=True)
        self.assertEqual(load_config(self.repo).sha256,before.sha256)
        after = install_node_task(plan)
        self.assertEqual(set(after.tasks),set(before.tasks))
        self.assertEqual(after.tasks['original'].argv,before.tasks['original'].argv)
        self.assertEqual(after.tasks['original'].timeout_seconds,7200)
        self.assertEqual(after.tasks['other0'],before.tasks['other0'])
        self.assertFalse(config_is_trusted(self.repo,after))
    def test_update_cannot_create_missing_task_change_argv_or_override_without_flag(self):
        self.existing()
        for name, script, update in [('original','a.cjs',False),('original','b.cjs',True),('missing','a.cjs',True)]:
            with self.subTest(name=name,script=script,update=update), self.assertRaises(NodeTaskRegistrationError):
                plan_node_task(self.repo,name,script,7200,update_timeout=update)
        with self.assertRaises(NodeTaskRegistrationError):
            plan_node_task(self.repo,'original','a.cjs',7200,update_timeout='yes')
    def test_update_at_capacity_retains_every_task(self):
        before = self.existing(20)
        after = install_node_task(plan_node_task(self.repo,'original','a.cjs',7200,update_timeout=True))
        self.assertEqual(len(after.tasks),20)
        self.assertEqual({k:v for k,v in after.tasks.items() if k!='original'}, {k:v for k,v in before.tasks.items() if k!='original'})
    def test_stale_and_forged_update_plans_do_not_write(self):
        self.existing()
        plan = plan_node_task(self.repo,'original','a.cjs',7200,update_timeout=True)
        payload = json.loads(plan.new_config_bytes)
        payload['tasks']['other0']['timeout_seconds'] = 7200
        blob = (json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
        forged = replace(plan,new_config_bytes=blob,new_config_sha256=hashlib.sha256(blob).hexdigest())
        before = (self.repo/CONFIG_NAME).read_bytes()
        with self.assertRaises(NodeTaskRegistrationError): install_node_task(forged)
        self.assertEqual((self.repo/CONFIG_NAME).read_bytes(),before)
        (self.repo/CONFIG_NAME).write_bytes(before+b'\n')
        with self.assertRaises(NodeTaskRegistrationError): install_node_task(plan)
        self.assertEqual((self.repo/CONFIG_NAME).read_bytes(),before+b'\n')
    def test_cli_default_and_update_keep_local_approval_separate(self):
        from folderbridge_mcp.cli import build_parser, main
        parsed = build_parser().parse_args(['node-task-add','--name','x','--script','a.cjs'])
        self.assertEqual(parsed.timeout,7200)
        self.existing()
        with contextlib.redirect_stdout(io.StringIO()):
            code=main(['node-task-add','--workspace',str(self.repo),'--name','original','--script','a.cjs','--timeout','7200','--update-timeout'])
        self.assertEqual(code,0)
        self.assertEqual(load_config(self.repo).tasks['original'].timeout_seconds,7200)
        self.assertFalse(config_is_trusted(self.repo,load_config(self.repo)))
    def test_gui_and_help_show_two_hour_maximum(self):
        from folderbridge_mcp.cli import build_parser
        gui=(Path(__file__).resolve().parents[1]/'folderbridge_mcp/gui.py').read_text(encoding='utf8')
        self.assertIn('maxvalue=7200',gui)
        self.assertNotIn('maxvalue=600, parent=self.root',gui)
        out=io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            build_parser().parse_args(['node-task-add','--help'])
        self.assertIn('7200',out.getvalue())
        self.assertIn('--update-timeout',out.getvalue())

if __name__=='__main__': unittest.main()
