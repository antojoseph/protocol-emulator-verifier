from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import uuid
import zipfile

MAX_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE = MAX_BYTES + 256 * 1024
ROOT = Path(__file__).resolve().parents[1]


def unpack(blob, destination=None):
    """Parse bounded regular files; never let ZIP extraction select filesystem paths."""
    if len(blob) > MAX_ARCHIVE:
        raise ValueError('Archive exceeds size limit')
    files = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= 256:
            raise ValueError('Expected 1..256 regular files')
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            mode = entry.external_attr >> 16
            if (not name or len(name) > 240 or '\\' in name or ':' in name
                    or any(ord(c) < 32 for c in name) or path.is_absolute()
                    or path.as_posix() != name
                    or any(p in ('.', '..', '__pycache__') or p.startswith('.') for p in path.parts)
                    or entry.is_dir() or name.endswith('.pyc') or name in files
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError('Unsafe or duplicate archive path')
            total += entry.file_size
            if total > MAX_BYTES or entry.file_size < 0:
                raise ValueError('Expanded archive exceeds size limit')
            with archive.open(entry) as stream:
                content = stream.read(entry.file_size + 1)
            if len(content) != entry.file_size:
                raise ValueError('Archive size mismatch')
            files[name] = content
    if 'candidate.json' not in files:
        raise ValueError('candidate.json must be at the archive root')
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if destination is not None:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        for name, data in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o644)
    return identity, hashes


def verifier_identity():
    from verifier.cli import harness_hashes
    hashes = harness_hashes()
    hashes['verify'] = hashlib.sha256((ROOT / 'verify').read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def classify(result, mode, returncode, hashes, seed):
    """Only the trusted verifier's complete, consistent report can be accepted."""
    if not isinstance(result, dict) or result.get('mode') != mode or result.get('seed') != seed:
        raise ValueError('Missing or mismatched verifier report')
    if result.get('status') == 'blocked' and returncode == 2:
        if result.get('accepted') is not False or result.get('score') is not None:
            raise ValueError('Inconsistent blocked report')
        return 'failed'
    if result.get('status') == 'fail' and returncode == 1:
        if result.get('accepted') is not False or result.get('score') is not None:
            raise ValueError('Inconsistent rejection report')
        return 'completed'
    if result.get('status') != 'pass' or returncode != 0:
        raise ValueError('Inconsistent verifier exit/report')
    if result.get('candidate', {}).get('source_sha256') != hashes:
        raise ValueError('Verifier checked different candidate bytes')
    from verifier.cli import harness_hashes
    if result.get('harness_sha256') != harness_hashes():
        raise ValueError('Verifier report has different harness hashes')
    checks = {c.get('name'): c.get('status') for c in result.get('checks', [])}
    if any(checks.get(k) != 'pass' for k in ('source_license_manifest','source_and_harness_unchanged')):
        raise ValueError('Missing final integrity checks')
    required = ('synthesis', 'functional', 'physical', 'gates') if mode == 'full' else ('synthesis', 'functional')
    if any(result.get('stages', {}).get(k, {}).get('status') != 'pass' for k in required):
        raise ValueError('Incomplete verification stages')
    if mode == 'full':
        score = result.get('score')
        area = result['stages']['physical'].get('metrics', {}).get('stdcell_area_um2')
        if (result.get('accepted') is not True or isinstance(score, bool)
                or not isinstance(score, (int, float)) or not math.isfinite(score) or score <= 0
                or isinstance(area, bool) or not isinstance(area, (int, float))
                or not math.isfinite(area) or area <= 0 or score != 1_000_000 / area):
            raise ValueError('Invalid acceptance or physical score')
    elif result.get('accepted') is not False or result.get('score') is not None:
        raise ValueError('Fast verification cannot accept a candidate')
    return 'completed'


class Store:
    def __init__(self, dsn):
        self.dsn = dsn

    def connection(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10)

    def initialize(self):
        with self.connection() as db:
            db.execute((ROOT / 'service/schema.sql').read_text())

    def submit(self, blob, mode, key):
        if mode not in ('fast', 'full') or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key):
            raise ValueError('Invalid mode or idempotency key')
        source_hash, _ = unpack(blob)
        verifier_hash = verifier_identity()
        with self.connection() as db:
            # Serialize admission, never the long-running verification itself.
            db.execute('SELECT pg_advisory_xact_lock(17092801)')
            existing = db.execute('SELECT * FROM asic_jobs WHERE request_key=%s', (key,)).fetchone()
            if existing:
                if (existing['source_hash'], existing['mode'], existing['verifier_hash']) != (source_hash, mode, verifier_hash):
                    raise ValueError('Idempotency key already names different inputs')
                return str(existing['id'])
            count = db.execute("SELECT count(*) AS n FROM asic_jobs WHERE state IN ('queued','running')").fetchone()['n']
            if count >= 100:
                raise OverflowError('Queue is full')
            job = str(uuid.uuid4())
            db.execute('''INSERT INTO asic_jobs
                (id, request_key, source_hash, verifier_hash, mode, seed, archive)
                VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (job, key, source_hash, verifier_hash, mode, secrets.randbits(32), blob))
            return job

    def get(self, job):
        job = str(uuid.UUID(job))
        with self.connection() as db:
            return db.execute('''SELECT id, source_hash, verifier_hash, mode, state,
                cancel_requested, attempt, created_at, updated_at, result, artifact, error
                FROM asic_jobs WHERE id=%s''', (job,)).fetchone()

    def claim(self, mode, lease_seconds=180):
        with self.connection() as db:
            # Bounded infrastructure retries; stale workers are fenced by lease_token.
            db.execute("""UPDATE asic_jobs SET state=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                lease_token=NULL, error='Worker lease expired after maximum attempts', updated_at=now()
                WHERE state='running' AND lease_until < now() AND (attempt>=3 OR cancel_requested)""")
            job = db.execute("""SELECT * FROM asic_jobs WHERE mode=%s
                AND NOT cancel_requested AND (state='queued' OR (state='running' AND lease_until<now()))
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""", (mode,)).fetchone()
            if not job:
                return None
            return db.execute("""UPDATE asic_jobs SET state='running', attempt=attempt+1,
                lease_token=%s, lease_until=now()+(%s * interval '1 second'), updated_at=now()
                WHERE id=%s RETURNING *""", (str(uuid.uuid4()), lease_seconds, job['id'])).fetchone()

    def heartbeat(self, job, token, lease_seconds=180):
        with self.connection() as db:
            row = db.execute("""UPDATE asic_jobs SET lease_until=now()+(%s * interval '1 second'), updated_at=now()
                WHERE id=%s AND lease_token=%s AND state='running' AND lease_until>now()
                RETURNING cancel_requested""", (lease_seconds, job, token)).fetchone()
            return None if row is None else row['cancel_requested']

    def cancel(self, job):
        with self.connection() as db:
            row = db.execute("""UPDATE asic_jobs SET cancel_requested=true,
                state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END, updated_at=now()
                WHERE id=%s AND state IN ('queued','running') RETURNING id""", (str(uuid.UUID(job)),)).fetchone()
            return row is not None

    def finish(self, job, token, state, result=None, artifact=None, error=None):
        from psycopg.types.json import Jsonb
        if state not in ('completed', 'failed', 'cancelled'):
            raise ValueError('Invalid terminal state')
        with self.connection() as db:
            # Cancellation races never publish acceptance; artifacts remain attempt-scoped.
            row = db.execute("""UPDATE asic_jobs SET
                state=CASE WHEN cancel_requested THEN 'cancelled' ELSE %s END,
                result=CASE WHEN cancel_requested THEN NULL ELSE %s::jsonb END,
                artifact=%s, error=%s, lease_token=NULL, lease_until=NULL, updated_at=now()
                WHERE id=%s AND lease_token=%s AND state='running' AND lease_until>now() RETURNING id""",
                (state, Jsonb(result) if result is not None else None,
                 Jsonb(artifact) if artifact is not None else None, error, job, token)).fetchone()
            return row is not None
