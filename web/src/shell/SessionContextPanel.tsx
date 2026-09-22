// Session "Context" rail tab (designs/PROJECT_CONTEXT.md §5.2): the project
// context this session was given at startup (collapsible) and a chronological
// list of the context tool calls it made, with the files / graph nodes each
// returned. Links jump to the project Context page.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDownIcon, ChevronRightIcon, ExternalLinkIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { Link } from "@/lib/routing";
import { ApiError } from "@/lib/sessionsApi";
import {
  type ContextTraceEvent,
  type SessionContextTrace,
  fetchSessionContextTrace,
} from "@/lib/projectContextApi";

/** Query key for a session's context trace. */
export function sessionContextTraceKey(sessionId: string): unknown[] {
  return ["session-context-trace", sessionId];
}

/**
 * The session's context trace, or `null` when the session has no project
 * context (the server answers 404 — not an error for this surface).
 */
export function useSessionContextTrace(sessionId: string | null | undefined) {
  return useQuery<SessionContextTrace | null>({
    queryKey: sessionContextTraceKey(sessionId ?? ""),
    queryFn: async () => {
      try {
        return await fetchSessionContextTrace(sessionId!);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
    enabled: Boolean(sessionId),
    retry: false,
    staleTime: 15_000,
  });
}

function describeArgs(event: ContextTraceEvent): string {
  const args = event.args ?? {};
  const main = args.path ?? args.query ?? args.question ?? args.node;
  return typeof main === "string" ? main : "";
}

function TraceEvent({ event, injectedSha }: { event: ContextTraceEvent; injectedSha: string }) {
  const time = new Date(event.ts * 1000).toLocaleTimeString();
  if (event.kind === "injected") {
    return (
      <li className="flex flex-col gap-0.5 border-l-2 border-primary/40 pl-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>{time}</span>
          <span className="font-medium text-foreground">startup injection</span>
          {event.sha256 !== injectedSha && <Badge variant="outline">older version</Badge>}
        </div>
        <div className="text-xs text-muted-foreground">
          {(event.chars ?? 0).toLocaleString()} chars
        </div>
      </li>
    );
  }
  const returned = event.paths ?? event.node_ids ?? [];
  return (
    <li className="flex flex-col gap-0.5 border-l-2 border-muted pl-2">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <span>{time}</span>
        <span className="font-mono font-medium text-foreground">{event.tool}</span>
        <span className="truncate">{describeArgs(event)}</span>
      </div>
      {returned.length > 0 && (
        <div className="line-clamp-3 text-xs break-all text-muted-foreground">
          {returned.slice(0, 12).join(", ")}
          {returned.length > 12 ? ` +${returned.length - 12} more` : ""}
        </div>
      )}
    </li>
  );
}

export function SessionContextPanel({ conversationId }: { conversationId: string }) {
  const { data, isLoading, error } = useSessionContextTrace(conversationId);
  const [expanded, setExpanded] = useState(false);
  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (error) return <p className="p-3 text-sm text-destructive">{String(error)}</p>;
  if (!data) {
    return (
      <p className="p-3 text-sm text-muted-foreground">
        This session is not in a project with context.
      </p>
    );
  }
  const contextHref = `/projects/${encodeURIComponent(data.project_id)}/context`;
  const toolCalls = data.events.filter((e) => e.kind === "tool").length;
  return (
    <div
      className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3"
      data-testid="session-context-panel"
    >
      <div className="flex items-center gap-2 text-sm">
        <span className="font-medium">{data.project_name}</span>
        <Link
          to={contextHref}
          className="ml-auto inline-flex items-center gap-1 text-xs text-primary hover:underline"
        >
          Open context
          <ExternalLinkIcon className="size-3" />
        </Link>
      </div>
      <section>
        <button
          type="button"
          className="flex w-full items-center gap-1 text-left text-sm font-medium"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? (
            <ChevronDownIcon className="size-3.5" />
          ) : (
            <ChevronRightIcon className="size-3.5" />
          )}
          Injected at startup
          <Badge variant="secondary" className="ml-1">
            ~{data.injected.approx_tokens.toLocaleString()} tokens
          </Badge>
          {data.injected.truncated && <Badge variant="destructive">truncated</Badge>}
        </button>
        {expanded && (
          <pre className="mt-2 max-h-80 overflow-auto rounded-md border bg-muted/40 p-2 text-xs whitespace-pre-wrap">
            {data.injected.text}
          </pre>
        )}
      </section>
      <section>
        <div className="mb-1 text-sm font-medium">
          Context tool calls <span className="text-muted-foreground">({toolCalls})</span>
        </div>
        {data.events.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            Nothing recorded yet. Claude and Codex sessions record their startup injection; any
            harness records context_* and graph_* tool calls.
          </p>
        ) : (
          <ol className="flex flex-col gap-2">
            {data.events.map((event) => (
              <TraceEvent
                key={`${event.ts}-${event.kind}-${event.tool ?? ""}`}
                event={event}
                injectedSha={data.injected.sha256}
              />
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
