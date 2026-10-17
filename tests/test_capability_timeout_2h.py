from __future__ import annotations
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import folderbridge_mcp.capabilities as caps
from folderbridge_mcp.config import _validate_task, default_config, load_config

class CapabilityTwoHourTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.calls = []
    def write(self, name, content=''):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf8')
    def capture(self, root, task):
        self.calls.append((root, task))
        return {'exit_code': 0, 'stdout':'fixture', 'stderr':'', 'timed_out':False}
    def run_and_check(self, name, argv):
        result = caps.run_capability(self.root, name, task_runner=self.capture)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.calls[-1][0], self.root)
        self.assertEqual(self.calls[-1][1].timeout_seconds, 7200)
        self.assertEqual(self.calls[-1][1].argv, tuple(argv))
        self.assertFalse((self.root/'.folderbridge.json').exists())
    def test_all_npm_execution_capabilities_receive_two_hours_at_public_dispatch(self):
        mapping={'test':'test','build':'build','package-windows':'package:windows','package-android':'package:android','release-sync':'release:sync'}
        self.write('package.json',json.dumps({'scripts':{v:'node fixture.cjs' for v in mapping.values()}}))
        for name,script in mapping.items():
            with self.subTest(name=name):
                self.run_and_check(name,['npm','run',script])
    def test_python_discovery_and_pytest_execution_receive_two_hours(self):
        self.write('tests/test_fixture.py','import unittest\n')
        self.run_and_check('test',['python','-m','unittest','discover','-s','tests','-p','test_*.py','-v'])
        (self.root/'tests/test_fixture.py').unlink()
        self.write('pytest.ini','[pytest]\n')
        self.run_and_check('test',['python','-m','pytest','-q'])
    def test_windows_and_posix_packaging_paths_receive_two_hours(self):
        self.write('scripts/build_windows.ps1',"Write-Output 'fixture'\n")
        with patch.object(caps,'os',types.SimpleNamespace(name='nt')):
            self.run_and_check('package-windows',['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','scripts/build_windows.ps1'])
        with patch.object(caps,'os',types.SimpleNamespace(name='posix')):
            self.run_and_check('package-windows',['pwsh','-NoProfile','-File','scripts/build_windows.ps1'])
        (self.root/'scripts/build_windows.ps1').unlink()
        self.write('fixture.spec','# fixture\n')
        self.run_and_check('package-windows',['python','-m','PyInstaller','--noconfirm','fixture.spec'])
    def test_gradle_and_flutter_receive_two_hours_without_executing_either(self):
        self.write('gradlew.bat','@echo fixture\n')
        with patch.object(caps,'os',types.SimpleNamespace(name='nt')):
            self.run_and_check('package-android',['cmd.exe','/d','/c','gradlew.bat','assembleRelease'])
        (self.root/'gradlew.bat').unlink()
        self.write('gradlew','#!/bin/sh\n')
        with patch.object(caps,'os',types.SimpleNamespace(name='posix')):
            self.run_and_check('package-android',['sh','gradlew','assembleRelease'])
        (self.root/'gradlew').unlink()
        self.write('pubspec.yaml','name: fixture\n')
        self.run_and_check('package-android',['flutter','build','apk','--release'])
    def test_git_push_execution_uses_two_hours_without_network_or_permission_changes(self):
        def inspect(root,*args):
            if args == ('rev-parse','--show-toplevel'): return str(root)
            if args == ('symbolic-ref','--quiet','--short','HEAD'): return 'test-branch'
            if args == ('config','--local','--list'): return ''
            if args == ('remote','get-url','--push','origin'): return 'https://github.com/example/fixture.git'
            self.fail('Unexpected Git inspection')
        with patch.object(caps,'_git_text',side_effect=inspect), patch.object(caps,'resolve_task_executable',return_value='git'):
            self.run_and_check('git-push',['git','push','--porcelain','--no-verify','origin','HEAD:refs/heads/test-branch'])
    def test_normal_new_task_defaults_are_two_hours_but_explicit_values_remain(self):
        self.assertEqual(_validate_task('new',{'argv':['node','fixture.cjs']}).timeout_seconds,7200)
        self.assertEqual(_validate_task('explicit',{'argv':['node','fixture.cjs'],'timeout_seconds':120}).timeout_seconds,120)
        self.write('pyproject.toml','[project]\nname="fixture"\n')
        self.write('package.json',json.dumps({'scripts':{'test':'node a.cjs','lint':'node b.cjs','typecheck':'node c.cjs'}}))
        config=default_config(self.root)
        self.assertEqual(len(config['tasks']),4)
        self.assertTrue(all(t['timeout_seconds']==7200 for t in config['tasks'].values()))
        self.assertEqual(load_config(self.root).tasks,{})
    def test_inspection_wait_and_unavailable_capabilities_are_not_promoted_into_long_tasks(self):
        with patch.object(caps,'resolve_task_executable',return_value='git'), patch.object(caps,'run_task',return_value={'exit_code':0,'stdout':'fixture','stderr':'','truncated':False,'timed_out':False}) as run:
            self.assertEqual(caps._git_text(self.root,'--version'),'fixture')
            self.assertEqual(run.call_args.args[1].timeout_seconds,15)
        with self.assertRaises(caps.ToolError):
            caps.run_capability(self.root,'package-windows',task_runner=self.capture)
        self.assertEqual(self.calls,[])

if __name__=='__main__': unittest.main()
