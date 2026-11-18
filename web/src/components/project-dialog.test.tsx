// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectDialog } from "@/components/project-dialog";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLElement;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("ProjectDialog Escape", () => {
  it("closes the dialog only, before a window Escape handler registered earlier (the sidebar drawer's)", () => {
    const drawer = vi.fn();
    window.addEventListener("keydown", drawer);
    const onClose = vi.fn();
    act(() => root.render(<ProjectDialog onClose={onClose} onCreated={vi.fn()} />));
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(drawer).not.toHaveBeenCalled();
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    });
    expect(drawer).toHaveBeenCalledTimes(1);
    window.removeEventListener("keydown", drawer);
  });
});
