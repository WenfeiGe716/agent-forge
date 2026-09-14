#!/usr/bin/env bun
/**
 * Agent Forge's gateway-only OpenCodex entrypoint.
 *
 * This deliberately uses OpenCodex's programmatic `startServer` API.  The
 * `ocx start` CLI owns native Codex/Claude/Grok integration and is not a
 * suitable embedded gateway lifecycle.
 */
import {
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, parse, relative, resolve, sep } from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";

type GatewayConfig = {
  port?: unknown;
  hostname?: unknown;
  runtimeRole?: unknown;
  clientIntegrations?: { codex?: unknown; grok?: unknown };
  codexAutoStart?: unknown;
  codexShimAutoRestore?: unknown;
  unauthenticatedLoopbackListener?: { enabled?: unknown };
  providers?: Record<string, unknown>;
  defaultProvider?: unknown;
};

type GatewayHomes = { codexHome: string; opencodexHome: string };

function preparedSource(): string {
  const configured = process.env.FORGE_GATEWAY_SOURCE_ROOT?.trim();
  if (configured && existsSync(join(configured, ".forge-build.json"))) return configured;
  const root = fileURLToPath(new URL("../", import.meta.url));
  const source = join(root, ".local/build/opencodex-gateway-source");
  const manifestPath = join(source, ".forge-build.json");
  if (!existsSync(manifestPath)) {
    throw new Error("run scripts/prepare-opencodex-gateway.sh before starting the isolated gateway");
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const lock = readFileSync(join(root, "third_party/UPSTREAMS.lock"), "utf8");
  const expectedCommit = /^opencodex\.commit = "([a-f0-9]{40})"$/m.exec(lock)?.[1];
  const patch = readFileSync(join(root, "patches/opencodex/0001-require-token-on-loopback.patch"));
  const patchHash = createHash("sha256").update(patch).digest("hex");
  if (!expectedCommit || manifest.upstream_commit !== expectedCommit || manifest.patch_sha256 !== patchHash) {
    throw new Error("OpenCodex prepared source does not match the reviewed upstream/patch lock");
  }
  return source;
}

function canonical(path: string): string {
  let current = resolve(path);
  const tail: string[] = [];
  for (;;) {
    try {
      return join(realpathSync.native(current), ...tail.reverse());
    } catch {
      const parent = dirname(current);
      if (parent === current) return resolve(path);
      tail.push(basename(current));
      current = parent;
    }
  }
}

function contains(parent: string, child: string): boolean {
  const p = canonical(parent);
  const c = canonical(child);
  const difference = relative(p, c);
  return difference === "" || (!isAbsolute(difference) && difference !== ".."
    && !difference.startsWith(`..${sep}`));
}

function requireIsolatedHome(label: string, raw: string): string {
  if (!raw.trim() || !isAbsolute(raw)) {
    throw new Error(`${label} must be an absolute Forge-owned path`);
  }
  const path = canonical(raw);
  const root = parse(path).root;
  const personalHome = canonical(homedir());
  const personalCodex = canonical(join(personalHome, ".codex"));
  const personalOpenCodex = canonical(join(personalHome, ".opencodex"));
  // Reject broad roots and every path that is, or is beneath, a personal home.
  if (path === root || path === personalHome
      || contains(personalCodex, path) || contains(personalOpenCodex, path)
      || contains(path, personalCodex) || contains(path, personalOpenCodex)) {
    throw new Error(`${label} resolves to a personal or broad filesystem root; refusing gateway start`);
  }
  return path;
}

export function validateGatewayConfig(value: unknown): GatewayConfig {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("gateway config must be a JSON object");
  }
  const config = value as GatewayConfig;
  if (config.runtimeRole !== "hub") throw new Error("gateway config must set runtimeRole=hub");
  if (config.clientIntegrations?.codex !== false || config.clientIntegrations?.grok !== false) {
    throw new Error("gateway config must disable Codex and Grok client integrations");
  }
  if (config.codexAutoStart !== false || config.codexShimAutoRestore !== false) {
    throw new Error("gateway config must disable Codex autostart and shim restoration");
  }
  if (config.unauthenticatedLoopbackListener?.enabled === true) {
    throw new Error("gateway config must not enable an unauthenticated loopback listener");
  }
  const hostname = typeof config.hostname === "string" ? config.hostname.trim().toLowerCase() : "127.0.0.1";
  if (hostname !== "127.0.0.1" && hostname !== "localhost" && hostname !== "::1") {
    throw new Error("gateway-only listener must bind to loopback");
  }
  if (typeof config.port !== "number" || !Number.isInteger(config.port) || config.port < 1 || config.port > 65535) {
    throw new Error("gateway config must contain an integer port in the range 1..65535");
  }
  if (!config.providers || typeof config.providers !== "object" || Array.isArray(config.providers)
      || typeof config.defaultProvider !== "string" || !config.providers[config.defaultProvider]) {
    throw new Error("gateway config must contain a valid default provider");
  }
  const openai = config.providers.openai;
  if (!openai || typeof openai !== "object" || Array.isArray(openai)
      || (openai as { disabled?: unknown }).disabled !== true) {
    throw new Error("gateway config must explicitly disable the default OpenAI provider");
  }
  return config;
}

function readGatewayConfig(opencodexHome: string): GatewayConfig {
  const path = join(opencodexHome, "config.json");
  if (!existsSync(path)) throw new Error(`missing gateway config: ${path}`);
  const resolvedConfig = canonical(path);
  if (!contains(opencodexHome, resolvedConfig)) {
    throw new Error("gateway config symlink escapes the Forge OpenCodex home");
  }
  let value: unknown;
  try {
    value = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`invalid gateway config: ${error instanceof Error ? error.message : String(error)}`);
  }
  return validateGatewayConfig(value);
}

/** Keep only non-secret runtime values and Forge-prefixed provider variables. */
export function requireDataPlaneToken(env: NodeJS.ProcessEnv = process.env): string {
  const token = env.FORGE_OPENCODEX_API_AUTH_TOKEN?.trim();
  if (!token) throw new Error("FORGE_OPENCODEX_API_AUTH_TOKEN is required for the gateway data plane");
  return token;
}

function scrubEnvironment(opencodexHome: string, codexHome: string): void {
  const inherited = { ...process.env };
  const keep = new Set([
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP",
    "SYSTEMROOT", "SystemRoot", "COMSPEC",
  ]);
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(inherited)) {
    if (value !== undefined && (keep.has(key) || key.startsWith("FORGE_PROVIDER_"))) env[key] = value;
  }
  // Provider credentials must be explicitly injected by Forge under the
  // FORGE_PROVIDER_* namespace; arbitrary OPENAI_API_KEY/OAUTH variables never
  // cross this boundary.  Network proxies also require an explicit Forge value.
  for (const [source, target] of [["FORGE_HTTP_PROXY", "HTTP_PROXY"], ["FORGE_HTTPS_PROXY", "HTTPS_PROXY"], ["FORGE_NO_PROXY", "NO_PROXY"]]) {
    const value = inherited[source];
    if (value?.trim()) env[target] = value;
  }
  env.HOME = opencodexHome;
  env.USERPROFILE = opencodexHome;
  env.CODEX_HOME = codexHome;
  env.OPENCODEX_HOME = opencodexHome;
  env.OPENCODEX_API_AUTH_TOKEN = requireDataPlaneToken(inherited);
  env.CLAUDE_CONFIG_DIR = join(opencodexHome, "claude");
  env.GROK_HOME = join(opencodexHome, "grok");
  env.CLOUDSDK_CONFIG = join(opencodexHome, "gcloud");
  env.XDG_CONFIG_HOME = join(opencodexHome, "xdg-config");
  env.XDG_DATA_HOME = join(opencodexHome, "xdg-data");
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, env);
}

export function gatewayHomesFromEnv(env: NodeJS.ProcessEnv = process.env): GatewayHomes {
  const rootRaw = env.FORGE_RUNTIME_DATA_ROOT?.trim();
  if (!rootRaw || !isAbsolute(rootRaw)) {
    throw new Error("FORGE_RUNTIME_DATA_ROOT must be an absolute path");
  }
  const root = requireIsolatedHome("FORGE_RUNTIME_DATA_ROOT", rootRaw);
  const opencodexHome = requireIsolatedHome("FORGE_OPENCODEX_HOME", env.FORGE_OPENCODEX_HOME?.trim() || join(root, "opencodex"));
  const codexHome = requireIsolatedHome("FORGE_CODEX_HOME", env.FORGE_CODEX_HOME?.trim() || join(root, "codex"));
  if (contains(opencodexHome, codexHome) || contains(codexHome, opencodexHome)) {
    throw new Error("Forge Codex and OpenCodex homes must be separate directories");
  }
  return { codexHome, opencodexHome };
}

export async function runGateway(): Promise<void> {
  const source = preparedSource();
  const homes = gatewayHomesFromEnv();
  const config = readGatewayConfig(homes.opencodexHome);
  mkdirSync(homes.opencodexHome, { recursive: true, mode: 0o700 });
  mkdirSync(homes.codexHome, { recursive: true, mode: 0o700 });
  scrubEnvironment(homes.opencodexHome, homes.codexHome);

  // A malformed provider row makes upstream loadConfig fall back to factory
  // defaults. Reject with the upstream schema BEFORE starting any service.
  const { validateConfigCandidate } = await import(pathToFileURL(join(source, "src/config.ts")).href);
  const candidate = validateConfigCandidate(config);
  if (!candidate.ok) throw new Error(`upstream gateway config rejected: ${candidate.error}`);
  validateGatewayConfig(candidate.config);
  const { startServer } = await import(pathToFileURL(join(source, "src/index.ts")).href);
  const server = startServer(typeof config.port === "number" ? config.port : undefined, {
    // Version-pinned adaptation seams: upstream labels these test-only.
    // Re-audit them on upgrades; never fall back to native-service inspection.
    resolveServiceHomes: () => homes,
    inspectNativeCodexOwnership: () => ({ ownership: "foreign", reason: "Agent Forge gateway-only runtime" }),
  });
  const stop = async () => {
    try { await server.stop(true); } finally { process.exit(0); }
  };
  process.once("SIGINT", () => void stop());
  process.once("SIGTERM", () => void stop());
  await new Promise<void>(() => {});
}

if (import.meta.main) {
  runGateway().catch(error => {
    console.error(`Agent Forge OpenCodex gateway refused to start: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  });
}
