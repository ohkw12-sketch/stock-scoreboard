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
- Treat `p1`, `p11`, `p2`, `growth`, `p3`, and `meta` as separately promotable sections. A growth-engine change must not change value, entry, rotation, holdings, or global metadata unless those sections were also explicitly requested.
- `p3` quantities and average purchase prices are immutable user inputs. Its judgment, action, fair-range, and display labels are not automatic-refresh outputs.
