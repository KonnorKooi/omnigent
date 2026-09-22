// History tab of the project Context page: the git log of the context folder.

import { useQuery } from "@tanstack/react-query";
import { Spinner } from "@/components/ui/spinner";
import { fetchContextHistory } from "@/lib/projectContextApi";
import { contextQueryKey } from "./contextQueryKey";

export function ContextHistoryTab({ projectId }: { projectId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: contextQueryKey(projectId, "history"),
    queryFn: () => fetchContextHistory(projectId),
  });
  if (isLoading) return <Spinner />;
  if (error || !data) return <p className="text-sm text-destructive">{String(error)}</p>;
  if (!data.versioned) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="context-history-unversioned">
        Not versioned. Put the context folder in a git repository (or run <code>git init</code>{" "}
        there) and edits will be committed automatically.
      </p>
    );
  }
  if (data.commits.length === 0) {
    return <p className="text-sm text-muted-foreground">No commits touch this folder yet.</p>;
  }
  return (
    <ul className="flex flex-col divide-y rounded-md border" data-testid="context-history-list">
      {data.commits.map((commit) => (
        <li key={commit.sha} className="flex flex-col gap-0.5 px-3 py-2 text-sm">
          <div className="flex items-baseline gap-2">
            <span className="font-medium">{commit.subject}</span>
            <span className="ml-auto shrink-0 font-mono text-xs text-muted-foreground">
              {commit.sha.slice(0, 8)}
            </span>
          </div>
          <div className="text-xs text-muted-foreground">
            {commit.author} · {new Date(commit.timestamp * 1000).toLocaleString()}
            {commit.files.length > 0 && ` · ${commit.files.join(", ")}`}
          </div>
        </li>
      ))}
    </ul>
  );
}
