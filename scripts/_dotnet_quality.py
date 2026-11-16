"""Internal .NET executor for qa_gate (not a separate gate or CLI).

Uses the repository-selected SDK and Microsoft.Testing.Platform runner. Locked
restore and warning-as-error build preserve configured analyzers; nothing installs
SDKs, changes feeds or suppresses warnings. Each invocation retains its own fresh
TRX directory for evidence. Required module names are assembly stems, matched from
TRX TestDefinitions storage/codeBase, never guessed from report filenames. CLR
assembly identity is case-insensitive: a shared validated, nonempty assembly-stem
key casefolds both sources and required names. Per-module result keys use the same
normalized stems; raw report attributes are not rewritten.

The timeout is a single monotonic budget across restore/build/test. POSIX children
own a new process group; scoped INT/TERM/HUP handlers stop that group only. TERM
is followed by bounded KILL/reaping even when the leader exits first. No global
build-server shutdown or process-name matching is used.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET


_SUFFIXES = {'.sln', '.slnx', '.slnf', '.csproj', '.fsproj', '.vbproj'}
_SKIP = {'.git', '.swarm', 'node_modules', 'bin', 'obj', '.venv', 'venv'}
_NS = '{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}'
_COUNTS = ('total', 'executed', 'passed', 'failed', 'skipped')
_OUTCOMES = {'Passed', 'Failed', 'Error', 'Timeout', 'Aborted', 'Inconclusive',
             'PassedButRunAborted', 'NotExecuted', 'NotRunnable'}
_SKIPPED = {'NotExecuted', 'NotRunnable'}


class TrxFailure(ValueError):
    """A valid report set with failing execution; retain its measured counts."""

    def __init__(self, reason: str, evidence: dict):
        super().__init__(reason)
        self.evidence = evidence


def project_files(root: Path) -> list[Path]:
    """Find workload evidence without requiring an installed dotnet executable."""
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in _SKIP and not Path(directory, name).is_symlink())
        found.extend(Path(directory, name) for name in sorted(files) if Path(name).suffix.lower() in _SUFFIXES)
    return found


def _target(root: Path, target: str | None) -> str:
    if target:
        selected = (root / target).resolve()
        if not selected.is_relative_to(root.resolve()) or not selected.is_file() or selected.suffix.lower() not in _SUFFIXES:
            raise ValueError('dotnet target must be an existing project/solution inside the repository')
        return str(selected.relative_to(root.resolve()))
    files = project_files(root)
    solutions = [path for path in files if path.parent == root and path.suffix.lower() in {'.sln', '.slnx', '.slnf'}]
    candidates = solutions or files
    if len(candidates) != 1:
        raise ValueError('select --dotnet-target explicitly: repository has no unique project/solution target')
    return str(candidates[0].relative_to(root))


def _assembly_stem_key(stem: str) -> str:
    """Compare assembly stems without accepting empty identities or path input."""
    if not isinstance(stem, str) or not stem or stem != stem.strip() or '/' in stem or '\\' in stem:
        raise ValueError('assembly stem must be nonempty, without surrounding whitespace or paths')
    return stem.casefold()


def _module(storage: str) -> str:
    # TRX can contain Windows paths even when parsed on Linux.
    name = storage.replace('\\', '/').rsplit('/', 1)[-1]
    if not name.lower().endswith(('.dll', '.exe')):
        raise ValueError('TRX test definition has no assembly storage/codeBase identity')
    return _assembly_stem_key(name.rsplit('.', 1)[0])


def _zero() -> dict:
    return dict.fromkeys(_COUNTS, 0)


def parse_trx(directory: Path, required_modules: list[str]) -> dict:
    """Validate actual TRX outcomes against counters and require per-module execution.

    Only files inside the invocation-owned directory count. All reports must be
    valid, including reports unrelated to the required modules. Missing identity,
    unknown outcomes, inconsistent counters and run-level abort/error fail closed.
    """
    required = [(name, _assembly_stem_key(name)) for name in required_modules]
    reports = sorted(directory.rglob('*.trx'))
    result = {**_zero(), 'modules': {}, 'reports': [str(path) for path in reports]}
    if not reports:
        raise ValueError('no fresh TRX reports produced')
    failures = []
    for path in reports:
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('TRX report escapes invocation-owned directory')
        tree = ET.parse(path).getroot()
        if tree.tag != _NS + 'TestRun':
            raise ValueError('TRX requires a namespaced TestRun')
        summary = tree.find(_NS + 'ResultSummary')
        counters = summary.find(_NS + 'Counters') if summary is not None else None
        if summary is None or counters is None or summary.get('outcome') not in _OUTCOMES | {'Completed'}:
            raise ValueError('TRX missing or invalid ResultSummary/Counters')
        if summary.get('outcome') not in {'Completed', 'Passed'}:
            failures.append(f"TRX ResultSummary outcome {summary.get('outcome')}")
        if tree.find(f'.//{_NS}ErrorInfo') is not None or any(
                info.get('outcome') in {'Error', 'Failed', 'Aborted', 'Timeout'}
                for info in tree.findall(f'.//{_NS}RunInfo')):
            failures.append('TRX contains run-level errors')
        values = {}
        for key, value in counters.attrib.items():
            if not re.fullmatch(r'[0-9]+', value):
                raise ValueError(f'TRX counter {key} is not a nonnegative integer')
            values[key] = int(value)
        if any(key not in values for key in ('total', 'executed', 'passed', 'failed')):
            raise ValueError('TRX missing required counters')
        definitions = {}
        for unit in tree.findall(f'{_NS}TestDefinitions/{_NS}UnitTest'):
            method = unit.find(_NS + 'TestMethod')
            identities = [value for value in (unit.get('storage'), method.get('codeBase') if method is not None else None)
                          if value is not None]
            test_id = unit.get('id')
            if not identities or not test_id:
                raise ValueError('TRX test definition missing id or assembly identity')
            if test_id in definitions:
                raise ValueError(f'TRX duplicate test definition id {test_id}')
            modules = {_module(value) for value in identities}
            if len(modules) != 1:
                raise ValueError(f'TRX conflicting test definition assembly identities for id {test_id}: {identities!r}')
            definitions[test_id] = modules.pop()
        actual = _zero()
        outcomes = {}
        seen = set()
        for item in tree.findall(f'{_NS}Results/{_NS}UnitTestResult'):
            outcome = item.get('outcome')
            module = definitions.get(item.get('testId'))
            execution = item.get('executionId')
            if outcome not in _OUTCOMES or module is None or not execution or execution in seen:
                raise ValueError('TRX unknown outcome, missing module or duplicate execution')
            seen.add(execution)
            counts = result['modules'].setdefault(module, _zero())
            delta = {'total': 1, 'executed': int(outcome not in _SKIPPED), 'passed': int(outcome == 'Passed'),
                     'failed': int(outcome not in _SKIPPED and outcome != 'Passed'), 'skipped': int(outcome in _SKIPPED)}
            for key in _COUNTS:
                counts[key] += delta[key]
                actual[key] += delta[key]
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        expected = {'total': actual['total'], 'executed': actual['executed'], 'passed': actual['passed'],
                    'failed': outcomes.get('Failed', 0)}
        expected.update({key: outcomes.get(outcome, 0) for key, outcome in
                         [('error', 'Error'), ('timeout', 'Timeout'), ('aborted', 'Aborted'),
                          ('inconclusive', 'Inconclusive'), ('passedButRunAborted', 'PassedButRunAborted'),
                          ('notExecuted', 'NotExecuted'), ('notRunnable', 'NotRunnable')]})
        for key, value in values.items():
            if value != expected.get(key, 0):
                raise ValueError(f'TRX counter {key} contradicts actual outcomes')
        if actual['failed'] or actual['executed'] == 0:
            failures.append('TRX contains failed outcomes or zero executed tests')
        for key in _COUNTS:
            result[key] += actual[key]
    if result['executed'] == 0:
        failures.append('TRX aggregate has zero executed tests')
    for module, key in required:
        if result['modules'].get(key, {}).get('executed', 0) == 0:
            failures.append(f'required test module {module} has no executed tests')
    if failures:
        raise TrxFailure('; '.join(failures), result)
    return result


@contextmanager
def _cancellation():
    """Keep a cancellation latched across stages and restore all prior handlers."""
    stopped = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        def stop(signum, frame):
            stopped.set()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, stop)
    try:
        yield stopped
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _reap(proc: subprocess.Popen, grace: float) -> None:
    """End only our session; leader exit does not imply its descendants exited."""
    deadline = time.monotonic() + grace
    _kill_group(proc.pid, signal.SIGTERM)
    while _group_alive(proc.pid) and time.monotonic() < deadline:
        proc.poll()
        time.sleep(.02)
    if _group_alive(proc.pid):
        _kill_group(proc.pid, signal.SIGKILL)
    proc.wait(timeout=2)
    # SIGKILL delivery is asynchronous. Give descendants time to exit and init
    # time to reap orphans, without mistaking the leader's wait for group cleanup.
    settle = time.monotonic() + 2
    while _group_alive(proc.pid) and time.monotonic() < settle:
        time.sleep(.02)


def _stage(name: str, argv: list[str], root: Path, deadline: float, stopped: threading.Event, grace: float) -> dict:
    result = {'name': name, 'argv': argv, 'returncode': None, 'status': 'error', 'reason': '', 'tail': ''}
    if stopped.is_set() or time.monotonic() >= deadline:
        result.update(status='cancelled' if stopped.is_set() else 'timeout', reason='runner cancelled' if stopped.is_set() else 'runner deadline exhausted')
        return result
    proc = None
    # File-backed output bounds memory even for noisy builds, and descendants
    # cannot block communicate by inheriting pipes after the leader has exited.
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                    start_new_session=True)
            while True:
                if stopped.is_set():
                    result.update(status='cancelled', reason='runner cancelled')
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result.update(status='timeout', reason='runner deadline exhausted')
                    break
                try:
                    proc.wait(timeout=min(.05, remaining))
                    result.update(status='pass' if proc.returncode == 0 else 'fail',
                                  reason='' if proc.returncode == 0 else f'{name} exited {proc.returncode}')
                    break
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            stopped.set()
            result.update(status='cancelled', reason='runner interrupted')
        except OSError as exc:
            result.update(reason=f'{type(exc).__name__}: {exc}')
        finally:
            if proc is not None:
                # Also remove owned stragglers after an ordinary leader exit.
                _reap(proc, grace)
                result['returncode'] = proc.returncode
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 1500))
            result['tail'] = output.read().decode('utf-8', errors='replace')
    if stopped.is_set():
        result.update(status='cancelled', reason='runner cancelled')
    return result


def run_dotnet(root: Path, *, target: str | None = None, configuration: str = 'Release',
               runtime_version: str | None = None, required_modules: list[str] | None = None,
               timeout: float = 1800, stop_grace: float = 1) -> dict:
    """Return staged, fail-closed evidence; issue_gate remains qa_gate's authority.

    Only SDK10 MTP reporting is supported. Other runner settings fail explicitly,
    rather than mixing VSTest options into an MTP invocation. Runtime pins are
    supplied by the caller, never discovered by modifying installed SDKs.
    """
    root = root.resolve()
    deadline = time.monotonic() + timeout
    result = {'runner': 'dotnet', 'ok': False, 'stage': 'configuration', 'reason': '', 'returncode': None,
              **_zero(), 'reports': [], 'modules': {}, 'stages': [], 'tail': ''}
    try:
        selected = _target(root, target)
        config = json.loads((root / 'global.json').read_text(encoding='utf-8'))
        if not isinstance(config, dict) or not isinstance(config.get('test'), dict) or config['test'].get('runner') != 'Microsoft.Testing.Platform':
            raise ValueError('dotnet quality requires global.json test.runner=Microsoft.Testing.Platform (SDK10)')
        required_modules = required_modules or []
        for name in required_modules:
            _assembly_stem_key(name)
            if name.casefold().endswith(('.dll', '.exe')):
                raise ValueError('required test modules must be nonempty assembly stems, without paths or extensions')
        directory = Path(tempfile.mkdtemp(prefix='agent-swarm-dotnet-'))
        result['results_directory'] = str(directory)
        properties = ['-p:TargetLatestRuntimePatch=false']
        if runtime_version:
            properties.append(f'-p:RuntimeFrameworkVersion={runtime_version}')
        commands = [
            ('restore', ['dotnet', 'restore', selected, '--locked-mode', *properties]),
            ('build', ['dotnet', 'build', selected, '-c', configuration, '--no-restore', '-warnaserror', *properties]),
            ('test', ['dotnet', 'test', selected, '-c', configuration, '--no-build', '--max-parallel-test-modules', '2',
                      '--report-trx', '--results-directory', str(directory)]),
        ]
        with _cancellation() as stopped:
            for name, argv in commands:
                if result['stages'] and result['stages'][-1]['status'] != 'pass':
                    result['stages'].append({'name': name, 'argv': argv, 'status': 'skipped:dependency',
                                             'returncode': None, 'reason': 'prerequisite failed', 'tail': ''})
                    continue
                stage = _stage(name, argv, root, deadline, stopped, stop_grace)
                result['stages'].append(stage)
                result.update(stage=name, returncode=stage['returncode'], reason=stage['reason'], tail=stage['tail'])
                # A nonzero exit still has useful execution evidence. Parsing
                # reports never clears the subprocess failure/cancel/timeout.
                if name == 'test':
                    try:
                        result['reports'] = [str(path) for path in sorted(directory.rglob('*.trx'))]
                        result.update(parse_trx(directory, required_modules))
                    except (OSError, ValueError, ET.ParseError) as exc:
                        if isinstance(exc, TrxFailure):
                            result.update(exc.evidence)
                        reason = '; '.join(part for part in (stage['reason'], str(exc)) if part)
                        stage.update(status='fail' if stage['status'] == 'pass' else stage['status'], reason=reason)
                        result['reason'] = reason
                if stopped.is_set() or time.monotonic() >= deadline:
                    stage.update(status='cancelled' if stopped.is_set() else 'timeout',
                                 reason='runner cancelled' if stopped.is_set() else 'runner deadline exhausted')
                    result['reason'] = stage['reason']
            result['ok'] = all(stage['status'] == 'pass' for stage in result['stages']) and not stopped.is_set()
    except (OSError, ValueError, TypeError) as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    return result
