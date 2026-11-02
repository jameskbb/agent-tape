/**
 * Minimal typed fetch client for `/api/v1`.
 *
 * Every error response from the backend uses the envelope
 * `{"error": {"code", "message", "details"}}`; it is surfaced as {@link ApiError}.
 * When the server requires a token (bound beyond loopback), the token saved in Settings
 * is sent as `Authorization: Bearer <token>`.
 */
import type { ApiErrorBody } from "./types";

export const API_BASE = "/api/v1";
const TOKEN_KEY = "agenttape.apiToken";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

// The token lives in sessionStorage: it is kept for this tab and cleared when the tab closes,
// which limits how long an injected script could find it. It is never put in URLs or logs.
export function getApiToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setApiToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY); // drop any copy saved by older builds
  } catch {
    /* storage unavailable: token lives only for this page */
  }
}

export function authHeaders(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type Query = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => params.append(key, String(v)));
    else params.set(key, String(value));
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: Query;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await fetch(buildUrl(path, options.query), {
    method: options.method ?? "GET",
    headers: {
      Accept: "application/json",
      ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(),
      ...options.headers,
    },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as Partial<ApiErrorBody> | null;
    const err = body?.error;
    throw new ApiError(
      response.status,
      err?.code ?? "http_error",
      err?.message ?? `Request failed with status ${response.status}`,
      err?.details ?? {},
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}

/** A non-JSON GET (text or binary) with the auth header; plain links cannot send it. */
async function fetchRaw(path: string, what = "Request"): Promise<Response> {
  const response = await fetch(buildUrl(path), { headers: authHeaders() });
  if (!response.ok) throw new Error(`${what} failed (${response.status})`);
  return response;
}

export async function fetchText(path: string): Promise<string> {
  return (await fetchRaw(path)).text();
}

/** Save a blob as a file download in the browser. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Download an API resource as a file. */
export async function downloadFromApi(path: string, filename: string): Promise<void> {
  saveBlob(await (await fetchRaw(path, "Download")).blob(), filename);
}
