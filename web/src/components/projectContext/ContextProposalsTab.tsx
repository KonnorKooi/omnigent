// Proposals tab of the project Context page: pending per-file proposals from
// "Update context", each reviewed as a side-by-side diff and applied or
// rejected individually.

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, TriangleAlertIcon, XIcon } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { ApiError } from "@/lib/sessionsApi";
import {
  type ContextProposal,
  applyContextProposal,
  fetchContextProposals,
  rejectContextProposal,
} from "@/lib/projectContextApi";
import { contextQueryKey } from "./contextQueryKey";
import { ContextDiffEditor } from "./ContextMonaco";

export function ContextProposalsTab({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: contextQueryKey(projectId, "proposals"),
    queryFn: () => fetchContextProposals(projectId),
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  if (isLoading) return <Spinner />;
  if (error || !data) return <p className="text-sm text-destructive">{String(error)}</p>;
  const proposals = data.proposals;
  if (proposals.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="context-proposals-empty">
        No pending proposals. Run “Update context” to review new raw sources and recent sessions.
      </p>
    );
  }
  const selected: ContextProposal = proposals.find((p) => p.id === selectedId) ?? proposals[0]!;

  async function act(proposal: ContextProposal, action: "apply" | "reject") {
    setBusy(proposal.id);
    try {
      if (action === "apply") {
        await applyContextProposal(projectId, proposal.id);
        toast.success(`Applied ${proposal.path}`);
      } else {
        await rejectContextProposal(projectId, proposal.id);
      }
      setSelectedId(null);
    } catch (err) {
      toast.error(
        err instanceof ApiError && err.status === 409
          ? `${proposal.path} changed since this proposal was made. Reject it or edit the file by hand.`
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      setBusy(null);
      await queryClient.invalidateQueries({ queryKey: contextQueryKey(projectId) });
    }
  }

  return (
    <div className="flex min-h-0 flex-1 gap-3" data-testid="context-proposals-tab">
      <ul className="flex w-72 shrink-0 flex-col gap-1 overflow-y-auto border-r pr-2">
        {proposals.map((proposal) => (
          <li key={proposal.id}>
            <button
              type="button"
              onClick={() => setSelectedId(proposal.id)}
              className={cn(
                "flex w-full flex-col items-start gap-0.5 rounded px-2 py-1.5 text-left text-sm hover:bg-muted",
                proposal.id === selected.id && "bg-muted",
              )}
            >
              <span className="flex items-center gap-1.5 font-mono text-xs">
                <Badge variant={proposal.action === "create" ? "secondary" : "outline"}>
                  {proposal.action}
                </Badge>
                {proposal.path}
              </span>
              <span className="line-clamp-2 text-xs text-muted-foreground">
                {proposal.rationale || "No rationale given"}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <section className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm">{selected.path}</span>
          {selected.stale && (
            <Badge variant="destructive">
              <TriangleAlertIcon />
              file changed since proposed
            </Badge>
          )}
          <div className="ml-auto flex gap-1">
            <Button
              size="sm"
              variant="ghost"
              disabled={busy !== null}
              onClick={() => void act(selected, "reject")}
            >
              <XIcon className="size-3.5" />
              Reject
            </Button>
            <Button
              size="sm"
              disabled={busy !== null || selected.stale}
              onClick={() => void act(selected, "apply")}
            >
              <CheckIcon className="size-3.5" />
              Apply
            </Button>
          </div>
        </div>
        <p className="text-sm">{selected.rationale}</p>
        {selected.sources.length > 0 && (
          <p className="text-xs text-muted-foreground">Sources: {selected.sources.join(", ")}</p>
        )}
        <div className="min-h-[24rem] flex-1 overflow-hidden rounded-md border">
          <ContextDiffEditor
            path={selected.path}
            original={selected.current_content ?? ""}
            modified={selected.new_content}
          />
        </div>
      </section>
    </div>
  );
}
