/**
 * Project Context page (`/projects/:projectId/context`,
 * designs/PROJECT_CONTEXT.md §5.1).
 *
 * A project's context repository is a folder of markdown on the server's
 * machine that seeds every session in the project. This page configures it
 * (empty state), opens its files in editor tabs next to an always-visible
 * knowledge/code graph, shows the exact text sessions receive, reviews update
 * proposals, and lists its git history.
 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderGit2Icon, Settings2Icon } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { contextQueryKey } from "@/components/projectContext/contextQueryKey";
import { ContextWorkspace } from "@/components/projectContext/ContextWorkspace";
import { ContextHistoryTab } from "@/components/projectContext/ContextHistoryTab";
import { ContextProposalsTab } from "@/components/projectContext/ContextProposalsTab";
import { UpdateContextButton } from "@/components/projectContext/UpdateContextButton";
import { useParams } from "@/lib/routing";
import { getProject, updateProjectConfig } from "@/lib/projectsApi";
import {
  type ContextStatusConfigured,
  type ProjectContextConfig,
  fetchContextStatus,
  initContext,
} from "@/lib/projectContextApi";

type ContextTab = "files" | "proposals" | "history";

/**
 * Form for the project's `config.context` block. Saving merges it into the
 * existing project config (the other defaults are preserved) and then
 * scaffolds the folder.
 */
export function ContextConfigForm({
  projectId,
  initial,
  submitLabel,
  onDone,
}: {
  projectId: string;
  initial: ProjectContextConfig | null;
  submitLabel: string;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [path, setPath] = useState(initial?.path ?? "");
  const [profilePath, setProfilePath] = useState(initial?.profile_path ?? "");
  const [repoPath, setRepoPath] = useState(initial?.repo_path ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const project = await getProject(projectId);
      const context: ProjectContextConfig = { path: path.trim() };
      if (profilePath.trim()) context.profile_path = profilePath.trim();
      if (repoPath.trim()) context.repo_path = repoPath.trim();
      await updateProjectConfig(projectId, { ...(project.config ?? {}), context });
      await initContext(projectId);
      await queryClient.invalidateQueries({ queryKey: contextQueryKey(projectId) });
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      toast.success("Context folder ready");
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="flex max-w-xl flex-col gap-3"
      data-testid="context-config-form"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Context folder</span>
        <Input
          aria-label="Context folder"
          placeholder="~/context/projects/my-project"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          required
        />
        <span className="text-muted-foreground">
          Absolute path on the server's machine. Created if missing.
        </span>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Profile folder (optional)</span>
        <Input
          aria-label="Profile folder"
          placeholder="~/context/me"
          value={profilePath}
          onChange={(e) => setProfilePath(e.target.value)}
        />
        <span className="text-muted-foreground">
          Shared "about me" markdown injected before this project's rules.
        </span>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Code repository (optional)</span>
        <Input
          aria-label="Code repository"
          placeholder="~/code/my-repo"
          value={repoPath}
          onChange={(e) => setRepoPath(e.target.value)}
        />
        <span className="text-muted-foreground">Enables the code graph (graphify).</span>
      </label>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div>
        <Button type="submit" disabled={busy || !path.trim()}>
          {busy ? <Spinner /> : submitLabel}
        </Button>
      </div>
    </form>
  );
}

function ConfiguredContext({ status }: { status: ContextStatusConfigured }) {
  const projectId = status.project_id;
  const [tab, setTab] = useState<ContextTab>("files");
  const [editing, setEditing] = useState(false);

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">{status.project_name} · Context</h1>
        <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{status.config.path}</code>
        {status.git ? (
          <Badge variant="secondary">
            <FolderGit2Icon />
            git
          </Badge>
        ) : (
          <Badge variant="outline">not versioned</Badge>
        )}
        {status.injected && (
          <Badge variant="outline" title="Always-loaded startup context size">
            ~{status.injected.approx_tokens.toLocaleString()} tokens injected
          </Badge>
        )}
        <div className="ml-auto flex items-center gap-2">
          {status.initialized && (
            <UpdateContextButton projectId={projectId} initialJob={status.active_job ?? null} />
          )}
          <Button size="sm" variant="ghost" onClick={() => setEditing((v) => !v)}>
            <Settings2Icon className="size-3.5" />
            Settings
          </Button>
        </div>
      </header>
      {editing && (
        <div className="rounded-md border p-3">
          <ContextConfigForm
            projectId={projectId}
            initial={status.config}
            submitLabel="Save"
            onDone={() => setEditing(false)}
          />
        </div>
      )}
      {!status.initialized ? (
        <InitializePrompt projectId={projectId} exists={status.exists} />
      ) : (
        <Tabs
          value={tab}
          onValueChange={(v) => setTab(v as ContextTab)}
          className="flex min-h-0 flex-1 flex-col"
        >
          <TabsList>
            <TabsTrigger value="files">Files</TabsTrigger>
            <TabsTrigger value="proposals">
              Proposals
              {(status.pending_proposals ?? 0) > 0 && (
                <Badge variant="secondary" className="ml-1">
                  {status.pending_proposals}
                </Badge>
              )}
            </TabsTrigger>
            <TabsTrigger value="history">History</TabsTrigger>
          </TabsList>
          <TabsContent
            value="files"
            forceMount
            className="flex min-h-0 flex-1 data-[state=inactive]:hidden"
          >
            <ContextWorkspace
              projectId={projectId}
              files={status.tree.files}
              hasCodeGraph={status.has_code_graph}
            />
          </TabsContent>
          <TabsContent value="proposals" className="flex min-h-0 flex-1">
            <ContextProposalsTab projectId={projectId} />
          </TabsContent>
          <TabsContent value="history" className="min-h-0 flex-1 overflow-y-auto">
            <ContextHistoryTab projectId={projectId} />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}

function InitializePrompt({ projectId, exists }: { projectId: string; exists: boolean }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex flex-col items-start gap-2 rounded-md border p-4 text-sm">
      <p>
        {exists
          ? "This folder has not been set up as a context repository yet."
          : "This folder does not exist yet."}{" "}
        Initialize creates <code>system/</code>, <code>wiki/</code>, <code>raw/</code>,{" "}
        <code>graph/</code> and a <code>CONTEXT.md</code> guide. Existing files are kept.
      </p>
      <Button
        disabled={busy}
        onClick={() => {
          setBusy(true);
          void initContext(projectId)
            .then(() => queryClient.invalidateQueries({ queryKey: contextQueryKey(projectId) }))
            .catch((err: unknown) => toast.error(err instanceof Error ? err.message : String(err)))
            .finally(() => setBusy(false));
        }}
      >
        {busy ? <Spinner /> : "Initialize"}
      </Button>
    </div>
  );
}

export function ProjectContextPage() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const { data, isLoading, error } = useQuery({
    queryKey: contextQueryKey(projectId, "status"),
    queryFn: () => fetchContextStatus(projectId),
    enabled: Boolean(projectId),
  });

  return (
    <div
      className="flex h-full min-h-0 flex-col px-4 pt-[calc(var(--omnigent-header-height,0px)+1rem)] pb-4"
      data-testid="project-context-page"
    >
      {isLoading ? (
        <Spinner />
      ) : error || !data ? (
        <p className="text-sm text-destructive">
          {error instanceof Error ? error.message : "Project not found"}
        </p>
      ) : data.configured ? (
        <ConfiguredContext status={data} />
      ) : (
        <div className="flex flex-col gap-4">
          <div>
            <h1 className="text-lg font-semibold">{data.project_name} · Context</h1>
            <p className="max-w-2xl text-sm text-muted-foreground">
              Give this project a context folder: plain markdown that every session in the project
              starts with (<code>system/</code>) or can look up through tools (<code>wiki/</code>,
              the code graph) — whichever harness runs it.
            </p>
          </div>
          <ContextConfigForm
            projectId={projectId}
            initial={null}
            submitLabel="Initialize"
            onDone={() => {}}
          />
        </div>
      )}
    </div>
  );
}
