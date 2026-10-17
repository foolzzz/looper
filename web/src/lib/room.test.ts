import { describe, expect, it } from "vitest";
import type { RoomMessage, RoomView } from "@/lib/api";
import { filterMessages, mergeRoom, roomNote, roomRoles } from "@/lib/room";

const message = (id: number, extra: Partial<RoomMessage> = {}): RoomMessage => ({
  id, from: "backend_rd@s1", to: "all", kind: "info", body: `message ${id}`, at: "2026-10-17T08:00:00Z",
  cc_planner: false, answered: false, ...extra,
});

const room = (messages: RoomMessage[], extra: Partial<RoomView> = {}): RoomView => ({
  enabled: true, seats: [], messages, more: false,
  counts: { messages: messages.length, by_kind: {}, questions: 0, unanswered: 0 }, by_step: {}, ...extra,
});

describe("mergeRoom", () => {
  it("takes a whole read as it is", () => {
    const next = room([message(4)]);
    expect(mergeRoom(room([message(1)]), next, false)).toBe(next);
  });

  it("appends the newer messages once, marks the questions they answer, and keeps the older-ones flag", () => {
    const question = message(2, { kind: "question", to: ["fe_rd@s2"] });
    const prev = room([message(1), question], { more: true });
    const next = room([message(2), message(3, { kind: "answer", reply_to: 2, from: "fe_rd@s2" })], {
      enabled: false, seats: [{ seat: "fe_rd@s2", role: "fe_rd", kind: "work", step: "s2", step_status: "dispatched",
                                presence: "online" }],
    });
    const merged = mergeRoom(prev, next, true);
    expect(merged.messages.map((m) => m.id)).toEqual([1, 2, 3]);
    expect(merged.messages[1]).toMatchObject({ id: 2, answered: true });
    expect(merged).toMatchObject({ more: true, enabled: false, seats: next.seats });
  });
});

describe("filterMessages", () => {
  const messages = [
    message(1, { kind: "interface" }),
    message(2, { kind: "question", from: "fe_rd@s2", to: ["backend_rd"] }),
    message(3, { kind: "question", from: "fe_rd@s2", to: ["planner"], answered: true }),
    message(4, { kind: "info", from: "planner", to: "all" }),
    message(5, { kind: "info", from: "planner", to: ["backend_rd@s1"] }),
  ];
  const ids = (shown: RoomMessage[]) => shown.map((m) => m.id);

  it.each([
    ["all", "", [1, 2, 3, 4, 5]],
    ["interface", "", [1]],
    ["unanswered", "", [2]],
    // From the role, or to it or one of its seats; a message to all is not counted as to it.
    ["all", "backend_rd", [1, 2, 5]],
    ["all", "planner", [3, 4, 5]],
    ["unanswered", "fe_rd", [2]],
  ] as const)("%s, role %j", (filter, role, expected) => {
    expect(ids(filterMessages(messages, filter, role))).toEqual(expected);
  });
});

it("lists the Planner, then each role of the roster once", () => {
  const seat = (seat: string, role: string) =>
    ({ seat, role, kind: "work", step: seat.split("@")[1], step_status: "pending", presence: "planned" }) as const;
  expect(roomRoles([seat("backend_rd@s1", "backend_rd"), seat("reviewer@s1", "reviewer"), seat("backend_rd@s3", "backend_rd")]))
    .toEqual(["planner", "backend_rd", "reviewer"]);
});

it.each([
  [undefined, ""],
  [{ sent: 2, received: 1, unanswered: 0 }, "✉ 3"],
  [{ sent: 0, received: 1, unanswered: 1 }, "✉ 1 · 1 unanswered"],
])("a step's room note for %j", (counts, note) => {
  expect(roomNote(counts)).toBe(note);
});
