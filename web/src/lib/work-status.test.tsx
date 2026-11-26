// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it } from "vitest";
import { WorkTaskLink } from "@/components/work-row";
import { workStatusLabel, type WorkTask } from "@/lib/work";
import { TaskStatusMark } from "@/pages/tasks";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

const task = (status: string): WorkTask => ({
  task_id: "task-handing-back", title: "交回中的任务", project_id: "example", project_name: "Example",
  status, group: "in_progress", last_activity_at: "2026-11-25T10:20:00Z",
  last_seq: 1, waiting_gates: 0, needs_you: false, unseen: false,
});

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

it("labels handing_back in Chinese instead of the raw status", () => {
  expect(workStatusLabel("handing_back")).toBe("交回中");
  act(() => root.render(<>
    <TaskStatusMark status="handing_back" label={workStatusLabel("handing_back")} />
    <WorkTaskLink task={task("handing_back")} current={false} onActivate={() => {}} />
  </>));
  expect(host.textContent).toContain("交回中");
  expect(host.textContent).not.toContain("handing_back");
});

it("labels handing_back in English instead of the raw status", () => {
  act(() => root.render(<TaskStatusMark status="handing_back" />));
  expect(host.textContent).toContain("Handing back");
  expect(host.textContent).not.toContain("handing_back");
});
