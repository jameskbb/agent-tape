/**
 * The one owner of the findings types, mirroring `GET /runs/{run_id}/context/findings` and
 * the findings read paths. Every feature imports `Finding`, `LinkedSource` and
 * `useFindings` from here, so there is exactly one `Finding` type in the codebase.
 */
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";

import { api, describeError } from "@/api/client";
import { useInvalidate } from "@/api/hooks";
import type { Page, Run, RunStatus } from "@/api/types";
import { useToast } from "@/components/ui";

import type { WorstVerdict } from "./model";

/** The six citation verdicts the verification engine decides (never invented client-side). */
export type VerdictValue =
  | "quote_matches"
  | "stale"
  | "never_read"
  | "read_not_observed"
  | "quote_not_found"
  | "not_checkable";

export interface Verdict {
  id: string;
  verdict: VerdictValue;
  reason: string | null;
  method: string;
  verifier_version: string | null;
  evidence: Record<string, unknown>;
  rerun_of_id: string | null;
  created_at: string;
}

export interface Reproduction {
  id: string;
  trigger: "automatic" | "user";
  mode: "search" | "command";
  status: "queued" | "running" | "done";
  result: "reproduced" | "not_reproduced" | "could_not_run" | null;
  reason: string | null;
  pinned_commit: string | null;
  exit_code: number | null;
  recorded_exit_code: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface Citation {
  id: string;
  position: number;
  kind: "file" | "event";
  path: string | null;
  inside_boundary: boolean | null;
  line_start: number | null;
  line_end: number | null;
  quote: string | null;
  event_ref: string | null;
  event_id: string | null;
  relation: string | null;
  details: Record<string, unknown>;
  verdict: Verdict | null;
  checks: number;
  reproduction: Reproduction | null;
  reproductions: number;
}

export interface LinkedSource {
  source_id: string | null;
  key: string;
  name: string;
  type: string;
  live: boolean | null;
  authority: string;
  origin: string;
}

/** A person's confirmation of a finding, written by `POST /findings/{id}/confirm`; the
 * ladder's Confirmed rung and the Confirm control both read it here. */
export interface Confirmation {
  actor: string;
  ts: string;
  note: string | null;
}

export interface Finding {
  id: string;
  key: string;
  kind: string;
  text: string | null;
  source: string | null;
  event_id: string | null;
  ts: string | null;
  supports: LinkedSource[];
  conflicts: LinkedSource[];
  missing: LinkedSource[];
  citations: Citation[];
  details: Record<string, unknown>;
  /** Null when no person has confirmed the finding; the ladder then reads "Not confirmed". */
  confirmation: Confirmation | null;
}

/** The reviewer's own reading of a challenged finding (findings/review.py REVIEW_OUTCOMES):
 * model judgment, never a citation verdict. */
export type ReviewOutcomeValue = "upheld" | "refuted" | "inconclusive";

export interface ReviewOutcome {
  target_finding_key: string;
  target_finding_id: string;
  outcome: ReviewOutcomeValue;
  review_finding_id: string;
  review_finding_key: string;
  text: string;
  citations: number;
}

export interface UnresolvedReview {
  review_finding_id: string;
  review_finding_key: string;
  reason: string | null;
}

/** `GET /runs/{run_id}/challenges` item (`ChallengeOut`): one child run that reviewed the
 * target's findings, on demand or through a workflow node. */
export interface Challenge {
  run: Run;
  trigger: "on_demand" | "workflow_node";
  state: "pending" | "completed" | "did_not_complete";
  findings_reviewed: string[];
  omitted: number;
  outcomes: ReviewOutcome[];
  unresolved: UnresolvedReview[];
  judgment: "model judgment";
  counts_as_proof: false;
}

export interface ChallengeCreate {
  agent_profile_id: string;
  agent_profile_version_id?: string;
  title?: string;
}

export interface ChallengeStartResult {
  run: Run;
  findings_reviewed: string[];
  omitted: number;
  neutralized: { finding_key: string; field: string }[];
}

/** The most lines `GET /citations/{id}/content` returns in one response (the backend's
 * `MAX_CONTENT_LINES`, docs/SECURITY.md T28): context never grows a view past it. */
export const CITATION_CONTENT_MAX_LINES = 800;

export interface CitationContent {
  path: string;
  lines: [number, string][];
  line_first: number | null;
  line_last: number | null;
  window: [number, number] | null;
  cited: [number, number] | null;
  truncated: boolean;
  redacted: boolean;
  /** The line cap withheld lines the citation points at: the rest of a longer cited range,
   * or (with `cited` null) everything after the first 800 lines of the retained read. */
  clipped: boolean;
  blob_ref: string | null;
  read_result_event_id: string | null;
  resolved_by: "verdict_read" | "covering_read" | null;
  /** Why the retained lines cannot be shown (today `no_retained_read`, `outside_boundary` or
   * `blob_unavailable`). The wire type is a plain string, so a new code is still a string
   * here, and the view only tests whether a reason is present. */
  unavailable_reason: string | null;
}

const enc = encodeURIComponent;

/** The same query key the Context tab has always used, so context invalidations still work. */
function findingsKey(runId: string): readonly [string, string, string] {
  return ["context", runId, "findings"] as const;
}

/** Polls every 3 s while the run is live, and while it is terminal, any citation verdict
 * is still null, and fewer than 30 s have passed since `finishedAt`; independently, polls
 * every 5 s while any citation's reproduction is `queued` or `running`. */
export function useFindings(runId: string, live: boolean, finishedAt: string | null = null) {
  return useQuery({
    queryKey: findingsKey(runId),
    queryFn: ({ signal }) => api<Finding[]>(`/runs/${enc(runId)}/context/findings`, { signal }),
    refetchInterval: (query) => {
      if (live) return 3000;
      const data = query.state.data;
      if (data?.some((finding) => finding.citations.some(isReproducing))) return 5000;
      if (!finishedAt || !data) return false;
      const stillPending = data.some((finding) =>
        finding.citations.some((citation) => citation.verdict === null),
      );
      if (!stillPending) return false;
      const elapsedMs = Date.now() - new Date(finishedAt).getTime();
      return elapsedMs < 30_000 ? 3000 : false;
    },
  });
}

function isReproducing(citation: Citation): boolean {
  const status = citation.reproduction?.status;
  return status === "queued" || status === "running";
}

export interface VerificationSummary {
  run_id: string;
  citations: number;
  recorded: number;
  unchanged: number;
  verdicts: Record<string, number>;
}

/** Re-checks a finished run's citations against AgentTape's own recording, appending a
 * fresh verdict for each and invalidating the findings query so the tab picks them up. */
export function useRecheckCitations(runId: string) {
  const invalidate = useInvalidate(findingsKey(runId));
  return useMutation({
    mutationFn: () =>
      api<VerificationSummary>(`/runs/${enc(runId)}/findings/verify`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

/** Requests a re-run of the recorded command an event citation cites: a
 * queued job, never run synchronously here. Toasts "Re-run queued" on success and
 * invalidates the findings query so the new `reproduction` row is picked up; a 409 ("a
 * re-run is already in progress") or 422 ("only a citation of a recorded event can be
 * re-run") is toasted verbatim. */
export function useReproduceCitation(runId: string) {
  const invalidate = useInvalidate(findingsKey(runId));
  const toast = useToast();
  return useMutation({
    mutationFn: (citationId: string) =>
      api<Reproduction>(`/citations/${enc(citationId)}/reproductions`, { method: "POST" }),
    onSuccess: () => {
      toast.show("Re-run queued", "info");
      invalidate();
    },
    onError: (error) => toast.show(describeError(error), "error"),
  });
}

/** The same query key both the ladder's Challenge rung and the Challenges section read, so
 * starting a challenge invalidates the one cache both use. */
function challengesKey(runId: string): readonly [string, string, string] {
  return ["findings", runId, "challenges"] as const;
}

/** Polls every 5 s while any challenge of this run is still `pending`. */
export function useChallenges(runId: string) {
  return useQuery({
    queryKey: challengesKey(runId),
    queryFn: ({ signal }) => api<Challenge[]>(`/runs/${enc(runId)}/challenges`, { signal }),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.some((challenge) => challenge.state === "pending") ? 5000 : false;
    },
  });
}

/** Starts a "Challenge findings" child run (`POST /runs/{id}/challenge`); on success
 * invalidates this run's challenges and findings so both the ladder and the Challenges
 * section pick up the new pending row. Errors (404 unknown profile, 409 not finished / no
 * findings) are left for the caller to show, since the dialog keeps itself open on error. */
export function useStartChallenge(runId: string) {
  const invalidateChallenges = useInvalidate(challengesKey(runId));
  const invalidateFindings = useInvalidate(findingsKey(runId));
  return useMutation({
    mutationFn: (body: ChallengeCreate) =>
      api<ChallengeStartResult>(`/runs/${enc(runId)}/challenge`, { method: "POST", body }),
    onSuccess: () => {
      invalidateChallenges();
      invalidateFindings();
    },
  });
}

/** Confirms a finding (the one human rung on the ladder): `POST
 * /findings/{id}/confirm` with an optional note, saved to the audit log. Invalidates the
 * findings query on success so rung 6 picks up the new confirmation; a 409 (not finished,
 * already confirmed) is left for the caller (`ConfirmControl` keeps its dialog open on
 * error, per the UI-SPEC). */
export function useConfirmFinding(runId: string) {
  const invalidate = useInvalidate(findingsKey(runId));
  return useMutation({
    mutationFn: ({ findingId, note }: { findingId: string; note?: string }) =>
      api<{ finding_id: string; confirmation: Confirmation }>(
        `/findings/${enc(findingId)}/confirm`,
        { method: "POST", body: { note: note || undefined } },
      ),
    onSuccess: invalidate,
  });
}

/** Withdraws a finding's confirmation (`POST /findings/{id}/confirm/withdraw`): the
 * finding and its citation verdicts are never touched, only the audit log gains the
 * `finding.withdraw_confirmation` row. Invalidates the findings query on success; a 409
 * (not currently confirmed) is left for the caller. */
export function useWithdrawConfirmation(runId: string) {
  const invalidate = useInvalidate(findingsKey(runId));
  return useMutation({
    mutationFn: ({ findingId, note }: { findingId: string; note?: string }) =>
      api<{ finding_id: string; confirmation: null }>(
        `/findings/${enc(findingId)}/confirm/withdraw`,
        { method: "POST", body: { note: note || undefined } },
      ),
    onSuccess: invalidate,
  });
}

/** Widening the window (the viewer's "Show 20 more lines" controls) keeps the previous
 * window's lines on screen while the wider one loads, instead of flashing a skeleton. */
export function useCitationContent(
  citationId: string,
  window: { before: number; after: number },
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["citation-content", citationId, window.before, window.after],
    queryFn: ({ signal }) =>
      api<CitationContent>(`/citations/${enc(citationId)}/content`, {
        query: { before: window.before, after: window.after },
        signal,
      }),
    enabled,
    placeholderData: keepPreviousData,
  });
}

/** A finding row's run, named for display in the cross-run inbox (`FindingRunOut`): enough
 * to link back to its run without a second lookup. `kind` decides "Launched run" vs.
 * "Observed session"; `source` is the run's own creation origin (ui, cli, schedule, ...). */
export interface FindingRunRef {
  id: string;
  title: string;
  status: RunStatus;
  kind: string;
  agent_label: string;
  project_id: string | null;
  project_name: string | null;
}

/** One row of `GET /findings` (`FindingRowOut`): a finding, its citation tally, and its run,
 * the same cross-run read model the dashboard's `findings` tally is built from. */
export interface FindingRow {
  id: string;
  key: string;
  kind: string | null;
  text: string | null;
  created_at: string;
  /** Citation count (not the citations themselves; the inbox never fetches cited lines). */
  citations: number;
  /** Latest verdict of each citation, keyed by verdict; a null latest verdict counts as
   * "unchecked". Only non-zero keys are present. */
  verdict_counts: Record<string, number>;
  worst_verdict: WorstVerdict;
  run: FindingRunRef;
}

/** The `findings` block of `GET /dashboard` (`FindingsTallyOut`): the same window and
 * verdict vocabulary as `GET /findings`, unfiltered and tallied instead of listed. */
export interface FindingsTally {
  recorded: number;
  /** Findings with at least one citation where every citation's latest verdict is
   * `quote_matches` (a stale citation does not count); the backend computes it. */
  all_quotes_match: number;
  declared_only: number;
  citations: number;
  /** Citation counts by latest verdict, excluding `unchecked`; only non-zero keys present. */
  verdicts: Record<string, number>;
  unchecked: number;
}

export interface FindingsInboxParams {
  days: number;
  project_id?: string;
  agent_profile_id?: string;
  verdict?: string;
  kind?: string;
  source?: string;
  q?: string;
  sort?: "flagged" | "newest";
  limit: number;
  offset: number;
}

/** `GET /findings`: every finding recorded across every run in the window, flagged
 * verdicts first by default (`sort=flagged`) or newest first (`sort=newest`). */
export function useFindingsInbox(params: FindingsInboxParams) {
  return useQuery({
    queryKey: ["findings-inbox", params],
    queryFn: ({ signal }) => api<Page<FindingRow>>("/findings", { query: { ...params }, signal }),
    placeholderData: keepPreviousData,
  });
}
