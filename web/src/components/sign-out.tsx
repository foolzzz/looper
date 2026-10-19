import { useEffect, useState } from "react";
import { LogOut } from "lucide-react";
import { fetchStatus } from "@/lib/api";

/** Ends a password session. A plain form POST, so the browser sends the page's Origin and follows the redirect to
 * /login. Shown only when the server says the session is one (the local browser's auto-login has nothing to end). */
export function SignOut() {
  const [can, setCan] = useState(false);

  useEffect(() => {
    fetchStatus()
      .then((s) => setCan(s.can_sign_out === true))
      .catch(() => setCan(false));
  }, []);

  if (!can) return null;
  return (
    <form method="post" action="/logout">
      <button
        type="submit"
        className="flex min-h-11 w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <LogOut className="h-4 w-4" aria-hidden="true" />
        Sign out
      </button>
    </form>
  );
}
