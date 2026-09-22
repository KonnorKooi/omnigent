import { useQuery } from "@tanstack/react-query";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";

// Mode-agnostic admin gate, sourced from the `/v1/me` identity probe
// (the shared `users.is_admin` column). Unlike `useMe`, which reads the
// accounts-only `/auth/me` endpoint, this works in EVERY auth mode —
// header, accounts, AND OIDC/SSO — so admin chrome (the Members /
// Policies settings sections) can surface under OIDC where `/auth/me`
// doesn't exist.
const QUERY_KEY = ["identity-is-admin"];

/**
 * Whether the current user is an admin, per `GET /v1/me`. Returns false
 * until identity resolves. Cached briefly so gating is instant across
 * consumers without re-probing on every navigation. Server enforces the
 * flag on every admin route regardless — this is chrome only.
 */
export function useIsAdmin(): boolean {
  return useIsAdminStatus().isAdmin;
}

/**
 * The admin flag plus whether the probe has answered yet.
 *
 * `useIsAdmin` seeds `false`, which conflates "not an admin" with "we have
 * not asked yet". A gate that renders a Forbidden screen cannot tell those
 * apart and so flashes one at every admin during boot. Surfaces that render
 * a denial (rather than just hiding chrome) read `isPending` and wait.
 */
export function useIsAdminStatus(): { isAdmin: boolean; isPending: boolean } {
  const { data, isPending } = useQuery<boolean>({
    queryKey: QUERY_KEY,
    queryFn: async () => {
      await resolveIdentity();
      return getCurrentIsAdmin();
    },
    staleTime: 30_000,
    // Seed from the already-resolved cache so first paint is correct when
    // identity resolved during boot (the common case).
    initialData: getCurrentIsAdmin,
  });
  return { isAdmin: data, isPending };
}
