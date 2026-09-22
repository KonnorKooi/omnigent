// "Update context" control for the project Context page header: starts the
// job, polls it while running, and shows per-step progress and the log.

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCwIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { type ContextJob, fetchContextJob, startContextUpdate } from "@/lib/projectContextApi";
import { contextQueryKey } from "./contextQueryKey";

/** How often a running job is polled. */
export const JOB_POLL_MS = 1500;

const STEP_LABELS: Record<string, string> = {
  code_graph: "Code graph",
  proposals: "Proposals",
  stamp: "Record update",
};

export function UpdateContextButton({
  projectId,
  initialJob,
}: {
  projectId: string;
  /** A job already running when the page loaded (from the status route). */
  initialJob: ContextJob | null;
}) {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(initialJob?.id ?? null);
  const [open, setOpen] = useState(false);
  const [starting, setStarting] = useState(false);

  const { data: job } = useQuery({
    queryKey: contextQueryKey(projectId, "job", jobId),
    queryFn: () => fetchContextJob(projectId, jobId!),
    enabled: Boolean(jobId),
    initialData: initialJob && initialJob.id === jobId ? initialJob : undefined,
    refetchInterval: (query) => (query.state.data?.status === "running" ? JOB_POLL_MS : false),
  });
  const running = starting || job?.status === "running";

  // When a job finishes, refresh everything it may have changed.
  const finishedStatus = job && job.status !== "running" ? job.status : null;
  useEffect(() => {
    if (!finishedStatus || !job) return;
    void queryClient.invalidateQueries({ queryKey: contextQueryKey(projectId) });
    if (finishedStatus === "succeeded") {
      toast.success(
        job.proposals_created > 0
          ? `Context updated · ${job.proposals_created} proposal(s) to review`
          : "Context updated",
      );
    } else {
      toast.error(`Update context failed: ${job.error ?? "see log"}`);
    }
    // Only react to the transition, not every refetch of the finished job.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedStatus, job?.id]);

  async function start() {
    setStarting(true);
    try {
      const created = await startContextUpdate(projectId);
      queryClient.setQueryData(contextQueryKey(projectId, "job", created.id), created);
      setJobId(created.id);
      setOpen(true);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div className="flex items-center gap-1">
        <Button size="sm" onClick={() => void start()} disabled={running}>
          <RefreshCwIcon className={running ? "size-3.5 animate-spin" : "size-3.5"} />
          {running ? "Updating…" : "Update context"}
        </Button>
        {job && (
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost" data-testid="context-job-log-toggle">
              {job.status === "running" ? "Progress" : `Last run: ${job.status}`}
            </Button>
          </PopoverTrigger>
        )}
      </div>
      {job && (
        <PopoverContent className="w-96" align="end" data-testid="context-job-log">
          <ul className="mb-2 flex flex-col gap-1 text-sm">
            {job.steps.map((step) => (
              <li key={step.name} className="flex gap-2">
                <span className="w-28 shrink-0 font-medium">
                  {STEP_LABELS[step.name] ?? step.name}
                </span>
                <span className="text-muted-foreground">
                  {step.status}
                  {step.detail ? ` — ${step.detail}` : ""}
                </span>
              </li>
            ))}
          </ul>
          <pre className="max-h-48 overflow-auto rounded bg-muted p-2 text-xs whitespace-pre-wrap">
            {job.log.join("\n") || "(no output yet)"}
          </pre>
        </PopoverContent>
      )}
    </Popover>
  );
}
