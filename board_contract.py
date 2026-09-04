"""Validate the user-approved display contract and board payload."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
CONTRACT_PATH = ROOT / "ui_contract.json"


def load_contract(path: Path = CONTRACT_PATH) -> dict:
    return json.loads(path.read_text("utf-8"))


def validate_html(html_path: Path, contract: dict) -> list[str]:
    html = html_path.read_text("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    errors: list[str] = []
    marker = soup.find("meta", attrs={"name": "scoreboard-ui-contract"})
    if marker is None or marker.get("content") != contract["version"]:
        errors.append(f"화면 계약 버전 표시가 {contract['version']}이 아닙니다.")
    for name, table_contract in contract["tables"].items():
        tbody = soup.find(id=table_contract["tbodyId"])
        if tbody is None:
            errors.append(f"{name}: tbody #{table_contract['tbodyId']}가 없습니다.")
            continue
        table = tbody.find_parent("table")
        headers = [th.get_text(" ", strip=True) for th in table.select("thead tr:last-child th")]
        if headers != table_contract["headers"]:
            errors.append(f"{name}: 제목/순서 변경 감지: {headers!r}")
    start = html.find("const normalizedRow=")
    end = html.find(";document.getElementById('p2body')", start)
    renderer = html[start:end] if start >= 0 and end > start else ""
    position = -1
    for token in contract["tables"]["p2"]["renderTokens"]:
        position = renderer.find(token, position + 1)
        if position < 0:
            errors.append(f"p2: 값 표시형식/순서 변경 감지: {token}")
            break
    for token in contract["tables"]["p2"]["forbiddenRenderTokens"]:
        if token.lower() in renderer.lower():
            errors.append(f"p2: 금지된 미래/T+ 표시 감지: {token}")
    return errors


def validate_board(board: dict, contract: dict) -> list[str]:
    errors: list[str] = []
    version = board.get("meta", {}).get("uiContractVersion")
    if version != contract["version"]:
        errors.append(f"data.json 화면 계약 버전이 {contract['version']}이 아닙니다: {version!r}")
    value_contract = contract["tables"]["p2"]
    for index, row in enumerate(board.get("p2", {}).get("rows", []), start=1):
        missing = [field for field in value_contract["fields"] if field not in row]
        if missing:
            errors.append(f"p2 {index}행 필드 누락: {', '.join(missing)}")
            continue
        numeric_fields = value_contract["fields"][2:-1]
        for field in numeric_fields:
            value = row.get(field)
            if value is not None and (not isinstance(value, (int, float)) or round(float(value), 1) != float(value)):
                errors.append(f"p2 {index}행 {field} 표시 정밀도 변경 감지: {value!r}")
        prefixes = tuple(prefix.lower() for prefix in value_contract["forbiddenFieldPrefixes"])
        forbidden = [key for key in row if key.lower().startswith(prefixes)]
        if forbidden:
            errors.append(f"p2 {index}행 미래/T+ 필드 감지: {', '.join(forbidden)}")
    return errors


def assert_contract(html_path: Path, board_path: Path, contract_path: Path = CONTRACT_PATH) -> None:
    contract = load_contract(contract_path)
    board = json.loads(board_path.read_text("utf-8-sig"))
    errors = validate_html(html_path, contract) + validate_board(board, contract)
    if errors:
        raise RuntimeError("\n".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description="주식평가창 표시 계약 검사")
    parser.add_argument("--html", type=Path, default=ROOT / "index.html")
    parser.add_argument("--board", type=Path, default=ROOT / "data.json")
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    args = parser.parse_args()
    assert_contract(args.html, args.board, args.contract)
    print(f"표시 계약 {load_contract(args.contract)['version']} 확인 완료")


if __name__ == "__main__":
    main()
