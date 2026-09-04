"""Promote only explicitly selected scoreboard sections from a validated candidate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from board_contract import ROOT, assert_contract, load_contract
from growth_discovery import json_write


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
    args = parser.parse_args()
    quality_path = args.candidate.parent / "collection_report.test.json"
    quality = json.loads(quality_path.read_text("utf-8"))
    if quality.get("qualityStatus") != "정상":
        raise RuntimeError("전체시장 자료 검증 실패로 반영을 중단했습니다.")
    live = json.loads(args.live.read_text("utf-8-sig"))
    candidate = json.loads(args.candidate.read_text("utf-8-sig"))
    result, report = promote(live, candidate, args.sections)
    temp_path = args.candidate.parent / "promotion-candidate.json"
    json_write(temp_path, result)
    assert_contract(args.html, temp_path)
    json_write(args.live, result)
    json_write(args.candidate.parent / "promotion_report.json", report)
    if args.youtube:
        youtube_candidate = args.candidate.parent / "youtube-market.test.json"
        if youtube_candidate.exists():
            json_write(ROOT / "youtube-market.json", json.loads(youtube_candidate.read_text("utf-8-sig")))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
