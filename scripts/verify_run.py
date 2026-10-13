"""Read-only сверка снимка в отдельном процессе; не числовая приёмка.

Коды: 1 — нарушена целостность, 2 — целостность проверена, приёмка не выполнена.
Путь указывает на каталог артефактов, SHA-256 берётся из доверенной записи Run.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fot_planner.harness_local import manifest as M


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', required=True)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    try:
        store = M.ArtifactStore(args.artifacts)
        manifest = M.load_manifest(store, args.manifest)
        problems = M.verify_manifest(store, manifest)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        problems = [str(exc)]
    result = {'schema': 1, 'manifest_sha256': args.manifest,
              'integrity': 'failed' if problems else 'verified',
              'acceptance': 'not_run', 'numerical_audit': 'not_run', 'problems': problems}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if problems else 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
