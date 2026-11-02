/**
 * The one human rung on the evidence ladder: "Confirm finding" opens a dialog recording
 * that a person reviewed this finding and its evidence; once confirmed, the same slot offers
 * "Withdraw confirmation" instead. Both write through `useConfirmFinding` /
 * `useWithdrawConfirmation` (`POST /findings/{id}/confirm(/withdraw)`), never a citation
 * verdict: confirming or withdrawing never changes what AgentTape checked. A failed attempt
 * (a 409 conflict, most often) keeps its dialog open with the reason shown inline; only a
 * success toasts and closes.
 */
import { UserCheck } from "lucide-react";
import { useState } from "react";

import { describeError } from "@/api/client";
import { Button, ConfirmDialog, useToast } from "@/components/ui";

import { useConfirmFinding, useWithdrawConfirmation, type Finding } from "./api";

export function ConfirmControl({ finding, runId }: { finding: Finding; runId: string }) {
  const [open, setOpen] = useState(false);
  const confirm = useConfirmFinding(runId);
  const withdraw = useWithdrawConfirmation(runId);
  const toast = useToast();

  const confirmed = finding.confirmation != null;
  const mutation = confirmed ? withdraw : confirm;

  const openDialog = () => {
    mutation.reset();
    setOpen(true);
  };
  const close = () => {
    mutation.reset();
    setOpen(false);
  };

  const submit = (note: string) => {
    mutation.mutate(
      { findingId: finding.id, note: note.trim() || undefined },
      {
        onSuccess: () => {
          toast.show(confirmed ? "Confirmation withdrawn" : "Finding confirmed", "success");
          setOpen(false);
        },
      },
    );
  };

  return (
    <>
      <Button
        variant="secondary"
        icon={<UserCheck aria-hidden className="size-4" />}
        onClick={openDialog}
      >
        {confirmed ? "Withdraw confirmation" : "Confirm finding"}
      </Button>
      <ConfirmDialog
        open={open}
        title={confirmed ? "Withdraw confirmation" : "Confirm finding"}
        description={
          confirmed
            ? "The confirmation is withdrawn and the withdrawal is saved to the audit log. The finding and its verdicts do not change."
            : "You are recording that you reviewed this finding and its evidence. Your name and note are saved to the audit log."
        }
        confirmLabel={confirmed ? "Withdraw confirmation" : "Confirm finding"}
        withNote
        busy={mutation.isPending}
        error={mutation.error ? describeError(mutation.error) : undefined}
        onConfirm={submit}
        onClose={close}
      />
    </>
  );
}
