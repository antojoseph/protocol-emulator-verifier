"""Run with python3 -m service.client through the documented private SSH tunnel."""
import argparse
import base64
import io
import json
from pathlib import Path
import urllib.request
import uuid
import zipfile

from .core import unpack


def pack(candidate):
    candidate = Path(candidate).resolve()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(candidate.rglob('*')):
            if path.is_symlink():
                raise ValueError('Candidate contains a symlink')
            relative = path.relative_to(candidate)
            if any(p.startswith('.') or p == '__pycache__' for p in relative.parts) or path.suffix == '.pyc':
                continue
            if path.is_file():
                archive.write(path, relative.as_posix())
    blob = output.getvalue()
    unpack(blob)
    return blob


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8123')
    sub = parser.add_subparsers(dest='command', required=True)
    submit = sub.add_parser('submit')
    submit.add_argument('candidate', type=Path)
    submit.add_argument('--mode', choices=('fast','full'), default='fast')
    submit.add_argument('--key', default=None, help='Reuse this key when retrying an uncertain submission')
    for command in ('status', 'cancel'):
        sub.add_parser(command).add_argument('job')
    sub.add_parser('health')
    args = parser.parse_args()
    data = None
    if args.command == 'submit':
        import sys
        key = args.key or str(uuid.uuid4())
        print('Request key (save for retries): ' + key, file=sys.stderr, flush=True)
        data = json.dumps({'archive': base64.b64encode(pack(args.candidate)).decode(),
                           'mode': args.mode, 'request_key': key}).encode()
        path = '/jobs'
    elif args.command == 'health':
        path = '/health'
    else:
        path = '/jobs/' + str(uuid.UUID(args.job))
        if args.command == 'cancel':
            path += '/cancel'
            data = b''
    request = urllib.request.Request(args.url.rstrip('/') + path, data=data,
        headers={'Content-Type': 'application/json', 'X-ASIC-Client': '1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        print(json.dumps(json.load(response), indent=2))


if __name__ == '__main__':
    main()
