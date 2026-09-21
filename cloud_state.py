"""Durable, encrypted, content-addressed state on the raw-data Git branch.

Only explicitly allowed data is transferred. Cookies and credentials never are.
Unchanged files reuse encrypted objects; old Git generations remain recoverable.
"""
import argparse
import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import ci_state
from refresh_store import json_write

ROOT = Path(__file__).resolve().parent
EXTRA = {
    'cache/issue-input.json',
    'cache/growth/verified_document_ledger.json',
    'cache/growth/verified_document_status.json',
    'cache/growth/verified_documents_input.json',
    'cache/youtube/content_ledger.json',
    'cache/youtube/content_status.json',
    'cache/youtube/verified_transcripts_input.json',
}
CHUNK = 16 * 1024 * 1024


def key_from_env():
    key = base64.b64decode(os.environ['RAW_STATE_KEY'], validate=True)
    if len(key) != 32:
        raise ValueError('RAW_STATE_KEY must encode exactly 32 random bytes')
    return key


def seal(data, key, label):
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, gzip.compress(data, mtime=0), label.encode())


def unseal(data, key, label):
    return gzip.decompress(AESGCM(key).decrypt(data[:12], data[12:], label.encode()))


def put_object(directory, data, key):
    identity = hmac.new(key, data, hashlib.sha256).hexdigest()
    target = directory/'objects'/f'{identity}.bin'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if unseal(target.read_bytes(), key, identity) != data:
            raise ValueError('Existing state object failed verification')
    else:
        target.write_bytes(seal(data, key, identity))
    return identity


def read_object(directory, identity, key):
    if not re.fullmatch('[0-9a-f]{64}', identity):
        raise ValueError('Invalid object identifier')
    path = directory/'objects'/f'{identity}.bin'
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError('State object escapes directory')
    raw = unseal(path.read_bytes(), key, identity)
    if hmac.new(key, raw, hashlib.sha256).hexdigest() != identity:
        raise ValueError('State content hash mismatch')
    return raw


def save(root, directory, key):
    root, directory = Path(root).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    entries = {}
    def add(name, raw):
        entries[name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                         'objects': [put_object(directory, raw[i:i+CHUNK], key)
                                     for i in range(0, len(raw), CHUNK)]}
    with tempfile.TemporaryDirectory(prefix='cloud-state-') as temp:
        archive = Path(temp)/'state.tar.gz'
        manifest = ci_state.pack(root, archive)
        with tarfile.open(archive, 'r:gz') as packed:
            for member in packed.getmembers():
                add(member.name, packed.extractfile(member).read())
    for name in sorted(EXTRA):
        path = ci_state._safe_target(root, name)
        if path.is_file():
            ci_state._check_secret_values(path)
            raw = path.read_bytes()
            json.loads(raw.decode('utf-8-sig'))
            add(name, raw)
    payload = {'schemaVersion': 1, 'snapshotId': manifest['snapshotId'],
               'sourceCutoff': manifest['sourceCutoff'], 'files': entries}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    pointer = directory/'state.enc'
    if not pointer.exists() or unseal(pointer.read_bytes(), key, 'manifest') != raw:
        pending = directory/'state.pending'
        pending.write_bytes(seal(raw, key, 'manifest'))
        os.replace(pending, pointer)
    return {'snapshotId': manifest['snapshotId'], 'sourceCutoff': manifest['sourceCutoff'],
            'files': len(entries), 'encryptedBytes': sum(p.stat().st_size for p in directory.rglob('*.bin'))}


def restore(root, directory, key):
    root, directory = Path(root).resolve(), Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    payload = json.loads(unseal((directory/'state.enc').read_bytes(), key, 'manifest'))
    if payload.get('schemaVersion') != 1 or not isinstance(payload.get('files'), dict):
        raise ValueError('Unknown state format')
    with tempfile.TemporaryDirectory(prefix='cloud-restore-', dir=root) as temp:
        staging, archive = Path(temp)/'staging', Path(temp)/'state.tar.gz'
        staging.mkdir()
        with tarfile.open(archive, 'w:gz') as packed:
            for name, entry in payload['files'].items():
                if name != ci_state.ARCHIVE_MANIFEST and name not in EXTRA and not ci_state.allowed(name):
                    raise ValueError('Unapproved state path')
                # Validate all target paths before writing any live data.
                ci_state._safe_target(root, name)
                raw = b''.join(read_object(directory, ident, key) for ident in entry['objects'])
                if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                    raise ValueError('State file checksum mismatch')
                if name in EXTRA:
                    json.loads(raw.decode('utf-8-sig'))
                    target = ci_state._safe_target(staging, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                else:
                    member = tarfile.TarInfo(name)
                    member.size = len(raw)
                    packed.addfile(member, io.BytesIO(raw))
        # The existing graph/manifest validator also runs before live mutation.
        verified = ci_state.restore(staging, archive)
        if verified['snapshotId'] != payload['snapshotId']:
            raise ValueError('Snapshot identity mismatch')
        paths = [p for p in staging.rglob('*') if p.is_file()]
        for path in paths:
            ci_state._check_secret_values(path)
        for path in sorted(paths, key=lambda p: p.name == 'verified_snapshot.json'):
            target = ci_state._safe_target(root, path.relative_to(staging).as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
    return {'snapshotId': verified['snapshotId'], 'sourceCutoff': verified['sourceCutoff'],
            'files': len(payload['files'])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['save', 'restore'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    result = (save if args.mode == 'save' else restore)(args.root, args.state_dir, key_from_env())
    print(json.dumps(result))


if __name__ == '__main__':
    main()
