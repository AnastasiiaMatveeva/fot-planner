"""Числовой аудит JSON-кандидата в отдельном процессе; без сервиса и оптимизации."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fot_planner.harness_local.candidate_audit import evaluate_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--workbook')
    parser.add_argument('--input-sha256')
    args = parser.parse_args()
    try:
        result = evaluate_bytes(Path(args.bundle).read_bytes(), args.sha256,
                                input_sha256=args.input_sha256,
                                workbook_bytes=Path(args.workbook).read_bytes() if args.workbook else None)
        text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        # В том числе ошибка самого обработчика: процесс обязан вернуть error,
        # а не молчаливый успех. Не печатаем входные значения и персональные данные.
        result = {'schema': 1, 'bundle_sha256': args.sha256, 'verdict': 'error',
                  'acceptance': 'not_run', 'checks': [], 'error_type': type(exc).__name__}
        text = json.dumps(result, ensure_ascii=False)
    print(text)
    return {'passed': 0, 'inconclusive': 2}.get(result['verdict'], 1)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
