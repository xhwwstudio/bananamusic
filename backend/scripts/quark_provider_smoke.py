from __future__ import annotations

import json
import sys

from app.services.storage.providers_quark import QuarkStorageProvider
from app.services.storage.types import StorageRef


def main() -> int:
    if len(sys.argv) < 3:
        print('usage: python quark_provider_smoke.py <cookie> <root_dir_id> [root_path] [test_path]')
        return 2

    cookie = sys.argv[1]
    root_dir_id = sys.argv[2]
    root_path = sys.argv[3] if len(sys.argv) > 3 else '/'
    test_path = sys.argv[4] if len(sys.argv) > 4 else '/'

    provider = QuarkStorageProvider(
        'quark-main',
        cookie=cookie,
        root_dir_id=root_dir_id,
        root_path=root_path,
    )

    out: dict[str, object] = {
        'provider_type': provider.provider_type,
        'root_dir_id': root_dir_id,
        'root_path': root_path,
        'test_path': test_path,
    }

    try:
        items = provider.list_dir(StorageRef(provider_id='quark-main', path=test_path))
        out['list_dir_ok'] = True
        out['list_dir_count'] = len(items)
        out['first_items'] = [
            {
                'name': item.get('name'),
                'is_dir': item.get('is_dir'),
                'path': item.get('path'),
                'object_id': item.get('object_id'),
            }
            for item in items[:10]
        ]
    except Exception as e:
        out['list_dir_ok'] = False
        out['list_dir_error'] = str(e)

    try:
        stat = provider.stat(StorageRef(provider_id='quark-main', path=test_path))
        out['stat'] = None if stat is None else {
            'name': stat.name,
            'path': stat.path,
            'is_dir': stat.is_dir,
            'size': stat.size,
            'object_id': stat.object_id,
        }
    except Exception as e:
        out['stat_error'] = str(e)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
