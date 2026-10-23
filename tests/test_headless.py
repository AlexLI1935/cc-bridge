import json
import pytest
import cc_bridge as cc


def test_write_headless_contract(bridge, project):
    tid=bridge.create_task('Code',str(project),'Implement',mode='write')['task_id']
    cmd=cc.build_command(bridge._task(tid))
    assert '--tools' not in cmd
    assert cmd[cmd.index('--permission-mode')+1]=='dontAsk'
    assert set(cmd[cmd.index('--allowedTools')+1:])=={'Read','Glob','Grep','Edit','Write','NotebookEdit','Bash'}
    assert not any('bypass' in c.lower() or 'skip-permissions' in c for c in cmd)


def test_read_only_keeps_write_and_bash_unavailable(bridge, project):
    tid=bridge.create_task('Read',str(project),'Review')['task_id']
    cmd=cc.build_command(bridge._task(tid))
    assert cmd[cmd.index('--tools')+1]=='Read,Glob,Grep'
    assert '--allowedTools' not in cmd


@pytest.mark.parametrize('raw',['"text"','123','null','true','[]'])
def test_json_scalar_output_fails_cleanly(raw):
    parsed=cc.parse_result_json(raw)
    assert parsed is None
    assert cc.classify_outcome(parsed,raw,0)==('FAILED','result_parse')


def test_valid_report_and_cli_success(bridge, project, tmp_path):
    import subprocess,sys
    from conftest import ROOT
    tid=bridge.create_task('Read',str(project),'Review')['task_id']
    assert bridge.run_task(tid)['status']=='COMPLETED'
    proc=subprocess.run([sys.executable,str(ROOT/'cc_bridge.py'),'result',tid],capture_output=True,text=True)
    value=json.loads(proc.stdout)
    assert proc.returncode==0 and value['status']=='COMPLETED' and value['report_validated']


def test_cli_version_diagnostic(fake_claude, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(cc.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=b'2.1.286 (Claude Code)'))
    assert cc.claude_diagnostic()=={'available':True,'version':'2.1.286'}
