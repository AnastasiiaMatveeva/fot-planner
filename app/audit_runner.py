"""Запуск отдельного аудитора по неизменяемым артефактам; без доступа к БД."""
import json
import subprocess
import sys
from pathlib import Path

from fot_planner.harness_local.manifest import canonical_json


def run_audit(store, draft, bundle_path, output_path):
    result = {'verdict': 'error', 'acceptance': 'not_run'}
    try:
        bundle_sha = store.put_file(bundle_path)
        output_sha = store.put_file(output_path)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/audit_candidate.py'),
                   '--bundle', store.path(bundle_sha), '--sha256', bundle_sha,
                   '--workbook', store.path(output_sha), '--input-sha256', draft['input']['sha256']]
        process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=120)
        result = json.loads(process.stdout)
        expected = [c['requirement'] for c in draft['evaluation_plan']['required_checks']]
        if result.get('verdict') == 'passed' and (
                process.returncode != 0 or result.get('bundle_sha256') != bundle_sha
                or result.get('output_sha256') != output_sha
                or result.get('input_sha256') != draft['input']['sha256']
                or (result.get('export_audit') or {}).get('verdict') != 'passed'
                or sorted(result.get('required_checks', [])) != sorted(expected)):
            raise ValueError('аудитор не подтвердил закреплённый состав')
        result['command'] = command
        result['returncode'] = process.returncode
    except Exception as exc:
        result = {'verdict': 'error', 'acceptance': 'not_run', 'error_type': type(exc).__name__}
    result['report_sha256'] = store.put_bytes(canonical_json(result))
    return result
