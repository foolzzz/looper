import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TaskCostBreakdown } from "@/components/task-cost";
import type { TaskCost, TaskModelCost } from "@/lib/api";
import { modelRunCaveats } from "@/lib/usage";

const MODEL: TaskModelCost = {
  model: "claude-opus-4-1", runs: 2, running: 0, unpriced_runs: 0, interrupted_runs: 0,
  input_tokens: 1_234_567, output_tokens: 20, cache_read_tokens: 1000, cache_creation_tokens: 50, cost_usd: 0.75,
};
const COST: TaskCost = {
  cost_usd: 0.76, runs: 4, running: 0, unpriced_runs: 1,
  by_role: { planner: { cost_usd: 0.75, runs: 1 }, worker: { cost_usd: 0, runs: 2 },
             reviewer: { cost_usd: 0.01, runs: 1 } },
  by_model: [MODEL, { ...MODEL, model: "claude-haiku-4-5", runs: 1, cost_usd: 0.01 },
             { ...MODEL, model: "deepseek-flash", runs: 1, input_tokens: 7, cost_usd: null,
                     unpriced_runs: 1, interrupted_runs: 1 }],
  manager_by_model: [{ ...MODEL, model: "claude-haiku-4-5", runs: 1, input_tokens: 30, output_tokens: 4,
                       cache_read_tokens: 50, cache_creation_tokens: 6, cost_usd: 0.02 }],
};

const text = (html: string) => html.replace(/<[^>]+>/g, "").replace(/&#x27;/g, "'");

describe("TaskCostBreakdown", () => {
  it("shows each model's tokens, cost and caveats, including the Manager's turns", () => {
    const html = renderToStaticMarkup(<TaskCostBreakdown cost={COST} />);
    const shown = text(html);
    expect(html).toContain("<table");
    for (const part of [
      "By model", "ModelRunsInputOutputCache readCache writeCost", "claude-opus-4-1", "deepseek-flash",
      "1.23M", "$0.7500", "–", "1 without a cost, 1 interrupted",
      "Manager · claude-haiku-4-5", "Not counted in the task's cost or spend cap",
    ]) expect(shown).toContain(part);
    const bodies = [...html.matchAll(/<tbody[^>]*>(.*?)<\/tbody>/g)].map((m) => m[1]);
    expect(bodies).toHaveLength(2);
    const modelCosts = [...bodies[0].matchAll(/<tr>(.*?)<\/tr>/g)].map((m) => {
      const cells = [...m[1].matchAll(/<td[^>]*>(.*?)<\/td>/g)];
      return Number(text(cells.at(-1)![1]).replace("$", "").replace("–", "0"));
    });
    const total = modelCosts.reduce((sum, value) => sum + value, 0);
    expect(total).toBeCloseTo(COST.cost_usd);
    expect(shown).toContain(`$${total.toFixed(4)} · 4 runs`);
    const managerCells = [...bodies[1].matchAll(/<td[^>]*>(.*?)<\/td>/g)].map((m) => text(m[1]));
    expect(managerCells.slice(1)).toEqual(["1", "30", "4", "50", "6", "$0.0200"]);
  });

  it("omits Manager rows when none of its turns belongs to the task", () => {
    expect(text(renderToStaticMarkup(<TaskCostBreakdown cost={{ ...COST, manager_by_model: [] }} />)))
      .not.toContain("Manager ·");
  });

  it("labels unrecorded Manager models and joins their caveats", () => {
    const cost = { ...COST, manager_by_model: [{ ...COST.manager_by_model[0], model: null, running: 1 }] };
    const shown = text(renderToStaticMarkup(<TaskCostBreakdown cost={cost} />));
    expect(shown).toContain("Manager · Not recorded");
    expect(shown).toContain("Not counted in the task's cost or spend cap · 1 running");
  });

  it("labels a missing model and omits an empty model breakdown", () => {
    expect(text(renderToStaticMarkup(<TaskCostBreakdown cost={{ ...COST, by_model: [{ ...MODEL, model: null }] }} />)))
      .toContain("Not recorded");
    const html = renderToStaticMarkup(<TaskCostBreakdown cost={{ ...COST, by_model: [], manager_by_model: [] }} />);
    expect(html).not.toContain("By model");
    expect(html).not.toContain("<table");
  });
});

describe("modelRunCaveats", () => {
  it.each([
    [0, 0, 0, ""],
    [1, 0, 0, "1 running"],
    [0, 2, 0, "2 without a cost"],
    [0, 0, 1, "1 interrupted"],
    [1, 1, 1, "1 running, 1 without a cost, 1 interrupted"],
  ])("describes %i running, %i unpriced and %i interrupted runs", (running, unpriced_runs, interrupted_runs, expected) => {
    expect(modelRunCaveats({ ...MODEL, running, unpriced_runs, interrupted_runs })).toBe(expected);
  });
});
