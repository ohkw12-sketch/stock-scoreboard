"""Build the separate research board from verified collectors; never publish.

Run after refresh_all.py, or by itself to reuse the existing verified snapshot.
All original data, recommendation ledgers and holding inputs remain unchanged.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from refresh_store import load_verified_frames, read_json, json_write, digest, public_fields
from scenario_engine import build, VERSION

ROOT = Path(__file__).resolve().parent


def engine_hash():
    # --root may point at a different workspace holding verified inputs.
    return file_hash(Path(__file__).resolve().parent / "scenario_engine.py")


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def validate(board, holdings):
    issues = []
    original = [{k: h.get(k) for k in ("ticker", "name", "qty", "avg")} for h in holdings]
    current = [{k: h.get(k) for k in ("ticker", "name", "qty", "avg")} for h in board["holdings"]]
    if original != current:
        issues.append("보유 입력 불일치")
    cutoff = board["meta"]["priceDate"]
    for key, scenario in board["scenarios"].items():
        tickers = scenario["tickers"]
        watch = scenario.get("watchTickers", [])
        from collections import Counter
        sectors = Counter(board["stocks"][t]["sector"] for t in tickers)
        if len(tickers) != len(set(tickers)) or len(sectors) > 5 or any(n > 3 for n in sectors.values()):
            issues.append(f"{key}: 중복·분산 규칙 불일치")
        watch_sectors = Counter(board["stocks"][t]["sector"] for t in watch)
        if (set(tickers) & set(watch) or len(watch) != len(set(watch)) or len(watch_sectors) > 5
                or any(n > 3 for n in watch_sectors.values())):
            issues.append(f"{key}: 관찰 후보 중복·분산 규칙 불일치")
        for t in tickers:
            s = board["stocks"][t]
            if not (s["eligible"] or s["heatObservation"]):
                issues.append(f"{t}: 허용되지 않은 후보 노출")
            if key in ("up", "range") and not s["scenarios"][key]["ready"]:
                issues.append(f"{t}: {key} 카드에 대기 후보 노출")
            if key == "up" and not s["fundamentals"]["growthQualified"]:
                issues.append(f"{t}: 성장 기준 미달 후보의 상승 카드 노출")
            if s["scenarios"][key]["ready"] and (s["blockers"] or not all(c["met"] for c in s["scenarios"][key]["checks"])):
                issues.append(f"{t}: 조건 충족 표시 불일치")
        for t in watch:
            s = board["stocks"][t]
            if (key != "up" or not s["eligible"] or not s["fundamentals"]["growthQualified"]
                    or s["scenarios"]["up"]["ready"]
                    or sum(not c["met"] for c in s["scenarios"]["up"]["checks"]) != 1):
                issues.append(f"{t}: 성장 관찰·가격 대기 조건 불일치")
    for t, s in board["stocks"].items():
        if s["price"]["date"] > cutoff:
            issues.append(f"{t}: 미래 가격 혼입")
        for p in s["fundamentals"]["forecasts"]:
            for metric in ("sales", "op"):
                if metric in p and (not p[metric]["url"] or not p[metric]["date"] or p[metric]["date"] > cutoff):
                    issues.append(f"{t}: 전망 출처·발표일 오류")
    if issues:
        raise ValueError("; ".join(issues[:20]))
    return {"status": "통과", "holdingInputsPreserved": True, "futureDatedInputsExcluded": True,
            "scenarioCount": len(board["scenarios"]), "stockCount": len(board["stocks"]),
            "sourceAttributionChecked": True}


def write_public_dataset(out, board, identity):
    """Load the small index first and fetch immutable detail shards on demand."""
    from collections import defaultdict
    shards = defaultdict(dict)
    summary = dict(board, meta=dict(board["meta"]), stocks={})
    generation = identity[:20]
    summary["meta"]["detailBase"] = f"scenario-stocks/{generation}"
    selected = {t for scenario in board["scenarios"].values()
                for t in scenario["tickers"] + scenario.get("watchTickers", [])}
    for ticker, stock in board["stocks"].items():
        shards[ticker[:2]][ticker] = stock
        small = {k: stock[k] for k in ("ticker", "name", "sector", "eligible", "heatObservation", "blockers")}
        small["fundamentals"] = {k: stock["fundamentals"][k] for k in ("track", "growthPath", "growthQualified",
            "annualOP", "nextOP", "nextOPGrowthPct", "nextOPDelta", "latestOPGrowthPct", "latestOPDelta",
            "annualMarginPct", "forecastDates")}
        small["price"] = {k: stock["price"][k] for k in ("rs20", "volumeRatio", "drawdown60")}
        if ticker in selected:
            small["price"]["box"] = stock["price"].get("box")
            small["scenarios"] = {k: {field: value[field] for field in ("ready", "state", "waiting")}
                                  for k, value in stock["scenarios"].items()}
        summary["stocks"][ticker] = small
    for prefix, stocks in shards.items():
        path = out / "scenario-stocks" / generation / f"{prefix}.json"
        if not path.exists():
            json_write(path, {"snapshotId": board["meta"]["snapshotId"], "engineHash": board["meta"]["engineHash"], "stocks": stocks})
    # This pointer is replaced only after every immutable detail shard exists.
    json_write(out / "scenario-public.test.json", summary)
    return {"indexBytes": (out / "scenario-public.test.json").stat().st_size, "detailShards": len(shards)}


def refresh(root=ROOT, *, output_dir=None, cache_dir=None, old_board=None):
    root = Path(root)
    out = Path(output_dir) if output_dir is not None else root / "test_output"
    cache = Path(cache_dir) if cache_dir is not None else root / "cache"
    sources = {"verifiedPointer": out / "verified_snapshot.json", "consensus": out / "forecast_engine/forecast_consensus.json",
        "guidance": out / "guidance.json", "observations": out / "forecast_engine/forecast_observations.json",
        "evidence": out / "evidence_snapshot.json", "publicBoard": root / "data.json"}
    before = {name: file_hash(path) for name, path in sources.items()}
    if not before["consensus"] or not before["verifiedPointer"]:
        raise ValueError("검증된 가격·전망 자료가 없습니다. 기존 수집기를 먼저 실행하세요.")
    prices, fundamentals, report = load_verified_frames(out, cache)
    pointer = read_json(sources["verifiedPointer"])
    comparison_board = old_board or read_json(out / "data.test.json", {})
    if not comparison_board:
        comparison_board = read_json(root / "data.json", {})
    # The public board remains the authority for immutable portfolio inputs.
    portfolio = read_json(root / "data.json", {}).get("p3", {})
    comparison_board = dict(comparison_board, p3=portfolio)
    output = out / "scenario-board.test.json"
    previous = read_json(output)
    if previous and previous.get("meta", {}).get("priceDate", "") > report["latestPriceDate"]:
        raise ValueError("기존 관찰 자료보다 과거인 가격 스냅샷입니다. 최신 검증 스냅샷을 복원하세요.")
    guidance = read_json(sources["guidance"], {})
    now = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    board = build(prices, fundamentals, report, read_json(sources["consensus"], []), guidance.get("rows", []),
        read_json(sources["observations"], []), read_json(sources["evidence"], {}), comparison_board,
        generated_at=now, snapshot_id=pointer["snapshotId"], previous=previous)
    board["meta"]["inputHashes"] = before
    board["meta"]["engineHash"] = engine_hash()
    board["meta"]["guidanceCollection"] = {k: guidance.get("status", {}).get(k) for k in ("status", "checked_at", "from", "to")}
    checks = validate(board, portfolio.get("rows", []))
    if before != {name: file_hash(path) for name, path in sources.items()}:
        raise ValueError("계산 중 입력파일이 바뀌었습니다. 혼합 자료 게시를 막고 다시 계산하세요.")
    board = public_fields(board)
    # Content-addressed observations preserve every generated version. These
    # are research snapshots, not actual recommendation/publication records.
    identity = digest({"inputs": before, "snapshot": pointer, "engine": board["meta"]["engineHash"]})
    archive = cache / "scenario_research" / f"{report['latestPriceDate']}-{identity[:20]}.json"
    if not archive.exists():
        json_write(archive, board)
    json_write(output, board)
    checks["publicFiles"] = write_public_dataset(out, board, identity)
    checks.update(inputFilesUnchanged=True, engineVersion=VERSION, priceDate=report["latestPriceDate"],
                  sameSnapshotComparison=board["comparison"]["sameSnapshot"], coverage=board["coverage"],
                  scenarios={k: {field: v[field] for field in ("candidateCount", "readyCount", "shownReadyCount")}
                             for k, v in board["scenarios"].items()})
    json_write(out / "scenario-validation.json", checks)
    json_write(out / "scenario-refresh-status.json", {"status": "계산완료", "attemptedAt": now,
                                                      "priceDate": report["latestPriceDate"], "version": VERSION})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = refresh(args.root)
    except Exception as exc:
        json_write(args.root / "test_output/scenario-refresh-status.json", {
            "status": "실패·이전유지", "attemptedAt": datetime.now(timezone.utc).isoformat(), "error": str(exc)})
        raise
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
