// Explorer for the Context page: a nested, collapsible folder tree with each
// file's frontmatter description under its name, a filter box, and a
// right-click menu for creating, renaming and deleting editable markdown.

import { useMemo, useState } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  FilePlusIcon,
  FileTextIcon,
  FolderIcon,
  LockIcon,
  NetworkIcon,
  SparklesIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { ContextFileEntry } from "@/lib/projectContextApi";
import {
  CODE_GRAPH_FILE,
  type ContextFolder,
  type ContextTreeFolder,
  WRITABLE_FOLDERS,
  baseName,
  buildContextTree,
} from "./contextPaths";

interface TreeActions {
  activePath: string | null;
  dirtyPaths: ReadonlySet<string>;
  onOpen: (path: string) => void;
  onNewFile: (folder: string) => void;
  onRename: (path: string) => void;
  onDelete: (path: string) => void;
}

function isWritableFolder(path: string): boolean {
  return WRITABLE_FOLDERS.includes(path.split("/")[0] as ContextFolder);
}

function FileRow({
  file,
  depth,
  showPath,
  actions,
}: {
  file: ContextFileEntry;
  depth: number;
  showPath?: boolean;
  actions: TreeActions;
}) {
  const active = actions.activePath === file.path;
  const Icon = file.path === CODE_GRAPH_FILE ? NetworkIcon : FileTextIcon;
  const row = (
    <button
      type="button"
      onClick={() => actions.onOpen(file.path)}
      title={file.path}
      style={{ paddingLeft: `${0.5 + depth * 0.75}rem` }}
      className={cn(
        "flex w-full flex-col rounded py-1 pr-2 text-left hover:bg-muted",
        active && "bg-muted",
        !file.readable && "text-muted-foreground",
      )}
    >
      <span className={cn("flex items-center gap-1.5 text-sm", active && "font-medium")}>
        <Icon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="truncate">{showPath ? file.path : baseName(file.path)}</span>
        {actions.dirtyPaths.has(file.path) && (
          <span className="size-1.5 shrink-0 rounded-full bg-primary" aria-label="unsaved" />
        )}
      </span>
      {file.description && (
        <span className="truncate pl-5 text-xs text-muted-foreground">{file.description}</span>
      )}
    </button>
  );
  if (!file.writable) return row;
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{row}</ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={() => actions.onRename(file.path)}>Rename…</ContextMenuItem>
        <ContextMenuItem variant="destructive" onSelect={() => actions.onDelete(file.path)}>
          Delete
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}

function FolderNode({
  folder,
  depth,
  collapsed,
  toggle,
  actions,
}: {
  folder: ContextTreeFolder;
  depth: number;
  collapsed: ReadonlySet<string>;
  toggle: (path: string) => void;
  actions: TreeActions;
}) {
  const open = !collapsed.has(folder.path);
  const writable = isWritableFolder(folder.path);
  const top = depth === 0;
  const header = (
    <button
      type="button"
      onClick={() => toggle(folder.path)}
      aria-expanded={open}
      style={{ paddingLeft: `${0.25 + depth * 0.75}rem` }}
      className="flex w-full items-center gap-1 rounded py-1 pr-2 text-left text-sm hover:bg-muted"
    >
      {open ? (
        <ChevronDownIcon className="size-3.5 shrink-0" />
      ) : (
        <ChevronRightIcon className="size-3.5 shrink-0" />
      )}
      <FolderIcon className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate font-medium">{folder.name}</span>
      {top && folder.name === "system" && (
        <Badge variant="secondary" className="ml-1">
          always loaded
        </Badge>
      )}
      {top && !writable && <LockIcon className="ml-1 size-3" aria-label="read-only" />}
    </button>
  );
  return (
    <div>
      {writable ? (
        <ContextMenu>
          <ContextMenuTrigger asChild>{header}</ContextMenuTrigger>
          <ContextMenuContent>
            <ContextMenuItem onSelect={() => actions.onNewFile(folder.path)}>
              New file here…
            </ContextMenuItem>
          </ContextMenuContent>
        </ContextMenu>
      ) : (
        header
      )}
      {open && (
        <div>
          {folder.folders.map((child) => (
            <FolderNode
              key={child.path}
              folder={child}
              depth={depth + 1}
              collapsed={collapsed}
              toggle={toggle}
              actions={actions}
            />
          ))}
          {folder.files.map((file) => (
            <FileRow key={file.path} file={file} depth={depth + 1} actions={actions} />
          ))}
          {folder.folders.length === 0 && folder.files.length === 0 && (
            <div
              className="py-0.5 text-xs text-muted-foreground italic"
              style={{ paddingLeft: `${1.75 + depth * 0.75}rem` }}
            >
              empty
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ContextFileTree({
  files,
  injectedActive,
  onOpenInjected,
  ...actions
}: TreeActions & {
  files: ContextFileEntry[];
  injectedActive: boolean;
  onOpenInjected: () => void;
}) {
  const tree = useMemo(() => buildContextTree(files), [files]);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [filter, setFilter] = useState("");
  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const q = filter.trim().toLowerCase();
  const matches = q
    ? files.filter(
        (f) => f.path.toLowerCase().includes(q) || (f.description ?? "").toLowerCase().includes(q),
      )
    : [];

  return (
    <nav className="flex min-h-0 flex-col gap-2" aria-label="Context files">
      <div className="flex gap-1">
        <Button
          size="sm"
          variant={injectedActive ? "secondary" : "outline"}
          className="flex-1 justify-start"
          onClick={onOpenInjected}
        >
          <SparklesIcon className="size-3.5" />
          What sessions see
        </Button>
        <Button
          size="icon-sm"
          variant="outline"
          aria-label="New file"
          onClick={() => actions.onNewFile("wiki")}
        >
          <FilePlusIcon className="size-3.5" />
        </Button>
      </div>
      <Input
        aria-label="Filter files"
        placeholder="Filter files…"
        className="h-8"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className="min-h-0 flex-1 overflow-y-auto" data-testid="context-file-tree">
        {q ? (
          matches.length === 0 ? (
            <p className="px-2 text-sm text-muted-foreground">No matching files.</p>
          ) : (
            matches.map((file) => (
              <FileRow key={file.path} file={file} depth={0} showPath actions={actions} />
            ))
          )
        ) : (
          <>
            {tree.files.map((file) => (
              <FileRow key={file.path} file={file} depth={0} actions={actions} />
            ))}
            {tree.folders.map((folder) => (
              <FolderNode
                key={folder.path}
                folder={folder}
                depth={0}
                collapsed={collapsed}
                toggle={toggle}
                actions={actions}
              />
            ))}
          </>
        )}
      </div>
    </nav>
  );
}
