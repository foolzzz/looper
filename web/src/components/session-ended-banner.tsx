import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSessionEnded } from "@/lib/session";

/** Says the session ended (an API call got 401); `App` shows only this then, and reloading shows the sign-in page. */
export function SessionEndedBanner() {
  if (!useSessionEnded()) return null;
  return (
    <div
      role="alert"
      className="flex max-w-xl items-center gap-3 rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm"
    >
      <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
      <span className="flex-1 font-medium text-destructive">Your session ended. Reload to sign in again.</span>
      <Button size="sm" variant="outline" onClick={() => window.location.reload()}>
        Reload
      </Button>
    </div>
  );
}
