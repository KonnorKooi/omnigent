// Files workspace of the project Context page: an explorer tree, editor tabs
// for the open files (Monaco with an optional live preview, saved with
// Ctrl/Cmd+S under optimistic concurrency), and a graph panel that is shown as
// soon as the page opens. Graph JSON files open as graphs, not raw text.

import {
  type ComponentProps,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown, { type Components, defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  CodeIcon,
  ColumnsIcon,
  EyeIcon,
  NetworkIcon,
  PanelRightOpenIcon,
  PencilIcon,
  SaveIcon,
  SparklesIcon,
  Trash2Icon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { hasCommandModifier } from "@/lib/hotkeys";
import { cn } from "@/lib/utils";
import { ApiError } from "@/lib/sessionsApi";
import {
  type ContextFile,
  type ContextFileEntry,
  createContextFile,
  deleteContextFile,
  fetchContextFile,
  fetchContextGraph,
  fetchInjectedPreview,
  renameContextFile,
  saveContextFile,
} from "@/lib/projectContextApi";
import { ContextFileTree } from "./ContextFileTree";
import { ContextGraphPanel, GraphExplorer } from "./ContextGraphPanel";
import { ContextMarkdownEditor } from "./ContextMonaco";
import { contextQueryKey } from "./contextQueryKey";
import {
  CODE_GRAPH_FILE,
  WIKILINK_SCHEME,
  baseName,
  defaultContextFile,
  formatBytes,
  parseNodeLinkGraph,
  resolveContextLink,
  rewriteWikilinks,
  splitFrontmatter,
  validateNewContextPath,
} from "./contextPaths";

/** Pseudo-path for the "What sessions see" tab. */
export const INJECTED_VIEW = "__injected__";

type EditorMode = "edit" | "split" | "preview";
type GraphLayout = "open" | "expanded" | "collapsed";

const EDITOR_MODE_KEY = "omnigent.context.editorMode";
const GRAPH_LAYOUT_KEY = "omnigent.context.graphLayout";

function readPref<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key);
    return allowed.includes(value as T) ? (value as T) : fallback;
  } catch {
    return fallback;
  }
}

function writePref(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage can be unavailable (private mode); the preference just resets.
  }
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** What an anchor inside the preview needs to resolve context-relative links. */
interface LinkEnv {
  fromPath: string;
  files: ContextFileEntry[];
  onOpen: (path: string) => void;
}

const LinkEnvContext = createContext<LinkEnv | null>(null);

/** `<a>` override: context links open in-page, wikilinks to missing pages are struck. */
function ContextAnchor({ href, children }: ComponentProps<"a">) {
  const env = useContext(LinkEnvContext);
  const target = href && env ? resolveContextLink(href, env.fromPath, env.files) : null;
  if (target && env) {
    return (
      <button
        type="button"
        className="cursor-pointer text-primary underline underline-offset-2"
        onClick={() => env.onOpen(target)}
      >
        {children}
      </button>
    );
  }
  if (href?.startsWith(WIKILINK_SCHEME)) {
    return (
      <span className="text-muted-foreground line-through" title="No such page">
        {children}
      </span>
    );
  }
  return (
    <a href={href} target="_blank" rel="noreferrer">
      {children}
    </a>
  );
}

const MARKDOWN_COMPONENTS: Components = { a: ContextAnchor };

function ContextMarkdownPreview({
  path,
  content,
  files,
  onOpen,
}: {
  path: string;
  content: string;
  files: ContextFileEntry[];
  onOpen: (path: string) => void;
}) {
  const { frontmatter, body } = splitFrontmatter(content);
  const rendered = useMemo(() => rewriteWikilinks(body), [body]);
  const env = useMemo(() => ({ fromPath: path, files, onOpen }), [path, files, onOpen]);
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert" data-testid="context-preview">
      {frontmatter && (
        <pre className="not-prose mb-3 rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
          {frontmatter}
        </pre>
      )}
      <LinkEnvContext.Provider value={env}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          urlTransform={(url) => (url.startsWith(WIKILINK_SCHEME) ? url : defaultUrlTransform(url))}
          components={MARKDOWN_COMPONENTS}
        >
          {rendered}
        </ReactMarkdown>
      </LinkEnvContext.Provider>
    </div>
  );
}

function InjectedView({ projectId }: { projectId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: contextQueryKey(projectId, "preview"),
    queryFn: () => fetchInjectedPreview(projectId),
  });
  if (isLoading) return <Spinner />;
  if (error || !data) return <p className="text-sm text-destructive">{String(error)}</p>;
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2" data-testid="context-injected-view">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="secondary">
          {data.chars.toLocaleString()} chars · ~{data.approx_tokens.toLocaleString()} tokens
        </Badge>
        {data.truncated && <Badge variant="destructive">truncated</Badge>}
        <span className="text-muted-foreground">
          Appended to Claude and Codex startup instructions for every session in this project.
        </span>
      </div>
      {Object.entries(data.skipped_by_harness ?? {}).map(([harness, paths]) => (
        <p key={harness} className="text-xs text-muted-foreground" data-testid="context-skipped">
          {paths.join(", ")}: skipped for {harness} sessions, which load it natively.
        </p>
      ))}
      <pre className="min-h-0 flex-1 overflow-auto rounded-md border bg-muted/40 p-3 text-xs whitespace-pre-wrap">
        {data.text}
      </pre>
    </div>
  );
}

/** `graph/graph.json` drawn through the (capped) code-graph endpoint. */
function CodeGraphFileView({ projectId }: { projectId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: contextQueryKey(projectId, "graph", "code"),
    queryFn: () => fetchContextGraph(projectId, "code"),
  });
  if (isLoading) return <Spinner />;
  if (error || !data) return <p className="text-sm text-destructive">{String(error)}</p>;
  return (
    <GraphExplorer
      nodes={data.nodes}
      edges={data.edges}
      kind="code"
      summary={
        data.truncated && data.total_nodes
          ? `Showing top ${data.nodes.length} of ${data.total_nodes.toLocaleString()} nodes by degree`
          : `${data.nodes.length.toLocaleString()} nodes · ${data.edges.length.toLocaleString()} edges`
      }
    />
  );
}

function NewFileDialog({
  open,
  initialPath,
  title,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  initialPath: string;
  title: string;
  onOpenChange: (open: boolean) => void;
  onSubmit: (path: string) => Promise<void>;
}) {
  const [path, setPath] = useState(initialPath);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) setPath(initialPath);
  }, [open, initialPath]);
  const error = validateNewContextPath(path);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (error) return;
            setBusy(true);
            void onSubmit(path.trim()).finally(() => setBusy(false));
          }}
        >
          <Input
            autoFocus
            aria-label="File path"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="wiki/topic.md"
          />
          <p className={cn("text-sm", error ? "text-destructive" : "text-muted-foreground")}>
            {error ?? "system/ files are injected into every session; keep them short."}
          </p>
          <DialogFooter>
            <Button type="submit" disabled={Boolean(error) || busy}>
              {busy ? <Spinner /> : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Body of one open file: editor / preview / graph, with its toolbar. */
function FileView({
  projectId,
  path,
  entry,
  files,
  draft,
  conflict,
  saving,
  mode,
  onMode,
  onDraft,
  onSave,
  onReload,
  onRename,
  onDelete,
  onOpen,
}: {
  projectId: string;
  path: string;
  entry: ContextFileEntry | null;
  files: ContextFileEntry[];
  draft: string | undefined;
  conflict: boolean;
  saving: boolean;
  mode: EditorMode;
  onMode: (mode: EditorMode) => void;
  onDraft: (value: string) => void;
  onSave: () => void;
  onReload: () => void;
  onRename: () => void;
  onDelete: () => void;
  onOpen: (path: string) => void;
}) {
  const [raw, setRaw] = useState(false);
  const isCodeGraph = path === CODE_GRAPH_FILE;
  const fileQuery = useQuery({
    queryKey: contextQueryKey(projectId, "file", path),
    queryFn: () => fetchContextFile(projectId, path),
    enabled: entry?.readable !== false && (!isCodeGraph || raw),
  });
  const data = fileQuery.data;
  const content = data?.content ?? null;
  const jsonGraph = useMemo(
    () =>
      !isCodeGraph && path.toLowerCase().endsWith(".json") && content && !data?.truncated
        ? parseNodeLinkGraph(content)
        : null,
    [isCodeGraph, path, content, data?.truncated],
  );
  const isGraph = isCodeGraph || jsonGraph !== null;

  if (entry && !entry.readable) {
    return (
      <p className="text-sm text-muted-foreground">
        {path} looks like it holds secrets, so its contents are not shown.
      </p>
    );
  }

  const value = draft ?? content ?? "";
  const dirty = draft !== undefined && draft !== content;
  const writable = Boolean(data?.writable) && content !== null;
  const isMarkdown = path.toLowerCase().endsWith(".md");

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <span className="font-mono text-sm">{path}</span>
      {data && <span className="text-xs text-muted-foreground">{formatBytes(data.size)}</span>}
      {entry?.always_loaded && <Badge variant="secondary">always loaded</Badge>}
      {data && !data.writable && <Badge variant="outline">read-only</Badge>}
      {data?.truncated && <Badge variant="outline">truncated</Badge>}
      <div className="ml-auto flex items-center gap-1">
        {isGraph && (
          <Button size="sm" variant="ghost" onClick={() => setRaw((v) => !v)}>
            {raw ? <NetworkIcon className="size-3.5" /> : <CodeIcon className="size-3.5" />}
            {raw ? "Graph" : "Raw"}
          </Button>
        )}
        {writable && (
          <>
            {isMarkdown &&
              (
                [
                  ["edit", PencilIcon, "Edit"],
                  ["split", ColumnsIcon, "Split"],
                  ["preview", EyeIcon, "Preview"],
                ] as const
              ).map(([m, Icon, label]) => (
                <Button
                  key={m}
                  size="sm"
                  variant={mode === m ? "secondary" : "ghost"}
                  onClick={() => onMode(m)}
                >
                  <Icon className="size-3.5" />
                  {label}
                </Button>
              ))}
            <Button size="sm" onClick={onSave} disabled={!dirty || saving} title="Save (Ctrl/⌘+S)">
              {saving ? <Spinner /> : <SaveIcon className="size-3.5" />}
              Save
            </Button>
            <Button size="sm" variant="ghost" onClick={onRename}>
              Rename
            </Button>
            <Button size="icon-sm" variant="ghost" aria-label="Delete file" onClick={onDelete}>
              <Trash2Icon className="size-3.5" />
            </Button>
          </>
        )}
      </div>
    </div>
  );

  let body;
  if (isGraph && !raw) {
    body = isCodeGraph ? (
      <CodeGraphFileView projectId={projectId} />
    ) : (
      <GraphExplorer
        nodes={jsonGraph!.nodes}
        edges={jsonGraph!.edges}
        kind="code"
        summary={`${jsonGraph!.nodes.length.toLocaleString()} nodes · ${jsonGraph!.edges.length.toLocaleString()} edges${jsonGraph!.truncated ? " (truncated)" : ""}`}
      />
    );
  } else if (fileQuery.isLoading) {
    body = <Spinner />;
  } else if (fileQuery.error || !data) {
    body = <p className="text-sm text-destructive">{String(fileQuery.error)}</p>;
  } else if (content === null) {
    body = <p className="text-sm text-muted-foreground">Binary file.</p>;
  } else {
    const preview = isMarkdown ? (
      <ContextMarkdownPreview path={path} content={value} files={files} onOpen={onOpen} />
    ) : (
      <pre className="text-xs whitespace-pre-wrap">{value}</pre>
    );
    const effective: EditorMode = !writable ? "preview" : isMarkdown ? mode : "edit";
    const editor = (
      <div className="h-full min-h-[20rem]">
        <ContextMarkdownEditor path={path} value={value} onChange={onDraft} />
      </div>
    );
    body =
      effective === "split" ? (
        <div className="grid min-h-0 flex-1 grid-cols-2 gap-2">
          <div className="min-h-0 overflow-hidden rounded-md border">{editor}</div>
          <div className="min-h-0 overflow-auto rounded-md border p-3">{preview}</div>
        </div>
      ) : (
        <div
          className={cn(
            "min-h-0 flex-1 overflow-auto rounded-md border",
            effective === "preview" && "p-3",
          )}
        >
          {effective === "edit" ? editor : preview}
        </div>
      );
  }

  return (
    <>
      {toolbar}
      {conflict && (
        <div
          role="alert"
          className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
        >
          <TriangleAlertIcon className="size-4 text-destructive" />
          This file changed on disk since you opened it. Copy your edits, then reload.
          <Button size="xs" variant="outline" className="ml-auto" onClick={onReload}>
            Reload
          </Button>
        </div>
      )}
      {body}
    </>
  );
}

export function ContextWorkspace({
  projectId,
  files,
  hasCodeGraph,
}: {
  projectId: string;
  files: ContextFileEntry[];
  hasCodeGraph: boolean;
}) {
  const queryClient = useQueryClient();
  const [openPaths, setOpenPaths] = useState<string[]>(() => {
    const first = defaultContextFile(files);
    return first ? [first] : [];
  });
  const [activePath, setActivePath] = useState<string | null>(() => defaultContextFile(files));
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [conflicts, setConflicts] = useState<Set<string>>(() => new Set());
  const [saving, setSaving] = useState(false);
  const [dialog, setDialog] = useState<{ kind: "new"; folder: string } | { kind: "rename" } | null>(
    null,
  );
  const [mode, setModeState] = useState<EditorMode>(() =>
    readPref(EDITOR_MODE_KEY, ["edit", "split", "preview"], "edit"),
  );
  const [graphLayout, setGraphLayoutState] = useState<GraphLayout>(() =>
    readPref(GRAPH_LAYOUT_KEY, ["open", "expanded", "collapsed"], "open"),
  );
  const setMode = (next: EditorMode) => {
    setModeState(next);
    writePref(EDITOR_MODE_KEY, next);
  };
  const setGraphLayout = (next: GraphLayout) => {
    setGraphLayoutState(next);
    writePref(GRAPH_LAYOUT_KEY, next);
  };

  const byPath = useMemo(() => new Map(files.map((f) => [f.path, f])), [files]);
  const fileData = (path: string) =>
    queryClient.getQueryData<ContextFile>(contextQueryKey(projectId, "file", path));
  const isDirty = (path: string) =>
    drafts[path] !== undefined && drafts[path] !== (fileData(path)?.content ?? null);
  const dirtyPaths = new Set(Object.keys(drafts).filter(isDirty));

  const open = useCallback((path: string) => {
    setOpenPaths((prev) => (prev.includes(path) ? prev : [...prev, path]));
    setActivePath(path);
  }, []);

  const dropFile = (path: string) => {
    setDrafts(({ [path]: _dropped, ...rest }) => rest);
    setConflicts((prev) => {
      const next = new Set(prev);
      next.delete(path);
      return next;
    });
  };

  const close = (path: string) => {
    if (isDirty(path) && !window.confirm(`Discard unsaved changes to ${path}?`)) return;
    const idx = openPaths.indexOf(path);
    const remaining = openPaths.filter((p) => p !== path);
    setOpenPaths(remaining);
    dropFile(path);
    if (activePath === path) setActivePath(remaining[Math.min(idx, remaining.length - 1)] ?? null);
  };

  const refreshAll = () => queryClient.invalidateQueries({ queryKey: contextQueryKey(projectId) });

  const saveRef = useRef<() => Promise<void>>(async () => {});
  saveRef.current = async () => {
    const path = activePath;
    if (!path || path === INJECTED_VIEW || !isDirty(path) || saving) return;
    const loaded = fileData(path);
    if (!loaded?.sha) return;
    setSaving(true);
    try {
      await saveContextFile(projectId, path, drafts[path]!, loaded.sha);
      toast.success(`Saved ${path}`);
      dropFile(path);
      await refreshAll();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflicts((prev) => new Set(prev).add(path));
      } else toast.error(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  // Ctrl/Cmd+S saves the active file (also while Monaco has focus).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== "s" || e.altKey || e.shiftKey || !hasCommandModifier(e)) return;
      e.preventDefault();
      void saveRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const anyDirty = dirtyPaths.size > 0;
  useEffect(() => {
    if (!anyDirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyDirty]);

  async function remove(path: string) {
    if (!window.confirm(`Delete ${path}?`)) return;
    try {
      await deleteContextFile(projectId, path);
      const remaining = openPaths.filter((p) => p !== path);
      setOpenPaths(remaining);
      dropFile(path);
      if (activePath === path) setActivePath(remaining[0] ?? null);
      await refreshAll();
    } catch (err) {
      toast.error(errorText(err));
    }
  }

  const renameTarget = activePath && activePath !== INJECTED_VIEW ? activePath : null;
  const graphOpen = graphLayout !== "collapsed";
  const graphExpanded = graphLayout === "expanded";
  const activeFile = activePath && activePath !== INJECTED_VIEW ? activePath : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row" data-testid="context-files-tab">
      <aside className="flex max-h-72 shrink-0 flex-col lg:max-h-none lg:w-64 lg:border-r lg:pr-2">
        <ContextFileTree
          files={files}
          activePath={activePath}
          dirtyPaths={dirtyPaths}
          injectedActive={activePath === INJECTED_VIEW}
          onOpenInjected={() => open(INJECTED_VIEW)}
          onOpen={open}
          onNewFile={(folder) => setDialog({ kind: "new", folder })}
          onRename={(path) => {
            open(path);
            setDialog({ kind: "rename" });
          }}
          onDelete={(path) => void remove(path)}
        />
      </aside>

      {!graphExpanded && (
        <section
          className="flex min-h-[24rem] min-w-0 flex-1 flex-col gap-2"
          data-testid="context-editor"
        >
          {openPaths.length > 0 && (
            <div className="flex gap-1 overflow-x-auto border-b" role="list">
              {openPaths.map((path) => (
                <div
                  key={path}
                  role="listitem"
                  className={cn(
                    "group flex shrink-0 items-center gap-1 rounded-t-md border-b-2 px-2 py-1 text-sm",
                    path === activePath
                      ? "border-primary bg-muted font-medium"
                      : "border-transparent text-muted-foreground hover:bg-muted/50",
                  )}
                >
                  <button type="button" title={path} onClick={() => setActivePath(path)}>
                    {path === INJECTED_VIEW ? (
                      <span className="flex items-center gap-1">
                        <SparklesIcon className="size-3.5" />
                        What sessions see
                      </span>
                    ) : (
                      baseName(path)
                    )}
                  </button>
                  {dirtyPaths.has(path) && (
                    <span className="size-1.5 rounded-full bg-primary" aria-label="unsaved" />
                  )}
                  <button
                    type="button"
                    aria-label={`Close ${path === INJECTED_VIEW ? "What sessions see" : path}`}
                    className="rounded p-0.5 opacity-60 hover:bg-muted hover:opacity-100"
                    onClick={() => close(path)}
                  >
                    <XIcon className="size-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          {activePath === INJECTED_VIEW ? (
            <InjectedView projectId={projectId} />
          ) : !activeFile ? (
            <p className="text-sm text-muted-foreground">
              Open a file from the explorer, or “What sessions see” for the exact startup context.
            </p>
          ) : (
            <FileView
              key={activeFile}
              projectId={projectId}
              path={activeFile}
              entry={byPath.get(activeFile) ?? null}
              files={files}
              draft={drafts[activeFile]}
              conflict={conflicts.has(activeFile)}
              saving={saving}
              mode={mode}
              onMode={setMode}
              onDraft={(value) => setDrafts((prev) => ({ ...prev, [activeFile]: value }))}
              onSave={() => void saveRef.current()}
              onReload={() => {
                dropFile(activeFile);
                void queryClient.invalidateQueries({
                  queryKey: contextQueryKey(projectId, "file", activeFile),
                });
              }}
              onRename={() => setDialog({ kind: "rename" })}
              onDelete={() => void remove(activeFile)}
              onOpen={open}
            />
          )}
        </section>
      )}

      {graphOpen ? (
        <div
          className={cn(
            "flex min-h-[24rem] min-w-0 flex-col",
            graphExpanded ? "flex-1" : "lg:w-[36%] lg:shrink-0 lg:border-l lg:pl-3",
          )}
        >
          <ContextGraphPanel
            projectId={projectId}
            hasCodeGraph={hasCodeGraph}
            activePath={activeFile}
            onOpenFile={(path) => {
              open(path);
              if (graphExpanded) setGraphLayout("open");
            }}
            expanded={graphExpanded}
            onToggleExpanded={() => setGraphLayout(graphExpanded ? "open" : "expanded")}
            onCollapse={() => setGraphLayout("collapsed")}
          />
        </div>
      ) : (
        <Button
          variant="outline"
          size="sm"
          className="self-start lg:h-auto lg:self-stretch lg:px-1 lg:[writing-mode:vertical-rl]"
          onClick={() => setGraphLayout("open")}
          aria-label="Show graph"
        >
          <PanelRightOpenIcon className="size-3.5" />
          Graph
        </Button>
      )}

      <NewFileDialog
        open={dialog?.kind === "new"}
        title="New context file"
        initialPath={dialog?.kind === "new" ? `${dialog.folder}/` : "wiki/"}
        onOpenChange={(isOpen) => !isOpen && setDialog(null)}
        onSubmit={async (path) => {
          try {
            const name = baseName(path).replace(/\.(md|txt)$/i, "");
            const seed = path.endsWith(".md") ? `---\ndescription: \n---\n\n# ${name}\n` : "";
            await createContextFile(projectId, path, seed);
            setDialog(null);
            await refreshAll();
            open(path);
          } catch (err) {
            toast.error(errorText(err));
          }
        }}
      />
      <NewFileDialog
        open={dialog?.kind === "rename"}
        title={`Rename ${renameTarget ?? ""}`}
        initialPath={renameTarget ?? ""}
        onOpenChange={(isOpen) => !isOpen && setDialog(null)}
        onSubmit={async (path) => {
          if (!renameTarget) return;
          if (isDirty(renameTarget)) {
            toast.error("Save or discard your changes before renaming.");
            return;
          }
          try {
            await renameContextFile(projectId, renameTarget, path);
            setDialog(null);
            setOpenPaths((prev) => prev.map((p) => (p === renameTarget ? path : p)));
            dropFile(renameTarget);
            setActivePath(path);
            await refreshAll();
          } catch (err) {
            toast.error(errorText(err));
          }
        }}
      />
    </div>
  );
}
