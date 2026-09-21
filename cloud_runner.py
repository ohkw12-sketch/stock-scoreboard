"""Trusted scheduling, verification and promotion around the AI evidence collector."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from growth_discovery import KST
from issue_spread import trading_day
from refresh_store import read_json, load_verified_frames, json_write

ROOT = Path(__file__).resolve().parent
PUBLIC = ('data.json', 'youtube-market.json', 'sampro-market.json', 'saveticker-market.json',
          'issue-spread.json', 'combined-recommendations.json', 'recommendation-performance.json',
          'refresh-status.json', 'recommendation-history.json', 'guidance.json')


def run(*args):
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def slot_for(schedule, requested, now):
    if requested in ('08:00', '10:30', '15:00'):
        return requested
    mapping = {'0 23 * * 0-4': '08:00', '30 1 * * 1-5': '10:30', '0 6 * * 1-5': '15:00'}
    if schedule:
        if schedule not in mapping:
            raise ValueError('Unknown scheduled slot')
        return mapping[schedule]
    return '08:00' if now.hour < 10 else '10:30' if now.hour < 15 else '15:00'


def verify():
    prices, fundamentals, report = load_verified_frames(ROOT/'test_output', ROOT/'cache')
    if prices.empty or fundamentals.empty or report.get('qualityStatus') != '정상':
        raise RuntimeError('Restored raw frames are incomplete')
    if str(prices.date.max().date()) != report['latestPriceDate']:
        raise RuntimeError('Restored price date mismatch')
    result = {'sourceDate': report['latestPriceDate'], 'priceTickers': int(prices.ticker.nunique()),
              'fundamentalRows': len(fundamentals), 'status': 'restored-and-verified',
              'universeSourceStatus': report.get('universeSourceStatus')}
    json_write(ROOT/'test_output/cloud-verification.json', result)
    print(json.dumps(result))


def build(slot):
    review = read_json(ROOT/'test_output/cloud-review.json', {})
    today = str(datetime.now(KST).date())
    if review.get('slot') != slot or not str(review.get('checkedAt', '')).startswith(today):
        raise RuntimeError('Current-slot AI review report is missing')
    # Collector may only write evidence and candidates, never source or public boards.
    subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--', '.'], cwd=ROOT, check=True)
    unexpected = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=ROOT, text=True)
    if unexpected.strip():
        raise RuntimeError('Unexpected collector files outside ignored evidence/output directories')
    if slot == '08:00':
        try:
            run('youtube_refresh.py', '--force')
            candidate = ROOT/'test_output/youtube-market.test.json'
            if candidate.exists():
                # Full refresh then reads this checked content to attach prices.
                run('promote_sections.py', '--youtube-only')
        except subprocess.CalledProcessError:
            prior = read_json(ROOT/'youtube-market.json')
            prior['contentStatus'] = {**prior.get('contentStatus', {}),
                'status': '원문 검증 실패·이전 자료 유지', 'checkedAt': datetime.now(KST).isoformat()}
            json_write(ROOT/'youtube-market.json', prior)
    if slot != '10:30':
        run('refresh_forecasts.py')
        run('refresh_all.py', '--config', 'config.kis.example.json', '--issue-slot', slot)
        run('promote_sections.py', '--sections', 'p1', 'p11', 'p2', 'p3', 'meta',
            '--youtube', '--research', '--allow-partial')
        run('refresh_forecasts.py', '--publish-only')
    run('issue_spread.py', '--slot', slot, '--promote')
    sampro = ROOT/'test_output/sections/sampro-market.json'
    if sampro.exists():
        try:
            run('sampro_validate.py', str(sampro), '--promote')
        except subprocess.CalledProcessError:
            prior = read_json(ROOT/'sampro-market.json')
            prior.update(status='원문 검증 실패·이전 자료 유지', checkedAt=datetime.now(KST).isoformat())
            json_write(ROOT/'sampro-market.json', prior)
    run('board_contract.py')


def verify_public():
    expected = {name: json.loads((ROOT/name).read_text('utf-8-sig')) for name in PUBLIC}
    for attempt in range(30):
        missing = []
        for name, value in expected.items():
            request = urllib.request.Request('https://stock-scoreboard.pages.dev/'+name+f'?verify={time.time_ns()}',
                                            headers={'Cache-Control': 'no-cache', 'User-Agent': 'scoreboard-publication-check'})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    actual = json.load(response)
                if actual != value:
                    missing.append(name)
            except Exception:
                missing.append(name)
        if not missing:
            print('All public scoreboard files match the verified local generation')
            return
        if attempt < 29:
            time.sleep(20)
    raise RuntimeError('Public deployment not verified: '+', '.join(missing))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['gate', 'verify', 'build', 'verify-public'])
    parser.add_argument('--slot', default='auto')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    now = datetime.now(KST)
    if args.mode == 'gate':
        slot = slot_for(os.getenv('EVENT_SCHEDULE', ''), args.slot, now)
        proceed = args.verify_only or trading_day(now)
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'run={str(proceed).lower()}\nslot={slot}\n')
        if not proceed:
            print('KRX closed: no collection or publication')
    elif args.mode == 'verify':
        verify()
    elif args.mode == 'verify-public':
        verify_public()
    else:
        build(args.slot)


if __name__ == '__main__':
    main()
