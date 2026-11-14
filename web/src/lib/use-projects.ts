import { startPolling } from "@/lib/polling";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchProjectsList, PROJECTS_CHANGED_EVENT, type Project } from "@/lib/api";

const REFRESH_MS = 10_000;

/**
 * The project list, refreshed every 10 s and at once when this dashboard
 * changes a project or a task's membership. `projects` is null until the
 * first load succeeds.
 */
export function useProjects() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectsRoot, setProjectsRoot] = useState("");
  const [error, setError] = useState("");
  const latest = useRef(0);

  const reload = useCallback((): Promise<void> => {
    // A slow poll must not overwrite the answer to a newer request.
    const seq = ++latest.current;
    return fetchProjectsList()
      .then((list) => {
        if (seq !== latest.current) return;
        setProjects(list.projects);
        setProjectsRoot(list.projects_root);
        setError("");
      })
      .catch((err) => {
        if (seq !== latest.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  useEffect(() => {
    const stop = startPolling(reload, REFRESH_MS);
    window.addEventListener(PROJECTS_CHANGED_EVENT, reload);
    return () => {
      stop();
      latest.current++;
      window.removeEventListener(PROJECTS_CHANGED_EVENT, reload);
    };
  }, [reload]);

  return { projects, projectsRoot, error, reload };
}
