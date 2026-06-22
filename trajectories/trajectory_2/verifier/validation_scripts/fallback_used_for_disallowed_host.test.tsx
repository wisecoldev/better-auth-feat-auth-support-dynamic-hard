// @ts-nocheck
import { createAuthMiddleware } from "@better-auth/core/api";
import { describe, expect, it } from "vitest";
import { createAuthClient } from "../../client";
import { getTestInstance } from "../../test-utils/test-instance";

import { getTestCases } from "./validationParams";

describe("fallback_used_for_disallowed_host", () => {
  const cases = getTestCases();
  it.each(cases)("case $#", async ({ inputs, expected }) => {
    let capturedBaseURL: string | undefined;

    const { customFetchImpl } = await getTestInstance({
      baseURL: { allowedHosts: inputs.allowed_hosts, fallback: inputs.fallback },
      hooks: {
        before: createAuthMiddleware(async (ctx) => {
          capturedBaseURL = ctx.context.baseURL;
        }),
      },
    });

    const client = createAuthClient({
      baseURL: "http://localhost:3000",
      fetchOptions: { customFetchImpl },
    });

    await client.$fetch("/ok", {
      headers: {
        "x-forwarded-host": inputs.forwarded_host,
        "x-forwarded-proto": "https",
      },
    });

    expect(capturedBaseURL).toBe(expected.captured_base_url);
  }, 60_000);
});
