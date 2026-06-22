// @ts-nocheck
import { createAuthMiddleware } from "@better-auth/core/api";
import { describe, expect, it } from "vitest";
import { createAuthClient } from "../../client";
import { getTestInstance } from "../../test-utils/test-instance";

import { getTestCases } from "./validationParams";

describe("trusted_origins_includes_all_allowed_hosts", () => {
  const cases = getTestCases();
  it.each(cases)("case $#", async ({ inputs, expected }) => {
    let trustedOrigins: string[] = [];

    const { customFetchImpl } = await getTestInstance({
      baseURL: { allowedHosts: inputs.allowed_hosts, fallback: inputs.fallback },
      hooks: {
        before: createAuthMiddleware(async (ctx) => {
          trustedOrigins = [...(ctx.context.trustedOrigins ?? [])];
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

    const requiredOriginsPresentJson = JSON.stringify(
      inputs.required_origins.filter((o: string) => trustedOrigins.includes(o)).sort()
    );

    expect(requiredOriginsPresentJson).toBe(expected.required_origins_present_json);
  }, 60_000);
});
