"""Refresh broker forecasts and official guidance before scoreboard calculation."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

from growth_discovery import KST
from refresh_store import json_write, read_json


ROOT = Path(__file__).resolve().parent
FORECAST_DIR = ROOT / "test_output" / "forecast_engine"
FORECAST = FORECAST_DIR / "forecast_consensus.json"
GUIDANCE = ROOT / "test_output" / "guidance.json"


def run(*args):
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def validate():
    rows = read_json(FORECAST, [])
    guidance = read_json(GUIDANCE, {})
    annual = [row for row in rows if row.get("scope") == "annual"
              and row.get("status") != "불일치 검토"]
    current = str(datetime.now(KST).year) + "FY"
    following = str(datetime.now(KST).year + 1) + "FY"
    by_ticker = {}
    for row in annual:
        by_ticker.setdefault(str(row.get("ticker", "")).zfill(6), set()).add(row.get("period"))
    paired = sorted(ticker for ticker, periods in by_ticker.items()
                    if {current, following}.issubset(periods))
    if not isinstance(guidance, dict) or not isinstance(guidance.get("rows"), list):
        raise RuntimeError("Guidance output format invalid")
    if not str(guidance.get("status", {}).get("status", "")).startswith("정상"):
        raise RuntimeError("Guidance collection is not fully verified; keep previous public file")
    if not paired:
        raise RuntimeError("No current/next-year broker forecast pairs")
    return {"status": "정상", "checkedAt": datetime.now(KST).isoformat(timespec="seconds"),
            "forecastPoints": len(rows), "annualForecastTickers": len(by_ticker),
            "currentNextYearPairs": len(paired), "guidanceRows": len(guidance["rows"]),
            "guidanceTickers": len({row["ticker"] for row in guidance["rows"]}),
            "policy": "60일 이내 유효 컨센서스 우선, 같은 기간·항목 컨센서스가 없을 때만 연결 연간 가이던스 보충; 불일치 검토는 점수 제외"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-forecast", action="store_true")
    parser.add_argument("--reuse-naver-index", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    args = parser.parse_args()
    if not args.publish_only:
        if not args.skip_forecast:
            command = ["forecast_report_engine.py", "--start-date", f"{datetime.now(KST).year}-01-01",
                       "--incremental", "--skip-hankyung-pdf-backfill",
                       "--freshness-days", "60", "--consensus-window-days", "30"]
            if args.reuse_naver_index:
                command.append("--reuse-naver-index")
            run(*command)
        run("guidance_engine.py", "--config", "config.kis.example.json",
            "--consensus", str(FORECAST.relative_to(ROOT)),
            "--output", str(GUIDANCE.relative_to(ROOT)))
    report = validate()
    json_write(ROOT / "test_output" / "forecast-integration-report.json", report)
    if args.publish_only:
        json_write(ROOT / "guidance.json", read_json(GUIDANCE))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
