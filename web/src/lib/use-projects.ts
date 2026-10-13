import { useCallback, useEffect, useRef, useState } from "react";
import { fetchProjects, PROJECTS_CHANGED_EVENT, type Project } from "@/lib/api";

const REFRESH_MS = 10_000;

/**
 * The project list, refreshed every 10 s and at once when this dashboard
 * changes a project or a task's membership. `projects` is null until the
 * first load succeeds.
 */
export function useProjects() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);

  const reload = useCallback((): Promise<void> => {
    // A slow poll must not overwrite the answer to a newer request.
    const seq = ++latest.current;
    return fetchProjects()
      .then((list) => {
        if (seq !== latest.current) return;
        setProjects(list);
        setError("");
      })
      .catch((err) => {
        if (seq !== latest.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  useEffect(() => {
    reload();
    const timer = setInterval(reload, REFRESH_MS);
    window.addEventListener(PROJECTS_CHANGED_EVENT, reload);
    return () => {
      clearInterval(timer);
      window.removeEventListener(PROJECTS_CHANGED_EVENT, reload);
    };
  }, [reload]);

  return { projects, error, reload };
}
