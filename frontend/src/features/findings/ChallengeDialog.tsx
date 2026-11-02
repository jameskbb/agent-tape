/**
 * "Challenge findings": a reviewer-agent picker over a finished run's findings. Starting a
 * challenge creates and opens an ordinary child run (`POST /runs/{id}/challenge`); nothing
 * here builds a prompt or touches a citation verdict; the server does both, through the one
 * `FindingReviewService` path (docs/SECURITY.md T27).
 */
import { useState } from "react";
import { useNavigate } from "react-router";

import { describeError } from "@/api/client";
import type { RunDetail } from "@/api/types";
import { Button, Drawer, Select, useToast } from "@/components/ui";
import { useProfiles } from "@/features/agents/api";

import { useStartChallenge } from "./api";

const FIELD =
  "rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-1.5 text-base text-ink";

/** Open while mounted: the caller mounts it only when the dialog should show. */
export function ChallengeDialog({
  run,
  findingCount,
  onClose,
}: {
  run: RunDetail["run"];
  findingCount: number;
  onClose: () => void;
}) {
  const [profileId, setProfileId] = useState("");
  const [title, setTitle] = useState("");
  const profiles = useProfiles({ limit: 100 });
  const start = useStartChallenge(run.id);
  const toast = useToast();
  const navigate = useNavigate();

  const options = (profiles.data?.items ?? []).map((profile) => ({
    value: profile.id,
    label: profile.name,
  }));

  const submit = () => {
    if (!profileId) return;
    start.mutate(
      { agent_profile_id: profileId, title: title.trim() || undefined },
      {
        onSuccess: (result) => {
          toast.show("Challenge started", "success");
          onClose();
          void navigate(`/runs/${result.run.id}`);
        },
        onError: (error) => toast.show(describeError(error), "error"),
      },
    );
  };

  return (
    <Drawer open eyebrow="Model judgment" title="Challenge findings" onClose={onClose}>
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <p className="text-sm text-ink-soft">
          A reviewer agent reads this run&apos;s {findingCount} finding
          {findingCount === 1 ? "" : "s"} and their citations and records its own opinion of each.
          Its verdicts are model judgment and never count as proof; the citation verdicts on this
          run do not change.
        </p>
        <Select
          label="Reviewer agent"
          options={options}
          value={profileId}
          onChange={setProfileId}
          any="Choose a reviewer"
          required
        />
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Title (optional)</span>
          <input
            className={FIELD}
            placeholder={`Challenge: ${run.title}`}
            value={title}
            maxLength={300}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <div className="flex items-center justify-end gap-2">
          <Button onClick={onClose} disabled={start.isPending}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={start.isPending} disabled={!profileId}>
            Start challenge
          </Button>
        </div>
      </form>
    </Drawer>
  );
}
