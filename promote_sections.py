"""Promote only explicitly selected scoreboard sections from a validated candidate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from board_contract import ROOT, assert_contract, load_contract
from refresh_store import json_write, read_json


SECTIONS = ("p1", "p11", "p2", "growth", "p3", "meta")


def digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def holding_inputs(board: dict) -> dict[str, tuple[object, object]]:
    return {row["name"]: (row.get("qty"), row.get("avg")) for row in board.get("p3", {}).get("rows", [])}


def promote(live: dict, candidate: dict, sections: list[str]) -> tuple[dict, dict]:
    unknown = sorted(set(sections) - set(SECTIONS))
    if unknown or not sections:
        raise ValueError(f"허용되지 않은 구역: {unknown or '선택 없음'}")
    before_holdings = holding_inputs(live)
    protected = {key: digest(live.get(key)) for key in SECTIONS if key not in sections}
    result = copy.deepcopy(live)
    for key in sections:
        if key not in candidate:
            raise KeyError(f"후보 파일에 {key} 구역이 없습니다.")
        result[key] = copy.deepcopy(candidate[key])
    if holding_inputs(result) != before_holdings:
        raise RuntimeError("보유 수량 또는 평균매입가 변경을 감지해 반영을 중단했습니다.")
    changed_protected = [key for key, value in protected.items() if digest(result.get(key)) != value]
    if changed_protected:
        raise RuntimeError(f"선택하지 않은 구역이 변경되었습니다: {', '.join(changed_protected)}")
    report = {
        "contractVersion": load_contract()["version"],
        "promotedSections": sections,
        "protectedSections": sorted(protected),
        "holdingsLocked": True,
        "status": "정상"
    }
    return result, report


def main() -> None:
    parser = argparse.ArgumentParser(description="검증된 구역만 data.json에 반영")
    parser.add_argument("--sections", nargs="+", required=True, choices=SECTIONS)
    parser.add_argument("--live", type=Path, default=ROOT / "data.json")
    parser.add_argument("--candidate", type=Path, default=ROOT / "test_output" / "data.test.json")
    parser.add_argument("--html", type=Path, default=ROOT / "index.html")
    parser.add_argument("--youtube", action="store_true")
    parser.add_argument("--research", action="store_true", help="별도 종합추천·성과검증 후보도 함께 반영")
    parser.add_argument("--allow-partial", action="store_true", help="실패한 평가창은 이전 게시값 유지")
    args = parser.parse_args()
    quality_path = args.candidate.parent / "collection_report.test.json"
    quality = json.loads(quality_path.read_text("utf-8"))
    if quality.get("qualityStatus") != "정상":
        raise RuntimeError("전체시장 자료 검증 실패로 반영을 중단했습니다.")
    live = json.loads(args.live.read_text("utf-8-sig"))
    candidate = json.loads(args.candidate.read_text("utf-8-sig"))
    failed = [k for k in args.sections if quality.get('sectionStates', {}).get(k, {}).get('status') == '실패·이전유지']
    if failed and not args.allow_partial:
        raise RuntimeError(f"실패한 구역 반영을 중단했습니다: {failed}")
    selected = [k for k in args.sections if k not in failed]
    if args.research and not {'p1', 'p11', 'p2', 'growth'}.issubset(set(args.sections)):
        raise RuntimeError('종합추천은 원본 진입·순환·가치·성장 갱신과 함께 반영해야 합니다.')
    result, report = promote(live, candidate, selected)
    report['retainedFailedSections'] = failed
    temp_path = args.candidate.parent / "promotion-candidate.json"
    json_write(temp_path, result)
    assert_contract(args.html, temp_path)
    pending = {args.live: result}
    if args.youtube:
        youtube_candidate = args.candidate.parent / "youtube-market.test.json"
        if youtube_candidate.exists():
            pending[args.live.parent / 'youtube-market.json'] = read_json(youtube_candidate)
    if args.research:
        for name in ('combined-recommendations', 'recommendation-performance'):
            data = read_json(args.candidate.parent / f'{name}.test.json')
            if data is None:
                raise RuntimeError(f'{name} 후보 없음')
            if data.get('schemaVersion') != 1 or not isinstance(data.get('rows'), list):
                raise RuntimeError(f'{name} 후보 형식 검증 실패')
            if name == 'combined-recommendations':
                if data.get('refreshState', {}).get('status') == '실패·이전유지':
                    continue
                data['publicationState'] = 'prepared'
                data['status'] = '공개 확인 대기 · 성과 기록은 사이트 반영 확인 후 시작'
                if data.get('runId') != candidate.get('meta', {}).get('runId'):
                    raise RuntimeError('종합추천과 원본 평가창의 계산 회차가 다릅니다.')
            pending[args.live.parent / f'{name}.json'] = data
        pending[args.live.parent / 'refresh-status.json'] = {
            'runId': candidate['meta'].get('runId'), 'attemptedAt': quality.get('attemptedAt'),
            'sourceDate': quality.get('latestPriceDate'), 'sections': quality.get('sectionStates', {})}
    # All candidates validated before any live file mutation; Git publication is one complete commit.
    for path, data in pending.items():
        json.dumps(data, ensure_ascii=False, allow_nan=False)
    for path, data in pending.items():
        json_write(path, data)
    json_write(args.candidate.parent / "promotion_report.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
