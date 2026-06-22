# Review: `better-auth-feat-auth-support-dynamic` (hard) task submission

> A SWE-bench-style **task-authoring submission**, reverse-engineered from a real merged PR
> (better-auth/better-auth #8009). This reviews the authored benchmark task, not a solution to it.

## What this is

An authored agentic-coding **feature** task. The brief: widen `BetterAuthOptions.baseURL` from a
static string to also accept a per-request allowlist config (`{ allowedHosts, fallback?, protocol? }`),
resolve each incoming request's origin against the allowlist (wildcards, fallback, explicit
protocol), and propagate that per-request origin to **every** existing string consumer of
`options.baseURL` — cookies, trusted origins, JWT issuer/audience, the MCP/OIDC/oauth-proxy URL
builders, svelte-kit, passkey rpID, telemetry (~9 sites). The static-string shape must keep working
unchanged. Motivated by Vercel-style multi-domain deployments. Oracle: 529 SLOC / 17 files / 32
hunks. Verified by a deterministic vitest regression test + 8 runtime validation stories + a
non-gating LLM rubric.

## Verdict

**A well-crafted, well-motivated hard task with a genuinely race-safe oracle — undermined by a
structural grading defect that the trajectory evidence proves is load-bearing.** The binary reward
(`reward = 1.0 iff verifier passes AND validation_score ≥ 1.0`,
[run_aggregate.py](tests/run_aggregate.py) lines 107-109) cannot measure the task's two hardest,
security-relevant requirements — **per-request concurrency isolation** and **avoiding `[object Object]`
consumer coercion** — and the rubric that *does* catch them is informational-only (line 179). A
recorded solver shipped the exact forbidden shared-context race and still earned reward 1.0.

---

## Strengths

**1. The oracle's per-request isolation is genuinely race-safe.** [oracle.patch](solution/oracle.patch)
(`auth/base.ts`) derives a per-request context via `handlerCtx = Object.create(ctx)` and writes a
**fresh** `{ ...ctx.options, baseURL }` object rather than mutating `ctx.options.baseURL` in place,
so concurrent requests resolving different hosts can't clobber each other while request-invariant
fields are read through the prototype chain. Consumer narrowing in the oracle is complete and
coercion-safe across all ~9 sites via `typeof x === "string" ? x : fallback`. This is the correct,
non-obvious solution to the concurrency hazard.

**2. Honest, abstract solver-facing framing with no solution leak.** The solver receives only
[instruction.md](instruction.md) — the Vercel motivation plus 8 behavior-phrased user stories (I
verified this against the solver transcript: the richer [details.md](details.md), which names the
reuse target and the isolation technique, is **reviewer-facing and withheld** from the solver). So
the genuine discovery work — finding all ~9 consumers, recognizing the concurrency hazard, choosing
an isolation technique — is preserved. The difficulty is honestly represented.

**3. User stories are written as observable behaviors, and the naive-failure enumeration is
exceptional.** [instruction.md](instruction.md) lines 19-26 phrase each story as an externally
observable outcome (rejection message names the rejected host; two requests produce different cookie
`Domain=`). [task.toml](task.toml) lines 182-205 list seven falsifiable failure modes — shared-ctx
caching, per-request mutation race, missing cookie recompute, missing `trustedOrigins` population,
missing `http://` localhost variant, `[object Object]` coercion, empty-`allowedHosts` validation —
each tied to a story. The accepted-alternatives list (lines 167-180) pre-empts grading disputes by
admitting three valid isolation techniques and helper-naming freedom.

**4. Complete 1:1 story-to-validation coverage with anti-trivial-pass assertions.** All 8 user
stories have a dedicated [validation_spec.toml](tests/validate/validation_spec.toml) block (matching
IDs, 2 cases each, distinct fixtures), asserting on real captured `AuthContext` fields with
substring-or-full-message and sorted-JSON-subset idioms so failures surface the actual value. The
`http://` localhost `trustedOrigins` variant and per-request cookie `Domain` *are* exercised.

**5. The rubric layer is genuinely discriminating — it just doesn't count.** The keyed rubric
correctly separates trajectory_1 (`per_request_isolation = 0.0`, rubric 0.5) from trajectory_2
(`per_request_isolation = 1.0`, rubric 0.833). The discriminating signal exists; it simply never
enters the reward.

---

## Weaknesses

**1. (High) The reward gate is structurally blind to concurrency isolation — and a recorded solver
shipped the forbidden race at reward 1.0.** No validation story issues overlapping in-flight
requests ([validation_spec.toml](tests/validate/validation_spec.toml) story 8, line 569, is
explicitly *two sequential* fetches; there is no `Promise.all` anywhere), and
[verify.test.ts](tests/verify/verify.test.ts) only uses the static-string `baseURL`. The only
coverage is the non-gating rubric. **Proof it is load-bearing:** trajectory_1's `agent.patch`
(`auth/base.ts`) mutates the *shared* context every request — `ctx.baseURL = …`,
`ctx.options.baseURL = …`, `ctx.authCookies = …` — the exact naive shape the task warns against
([task.toml](task.toml) "what naive implementations get wrong" #2). It scored rubric
`per_request_isolation = 0.0`, yet earned **reward 1.0** (verifier 1.0, validation 1.0). The hardest,
most security-relevant requirement is unmeasured by the gate.

**2. (High) `[object Object]` consumer coercion is gated by nothing — both trajectories shipped a
partial-narrowing defect at reward 1.0.** All 8 stories assert only on
`baseURL`/`options.baseURL`/`trustedOrigins`/cookie-domain; none drives a JWT issuer, passkey rpID,
telemetry project ID, or MCP/OIDC/oauth-proxy URL builder under the dynamic config, so the coercion
hazard ([task.toml](task.toml) lines 199-202) is never exercised. The stated safety net — "the
workspace TypeScript build / `pnpm typecheck` stays green" — appears only inside the rubric
description text and is **never executed by any harness step** ([test.sh](tests/test.sh) runs no
`tsc`; the `dist/` build is skipped when present; validation files are `// @ts-nocheck`). Both
trajectories left `svelte-kit.ts` and `mcp/index.ts` un-narrowed (`consumer_type_safety = 0.5`) and
both earned reward 1.0.

**3. (High, structural cause) The reward computation ignores the rubric entirely.**
[run_aggregate.py](tests/run_aggregate.py) lines 107-109 compute reward solely from
`verifier_ok AND validation_ok`; rubric/rubric_all are written only to `reward_details.json` and are
explicitly informational (line 179). Consequence: a 0.0 on the hardest criterion *ties* a perfect
score (trajectory_1 rubric 0.5 and trajectory_2 rubric 0.833 both reward 1.0). This is the structural
root of weaknesses 1 and 2.

**4. (Medium) The cookie-domain story can't distinguish per-request isolation from a racy shared
mutation.** Story 8 issues two *sequential* requests and asserts the set of captured `Domain`
attributes equals both hosts — a handler that mutates shared `ctx.authCookies` per request passes
*identically* to one using `Object.create` isolation. It also reads the internal
`authCookies.sessionToken.attributes.domain` object rather than an actual `Set-Cookie` wire header,
so a recompute-but-never-apply bug would still pass.

**5. (Medium) The `trustedOrigins` story asserts list membership, not CSRF enforcement.** Story 7
snapshots `ctx.context.trustedOrigins` and checks required origins are present, but never drives a
cross-origin request through the CSRF middleware to confirm an allowed host is accepted / a
non-listed one rejected — the actual enforcement rationale. List membership is a strong proxy, but
the observable behavior is unverified.

**6. (Low) Minor harness/oracle nits.** An error story accepts the bare substring `"allowed"`
(story 1 case 2), which any "host not allowed" message satisfies rather than naming the empty
`allowedHosts` field (mitigated: case 1 still requires the strict `allowedhosts` substring). Oracle:
the request host's `:port` is not stripped, so a portless pattern fails a `host:port` request
(largely by-design for non-default ports); the cookie `Secure` flag is frozen at init while the
domain is recomputed per request (documented asymmetry); the static branch retains a documented
benign first-request shared-ctx mutation; `matchesHostPattern` redundantly double-lowercases and the
fallback/protocol URL parsers use silent empty `catch` blocks. `rubric.json` (3 criteria) is a strict
subset of `rubric_all.json` (11) with no in-file precedence note.

> **Findings I investigated and dropped:** the analysis initially flagged "details.md over-leaks the
> solution to the solver" and a "provenance/mixed-task" defect. Both are **incorrect**: I verified
> the solver receives only `instruction.md` (details.md is withheld), and the provenance concern was
> an artifact of inspecting the wrong working directory. A `resolveBaseURL`/`resolveDynamicBaseURL`
> parameter-order "footgun" and an `x-forwarded-proto` comma-list bug were also refuted (the type
> system and `validateProxyHeader` respectively prevent them).

---

## Dimension summary

| Dimension | Assessment |
|---|---|
| **Code quality (oracle)** | Sound and race-safe (`Object.create` + fresh `options` spread); complete, coercion-safe consumer narrowing. Minor low-severity nits (port handling, secure-flag asymmetry, double-lowercase, silent catches). |
| **Testing approach** | Coherent two-layer split (deterministic static-string regression + 8 anti-overfit runtime stories), but the deterministic verifier gates zero forward-pass behavior, no story exercises concurrency or consumer coercion, and the cookie-domain story is sequential-only. |
| **Problem-solving** | Strong: defense-in-depth host validation, precise type guard, early init-time validation; genuinely discriminating in the rubric layer. |
| **Maintainability** | Reasonable; the dynamic-isolated vs static-mutating branch asymmetry is a documented smell; rubric duplication and inconsistent token/cost accounting are artifact-hygiene nits. |
| **Communication** | Strong and honest — concrete motivation, behavior-phrased stories, thorough naive-failure list, and (verified) no solution leak to the solver. |

## If I were to prioritize fixes

1. **Add a deterministic concurrency gate:** issue overlapping `auth.handler` requests for two
   distinct allowed hosts (`Promise.all`) and assert neither sees the other's resolved
   `baseURL`/cookie `Domain`. This single fix would have failed trajectory_1's shared-ctx race
   instead of awarding it reward 1.0.
2. **Gate consumer coercion:** drive at least one JWT sign/verify, passkey rpID, and
   telemetry/MCP/OIDC path under the dynamic config and assert the emitted value is a real URL, not
   `"[object Object]"` — and actually **run `pnpm typecheck`** as an enforced gate (today it is only
   named in rubric text and bypassed by `// @ts-nocheck`).
3. **Fold the load-bearing rubric criteria** (`per_request_isolation`, `consumer_type_safety`) into
   the reward, or replace them with deterministic gates, so a 0.0 on the hardest requirement can no
   longer tie a perfect score.
4. **Strengthen story 8** to assert on the real `Set-Cookie` wire header and add a concurrent
   variant; **upgrade story 7** to drive a real cross-origin request through CSRF; tighten the
   empty-`allowedHosts` assertion from `"allowed"` to `"allowedhosts"`.

Net: a strong, well-communicated task with a correct, race-safe reference solution — but its binary
reward is gameable on exactly the two requirements that make it "hard," and the trajectories prove a
solver can ship the forbidden race and an incomplete narrowing at a perfect score. Folding the
concurrency and coercion checks into the gate would move this from a gameable benchmark to a strongly
discriminating one.
