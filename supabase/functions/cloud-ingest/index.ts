// GitHub OIDC -> allow-listed Supabase RPC gateway.
// Existing read/write operations remain compatible while venue publication is additive.
const ALLOWED_TYPES = new Set([
  "cloud_racecard", "cloud_races_index", "cloud_acquisition_manifest",
  "cloud_race_result", "cloud_race_payout", "cloud_roster_observation",
  "cloud_race_comment", "cloud_comment_manifest",
]);
const GITHUB_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks";
const JWKS_TTL_MS = 10 * 60 * 1000;
let jwksCache: { fetchedAt: number; keys: (JsonWebKey & { kid?: string })[] } | null = null;

function base64url(input: string): ArrayBuffer {
  const text = input.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(input.length / 4) * 4, "=");
  return Uint8Array.from(atob(text), (c) => c.charCodeAt(0)).buffer as ArrayBuffer;
}

async function loadGithubJwks(forceRefresh = false) {
  if (!forceRefresh && jwksCache && Date.now() - jwksCache.fetchedAt < JWKS_TTL_MS) return jwksCache.keys;
  const response = await fetch(GITHUB_JWKS_URL, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error("github oidc jwks unavailable");
  const body = await response.json();
  if (!Array.isArray(body.keys) || body.keys.length === 0) throw new Error("github oidc jwks invalid");
  jwksCache = { fetchedAt: Date.now(), keys: body.keys };
  return jwksCache.keys;
}

async function verifySignature(parts: string[], kid: string, forceRefresh = false) {
  const keys = await loadGithubJwks(forceRefresh);
  const jwk = keys.find((key) => key.kid === kid);
  if (!jwk) return false;
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, base64url(parts[2]), new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
}

async function verifyGithubOidc(token: string) {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("invalid oidc token");
  const header = JSON.parse(new TextDecoder().decode(base64url(parts[0])));
  const claims = JSON.parse(new TextDecoder().decode(base64url(parts[1])));
  if (header.alg !== "RS256" || !header.kid) throw new Error("unsupported oidc algorithm");
  let valid = await verifySignature(parts, header.kid);
  if (!valid) valid = await verifySignature(parts, header.kid, true);
  if (!valid) throw new Error("oidc signature mismatch");
  const now = Math.floor(Date.now() / 1000);
  const expected = {
    audience: Deno.env.get("CLOUD_INGEST_AUDIENCE"),
    repository: Deno.env.get("ALLOWED_GITHUB_REPOSITORY"),
    ref: `refs/heads/${Deno.env.get("ALLOWED_GITHUB_BRANCH")}`,
    workflowRefs: [
      Deno.env.get("ALLOWED_WORKFLOW_REF"),
      Deno.env.get("ALLOWED_COMMENT_WORKFLOW_REF"),
      Deno.env.get("ALLOWED_RESULT_WORKFLOW_REF"),
    ],
    environment: Deno.env.get("ALLOWED_GITHUB_ENVIRONMENT"),
    subject: Deno.env.get("ALLOWED_GITHUB_SUBJECT"),
  };
  const scalarPolicy = [expected.audience, expected.repository, expected.ref, expected.environment, expected.subject];
  if (scalarPolicy.some((value) => !value || value.includes("undefined")) ||
      expected.workflowRefs.some((value) => !value || value.includes("undefined")) ||
      new Set(expected.workflowRefs).size !== expected.workflowRefs.length) {
    throw new Error("oidc policy is not configured");
  }
  const workflowRef = claims.job_workflow_ref || claims.workflow_ref;
  const exp = Number(claims.exp || 0);
  const nbf = Number(claims.nbf || 0);
  const iat = Number(claims.iat || 0);
  if (claims.iss !== "https://token.actions.githubusercontent.com" || claims.aud !== expected.audience ||
      claims.repository !== expected.repository || claims.ref !== expected.ref ||
      !expected.workflowRefs.includes(workflowRef) || claims.environment !== expected.environment ||
      claims.sub !== expected.subject || !/^\d+$/.test(String(claims.run_id || "")) ||
      !/^\d+$/.test(String(claims.run_attempt || "")) || exp <= now || nbf > now + 30 ||
      iat > now + 30 || iat < now - 600 || exp - iat > 900) {
    throw new Error("oidc claims rejected");
  }
  return claims;
}

function validIdentity(type: string, key: string) {
  if (!ALLOWED_TYPES.has(type)) return false;
  const dated = type === "cloud_races_index" || type === "cloud_acquisition_manifest" || type === "cloud_comment_manifest";
  return dated ? /^\d{4}-\d{2}-\d{2}$/.test(key) : /^\d{12}$/.test(key);
}

function validVenueIdentity(body: Record<string, unknown>) {
  return body.dataset_family === "racecard" && /^\d{4}-\d{2}-\d{2}$/.test(String(body.race_date || "")) &&
    /^\d{2}$/.test(String(body.venue_code || ""));
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function rpc(url: string, headers: Record<string, string>, name: string, body: Record<string, unknown>) {
  const result = await fetch(`${url}/rest/v1/rpc/${name}`, { method: "POST", headers, body: JSON.stringify(body) });
  const responseBody = await result.text();
  if (!result.ok) return new Response(responseBody.slice(0, 1000), { status: result.status });
  return new Response(responseBody, { status: 200, headers: { "content-type": "application/json" } });
}

Deno.serve(async (request) => {
  try {
    if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
    const auth = request.headers.get("authorization") || "";
    if (!auth.startsWith("Bearer ")) return new Response("unauthorized", { status: 401 });
    const token = auth.slice(7);
    if (token.length < 100 || token.length > 4096) return new Response("unauthorized", { status: 401 });
    const claims = await verifyGithubOidc(token);
    const rawBody = await request.text();
    if (new TextEncoder().encode(rawBody).byteLength > 1_200_000) return new Response("request too large", { status: 413 });
    const body = JSON.parse(rawBody);
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const secret = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const commonHeaders = { apikey: secret, Authorization: `Bearer ${secret}`, "Content-Type": "application/json" };
    const actor = `github:${claims.repository}:${String(claims.ref).replace("refs/heads/", "")}:run=${claims.run_id}:attempt=${claims.run_attempt}`;

    if (body.operation === "stage_venue_generation") {
      if (!validVenueIdentity(body) || !/^[0-9a-f]{64}$/.test(String(body.generation_id || "")) ||
          !/^[0-9a-f]{64}$/.test(String(body.manifest_hash || "")) || !Array.isArray(body.records) ||
          !Array.isArray(body.expected_race_ids) || typeof body.manifest !== "object") {
        return new Response("invalid venue generation", { status: 400 });
      }
      return rpc(supabaseUrl, commonHeaders, "cloud_stage_venue_generation", {
        p_dataset_family: body.dataset_family, p_race_date: body.race_date,
        p_venue_code: body.venue_code, p_venue_name: body.venue_name,
        p_generation_id: body.generation_id, p_expected_race_ids: body.expected_race_ids,
        p_manifest: body.manifest, p_manifest_hash: body.manifest_hash, p_records: body.records,
      });
    }

    if (body.operation === "read_venue_pointer") {
      if (!validVenueIdentity(body)) return new Response("invalid venue identity", { status: 400 });
      return rpc(supabaseUrl, commonHeaders, "cloud_read_active_venue_pointer", {
        p_dataset_family: body.dataset_family, p_race_date: body.race_date,
        p_venue_code: body.venue_code,
      });
    }

    if (body.operation === "publish_venue_generation") {
      if (!validVenueIdentity(body) || !/^[0-9a-f]{64}$/.test(String(body.generation_id || "")) ||
          !Number.isSafeInteger(body.expected_revision) || body.expected_revision < 0 ||
          !["published", "rollback"].includes(String(body.action || ""))) {
        return new Response("invalid venue publication", { status: 400 });
      }
      return rpc(supabaseUrl, commonHeaders, "cloud_publish_venue_generation", {
        p_dataset_family: body.dataset_family, p_race_date: body.race_date,
        p_venue_code: body.venue_code, p_generation_id: body.generation_id,
        p_expected_revision: body.expected_revision, p_actor: actor, p_action: body.action,
      });
    }

    if (body.operation === "read_day_index") {
      if (body.dataset_family !== "racecard" || !/^\d{4}-\d{2}-\d{2}$/.test(String(body.race_date || ""))) {
        return new Response("invalid day identity", { status: 400 });
      }
      return rpc(supabaseUrl, commonHeaders, "cloud_read_active_day_index", {
        p_dataset_family: body.dataset_family, p_race_date: body.race_date,
      });
    }

    if (!validIdentity(String(body.record_type || ""), String(body.record_key || ""))) {
      return new Response("invalid record identity", { status: 400 });
    }
    if (body.operation === "read") {
      const query = new URLSearchParams({ select: "record_type,record_key,payload,content_hash,source_updated_at", record_type: `eq.${body.record_type}`, record_key: `eq.${body.record_key}`, limit: "1" });
      const result = await fetch(`${supabaseUrl}/rest/v1/shared_snapshots?${query}`, { headers: commonHeaders });
      if (!result.ok) throw new Error(`storage read failed ${result.status}`);
      const rows = await result.json();
      return Response.json({ record: rows[0] || null });
    }
    if (body.operation !== "write") return new Response("invalid operation", { status: 400 });
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(String(body.request_id || "")) ||
        !/^[0-9a-f]{64}$/.test(String(body.content_hash || "")) ||
        (body.expected_hash != null && !/^[0-9a-f]{64}$/.test(String(body.expected_hash)))) {
      return new Response("invalid write identity", { status: 400 });
    }
    if (typeof body.payload_json !== "string" || new TextEncoder().encode(body.payload_json).byteLength > 1_048_576) {
      return new Response("invalid canonical payload", { status: 400 });
    }
    const computedHash = await sha256Hex(body.payload_json);
    if (computedHash !== body.content_hash) return new Response("content hash mismatch", { status: 400 });
    let parsedPayload: unknown;
    try { parsedPayload = JSON.parse(body.payload_json); }
    catch { return new Response("invalid canonical payload", { status: 400 }); }
    return rpc(supabaseUrl, commonHeaders, "cloud_cas_write", {
      p_request_id: body.request_id, p_actor: actor, p_record_type: body.record_type,
      p_record_key: body.record_key, p_payload: parsedPayload, p_content_hash: body.content_hash,
      p_source_updated_at: body.source_updated_at, p_expected_hash: body.expected_hash ?? null,
    });
  } catch (error) {
    console.error("cloud ingest rejected", error instanceof Error ? error.message : "unknown error");
    return new Response("cloud ingest rejected", { status: 403 });
  }
});
