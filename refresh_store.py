"""Shared atomic storage; collection, scoring and presentation have no IO dependency on each other."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str, allow_nan=False).encode()).hexdigest()


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + '\n'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
        temp = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text('utf-8-sig')) if path.exists() else default


@contextmanager
def run_lock(directory):
    """A stale lock is reported for operator review, never silently stolen."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / 'refresh.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f'이미 갱신 중이거나 중단된 실행 잠금이 있습니다: {lock}') from exc
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(f'{os.getpid()} {datetime.now(timezone.utc).isoformat()}')
        yield
    finally:
        lock.unlink(missing_ok=True)


def snapshot_files(directory, files, metadata):
    """Keep immutable content-addressed objects; identical cache reuse costs no extra copy."""
    directory = Path(directory)
    objects = directory / 'objects'
    objects.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name, source in files.items():
        source = Path(source)
        with source.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        target = objects / (checksum + source.suffix)
        if not target.exists():
            temporary = objects / (checksum + '.tmp')
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        entries[name] = {'sha256': checksum, 'path': target.relative_to(directory).as_posix(),
                         'bytes': source.stat().st_size}
    identity = digest({'files': entries, 'sourceCutoff': metadata.get('sourceCutoff')})
    manifest = {'snapshotId': identity, 'files': entries, **metadata}
    manifest_path = directory / 'manifests' / f'{identity}.json'
    if not manifest_path.exists():
        json_write(manifest_path, manifest)
    return read_json(manifest_path)


def store_verified_frames(output_dir, cache_dir, prices, fundamentals, report, generated_at):
    """Publish one pointer only after all three immutable objects exist; interruptions cannot mix generations."""
    output_dir, cache_dir = Path(output_dir), Path(cache_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='raw-generation-', dir=output_dir) as temp:
        temp = Path(temp)
        prices.to_pickle(temp/'prices.pkl.gz')
        fundamentals.to_pickle(temp/'fundamentals.pkl.gz')
        json_write(temp/'report.json', report)
        manifest = snapshot_files(cache_dir/'snapshots',
            {'prices': temp/'prices.pkl.gz', 'fundamentals': temp/'fundamentals.pkl.gz', 'report': temp/'report.json'},
            {'sourceCutoff': report.get('latestPriceDate'), 'firstStoredAt': generated_at})
    json_write(output_dir/'verified_snapshot.json', {'snapshotId': manifest['snapshotId']})
    return manifest


def load_verified_frames(output_dir, cache_dir):
    import pandas as pd
    output_dir, cache_dir = Path(output_dir), Path(cache_dir)
    pointer = read_json(output_dir/'verified_snapshot.json')
    if not pointer:
        return (pd.read_pickle(output_dir/'verified_prices.pkl'),
                pd.read_pickle(output_dir/'verified_fundamentals.pkl'),
                read_json(output_dir/'verified_report.json'))
    identity = pointer['snapshotId']
    if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
        raise RuntimeError('검증 스냅샷 식별자가 올바르지 않습니다.')
    root = (cache_dir/'snapshots').resolve()
    manifest = read_json(root/'manifests'/f'{identity}.json')
    paths = {}
    for name in ('prices', 'fundamentals', 'report'):
        item = manifest['files'][name]
        path = (root/item['path'].replace('\\', '/')).resolve()
        if not path.is_relative_to(root/'objects'):
            raise RuntimeError('원자료 스냅샷 경로 오류')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != item['sha256']:
            raise RuntimeError('원자료 스냅샷 무결성 검증 실패')
        paths[name] = path
    return pd.read_pickle(paths['prices']), pd.read_pickle(paths['fundamentals']), read_json(paths['report'])


def public_fields(value):
    if isinstance(value, dict):
        return {k: public_fields(v) for k, v in value.items() if not k.startswith('_')}
    if isinstance(value, list):
        return [public_fields(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)((?:[?&]|\b)(?:crtfc_key|api[_-]?key|appkey|appsecret|access_token|authorization|client_secret)=)[^&\s"<>]+',
                       r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~-]+', r'\1[REDACTED]', value)
        for name in ('DART_API_KEY', 'KIS_APP_KEY', 'KIS_APP_SECRET', 'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET'):
            secret = os.environ.get(name)
            if secret and len(secret) >= 8:
                value = value.replace(secret, '[REDACTED]')
    return value
