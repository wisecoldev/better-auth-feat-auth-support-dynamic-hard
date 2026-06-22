// @ts-nocheck
//
// Behavioural verifier for better-auth-feat-auth-support-dynamic (PR #8009).
//
// Pass-to-pass regression guard: configuring `baseURL` as the existing
// static string still produces a working auth instance — sign-up, sign-in,
// and session-retrieval through the pre-existing `auth.api` surface work
// end-to-end. Catches implementations that "fix" the dynamic-baseURL
// shape by quietly breaking the static-string shape (forgetting the
// branch, mistyped narrowing, etc.).
//
// All forward-pass behaviour (allowlist matching, fallback, protocol,
// trustedOrigins expansion, per-request cookie domain) is exercised by
// the validation_spec stories. We don't duplicate them here.
//
// We import only pre-existing public modules (`../test-utils/test-instance`
// is the same path that pre-fix tests like `full.test.ts:8` use). No
// task-introduced helper / type / file path is referenced.
//
// File location at runtime: this file is copied into
// /repo/better-auth/packages/better-auth/src/auth/verify.test.ts by
// the harbor JS runner (see verify.toml: test_parent =
// "packages/better-auth/src/auth"), matching the directory of
// `full.test.ts` so its relative imports resolve identically.

import { describe, expect, it } from "vitest";
import { getTestInstance } from "../test-utils/test-instance";

describe("baseURL static-string regression guard", () => {
	it("static string baseURL: getTestInstance + signInEmail works end-to-end", async () => {
		const { auth, testUser } = await getTestInstance({
			baseURL: "http://localhost:3000",
		});

		const result = await auth.api.signInEmail({
			body: {
				email: testUser.email,
				password: testUser.password,
			},
		});

		expect(result.user).toBeDefined();
		expect(result.user.email).toBe(testUser.email);
	}, 60_000);

	it("static string baseURL: getSession returns a session for the signed-in user", async () => {
		const { auth, testUser, cookieSetter } = await getTestInstance({
			baseURL: "http://localhost:3000",
		});

		const signInRes = await auth.api.signInEmail({
			body: {
				email: testUser.email,
				password: testUser.password,
			},
			asResponse: true,
		});

		const headers = new Headers();
		cookieSetter(headers)({ response: signInRes });

		const session = await auth.api.getSession({ headers });
		expect(session).toBeDefined();
		expect(session?.user?.email).toBe(testUser.email);
	}, 60_000);
});
