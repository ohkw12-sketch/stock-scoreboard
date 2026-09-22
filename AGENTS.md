# Canonical scoreboard instructions

- This repository is the only canonical scoreboard source.
- The only canonical deployed scoreboard is `https://stock-scoreboard.pages.dev/`.
- Never modify, deploy, reconnect, or use `semiconductor-scoreboard.ohkw12.chatgpt.site` or the former `semiconductor-scoreboard` Sites project.
- Ignore old scoreboard chats, downloads, archives, and cached previews when deciding what to edit or deploy.
- Apply all Project 1, Project 1-1, Project 2, and Project 3 changes only in this repository and deploy only through its existing Cloudflare Pages deployment.

## Authorized routine work

- For requests to change, build, fix, refresh, or test the scoreboard, Codex may read and edit files in this repository, run the repository's non-destructive setup and test commands, fetch whole-market source data, repair missing records, and write local test outputs without asking again.
- Treat KOSPI and KOSDAQ price, sector, disclosure, and consensus collection as normal in-scope network access for this project.
- Prefer targeted retries and per-stock fallback recovery when a source is incomplete. Never present a failed fetch as "변경 없음".
- Keep credentials out of tracked files. Read DART, consensus-provider, GitHub, or Cloudflare credentials only from the environment, OS credential store, connected app, or CI secret store.
- Do not place orders, access brokerage trading functions, purchase data, delete material data, or broaden access beyond this repository without explicit user authorization.

## Deployment boundary

- Local edits, data refreshes, validation, and test-output generation are authorized by a change/fix request.
- Do not publish, push to the deployment branch, or trigger Cloudflare Pages unless the user explicitly asks to deploy or the active scheduled-task prompt explicitly authorizes deployment after validation.
- When deployment is authorized, update only `ohkw12-sketch/stock-scoreboard`; the existing GitHub-connected Cloudflare Pages project is the only deployment target.
- If recovery cannot obtain required data, update the unaffected sections, retain the last verified values only for affected fields with their original dates, record the missing stocks and attempted sources, and report the unresolved physical limitation to the user.

## Required validation

- Compare requested and collected KOSPI/KOSDAQ ticker sets and record missing ticker names and reasons.
- Verify price dates and consensus dates separately, plus source and fetch status for every retained value.
- Re-run failed tickers through configured alternative sources before accepting a partial result.
- Never label stale, cached, missing, or failed data as current or unchanged.
- Run the relevant tests before any authorized deployment and preserve the previous verified board section when an affected section still fails validation.

## Locked display contract

- `ui_contract.json` is the user-approved source of truth for table titles, column order, and value display formats. Do not change it unless the user explicitly requests a display change.
- Run `python board_contract.py` before deployment. A contract mismatch is a deployment blocker, not an automatic migration opportunity.
- Generate candidates in `test_output/sections/` and promote only the explicitly requested sections with `promote_sections.py`. Never replace the whole live `data.json` for a one-section request.
- Treat `p1`, `p11`, `p2`, `p3`, and `meta` as separately promotable public sections. The former public value and growth sections are internal source calculations for the single `p2` 가치성장 project.
- `p3` quantities and average purchase prices are immutable user inputs. Its judgment, action, fair-range, and display labels are not automatic-refresh outputs.
- Project 2 가치성장 ranks the intersection of the verified value and growth-source candidates: value score 50% + growth score 50% - capped risk penalties. Keep the source value score free of T+, forward earnings, consensus growth, future P/OP, and future discounts. Consensus may appear only as verified growth evidence and receives the configured penalty when it is the sole growth evidence.
- User-approved p11 split (2026-09-19): entry ranks 1–3 retain the four-quarter average sales 50 billion KRW and mean operating margin 15% hard gates. Observation ranks 4–5 may include below-threshold companies only with complete, dated four-quarter financials and verified latest-quarter sales/profit improvement with positive operating profit. All chart, liquidity, heat, sector and theme constraints still apply. Financial observation candidates never automatically become entry candidates. This exception does not apply to p1 or p2.

- Latest p11 display approval: at most FIVE DISTINCT SECTORS, up to THREE qualified stocks per sector. Keep at most three entry-review candidates; allow eligible observation candidates within those sectors, at most fifteen stocks overall. Do not fill when qualifying sectors are unavailable. Rank is display order, not entry eligibility; use entryFit/signal to distinguish entry from observation. This supersedes fixed observation ranks 4–5 and the two-observation limit.

- The upper whole-market rotation-strength board is independent of the five-sector stock list. Show active sectors using existing engine criteria (score >= 58, five-day relative strength > 0, neither early-exit nor ended), displaying only the first ten qualifying sectors (latest user correction). Do not fill missing slots.

## Latest integration approval (2026-09-20)
Supersedes observation exceptions and entry-three cap: retire the separate p1 screen (empty retired payload only for compatibility), use p11 rotation-entry-3.0 for strict entry-qualified stocks only. No financialWatch or watchOnly candidates. Up to five sectors and three stocks each, no forced filling. All displayed candidates retain entry eligibility independent of rank. Keep upper strength display at ten. Combined ranking ignores retired p1 and derives entry state from strict rotation. Preserve historical publications and version new recommendations separately.

## Separate observation area (2026-09-21)
User approved a separate watchCandidates area below strict p11 rows, with a 후보 badge. Restore the previously verified financial-improvement exception only for this separate area. Existing price, liquidity, excluded-sector and heat gates still apply. At most five sectors and three candidates each, no forced filling. These observations never enter p11.rows, combined recommendations or entry performance records. Show explicit watch reasons; preserve strict entry rules.

## Explicit holdings update (2026-09-21)
User supplied two holdings images. For duplicate tickers use the entire lower-average-cost row, never sum quantities. This explicit import is allowed only with `promote_sections.py --sections p3 --holdings-input <local-transcription.json>` matching the approved roster exactly. Subsequent automatic refreshes keep quantity and average cost locked. Never publish raw brokerage screenshots or account identifiers. Below the holdings table, show only dated assessments with concrete sourced facts, separate interpretation and next checks; lack of evidence means omit the assessment, not invent a reason. Price refreshes must preserve assessment dates and sources.

## Future-fundamentals value update (2026-09-22)
This explicit user approval supersedes the older Project 2 restriction against forward estimates. Project 2 value and fundamental prerequisites must use the current year's newest official guidance or external consensus values. For the same period and metric, use the newer publication; official guidance wins on the same date. If neither source provides a usable current-year sales and operating-profit pair, estimate current-year Q3/Q4 from verified current-year Q1/Q2 and the prior year's quarter pattern. Prior-year absolute sales, profit, margin and valuation must not enter the score or hard gates; prior-year quarterly values may supply seasonality ratios only. Missing consensus or guidance is not itself a penalty. Keep the strict rotation p11 reported-four-quarter rules unchanged.
