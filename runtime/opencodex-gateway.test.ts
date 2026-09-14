import { afterAll, describe, expect, test } from "bun:test";
import { mkdtempSync, realpathSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import {
  gatewayHomesFromEnv,
  requireDataPlaneToken,
  validateGatewayConfig,
} from "./opencodex-gateway.ts";

const ownedDirectories: string[] = [];
function temporaryDirectory(prefix: string): string {
  const directory = mkdtempSync(join(tmpdir(), prefix));
  ownedDirectories.push(directory);
  return directory;
}
afterAll(() => {
  for (const directory of ownedDirectories) rmSync(directory, { recursive: true, force: true });
});

const validConfig = () => ({
  port: 19100,
  runtimeRole: "hub",
  clientIntegrations: { codex: false, grok: false },
  codexAutoStart: false,
  codexShimAutoRestore: false,
  providers: {
    openai: { disabled: true },
    forge: { adapter: "openai-chat", baseUrl: "https://provider.invalid/v1" },
  },
  defaultProvider: "forge",
});

describe("OpenCodex gateway guards", () => {
  test("accepts separate absolute Forge homes and rejects overlapping homes", () => {
    const root = temporaryDirectory("forge-gateway-");
    expect(gatewayHomesFromEnv({ FORGE_RUNTIME_DATA_ROOT: root })).toEqual({
      opencodexHome: realpathSync(root) + "/opencodex",
      codexHome: realpathSync(root) + "/codex",
    });
    expect(() => gatewayHomesFromEnv({
      FORGE_RUNTIME_DATA_ROOT: root,
      FORGE_CODEX_HOME: root,
    })).toThrow();
  });

  test("rejects broad-root and symlink escapes", () => {
    expect(() => gatewayHomesFromEnv({ FORGE_RUNTIME_DATA_ROOT: "/" })).toThrow();
    const root = temporaryDirectory("forge-gateway-link-");
    const target = temporaryDirectory("forge-gateway-target-");
    const link = join(root, "link");
    symlinkSync(target, link, "dir");
    expect(gatewayHomesFromEnv({
      FORGE_RUNTIME_DATA_ROOT: root,
      FORGE_OPENCODEX_HOME: link,
      FORGE_CODEX_HOME: join(root, "codex"),
    }).opencodexHome).toBe(realpathSync(target));
    const rootLink = join(root, "root-link");
    symlinkSync("/", rootLink, "dir");
    expect(() => gatewayHomesFromEnv({ FORGE_RUNTIME_DATA_ROOT: rootLink })).toThrow();
  });

  test("keeps the first path segment when ancestors do not exist", () => {
    // Path normalization only; no directory is created under the filesystem root.
    const root = `/forge-nonexistent-${randomUUID()}/runtimes`;
    expect(gatewayHomesFromEnv({ FORGE_RUNTIME_DATA_ROOT: root })).toEqual({
      opencodexHome: `${root}/opencodex`,
      codexHome: `${root}/codex`,
    });
  });

  test("requires a valid port and explicit disabled OpenAI provider", () => {
    expect(validateGatewayConfig(validConfig()).port).toBe(19100);
    expect(() => validateGatewayConfig({ ...validConfig(), port: 0 })).toThrow();
    expect(() => validateGatewayConfig({ ...validConfig(), providers: { forge: {} } })).toThrow();
  });

  test("requires Forge-scoped data-plane auth", () => {
    expect(requireDataPlaneToken({ FORGE_OPENCODEX_API_AUTH_TOKEN: " forge-token " })).toBe("forge-token");
    expect(() => requireDataPlaneToken({ OPENCODEX_API_AUTH_TOKEN: "personal-token" })).toThrow();
  });
});
