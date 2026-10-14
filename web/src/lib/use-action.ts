import { useState } from "react";

export const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** One request at a time, with the error it ended with; `run` resolves to whether it succeeded. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (err) {
      setError(errorText(err));
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

