// @ts-nocheck
import { createAuthMiddleware } from "@better-auth/core/api";
import { describe, expect, it } from "vitest";
import { createAuthClient } from "../../client";
import { getTestInstance } from "../../test-utils/test-instance";

import { getTestCases } from "./validationParams";

describe("allowed_exact_host_resolves", () => {
  const cases = getTestCases();
  it.each(cases)("case $#", async ({ inputs, expected }) => {
    let capturedBaseURL: string | undefined;
    let capturedOptionsBaseURL: unknown;

    const { customFetchImpl } = await getTestInstance({
      baseURL: { allowedHosts: inputs.allowed_hosts },
      hooks: {
        before: createAuthMiddleware(async (ctx) => {
          capturedBaseURL = ctx.context.baseURL;
          capturedOptionsBaseURL = ctx.context.options.baseURL;
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
        "x-forwarded-proto": inputs.forwarded_proto,
      },
    });

    expect(capturedBaseURL).toBe(expected.captured_base_url);
    expect(capturedOptionsBaseURL).toBe(expected.captured_options_base_url);
  }, 60_000);
});
