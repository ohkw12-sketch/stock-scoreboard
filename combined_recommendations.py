"""Experimental, versioned combination. Does not modify any source score or the value engine."""
from __future__ import annotations

import math
from refresh_store import digest

RULES = {
    'version': 'combined-1.0', 'topCount': 10,
    'valueMaxPOP': 10, 'valueMinPOP': 1, 'valueMaxPremiumPct': 0,
    'valueMinQuarters': 4, 'valueConfidence': ['A', 'B'],
    'growthMinEvidence': 2, 'growthMinScore': 30,
    'growthPriceReflection': ['미반영 가능', '일부 가격 반응'],
}


def finite(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def build_combined(entry_rows, value_rows, growth_rows, rotation_rows, *, source_date,
                   snapshot_id, generated_at, rules=None):
    rules = {**RULES, **(rules or {})}
    by_value = {r['ticker']: r for r in value_rows}
    by_growth = {r['ticker']: r for r in growth_rows}
    by_rotation = {r['ticker']: r for r in rotation_rows}
    rows, audit = [], []
    for entry in entry_rows:
        ticker = entry['ticker']
        value, growth = by_value.get(ticker, {}), by_growth.get(ticker, {})
        pop, premium = finite(value.get('normalizedPOP')), finite(value.get('normalizedPremiumPct'))
        score = finite(growth.get('score'))
        value_ok = (pop is not None and rules['valueMinPOP'] <= pop <= rules['valueMaxPOP']
                    and premium is not None and premium < rules['valueMaxPremiumPct']
                    and value.get('confidence') in rules['valueConfidence']
                    and (value.get('normalizedQuarterCount') or 0) >= rules['valueMinQuarters']
                    and value.get('priceDate') == source_date)
        growth_ok = (score is not None and score >= rules['growthMinScore']
                     and (growth.get('evidenceCount') or 0) >= rules['growthMinEvidence']
                     and growth.get('priceReflection') in rules['growthPriceReflection']
                     and growth.get('sourceDate') == source_date)
        entry_ok = entry.get('entryState') in ('진입가능', '곧진입') and entry.get('priceDate') == source_date
        audit.append({'ticker': ticker, 'value': bool(value_ok), 'growth': bool(growth_ok),
                      'entry': entry_ok})
        if not entry_ok or not (value_ok or growth_ok):
            continue
        conditions = ['진입']
        components = [max(0, min(100, finite(entry.get('entryScore')) or 0))]
        if value_ok:
            conditions.append('가치')
            components.append(finite(value.get('valueScore')) or 0)
        if growth_ok:
            conditions.append('성장')
            components.append(score)
        rotation = by_rotation.get(ticker, {})
        if rotation.get('priceDate') != source_date:
            rotation = {}
        rows.append({'ticker': ticker, 'name': entry['name'], 'sector': entry.get('sector'),
                     'condition': ' + '.join(conditions), 'conditions': conditions,
                     'entryState': entry['entryState'], 'currentPrice': entry.get('currentPrice'),
                     'valueScore': value.get('valueScore'), 'growthScore': growth.get('score'),
                     'sectorRelation': rotation.get('relation') or entry.get('relation') or '미확인',
                     'combinedScore': round(sum(components) / len(components), 2),
                     'sourceDate': source_date, 'actionable': entry['entryState'] == '진입가능'})
    rows.sort(key=lambda r: (not r['actionable'], -r['combinedScore'], r['ticker']))
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    return {'schemaVersion': 1, 'ruleVersion': rules['version'], 'rulesHash': digest(rules),
            'snapshotId': snapshot_id, 'generatedAt': generated_at, 'sourceDate': source_date,
            'status': '검증용 후보 · 아직 공개 확정 전', 'publicationState': 'preview',
            'notice': '실험 규칙입니다. 수익성이 검증된 전략이 아니며 곧진입은 관찰 후보입니다. 기존 가치·성장 점수는 변경하지 않습니다.',
            'rules': rules, 'candidateCount': len(rows), 'rows': rows[:rules['topCount']],
            '_allRows': rows, '_audit': audit,
            'inputCounts': {'entry': len(entry_rows), 'value': len(value_rows),
                            'growth': len(growth_rows), 'rotation': len(rotation_rows)}}
