"""Validated raw-data state. Ninety-day CI artifacts are not a permanent database."""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from refresh_store import digest

ROOT = Path(__file__).resolve().parent
ARCHIVE_MANIFEST = 'raw-state-manifest.json'
TOP_FILES = {'krx_prices.csv.gz', 'krx_market_snapshot.csv', 'krx_listing_desc.csv',
             'dart_fundamentals_cache.csv', 'dart_corp_codes.csv', 'dart_retry_state.json',
             'dart_disclosure_state.json', 'kis_consensus_state.json', 'kis_consensus_history.csv',
             'kis_consensus_cache.csv'}
TOP_FILES |= {'performance_prices.pkl.gz', 'performance_price_status.json'}
GROWTH_FILES = {'event_ledger.json', 'collection_status.json', 'disclosure_index.json',
                'news_hints.json', 'news_status.json', 'news_queue.json', 'news_search_state.json',
                'trade_ledger.json', 'trade_status.json', 'trade_release_cache.json'}
SECRET_ENV = ('DART_API_KEY', 'KIS_APP_KEY', 'KIS_APP_SECRET', 'NAVER_CLIENT_ID',
              'NAVER_CLIENT_SECRET', 'GH_TOKEN', 'GITHUB_TOKEN')


def _parts(relative):
    raw = relative.as_posix() if isinstance(relative, Path) else str(relative).replace('\\', '/')
    parts = raw.split('/')
    if not raw or raw.startswith('/') or ':' in raw or '\x00' in raw or any(p in {'', '.', '..'} for p in parts):
        return ()
    return tuple(parts)


def allowed(relative):
    parts = _parts(relative)
    if len(parts) == 2 and parts[0] == 'cache' and parts[1] in TOP_FILES:
        return True
    if len(parts) == 3 and parts[:2] == ('cache', 'dart_periods'):
        return bool(re.fullmatch(r'20\d{2}_110(?:11|12|13|14)\.json', parts[2]))
    if len(parts) == 3 and parts[:2] == ('cache', 'growth'):
        return parts[2] in GROWTH_FILES
    if len(parts) == 4 and parts[:2] == ('cache', 'growth'):
        if parts[2] in {'documents', 'parsed_documents'}:
            extension = 'html' if parts[2] == 'documents' else 'json'
            return bool(re.fullmatch(r'\d{14}\.' + extension, parts[3]))
        if parts[2] in {'trade_documents', 'trade_parsed'}:
            extension = 'html' if parts[2] == 'trade_documents' else 'json'
            return bool(re.fullmatch(r'[0-9a-f]{24,64}\.' + extension, parts[3]))
    if len(parts) == 4 and parts[:2] == ('cache', 'snapshots'):
        if parts[2] == 'manifests':
            return bool(re.fullmatch(r'[0-9a-f]{64}\.json', parts[3]))
        if parts[2] == 'objects':
            return bool(re.fullmatch(r'[0-9a-f]{64}\.(?:pkl|gz|json)', parts[3]))
    return len(parts) == 2 and parts[0] == 'test_output' and parts[1] in {
        'verified_prices.pkl', 'verified_fundamentals.pkl', 'verified_report.json', 'verified_snapshot.json'}


def _checksum(stream):
    return hashlib.file_digest(stream, 'sha256').hexdigest()


def _safe_target(root, name):
    parts = _parts(name)
    if not parts:
        raise RuntimeError('승인되지 않은 원자료 아카이브 경로')
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('원자료 경로가 저장소 밖을 가리킵니다.')
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise RuntimeError('원자료 경로의 링크 또는 junction을 허용하지 않습니다.')
    return target


def _check_secret_values(path):
    secrets = [value.encode() for name in SECRET_ENV if len(value := os.getenv(name, '')) >= 8]
    if not secrets:
        return
    overlap, tail = max(map(len, secrets)), b''
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            data = tail + chunk
            if any(secret in data for secret in secrets):
                raise RuntimeError('원자료 파일에서 인증정보가 발견되어 아카이브 작업을 중단했습니다.')
            tail = data[-overlap:]


def _contains_provided_material(value):
    """Supplied/permissioned source text is local-only unless backup is authorized separately."""
    if isinstance(value, dict):
        if 'transcript' in value or str(value.get('receipt', '')).startswith('DOC-'):
            return True
        if value.get('accessBasis') in {'user_supplied', 'source_permission', 'creator_permission', 'licensed'}:
            return True
        return any(_contains_provided_material(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_provided_material(item) for item in value)
    return False


def _validate_snapshot_graph(files, read_json):
    pointer_name = 'test_output/verified_snapshot.json'
    if pointer_name not in files:
        raise RuntimeError('원자적으로 검증된 원자료 포인터가 없어 캐시를 저장·복원하지 않습니다.')
    snapshot_id = read_json(pointer_name).get('snapshotId')
    if not isinstance(snapshot_id, str) or not re.fullmatch(r'[0-9a-f]{64}', snapshot_id):
        raise RuntimeError('원자료 포인터 식별자가 유효하지 않습니다.')
    primary = f'cache/snapshots/manifests/{snapshot_id}.json'
    if primary not in files:
        raise RuntimeError('검증 원자료 manifest가 없습니다.')
    primary_manifest = None
    for name in files:
        if not name.startswith('cache/snapshots/manifests/'):
            continue
        manifest = read_json(name)
        entries = manifest.get('files')
        if not isinstance(entries, dict) or not entries:
            raise RuntimeError('비어 있는 원자료 snapshot manifest')
        expected = digest({'files': entries, 'sourceCutoff': manifest.get('sourceCutoff')})
        if manifest.get('snapshotId') != expected or name.rsplit('/', 1)[-1] != expected + '.json':
            raise RuntimeError('원자료 snapshot 식별자·내용이 일치하지 않습니다.')
        for entry in entries.values():
            relative = str(entry.get('path', '')).replace('\\', '/')
            object_name = 'cache/snapshots/' + relative
            if not relative.startswith('objects/') or not allowed(object_name) or object_name not in files:
                raise RuntimeError('원자료 snapshot 객체가 누락되었거나 경로가 잘못되었습니다.')
            if files[object_name]['sha256'] != entry.get('sha256') or files[object_name]['bytes'] != entry.get('bytes'):
                raise RuntimeError('원자료 snapshot 객체의 크기·해시 검증 실패')
        if name == primary:
            primary_manifest = manifest
    if not primary_manifest or not {'prices', 'fundamentals', 'report'}.issubset(primary_manifest['files']):
        raise RuntimeError('가격·재무·검증보고서 셋이 완전하지 않습니다.')
    report = read_json('cache/snapshots/' + primary_manifest['files']['report']['path'].replace('\\', '/'))
    if report.get('qualityStatus') != '정상':
        raise RuntimeError('검증 실패한 원자료는 정상 캐시로 저장·복원할 수 없습니다.')
    if report.get('latestPriceDate') != primary_manifest.get('sourceCutoff'):
        raise RuntimeError('원자료 manifest와 검증보고서 기준일이 다릅니다.')
    return {'snapshotId': snapshot_id, 'sourceCutoff': primary_manifest['sourceCutoff']}


def pack(root, archive_path):
    root, archive_path = Path(root).resolve(), Path(archive_path)
    files, paths = {}, {}
    for folder in ('cache', 'test_output'):
        for path in (root/folder).rglob('*'):
            name = path.relative_to(root).as_posix()
            if not path.is_file() or path.is_symlink() or not allowed(name):
                continue
            path = _safe_target(root, name)
            # Pointer objects contain these bytes already: do not upload duplicate frames.
            if name in {'test_output/verified_prices.pkl', 'test_output/verified_fundamentals.pkl', 'test_output/verified_report.json'}:
                continue
            _check_secret_values(path)
            with path.open('rb') as stream:
                files[name] = {'sha256': _checksum(stream), 'bytes': path.stat().st_size}
            paths[name] = path
    private_objects = {name for name, path in paths.items()
                       if name.startswith('cache/snapshots/objects/') and name.endswith('.json')
                       and _contains_provided_material(json.loads(path.read_text('utf-8-sig')))}
    private_manifests = set()
    for name, path in paths.items():
        if name.startswith('cache/snapshots/manifests/'):
            manifest = json.loads(path.read_text('utf-8-sig'))
            if any('cache/snapshots/' + entry['path'].replace('\\', '/') in private_objects
                   for entry in manifest.get('files', {}).values()):
                private_manifests.add(name)
    for name in private_objects | private_manifests:
        paths.pop(name)
        files.pop(name)
    verified = _validate_snapshot_graph(files, lambda name: json.loads(paths[name].read_text('utf-8-sig')))
    manifest = {'schemaVersion': 2, 'status': 'verified', **verified, 'files': files,
                'createdAt': datetime.now(timezone.utc).isoformat(),
                'privateSnapshotsExcluded': len(private_manifests),
                'privacyNotice': '제공 문서·자막 원문과 해당 로컬 스냅샷은 자동 백업에서 제외합니다.',
                'retentionNotice': 'CI 원자료 아카이브 보존은 90일이며 영구 데이터베이스가 아닙니다. 추천 장부는 Git에 보존합니다.'}
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=archive_path.name+'.', suffix='.tmp', dir=archive_path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, 'w:gz') as archive:
            for name, path in paths.items():
                archive.add(path, arcname=name, recursive=False)
            data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
            member = tarfile.TarInfo(ARCHIVE_MANIFEST)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        os.replace(temporary, archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def restore(root, archive_path):
    root = Path(root).resolve()
    with tarfile.open(archive_path, 'r:gz') as archive:
        members, files = {}, {}
        for member in archive.getmembers():
            name = member.name
            if name in members or not member.isfile() or (name != ARCHIVE_MANIFEST and not allowed(name)):
                raise RuntimeError('승인되지 않은 원자료 아카이브 경로·중복·링크')
            _safe_target(root, name)
            members[name] = member
            if name != ARCHIVE_MANIFEST:
                with archive.extractfile(member) as stream:
                    files[name] = {'sha256': _checksum(stream), 'bytes': member.size}
        def read_json(name):
            member = members.get(name)
            if member is None or member.size > 20*1024*1024:
                raise RuntimeError('필수 원자료 manifest 누락 또는 크기 제한 초과')
            with archive.extractfile(member) as stream:
                return json.load(stream)
        manifest = read_json(ARCHIVE_MANIFEST)
        if manifest.get('schemaVersion') != 2 or manifest.get('status') != 'verified' or manifest.get('files') != files:
            raise RuntimeError('원자료 아카이브 상태 manifest·내용 해시 검증 실패')
        verified = _validate_snapshot_graph(files, read_json)
        if any(manifest.get(key) != value for key, value in verified.items()):
            raise RuntimeError('원자료 상태 manifest와 검증 포인터 불일치')
        for name in files:
            if name.startswith('cache/snapshots/objects/') and name.endswith('.json'):
                if _contains_provided_material(read_json(name)):
                    raise RuntimeError('제공 문서·자막이 포함된 아카이브는 자동 복원하지 않습니다.')
        # Validate all contents before mutation; switch the primary pointer last.
        with tempfile.TemporaryDirectory(prefix='raw-state-restore-', dir=root) as folder:
            staging = Path(folder)
            for name, member in members.items():
                if name == ARCHIVE_MANIFEST:
                    continue
                target = _safe_target(staging, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                _check_secret_values(target)
            for name in sorted(files, key=lambda name: name == 'test_output/verified_snapshot.json'):
                target = _safe_target(root, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging/name, target)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['pack', 'restore', 'audit'])
    parser.add_argument('--archive', type=Path, default=ROOT/'test_output/raw-state.tar.gz')
    args = parser.parse_args()
    if args.mode == 'audit':
        paths = list((ROOT/'test_output').glob('*report*.json'))
        paths += [ROOT/'test_output/full_candidates.json', ROOT/'test_output/combined_audit.test.json']
        for path in paths:
            if path.exists():
                _check_secret_values(_safe_target(ROOT, path.relative_to(ROOT).as_posix()))
                if _contains_provided_material(json.loads(path.read_text('utf-8-sig'))):
                    raise RuntimeError('제공 원문이 포함된 검증 보고서는 자동 업로드하지 않습니다.')
        print('Audit credential check completed')
        return
    manifest = pack(ROOT, args.archive) if args.mode == 'pack' else restore(ROOT, args.archive)
    print(json.dumps({'status': 'verified', 'snapshotId': manifest['snapshotId'],
                      'fileCount': len(manifest['files']), 'retentionDays': 90}, ensure_ascii=False))


if __name__ == '__main__':
    main()
