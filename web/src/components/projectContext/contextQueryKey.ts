/** React Query key for everything under one project's context. */
export function contextQueryKey(projectId: string, ...rest: unknown[]): unknown[] {
  return ["project-context", projectId, ...rest];
}
