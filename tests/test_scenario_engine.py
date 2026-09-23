import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from scenario_engine import (forecast_index, revision_index, actuals, price_features, confirmed_box,
                             stock_fundamentals, assess, choose, evidence_index, regime)
from refresh_scenarios import write_public_dataset, validate, engine_hash

CUTOFF = "2026-09-21"


def consensus(period="2026FY", day="2026-09-01", sales=3000, op=300):
    return {"ticker": "123456", "period": period, "scope": "annual" if "FY" in period else "quarter",
            "status": "개별 추정치", "latestReportDate": day, "salesMedianKrw100m": sales,
            "operatingProfitMedianKrw100m": op,
            "estimates": [{"broker": "검증증권", "reportDate": day, "salesKrw100m": sales,
                           "operatingProfitKrw100m": op, "sourceUrl": "https://example.com/report"}]}


def guidance(day="2026-09-01", sales=4e11, op=4e10):
    return {"ticker": "123456", "period": "2026", "basis": "consolidated", "active": True,
            "fetch_status": "정상", "published_at": day, "source_url": "https://example.com/official",
            "sales": {"mid": sales}, "operating_profit": {"mid": op}}


def raw_fundamentals():
    return {"normalization_status": "정상", "quarter_value_verified": True, "receipt": "20260814000001",
            "normalization_periods": "2025Q3,2025Q4,2026Q1,2026Q2",
            "normalization_sources": json.dumps([{"quarter": q, "receipt": r} for q, r in [
                ("2025Q3", "20251114000001"), ("2025Q4", "20260314000001"),
                ("2026Q1", "20260514000001"), ("2026Q2", "20260814000001")]]),
            **{f"normalized_sales_q{i}": 6e10 for i in range(1,5)},
            **{f"normalized_op_q{i}": 6e9 for i in range(1,5)},
            "quarter_as_of": "2026-06-30", "sales_quarter_previous": 5e10, "op_quarter_previous": 3e9}


def history(count=65):
    return pd.DataFrame({"date": pd.bdate_range(end=CUTOFF, periods=count), "ticker": "123456",
        "name": "테스트", "sector": "반도체", "market": "KOSDAQ", "open": 100., "high": 102.,
        "low": 98., "close": 100., "adjusted_close": 100., "volume": 100., "value": 2e9,
        "market_cap": 1e12, "price_source": "verified", "value_basis": "actual", "fetched_at": CUTOFF})


def candidate(ticker="123456", sector="반도체"):
    p = price_features(history(), CUTOFF)
    p.update(close=105, ma20=101, ma60=100, breakout=True, distanceResistancePct=3,
             volumeRatio=2, rs20=10, closePosition=.9, drawdown60=-3, support=101,
             distanceSupportPct=3, rangeWidthPct=10, r20=5, change1=1)
    points = forecast_index([consensus(), consensus("2027FY", op=450, sales=3500)], [], CUTOFF)
    f = stock_fundamentals(raw_fundamentals(), points, [], CUTOFF, "123456")
    return {"ticker": ticker, "name": ticker, "sector": sector, "asOf": CUTOFF,
            "price": p, "fundamentals": f, "evidence": []}


class ForecastSelectionTests(unittest.TestCase):
    def test_newest_guidance_and_consensus_are_chosen_without_age_cutoff(self):
        p = forecast_index([consensus(day="2026-01-01")], [guidance(day="2026-02-01")], CUTOFF)
        self.assertEqual(p["123456", "2026FY"]["op"]["value"], 4e10)
        p = forecast_index([consensus(day="2026-03-01")], [guidance(day="2026-02-01")], CUTOFF)
        self.assertEqual(p["123456", "2026FY"]["op"]["kind"], "consensus")

    def test_same_day_official_wins_and_missing_metric_falls_back(self):
        p = forecast_index([consensus()], [guidance(op=None)], CUTOFF)["123456", "2026FY"]
        self.assertEqual(p["sales"]["kind"], "guidance")
        self.assertEqual(p["op"]["kind"], "consensus")

    def test_sales_cannot_borrow_newer_op_report_date(self):
        row = consensus(day="2026-09-10")
        row["estimates"][0]["salesKrw100m"] = None
        row["estimates"].append({"broker": "다른증권", "reportDate": "2026-08-01", "salesKrw100m": 3000,
                                 "operatingProfitKrw100m": 300, "sourceUrl": "https://example.com/older"})
        p = forecast_index([row], [guidance(day="2026-09-01")], CUTOFF)["123456", "2026FY"]
        self.assertEqual(p["sales"]["kind"], "guidance")
        self.assertEqual(p["op"]["kind"], "consensus")

    def test_future_publications_disagreement_and_unattributed_values_rejected(self):
        rows = [consensus(day="2026-09-22"), consensus("2027FY"), consensus("2026Q3")]
        rows[1]["status"] = "불일치 검토"
        rows[2]["estimates"] = []
        self.assertFalse(forecast_index(rows, [guidance(day="2026-09-22")], CUTOFF))

    def test_quarter_forecasts_preserved_as_quarters(self):
        rows = [consensus("2026Q3",op=80),consensus("2026Q4",op=90)]
        points = forecast_index(rows, [], CUTOFF)
        f = stock_fundamentals({}, points, [], CUTOFF, "123456")
        self.assertIsNone(f["annualOP"])
        self.assertEqual([p["period"] for p in f["forecasts"]], ["2026Q3","2026Q4"])

    def test_aggregate_with_hidden_future_component_is_not_reused(self):
        row=consensus()
        row["estimates"].append(dict(row["estimates"][0],reportDate="2026-09-22",broker="미래증권"))
        self.assertFalse(forecast_index([row],[],CUTOFF)["123456","2026FY"])

    def test_actuals_keep_period_specific_disclosure_sources(self):
        rows=actuals(raw_fundamentals(), CUTOFF)
        self.assertEqual(rows[0]["date"], "2025-11-14")
        self.assertEqual(rows[-1]["date"], "2026-08-14")
        raw=raw_fundamentals();raw["receipt"]="20260922000001"
        self.assertEqual(actuals(raw,CUTOFF),[])

    def test_missing_forecast_is_not_zero_and_actual_recovery_still_exists(self):
        f=stock_fundamentals(raw_fundamentals(),{},[],CUTOFF,"123456")
        self.assertIsNone(f["annualOP"])
        self.assertIsNone(f["nextOPGrowthPct"])
        self.assertEqual(f["track"],"실적 회복 확인")
        self.assertTrue(f["growthQualified"])

    def test_small_forward_growth_cannot_borrow_strong_quarter_to_enter_up(self):
        s=candidate()
        points=forecast_index([consensus(),consensus("2027FY",op=305,sales=3100)],[],CUTOFF)
        s["fundamentals"]=stock_fundamentals(raw_fundamentals(),points,[],CUTOFF,"123456")
        assess(s)
        self.assertTrue(s["fundamentals"]["strongActual"])
        self.assertFalse(s["fundamentals"]["growthQualified"])
        self.assertTrue(s["eligible"])
        self.assertFalse(s["scenarios"]["up"]["ready"])
        self.assertEqual(choose([s],"up")["candidateCount"],0)

    def test_missing_forward_sales_does_not_bypass_op_forecast(self):
        row=consensus("2027FY",op=305,sales=3100)
        row["estimates"][0]["salesKrw100m"]=None
        row["salesMedianKrw100m"]=None
        points=forecast_index([consensus(),row],[],CUTOFF)
        f=stock_fundamentals(raw_fundamentals(),points,[],CUTOFF,"123456")
        self.assertTrue(f["opForecastPair"])
        self.assertFalse(f["forwardPair"])
        self.assertTrue(f["strongActual"])
        self.assertFalse(f["growthQualified"])

    def test_single_small_actual_increase_does_not_qualify_without_forecast(self):
        raw=raw_fundamentals();raw["op_quarter_previous"]=5e9
        f=stock_fundamentals(raw,{},[],CUTOFF,"123456")
        self.assertTrue(f["actualImproving"])
        self.assertFalse(f["growthQualified"])

    def test_h2_remainder_does_not_become_q3_forecast(self):
        f=candidate()["fundamentals"]
        self.assertEqual(f["h2ImpliedOP"],3e10-12e9)
        self.assertFalse(any("Q3" in p["period"] for p in f["forecasts"]))

    def test_new_actual_quarter_replaces_q2_for_improvement_checks(self):
        raw=raw_fundamentals()
        raw.update(normalization_periods="2025Q4,2026Q1,2026Q2,2026Q3",receipt="20261114000001",
                   quarter_as_of="2026-09-30",normalized_op_q3=-2e9)
        raw["normalization_sources"]=json.dumps([{"quarter":p,"receipt":"20261114000001"}
                                                for p in raw["normalization_periods"].split(',')])
        f=stock_fundamentals(raw,{},[],"2026-11-20","123456")
        self.assertEqual(f["latestActualPeriod"],"2026Q3")
        self.assertTrue(f["latestWeak"])
        self.assertFalse(f["actualImproving"])

    def test_mismatched_previous_year_quarter_is_not_compared(self):
        raw=raw_fundamentals();raw["quarter_as_of"]="2026-03-31"
        f=stock_fundamentals(raw,{},[],CUTOFF,"123456")
        self.assertIsNone(f["latestOPGrowthPct"])
        self.assertFalse(f["actualImproving"])


class RevisionTests(unittest.TestCase):
    def row(self, day, value, broker="검증증권", period="2026FY"):
        return {"ticker":"123456","broker":broker,"period":period,"report_date":day,
                "operating_profit_krw_100m":value,"confidence":.95,"source_url":"https://example.com/"+day}

    def test_only_same_broker_same_period_revision(self):
        rows=[self.row("2026-06-01",100),self.row("2026-09-01",130),
              self.row("2026-09-01",400,period="2027FY"),self.row("2026-09-01",10,broker="다른증권")]
        r=revision_index(rows,CUTOFF,2026)["123456"]
        self.assertEqual(len(r),1);self.assertAlmostEqual(r[0]["changePct"],30)

    def test_duplicate_and_future_reports_do_not_create_revisions(self):
        r=self.row("2026-09-01",100)
        self.assertFalse(revision_index([r,r,self.row("2026-09-22",200)],CUTOFF,2026))

    def test_conflicting_same_day_values_are_not_averaged(self):
        rows=[self.row("2026-06-01",100),self.row("2026-09-01",120),self.row("2026-09-01",240)]
        self.assertFalse(revision_index(rows,CUTOFF,2026))

    def test_public_aggregate_not_claimed_as_same_broker_revision(self):
        rows=[self.row("2026-06-01",100,broker="FnGuide 공개 집계"),self.row("2026-09-01",120,broker="FnGuide 공개 집계")]
        self.assertFalse(revision_index(rows,CUTOFF,2026))


class PriceAndSelectionTests(unittest.TestCase):
    def test_box_requires_repeated_turns_and_current_lower_quarter(self):
        dates=pd.bdate_range(end=CUTOFF,periods=32).to_series(index=range(32))
        closes=pd.Series([102.]*32);highs=pd.Series([104.]*32);lows=pd.Series([101.]*32)
        for i in (4,16,28): lows.iloc[i]=99.;closes.iloc[i]=101.
        for i in (10,11,12,22,23,24): highs.iloc[i]=113.;closes.iloc[i]=110.
        closes.iloc[-1]=102.;lows.iloc[-1]=100.
        box=confirmed_box(dates,highs,lows,closes,100.,0)
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box["upperTests"],2)
        self.assertLess(box["positionPct"],25)
        for i in (22,23,24): highs.iloc[i]=104.
        self.assertIsNone(confirmed_box(dates,highs,lows,closes,100.,0))

    def test_near_flipped_support_is_not_enough_without_box_lower(self):
        s=candidate();p=s["price"]
        p.update(supportZone={"low":95,"high":100},support=100,distanceSupportPct=2,
                 supportFlipDay="2026-08-01",r20=0,change1=1,closePosition=.9)
        self.assertFalse(assess(s)["scenarios"]["range"]["ready"])
        self.assertEqual(choose([s],"range")["tickers"],[])
        p["box"]={"low":100,"high":112,"positionPct":42,"lowerTests":2,"upperTests":2}
        self.assertFalse(assess(s)["scenarios"]["range"]["ready"])
        p.update(close=102);p["box"]["positionPct"]=17
        self.assertTrue(assess(s)["scenarios"]["range"]["ready"])
        self.assertEqual(choose([s],"range")["tickers"],[s["ticker"]])

    def test_three_and_prior_fourteen_sessions_do_not_overlap(self):
        h=history();h.loc[h.index[-3:],"volume"]=200
        p=price_features(h,CUTOFF)
        self.assertEqual(p["volume3"],200);self.assertEqual(p["volumePrior14"],100)
        self.assertEqual(p["volumeRatio"],2)

    def test_intraday_breakout_is_not_close_breakout(self):
        h=history();h.loc[h.index[-1],"high"]=110
        p=price_features(h,CUTOFF)
        self.assertFalse(p["breakout"]);self.assertEqual(p["swingResistance"],102)
        self.assertIsNone(p["breakoutDay"])

    def test_old_sideways_supply_needs_volume_backed_close_cross(self):
        h=history(100)
        h.loc[:,["low","high"]]=[96.,108.]
        h.loc[h.index[-1],["open","high","low","close","adjusted_close","volume"]]=[100.,113.,99.,112.,112.,250.]
        p=price_features(h,CUTOFF)
        self.assertEqual(p["swingResistance"],108)
        self.assertAlmostEqual(p["resistance"],108,delta=2)
        self.assertTrue(p["breakout"])
        self.assertGreaterEqual(p["breakoutDayVolumeRatio"],2)
        h.loc[h.index[-1],"volume"]=120.
        self.assertFalse(price_features(h,CUTOFF)["breakout"])

    def test_sideways_support_ignores_one_old_low_wick(self):
        h=history(100)
        h.loc[:,["low","high"]]=[96.,108.]
        h.loc[h.index[-10],"low"]=80.
        h.loc[h.index[-1],["close","adjusted_close"]]=[97.,97.]
        p=price_features(h,CUTOFF)
        self.assertEqual(p["swingSupport"],80)
        self.assertIsNone(p["support"])
        self.assertGreater(p["resistanceZone"]["low"],90)
        self.assertGreater(p["resistanceZone"]["days"],40)

    def test_old_resistance_becomes_support_only_after_recorded_breakout(self):
        h=history(120);h.loc[:,["low","high"]]=[96.,108.]
        for i in range(len(h)-15,len(h)):
            h.loc[h.index[i],["open","low","high","close","adjusted_close"]]=[110.,107.,113.,112.,112.]
        h.loc[h.index[-15],"volume"]=250.
        p=price_features(h,CUTOFF)
        self.assertIsNotNone(p["supportFlipDay"])
        self.assertAlmostEqual(p["support"],108,delta=2)
        self.assertLess(p["distanceSupportPct"],5)
        self.assertFalse(p["breakout"])

    def test_recent_low_volume_rise_does_not_become_major_supply_or_support(self):
        h=history(200)
        for i in range(150,200):
            close=105+(i-150)*.5
            h.loc[h.index[i],["open","high","low","close","adjusted_close","volume","value"]]=[
                close,close+1,close-1,close,close,5.,1e8]
        p=price_features(h,CUTOFF)
        self.assertFalse(p["breakout"])
        self.assertIsNone(p["support"])
        self.assertNotEqual(p["resistanceBasis"],"최근 20일 고점")

    def test_old_heavy_supply_outside_recent_120_days_remains_visible(self):
        h=history(200)
        h.loc[h.index[80:],["open","high","low","close","adjusted_close","volume","value"]]=[
            70.,72.,68.,70.,70.,10.,2e8]
        p=price_features(h,CUTOFF)
        self.assertIsNotNone(p["resistanceZone"])
        self.assertGreater(p["resistance"],90)
        self.assertLess(p["resistanceZone"]["lastDate"],h.loc[h.index[-120],"date"].strftime("%Y-%m-%d"))

    def test_future_bars_excluded_and_adjustment_basis_not_false_decline(self):
        h=history();h.loc[h.index[:-1], ["open","high","low","close"]]*=2
        h.loc[h.index[:-1],"adjusted_close"]=100
        future=h.iloc[-1:].copy();future["date"]=pd.Timestamp("2026-09-22");future["close"]=1000
        p=price_features(pd.concat([h,future],ignore_index=True),CUTOFF)
        self.assertAlmostEqual(p["r20"],0);self.assertEqual(p["close"],100)
        self.assertTrue(p["basisChange"]);self.assertIsNone(p["volumeRatio"])

    def test_complete_scenario_conditions_can_qualify(self):
        s=assess(candidate())
        self.assertTrue(s["eligible"])
        self.assertTrue(s["fundamentals"]["upCore"])
        self.assertTrue(s["scenarios"]["up"]["ready"])

    def test_forward_growth_without_strong_actuals_is_not_an_up_pick(self):
        s=candidate();raw=raw_fundamentals();raw["op_quarter_previous"]=5e9
        points=forecast_index([consensus(),consensus("2027FY",op=450,sales=3500)],[],CUTOFF)
        s["fundamentals"]=stock_fundamentals(raw,points,[],CUTOFF,"123456")
        assess(s)
        self.assertTrue(s["fundamentals"]["strongForward"])
        self.assertFalse(s["fundamentals"]["strongActual"])
        self.assertFalse(s["fundamentals"]["upCore"])
        self.assertEqual(choose([s],"up")["candidateCount"],0)

    def test_large_unexplained_h2_jump_needs_matching_quarter_forecasts(self):
        s=candidate()
        points=forecast_index([consensus(op=400),consensus("2027FY",op=600,sales=3500)],[],CUTOFF)
        s["fundamentals"]=stock_fundamentals(raw_fundamentals(),points,[],CUTOFF,"123456")
        assess(s)
        self.assertFalse(s["fundamentals"]["h2BridgeVerified"])
        self.assertFalse(s["fundamentals"]["upCore"])
        points=forecast_index([consensus(op=400),consensus("2027FY",op=600,sales=3500),
            consensus("2026Q3",op=140),consensus("2026Q4",op=140)],[],CUTOFF)
        s["fundamentals"]=stock_fundamentals(raw_fundamentals(),points,[],CUTOFF,"123456")
        assess(s)
        self.assertTrue(s["fundamentals"]["h2BridgeVerified"])
        self.assertTrue(s["fundamentals"]["upCore"])

    def test_financial_sector_uses_a_separate_profit_basis(self):
        s=assess(candidate(sector="금융"))
        self.assertTrue(s["eligible"])
        self.assertFalse(s["fundamentals"]["upCore"])
        self.assertEqual(choose([s],"up")["candidateCount"],0)

    def test_growth_watch_order_ignores_price_proximity(self):
        near=candidate("111111","산업1");near["price"]["breakout"]=False;assess(near)
        far=candidate("222222","산업2")
        far["price"].update(breakout=False,volumeRatio=1,rs20=-2,ma20=110)
        far["fundamentals"]["nextOPDelta"]=near["fundamentals"]["nextOPDelta"]+1e10
        assess(far)
        self.assertGreater(len(far["scenarios"]["up"]["waiting"]),len(near["scenarios"]["up"]["waiting"]))
        self.assertEqual(choose([near,far],"up")["watchTickers"],["222222","111111"])

    def test_up_card_does_not_show_price_conditions_waiting(self):
        s=candidate();s["price"]["breakout"]=False;assess(s)
        self.assertTrue(s["eligible"])
        self.assertFalse(s["scenarios"]["up"]["ready"])
        self.assertEqual(choose([s],"up")["candidateCount"],1)
        self.assertEqual(choose([s],"up")["tickers"],[])
        self.assertEqual(choose([s],"up")["watchTickers"],[s["ticker"]])

    def test_overheat_exception_never_bypasses_other_minimums(self):
        for change in (lambda s:s.update(sector="건축기술 서비스업"),
                       lambda s:s["price"].update(turnover20=100),
                       lambda s:s["price"].update(date="2026-09-18")):
            s=candidate();s["price"]["extended"]=True;change(s);assess(s)
            self.assertFalse(s["heatObservation"])
            self.assertFalse(choose([s],"up")["tickers"])

    def test_valid_heat_exception_is_observation_only(self):
        s=candidate();s["price"]["extended"]=True;assess(s)
        self.assertTrue(s["heatObservation"]);self.assertFalse(s["scenarios"]["up"]["ready"])
        s["price"]["blowoff"]=True;assess(s)
        self.assertFalse(s["heatObservation"])

    def test_sector_diversity_applies_after_qualification_without_filling(self):
        rows=[assess(candidate(str(100000+i),f"산업{i//5}")) for i in range(40)]
        ids=choose(rows,"up")["tickers"]
        self.assertEqual(len(ids),15)
        self.assertEqual(len(choose(rows[:2],"up")["tickers"]),2)

    def test_legacy_scores_and_event_counts_cannot_improve_order(self):
        a=assess(candidate("123456"));b=assess(candidate("234567"))
        before=choose([a,b],"up")["tickers"]
        b.update(valueScore=100000,growthScore=100000,evidence=[{"kind":"수주"}]*500)
        self.assertEqual(choose([a,b],"up")["tickers"],before)

    def test_unknown_market_not_classified_as_range(self):
        self.assertEqual(regime([])["label"],"자료 부족")
        self.assertEqual(regime([price_features(history(),CUTOFF)])["label"],"표본 부족")

    def test_duplicate_economic_event_keeps_one_card(self):
        event={"ticker":"123456","kind":"수주","status":"유효","subject":"공급계약","customer":"A",
               "publishedAt":"2026-09-01","firstPublished":"2026-08-01","url":"https://example.com/a"}
        other=dict(event,url="https://example.com/b",eventId="second")
        self.assertEqual(len(evidence_index([event,other],CUTOFF)["123456"]),1)


class PublicationSafetyTests(unittest.TestCase):
    def test_engine_identity_uses_running_code_not_input_data_folder(self):
        import scenario_engine
        self.assertEqual(engine_hash(),hashlib.sha256(Path(scenario_engine.__file__).read_bytes()).hexdigest())

    def test_validation_rejects_waiting_stock_on_up_card(self):
        s=candidate();s["price"]["breakout"]=False;assess(s)
        board={"meta":{"priceDate":CUTOFF},"holdings":[],"stocks":{s["ticker"]:s},
               "scenarios":{"up":{**choose([s],"up"),"tickers":[s["ticker"]]}}}
        with self.assertRaisesRegex(ValueError,"카드에 대기 후보 노출"):
            validate(board,[])

    def test_shards_and_index_share_generation_and_details_are_lazy(self):
        s=assess(candidate())
        board={"meta":{"snapshotId":"snapshot","engineHash":"engine"},"stocks":{"123456":s},
               "scenarios":{k:choose([s],k) for k in ("up","range","down")}}
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);result=write_public_dataset(out,board,"a"*64)
            small=json.loads((out/"scenario-public.test.json").read_text("utf8"))
            detail=json.loads((out/"scenario-stocks"/("a"*20)/"12.json").read_text("utf8"))
            self.assertNotIn("chart",small["stocks"]["123456"]["price"])
            self.assertEqual(detail["snapshotId"],small["meta"]["snapshotId"])
            self.assertEqual(detail["engineHash"],small["meta"]["engineHash"])
            self.assertIn("chart",detail["stocks"]["123456"]["price"])
            self.assertEqual(result["detailShards"],1)

    def test_holdings_input_change_is_a_hard_error(self):
        s=assess(candidate());holding={"ticker":"123456","name":"test","qty":3,"avg":100}
        board={"meta":{"priceDate":CUTOFF},"holdings":[dict(holding,qty=4)],"stocks":{"123456":s},
               "scenarios":{k:choose([s],k) for k in ("up","range","down")}}
        with self.assertRaisesRegex(ValueError,"보유"):
            validate(board,[holding])


if __name__ == "__main__":
    unittest.main()
