import { describe, expect, test } from "bun:test";
import { mkdirSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { Bridge } from "../src/bridge.ts";
import { createSwarmExtension } from "../src/index.ts";
import { BAR_RANK, createRestack, RESTACK_CHANNEL } from "../src/bar_stack.ts";
import type { EventBus, ExtensionContext, SessionEntry, WidgetComponent, WidgetFactory, WidgetHost } from "../src/omp-api.ts";
import { LIFECYCLE_CHANNEL, PROGRESS_CHANNEL, SWARM_BAR_KEY, TOKEN_CHANNEL, type BarTimers } from "../src/swarm_bar.ts";
import { displayWidth, stripAnsi, type BarTheme } from "../src/swarm_dock.ts";
import { fakeCtx, fakePi, type FakePi, gitRepo, isolateEnv, REPO_ROOT, tmpDir } from "./helpers.ts";

isolateEnv("SWARM_BAR", "SWARM_DIR", "SWARM_AGENT");

const theme: BarTheme = {
  fg: (color, text) => `\x1b[36m${text}\x1b[39m`,
  bold: (text) => `\x1b[1m${text}\x1b[22m`,
  getSpinnerFrames: () => ["⠋", "⠙"],
};

/** omp's EventBus: handlers run synchronously on emit, `on` returns an unsubscribe. */
class FakeBus implements EventBus {
  readonly listeners = new Map<string, Set<(data: unknown) => void>>();
  emit(channel: string, data: unknown): void {
    for (const handler of this.listeners.get(channel) ?? []) handler(data);
  }
  on(channel: string, handler: (data: unknown) => void): () => void {
    const set = this.listeners.get(channel) ?? new Set();
    set.add(handler);
    this.listeners.set(channel, set);
    return () => void set.delete(handler);
  }
  count(): number {
    return [...this.listeners.values()].reduce((sum, set) => sum + set.size, 0);
  }
}

/** Timers the test turns by hand: `turn()` is one macrotask, so a timer queued by a timer fires on the NEXT turn. */
class FakeTimers implements BarTimers {
  private seq = 0;
  readonly timeouts = new Map<number, { fn: () => void; ms: number }>();
  readonly intervals = new Map<number, () => void>();
  readonly intervalMs = new Map<number, number>();
  setTimeout(fn: () => void, ms: number): unknown {
    this.timeouts.set(++this.seq, { fn, ms });
    return this.seq;
  }
  clearTimeout(handle: unknown): void {
    this.timeouts.delete(handle as number);
  }
  setInterval(fn: () => void, ms: number): unknown {
    this.intervals.set(++this.seq, fn);
    this.intervalMs.set(this.seq, ms);
    return this.seq;
  }
  clearInterval(handle: unknown): void {
    this.intervals.delete(handle as number);
    this.intervalMs.delete(handle as number);
  }
  /** Intervals running at `ms`: the 1 Hz ticker is 1000, the idle watcher defaults to 2000. */
  count(ms: number): number {
    return [...this.intervalMs.values()].filter((value) => value === ms).length;
  }
  turn(): void {
    const due = [...this.timeouts].filter(([, t]) => t.ms === 0);
    for (const [id, t] of due) {
      this.timeouts.delete(id);
      t.fn();
    }
  }
  /** The non-zero timeouts (the linger wake). */
  wakeUp(): void {
    for (const [id, t] of [...this.timeouts].filter(([, entry]) => entry.ms > 0)) {
      this.timeouts.delete(id);
      t.fn();
    }
  }
  tickInterval(): void {
    for (const fn of [...this.intervals.values()]) fn();
  }
}

interface WidgetCall {
  op: "widget";
  key: string;
  /** `undefined` removes the widget. */
  content: string[] | WidgetFactory | undefined;
  options?: { placement?: string };
}

/** The host side of `ctx.ui`: widgets stack in insertion order, re-setting a key moves it to the bottom and disposes the old component. */
function fakeUi(options: { throwOnWidget?: boolean; theme?: BarTheme } = {}) {
  const calls: WidgetCall[] = [];
  const stack = new Map<string, WidgetComponent | string[]>();
  const statuses: Array<{ key: string; text: string | undefined }> = [];
  const tui = { renders: 0, requestRender() { this.renders++; } } satisfies WidgetHost & { renders: number };
  let throwOnWidget = options.throwOnWidget ?? false;
  const ui = {
    notify() {},
    /** The footer status channel: every write is recorded, so a footer line the bar draws is seen by the tests, never dropped. */
    setStatus(key: string, text: string | undefined) {
      statuses.push({ key, text });
    },
    setWidget(key: string, content: string[] | WidgetFactory | undefined, widgetOptions?: { placement?: string }) {
      if (throwOnWidget) throw new Error("host widget failure");
      calls.push({ op: "widget", key, content, options: widgetOptions });
      const old = stack.get(key);
      if (old && !Array.isArray(old)) old.dispose?.();
      stack.delete(key);
      if (content === undefined) return;
      // a partial theme stands in for omp's Theme: the bar reads it defensively
      stack.set(key, typeof content === "function" ? content(tui, (options.theme ?? theme) as never) : content);
    },
  };
  return {
    ui,
    tui,
    calls,
    stack,
    statuses,
    widgetCalls: () => calls.filter((call): call is WidgetCall => call.op === "widget"),
    order: () => [...stack.keys()],
    /** The component's rows exactly as rendered (colours stripped, padding kept). */
    rows(width = 120): string[] {
      const component = stack.get(SWARM_BAR_KEY);
      return component && !Array.isArray(component) ? [...component.render(width)].map((line) => stripAnsi(line)) : [];
    },
    /** The same rows without their trailing padding, for content checks. */
    lines(width = 120): string[] {
      const component = stack.get(SWARM_BAR_KEY);
      return component && !Array.isArray(component) ? [...component.render(width)].map((line) => stripAnsi(line).trimEnd()) : [];
    },
    failWidgets(on: boolean) {
      throwOnWidget = on;
    },
  };
}

/** A wall clock pinned far from the real one: every mtime a test writes is derived from it, never from `Date.now()`. */
const FIXED_WALL = Date.UTC(2001, 8, 9);

interface Harness {
  pi: FakePi;
  bus: FakeBus;
  timers: FakeTimers;
  host: ReturnType<typeof fakeUi>;
  ctx: ExtensionContext;
  stderr: string[];
  clock: { mono: number };
  home: string;
  fire(event: string, payload?: unknown): unknown;
}

function harness(
  options: {
    ctx?: Partial<ExtensionContext>;
    env?: Record<string, string | undefined>;
    swarmDir?: string;
    wallNow?: () => number;
    entries?: SessionEntry[];
    hostOptions?: { throwOnWidget?: boolean; theme?: BarTheme };
    activeTools?: string[];
    sessionId?: () => unknown;
  } = {},
): Harness {
  const home = tmpDir("swarm-home-");
  const bus = new FakeBus();
  const timers = new FakeTimers();
  const host = fakeUi(options.hostOptions);
  const stderr: string[] = [];
  const clock = { mono: 50_000 };
  const bridge: Bridge = async () => ({ code: 0, stdout: "{}", stderr: "", json: {} }) as never;
  const pi = fakePi({ activeTools: options.activeTools });
  pi.api.events = bus;
  const dir = options.swarmDir;
  pi.load(
    createSwarmExtension({
      bridge,
      bar: {
        env: { HOME: home, ...options.env },
        now: () => clock.mono,
        wallNow: options.wallNow ?? (() => FIXED_WALL),
        timers,
        stderr: (text) => void stderr.push(text),
        swarmDir: dir === undefined ? () => join(tmpDir("swarm-nolive-"), "none") : () => dir,
      },
    }),
  );
  const entries = options.entries ?? [];
  const base = fakeCtx(gitRepo(), entries);
  const sessionId = options.sessionId;
  const ctx: ExtensionContext = {
    ...base,
    ...(sessionId ? { sessionManager: { ...base.sessionManager, getSessionId: sessionId } } : {}),
    ui: host.ui,
    mode: "tui",
    hasUI: true,
    agent: { kind: "main", id: "Main", name: "main", depth: 0 },
    ...options.ctx,
  };
  return {
    pi,
    bus,
    timers,
    host,
    home,
    ctx,
    stderr,
    clock,
    fire: (event, payload = {}) => pi.handler(event)(payload, ctx),
  };
}

function frame(id: string, agent: string, status: string, durationMs = 0, extra: Record<string, unknown> = {}) {
  return {
    index: 0,
    agent,
    task: "t",
    progress: { index: 0, id, agent, status, task: "t", toolCount: 1, requests: 1, tokens: 1, cost: 0, durationMs, ...extra },
  };
}

/** The two rows of the bar when nothing runs, and when only the telemetry marker is up (no token snapshot). */
const IDLE = ["○ swarm idle", "no subagents running"];
const TELEMETRY = ["swarm: telemetry unavailable", "no subagents running"];

/** Zero-delay timeouts: the deferred remount. */
const pendingRemounts = (h: Harness): number => [...h.timers.timeouts.values()].filter((t) => t.ms === 0).length;

/** Timeouts with a delay: the linger wake-up and the one-shot trailing token render. */
const delayed = (h: Harness): Array<{ fn: () => void; ms: number }> => [...h.timers.timeouts.values()].filter((t) => t.ms > 0);

/** A valid `token-opt:savings` v1 snapshot: 5k before, 3.8k now, saved 1.2k. */
function savings(over: Record<string, unknown> = {}): Record<string, unknown> {
  return { v: 1, sessionId: "mine", updatedAt: 1, baselineTokens: 5000, currentTokens: 3800, savedTokens: 1200, points: [], ...over };
}

describe("mounting", () => {
  const started = { id: "a", agent: "a05-backend", status: "started", index: 0 };
  const movers = ["agent_start", "turn_start", "before_agent_start", "tool_execution_start", "tool_execution_end", "turn_end", "agent_end", "session_start"];

  test("an eligible session mounts at its first event and shows the idle bar: key agent-swarm, aboveEditor, a component factory", () => {
    const h = harness();
    expect(h.host.calls).toEqual([]); // extension load touches nothing
    h.fire("session_start");
    const widgets = h.host.widgetCalls();
    expect(widgets).toHaveLength(1);
    expect(widgets[0]).toMatchObject({ key: SWARM_BAR_KEY, options: { placement: "aboveEditor" } });
    expect(typeof widgets[0].content).toBe("function");
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("whichever eligible event comes first mounts the bar, once", () => {
    for (const event of ["session_start", "session_switch", "agent_start", "turn_start", "before_agent_start", "turn_end", "tool_execution_start", "tool_execution_end", "agent_end"]) {
      const h = harness();
      h.fire(event);
      expect({ event, order: h.host.order(), mounts: h.host.widgetCalls().length }).toEqual({ event, order: [SWARM_BAR_KEY], mounts: 1 });
    }
  });

  test("an accepted `task` call is an eligible first event too", () => {
    const h = harness();
    h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ agent: "a05-backend" }] } });
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()[0]).toContain("1 run");
  });

  test("the bar stays mounted when everything lingered out: it returns to the idle rows and never unmounts", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(LIFECYCLE_CHANNEL, started);
    h.bus.emit(LIFECYCLE_CHANNEL, { ...started, status: "completed" });
    expect(h.host.lines()[0]).toContain("1 done");
    h.clock.mono += 21_000;
    h.timers.wakeUp();
    expect(h.host.lines()).toEqual(IDLE);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    h.fire("turn_end");
    h.timers.turn();
    expect(h.host.lines()).toEqual(IDLE);
    h.bus.emit(LIFECYCLE_CHANNEL, { ...started, id: "b", agent: "a06-frontend" });
    expect(h.host.lines()[1]).toContain("a06-frontend");
    expect(h.host.widgetCalls().filter((call) => call.content === undefined)).toEqual([]);
  });

  test("session_start subscribes progress, lifecycle, token and restack once; extension load subscribes none", () => {
    const h = harness();
    expect(h.bus.count()).toBe(0);
    h.fire("session_start");
    for (const channel of [PROGRESS_CHANNEL, LIFECYCLE_CHANNEL, TOKEN_CHANNEL, RESTACK_CHANNEL]) {
      expect({ channel, listeners: h.bus.listeners.get(channel)?.size }).toEqual({ channel, listeners: 1 });
    }
    h.fire("session_start");
    h.fire("turn_start");
    expect(h.bus.count()).toBe(4);
  });

  test("remount discipline: every position-moving event schedules ONE deferred remount, a single tick later", () => {
    const h = harness();
    h.fire("session_start");
    expect(h.host.widgetCalls()).toHaveLength(1);
    expect(pendingRemounts(h)).toBe(1);
    h.timers.turn(); // the one deferral: the remount
    expect(h.host.widgetCalls()).toHaveLength(2);
    h.timers.turn(); // no second tick
    expect(h.host.widgetCalls()).toHaveLength(2);
    expect(pendingRemounts(h)).toBe(0);

    // every trigger of one burst still produces a single remount
    for (const event of movers) h.fire(event);
    h.fire("tool_execution_start");
    expect(pendingRemounts(h)).toBe(1);
    h.timers.turn();
    expect(h.host.widgetCalls()).toHaveLength(3);
    h.timers.turn();
    h.timers.turn();
    expect(h.host.widgetCalls()).toHaveLength(3);
  });

  test("each event Ultrathink also remounts on schedules the remount", () => {
    for (const event of movers) {
      const h = harness();
      h.fire("session_start");
      h.timers.turn();
      const before = h.host.widgetCalls().length;
      h.fire(event);
      h.timers.turn();
      expect({ event, remounted: h.host.widgetCalls().length - before }).toEqual({ event, remounted: 1 });
    }
  });

  test("an event while a remount is pending is absorbed by it", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("turn_start");
    h.timers.turn();
    expect(h.host.widgetCalls()).toHaveLength(2);
    h.timers.turn();
    expect(h.host.widgetCalls()).toHaveLength(2);
  });

  test("an event after the remount ran schedules a new one", () => {
    const h = harness();
    h.fire("session_start");
    h.timers.turn();
    h.fire("turn_end");
    expect(pendingRemounts(h)).toBe(1);
    h.timers.turn();
    expect(h.host.widgetCalls()).toHaveLength(3);
  });

  test("every mount sets the widget THEN announces rank 10, so the announcement sees the bar already in place", () => {
    const h = harness();
    const heard: Array<{ payload: unknown; order: string[] }> = [];
    h.bus.on(RESTACK_CHANNEL, (payload) => void heard.push({ payload, order: h.host.order() }));
    h.fire("session_start");
    expect(heard).toEqual([{ payload: { v: 1, after: BAR_RANK.swarm }, order: [SWARM_BAR_KEY] }]);
    h.timers.turn(); // the deferred remount announces again
    expect(heard).toHaveLength(2);
    expect(heard[1]).toEqual(heard[0]);
  });

  test("the bar is last after other bars' remount that was queued for the same tick", () => {
    const h = harness();
    h.host.ui.setWidget("ultrathink", ["u"]);
    h.host.ui.setWidget("aio-chrome", ["c"]);
    // their handlers for the same event ran first and queued their own remount
    h.timers.setTimeout(() => {
      h.host.ui.setWidget("ultrathink", ["u"]);
      h.host.ui.setWidget("aio-chrome", ["c"]);
    }, 0);
    h.fire("session_start"); // mounted at the event: already after the other two
    expect(h.host.order()).toEqual(["ultrathink", "aio-chrome", SWARM_BAR_KEY]);
    h.timers.turn(); // theirs, then ours: one tick is enough
    expect(h.host.order()).toEqual(["ultrathink", "aio-chrome", SWARM_BAR_KEY]);
  });

  test("bus frames only request a render once mounted: no setWidget", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(LIFECYCLE_CHANNEL, { ...started, id: "first", agent: "first" });
    h.timers.turn();
    const widgetsBefore = h.host.widgetCalls().length;
    const rendersBefore = h.host.tui.renders;
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "running", 1000));
    h.bus.emit(LIFECYCLE_CHANNEL, started);
    expect(h.host.widgetCalls()).toHaveLength(widgetsBefore);
    expect(h.host.tui.renders).toBeGreaterThan(rendersBefore);
    expect(h.host.lines().join("\n")).toContain("a05-backend");
  });

  test("the telemetry-error marker is on the bar from the first event", () => {
    const dir = join(tmpDir("swarm-marker-"), ".swarm");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "live.json"), "{ not json");
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()).toEqual(TELEMETRY);
  });

  test("a host that fails the first setWidget does not break the session, and the deferred remount mounts the bar", () => {
    const h = harness({ hostOptions: { throwOnWidget: true } });
    expect(() => h.fire("session_start")).not.toThrow();
    expect(h.host.order()).toEqual([]);
    h.host.failWidgets(false);
    h.timers.turn();
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
  });
});

describe("ticker, status and lingering", () => {
  test("the 1 Hz ticker exists only while something is live; settled rows linger, then everything is cleared", () => {
    const h = harness();
    h.fire("session_start");
    expect(h.timers.count(1000)).toBe(0);
    expect(h.timers.count(2000)).toBe(1); // the idle watcher
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
    expect(h.timers.count(1000)).toBe(1);
    expect(h.timers.intervals.size).toBe(1); // the watcher yields to the ticker: never two pollers
    expect(h.host.lines()[0]).toContain("1 run");

    h.clock.mono += 7000;
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toMatch(/a05-backend\s+7s/);
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "completed", index: 0 });
    expect(h.timers.count(1000)).toBe(0);
    expect(h.timers.count(2000)).toBe(1);
    expect(h.host.lines()[1]).toMatch(/✓ a05-backend\s+7s/);

    // the settled row lingers 20 s and one wake-up removes it
    expect([...h.timers.timeouts.values()].some((t) => t.ms > 0)).toBe(true);
    h.clock.mono += 21_000;
    h.timers.wakeUp();
    expect(h.host.lines()).toEqual(IDLE);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect([...h.timers.timeouts.values()].filter((t) => t.ms > 0)).toHaveLength(0);
  });

  test("a failed run lingers for the long window and puts a failure count in the status line", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "running", 0));
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "failed", 5000));
    expect(h.host.lines()[0]).toContain("1 fail");
    h.clock.mono += 60_000;
    h.timers.wakeUp();
    expect(h.host.lines().join("\n")).toContain("✕ FAIL");
    h.clock.mono += 600_000;
    h.timers.wakeUp();
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("the linger windows and reduced motion come from the environment", () => {
    const h = harness({ env: { SWARM_BAR_LINGER_S: "2", SWARM_BAR_FAIL_LINGER_S: "4", SWARM_BAR_REDUCED_MOTION: "1" } });
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a", "running", 0));
    expect(h.host.lines()[0]).toContain("●");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a", "completed", 1000));
    h.clock.mono += 2001;
    h.timers.wakeUp();
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("an active turn keeps the ticker (live.json may appear while the model thinks); agent_end releases it", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("turn_start");
    expect(h.timers.count(1000)).toBe(1);
    h.fire("agent_end");
    expect(h.timers.count(1000)).toBe(0);
    h.fire("tool_execution_start");
    expect(h.timers.count(1000)).toBe(1);
    h.fire("tool_execution_end");
    expect(h.timers.count(1000)).toBe(0);
  });
});

describe("who gets a bar", () => {
  test("a subagent session mounts nothing and subscribes to nothing", () => {
    const h = harness({ ctx: { agent: { kind: "sub", id: "0-Explore", name: "explore", depth: 1, parentId: "Main" } } });
    h.fire("session_start");
    h.fire("turn_start");
    h.timers.turn();
    h.timers.turn();
    expect(h.host.calls).toEqual([]);
    expect(h.bus.count()).toBe(0);
    expect(h.stderr).toEqual([]);
  });

  test("a child session (session_init entry) mounts nothing even where the host sends no agent identity", () => {
    const h = harness({ ctx: { agent: undefined }, entries: [{ type: "session_init", agent: "a05-backend" }] });
    h.fire("session_start");
    expect(h.host.calls).toEqual([]);
    expect(h.bus.count()).toBe(0);
  });

  test("a subagent, SWARM_BAR=off and RPC sessions never subscribe the token or restack channels, read no token file and answer no restack", () => {
    const home = tmpDir("swarm-nosubs-");
    mkdirSync(join(home, ".omp", "agent", "token-opt"), { recursive: true });
    writeFileSync(join(home, ".omp", "agent", "token-opt", "live.json"), JSON.stringify(savings()));
    const cases: Array<{ env?: Record<string, string>; ctx?: Partial<ExtensionContext> }> = [
      { env: { SWARM_BAR: "off" } },
      { ctx: { agent: { kind: "sub", id: "0-Explore", name: "explore", depth: 1, parentId: "Main" } } },
      { ctx: { mode: "rpc" } },
    ];
    for (const [index, { env, ctx }] of cases.entries()) {
      const h = harness({ env: { ...env, HOME: home }, ctx });
      h.fire("session_start");
      h.fire("agent_start");
      h.bus.emit(TOKEN_CHANNEL, savings());
      h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
      h.timers.turn();
      expect({ index, subs: h.bus.count(), calls: h.host.calls, renders: h.host.tui.renders, timers: h.timers.timeouts.size + h.timers.intervals.size }).toEqual({
        index,
        subs: 0,
        calls: [],
        renders: 0,
        timers: 0,
      });
    }
  });

  test("RPC mode, no UI and a context without ui mount nothing", () => {
    for (const ctx of [{ mode: "rpc" as const }, { hasUI: false, mode: "rpc" as const }, { ui: undefined }, { mode: undefined }, { hasUI: false, mode: undefined }]) {
      const h = harness({ ctx });
      h.fire("session_start");
      h.fire("tool_execution_start");
      h.timers.turn();
      h.timers.turn();
      expect({ ctx, calls: h.host.calls, subs: h.bus.count(), err: h.stderr }).toEqual({ ctx, calls: [], subs: 0, err: [] });
    }
  });

  test("SWARM_BAR=off disables the bar everywhere", () => {
    for (const ctx of [{}, { hasUI: false, mode: "print" as const, ui: undefined }]) {
      const h = harness({ env: { SWARM_BAR: " OFF " }, ctx });
      h.fire("session_start");
      h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
      expect(h.host.calls).toEqual([]);
      expect(h.stderr).toEqual([]);
    }
  });

  test("a plain fake context (no ui, no mode) keeps the pre-existing handlers silent", () => {
    const pi = fakePi();
    pi.load(createSwarmExtension({ bridge: (async () => ({})) as never }));
    for (const event of ["session_start", "turn_start", "tool_execution_start", "agent_end"]) {
      expect(pi.handler(event)({}, fakeCtx(gitRepo()))).toBeUndefined();
    }
  });
});

describe("headless -p", () => {
  function headless(env: Record<string, string | undefined> = {}) {
    return harness({ env, ctx: { hasUI: false, mode: "print", ui: undefined } });
  }

  test("one stderr line on spawn and one on settle, nothing in between, and no widget", () => {
    const h = headless();
    h.fire("session_start");
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
    h.clock.mono += 3000;
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "running", 3000, { currentTool: "edit" }));
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "running", 3100, { currentTool: "read" }));
    expect(h.stderr).toEqual(["[swarm] a05-backend starting 0s\n"]);
    h.clock.mono += 62_000;
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "completed", 65_000));
    expect(h.stderr).toEqual(["[swarm] a05-backend starting 0s\n", "[swarm] a05-backend completed 1m05s\n"]);
    expect(h.host.calls).toEqual([]);
    expect(h.timers.timeouts.size + h.timers.intervals.size).toBe(0);
  });

  test("a headless session listens to its two stderr channels only: no token channel, no restack, no widget", () => {
    const h = headless();
    h.fire("session_start");
    expect(h.bus.count()).toBe(2);
    expect([...h.bus.listeners.keys()].sort()).toEqual([LIFECYCLE_CHANNEL, PROGRESS_CHANNEL]);
    h.bus.emit(TOKEN_CHANNEL, savings());
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    expect(h.host.calls).toEqual([]);
    expect(h.host.tui.renders).toBe(0);
  });

  test("a failed settle and staggered agents each print their own lines", () => {
    const h = headless();
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "agent-a", "running", 0));
    h.bus.emit(PROGRESS_CHANNEL, frame("b", "agent-b", "running", 0));
    h.bus.emit(PROGRESS_CHANNEL, frame("b", "agent-b", "failed", 4000));
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "agent-a", "completed", 9000));
    expect(h.stderr).toEqual([
      "[swarm] agent-a running 0s\n",
      "[swarm] agent-b running 0s\n",
      "[swarm] agent-b failed 4s\n",
      "[swarm] agent-a completed 9s\n",
    ]);
  });

  test("agent text on the line is sanitized", () => {
    const h = headless();
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "evil\x1b[31m-agent\r\nsecond line", "running", 0));
    expect(h.stderr).toHaveLength(1);
    expect(h.stderr[0]).toBe("[swarm] evil-agent second line running 0s\n");
  });

  test("json mode prints too; a settled-on-arrival run prints once", () => {
    const h = harness({ ctx: { hasUI: false, mode: "json", ui: undefined } });
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a", "completed", 2000));
    expect(h.stderr).toEqual(["[swarm] a completed 2s\n"]);
  });

  test("headless sessions trim settled rows as events arrive: a frame after the linger starts a fresh row", () => {
    const h = harness({ ctx: { hasUI: false, mode: "print", ui: undefined } });
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "agent-a", "running", 0));
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "agent-a", "completed", 65_000));
    expect(h.stderr).toHaveLength(2);
    // within the linger a stale "running" frame for the settled id is ignored
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "agent-a", "running", 65_000));
    expect(h.stderr).toHaveLength(2);
    h.clock.mono += 21_000;
    h.bus.emit(PROGRESS_CHANNEL, frame("b", "agent-b", "running", 0)); // any event trims, there is no ticker in -p mode
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "agent-a", "running", 65_000));
    expect(h.stderr.at(-1)).toBe("[swarm] agent-a running 1m05s\n");
  });
});

describe("teardown", () => {
  test("session_shutdown unsubscribes, clears timers and removes the widget", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
    expect(h.bus.count()).toBe(4);
    expect(h.timers.intervals.size).toBe(1);
    expect(h.timers.timeouts.size).toBeGreaterThan(0);
    h.fire("session_shutdown");
    expect(h.bus.count()).toBe(0);
    expect(h.timers.intervals.size).toBe(0);
    expect([...h.timers.timeouts.values()]).toHaveLength(0);
    expect(h.host.order()).toEqual([]);
    expect(h.host.widgetCalls().at(-1)).toMatchObject({ key: SWARM_BAR_KEY, content: undefined });
    expect(h.host.calls.at(-1)).toBe(h.host.widgetCalls().at(-1));
    // late bus frames and events no longer do anything
    const before = h.host.calls.length;
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "b", agent: "x", status: "started", index: 0 });
    h.timers.turn();
    h.timers.turn();
    expect(h.host.calls).toHaveLength(before);
    h.fire("session_shutdown");
  });

  test("session_switch tears the old session down and the new one is mounted again at once, idle and without the old rows", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "old-agent", status: "started", index: 0 });
    expect(h.host.lines().join("\n")).toContain("old-agent");
    h.fire("session_switch");
    expect(h.bus.count()).toBe(4);
    expect(h.timers.count(1000)).toBe(0);
    expect(h.timers.count(2000)).toBe(1); // only the new session's idle watcher
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()).toEqual(IDLE);
    expect(h.host.widgetCalls().filter((call) => call.content === undefined)).toHaveLength(1);
    h.timers.turn();
    h.timers.turn();
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "b", agent: "new-agent", status: "started", index: 0 });
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines().join("\n")).toContain("new-agent");
    expect(h.host.lines().join("\n")).not.toContain("old-agent");
  });

  test("a remount that was pending at shutdown never fires", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("session_shutdown");
    const before = h.host.widgetCalls().length;
    h.timers.turn();
    h.timers.turn();
    expect(h.host.widgetCalls()).toHaveLength(before);
  });

  test("events after session_shutdown never restart the bar (omp aborts the agent after it emits shutdown)", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
    h.fire("session_shutdown");
    const before = h.host.calls.length;
    // the in-flight task call settles, and the agent loop winds down: all of it arrives after shutdown
    h.fire("tool_call", { toolName: "task", toolCallId: "call-9", input: { tasks: [{ agent: "a05-backend" }] } });
    h.fire("tool_result", { toolName: "task", toolCallId: "call-9", isError: false });
    h.fire("tool_execution_end", { toolName: "task", toolCallId: "call-9", isError: true });
    h.fire("turn_end");
    h.fire("agent_end");
    h.timers.turn();
    h.timers.turn();
    expect(h.bus.count()).toBe(0);
    expect(h.host.calls).toHaveLength(before);
    expect(h.timers.intervals.size).toBe(0);
    expect(h.timers.timeouts.size).toBe(0);
  });

  test("a new session_start after shutdown reopens the bar", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("session_shutdown");
    h.fire("session_start");
    expect(h.bus.count()).toBe(4);
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
  });

  test("session_shutdown disposes every subscription (progress, lifecycle, token, restack) and the pending trailing render", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(TOKEN_CHANNEL, savings()); // inside the render gap: a one-shot trailing render is queued
    expect(delayed(h)).toHaveLength(1);
    h.fire("session_shutdown");
    for (const channel of [PROGRESS_CHANNEL, LIFECYCLE_CHANNEL, TOKEN_CHANNEL, RESTACK_CHANNEL]) {
      expect({ channel, listeners: h.bus.listeners.get(channel)?.size }).toEqual({ channel, listeners: 0 });
    }
    expect(h.timers.timeouts.size).toBe(0);
    expect(h.timers.intervals.size).toBe(0);
    expect(h.host.order()).toEqual([]);
    const calls = h.host.calls.length;
    const renders = h.host.tui.renders;
    h.bus.emit(TOKEN_CHANNEL, savings({ currentTokens: 3700, savedTokens: 1300 }));
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    h.clock.mono += 1000;
    h.timers.turn();
    h.timers.wakeUp();
    expect(h.host.calls).toHaveLength(calls);
    expect(h.host.tui.renders).toBe(renders);
  });

  test("session_switch leaves exactly one listener per channel: a lower-rank announcement remounts once", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("session_switch");
    h.fire("session_switch");
    for (const channel of [PROGRESS_CHANNEL, LIFECYCLE_CHANNEL, TOKEN_CHANNEL, RESTACK_CHANNEL]) {
      expect({ channel, listeners: h.bus.listeners.get(channel)?.size }).toEqual({ channel, listeners: 1 });
    }
    const before = h.host.widgetCalls().length;
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    expect(h.host.widgetCalls()).toHaveLength(before + 1);
  });
});

describe("failure isolation", () => {
  test("a host that throws on setWidget or requestRender never throws into a handler", () => {
    const h = harness();
    h.fire("session_start");
    h.host.failWidgets(true);
    h.host.tui.requestRender = () => {
      throw new Error("render failure");
    };
    for (const event of ["session_start", "turn_start", "before_agent_start", "tool_execution_start", "tool_execution_end", "agent_end", "session_switch"]) {
      expect(() => h.fire(event, { prompt: "hello" })).not.toThrow();
    }
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a", status: "started", index: 0 });
    expect(() => {
      h.timers.turn();
      h.timers.turn();
      h.timers.tickInterval();
      h.fire("session_shutdown");
    }).not.toThrow();
  });

  test("garbage bus payloads are ignored", () => {
    const h = harness();
    h.fire("session_start");
    for (const payload of [null, undefined, 3, "x", [], {}, { progress: null }, { status: "started" }, { progress: { status: "running" } }]) {
      expect(() => {
        h.bus.emit(PROGRESS_CHANNEL, payload);
        h.bus.emit(LIFECYCLE_CHANNEL, payload);
      }).not.toThrow();
    }
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("the task tool_call decision is the guard's, whether or not the bar works", () => {
    const input = { tasks: [{ id: "be", agent: "a05-backend", description: "x", assignment: "y" }] };
    const event = { toolName: "task", toolCallId: "call-1", input };
    const healthy = harness();
    healthy.fire("session_start");
    const withBar = healthy.fire("tool_call", event);
    const broken = harness();
    broken.fire("session_start");
    broken.host.failWidgets(true);
    broken.host.tui.requestRender = () => {
      throw new Error("boom");
    };
    const withBrokenBar = broken.fire("tool_call", event);
    const bare = fakePi();
    bare.load(createSwarmExtension({ bridge: (async () => ({})) as never }));
    const without = bare.handler("tool_call")(event, fakeCtx(gitRepo()));
    expect(withBar).toEqual(without);
    expect(withBrokenBar).toEqual(without);
    expect(without).toBeUndefined();
  });

  test("a task call the guard blocks passes through unchanged, and no row is shown for it", () => {
    process.env.SWARM_AGENT = "a01-orchestrator"; // A01 without the `task` tool: the depth cap blocks the call
    const event = { toolName: "task", toolCallId: "call-1", input: { tasks: [{ name: "be", agent: "a05-backend" }] } };
    const h = harness({ activeTools: [] });
    h.fire("session_start");
    const withBar = h.fire("tool_call", event);
    const bare = fakePi({ activeTools: [] });
    bare.load(createSwarmExtension({ bridge: (async () => ({})) as never }));
    const without = bare.handler("tool_call")(event, fakeCtx(gitRepo()));
    expect(without).toMatchObject({ block: true });
    expect(withBar).toEqual(without);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()).toEqual(IDLE); // the blocked call got no row
  });

  test("every width gives exactly two rows of exactly that width, for a themed, a powerline, a throwing and an empty theme, idle, telemetry error or busy", () => {
    const hostile: BarTheme = {
      fg() {
        throw new Error("no such color");
      },
      bold() {
        throw new Error("no bold");
      },
      getFgAnsi() {
        throw new Error("no such color");
      },
      getBgAnsi() {
        throw new Error("no such color");
      },
      getSpinnerFrames() {
        throw new Error("no frames");
      },
    };
    const powerline: BarTheme = {
      ...theme,
      getBgAnsi: () => "\x1b[48;5;236m",
      getFgAnsi: (color: string) => (color === "text" ? "\x1b[38;5;252m" : "\x1b[38;5;240m"),
      sep: { powerlineThinLeft: "\ue0b3", powerlineCapLeft: "\ue0b6", powerlineLeft: "\ue0b0" },
    };
    const tokens = savings({ lastTurnMs: 840, cacheHitPct: 72, lastQueryMs: 15, rtkSavedTokens: 2500, lastPayloadChars: 4096, ttftMs: 120, lastIdleMs: 40, costCents: 3, providerInput: 1500, providerOutput: 300, prefixChanged: 1, toolsChanged: 1, graphReady: 1 });
    const errorDir = join(tmpDir("swarm-width-"), ".swarm");
    mkdirSync(errorDir, { recursive: true });
    writeFileSync(join(errorDir, "live.json"), "{ not json");
    const states: Record<string, { swarmDir?: string; setup: (h: Harness) => void }> = {
      idle: { setup: () => {} },
      error: { swarmDir: errorDir, setup: () => {} },
      busy: {
        setup: (h) => {
          h.bus.emit(PROGRESS_CHANNEL, frame("a", "日本語".repeat(20), "running", 0, { currentToolIntent: "🚀".repeat(80) }));
          h.bus.emit(PROGRESS_CHANNEL, frame("b", "a06-frontend", "running", 4000));
          h.bus.emit(TOKEN_CHANNEL, tokens);
        },
      },
      failed: {
        setup: (h) => {
          h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "failed", 5000));
          h.bus.emit(TOKEN_CHANNEL, tokens);
        },
      },
    };
    for (const [themeName, custom] of Object.entries({ themed: theme, powerline, throwing: hostile, empty: {} as BarTheme })) {
      for (const [stateName, state] of Object.entries(states)) {
        const h = harness({ hostOptions: { theme: custom }, swarmDir: state.swarmDir });
        h.fire("session_start");
        state.setup(h);
        const bad: string[] = [];
        for (let width = 1; width <= 300; width++) {
          const widths = h.host.rows(width).map((row) => displayWidth(row));
          if (widths.length !== 2 || widths.some((columns) => columns !== width)) bad.push(`${themeName}/${stateName}@${width}: rows ${JSON.stringify(widths)}`);
        }
        expect(bad.slice(0, 5)).toEqual([]);
        expect(h.host.rows(0)).toEqual([]);
      }
    }
  });
});

describe("task tool rows (last resort)", () => {
  test("an accepted task call shows loading rows at once, the bus takes over, the result settles what nothing richer owns", () => {
    const h = harness();
    h.fire("session_start");
    const input = { tasks: [{ id: "be", agent: "a05-backend" }, { id: "fe", agent: "a06-frontend" }] };
    expect(h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input })).toBeUndefined();
    expect(h.host.lines().join("\n")).toContain("start");
    expect(h.host.lines()[0]).toContain("2 run");
    h.clock.mono += 4000;
    h.bus.emit(PROGRESS_CHANNEL, { ...frame("be", "a05-backend", "completed", 4000), parentToolCallId: "call-1", index: 0 });
    h.fire("tool_result", { toolName: "task", toolCallId: "call-1", isError: false });
    const text = h.host.lines().join("\n");
    expect(text).toContain("a05-backend");
    expect(text).toContain("a06-frontend");
    // a05-backend completed through the bus; a06-frontend had no bus part and settled with the call
    expect(h.host.lines()[0]).toContain("2 done");
  });

  test("a task result for another tool or without an id changes nothing", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ agent: "a05-backend" }] } });
    h.fire("tool_result", { toolName: "read", toolCallId: "call-1", isError: false });
    h.fire("tool_result", { toolName: "task", isError: true });
    h.fire("tool_result", null);
    expect(h.host.lines()[0]).toContain("1 run");
  });

  test("a task call something else blocked after our guard: its tool_execution_end settles the row as cancelled, not a failure", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("turn_start");
    h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ name: "BackendWork", agent: "a05-backend" }] } });
    h.fire("tool_execution_start", { toolName: "task", toolCallId: "call-1" });
    expect(h.host.lines()[0]).toContain("1 run");
    // omp emits tool_execution_end (isError) for a blocked call, but never a tool_result
    h.fire("tool_execution_end", { toolName: "task", toolCallId: "call-1", isError: true, result: {} });
    expect(h.host.lines()[0]).toContain("1 cancel");
    expect(h.host.lines()[0]).not.toContain("1 fail");
    expect(h.host.lines()[1]).toContain("⊘ cancelled");
  });

  test("tool_execution_end of another tool never settles a task row", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ agent: "a05-backend" }] } });
    h.fire("tool_execution_end", { toolName: "read", toolCallId: "call-1", isError: true });
    h.fire("tool_execution_end", null);
    expect(h.host.lines()[0]).toContain("1 run");
  });

  test("a spawn that never settles (no tool_execution_end, no tool_result) is cancelled at agent_end and the ticker stops", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("turn_start");
    h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ agent: "a05-backend" }] } });
    expect(h.timers.count(1000)).toBe(1);
    h.fire("agent_end");
    expect(h.host.lines()[1]).toContain("⊘ cancelled");
    expect(h.timers.count(1000)).toBe(0);
    // an hour later nothing is left on screen
    h.clock.mono += 3_600_000;
    h.timers.wakeUp();
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("agent_end leaves a row the bus reports on alone: a detached swarm outlives the turn", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("turn_start");
    h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ agent: "a05-backend" }] } });
    h.bus.emit(LIFECYCLE_CHANNEL, { id: "x", agent: "a05-backend", status: "started", index: 0, parentToolCallId: "call-1", detached: true });
    h.fire("agent_end");
    expect(h.host.lines()[0]).toContain("1 run");
    expect(h.host.lines()[0]).not.toContain("cancel");
  });

  test("the `name` omp gives a task item aliases its row for the bus: one row, not two", () => {
    const h = harness();
    h.fire("session_start");
    h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ name: "BackendWork", agent: "a05-backend" }] } });
    // the bus names the run by the item's name and calls the agent something else: only the id can tie them together
    h.bus.emit(PROGRESS_CHANNEL, frame("BackendWork", "explore", "running", 1000));
    expect(h.host.lines()[0]).toContain("1 run");
    expect(h.host.lines()[0]).not.toContain("2 run");
    expect(h.host.lines().join("\n")).not.toContain("a05-backend");
  });
});

describe("live.json", () => {
  function liveDir() {
    const dir = join(tmpDir("swarm-live-"), ".swarm");
    mkdirSync(dir, { recursive: true });
    return dir;
  }
  const doc = (runs: unknown[], swarm: Record<string, unknown> = {}) => JSON.stringify({ schema: "swarm.live.v1", seq: 1, swarm: { max_parallel: 4, ...swarm }, runs });
  const running = { run_id: "T1.a1", task_id: "T1", agent: "a05-backend", state: "running", elapsed_s: 30, activity: "edit swarm/x.py" };

  test("an existing live.json is read at session_start and polled once per tick by mtime", () => {
    const dir = liveDir();
    writeFileSync(join(dir, "live.json"), doc([running], { effective_parallel: 3 }));
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()[0]).toContain("slots 1/3");
    expect(h.host.lines()[1]).toMatch(/a05-backend\s+30s edit swarm\/x\.py/);
    expect(h.timers.count(1000)).toBe(1);
    h.clock.mono += 5000;
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toMatch(/a05-backend\s+35s/);
    // the producer writes a newer snapshot: queued work shows up
    writeFileSync(join(dir, "live.json"), doc([running, { run_id: "T2.a1", task_id: "T2", agent: "a06-frontend", state: "queued" }]));
    utimesSync(join(dir, "live.json"), new Date(FIXED_WALL), new Date(FIXED_WALL + 5000));
    h.clock.mono += 1000;
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toContain("a06-frontend");
    expect(h.host.lines()[0]).toContain("1 queued");
  });

  test("no live.json is not an error", () => {
    const h = harness({ swarmDir: liveDir() });
    h.fire("session_start");
    h.fire("turn_start");
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("a malformed live.json shows the telemetry marker, never throws, and clears when it is fixed", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, "{ not json");
    const h = harness({ swarmDir: dir });
    expect(() => h.fire("session_start")).not.toThrow();
    expect(h.host.lines()).toEqual(TELEMETRY);
    writeFileSync(path, JSON.stringify({ schema: "other.v9", runs: [] }));
    utimesSync(path, new Date(FIXED_WALL), new Date(FIXED_WALL + 3000));
    h.fire("turn_start");
    expect(h.host.lines()).toEqual(TELEMETRY);
    writeFileSync(path, doc([running]));
    utimesSync(path, new Date(FIXED_WALL), new Date(FIXED_WALL + 6000));
    h.clock.mono += 1000;
    h.fire("turn_start");
    expect(h.host.lines()[1]).toContain("a05-backend");
    expect(h.host.lines().join("\n")).not.toContain("telemetry");
  });

  test("a live.json older than three heartbeats at first read, with live runs, is stale: marker, no rows", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, doc([running]));
    const old = new Date(FIXED_WALL - 100_000);
    utimesSync(path, old, old);
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()).toEqual(TELEMETRY);
  });

  test("an old live.json whose runs all settled long ago is just history: nothing shown, no marker", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, doc([{ ...running, state: "completed" }]));
    const old = new Date(FIXED_WALL - 3_600_000);
    utimesSync(path, old, old);
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("a live.json from a swarm that died days ago is history even with runs marked live: no marker, the idle bar", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, doc([running]));
    const old = new Date(FIXED_WALL - 2 * 86_400_000);
    utimesSync(path, old, old);
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    h.fire("turn_start");
    expect(h.host.lines()).toEqual(IDLE);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    // and it never turns into a warning later
    h.clock.mono += 3_600_000;
    h.fire("turn_start");
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("a dead swarm seen shortly after it died warns once, and the warning ages out with the failure linger", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, doc([running]));
    const old = new Date(FIXED_WALL - 100_000);
    utimesSync(path, old, old);
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()).toEqual(TELEMETRY);
    h.clock.mono += 601_000;
    h.fire("turn_start");
    expect(h.host.lines()).toEqual(IDLE);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
  });

  test("a snapshot that stops updating while it lists live runs goes stale on the consumer's monotonic clock", () => {
    const dir = liveDir();
    writeFileSync(join(dir, "live.json"), doc([running]));
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()[1]).toContain("a05-backend");
    h.clock.mono += 20_000;
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toContain("a05-backend");
    h.clock.mono += 11_000; // 31 s with no new snapshot
    h.timers.tickInterval();
    expect(h.host.lines()).toEqual(TELEMETRY);
  });

  test("the heartbeat environment sets the staleness window", () => {
    const dir = liveDir();
    writeFileSync(join(dir, "live.json"), doc([running]));
    const h = harness({ swarmDir: dir, env: { SWARM_HEARTBEAT_S: "1" } });
    h.fire("session_start");
    h.clock.mono += 3500;
    h.timers.tickInterval();
    expect(h.host.lines()).toEqual(TELEMETRY);
  });

  test("a removed live.json clears the marker and the live rows", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, "garbage");
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()).toEqual(TELEMETRY);
    writeFileSync(path, doc([running]));
    utimesSync(path, new Date(FIXED_WALL), new Date(FIXED_WALL + 3000));
    h.fire("turn_start");
    expect(h.host.lines()[1]).toContain("a05-backend");
    // unlink
    rmSync(path);
    h.clock.mono += 1000;
    h.timers.tickInterval();
    expect(h.host.lines()).toEqual(IDLE);
  });

  test("a wall-clock jump does not move a running timer", () => {
    const dir = liveDir();
    writeFileSync(join(dir, "live.json"), doc([running]));
    let wall = FIXED_WALL;
    const h = harness({ swarmDir: dir, wallNow: () => wall });
    h.fire("session_start");
    wall += 86_400_000;
    h.clock.mono += 10_000;
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toMatch(/a05-backend\s+40s/);
  });

  test("bus rows and live rows for different agents show side by side", () => {
    const dir = liveDir();
    writeFileSync(join(dir, "live.json"), doc([running]));
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("x", "explore", "running", 2000));
    const text = h.host.lines().join("\n");
    expect(text).toContain("a05-backend");
    expect(text).toContain("explore");
    expect(h.host.lines()[0]).toContain("2 run");
  });
});

describe("idle watcher", () => {
  function liveDir() {
    const dir = join(tmpDir("swarm-idle-"), ".swarm");
    mkdirSync(dir, { recursive: true });
    return dir;
  }
  const doc = (runs: unknown[]) => JSON.stringify({ schema: "swarm.live.v1", seq: 1, swarm: { max_parallel: 4 }, runs });
  const running = { run_id: "T1.a1", task_id: "T1", agent: "a05-backend", state: "running", elapsed_s: 30, activity: "edit swarm/x.py" };

  test("a swarm that appears while the bar is idle shows in the bar that is already mounted, and the 1 Hz ticker takes over from the watcher", () => {
    const dir = liveDir();
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.timers.intervals.size).toBe(1);
    expect(h.timers.count(2000)).toBe(1);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.lines()).toEqual(IDLE);
    const mounts = h.host.widgetCalls().length;
    writeFileSync(join(dir, "live.json"), doc([running]));
    h.clock.mono += 2000;
    h.timers.tickInterval();
    expect(h.host.widgetCalls()).toHaveLength(mounts); // nothing to mount: the bar was there
    expect(h.host.lines()[1]).toContain("a05-backend");
    expect(h.timers.count(1000)).toBe(1);
    expect(h.timers.intervals.size).toBe(1);
  });

  test("a live.json that disappears while idle clears the telemetry marker; the bar stays mounted and idle", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, "{ not json");
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()).toEqual(TELEMETRY);
    expect(h.timers.count(2000)).toBe(1);
    rmSync(path);
    h.timers.tickInterval();
    expect(h.host.lines()).toEqual(IDLE);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    expect(h.host.widgetCalls().filter((call) => call.content === undefined)).toEqual([]);
  });

  test("an unchanged file, or none, costs no setWidget or requestRender over 50 ticks", () => {
    const dir = liveDir();
    const settled = join(dir, "live.json");
    for (const present of [false, true]) {
      if (present) {
        writeFileSync(settled, doc([{ ...running, state: "completed" }]));
        const t = new Date(FIXED_WALL - 1000);
        utimesSync(settled, t, t);
      }
      const h = harness({ swarmDir: dir });
      h.fire("session_start");
      expect(h.timers.count(2000)).toBe(1);
      const calls = h.host.calls.length;
      const renders = h.host.tui.renders;
      for (let i = 0; i < 50; i++) {
        h.clock.mono += 2000;
        h.timers.tickInterval();
      }
      expect(h.host.calls.length).toBe(calls);
      expect(h.host.tui.renders).toBe(renders);
    }
  });

  test("the idle tick is a stat: same mtime and size means the file is not read again", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, doc([{ ...running, state: "completed" }]));
    const t = new Date(FIXED_WALL - 1000);
    utimesSync(path, t, t);
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    expect(h.host.lines()[1]).toContain("a05-backend");
    writeFileSync(path, doc([{ ...running, agent: "a06-backend", state: "completed" }])); // same size
    utimesSync(path, t, t);
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toContain("a05-backend");
    const later = new Date(t.getTime() + 5000);
    utimesSync(path, later, later); // an mtime change does wake the full poll
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toContain("a06-backend");
  });

  test("a file already stale at first sight stays as it was: history shows nothing, a fresh-dead one keeps its single marker", () => {
    const dir = liveDir();
    const path = join(dir, "live.json");
    writeFileSync(path, doc([running]));
    const ancient = new Date(FIXED_WALL - 2 * 86_400_000);
    utimesSync(path, ancient, ancient);
    const history = harness({ swarmDir: dir });
    history.fire("session_start");
    for (let i = 0; i < 50; i++) history.timers.tickInterval();
    expect(history.host.lines()).toEqual(IDLE);
    expect(history.host.widgetCalls()).toHaveLength(1); // the mount; the ancient file changed nothing since

    const dead = new Date(FIXED_WALL - 100_000);
    utimesSync(path, dead, dead);
    const warned = harness({ swarmDir: dir });
    warned.fire("session_start");
    expect(warned.host.lines()).toEqual(TELEMETRY);
    const calls = warned.host.calls.length;
    for (let i = 0; i < 50; i++) warned.timers.tickInterval();
    expect(warned.host.calls.length).toBe(calls);
    expect(warned.host.lines()).toEqual(TELEMETRY);
  });

  test("session_shutdown and session_switch clear the watcher; a late event does not bring it back", () => {
    const h = harness({ swarmDir: liveDir() });
    h.fire("session_start");
    expect(h.timers.intervals.size).toBe(1);
    h.fire("session_shutdown");
    expect(h.timers.intervals.size).toBe(0);
    h.fire("turn_end");
    expect(h.timers.intervals.size).toBe(0);
    h.fire("session_start");
    expect(h.timers.intervals.size).toBe(1);
    h.fire("session_switch");
    expect(h.timers.intervals.size).toBe(1); // the new session's own watcher, never two
    h.fire("session_shutdown");
    expect(h.timers.intervals.size).toBe(0);
  });

  test("SWARM_BAR=off, subagent, headless and RPC sessions create no interval and never look at live.json", () => {
    const cases: Array<{ env?: Record<string, string>; ctx?: Partial<ExtensionContext> }> = [
      { env: { SWARM_BAR: "off" } },
      { ctx: { agent: { kind: "sub", id: "0-Explore", name: "explore", depth: 1, parentId: "Main" } } },
      { ctx: { hasUI: false, mode: "print", ui: undefined } },
      { ctx: { mode: "rpc" } },
    ];
    for (const { env, ctx } of cases) {
      const dir = liveDir();
      writeFileSync(join(dir, "live.json"), doc([running])); // would mount at once if anything looked
      const home = tmpDir("swarm-tokens-");
      mkdirSync(join(home, ".omp", "agent", "token-opt"), { recursive: true });
      writeFileSync(join(home, ".omp", "agent", "token-opt", "live.json"), JSON.stringify(savings()));
      const h = harness({ swarmDir: dir, env: { ...env, HOME: home }, ctx });
      h.fire("session_start");
      h.fire("turn_start");
      expect(h.timers.intervals.size).toBe(0);
      writeFileSync(join(dir, "live.json"), doc([running, { ...running, run_id: "T2.a1" }]));
      h.timers.tickInterval();
      h.bus.emit(TOKEN_CHANNEL, savings());
      h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
      expect(h.host.calls).toEqual([]);
      expect(h.timers.timeouts.size).toBe(0);
      expect(h.host.tui.renders).toBe(0);
      // a headless session listens to its two stderr channels only: no token channel, no restack
      expect(h.bus.listeners.get(TOKEN_CHANNEL)?.size ?? 0).toBe(0);
      expect(h.bus.listeners.get(RESTACK_CHANNEL)?.size ?? 0).toBe(0);
    }
  });

  test("SWARM_BAR_IDLE_POLL_MS sets the period, clamped to 500..30000; junk falls back to 2000", () => {
    const period = (value: string) => {
      const h = harness({ swarmDir: liveDir(), env: { SWARM_BAR_IDLE_POLL_MS: value } });
      h.fire("session_start");
      return [...h.timers.intervalMs.values()];
    };
    expect(period("100")).toEqual([500]);
    expect(period("99999")).toEqual([30_000]);
    expect(period("5000")).toEqual([5000]);
    expect(period("fast")).toEqual([2000]);
    expect(period("0")).toEqual([2000]);
  });

  test("a vanished swarm dir never throws out of the watcher", () => {
    const h = harness({ swarmDir: join(tmpDir("swarm-gone-"), "missing", ".swarm") });
    h.fire("session_start");
    expect(() => h.timers.tickInterval()).not.toThrow();
  });
});

describe("in-session specialists (A01 spawning a02..a30)", () => {
  const lane = (id: string, agent: string, status: string, durationMs: number, extra: Record<string, unknown> = {}) => ({
    index: 0,
    id,
    agent,
    agentSource: "project",
    status,
    task: `assignment for ${id}`,
    assignment: `assignment for ${id}`,
    recentTools: [],
    recentOutput: [],
    toolCount: 1,
    requests: 1,
    tokens: 1,
    cost: 0,
    durationMs,
    ...extra,
  });
  /** A01's own frame as the top-level session receives it: its progress carries the in-flight `task` details. */
  const a01Frame = (lanes: unknown[], results: unknown[] = [], durationMs = 0) =>
    frame("Orchestrate", "a01-orchestrator", "running", durationMs, {
      currentTool: "task",
      inflightTaskDetails: { projectAgentsDir: null, results, totalDurationMs: durationMs, progress: lanes },
    });
  const doc = (runs: unknown[]) => JSON.stringify({ schema: "swarm.live.v1", seq: 1, swarm: { max_parallel: 4 }, runs });

  test("the bar shows every specialist with its own timer, live from A01's frames, and settles them", () => {
    const h = harness();
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, a01Frame([lane("be", "a05-backend", "running", 30_000, { currentTool: "edit" }), lane("fe", "a06-frontend", "running", 4000)], [], 31_000));
    const first = h.host.lines();
    expect(first[0]).toContain("3 run");
    expect(first[1]).toMatch(/a05-backend\s+30s edit/);
    expect(first[1]).toMatch(/a06-frontend\s+4s/);
    h.clock.mono += 6000;
    h.timers.tickInterval();
    expect(h.host.lines()[1]).toMatch(/a05-backend\s+36s/);
    expect(h.host.lines()[1]).toMatch(/a06-frontend\s+10s/);
    h.bus.emit(PROGRESS_CHANNEL, a01Frame([lane("be", "a05-backend", "completed", 36_000), lane("fe", "a06-frontend", "failed", 10_000)], [], 37_000));
    const settled = h.host.lines();
    expect(settled[0]).toMatch(/1 run .*1 done .*1 fail/);
    expect(settled[1]).toContain("✕ FAIL a06-frontend");
  });

  test("a specialist named by live.json and by A01's frame is one row, joined by task id", () => {
    const dir = join(tmpDir("swarm-nested-"), ".swarm");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "live.json"),
      doc([
        { run_id: "T7f3a-be.a1", task_id: "T7f3a-be", agent: "a05-backend", state: "running", elapsed_s: 20, activity: "store" },
        { run_id: "T7f3a-fe.a1", task_id: "T7f3a-fe", agent: "a05-backend", state: "waiting", elapsed_s: 5 },
      ]),
    );
    const h = harness({ swarmDir: dir });
    h.fire("session_start");
    h.bus.emit(
      PROGRESS_CHANNEL,
      a01Frame([
        lane("be", "a05-backend", "running", 21_000, { assignment: "do T7f3a-be: the module", currentTool: "edit", currentToolArgs: "swarm/handover.py" }),
        lane("fe", "a05-backend", "pending", 0, { assignment: "then T7f3a-fe: its tests", index: 1 }),
      ]),
    );
    const lines = h.host.lines();
    expect(lines[0]).toContain("2 run"); // A01 + the running lane
    expect(lines[0]).toContain("1 wait");
    expect(lines[0]).toContain("0 queued");
    expect(lines[1].match(/a05-backend/g)).toHaveLength(2); // one row each, not four
    expect(lines[1]).toContain("edit swarm/handover.py");
  });

  test("headless -p: one stderr line per nested spawn and per settle", () => {
    const h = harness({ ctx: { hasUI: false, mode: "print", ui: undefined } });
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, a01Frame([lane("be", "a05-backend", "running", 0), lane("fe", "a06-frontend", "running", 0, { index: 1 })]));
    h.clock.mono += 12_000;
    h.bus.emit(PROGRESS_CHANNEL, a01Frame([lane("be", "a05-backend", "completed", 12_000), lane("fe", "a06-frontend", "running", 12_000, { index: 1 })], [], 12_000));
    h.bus.emit(PROGRESS_CHANNEL, a01Frame([lane("be", "a05-backend", "completed", 12_000), lane("fe", "a06-frontend", "running", 12_000, { index: 1 })], [], 12_000));
    expect(h.stderr).toEqual([
      "[swarm] a01-orchestrator running 0s\n",
      "[swarm] a05-backend running 0s\n",
      "[swarm] a06-frontend running 0s\n",
      "[swarm] a05-backend completed 12s\n",
    ]);
    expect(h.host.calls).toEqual([]);
  });

  test("hostile nested payloads on the bus never throw out of the handler", () => {
    const h = harness();
    h.fire("session_start");
    for (const details of ["x", 7, null, [], { progress: "no" }, { progress: [null, 1, {}], results: [{}] }]) {
      expect(() => h.bus.emit(PROGRESS_CHANNEL, frame("Orchestrate", "a01-orchestrator", "running", 1000, { currentTool: "task", inflightTaskDetails: details }))).not.toThrow();
    }
    expect(h.host.lines()[1]).toContain("a01-orchestrator");
  });
});

describe("RV-F late fixes: the derived live.json, background specialists, the evidence timeout", () => {
  const derivedText = (): string => readFileSync(join(REPO_ROOT, "omp", "test", "fixtures", "live_derived_sample.json"), "utf8");
  /** A `.swarm` dir holding `text` as live.json, its mtime `ageMs` in the past. */
  function swarmWith(text: string, ageMs = 0): string {
    const dir = join(tmpDir("swarm-derived-"), ".swarm");
    mkdirSync(dir, { recursive: true });
    const path = join(dir, "live.json");
    writeFileSync(path, text);
    if (ageMs > 0) {
      const old = new Date(FIXED_WALL - ageMs);
      utimesSync(path, old, old);
    }
    return dir;
  }
  const schedulerText = (): string => {
    const doc = JSON.parse(derivedText()) as Record<string, unknown>;
    delete doc.producer;
    return JSON.stringify(doc);
  };
  const entry = (id: string, agent: string, status: string, extra: Record<string, unknown> = {}) => ({
    index: 0,
    id,
    agent,
    agentSource: "project",
    status,
    task: `assignment for ${id}`,
    assignment: `assignment for ${id}`,
    recentTools: [],
    recentOutput: [],
    toolCount: 1,
    requests: 1,
    tokens: 1,
    cost: 0,
    durationMs: 5000,
    ...extra,
  });
  /** omp's `buildAsyncDetails`: results empty, every spawn in progress, `async` on. */
  const asyncDetails = (lanes: unknown[]) => ({ projectAgentsDir: null, results: [], totalDurationMs: 12, progress: lanes, async: { state: "running", jobId: "ApiImpl", type: "task" } });
  const spawnFrame = (lanes: unknown[]) => frame("Orchestrate", "a01-orchestrator", "running", 0, { currentTool: "task", inflightTaskDetails: asyncDetails(lanes) });
  const clearedFrame = () => frame("Orchestrate", "a01-orchestrator", "running", 0, { currentTool: "bash", currentToolArgs: "python3 scripts/orch_status.py" });
  const twoLanes = () => [
    entry("ApiImpl", "a05-backend", "running", { assignment: "implement T38a7-be: the api", currentTool: "edit", currentToolArgs: "swarm/x.py" }),
    entry("UiImpl", "a06-frontend", "running", { index: 1, assignment: "implement T38a7-fe: the ui" }),
  ];

  test("a derived live.json is never stale for want of a transition; the scheduler's document still is (30 s)", () => {
    const derived = harness({ swarmDir: swarmWith(derivedText()) });
    derived.fire("session_start");
    expect(derived.host.lines()[0]).toContain("1 run");
    expect(derived.host.lines()[0]).toContain("10 wait");
    derived.clock.mono += 31_000;
    derived.timers.tickInterval();
    expect(derived.host.lines()[0]).toContain("1 run");
    expect(derived.host.lines().join("\n")).not.toContain("telemetry unavailable");
    derived.clock.mono += 3_600_000;
    derived.timers.tickInterval();
    expect(derived.host.lines()[0]).toContain("10 wait");
    expect(derived.host.lines().join("\n")).not.toContain("telemetry unavailable");

    const scheduler = harness({ swarmDir: swarmWith(schedulerText()) });
    scheduler.fire("session_start");
    expect(scheduler.host.lines()[0]).toContain("1 run");
    scheduler.clock.mono += 31_000;
    scheduler.timers.tickInterval();
    expect(scheduler.host.lines()).toEqual(TELEMETRY);
  });

  test("a derived document older than 30 s at first read still supplies live rows; the scheduler's is stale", () => {
    const derived = harness({ swarmDir: swarmWith(derivedText(), 100_000) });
    derived.fire("session_start");
    expect(derived.host.lines()[0]).toContain("1 run");
    expect(derived.host.lines()[0]).toContain("10 wait");
    const scheduler = harness({ swarmDir: swarmWith(schedulerText(), 100_000) });
    scheduler.fire("session_start");
    expect(scheduler.host.lines()).toEqual(TELEMETRY);
  });

  test("an old derived live.json joins A01's specialists by task id and protects them after A01's async call returned", () => {
    const h = harness({ swarmDir: swarmWith(derivedText(), 100_000) });
    h.fire("session_start");
    h.bus.emit(
      PROGRESS_CHANNEL,
      spawnFrame([
        entry("Arch", "a03-architect", "running", { assignment: "design it (T38a7-arch)", currentTool: "write", currentToolArgs: "docs/arch.md" }),
        entry("Ux", "a04-ux-designer", "running", { index: 1, assignment: "tokens (T38a7-ux)" }),
      ]),
    );
    const first = h.host.lines(240);
    expect(first[0]).toContain("2 run"); // A01 and the running A03; A04 is queued in the store
    expect(first[0]).toContain("1 queued");
    expect(first[1].match(/a03-architect/g)).toHaveLength(1);
    h.bus.emit(PROGRESS_CHANNEL, clearedFrame());
    h.clock.mono += 31_000;
    h.timers.tickInterval();
    h.bus.emit(PROGRESS_CHANNEL, clearedFrame());
    const later = h.host.lines(240);
    expect(later[0]).toContain("2 run");
    expect(later[0]).not.toContain("cancel");
    expect(later.join("\n")).not.toContain("telemetry unavailable");
    expect(later[0]).toContain("10 wait");
    expect(later[0]).toContain("1 queued");
  });

  test("headless -p: a detached specialist never prints `cancelled` while A01 runs or after it completes", () => {
    const h = harness({ ctx: { hasUI: false, mode: "print", ui: undefined } });
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, spawnFrame(twoLanes()));
    h.clock.mono += 1000;
    h.bus.emit(PROGRESS_CHANNEL, clearedFrame()); // the call returned; the specialists run on
    h.clock.mono += 120_000;
    h.bus.emit(PROGRESS_CHANNEL, clearedFrame());
    expect(h.stderr.join("")).not.toContain("cancelled");
    expect(h.stderr).toEqual(["[swarm] a01-orchestrator running 0s\n", "[swarm] a05-backend running 5s\n", "[swarm] a06-frontend running 5s\n"]);
    h.bus.emit(PROGRESS_CHANNEL, frame("Orchestrate", "a01-orchestrator", "completed", 125_000));
    expect(h.stderr.join("")).not.toContain("cancelled");
    expect(h.stderr.at(-1)).toBe("[swarm] a01-orchestrator completed 2m05s\n");

    // A01 failing is an end for its specialists: that, and only that, prints `cancelled`
    const failed = harness({ ctx: { hasUI: false, mode: "print", ui: undefined } });
    failed.fire("session_start");
    failed.bus.emit(PROGRESS_CHANNEL, spawnFrame(twoLanes()));
    failed.bus.emit(PROGRESS_CHANNEL, clearedFrame());
    expect(failed.stderr.join("")).not.toContain("cancelled");
    failed.bus.emit(PROGRESS_CHANNEL, frame("Orchestrate", "a01-orchestrator", "failed", 9000));
    expect(failed.stderr.filter((line) => line.includes("cancelled")).sort()).toEqual(["[swarm] a05-backend cancelled 5s\n", "[swarm] a06-frontend cancelled 5s\n"]);
  });

  test("SWARM_BAR_DETACHED_S: default 900 s, clamped to 60..86400, junk falls back to the default", () => {
    // [env, last tick still showing the row (s after the cleared frame), first tick that has dropped it]; the last evidence is 1 s older
    const cases: Array<[string | undefined, number, number]> = [
      [undefined, 898, 900],
      ["abc", 898, 900],
      ["0", 898, 900],
      ["5", 58, 60],
      ["120", 118, 120],
      ["999999", 86_398, 86_400],
    ];
    for (const [value, keep, drop] of cases) {
      const h = harness({ env: value === undefined ? {} : { SWARM_BAR_DETACHED_S: value } });
      h.fire("session_start");
      h.bus.emit(PROGRESS_CHANNEL, spawnFrame(twoLanes()));
      h.clock.mono += 1000;
      h.bus.emit(PROGRESS_CHANNEL, clearedFrame());
      expect(h.host.lines(240)[1]).toMatch(/a05-backend\s+\S+~/);
      h.clock.mono += keep * 1000;
      h.timers.tickInterval();
      expect({ value, shown: h.host.lines(240)[1].includes("a05-backend") }).toEqual({ value, shown: true });
      h.clock.mono += (drop - keep) * 1000;
      h.timers.tickInterval();
      expect({ value, shown: h.host.lines(240)[1].includes("a05-backend") }).toEqual({ value, shown: false });
      expect(h.host.lines(240)[1]).toContain("a01-orchestrator");
    }
  });
});

describe("token savings on row 1", () => {
  const row1 = (h: Harness, width = 120): string => h.host.lines(width)[0] ?? "";
  const fullSnapshot = (): Record<string, unknown> =>
    savings({ lastTurnMs: 840, cacheHitPct: 72, lastQueryMs: 15, rtkSavedTokens: 2500, lastPayloadChars: 4096, ttftMs: 120, lastIdleMs: 40, costCents: 3, providerInput: 1500, providerOutput: 300 });

  test("a snapshot after the render gap paints at once; saved is the right-most chip of row 1", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    expect(h.host.lines()).toEqual(IDLE); // no snapshot yet: no chips, no invented zeros
    h.clock.mono += 300;
    const renders = h.host.tui.renders;
    h.bus.emit(TOKEN_CHANNEL, savings());
    expect(h.host.tui.renders).toBe(renders + 1);
    expect(delayed(h)).toHaveLength(0);
    const [first] = h.host.rows(120);
    expect(first).toContain("○ swarm idle");
    expect(first).toContain("before 5k · now 3.8k");
    expect(first.trimEnd().endsWith("saved 1.2k")).toBe(true);
    expect(displayWidth(first)).toBe(120);
  });

  test("later snapshots replace the numbers, whichever way they went", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings());
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ currentTokens: 2600, savedTokens: 2400 }));
    expect(row1(h)).toContain("saved 2.4k");
    expect(row1(h)).not.toContain("saved 1.2k");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ baselineTokens: 3800, savedTokens: 0 }));
    expect(row1(h)).toContain("saved 0");
    expect(row1(h)).not.toContain("saved 2.4k");
  });

  test("optional measurements show only when the snapshot carries them", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings());
    const absent = [/turn \d/, /cache \d/, /query \d/, /rtk \+/, /payload \d/, /ttft \d/, /idle \d/, /cost \d/, /bill \d/, /out \d/];
    for (const pattern of absent) expect({ pattern: String(pattern), shown: pattern.test(row1(h, 300)) }).toEqual({ pattern: String(pattern), shown: false });
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, fullSnapshot());
    for (const shown of ["turn 840ms", "cache 72%", "query 15ms", "rtk +2.5k", "payload 4096c", "ttft 120ms", "idle 40ms", "cost 3c", "bill 1.5k · out 300"]) {
      expect({ shown, there: row1(h, 300).includes(shown) }).toEqual({ shown, there: true });
    }
  });

  test("the chips sit right of the summary and give way by priority: a narrow row keeps the swarm mark and saved, then drops the rest", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "running", 0));
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, fullSnapshot());
    const wide = row1(h, 300);
    expect(wide.indexOf("1 run")).toBeGreaterThan(-1);
    expect(wide.indexOf("saved 1.2k")).toBeGreaterThan(wide.indexOf("1 run"));
    expect(wide.indexOf("turn 840ms")).toBeLessThan(wide.indexOf("cache 72%"));
    expect(wide.indexOf("cache 72%")).toBeLessThan(wide.indexOf("saved 1.2k"));
    const narrow = row1(h, 24);
    expect(narrow).toContain("swarm");
    expect(narrow).toContain("saved 1.2k");
    expect(narrow).not.toContain("before");
    expect(narrow).not.toContain("1 run");
    expect(displayWidth(h.host.rows(24)[0])).toBe(24);
  });

  test("a snapshot of another session is ignored once this session's id is known; its own snapshot shows", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "other" }));
    expect(row1(h)).not.toContain("saved");
    expect(row1(h)).not.toContain("before");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "mine", currentTokens: 3700, savedTokens: 1300 }));
    expect(row1(h)).toContain("saved 1.3k");
  });

  test("another session's snapshot never replaces this session's last one: the chips keep this session's numbers", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "mine", currentTokens: 3700, savedTokens: 1300 }));
    const before = row1(h);
    expect(before).toContain("saved 1.3k");
    const renders = h.host.tui.renders;
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "other", currentTokens: 100, savedTokens: 4900 }));
    expect(row1(h)).toBe(before);
    expect(row1(h)).not.toContain("saved 4.9k");
    expect(h.host.tui.renders).toBe(renders); // nothing changed: no repaint, no queued timer
    expect(delayed(h)).toHaveLength(0);
    h.clock.mono += 4000;
    h.fire("turn_start");
    expect(row1(h)).toContain("saved 1.3k");
  });

  test("a session switch to another id drops the snapshot of the old session and repaints", () => {
    let id = "mine";
    const h = harness({ sessionId: () => id });
    h.fire("session_start");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "mine" }));
    expect(row1(h)).toContain("saved 1.2k");
    h.clock.mono += 300;
    id = "next";
    h.fire("turn_start");
    h.clock.mono += 300;
    h.timers.wakeUp();
    expect(row1(h)).not.toContain("saved");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "mine", currentTokens: 100, savedTokens: 4900 }));
    expect(row1(h)).not.toContain("saved");
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "next" }));
    expect(row1(h)).toContain("saved 1.2k");
  });

  test("the session id is read on every event: any snapshot shows until a valid id is known, then only this session's", () => {
    let id: unknown;
    const h = harness({ sessionId: () => id });
    h.fire("session_start");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "other" }));
    for (const bad of [undefined, "", "a/b", "x".repeat(129), 7, null]) {
      id = bad;
      h.fire("turn_start");
      expect({ bad: String(bad), shown: row1(h).includes("saved 1.2k") }).toEqual({ bad: String(bad), shown: true });
    }
    id = "mine";
    h.fire("turn_start");
    expect(row1(h)).not.toContain("saved");
    id = "other"; // a resumed session changes its id: the snapshot of the session that is gone does not follow it
    h.fire("turn_start");
    expect(row1(h)).not.toContain("saved");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "other" }));
    expect(row1(h)).toContain("saved 1.2k");
  });

  test("a session manager without getSessionId, or one that throws, shows every valid snapshot and never throws", () => {
    const throwing = harness({
      sessionId: () => {
        throw new Error("no id yet");
      },
    });
    for (const h of [harness(), throwing]) {
      expect(() => h.fire("session_start")).not.toThrow();
      h.clock.mono += 300;
      h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "anyone" }));
      expect(row1(h)).toContain("saved 1.2k");
      expect(() => h.fire("turn_start")).not.toThrow();
      expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
    }
  });

  test("an invalid snapshot is ignored: no repaint, no queued timer, and the last good numbers stay", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings());
    const renders = h.host.tui.renders;
    h.clock.mono += 300;
    const invalid: unknown[] = [
      null,
      undefined,
      7,
      "x",
      [],
      {},
      savings({ v: 2 }),
      savings({ savedTokens: 1 }),
      savings({ sessionId: "a/b" }),
      savings({ cacheHitPct: 101 }),
      savings({ points: [{ at: 1, source: "graph", baseline: 5, current: 1, saved: 3 }] }),
    ];
    for (const payload of invalid) expect(() => h.bus.emit(TOKEN_CHANNEL, payload)).not.toThrow();
    expect(h.host.tui.renders).toBe(renders);
    expect(delayed(h)).toHaveLength(0);
    expect(row1(h)).toContain("saved 1.2k");
  });

  test("a snapshot inside the render gap is painted by one trailing timer, not lost", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start"); // the render of this event starts the gap
    h.clock.mono += 100;
    const renders = h.host.tui.renders;
    h.bus.emit(TOKEN_CHANNEL, savings());
    expect(h.host.tui.renders).toBe(renders); // inside the gap: not painted yet
    const timers = delayed(h);
    expect(timers).toHaveLength(1);
    expect(timers[0].ms).toBeGreaterThan(0);
    expect(timers[0].ms).toBeLessThanOrEqual(151); // the rest of the gap, never a whole new one
    h.clock.mono += 151;
    h.timers.wakeUp();
    expect(h.host.tui.renders).toBe(renders + 1);
    expect(delayed(h)).toHaveLength(0);
    expect(row1(h)).toContain("saved 1.2k");
  });

  test("several snapshots inside one gap share the timer and the last one is what gets painted", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.clock.mono += 50;
    const renders = h.host.tui.renders;
    h.bus.emit(TOKEN_CHANNEL, savings());
    h.bus.emit(TOKEN_CHANNEL, savings({ currentTokens: 3000, savedTokens: 2000 }));
    h.bus.emit(TOKEN_CHANNEL, savings({ currentTokens: 2600, savedTokens: 2400 }));
    expect(delayed(h)).toHaveLength(1);
    h.clock.mono += 250;
    h.timers.wakeUp();
    expect(h.host.tui.renders).toBe(renders + 1);
    expect(row1(h)).toContain("saved 2.4k");
  });

  test("the gap is 250 ms: a snapshot at 249 ms waits for the timer, one at 250 ms paints at once", () => {
    for (const [elapsed, immediate] of [[0, false], [249, false], [250, true], [900, true]] as const) {
      const h = harness({ sessionId: () => "mine" });
      h.fire("session_start");
      h.clock.mono += elapsed;
      const renders = h.host.tui.renders;
      h.bus.emit(TOKEN_CHANNEL, savings());
      expect({ elapsed, painted: h.host.tui.renders - renders, queued: delayed(h).length }).toEqual({ elapsed, painted: immediate ? 1 : 0, queued: immediate ? 0 : 1 });
    }
  });

  test("after the trailing render fired, the next snapshot inside the new gap gets a timer of its own", () => {
    const h = harness({ sessionId: () => "mine" });
    h.fire("session_start");
    h.bus.emit(TOKEN_CHANNEL, savings());
    h.clock.mono += 251;
    h.timers.wakeUp();
    const renders = h.host.tui.renders;
    h.bus.emit(TOKEN_CHANNEL, savings({ currentTokens: 2600, savedTokens: 2400 })); // right after a render: inside its gap
    expect(h.host.tui.renders).toBe(renders);
    expect(delayed(h)).toHaveLength(1);
    h.clock.mono += 251;
    h.timers.wakeUp();
    expect(h.host.tui.renders).toBe(renders + 1);
    expect(row1(h)).toContain("saved 2.4k");
  });
});

describe("token savings restored from live.json", () => {
  const body = JSON.stringify(savings());
  const padded = (bytes: number): string => body + " ".repeat(bytes - Buffer.byteLength(body));
  /** A HOME whose omp-token-opt directory holds `content` as live.json (nothing at all when undefined). */
  function homeWith(content: string | undefined): string {
    const home = tmpDir("swarm-home-");
    if (content === undefined) return home;
    const dir = join(home, ".omp", "agent", "token-opt");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "live.json"), content);
    return home;
  }
  const row1 = (h: Harness): string => h.host.lines()[0] ?? "";

  test("the last snapshot is restored once, at the first event, before any bus message", () => {
    const h = harness({ env: { HOME: homeWith(body) }, sessionId: () => "mine" });
    h.fire("session_start");
    expect(row1(h)).toContain("saved 1.2k");
    expect(row1(h)).toContain("before 5k · now 3.8k");
    expect(h.host.order()).toEqual([SWARM_BAR_KEY]);
  });

  test("it is read once: later events and a rewritten file change nothing, a bus snapshot replaces it", () => {
    const home = homeWith(body);
    const h = harness({ env: { HOME: home }, sessionId: () => "mine" });
    h.fire("session_start");
    writeFileSync(join(home, ".omp", "agent", "token-opt", "live.json"), JSON.stringify(savings({ currentTokens: 2600, savedTokens: 2400 })));
    for (const event of ["session_start", "turn_start", "agent_start", "turn_end", "agent_end"]) h.fire(event);
    h.timers.tickInterval();
    h.timers.turn();
    expect(row1(h)).toContain("saved 1.2k");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ currentTokens: 2600, savedTokens: 2400 }));
    expect(row1(h)).toContain("saved 2.4k");
  });

  test("a restored snapshot of another session stays hidden until this session's own snapshot arrives", () => {
    const h = harness({ env: { HOME: homeWith(JSON.stringify(savings({ sessionId: "other" }))) }, sessionId: () => "mine" });
    h.fire("session_start");
    expect(row1(h)).not.toContain("saved");
    h.clock.mono += 300;
    h.bus.emit(TOKEN_CHANNEL, savings({ sessionId: "mine" }));
    expect(row1(h)).toContain("saved 1.2k");
  });

  test("the session id is read before the restore: another session's live.json is not adopted, and does not surface when the id later changes", () => {
    let id = "mine";
    const h = harness({ env: { HOME: homeWith(JSON.stringify(savings({ sessionId: "other" }))) }, sessionId: () => id });
    h.fire("session_start");
    expect(row1(h)).not.toContain("saved");
    id = "other";
    h.fire("turn_start");
    expect(row1(h)).not.toContain("saved");
  });

  test("a live.json restored while the id is unknown is dropped once a different id becomes known, and kept when it matches", () => {
    for (const [known, shown] of [["other", true], ["mine", false]] as const) {
      let id: unknown;
      const h = harness({ env: { HOME: homeWith(JSON.stringify(savings({ sessionId: "other" }))) }, sessionId: () => id });
      h.fire("session_start");
      expect(row1(h)).toContain("saved 1.2k"); // no id yet: any valid snapshot shows
      id = known;
      h.fire("turn_start");
      expect({ known, shown: row1(h).includes("saved 1.2k") }).toEqual({ known, shown });
    }
  });

  test("a missing file, a missing directory and a directory in its place restore nothing and break nothing", () => {
    const asDirectory = tmpDir("swarm-home-");
    mkdirSync(join(asDirectory, ".omp", "agent", "token-opt", "live.json"), { recursive: true });
    for (const [name, home] of [["no home dir content", homeWith(undefined)], ["live.json is a directory", asDirectory]] as const) {
      const h = harness({ env: { HOME: home } });
      expect(() => h.fire("session_start")).not.toThrow();
      expect({ name, lines: h.host.lines(), order: h.host.order() }).toEqual({ name, lines: IDLE, order: [SWARM_BAR_KEY] });
    }
  });

  test("malformed, wrong-shaped and invalid documents are ignored", () => {
    const documents = [
      "",
      "{ not json",
      "[]",
      "null",
      "42",
      '"saved"',
      JSON.stringify(savings({ v: 2 })),
      JSON.stringify(savings({ savedTokens: 1 })),
      JSON.stringify(savings({ points: "x" })),
      JSON.stringify(savings({ sessionId: "a/b" })),
      JSON.stringify(savings({ lastTurnMs: -1 })),
      body.slice(0, body.length - 3),
    ];
    for (const [index, document] of documents.entries()) {
      const h = harness({ env: { HOME: homeWith(document) } });
      expect(() => h.fire("session_start")).not.toThrow();
      expect({ index, lines: h.host.lines() }).toEqual({ index, lines: IDLE });
    }
  });

  test("the file may be exactly 1 MiB; one byte more is ignored", () => {
    const limit = 1_048_576;
    for (const [bytes, restored] of [[limit, true], [limit + 1, false], [limit * 2, false]] as const) {
      const h = harness({ env: { HOME: homeWith(padded(bytes)) } });
      h.fire("session_start");
      expect({ bytes, restored: row1(h).includes("saved 1.2k") }).toEqual({ bytes, restored });
    }
  });

  test("the HOME of the session decides which file is read", () => {
    const mine = homeWith(body);
    const elsewhere = homeWith(undefined);
    const withIt = harness({ env: { HOME: mine } });
    const without = harness({ env: { HOME: elsewhere } });
    withIt.fire("session_start");
    without.fire("session_start");
    expect(row1(withIt)).toContain("saved 1.2k");
    expect(row1(without)).not.toContain("saved");
  });
});

describe("stack order with a rank-20 peer bar (restack protocol)", () => {
  /** A stand-in for Ultrathink: sets its widget THEN announces rank 20, and re-mounts (re-announcing) when a lower rank announces. */
  function peer(h: Harness) {
    const counts = { mounts: 0 };
    let mounted = false;
    const restack = createRestack(h.bus, {
      rank: BAR_RANK.ultrathink,
      remount: () => {
        if (mounted) mountAndAnnounce();
      },
    });
    function mountAndAnnounce(): void {
      h.host.ui.setWidget("ultrathink", ["u"], { placement: "aboveEditor" });
      counts.mounts++;
      restack.announce();
    }
    return {
      counts,
      mount() {
        mounted = true;
        mountAndAnnounce();
      },
      remount: mountAndAnnounce,
    };
  }
  /** The relative order of the two bars, whatever else is on the stack. */
  const bars = (h: Harness): string[] => h.host.order().filter((key) => key === SWARM_BAR_KEY || key === "ultrathink");

  test("swarm loads first, the peer second: the end order is [agent-swarm, peer] and the swarm never re-mounts for it", () => {
    const h = harness();
    const p = peer(h);
    h.fire("session_start");
    p.mount();
    expect(bars(h)).toEqual([SWARM_BAR_KEY, "ultrathink"]);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY, "ultrathink"]);
    expect(h.host.widgetCalls().filter((call) => call.key === SWARM_BAR_KEY)).toHaveLength(1);
    expect(p.counts.mounts).toBe(1);
  });

  test("the peer loads first, the swarm second: the swarm's mount pushes the peer back below it", () => {
    const h = harness();
    const p = peer(h);
    p.mount();
    h.fire("session_start");
    expect(h.host.order()).toEqual([SWARM_BAR_KEY, "ultrathink"]);
    expect(p.counts.mounts).toBe(2); // its own mount and the answer to the swarm's announcement, no more
    expect(h.host.widgetCalls().filter((call) => call.key === SWARM_BAR_KEY)).toHaveLength(1);
  });

  test("in both load orders, with a third-party widget re-set in between and after, the end order is [agent-swarm, peer]", () => {
    for (const first of ["swarm", "peer"]) {
      const h = harness();
      const p = peer(h);
      if (first === "swarm") {
        h.fire("session_start");
        h.host.ui.setWidget("aio-chrome", ["c"]);
        p.mount();
      } else {
        p.mount();
        h.host.ui.setWidget("aio-chrome", ["c"]);
        h.fire("session_start");
      }
      expect({ first, order: bars(h) }).toEqual({ first, order: [SWARM_BAR_KEY, "ultrathink"] });
      // a third party re-sets after both bars; the swarm's deferred remount then restores the order
      h.host.ui.setWidget("aio-chrome", ["c"]);
      h.timers.turn();
      expect({ first, order: h.host.order().slice(-2) }).toEqual({ first, order: [SWARM_BAR_KEY, "ultrathink"] });
      // and every later remount keeps it
      for (const event of ["turn_start", "tool_execution_end", "agent_end"]) {
        h.host.ui.setWidget("aio-chrome", ["c"]);
        h.fire(event);
        h.timers.turn();
        expect({ first, event, order: h.host.order().slice(-2) }).toEqual({ first, event, order: [SWARM_BAR_KEY, "ultrathink"] });
      }
    }
  });

  test("each deferred remount of the swarm makes the peer re-mount exactly once", () => {
    const h = harness();
    const p = peer(h);
    p.mount();
    h.fire("session_start");
    h.timers.turn();
    const before = p.counts.mounts;
    h.fire("turn_end");
    h.timers.turn();
    expect(p.counts.mounts).toBe(before + 1);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY, "ultrathink"]);
  });

  test("the peer re-mounting on its own events never moves the swarm", () => {
    const h = harness();
    const p = peer(h);
    h.fire("session_start");
    p.mount();
    const mounts = h.host.widgetCalls().filter((call) => call.key === SWARM_BAR_KEY).length;
    for (let i = 0; i < 5; i++) p.remount();
    expect(h.host.widgetCalls().filter((call) => call.key === SWARM_BAR_KEY)).toHaveLength(mounts);
    expect(h.host.order()).toEqual([SWARM_BAR_KEY, "ultrathink"]);
  });

  test("a lower-rank announcement re-mounts the bar at once (no timer); equal, higher and malformed ones do nothing", () => {
    const h = harness();
    h.fire("session_start");
    const mounts = (): number => h.host.widgetCalls().length;
    const before = mounts();
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    expect(mounts()).toBe(before + 1);
    expect(pendingRemounts(h)).toBe(1); // only the deferral the first event queued
    for (const payload of [{ v: 1, after: BAR_RANK.swarm }, { v: 1, after: BAR_RANK.ultrathink }, { v: 1, after: 99 }, { v: 2, after: 1 }, { v: 1, after: Number.NaN }, { v: 1, after: "1" }, { after: 1 }, null, undefined, 3, "x"]) {
      h.bus.emit(RESTACK_CHANNEL, payload);
    }
    expect(mounts()).toBe(before + 1);
  });

  test("a re-mount caused by an announcement announces again, so peers ranked above it follow", () => {
    const h = harness();
    const heard: unknown[] = [];
    h.fire("session_start");
    h.bus.on(RESTACK_CHANNEL, (payload) => void heard.push(payload));
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    // the swarm's own answer is announced from inside the handler, so it reaches a later listener before the triggering message
    expect(heard).toEqual([{ v: 1, after: BAR_RANK.swarm }, { v: 1, after: 1 }]);
  });

  test("a lower-rank announcement after stop does nothing: not before the first event, not after shutdown, not for a closed session", () => {
    const h = harness();
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 }); // before the first event: nobody listens
    expect(h.host.calls).toEqual([]);
    h.fire("session_start");
    h.fire("session_shutdown");
    const calls = h.host.calls.length;
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    h.fire("turn_end"); // omp's trailing events after shutdown
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    h.timers.turn();
    expect(h.host.calls).toHaveLength(calls);
    expect(h.host.order()).toEqual([]);
  });

  test("a lower-rank announcement does nothing for sessions without a bar", () => {
    for (const options of [{ env: { SWARM_BAR: "off" } }, { ctx: { hasUI: false, mode: "print" as const, ui: undefined } }, { ctx: { agent: { kind: "sub" as const, id: "0-Explore", name: "explore", depth: 1, parentId: "Main" } } }]) {
      const h = harness(options);
      h.fire("session_start");
      h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
      expect(h.host.calls).toEqual([]);
    }
  });

  test("a host that fails the re-mount never throws out of the announcement, and the bar keeps working afterwards", () => {
    const h = harness();
    h.fire("session_start");
    h.host.failWidgets(true);
    expect(() => h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 })).not.toThrow();
    h.host.failWidgets(false);
    const before = h.host.widgetCalls().length;
    h.bus.emit(RESTACK_CHANNEL, { v: 1, after: 1 });
    expect(h.host.widgetCalls()).toHaveLength(before + 1);
  });
});

describe("footer status stays absent", () => {
  test("the shared host records every footer write, so an empty footer assertion can fail", () => {
    const h = harness();
    h.host.ui.setStatus("swarm", "swarm: control write");
    expect(h.host.statuses).toEqual([{ key: "swarm", text: "swarm: control write" }]);
  });

  test("no status or footer text is written in the loading, idle, running, completed or error states, nor as the timers drive them", () => {
    const errorDir = join(tmpDir("swarm-footer-"), ".swarm");
    mkdirSync(errorDir, { recursive: true });
    writeFileSync(join(errorDir, "live.json"), "{ not json");
    const states: Record<string, { swarmDir?: string; setup: (h: Harness) => void; rows: (lines: string[]) => boolean }> = {
      loading: {
        setup: (h) => {
          h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ id: "a", agent: "a05-backend" }] } });
        },
        rows: (lines) => lines.join("\n").includes("start"),
      },
      idle: { setup: () => {}, rows: (lines) => lines.join("\n") === IDLE.join("\n") },
      running: {
        setup: (h) => {
          h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "running", 1000));
          h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
        },
        rows: (lines) => lines.join("\n").includes("a05-backend"),
      },
      completed: {
        setup: (h) => {
          h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "started", index: 0 });
          h.bus.emit(LIFECYCLE_CHANNEL, { id: "a", agent: "a05-backend", status: "completed", index: 0 });
        },
        rows: (lines) => lines.length > 0,
      },
      error: { swarmDir: errorDir, setup: () => {}, rows: (lines) => lines.join("\n") === TELEMETRY.join("\n") },
    };
    for (const [name, state] of Object.entries(states)) {
      const h = harness({ swarmDir: state.swarmDir });
      h.fire("session_start");
      state.setup(h);
      expect({ state: name, mounted: h.host.order(), rows: state.rows(h.host.lines()) }).toEqual({ state: name, mounted: [SWARM_BAR_KEY], rows: true });
      h.timers.turn();
      h.timers.tickInterval();
      expect({ state: name, statuses: h.host.statuses }).toEqual({ state: name, statuses: [] });
    }
  });

  test("a state change writes no status text: one session walks loading, running, completed and the linger clear, checking the footer at each step", () => {
    const h = harness();
    const started = { id: "a", agent: "a05-backend", status: "started", index: 0 };
    const steps: Array<{ step: string; act: () => void; marker: string }> = [
      {
        step: "loading",
        act: () => h.fire("tool_call", { toolName: "task", toolCallId: "call-1", input: { tasks: [{ id: "a", agent: "a05-backend" }] } }),
        marker: "start",
      },
      {
        step: "running",
        act: () => {
          h.bus.emit(LIFECYCLE_CHANNEL, started);
          h.bus.emit(PROGRESS_CHANNEL, frame("a", "a05-backend", "running", 1000));
        },
        marker: "1 run",
      },
      { step: "completed", act: () => h.bus.emit(LIFECYCLE_CHANNEL, { ...started, status: "completed" }), marker: "1 done" },
      {
        step: "linger cleared",
        act: () => {
          h.clock.mono += 21_000;
          h.timers.wakeUp();
        },
        marker: "",
      },
    ];
    h.fire("session_start");
    for (const { step, act, marker } of steps) {
      act();
      const shown = marker === "" || h.host.lines().join("\n").includes(marker);
      expect({ step, shown, mounted: h.host.order() }).toEqual({ step, shown: true, mounted: [SWARM_BAR_KEY] });
      expect({ step, statuses: h.host.statuses }).toEqual({ step, statuses: [] });
    }
  });
});
