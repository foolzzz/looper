// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { createSchedule, updateSchedule, deleteSchedule, runSchedule, resetSchedulePlannerSession } from "@/lib/api";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

const fields = { name: "Hourly", cron: "0 * * * *", requirement: "Read reports" };
const cases = [
  ["create", () => createSchedule("proj-example", fields)],
  ["update", () => updateSchedule("sched-example", { enabled: true })],
  ["delete", () => deleteSchedule("sched-example")],
  ["run", () => runSchedule("sched-example")],
  ["reset_planner_session", () => resetSchedulePlannerSession("sched-example")],
] as const;

it.each(cases)("never reports a pending %s as successful or auto-confirms", async (_action, call) => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    expires_in_seconds: 300, impact: "Explicit approval required" }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(call()).rejects.toMatchObject({ status: 202, message: "Explicit approval required" });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("keeps the no-scheduler conflict and refuses writes after session expiry", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "error", error: { code: "conflict",
    message: "The scheduler is not running" } }, { status: 409 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(runSchedule("sched-example")).rejects.toMatchObject({ status: 409, code: "conflict" });
  fetcher.mockResolvedValueOnce(new Response(null, { status: 401 }));
  installSessionGuard();
  await expect(resetSchedulePlannerSession("sched-example")).rejects.toBeInstanceOf(SessionEndedError);
  await expect(deleteSchedule("sched-example")).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledTimes(2);
});
