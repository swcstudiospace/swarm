"""Isolated .NET lifecycle/report tests: real child processes and real TRX parsing.

The fixture is a controlled CLI, not an installed SDK or console-count substitute.
Only the parent runs these tests; canonical SDK/MTP compatibility needs app checks.
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts import _dotnet_quality as dotnet


CLI = r'''#!@PY@
import json, os, signal, subprocess, sys, time
from pathlib import Path
from xml.etree import ElementTree as ET
root = Path.cwd()
config = json.loads((root / 'scenario.json').read_text())
stage = sys.argv[1]
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\n')
if config.get('hang') == stage:
    # The grandchild deliberately drops the leader's output handles and ignores
    # TERM: leader exit alone is not proof that owned descendants have stopped.
    child = subprocess.Popen([sys.executable, '-c',
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('descendant.pid').write_text(str(os.getpid())); time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not (root / 'descendant.pid').exists():
        time.sleep(.01)
    (root / 'ready').write_text(str(os.getpid()))
    while True:
        time.sleep(1)
if stage == 'test':
    output = Path(sys.argv[sys.argv.index('--results-directory') + 1])
    for index, report in enumerate(config.get('reports', [{'module': 'Auth.Tests', 'outcomes': ['Passed']}])):
        path = output / (str(index) + '.trx')
        if 'raw' in report:
            path.write_text(report['raw'])
            continue
        outcomes = report.get('outcomes', [])
        ns = 'http://microsoft.com/schemas/VisualStudio/TeamTest/2010'
        tag = lambda name: '{' + ns + '}' + name
        run = ET.Element(tag('TestRun'), id=str(index))
        definitions = ET.SubElement(run, tag('TestDefinitions'))
        results = ET.SubElement(run, tag('Results'))
        for number, outcome in enumerate(outcomes):
            test_id = str(number)
            definition = ET.SubElement(definitions, tag('UnitTest'), id=test_id,
                storage='/build/' + report.get('storage_module', report.get('module', 'Auth.Tests')) + '.dll')
            ET.SubElement(definition, tag('TestMethod'),
                codeBase='/build/' + report.get('codebase_module', report.get('module', 'Auth.Tests')) + '.dll')
            ET.SubElement(results, tag('UnitTestResult'), testId=test_id, executionId=test_id, outcome=outcome)
        summary = ET.SubElement(run, tag('ResultSummary'), outcome=report.get('summary', 'Completed'))
        counts = dict(total=len(outcomes), executed=sum(o not in ('NotExecuted', 'NotRunnable') for o in outcomes),
                      passed=outcomes.count('Passed'), failed=outcomes.count('Failed'))
        counts.update(report.get('counters', {}))
        ET.SubElement(summary, tag('Counters'), **{key: str(value) for key, value in counts.items()})
        ET.ElementTree(run).write(path, encoding='utf-8', xml_declaration=True)
sys.exit(config.get(stage + '_exit', 0))
'''


@pytest.fixture
def dotnet_repo(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'global.json').write_text(json.dumps({'sdk': {'version': '10.0.402', 'rollForward': 'disable'},
                                               'test': {'runner': 'Microsoft.Testing.Platform'}}))
    (root / 'App.slnf').write_text('{}')
    (root / 'scenario.json').write_text('{}')
    binary = tmp_path / 'bin'
    binary.mkdir()
    executable = binary / 'dotnet'
    executable.write_text(CLI.replace('@PY@', sys.executable))
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary))
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.setattr(dotnet.tempfile, 'tempdir', str(tmp_path))
    return root


def configure(root, **scenario):
    (root / 'scenario.json').write_text(json.dumps(scenario))


def execute(root, **kwargs):
    return dotnet.run_dotnet(root, target='App.slnf', configuration='Release', runtime_version='10.0.13',
                             required_modules=['Auth.Tests'], timeout=10, **kwargs)


def calls(root):
    path = root / 'calls.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_positive_trx_and_stage_execution(dotnet_repo):
    result = execute(dotnet_repo)
    assert result['ok'] and result['executed'] == result['passed'] == 1
    assert result['modules']['auth.tests']['executed'] == 1
    assert Path(result['reports'][0]).parent == Path(result['results_directory'])
    assert [stage['status'] for stage in result['stages']] == ['pass', 'pass', 'pass']


@pytest.mark.parametrize('stage,exit_code', [('restore', 23), ('build', 17), ('test', 9)])
def test_stage_failure_never_launches_dependents(dotnet_repo, stage, exit_code):
    configure(dotnet_repo, **{stage + '_exit': exit_code})
    result = execute(dotnet_repo)
    assert not result['ok'] and result['returncode'] == exit_code and result['stage'] == stage
    names = ['restore', 'build', 'test']
    assert [call[0] for call in calls(dotnet_repo)] == names[:names.index(stage) + 1]
    assert all(item['status'] == 'skipped:dependency' for item in result['stages'][names.index(stage) + 1:])


@pytest.mark.parametrize('reports', [
    [], [{'raw': '<broken'}], [{'raw': '<TestRun />'}],
    [{'module': 'Auth.Tests', 'outcomes': []}],
    [{'module': 'Auth.Tests', 'outcomes': ['NotExecuted']}],
    [{'module': 'Auth.Tests', 'outcomes': ['Failed']}],
    [{'module': 'Auth.Tests', 'outcomes': ['Error']}],
    [{'module': 'Auth.Tests', 'outcomes': ['Timeout']}],
    [{'module': 'Auth.Tests', 'outcomes': ['Aborted']}],
    [{'module': 'Auth.Tests', 'outcomes': ['Passed'], 'summary': 'Aborted'}],
    [{'module': 'Auth.Tests', 'outcomes': ['Passed'], 'counters': {'executed': 0}}],
    [{'module': 'Auth.Tests', 'outcomes': ['Passed'], 'counters': {'passed': '-1'}}],
    [{'module': 'Auth.Tests', 'outcomes': ['Passed'], 'counters': {'total': 'NaN'}}],
    [{'module': 'Auth.Tests', 'outcomes': ['Passed'], 'counters': {'error': 1}}],
    [{'module': 'Other.Tests', 'outcomes': ['Passed']}],
    [{'module': 'Other.Tests', 'outcomes': ['Passed']}, {'module': 'Auth.Tests', 'outcomes': []}],
    [{'module': 'Other.Tests', 'outcomes': ['Passed']}, {'module': 'Auth.Tests', 'outcomes': ['NotExecuted']}],
])
def test_invalid_or_missing_execution_fails_closed(dotnet_repo, reports):
    configure(dotnet_repo, reports=reports)
    result = execute(dotnet_repo)
    assert not result['ok'] and result['stage'] == 'test' and result['reason']
    assert result['stages'][-1]['status'] == 'fail'


def test_stale_external_reports_do_not_count(dotnet_repo):
    first = execute(dotnet_repo)
    assert first['ok']
    configure(dotnet_repo, reports=[])
    second = execute(dotnet_repo)
    assert not second['ok'] and second['reports'] == []
    assert first['results_directory'] != second['results_directory']
    assert Path(first['reports'][0]).exists()


def test_required_modules_each_need_execution(dotnet_repo):
    configure(dotnet_repo, reports=[{'module': 'Auth.Tests', 'outcomes': ['Passed', 'NotExecuted']},
                                   {'module': 'Accounts.Tests', 'outcomes': ['Passed']}])
    result = dotnet.run_dotnet(dotnet_repo, target='App.slnf', timeout=10,
                               required_modules=['Auth.Tests', 'Accounts.Tests'])
    assert result['ok'] and result['executed'] == 2 and result['total'] == 3
    assert result['modules']['accounts.tests']['passed'] == 1


def test_missing_sdk_is_required_infrastructure_failure(dotnet_repo, monkeypatch):
    monkeypatch.setenv('PATH', '')
    result = execute(dotnet_repo)
    assert not result['ok'] and result['stage'] == 'restore' and result['returncode'] is None
    assert result['stages'][0]['status'] == 'error'


def test_target_ambiguity_and_unsupported_runner_fail_before_spawn(dotnet_repo):
    (dotnet_repo / 'Other.sln').write_text('')
    result = dotnet.run_dotnet(dotnet_repo, timeout=10)
    assert not result['ok'] and 'target' in result['reason'] and calls(dotnet_repo) == []
    (dotnet_repo / 'global.json').write_text('{}')
    result = execute(dotnet_repo)
    assert not result['ok'] and 'Microsoft.Testing.Platform' in result['reason']
    assert calls(dotnet_repo) == []


def wait_for(path, proc, timeout=8):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, str(path)
        time.sleep(.01)


def alive(pid):
    # A killed orphan can briefly remain a zombie until the host's init reaps it.
    status = Path(f'/proc/{pid}/stat')
    if status.exists() and status.read_text().split(') ', 1)[1].startswith('Z'):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.skipif(os.name != 'posix', reason='POSIX owned process groups')
@pytest.mark.parametrize('mode', ['timeout', 'SIGTERM', 'SIGINT', 'SIGHUP'])
def test_owned_descendants_stopped_and_unrelated_sentinel_survives(dotnet_repo, mode):
    configure(dotnet_repo, hang='build')
    result_path = dotnet_repo / 'result.json'
    driver = (
        'import json, sys; from pathlib import Path; '
        f'sys.path.insert(0, {str(ROOT)!r}); '
        'from scripts._dotnet_quality import run_dotnet; '
        f'r = run_dotnet(Path({str(dotnet_repo)!r}), target="App.slnf", '
        f'timeout={5 if mode == "timeout" else 30}, stop_grace=.1); '
        f'Path({str(result_path)!r}).write_text(json.dumps(r))'
    )
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
    proc = subprocess.Popen([sys.executable, '-c', driver], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    owned = []
    try:
        wait_for(dotnet_repo / 'ready', proc)
        owned = [int((dotnet_repo / name).read_text()) for name in ('ready', 'descendant.pid')]
        if mode != 'timeout':
            proc.send_signal(getattr(signal, mode))
        stdout, stderr = proc.communicate(timeout=12)
        assert proc.returncode == 0, (stdout, stderr)
        result = json.loads(result_path.read_text())
        assert not result['ok'] and result['stage'] == 'build'
        assert result['stages'][1]['status'] == ('timeout' if mode == 'timeout' else 'cancelled')
        assert [call[0] for call in calls(dotnet_repo)] == ['restore', 'build']
        assert all(not alive(pid) for pid in owned)
        assert sentinel.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        for pid in owned:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        sentinel.terminate()
        sentinel.wait(timeout=5)


def load_gate():
    spec = importlib.util.spec_from_file_location('dotnet_gate_under_test', ROOT / 'scripts' / 'qa_gate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('first_fails', [True, False])
def test_mixed_runner_failure_cannot_be_overwritten(dotnet_repo, monkeypatch, first_fails):
    from types import SimpleNamespace

    gate = load_gate()
    ordinary = {'name': 'python assertion', 'cmd': [sys.executable, '-c', 'raise SystemExit(7)'],
                'pass_re': r'(\d+) passed', 'fail_re': r'(\d+) failed'}
    runners = [ordinary, {'name': 'dotnet'}] if first_fails else [{'name': 'dotnet'}, ordinary]
    monkeypatch.setattr(gate, 'detect_runners', lambda root: runners)
    captured = {}

    def capture(ctx, **kwargs):
        captured.update(kwargs)
        return {}, set()

    # Only the authority boundary is replaced here. The actual executor, child
    # exit statuses and report parser all run; signature authority is tested in
    # test_runner_gates with the ordinary isolated TaskStore/signing fixtures.
    monkeypatch.setattr(gate, 'issue_gate', capture)
    monkeypatch.setattr(gate, 'gate_verdict', lambda env, recorded: 'fail')
    args = gate.AgentScript('A08', 'qa_gate', gate.run, add_args=gate.add_args).parser().parse_args([
        '--risk-class', 'medium', '--dotnet-target', 'App.slnf',
        '--dotnet-required-test-module', 'Auth.Tests'])
    result = gate.run(args, SimpleNamespace(root=dotnet_repo, task_id=None, dry_run=False))
    assert result['runs']['unit'] == result['runs']['integration'] == 'fail'
    assert captured['findings'] and captured['runs']['unit'] == 'fail'
    assert next(item for item in result['executed'] if item['runner'] == 'dotnet')['executed'] == 1


def test_shared_deadline_and_restored_signal_handlers(dotnet_repo, monkeypatch):
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    deadlines = []
    real_stage = dotnet._stage

    def observe(name, argv, root, deadline, stopped, grace):
        deadlines.append(deadline)
        return real_stage(name, argv, root, deadline, stopped, grace)

    monkeypatch.setattr(dotnet, '_stage', observe)
    assert execute(dotnet_repo)['ok']
    assert len(deadlines) == 3 and len(set(deadlines)) == 1
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


def test_exhausted_budget_never_spawns(dotnet_repo):
    result = dotnet.run_dotnet(dotnet_repo, target='App.slnf', timeout=0)
    assert not result['ok'] and result['stage'] == 'restore'
    assert result['stages'][0]['status'] == 'timeout' and calls(dotnet_repo) == []


@pytest.mark.parametrize('exit_code', [0, 9])
@pytest.mark.parametrize('summary', ['Completed', 'Failed', 'Aborted'])
def test_failed_reports_preserve_measured_counts(dotnet_repo, exit_code, summary):
    configure(dotnet_repo, test_exit=exit_code,
              reports=[{'module': 'Auth.Tests', 'outcomes': ['Passed', 'Failed'], 'summary': summary}])
    result = execute(dotnet_repo)
    assert not result['ok'] and result['returncode'] == exit_code
    assert result['executed'] == 2 and result['passed'] == result['failed'] == 1
    assert result['modules']['auth.tests']['failed'] == 1
    assert result['stages'][-1]['status'] == 'fail'
    assert result['stages'][-1]['returncode'] == exit_code
    if exit_code:
        assert f'test exited {exit_code}' in result['reason']


def test_trx_identity_is_storage_not_filename(dotnet_repo):
    from xml.etree import ElementTree as ET

    result = execute(dotnet_repo)
    path = Path(result['reports'][0])
    tree = ET.parse(path)
    namespace = '{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}'
    unit = tree.find(f'{namespace}TestDefinitions/{namespace}UnitTest')
    unit.set('storage', r'C:\build\Auth.Tests.dll')
    unit.find(namespace + 'TestMethod').set('codeBase', r'C:\build\Auth.Tests.dll')
    tree.write(path)
    assert dotnet.parse_trx(path.parent, ['Auth.Tests'])['modules']['auth.tests']['executed'] == 1
    with pytest.raises(ValueError, match='required test module'):
        dotnet.parse_trx(path.parent, ['Other.Tests'])


@pytest.mark.parametrize('mutation', ['missing-counters', 'missing-definition', 'duplicate-definition',
                                    'duplicate-result', 'run-error'])
def test_report_structure_and_run_errors_fail_closed(dotnet_repo, mutation):
    from xml.etree import ElementTree as ET

    result = execute(dotnet_repo)
    path = Path(result['reports'][0])
    tree = ET.parse(path)
    run = tree.getroot()
    namespace = '{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}'
    if mutation == 'missing-counters':
        summary = run.find(namespace + 'ResultSummary')
        summary.remove(summary.find(namespace + 'Counters'))
    elif mutation == 'missing-definition':
        run.remove(run.find(namespace + 'TestDefinitions'))
    elif mutation == 'duplicate-definition':
        definitions = run.find(namespace + 'TestDefinitions')
        definitions.append(ET.fromstring(ET.tostring(definitions[0])))
    elif mutation == 'duplicate-result':
        results = run.find(namespace + 'Results')
        results.append(ET.fromstring(ET.tostring(results[0])))
    else:
        ET.SubElement(run.find(namespace + 'ResultSummary'), namespace + 'ErrorInfo')
    tree.write(path)
    with pytest.raises(ValueError):
        dotnet.parse_trx(path.parent, ['Auth.Tests'])


def test_case_equivalent_definition_sources_and_required_names_merge(dotnet_repo):
    configure(dotnet_repo, reports=[
        {'storage_module': 'auth.tests', 'codebase_module': 'Auth.Tests', 'outcomes': ['Passed']},
        {'storage_module': 'AUTH.TESTS', 'codebase_module': 'auth.tests', 'outcomes': ['Passed']},
    ])
    result = dotnet.run_dotnet(dotnet_repo, target='App.slnf', timeout=10, required_modules=['AuTh.TeStS'])
    assert result['ok'] and result['executed'] == result['passed'] == 2
    assert list(result['modules']) == ['auth.tests']
    assert result['modules']['auth.tests']['executed'] == 2


def test_genuinely_conflicting_definition_sources_still_fail(dotnet_repo):
    configure(dotnet_repo, reports=[
        {'storage_module': 'Auth.Tests', 'codebase_module': 'Other.Tests', 'outcomes': ['Passed']},
    ])
    result = execute(dotnet_repo)
    assert not result['ok'] and 'conflicting test definition assembly identities' in result['reason']
    assert result['stages'][-1]['status'] == 'fail'


def test_nonzero_test_exit_keeps_positive_passing_report_counts_without_passing(dotnet_repo):
    configure(dotnet_repo, test_exit=9)
    result = execute(dotnet_repo)
    assert not result['ok'] and result['returncode'] == 9
    assert result['executed'] == result['passed'] == 1 and result['failed'] == 0
    assert result['modules']['auth.tests']['executed'] == 1
    assert result['stages'][-1]['status'] == 'fail' and 'test exited 9' in result['reason']


@pytest.mark.parametrize('summary', ['Failed', 'Aborted'])
def test_failed_summary_keeps_positive_counts_but_never_passes(dotnet_repo, summary):
    configure(dotnet_repo, reports=[{'module': 'Auth.Tests', 'outcomes': ['Passed'], 'summary': summary}])
    result = execute(dotnet_repo)
    assert not result['ok'] and result['returncode'] == 0
    assert result['executed'] == result['passed'] == 1 and result['failed'] == 0
    assert result['modules']['auth.tests']['executed'] == 1
    assert result['stages'][-1]['status'] == 'fail' and summary in result['reason']


@pytest.mark.parametrize('reports', [[], [{'raw': '<broken'}]])
def test_nonzero_test_exit_with_missing_or_malformed_evidence_stays_failed(dotnet_repo, reports):
    configure(dotnet_repo, test_exit=9, reports=reports)
    result = execute(dotnet_repo)
    assert not result['ok'] and result['returncode'] == 9
    assert result['stages'][-1]['status'] == 'fail' and 'test exited 9' in result['reason']


# Minimized from the pinned MTP auth/Cowork report structure. Paths are sanitized
# and all XML is embedded: no backup checkout or machine-specific fixture input.
# Keep Results before TestDefinitions, GUID references, Execution children and
# adapter metadata. Definition order differs from result order to exercise lookup.
_PINNED_MTP_IDENTITY_TRX = """<?xml version="1.0" encoding="utf-8"?>
<TestRun id="811007f0-05f2-40a7-a61a-30fa83b75e43" xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
  <Results>
    <UnitTestResult executionId="4a530c06-65e2-4555-ad70-4b3fffc8d9bd" testId="60d9cd7c-3de3-e42b-6be7-f8fd9c019478" testName="Autonogrammer.Web.Tests.AuthTests.AuthCase" testType="13CDC9D9-DDB5-4fa4-A97D-D965CCFC6D4B" outcome="Passed" testListId="8C84FA94-04C1-424b-9868-57A2D4851A1D" relativeResultsDirectory="4a530c06-65e2-4555-ad70-4b3fffc8d9bd" />
    <UnitTestResult executionId="8cdc4f5c-c2d4-4421-bd2a-4c0758e8c509" testId="90c0f9dc-2318-3c4d-6e3c-03ac11ec2785" testName="Autonogrammer.Web.Tests.CoworkTests.CoworkCase" testType="13CDC9D9-DDB5-4fa4-A97D-D965CCFC6D4B" outcome="Passed" testListId="8C84FA94-04C1-424b-9868-57A2D4851A1D" relativeResultsDirectory="8cdc4f5c-c2d4-4421-bd2a-4c0758e8c509" />
  </Results>
  <TestDefinitions>
    <UnitTest name="Autonogrammer.Web.Tests.CoworkTests.CoworkCase" storage="/work/tests/autonogrammer.web.tests/bin/release/net10.0/autonogrammer.web.tests.dll" id="90c0f9dc-2318-3c4d-6e3c-03ac11ec2785">
      <Execution id="8cdc4f5c-c2d4-4421-bd2a-4c0758e8c509" />
      <TestMethod codeBase="/work/tests/Autonogrammer.Web.Tests/bin/Release/net10.0/Autonogrammer.Web.Tests.dll" adapterTypeName="executor://30ea7c6e-dd24-4152-a360-1387158cd41d/4.0.2" className="Autonogrammer.Web.Tests.CoworkTests" name="CoworkCase" />
    </UnitTest>
    <UnitTest name="Autonogrammer.Web.Tests.AuthTests.AuthCase" storage="/work/tests/autonogrammer.web.tests/bin/release/net10.0/autonogrammer.web.tests.dll" id="60d9cd7c-3de3-e42b-6be7-f8fd9c019478">
      <Execution id="4a530c06-65e2-4555-ad70-4b3fffc8d9bd" />
      <TestMethod codeBase="/work/tests/Autonogrammer.Web.Tests/bin/Release/net10.0/Autonogrammer.Web.Tests.dll" adapterTypeName="executor://30ea7c6e-dd24-4152-a360-1387158cd41d/4.0.2" className="Autonogrammer.Web.Tests.AuthTests" name="AuthCase" />
    </UnitTest>
  </TestDefinitions>
  <TestEntries>
    <TestEntry testId="60d9cd7c-3de3-e42b-6be7-f8fd9c019478" executionId="4a530c06-65e2-4555-ad70-4b3fffc8d9bd" testListId="8C84FA94-04C1-424b-9868-57A2D4851A1D" />
    <TestEntry testId="90c0f9dc-2318-3c4d-6e3c-03ac11ec2785" executionId="8cdc4f5c-c2d4-4421-bd2a-4c0758e8c509" testListId="8C84FA94-04C1-424b-9868-57A2D4851A1D" />
  </TestEntries>
  <TestLists>
    <TestList name="Results Not in a List" id="8C84FA94-04C1-424b-9868-57A2D4851A1D" />
    <TestList name="All Loaded Results" id="19431567-8539-422a-85d7-44ee4e166bda" />
  </TestLists>
  <ResultSummary outcome="Completed">
    <Counters total="2" executed="2" passed="2" failed="0" error="0" timeout="0" aborted="0" inconclusive="0" passedButRunAborted="0" notRunnable="0" notExecuted="0" disconnected="0" warning="0" completed="0" inProgress="0" pending="0" />
  </ResultSummary>
</TestRun>
"""


@pytest.mark.parametrize('required', ['Autonogrammer.Web.Tests', 'AUTONOGRAMMER.WEB.TESTS',
                                    'autonogrammer.web.tests'])
def test_pinned_mtp_identity_shape_and_required_names(tmp_path, required):
    report = tmp_path / 'identity.trx'
    report.write_text(_PINNED_MTP_IDENTITY_TRX, encoding='utf-8')
    result = dotnet.parse_trx(tmp_path, [required])
    assert result['executed'] == result['passed'] == result['total'] == 2
    assert result['failed'] == 0
    assert result['modules'] == {'autonogrammer.web.tests':
                                 {'total': 2, 'executed': 2, 'passed': 2, 'failed': 0, 'skipped': 0}}
    assert report.read_text(encoding='utf-8') == _PINNED_MTP_IDENTITY_TRX


@pytest.mark.parametrize('source', ['storage', 'codeBase'])
def test_pinned_mtp_one_absent_identity_source_retains_supported_fallback(tmp_path, source):
    from xml.etree import ElementTree as ET

    root = ET.fromstring(_PINNED_MTP_IDENTITY_TRX)
    ns = '{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}'
    unit = root.find(f'{ns}TestDefinitions/{ns}UnitTest')
    owner = unit if source == 'storage' else unit.find(ns + 'TestMethod')
    del owner.attrib[source]
    ET.ElementTree(root).write(tmp_path / 'identity.trx', encoding='utf-8')
    assert dotnet.parse_trx(tmp_path, ['Autonogrammer.Web.Tests'])['executed'] == 2


def test_pinned_mtp_windows_paths_and_executable_extension(tmp_path):
    from xml.etree import ElementTree as ET

    root = ET.fromstring(_PINNED_MTP_IDENTITY_TRX)
    ns = '{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}'
    for unit in root.findall(f'{ns}TestDefinitions/{ns}UnitTest'):
        unit.set('storage', r'C:\work\tests\autonogrammer.web.tests\bin\release\net10.0\autonogrammer.web.tests.exe')
        unit.find(ns + 'TestMethod').set(
            'codeBase', r'C:\work\tests\Autonogrammer.Web.Tests\bin\Release\net10.0\Autonogrammer.Web.Tests.EXE')
    ET.ElementTree(root).write(tmp_path / 'identity.trx', encoding='utf-8')
    assert dotnet.parse_trx(tmp_path, ['AUTONOGRAMMER.WEB.TESTS'])['executed'] == 2


@pytest.mark.parametrize('mutation,diagnostic', [
    ('conflict', 'conflicting test definition assembly identities'),
    ('empty-storage', 'assembly'),
    ('empty-codebase', 'assembly'),
    ('empty-storage-stem', 'assembly stem'),
    ('empty-codebase-stem', 'assembly stem'),
    ('missing-both', 'missing id or assembly identity'),
    ('empty-definition-id', 'missing id or assembly identity'),
    ('duplicate-definition', 'duplicate test definition id'),
    ('duplicate-execution', 'duplicate execution'),
    ('counter-mismatch', 'counter executed contradicts actual outcomes'),
])
def test_pinned_mtp_invalid_identity_and_reference_cases(tmp_path, mutation, diagnostic):
    from xml.etree import ElementTree as ET

    root = ET.fromstring(_PINNED_MTP_IDENTITY_TRX)
    ns = '{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}'
    definitions = root.find(ns + 'TestDefinitions')
    unit = definitions[0]
    method = unit.find(ns + 'TestMethod')
    if mutation == 'conflict':
        method.set('codeBase', '/work/tests/Other.Tests/bin/Release/net10.0/Other.Tests.dll')
    elif mutation == 'empty-storage':
        unit.set('storage', '')
    elif mutation == 'empty-codebase':
        method.set('codeBase', '')
    elif mutation == 'empty-storage-stem':
        unit.set('storage', '/work/tests/.dll')
    elif mutation == 'empty-codebase-stem':
        method.set('codeBase', r'C:\work\tests\.exe')
    elif mutation == 'missing-both':
        del unit.attrib['storage']
        del method.attrib['codeBase']
    elif mutation == 'empty-definition-id':
        unit.set('id', '')
    elif mutation == 'duplicate-definition':
        duplicate = ET.fromstring(ET.tostring(unit))
        duplicate.set('storage', '/work/tests/AUTONOGRAMMER.WEB.TESTS.DLL')
        duplicate.find(ns + 'TestMethod').set('codeBase', '/work/tests/autonogrammer.web.tests.dll')
        definitions.append(duplicate)
    elif mutation == 'duplicate-execution':
        results = root.find(ns + 'Results')
        results[1].set('executionId', results[0].get('executionId'))
    else:
        root.find(f'{ns}ResultSummary/{ns}Counters').set('executed', '1')
    ET.ElementTree(root).write(tmp_path / 'identity.trx', encoding='utf-8')
    with pytest.raises(ValueError, match=diagnostic):
        dotnet.parse_trx(tmp_path, ['Autonogrammer.Web.Tests'])


@pytest.mark.parametrize('required', ['', ' ', '\t', '/work/Autonogrammer.Web.Tests'])
def test_pinned_mtp_invalid_required_identity_is_not_normalized_away(tmp_path, required):
    (tmp_path / 'identity.trx').write_text(_PINNED_MTP_IDENTITY_TRX, encoding='utf-8')
    with pytest.raises(ValueError, match='assembly stem'):
        dotnet.parse_trx(tmp_path, [required])


def test_pinned_mtp_counts_follow_guid_module_association_not_xml_order(tmp_path):
    from xml.etree import ElementTree as ET

    root = ET.fromstring(_PINNED_MTP_IDENTITY_TRX)
    ns = '{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}'
    result = root.find(ns + 'Results')[1]
    result.set('outcome', 'NotExecuted')
    # This result's definition comes first, whereas its result comes second.
    unit = next(unit for unit in root.findall(f'{ns}TestDefinitions/{ns}UnitTest')
                if unit.get('id') == result.get('testId'))
    unit.set('storage', '/work/tests/companion.tests/bin/release/net10.0/companion.tests.dll')
    unit.find(ns + 'TestMethod').set(
        'codeBase', '/work/tests/Companion.Tests/bin/Release/net10.0/Companion.Tests.dll')
    counters = root.find(f'{ns}ResultSummary/{ns}Counters')
    counters.set('executed', '1')
    counters.set('passed', '1')
    counters.set('notExecuted', '1')
    ET.ElementTree(root).write(tmp_path / 'identity.trx', encoding='utf-8')
    parsed = dotnet.parse_trx(tmp_path, ['Autonogrammer.Web.Tests'])
    assert parsed['executed'] == parsed['passed'] == 1
    assert parsed['modules']['autonogrammer.web.tests']['executed'] == 1
    assert parsed['modules']['companion.tests']['executed'] == 0
    assert parsed['modules']['companion.tests']['skipped'] == 1
    with pytest.raises(ValueError, match='required test module Companion.Tests has no executed tests'):
        dotnet.parse_trx(tmp_path, ['Companion.Tests'])
