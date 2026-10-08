"""Optional local layout diagnosis; no credential discovery or execution."""
import argparse
import json

from assistant_mesh.config import private_json
from assistant_mesh.runtime_health import diagnose


def main():
    parser = argparse.ArgumentParser(description='Read-only node runtime layout observations')
    parser.add_argument('--config', required=True, help='owned private worker configuration handle')
    args = parser.parse_args()
    try:
        report = diagnose(private_json(args.config))
    except Exception:
        # Do not leak JSON fragments, credential-bearing paths or exceptions.
        report = {'schema': 'runtime-health/1', 'read_only': True,
                  'error': 'runtime_doctor_configuration_unavailable'}
    print(json.dumps(report, ensure_ascii=False))
    if report.get('error'):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
