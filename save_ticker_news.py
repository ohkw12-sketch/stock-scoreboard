"""Collect SaveTicker articles authored by 오선 that matter to the Korean stock market."""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from refresh_store import json_write, read_json


ROOT = Path(__file__).parent
KST = timezone(timedelta(hours=9))
SOURCE_URL = "https://saveticker.com/news"
LIST_URL = "https://saveticker.com/api/news/list"
DETAIL_URL = "https://saveticker.com/api/news/detail"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-scoreboard/1.0; +https://stock-scoreboard.pages.dev/)",
    "Accept": "application/json",
    "Referer": SOURCE_URL,
}


DIRECT_RULES = (
    (r"한국거래소|코스피|코스닥|한국 증시|국내 증시", "국내시장", "코스피·코스닥 및 거래제도에 직접 연결되는 뉴스입니다."),
    (r"한국은행|금융위원회|금융감독원|기획재정부|산업통상자원부|한국 대통령실|한국 정부", "정책·금융", "국내 금리·환율·정책 기대에 직접 연결되는 뉴스입니다."),
    (r"한국 (?:실업률|고용|수출|물가|GDP|성장률)|원화|달러/원|원/달러|국고채|통화안정채권", "국내경제", "국내 경기·금리·환율과 외국인 수급에 직접 연결되는 뉴스입니다."),
    (r"삼성(?:전자|전기|SDI|디스플레이|중공업|물산|생명|화재)|삼성(?=·|\s|,|$)|SK하이닉스|SK이노베이션", "반도체·전자", "국내 삼성 계열·SK 반도체·전자 기업을 직접 언급한 뉴스입니다."),
    (r"현대차|기아|현대모비스|현대제철", "자동차·소재", "국내 자동차·소재 기업을 직접 언급한 뉴스입니다."),
    (r"한화오션|한화에어로스페이스|현대로템|LIG넥스원", "조선·방산", "국내 조선·방산 기업의 수주와 사업에 직접 연결되는 뉴스입니다."),
    (r"HD한국조선해양|HD현대중공업|삼성중공업", "조선", "국내 조선 기업의 수주와 업황에 직접 연결되는 뉴스입니다."),
    (r"POSCO|포스코|LG에너지솔루션|에코프로|엘앤에프", "철강·2차전지", "국내 철강·2차전지 기업을 직접 언급한 뉴스입니다."),
    (r"셀트리온|삼성바이오|유한양행|한미약품", "바이오", "국내 바이오 기업을 직접 언급한 뉴스입니다."),
    (r"NAVER|네이버|카카오|두산에너빌리티|LS ELECTRIC|효성중공업", "인터넷·전력", "국내 인터넷·전력 기업을 직접 언급한 뉴스입니다."),
    (r"한국전력|대한항공|아시아나항공|현대글로비스|SK텔레콤|KT&G|LG유플러스", "운송·통신·공공", "국내 운송·통신·공공 기업을 직접 언급한 뉴스입니다."),
    (r"KB금융|신한지주|하나금융지주|우리금융지주|삼성생명|삼성화재", "금융", "국내 금융 기업을 직접 언급한 뉴스입니다."),
    (r"삼성물산|현대건설|DL이앤씨|삼성E&A|한국항공우주|한화시스템", "건설·방산", "국내 건설·방산 기업을 직접 언급한 뉴스입니다."),
    (r"LG전자|LG화학|LG디스플레이|크래프톤|엔씨소프트|카카오뱅크|고려아연|포스코퓨처엠|에코프로비엠|삼성바이오로직스", "국내대형주", "국내 주요 상장기업을 직접 언급한 뉴스입니다."),
)

SECTOR_RULES = (
    (r"HBM|DRAM|낸드|메모리 반도체|AI 반도체|AI 칩|반도체 패키징|엔비디아|TSMC|마이크론", "반도체", "삼성전자·SK하이닉스와 국내 반도체 장비·소재주의 수요 및 경쟁 구도에 영향을 줄 수 있습니다."),
    (r"폴더블|OLED|유기발광다이오드", "디스플레이·부품", "국내 OLED 패널·힌지·카메라 모듈 공급망의 수요 기대에 연결됩니다."),
    (r"브렌트유|유가.{0,12}100달러|호르무즈|원유 유조선|중동 분쟁", "정유·화학·운송", "정유사는 제품 가격과 정제마진, 항공·운송·화학은 연료·원료비 부담을 함께 확인해야 합니다."),
    (r"달러/엔|엔화.{0,8}강세|엔화.{0,8}급등", "자동차·수출", "일본 경쟁사의 가격 경쟁력 변화가 국내 자동차·기계 등 수출주에 상대적으로 영향을 줄 수 있습니다."),
    (r"AI 인프라|데이터센터|전력망|변압기|원자력 전력|전력 수요", "전력기기·냉각", "국내 변압기·전력기기·냉각 설비 기업의 수주 기대와 투자 속도에 연결됩니다."),
    (r"리튬|니켈|양극재|전고체.{0,8}배터리|배터리 공급망", "2차전지", "국내 배터리·소재 기업의 원가와 수요 전망에 영향을 줄 수 있습니다."),
    (r"철강.{0,20}관세|자동차.{0,20}관세|대중국.{0,12}관세|미국.{0,12}관세", "수출·관세", "국내 자동차·철강·기계 수출기업의 가격 경쟁력과 현지 생산 전략을 확인해야 합니다."),
)

POSITIVE_TERMS = re.compile(r"협력|선정|수주|확대|증가|급증|상향|돌파구|투자 계획|완화|회복|재평가")
NEGATIVE_TERMS = re.compile(r"하락|급락|취소|제재|공격|부담|경고|축소|금지|우려")
RUMOR = re.compile(r"카더라|소식통|검토 중|추진")


def _request_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(KST)


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(part.get("content") or "") for part in value if isinstance(part, dict))
    return ""


def _clean(value: str) -> str:
    value = re.sub(r"https?://\S+", "", value or "")
    value = re.sub(r"본 콘텐츠는.*$", "", value, flags=re.S)
    return re.sub(r"\s+", " ", value).strip(" -·\n")


def _summary(detail: dict, fallback: dict) -> str:
    content = _clean(_text(detail.get("content")) or _text(fallback.get("content")))
    title = _clean(str(detail.get("title") or fallback.get("title") or ""))
    if not content:
        return title
    pieces = [p.strip(" -·") for p in re.split(r"(?<=[.!?다])\s+|\s+-\s+", content) if len(p.strip()) >= 12]
    chosen = []
    for piece in pieces:
        if piece == title or piece in chosen:
            continue
        chosen.append(piece)
        if len(" ".join(chosen)) >= 190 or len(chosen) == 2:
            break
    return (" ".join(chosen) or content)[:240].rstrip() + ("…" if len(" ".join(chosen) or content) > 240 else "")


def _matches(text: str, rules) -> list[tuple[str, str]]:
    found = []
    for pattern, sector, impact in rules:
        if re.search(pattern, text, re.I):
            found.append((sector, impact))
    return found


def classify(item: dict, now: datetime) -> dict | None:
    if str(item.get("author_name") or "").strip() != "오선":
        return None
    created = _parse_time(item.get("created_at"))
    text = _clean(str(item.get("title") or "") + " " + _text(item.get("content")))
    direct = _matches(text, DIRECT_RULES)
    sectors = _matches(text, SECTOR_RULES)
    if not direct and not sectors:
        return None
    scope = "국장 직접" if direct else "국내 섹터 영향"
    selected = direct or sectors
    labels = list(dict.fromkeys(label for label, _ in selected))[:3]
    impacts = list(dict.fromkeys(impact for _, impact in selected))[:2]
    views = int(item.get("view_count") or 0)
    age_hours = max(0.0, (now - created).total_seconds() / 3600)
    score = (85 if direct else 52) + min(20, math.log10(max(1, views)) * 5)
    score += 7 if item.get("is_top_story") or item.get("is_group_top_story") else 0
    score -= min(18, age_hours * .35)
    if RUMOR.search(text):
        score -= 12
    positive, negative = bool(POSITIVE_TERMS.search(text)), bool(NEGATIVE_TERMS.search(text))
    tone = "혼합" if positive and negative else "긍정" if positive else "부정" if negative else "확인"
    return {
        "id": str(item.get("id")),
        "title": _clean(str(item.get("title") or "")),
        "scope": scope,
        "tone": tone,
        "sectors": labels,
        "marketImpact": " ".join(impacts),
        "publishedKST": created.strftime("%Y-%m-%d %H:%M"),
        "createdAt": created.isoformat(timespec="seconds"),
        "source": str(item.get("source") or "SaveTicker"),
        "author": "오선",
        "viewCount": views,
        "isRumor": bool(RUMOR.search(text)),
        "importanceScore": round(score, 1),
        "url": f"https://saveticker.com/news/{item.get('id')}",
    }


def _dedupe_key(item: dict) -> str:
    title = re.sub(r"\[[^]]+]|\([^)]*카더라[^)]*\)|\d+보|종합|수정|내용 추가", "", item["title"])
    words = re.findall(r"[가-힣A-Za-z0-9]+", title.lower())
    return " ".join(words[:8])


def select_news(items: list[dict], now: datetime, limit: int = 10) -> list[dict]:
    candidates = [classified for item in items if (classified := classify(item, now))]
    candidates.sort(key=lambda item: (item["importanceScore"], item["createdAt"]), reverse=True)
    unique, keys = [], []
    for item in candidates:
        key = _dedupe_key(item)
        word_set = set(key.split())
        if any(key == prior or (len(word_set) >= 4 and len(word_set & set(prior.split())) / len(word_set | set(prior.split())) >= .62) for prior in keys):
            continue
        keys.append(key)
        unique.append(item)
    direct = [item for item in unique if item["scope"] == "국장 직접"][:6]
    selected = list(direct)
    used_primary = {item["sectors"][0] for item in selected if item["sectors"]}
    for item in unique:
        if item in selected:
            continue
        primary = item["sectors"][0] if item["sectors"] else "기타"
        if item["scope"] == "국내 섹터 영향" and primary in used_primary:
            continue
        selected.append(item)
        used_primary.add(primary)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda item: item["importanceScore"], reverse=True)[:limit]


def collect_list(now: datetime, hours: int = 36, max_pages: int = 12) -> list[dict]:
    cutoff = now - timedelta(hours=hours)
    items = []
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode({"page": page, "page_size": 100, "sort": "created_at_desc"})
        batch = _request_json(LIST_URL + "?" + query).get("news_list", [])
        if not batch:
            break
        items.extend(item for item in batch if _parse_time(item.get("created_at")) >= cutoff)
        if _parse_time(batch[-1].get("created_at")) < cutoff:
            break
    return items


def build_board(items: list[dict], details: dict[str, dict], now: datetime, hours: int = 36) -> dict:
    selected = select_news(items, now)
    for item in selected:
        detail = details.get(item["id"], {})
        item["summary"] = _summary(detail, next(x for x in items if str(x.get("id")) == item["id"]))
        item["source"] = str(detail.get("source") or item["source"])
    author_count = sum(str(item.get("author_name") or "").strip() == "오선" for item in items)
    direct_count = sum(item["scope"] == "국장 직접" for item in selected)
    oldest = min((_parse_time(item.get("created_at")) for item in items), default=now)
    return {
        "schemaVersion": 1,
        "meta": {
            "title": "유튜브시황2",
            "status": "SaveTicker 오선 국장 주요뉴스 갱신",
            "updatedKST": now.strftime("%Y-%m-%d %H:%M"),
            "range": f"{oldest.strftime('%m.%d %H:%M')}–{now.strftime('%m.%d %H:%M')}",
            "sourceUrl": SOURCE_URL,
            "source": "SaveTicker · 작성자 오선",
            "selectionRule": "국내 기업·시장 직접 뉴스 우선, 국내 업종 영향이 명확한 글로벌 뉴스 보완, 유사 속보 통합",
            "fetchedCount": len(items),
            "authorCount": author_count,
            "selectedCount": len(selected),
            "directCount": direct_count,
            "sectorCount": len(selected) - direct_count,
            "windowHours": hours,
        },
        "items": selected,
        "collection": {"status": "정상", "checkedAtKST": now.isoformat(timespec="seconds")},
    }


def validate_board(board: dict) -> None:
    if board.get("schemaVersion") != 1 or not isinstance(board.get("items"), list):
        raise ValueError("SaveTicker 시황 형식 오류")
    for item in board["items"]:
        if item.get("author") != "오선":
            raise ValueError("오선 작성자가 아닌 기사가 포함됐습니다")
        if item.get("scope") not in ("국장 직접", "국내 섹터 영향"):
            raise ValueError("국내 증시 연결 구분이 없습니다")
        if not re.fullmatch(r"https://saveticker\.com/news/\d+", str(item.get("url") or "")):
            raise ValueError("허용되지 않은 기사 링크")


def refresh(now: datetime | None = None, hours: int = 36) -> dict:
    now = (now or datetime.now(KST)).astimezone(KST)
    items = collect_list(now, hours=hours)
    initial = select_news(items, now)
    details = {}
    for item in initial:
        try:
            details[item["id"]] = _request_json(DETAIL_URL + "?" + urllib.parse.urlencode({"id": item["id"]}))
        except Exception:
            details[item["id"]] = {}
    board = build_board(items, details, now, hours)
    validate_board(board)
    return board


def refresh_or_retain(previous: dict | None, now: datetime | None = None, hours: int = 36) -> dict:
    now = (now or datetime.now(KST)).astimezone(KST)
    try:
        return refresh(now, hours)
    except Exception as exc:
        if previous:
            retained = copy.deepcopy(previous)
            retained.setdefault("meta", {})["status"] = "갱신 실패·이전 자료 유지"
            retained["collection"] = {"status": "실패·이전유지", "checkedAtKST": now.isoformat(timespec="seconds"), "reason": type(exc).__name__}
            return retained
        empty = build_board([], {}, now, hours)
        empty["meta"]["status"] = "SaveTicker 수집 실패"
        empty["collection"] = {"status": "수집 실패", "checkedAtKST": now.isoformat(timespec="seconds"), "reason": type(exc).__name__}
        return empty


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "saveticker-market.json")
    parser.add_argument("--hours", type=int, default=36)
    args = parser.parse_args()
    previous = read_json(args.output)
    board = refresh_or_retain(previous, hours=args.hours)
    validate_board(board)
    json_write(args.output, board)
    print(json.dumps({"status": board["collection"]["status"], "selected": len(board["items"]), "updatedKST": board["meta"]["updatedKST"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
