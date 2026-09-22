// Thin Monaco wrappers for the Context page: a markdown editor and a
// side-by-side diff for proposal review. Both share the app's Shiki-backed
// themes via `monacoSetup` so colours match the session file viewer.

import { useEffect, useState } from "react";
import { DiffEditor, Editor } from "@monaco-editor/react";
import { Spinner } from "@/components/ui/spinner";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import {
  ensureLanguage,
  ensureMonacoReady,
  monacoLanguageId,
  resolvedThemeToMonaco,
} from "@/shell/monacoSetup";

/** Wait for the Shiki themes + a grammar; returns readiness and error flags. */
function useMonacoReady(lang: "markdown" | "text"): { ready: boolean; failed: boolean } {
  const [state, setState] = useState({ ready: false, failed: false });
  useEffect(() => {
    let cancelled = false;
    void Promise.all([ensureMonacoReady(), ensureLanguage(lang)]).then(
      () => !cancelled && setState({ ready: true, failed: false }),
      () => !cancelled && setState({ ready: false, failed: true }),
    );
    return () => {
      cancelled = true;
    };
  }, [lang]);
  return state;
}

function languageFor(path: string): "markdown" | "text" {
  return path.toLowerCase().endsWith(".md") ? "markdown" : "text";
}

function EditorFallback({ failed }: { failed: boolean }) {
  return (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      {failed ? "Editor failed to load." : <Spinner />}
    </div>
  );
}

export function ContextMarkdownEditor({
  path,
  value,
  onChange,
}: {
  path: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const lang = languageFor(path);
  const { ready, failed } = useMonacoReady(lang);
  const theme = resolvedThemeToMonaco(useResolvedThemeMode());
  if (!ready) return <EditorFallback failed={failed} />;
  return (
    <Editor
      path={`context://${path}`}
      value={value}
      language={monacoLanguageId(lang)}
      theme={theme}
      onChange={(next) => onChange(next ?? "")}
      options={{
        wordWrap: "on",
        minimap: { enabled: false },
        lineNumbers: "on",
        scrollBeyondLastLine: false,
        fontSize: 13,
      }}
    />
  );
}

export function ContextDiffEditor({
  path,
  original,
  modified,
}: {
  path: string;
  original: string;
  modified: string;
}) {
  const lang = languageFor(path);
  const { ready, failed } = useMonacoReady(lang);
  const theme = resolvedThemeToMonaco(useResolvedThemeMode());
  if (!ready) return <EditorFallback failed={failed} />;
  return (
    <DiffEditor
      original={original}
      modified={modified}
      language={monacoLanguageId(lang)}
      theme={theme}
      options={{
        readOnly: true,
        renderSideBySide: true,
        wordWrap: "on",
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 13,
      }}
    />
  );
}
