# Six-fixture comparison: vanilla `claude` vs `claude-appmap-3step`

Run date: 2026-05-04. Model: `claude-opus-4-7` for all backends/runs.
Each fixture run once per backend; per-run wall and cost from
`work/<backend>/synth__<fixture>/<ts>/usage.json`, verdicts from
`synth_verdict.json` (the hidden post-extraction verify test).

## Results

| Fixture | Difficulty | Backend | Verdict | Wall | Msgs | Cost | Patch |
|---|---|---|---|---|---|---|---|
| oscar_4016 | hard / logic | vanilla | **❌ FAIL** | 16.2m | 130 | $30.55 | 4298c, 2f (wrong file) |
| oscar_4016 | hard / logic | 3-step | ✅ PASS | 12.1m | 151 | $18.82 | 1036c, 1f |
| oscar_n1_basket | medium / perf | vanilla | ✅ PASS | 15.8m | 130 | $29.71 | 2264c, 3f |
| oscar_n1_basket | medium / perf | 3-step | ✅ PASS | 12.7m | 166 | $21.10 | 1632c, 2f |
| oscar_partner_orders_leak | medium / auth | vanilla | ✅ PASS | 1.9m | 36 | $3.15 | 666c, 1f |
| oscar_partner_orders_leak | medium / auth | 3-step | ✅ PASS | 4.7m | 64 | $7.19 | 666c, 1f |
| oscar_dup_order_email | medium / signal | vanilla | ✅ PASS | 1.7m | 28 | $3.23 | 926c, 1f |
| oscar_dup_order_email | medium / signal | 3-step | ✅ PASS | 6.0m | 89 | $8.55 | 926c, 1f |
| oscar_voucher_case | easy / grep | vanilla | ✅ PASS | 2.1m | 40 | $3.92 | 458c, 1f |
| oscar_voucher_case | easy / grep | 3-step | ✅ PASS | 5.5m | 80 | $8.16 | 458c, 1f |
| oscar_discount_rounding | medium / numerical | vanilla | ✅ PASS | 1.2m | 21 | $1.96 | 780c, 1f |
| oscar_discount_rounding | medium / numerical | 3-step | ✅ PASS | 3.3m | 52 | $4.62 | 780c, 1f |

## Aggregate

|  | Vanilla | 3-step |
|---|---|---|
| Pass rate | 5/6 | **6/6** |
| Total cost | $72.52 | **$68.44** (-5.6%) |
| Total wall | 39 min | 44 min (+13%) |

## Per-fixture cost ratio

| Fixture | Vanilla | 3-step | Ratio | Comment |
|---|---|---|---|---|
| oscar_4016 | $30.55 | $18.82 | **3-step 38% cheaper** | Vanilla failed (wrong file) |
| oscar_n1_basket | $29.71 | $21.10 | **3-step 29% cheaper** | Both pass; 3-step tighter patch |
| oscar_partner_orders_leak | $3.15 | $7.19 | 3-step 2.3× | Vanilla wins |
| oscar_dup_order_email | $3.23 | $8.55 | 3-step 2.6× | Vanilla wins |
| oscar_voucher_case | $3.92 | $8.16 | 3-step 2.1× | Vanilla wins |
| oscar_discount_rounding | $1.96 | $4.62 | 3-step 2.4× | Vanilla wins |

## Findings

**1. The 3-step architecture buys reliability, not raw speed.** 3-step
is 6/6 vs vanilla's 5/6. Vanilla's failure on oscar_4016 ($30.55 burned
on a wrong-file edit) is the dominant cost item across the matrix — if
you remove that single failure, vanilla totals $42 and 3-step totals
$50 on the bugs vanilla solves. So 3-step is more expensive on solvable
bugs, but solves bugs vanilla doesn't.

**2. 3-step has a fixed overhead of ~$5–8 per fixture.** Two extra
claude invocations (RCA dispatch + verify dispatch) and their subagent
contexts. That overhead doesn't pay off when the bug is grep-discoverable.

**3. Both backends produce byte-identical patches on the easy bugs.**
On 4 of 6 fixtures (partner_orders_leak, dup_order_email, voucher_case,
discount_rounding) the patches match exactly. The two backends differ
only on the structurally-hard ones (oscar_4016, n1_basket), where 3-step
produces a tighter patch with fewer files touched.

**4. Bug discoverability predicts the winner.** Single-line typos in
clearly-named helper functions (clean_code, queryset_orders_for_user,
Benefit.round) yield to grep + reading. AppMap's runtime grounding
provides no additional signal because there's no structural-shape
question to answer. The two bugs where 3-step wins decisively
(oscar_4016, n1_basket) both involve symptom-vs-location gap or
multi-file fix surface.

## Surprises against initial expectations

- **partner_orders_leak ended up "easy."** I'd classified it as
  hard / multi-file in `synth_bugs/PROPOSED.md`. Both backends solved
  it in 1-3 min with a 666-char patch. The bug is `Partner.objects.all()`
  vs `.filter(users=user)` — a single-line oddity that grep finds in
  the right file. The "multi-tenant" framing made it sound harder than
  it was.

- **discount_rounding was the cheapest fixture for both backends.**
  Single-file, single-import bug; both solved it in <4 min. Theoretically
  AppMap-friendly (numerical traces), but the bug is small enough that
  even vanilla nails it via grep on `ROUND_DOWN`.

## Implication

Bug difficulty for an LLM-driven solver isn't well-modeled by the
"developer-effort" axis (single-tenant vs multi-tenant, simple vs
complex domain). It's better modeled by the **symptom-to-location gap**:
how many plausibly-wrong code paths could produce the reported symptom,
and how plausibly the agent picks one of the wrong ones. On
oscar_4016 the symptom (two exclusive vouchers stack) plausibly points
at the applicator (where iteration happens) — vanilla went there and
over-edited. The actual bug is one layer down at the consumer
guard. On the four "easy" bugs the symptom points directly at the
bug's function.

A natural next iteration would be to test a **conditional 3-step
strategy**: have the appmap-rca subagent's first action be to decide
"is this answerable from the issue text + a quick source read, or do
I need a recording?" If the former, return immediately and let main
proceed vanilla-style. If the latter, do the full RCA. That collapses
the 3-step's fixed overhead on grep-discoverable bugs while preserving
the reliability win on hard ones.
