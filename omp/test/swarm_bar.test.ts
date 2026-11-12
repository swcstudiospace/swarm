import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  agentKey,
  type BarRun,
  type BarFrame,
  barState,
  createModel,
  dropLive,
  formatElapsed,
  ingestLifecycle,
  ingestLive,
  ingestProgress,
  ingestTaskResult,
  ingestTaskSpawn,
  isDetached,
  nextExpiryMs,
  pruneExpired,
  redactSpans,
  redactText,
  renderSwarmBar,
  renderSwarmBarFrame,
  sanitizeText,
  runElapsedS,
  settleOrphanedSpawns,
  shownTokens,
  type SwarmBarModel,
  visibleRuns,
} from "../src/swarm_bar.ts";
import { displayWidth, stripAnsi, type BarTheme } from "../src/swarm_dock.ts";
import type { TokenSavingsSnapshot } from "../src/token_savings.ts";
import { REPO_ROOT } from "./helpers.ts";

const T0 = 1_000_000;
const SEC = 1000;

/**
 * Secret placeholders: no secret-shaped literal sits in the repo (GitHub push protection rejects them). A fixture stores `{{tok:KIND:INDEX}}`
 * or `{{tok:KIND:INDEX:LENGTH}}`; this file and tests/test_redact.py expand it at runtime with ONE algorithm, identical in both languages
 * (`_token` there, `token` here; pinned by the `tokens` golden digests of tests/fixtures/redact_generated.json):
 *   seed  = FNV-1a 32-bit over the UTF-8 bytes of "KIND:INDEX:LENGTH" (LENGTH is 0 when omitted); state = seed, or 1 when that is 0
 *   next(bound) = one xorshift32 step (x ^= x << 13; x ^= x >> 17; x ^= x << 5, 32 bits), then state % bound
 *   every random character is ALPHABET[next(len(ALPHABET))], drawn left to right; LENGTH is the whole token (default: the provider's own)
 */
const TOKEN_RE = /\{\{tok:([a-z0-9_]+):(\d+)(?::(\d+))?\}\}/g;
const UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
const LOWER = "abcdefghijklmnopqrstuvwxyz";
const DIGITS = "0123456789";
const ALNUM = LOWER + UPPER + DIGITS;
const PLAIN_CHARS = `${ALNUM}_.-+/=~`;
const URLSAFE_CHARS = `${ALNUM}_-`;
/** kind -> [prefix, alphabet after it, default total length] */
const TOKEN_PREFIXED: Record<string, [string, string, number]> = {
  ghp: ["ghp_", ALNUM, 40],
  gho: ["gho_", ALNUM, 40],
  ghs: ["ghs_", ALNUM, 40],
  ghu: ["ghu_", ALNUM, 40],
  github_pat: ["github_pat_", `${ALNUM}_`, 93],
  xoxb: ["xoxb-", `${ALNUM}-`, 55],
  xoxp: ["xoxp-", `${ALNUM}-`, 55],
  sk: ["sk-", URLSAFE_CHARS, 51],
  sk_live: ["sk_live_", ALNUM, 32],
  aiza: ["AIza", URLSAFE_CHARS, 39],
  glpat: ["glpat-", URLSAFE_CHARS, 26],
  npm: ["npm_", ALNUM, 40],
  aws_key: ["AKIA", `${UPPER}234567`, 20],
};

function fnv1a32(text: string): number {
  let h = 0x811c9dc5;
  for (const byte of Buffer.from(text, "utf8")) h = Math.imul(h ^ byte, 0x01000193) >>> 0;
  return h;
}

/** xorshift32, the twin of `_Xorshift` in tests/test_redact.py. */
function xorshift(seed: number): (bound: number) => number {
  let state = seed >>> 0 || 1;
  return (bound) => {
    state = (state ^ (state << 13)) >>> 0;
    state = (state ^ (state >>> 17)) >>> 0;
    state = (state ^ (state << 5)) >>> 0;
    return state % bound;
  };
}

function expandToken(kind: string, index: number, length?: number): string {
  const next = xorshift(fnv1a32(`${kind}:${index}:${length || 0}`));
  const pick = (alphabet: string, n: number): string => {
    let out = "";
    for (let i = 0; i < n; i++) out += alphabet[next(alphabet.length)]!;
    return out;
  };
  const prefixed = TOKEN_PREFIXED[kind];
  if (prefixed) {
    const [prefix, alphabet, fallback] = prefixed;
    return prefix + pick(alphabet, Math.max(length || fallback, prefix.length) - prefix.length);
  }
  switch (kind) {
    case "sg":
      return `SG.${pick(URLSAFE_CHARS, 22)}.${pick(URLSAFE_CHARS, 43)}`;
    case "aws_secret":
      return pick(`${ALNUM}/+`, length || 40);
    case "jwt": {
      const rest = Math.max(length || 80, 15) - 15; // `eyJ` + 5, `.` + 5, `.`
      const a = next(rest + 1);
      const b = next(rest + 1);
      const first = Math.min(a, b);
      const second = Math.max(a, b);
      const head = pick(URLSAFE_CHARS, 5 + first);
      const body = pick(URLSAFE_CHARS, 5 + second - first);
      return `eyJ${head}.${body}.${pick(URLSAFE_CHARS, rest - second)}`;
    }
    case "plain": {
      const n = length || 24;
      return pick(ALNUM, 1) + pick(PLAIN_CHARS, n - 1);
    }
    case "comma": {
      const n = Math.max(length || 24, 3);
      let text = "";
      while (text.length < n) text += `${pick(ALNUM, 1 + next(12))},`;
      return `${text.slice(0, n - 1)}z`;
    }
    case "quoted": {
      const n = length || 24;
      const head = pick(ALNUM, 1);
      if (n === 1) return head;
      const middle = pick(`${ALNUM} ,;:=/+.-`, n - 2);
      return head + middle + pick(ALNUM, 1);
    }
    case "urlsafe":
      return pick(URLSAFE_CHARS, length || 32);
    case "bearer":
      return pick(`${ALNUM}._~+/=-`, length || 32);
    case "b64":
      return pick(`${ALNUM}+/_-`, length || 32);
    case "mixed": {
      const body = pick(`${ALNUM}_+/=~.-`, Math.max(length || 33, 3));
      const upper = UPPER[next(26)]!;
      const lower = LOWER[next(26)]!;
      const digit = DIGITS[next(10)]!;
      return upper + lower + digit + body.slice(3);
    }
    case "pem": {
      const n = length || 128;
      const body = pick(`${ALNUM}+/`, n);
      const rows: string[] = [];
      for (let i = 0; i < n; i += 64) rows.push(body.slice(i, i + 64));
      return rows.join("\n");
    }
    case "userinfo": {
      const n = length || 24;
      const head = pick(ALNUM, 1);
      if (n === 1) return head;
      const middle = pick(`${ALNUM}_.~%!$'()*+,;=:@-`, n - 2);
      return head + middle + pick(ALNUM, 1);
    }
    case "user":
      return pick(`${ALNUM}_.~%-`, length || 24);
    case "cli": {
      const head = pick(ALNUM, 1);
      return head + pick(`${ALNUM}_.~%+,=:@!*-`, (length || 24) - 1);
    }
    case "lines": {
      const n = length || 80;
      const body = pick(ALNUM, n);
      const rows: string[] = [];
      for (let i = 0; i < n; i += 40) rows.push(body.slice(i, i + 40));
      return rows.join("\n  ");
    }
    default:
      throw new Error(`unknown token kind ${kind}`);
  }
}

function expandTokens(text: string): string {
  return text.replace(TOKEN_RE, (_m, kind: string, index: string, length?: string) => expandToken(kind, Number(index), length ? Number(length) : undefined));
}

/** The expanded text and the `[start, end]` of every expanded token in it. */
function expandWithSpans(text: string): { text: string; secrets: Array<[number, number]> } {
  let out = "";
  let last = 0;
  const secrets: Array<[number, number]> = [];
  for (const m of text.matchAll(TOKEN_RE)) {
    out += text.slice(last, m.index);
    const token = expandToken(m[1]!, Number(m[2]), m[3] ? Number(m[3]) : undefined);
    secrets.push([out.length, out.length + token.length]);
    out += token;
    last = m.index! + m[0].length;
  }
  return { text: out + text.slice(last), secrets };
}

/** A theme that colors with plain SGR sequences, like omp's `fg`, and throws on an unknown color. */
const colorTheme: BarTheme = {
  fg: (color, text) => {
    const code: Record<string, number> = { accent: 36, error: 31, success: 32, warning: 33, dim: 90, muted: 37 };
    if (code[color] === undefined) throw new Error(`Unknown theme color: ${color}`);
    return `\x1b[${code[color]}m${text}\x1b[39m`;
  },
  bold: (text) => `\x1b[1m${text}\x1b[22m`,
  getSpinnerFrames: () => ["⠋", "⠙", "⠹", "⠸"],
};
const powerlineTheme: BarTheme = {
  ...colorTheme,
  getBgAnsi: () => "\x1b[48;5;236m",
  getFgAnsi: (color) => (color === "text" ? "\x1b[38;5;252m" : "\x1b[38;5;240m"),
  sep: { powerlineThinLeft: "\ue0b3", powerlineCapLeft: "\ue0b6", powerlineLeft: "\ue0b0" },
};

interface BusRun {
  id: string;
  agent?: string;
  status?: "pending" | "running" | "completed" | "failed" | "aborted";
  durationMs?: number;
  tool?: string;
  intent?: string;
  call?: string;
  index?: number;
  detached?: boolean;
}

function progress(model: SwarmBarModel, run: BusRun, now: number): boolean {
  return ingestProgress(
    model,
    {
      index: run.index ?? 0,
      agent: run.agent ?? "explore",
      task: "task text",
      parentToolCallId: run.call,
      detached: run.detached,
      progress: {
        index: run.index ?? 0,
        id: run.id,
        agent: run.agent ?? "explore",
        status: run.status ?? "running",
        task: "task text",
        toolCount: 3,
        requests: 1,
        tokens: 100,
        cost: 0,
        durationMs: run.durationMs ?? 0,
        currentTool: run.tool,
        currentToolIntent: run.intent,
      },
    },
    now,
  );
}

function live(
  runs: Array<{ run_id: string; task_id?: string; agent?: string; state: string; elapsed_s?: number; stalled?: boolean; activity?: string; error?: string | null }>,
  swarm: Record<string, unknown> = {},
) {
  return { schema: "swarm.live.v1", correlation_id: "c", seq: 1, updated_wall: 1_760_000_000, swarm: { max_parallel: 4, ...swarm }, runs };
}

const plain = (model: SwarmBarModel, width: number, now: number): string[] => renderSwarmBar(model, width, undefined, now);

/** Strip every complete SGR/CSI sequence: anything left of an ESC is a cut or stray escape. */
function hasStrayEscape(line: string): boolean {
  return line.replace(/\x1b\[[0-9;]*m/g, "").includes("\x1b");
}

/** A theme whose every member throws: the bar must still draw plain rows. */
const throwingTheme: BarTheme = {
  fg: () => {
    throw new Error("boom");
  },
  bold: () => {
    throw new Error("boom");
  },
  getSpinnerFrames: () => {
    throw new Error("boom");
  },
  getBgAnsi: () => {
    throw new Error("boom");
  },
  getFgAnsi: () => {
    throw new Error("boom");
  },
  get sep(): never {
    throw new Error("boom");
  },
};
/** A transparent bar (`\x1b[49m` background): powerline pieces but no caps. */
const transparentTheme: BarTheme = { ...powerlineTheme, getBgAnsi: () => "\x1b[49m" };
/** Theme variants of invariant I1: none, colours only, full powerline, transparent, partial (no separators), throwing. */
const themeVariants: Array<[string, BarTheme | undefined]> = [
  ["no theme", undefined],
  ["colors only", colorTheme],
  ["powerline", powerlineTheme],
  ["transparent", transparentTheme],
  ["partial powerline", { ...colorTheme, getBgAnsi: powerlineTheme.getBgAnsi }],
  ["throwing", throwingTheme],
];

/** What the user reads of a plain row: the right padding removed. */
const visible = (row: string): string => stripAnsi(row).trimEnd();

function savings(extra: Partial<TokenSavingsSnapshot> = {}): TokenSavingsSnapshot {
  return { v: 1, sessionId: "sess-1", updatedAt: 1, baselineTokens: 10_000, currentTokens: 4_000, savedTokens: 6_000, points: [], ...extra };
}
/** Every optional measurement present. The provider cache tokens (77.7k, 33.3k) are unlike every other number on screen. */
const FULL_SAVINGS = savings({
  lastTurnMs: 900,
  cacheHitPct: 62,
  lastQueryMs: 40,
  rtkSavedTokens: 1500,
  lastPayloadChars: 156523,
  ttftMs: 310,
  lastIdleMs: 1200,
  costCents: 12,
  providerInput: 90_000,
  providerOutput: 50_000,
  providerCacheRead: 77_700,
  providerCacheWrite: 33_300,
  prefixChanged: 1,
  toolsChanged: 1,
  graphReady: 1,
});

/** A piece of row 1 with the priority the contract gives it; lists are in on-screen order. */
type Piece = [text: string, priority: number];
/** Row 1, left, of the `mixed()` model with a telemetry error. */
const MIXED_PIECES: Piece[] = [
  ["● swarm 10m00s", 0],
  ["2 run", 1],
  ["1 wait", 4],
  ["1 queued", 3],
  ["1 done", 4],
  ["1 fail", 2],
  ["1 timeout", 2],
  ["1 stall", 2],
  ["1 cancel", 5],
  ["slots 2/4", 6],
  ["telemetry unavailable", 2],
];
/** Row 1, right, of `FULL_SAVINGS`. */
const CHIP_PIECES: Piece[] = [
  ["graph ready", 11],
  ["bill 90k · out 50k", 10],
  ["idle 1200ms", 9],
  ["payload 156523c", 8],
  ["query 40ms", 7],
  ["rtk +1.5k", 6],
  ["cost 12c", 5],
  ["turn 900ms", 4],
  ["before 10k · now 4k", 3],
  ["prefix moved", 2],
  ["tools moved", 2],
  ["ttft 310ms", 2],
  ["cache 62%", 1],
  ["saved 6k", 0],
];

/**
 * The drop rule at every width 1..300: the row is exactly the width; a piece shown at one width is shown at every wider one;
 * the pieces on screen keep their order; and a piece is missing only if every piece that stayed has a smaller priority number
 * (or the same number and an earlier place). Widths where the identity (the first piece) is itself clipped are skipped.
 */
function expectDropOrder(label: string, render: (width: number) => string, pieces: readonly Piece[]): void {
  let before = new Set<number>();
  for (let width = 1; width <= 300; width++) {
    const row = render(width);
    if (displayWidth(row) !== width) throw new Error(`${label}@${width}: ${displayWidth(row)} columns: ${JSON.stringify(row)}`);
    const shown = new Set<number>();
    if (row.includes(pieces[0][0])) pieces.forEach(([text], index) => void (row.includes(text) && shown.add(index)));
    for (const gone of before) if (!shown.has(gone)) throw new Error(`${label}@${width}: "${pieces[gone][0]}" was shown at ${width - 1} and vanished: ${JSON.stringify(row)}`);
    let last = -1;
    for (const index of [...shown].sort((a, b) => a - b)) {
      const at = row.indexOf(pieces[index][0]);
      if (at <= last) throw new Error(`${label}@${width}: "${pieces[index][0]}" is out of order: ${JSON.stringify(row)}`);
      last = at;
    }
    if (shown.size > 0) {
      for (const kept of shown) {
        for (let gone = 0; gone < pieces.length; gone++) {
          if (shown.has(gone)) continue;
          const [, keptPriority] = pieces[kept];
          const [, gonePriority] = pieces[gone];
          if (keptPriority > gonePriority || (keptPriority === gonePriority && kept > gone)) {
            throw new Error(`${label}@${width}: "${pieces[gone][0]}" (P${gonePriority}) was dropped while "${pieces[kept][0]}" (P${keptPriority}) stayed: ${JSON.stringify(row)}`);
          }
        }
      }
    }
    before = shown;
  }
}

/** The narrowest width 1..300 at which `text` is on screen. */
function firstWidth(render: (width: number) => string, text: string): number | undefined {
  for (let width = 1; width <= 300; width++) if (render(width).includes(text)) return width;
  return undefined;
}

/** `texts` come on screen in this order as the row widens, one strictly after the other. */
function expectAppearOrder(label: string, render: (width: number) => string, texts: readonly string[]): void {
  const widths = texts.map((text) => firstWidth(render, text));
  const got = Object.fromEntries(texts.map((text, index) => [text, widths[index]]));
  texts.forEach((text, index) => {
    const width = widths[index];
    if (width === undefined) throw new Error(`${label}: "${text}" never appears up to 300 columns: ${JSON.stringify(got)}`);
    const earlier = widths[index - 1];
    if (earlier !== undefined && width <= earlier) throw new Error(`${label}: "${text}" must need more columns than "${texts[index - 1]}": ${JSON.stringify(got)}`);
  });
}

/**
 * Deterministic redaction contract on the original UTF-16 offsets: ordered, bounded spans reconstruct the ENTIRE output,
 * including untouched text. A masked output is a fixed point, and its status-row consumer stays within its display bound.
 * These are output regressions, not performance measurements; timing benchmarks belong outside unit tests.
 */
function expectRedactionProperties(text: string, label: string): { out: string; spans: Array<[number, number]> } {
  const spans = redactSpans(text);
  const valid = spans.every(([a, b], i) =>
    Number.isInteger(a) && Number.isInteger(b) && 0 <= a && a < b && b <= text.length && (i === 0 || spans[i - 1]![1] < a),
  );
  expect({ label, valid }).toEqual({ label, valid: true });
  const parts: string[] = [];
  let last = 0;
  let length = text.length;
  for (const [a, b] of spans) {
    parts.push(text.slice(last, a), "[REDACTED]");
    length += "[REDACTED]".length - (b - a);
    last = b;
  }
  parts.push(text.slice(last));
  const out = redactText(text);
  expect({ label, reconstructed: out === parts.join(""), length: out.length }).toEqual({ label, reconstructed: true, length });
  expect({ label, fixedPoint: redactText(out) === out }).toEqual({ label, fixedPoint: true });
  expect(displayWidth(sanitizeText(text))).toBeLessThanOrEqual(120);
  return { out, spans };
}

/** Both former timing sizes must mask the whole planted suffix, even after quote/command/carrier floods. */
function expectLargeRedaction(unit: string, tail: string, label: string): string {
  let big = "";
  for (const size of [250_000, 1_000_000]) {
    const text = unit.repeat(Math.floor(size / unit.length)) + tail;
    const { out, spans } = expectRedactionProperties(text, `${label}@${size}`);
    const start = text.length - "hunter2".length;
    expect({ label, size, covered: spans.some(([a, b]) => a <= start && b === text.length), leaked: out.includes("hunter2") })
      .toEqual({ label, size, covered: true, leaked: false });
    big = out;
  }
  return big;
}

describe("formatElapsed", () => {
  const cases = JSON.parse(readFileSync(join(REPO_ROOT, "tests", "fixtures", "elapsed_cases.json"), "utf8")) as Array<[number | string, string]>;
  test("is byte-identical to the Python twin for every fixture case", () => {
    expect(cases.length).toBeGreaterThan(10);
    for (const [input, expected] of cases) {
      const seconds = typeof input === "string" ? Number(input) : input;
      expect({ input, text: formatElapsed(seconds) }).toEqual({ input, text: expected });
    }
  });

  test("non-numbers render 0s", () => {
    expect(formatElapsed("7" as unknown as number)).toBe("0s");
    expect(formatElapsed(undefined as unknown as number)).toBe("0s");
  });
});

describe("redactText shared vectors", () => {
  type Needle = string | string[];
  interface Vector {
    name: string;
    input: Needle;
    must_not_contain: Needle[];
    must_contain: Needle[];
    expected?: string;
  }
  const flat = (value: Needle): string => expandTokens(typeof value === "string" ? value : value.join("")); // `{{tok:...}}` secrets expand at runtime
  interface PropertySpec {
    seed: number;
    count: number;
    secret_from: number;
    templates: string[];
    sha256: string;
  }
  const shared = JSON.parse(readFileSync(join(REPO_ROOT, "tests", "fixtures", "redact_cases.json"), "utf8")) as { cases: Vector[]; property: PropertySpec; property_comments: PropertySpec; property_joined: PropertySpec; property_comment_lines: PropertySpec };
  const { cases } = shared;

  test("tests/fixtures/redact_cases.json: the same vectors tests/test_redact.py runs against swarm/redact.py", () => {
    expect(cases.length).toBeGreaterThan(30);
    for (const vector of cases) {
      const out = redactText(flat(vector.input));
      for (const needle of vector.must_not_contain) expect({ name: vector.name, leaked: out.includes(flat(needle)) }).toEqual({ name: vector.name, leaked: false });
      for (const needle of vector.must_contain) expect({ name: vector.name, kept: out.includes(flat(needle)) }).toEqual({ name: vector.name, kept: true });
      if (vector.expected !== undefined) expect({ name: vector.name, out }).toEqual({ name: vector.name, out: vector.expected });
      expect({ name: vector.name, fixedPoint: redactText(out) === out }).toEqual({ name: vector.name, fixedPoint: true }); // a masked output is a fixed point
    }
  });
  test("tests/fixtures/redact_cases.json property: seeded shell snippets leak no planted secret and hash to the Python suite's digest", () => {
    // the twin of `_property_snippets` in tests/test_redact.py: the same xorshift32 draws, the same templates, the same placeholders
    const spec = shared.property;
    const next = xorshift(spec.seed);
    const first = spec.secret_from;
    let planted = 0;
    const outs: string[] = [];
    for (let c = 0; c < spec.count; c++) {
      const parts: string[] = [];
      const k = 2 + next(3);
      for (let j = 0; j < k; j++) parts.push(spec.templates[next(spec.templates.length)]!);
      parts.push(spec.templates[first + next(spec.templates.length - first)]!);
      const text = `${parts.join("\n")}\n`.replace(/\{S\}/g, () => `{{tok:urlsafe:${100000 + ++planted}:20}}`);
      const out = redactText(expandTokens(text));
      for (const m of text.matchAll(TOKEN_RE)) expect({ text, leaked: out.includes(expandTokens(m[0])) }).toEqual({ text, leaked: false });
      outs.push(out);
    }
    expect(outs.length).toBe(spec.count);
    expect(createHash("sha256").update(outs.join("\u0000"), "utf8").digest("hex")).toBe(spec.sha256);
  });
  test("tests/fixtures/redact_cases.json property_comments: comments with sensitive values leak no 6-byte window of a planted secret and hash to the Python suite's digest", () => {
    // the twin of the `property_comments` test in tests/test_redact.py: the same draws, templates and placeholders; every window is checked
    const spec = shared.property_comments;
    const next = xorshift(spec.seed);
    const first = spec.secret_from;
    let planted = 0;
    const outs: string[] = [];
    const leaks: string[] = [];
    for (let c = 0; c < spec.count; c++) {
      const parts: string[] = [];
      const k = 2 + next(3);
      for (let j = 0; j < k; j++) parts.push(spec.templates[next(spec.templates.length)]!);
      parts.push(spec.templates[first + next(spec.templates.length - first)]!);
      const text = `${parts.join("\n")}\n`.replace(/\{S\}/g, () => `{{tok:urlsafe:${100000 + ++planted}:20}}`);
      const out = redactText(expandTokens(text));
      for (const m of text.matchAll(TOKEN_RE)) {
        const secret = expandTokens(m[0]);
        for (let w = 0; w + 6 <= secret.length; w++) {
          if (out.includes(secret.slice(w, w + 6))) leaks.push(text);
        }
      }
      outs.push(out);
    }
    expect(outs.length).toBe(spec.count);
    expect(leaks.slice(0, 3)).toEqual([]);
    expect(createHash("sha256").update(outs.join("\u0000"), "utf8").digest("hex")).toBe(spec.sha256);
  });
  test("tests/fixtures/redact_cases.json property_joined: joined words with blanks inside quoted segments leak no planted secret and hash to the Python suite's digest", () => {
    // the twin of `test_joined_property_snippets_with_blanks_inside_quoted_segments_leak_nothing_and_match_typescript`: the same draws, templates and placeholders
    const spec = shared.property_joined;
    const next = xorshift(spec.seed);
    const first = spec.secret_from;
    let planted = 0;
    const outs: string[] = [];
    for (let c = 0; c < spec.count; c++) {
      const parts: string[] = [];
      const k = 2 + next(3);
      for (let j = 0; j < k; j++) parts.push(spec.templates[next(spec.templates.length)]!);
      parts.push(spec.templates[first + next(spec.templates.length - first)]!);
      const text = `${parts.join("\n")}\n`.replace(/\{S\}/g, () => `{{tok:urlsafe:${100000 + ++planted}:20}}`);
      const out = redactText(expandTokens(text));
      for (const m of text.matchAll(TOKEN_RE)) expect({ text, leaked: out.includes(expandTokens(m[0])) }).toEqual({ text, leaked: false });
      outs.push(out);
    }
    expect(outs.length).toBe(spec.count);
    expect(createHash("sha256").update(outs.join("\u0000"), "utf8").digest("hex")).toBe(spec.sha256);
  });

  test("tests/fixtures/redact_cases.json property_comment_lines: stray quotes in comments before sensitive assignments leak no 6-byte window of a planted secret and hash to the Python suite's digest", () => {
    // the twin of `test_comment_line_property_snippets_leak_no_planted_secret_and_match_typescript`: the same draws, templates and placeholders
    const spec = shared.property_comment_lines;
    const next = xorshift(spec.seed);
    const first = spec.secret_from;
    let planted = 0;
    const outs: string[] = [];
    const leaks: string[] = [];
    for (let c = 0; c < spec.count; c++) {
      const parts: string[] = [];
      const k = 2 + next(3);
      for (let j = 0; j < k; j++) parts.push(spec.templates[next(spec.templates.length)]!);
      parts.push(spec.templates[first + next(spec.templates.length - first)]!);
      const text = `${parts.join("\n")}\n`.replace(/\{S\}/g, () => `{{tok:urlsafe:${100000 + ++planted}:20}}`);
      const out = redactText(expandTokens(text));
      for (const m of text.matchAll(TOKEN_RE)) {
        const secret = expandTokens(m[0]);
        for (let w = 0; w + 6 <= secret.length; w++) {
          if (out.includes(secret.slice(w, w + 6))) leaks.push(text);
        }
      }
      outs.push(out);
    }
    expect(outs.length).toBe(spec.count);
    expect(leaks.slice(0, 3)).toEqual([]);
    expect(createHash("sha256").update(outs.join("\u0000"), "utf8").digest("hex")).toBe(spec.sha256);
  });

  test("tests/fixtures/redact_generated.json: 6000 seeded option lists give exactly the Python suite's output (reference model + wild shapes)", () => {
    type Generated = Array<{ input: string; expected: string }>;
    const { cases: generated, wild } = JSON.parse(readFileSync(join(REPO_ROOT, "tests", "fixtures", "redact_generated.json"), "utf8")) as {
      cases: Generated;
      wild: Generated;
    };
    expect([generated.length, wild.length]).toEqual([3000, 3000]);
    // `cases`: the reference model of the spec; `wild`: Python's output over every separator/quote/flag shape (and no sensitive value survives there).
    for (const group of [generated, wild]) {
      const wrong = group.filter((c) => redactText(c.input) !== c.expected);
      expect(wrong.slice(0, 3)).toEqual([]);
    }
  });
  test("tests/fixtures/redact_generated.json: the quoting model (every quoting form of a secret value in every option form) gives exactly Python's output and leaves no planted secret", () => {
    const { quoting } = JSON.parse(readFileSync(join(REPO_ROOT, "tests", "fixtures", "redact_generated.json"), "utf8")) as {
      quoting: Array<{ input: string; expected: string }>;
    };
    expect(quoting.length).toBeGreaterThan(2500);
    const wrong = quoting.filter((c) => {
      const out = redactText(c.input);
      return out !== c.expected || out.includes("Zq") || redactText(out) !== out;
    });
    expect(wrong.slice(0, 3)).toEqual([]);
  });

  test("GL8: a password that starts with a blank inside escaped quotes is masked, whatever the quoting form", () => {
    expect(redactText('mysql -p\\" leading-space\\" db')).toBe("mysql -p[REDACTED] db");
    expect(redactText('{"cmd": "mysql -p\\" leading-space\\" db"}')).toBe('{"cmd": "mysql -p[REDACTED] db"}');
    for (const quoted of ['" "', "' '", '\\" \\"', '$" "', "$' '", '"..."', '\\"!!\\"']) {
      expect({ quoted, out: redactText(`mysql -p${quoted} db`) }).toEqual({ quoted, out: "mysql -p[REDACTED] db" });
      expect({ quoted, out: redactText(`redis-cli -a ${quoted} ping`) }).toEqual({ quoted, out: "redis-cli -a [REDACTED] ping" });
      expect({ quoted, out: redactText(`curl -u me:${quoted} h`) }).toEqual({ quoted, out: "curl -u [REDACTED] h" });
    }
    const prose = 'run "mysql -p" ok; use \\"mysql -p\\" ok; mysql "-p" and \\"-p\\" ok; \'mysql -p\' ok';
    expect(redactText(prose)).toBe(prose);
  });

  test("GL8: 1 MB of quote openers, escaped quotes and unclosed quotes preserves redaction spans and masks the trailing password", () => {
    const units = ['mysql -p" ', "mysql -p\\\" ", "-u me:'", "x --password $'", 'DB_PASSWORD="', 'mysql -p\\\\\\" ', '"password": "', '\\"password\\": \\"', "a\\"];
    for (const unit of units) {
      const out = expectLargeRedaction(unit, "\npassword=hunter2", unit);
      expect({ unit, masked: !out.includes("hunter2") }).toEqual({ unit, masked: true });
    }
  });

  /** The class-level properties (GL5-B): detectors report spans on the ORIGINAL text, one union replaces them. */
  describe("span properties (shared tests/fixtures/redact_generated.json)", () => {
    interface PropertyCase {
      input: string; // with `{{tok:...}}` placeholders: the planted secrets are exactly the expanded tokens
      digest: string;
      open?: number;
    }
    const fixture = JSON.parse(readFileSync(join(REPO_ROOT, "tests", "fixtures", "redact_generated.json"), "utf8")) as {
      property: PropertyCase[];
      noise: PropertyCase[];
      random: { seed: number; count: number; fragments: string[]; inputs: string; digests: string[] };
      tokens: Array<[string, string]>;
    };
    const property = fixture.property.map((c) => ({ ...c, ...expandWithSpans(c.input) })); // text: the expanded input, secrets: its planted ranges
    const digest = (text: string): string => {
      const spans = redactSpans(text).map(([a, b]) => `${a},${b}`).join(";");
      return createHash("sha256").update(`${redactText(text)}\u0000${spans}`, "utf8").digest("hex").slice(0, 8);
    };
    const covered = (spans: Array<[number, number]>, start: number, end: number): boolean => spans.some(([a, b]) => a <= start && end <= b);

    test("soundness: no byte of a planted secret (1..5000 characters, any carrier) survives", () => {
      expect(property.length).toBeGreaterThan(2000);
      const leaks = property.filter((c) => c.secrets.some(([start, end]) => !covered(redactSpans(c.text), start, end)));
      expect(leaks.slice(0, 3).map((c) => c.text.slice(0, 200))).toEqual([]);
    });

    test("the placeholder expansion is the Python algorithm: golden digests and provider shapes", () => {
      expect(fixture.tokens.length).toBeGreaterThan(60);
      const wrong = fixture.tokens.filter(([ref, sha]) => createHash("sha256").update(expandTokens(ref), "utf8").digest("hex").slice(0, 16) !== sha);
      expect(wrong.map(([ref]) => ref)).toEqual([]);
      const shapes: Record<string, RegExp> = {
        aws_key: /^AKIA[A-Z2-7]{16}$/,
        aws_secret: /^[A-Za-z0-9/+]{40}$/,
        ghp: /^ghp_[A-Za-z0-9]{36}$/,
        github_pat: /^github_pat_[A-Za-z0-9_]{82}$/,
        xoxb: /^xoxb-[A-Za-z0-9-]{50}$/,
        sk_live: /^sk_live_[A-Za-z0-9]{24}$/,
        aiza: /^AIza[A-Za-z0-9_-]{35}$/,
        sg: /^SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}$/,
        npm: /^npm_[A-Za-z0-9]{36}$/,
        glpat: /^glpat-[A-Za-z0-9_-]{20}$/,
        jwt: /^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/,
        pem: /^([A-Za-z0-9+/]{64}\n)*[A-Za-z0-9+/]{1,64}$/,
      };
      for (const [kind, shape] of Object.entries(shapes)) expect({ kind, ok: shape.test(expandToken(kind, 9)) }).toEqual({ kind, ok: true });
      const { text, secrets } = expandWithSpans("x={{tok:plain:1:12}};y={{tok:aws_key:2}}");
      expect(secrets.map(([a, b]) => text.slice(a, b))).toEqual([expandToken("plain", 1, 12), expandToken("aws_key", 2)]);
    });

    test("the structured cases give the Python spans and output byte for byte", () => {
      const wrong = property.filter((c) => digest(c.text) !== c.digest);
      expect(wrong.slice(0, 3).map((c) => c.text.slice(0, 200))).toEqual([]);
    });

    test("GL9: planted secrets survive quote noise (prose apostrophes, stray quotes, multi-line quoted values, restarting lines) and match Python byte for byte", () => {
      const noise = fixture.noise.map((c) => ({ ...c, ...expandWithSpans(c.input) }));
      expect(noise.length).toBe(2400);
      const leaks = noise.filter((c) => c.secrets.some(([start, end]) => !covered(redactSpans(c.text), start, end)));
      expect(leaks.slice(0, 3).map((c) => c.text.slice(0, 200))).toEqual([]);
      const wrong = noise.filter((c) => digest(c.text) !== c.digest);
      expect(wrong.slice(0, 3).map((c) => c.text.slice(0, 200))).toEqual([]);
    });

    test("GL9: a later unmatched quote never changes how an earlier value is read (no state shared by restarting scans)", () => {
      expect(redactText("'password': 'alpha\n  beta'\n# sshpass -p'")).toBe("'password': '[REDACTED]'\n# sshpass -p'");
      const heads = ["'password': 'alpha\n  beta'", '"password": "alpha\n  beta"', "DB_PASSWORD='alpha\n  beta'", 'DB_PASSWORD="alpha\n  beta"',
        "tool --password 'alpha\n  beta' db", "mysql -p'alpha\n  beta' db", "curl -u 'me:alpha\n  beta' h", 'sshpass -p "alpha\n  beta" ssh h'];
      for (const tail of ["\n# sshpass -p'", '\n# mysql -p"', "\n'", '\n"', "\ndon't", " ; echo 'x"]) {
        for (const head of heads) {
          const out = redactText(head + tail);
          expect({ head, tail, ok: !out.includes("alpha") && !out.includes("beta") }).toEqual({ head, tail, ok: true });
        }
      }
    });

    test("GL9: unrelated quotes do not turn a password opener into a closer", () => {
      expect(redactText("echo \"it's ready\"; mysql -p'***' db")).toBe("echo \"it's ready\"; mysql -p[REDACTED] db");
      const noises = ["it's ready", "don't", "the users' list", 'echo "it\'s ready";', "'cause", "rock 'n' roll", "O'Brien", 'say "hi', "'", '"', '5" wide', "x = 'it''s'"];
      const commands = ["mysql -p'%s' db", 'mysql -p"%s" db', "sshpass -p '%s' ssh h", "redis-cli -a '%s' ping", 'docker login -p "%s" r', "curl -u me:'%s' h"];
      for (const noise of noises) {
        for (const command of commands) {
          for (const password of ["***", "!@#", "&;|", ")]}"]) {
            for (const joiner of ["; ", "\n", " && ", " "]) {
              const out = redactText(noise + joiner + command.replace("%s", password));
              expect({ noise, joiner, command, password, ok: !out.replace(noise, "").includes(password) }).toEqual({ noise, joiner, command, password, ok: true });
            }
          }
        }
      }
      for (const [text, expected] of [
        ["'unclosed && mysql -p'***' db", "'unclosed && mysql -p[REDACTED] db"],
        ['"quoted ; mariadb -p")]}" db', '"quoted ; mariadb -p[REDACTED] db'],
        ["the users' list; sshpass -p'&;|' ssh h", "the users' list; sshpass -p[REDACTED] ssh h"],
      ] as const) {
        expect(redactText(text)).toBe(expected);
      }
    });

    test("union: splitting the input into carriers, in any order, changes no span", () => {
      const closed = property.filter((c) => c.open === undefined).map((c) => c.text).slice(0, 700);
      const n = closed.length;
      expect(n).toBe(700);
      for (let i = 0; i < n; i++) {
        const trio = [closed[i]!, closed[(i * 7 + 3) % n]!, closed[(i * 13 + 5) % n]!];
        const joiner = i % 7 === 0 ? "\n\n" : "\n";
        for (const parts of [trio, [...trio].reverse()]) {
          const expected: Array<[number, number]> = [];
          let offset = 0;
          for (const part of parts) {
            for (const [a, b] of redactSpans(part)) expected.push([a + offset, b + offset]);
            offset += part.length + joiner.length;
          }
          const joined = parts.join(joiner);
          expect(redactSpans(joined)).toEqual(expected);
          expect(redactText(joined)).toBe(parts.map(redactText).join(joiner));
        }
      }
    });

    test("80k seeded random strings: TypeScript and Python agree on every output and span", () => {
      const { seed, count, inputs, digests } = fixture.random;
      const fragments = fixture.random.fragments.map(expandTokens); // the table holds placeholders, never a secret-shaped literal
      expect(count).toBe(80_000);
      let state = seed >>> 0 || 1; // xorshift32, the twin of `_Xorshift` in tests/test_redact.py
      const next = (bound: number): number => {
        state = (state ^ (state << 13)) >>> 0;
        state = (state ^ (state >>> 17)) >>> 0;
        state = (state ^ (state << 5)) >>> 0;
        return state % bound;
      };
      const strings: string[] = [];
      for (let i = 0; i < count; i++) {
        let text = "";
        for (let k = 2 + next(11); k > 0; k--) text += fragments[next(fragments.length)]!;
        strings.push(text);
      }
      expect(createHash("sha256").update(strings.join("\u0000"), "utf8").digest("hex").slice(0, 16)).toBe(inputs);
      const wrong: number[] = [];
      strings.forEach((text, i) => {
        if (digest(text) !== digests[i]) wrong.push(i);
      });
      expect(wrong.slice(0, 3).map((i) => strings[i])).toEqual([]);
    });

    test("long session cookies (513, 4096 and 100000 characters) are masked whole", () => {
      const b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
      for (const length of [513, 4096, 100_000]) {
        let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.";
        for (let i = 0; jwt.length < length; i++) jwt += b64[(i * 7 + (i >> 3)) % b64.length]!;
        for (const carrier of ["Cookie: session=%s", "Set-Cookie: session=%s; Path=/", "curl -H 'Cookie: session=%s' https://h.test", "Authorization: Token %s"]) {
          const text = `saw ${carrier.replace("%s", jwt)} next`;
          const out = redactText(text);
          const at = 4 + carrier.indexOf("%s");
          expect({ length, carrier, covered: covered(redactSpans(text), at, at + jwt.length) }).toEqual({ length, carrier, covered: true });
          for (let i = 0; i + 12 <= jwt.length; i += 997) expect(out.includes(jwt.slice(i, i + 12))).toBe(false);
          expect(sanitizeText(text, 400).includes(jwt.slice(-40))).toBe(false);
        }
      }
    });
  });

  test("1 MB assignment floods mask the complete trailing password without truncating other text", () => {
    for (const unit of ["a=", "a=a=", "user=a,", "user=a;", "x=1&"]) {
      for (const size of [250_000, 1_000_000]) {
        const prefix = unit.repeat(Math.floor(size / unit.length));
        const text = prefix + "password=hunter2";
        const { out, spans } = expectRedactionProperties(text, `${unit}@${size}`);
        expect(spans).toEqual([[prefix.length + "password=".length, text.length]]);
        expect(out).toBe(prefix + "password=[REDACTED]");
        expect({ unit, masked: out.endsWith("password=[REDACTED]") && !out.includes("hunter2") }).toEqual({ unit, masked: true });
      }
    }
  });

  // The comment scan keeps one cursor through the comment line (`vscan` in `Lexer`): every character is scanned a bounded number of
  // times. The outputs pinned here are the pre-fix outputs (the quadratic scan returned the same text; only its speed changed).
  test("a comment of repeated assignments keeps its pre-fix output", () => {
    expect(redactText("# " + "PASSWORD=".repeat(4) + '"x"')).toBe("# PASSWORD=[REDACTED]");
    expect(redactText("# " + "x=y ".repeat(4))).toBe("# x=y x=y x=y x=y ");
    expect(redactText("# " + 'PASSWORD="ab cd" '.repeat(3) + "tail")).toBe("# " + "PASSWORD=[REDACTED] ".repeat(3) + "tail");
    expect(redactText("# " + "PASSWORD=a'b c'\"d\" ".repeat(2) + "end")).toBe("# " + "PASSWORD=[REDACTED] ".repeat(2) + "end");
  });

  test("1 MB comments of repeated assignments preserve exact output and whole-value spans", () => {
    for (const unit of ["PASSWORD=", "x=y ", 'PASSWORD="ab cd" ']) {
      for (const size of [250_000, 1_000_000]) {
        const reps = Math.floor(size / unit.length);
        const text = "# " + unit.repeat(reps) + '"x"';
        const { out, spans } = expectRedactionProperties(text, `${unit}@${size}`);
        if (unit === "PASSWORD=") {
          expect(spans).toEqual([["# PASSWORD=".length, text.length]]);
          expect(out).toBe("# PASSWORD=[REDACTED]");
          expect(sanitizeText(text)).toBe("# PASSWORD=[REDACTED]");
        } else if (unit === "x=y ") {
          expect(spans).toEqual([]);
          expect(out).toBe(text);
        } else {
          expect(spans.length).toBe(reps);
          expect(spans.every(([a, b], i) => a === 2 + i * unit.length + "PASSWORD=".length && b === 2 + (i + 1) * unit.length - 1)).toBe(true);
          expect(out).toBe("# " + "PASSWORD=[REDACTED] ".repeat(reps) + '"x"');
          expect(out).not.toContain("ab cd");
          expect(sanitizeText(text)).not.toContain("ab cd");
        }
      }
    }
  });

  test("a space-separated password flag in currentToolArgs never reaches the activity row", () => {
    const row = sanitizeText("login --password hunter2");
    expect(row).toBe("login --password [REDACTED]");
  });

  test("1 MB adversarial inputs preserve output/span consistency and mask a planted password", () => {
    const units = ["a://b:b:b:", "--password ", "mode=fast ", "'a':'", "Authorization: Basic ", "-----BEGIN PRIVATE KEY-----", "Bearer ", "a.b-",
      "SWARM_ED25519_KEY=", "SWARM_ED25519_KEY_FILE=", "a,", "k=a,", "--ed25519-key ", "ed25519.seed.", "Cookie: ", "Authorization:x",
      "--mode a;", "-v ", "password=a,token = ", "token=a,--password ", 'Cookie: a="', "x=-u=", '-u "', "A_KEY=", "STRIPE_KEY=A_KEY=", "a://u:",
      'password="', "-u=a:", 'Cookie: a="b" ', "-u a:", "--user=-u="];
    for (const unit of units) {
      const out = expectLargeRedaction(unit, "\npassword=hunter2", unit);
      if (unit === "-----BEGIN PRIVATE KEY-----") {
        expect(out).toBe("[REDACTED]"); // an unclosed PEM masks everything, not just its first header
        expect(sanitizeText(unit.repeat(Math.floor(1_000_000 / unit.length)))).toBe("[REDACTED]");
      }
      if (unit === "mode=fast ") {
        expect(out).toBe(unit.repeat(Math.floor(1_000_000 / unit.length)) + "\npassword=[REDACTED]");
      }
    }
  });

  test("a megabyte-long unbroken secret value is still redacted (the regex engine must not give up and leak it)", () => {
    for (const lead of ["AUTH=", "--password ", "{\"password\": \""]) {
      const out = redactText(`${lead}${"q".repeat(1_000_000)}${lead.startsWith("{") ? "\"}" : ""}`);
      expect({ lead, leaked: out.includes("qqqqqqqq") }).toEqual({ lead, leaked: false });
    }
  });

  test("a megabyte-long comma-separated secret value is redacted whole", () => {
    const out = redactText(`API_TOKEN=${"q,".repeat(500_000)} done`);
    expect(out.length).toBeLessThan(100); // bounded output: the 1 MB value is replaced, never echoed
    expect(out.includes("q,q")).toBe(false);
    expect(out.endsWith("[REDACTED] done")).toBe(true);
  });

  test("a signing seed assignment never reaches the activity row", () => {
    const seed = "0123456789abcdef".repeat(4);
    expect(sanitizeText(`SWARM_ED25519_KEY=${seed} bun run`)).toBe("SWARM_ED25519_KEY=[REDACTED] bun run");
    expect(sanitizeText(`export SWARM_SIGNING_KEY=${seed}`)).toBe("export SWARM_SIGNING_KEY=[REDACTED]");
    expect(sanitizeText("SWARM_ED25519_KEY_FILE=/home/u/signing-key.hex")).toBe("SWARM_ED25519_KEY_FILE=/home/u/signing-key.hex");
  });
});

describe("sanitization", () => {
  test("strips ANSI sequences and control characters, collapses whitespace", () => {
    expect(sanitizeText("\x1b[31mred\x1b[0m\x07 bell\r\n\tnext\u0000line\x1b]0;title\x07")).toBe("red bell next line");
    expect(sanitizeText("a\u202eevil\u2066x")).toBe("aevilx");
    expect(sanitizeText(42)).toBe("");
    expect(sanitizeText(undefined)).toBe("");
  });

  test("clips to the requested columns, wide characters included", () => {
    expect(sanitizeText("x".repeat(500), 10)).toBe("xxxxxxxxx…");
    expect(displayWidth(sanitizeText("日本語のテキストです".repeat(10), 21))).toBeLessThanOrEqual(21);
  });

  test("redacts the secret shapes of DESIGN 4.3 and leaves identifiers and prose alone", () => {
    const secrets = [
      "ghp_" + "a".repeat(30),
      "github_pat_" + "A1b2".repeat(8),
      "sk-" + "x1y2z3".repeat(4),
      "AKIA" + "ABCDEFGHIJKLMNOP",
      "xoxb-1234567890-abcdefghij",
      "Bearer " + "q".repeat(24),
      "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def",
      "https://user:hunter2@example.com/path",
      "API_TOKEN=abcdef123456",
      '{"password": "hunter2"}',
    ];
    for (const secret of secrets) {
      const out = sanitizeText(`ran with ${secret} ok`);
      expect({ secret, redacted: out.includes("[REDACTED]") }).toEqual({ secret, redacted: true });
      expect(out).not.toContain("hunter2");
      expect(out).not.toContain("a".repeat(30));
    }
    expect(redactText("-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----")).toBe("[REDACTED]");
    for (const safe of ["T7f3a-be.a1", "sha256:" + "0123456789abcdef".repeat(4), "edit swarm/x.py", "author=bob", "turns=3"]) {
      expect(sanitizeText(safe)).toBe(safe);
    }
  });

  test("redaction runs before clipping, so a cut cannot leave half a token behind", () => {
    const out = sanitizeText(`${"y".repeat(20)} ghp_${"b".repeat(40)}`, 30);
    expect(out).not.toContain("ghp_b");
  });

  test("a secret split by bidi, zero-width or control characters is redacted before they are stripped (they must not re-join it)", () => {
    const ghp = `ghp_${"Zq8Lm3Nv5Bx7Cd9Fg1Hj2Kp4Rs6Tw0Yu3Ab"}`;
    for (const mark of ["\u202e", "\u200b", "\u2066", "\ufeff", "\u0000", "\u0007", "\u009b"]) {
      const split = `${ghp.slice(0, 6)}${mark}${ghp.slice(6)}`;
      expect({ mark, out: sanitizeText(`x ${split} y`) }).toEqual({ mark, out: "x [REDACTED] y" });
      expect({ mark, out: sanitizeText(`pass${mark}word=hunter2secret`) }).toEqual({ mark, out: "password=[REDACTED]" });
      expect({ mark, out: sanitizeText(`AKIAABCDEF${mark}GHIJ012345`) }).toEqual({ mark, out: "[REDACTED]" });
    }
    // text without a secret keeps its display: controls still become spaces, bidi marks are still dropped
    expect(sanitizeText("a\u0000b\u202ec")).toBe("a bc");
  });

  test("re-serialised JSON pairs keep their structure (the closing escaped quote is not replaced by the key name)", () => {
    expect(redactText(String.raw`x={\"password\":\"abc\",\"user\":\"u\"}`)).toBe(String.raw`x={\"password\":\"[REDACTED]\",\"user\":\"u\"}`);
  });

  test("1 MB command-line, YAML and vendor-key floods preserve redaction spans and mask the trailing password", () => {
    for (const unit of ["mysql ", "docker login ", "sshpass -p ", "-u a:b ", "- name: PASSWORD\n  value: ", "password: |\n  ", "STRIPE_KEY=", "redis-cli ", "- name: ", "password:  "]) {
      const out = expectLargeRedaction(unit, "\npassword=hunter2", unit);
      expect({ unit, masked: out.endsWith("password=[REDACTED]") && !out.includes("hunter2") }).toEqual({ unit, masked: true });
    }
    expect(redactText("- name: PASSWORD\n  value: x\nmysql -u a -pb db\n".repeat(250_000)).split("[REDACTED]").length - 1).toBe(500_000);
  });

  // ---- GL6 round 6: command checks read whole arguments, Markdown delimiters around an option (twins of tests/test_redact.py) ----
  const COMMAND_LINES: Record<string, [string, string]> = {
    "docker login": ["docker login -u me -p ", ""],
    "podman login": ["podman login -p ", " reg.io"],
    "helm registry login": ["helm registry login reg.io -p ", ""],
    "az login": ["az login -u me -p ", ""],
    sshpass: ["sshpass -p ", " ssh host"],
    mysql: ["mysql -u root -p", " db"],
    mongosh: ["mongosh --host h -u me -p ", ""],
    "redis-cli": ["redis-cli -a ", " ping"],
    sqlcmd: ["sqlcmd -S h -U sa -P ", ""],
    "ssh-keygen": ["ssh-keygen -t ed25519 -N ", " -f k"],
  };

  test("command flags are checked after arguments of any length and number", () => {
    for (const [name, [before, after]] of Object.entries(COMMAND_LINES)) {
      for (const size of [1, 128, 129, 1000]) {
        for (const count of [0, 1, 10, 11, 40]) {
          const secret = expandToken("plain", size + count, 16);
          const parts = before.split(" -");
          const head = parts[0]!;
          const tail = ` -${parts.slice(1).join(" -")}`;
          const args = Array.from({ length: count }, (_, i) => ` --argument${i}=${"v".repeat(size)}`).join("");
          expect({ name, size, count, out: redactText(head + args + tail + secret + after) }).toEqual({ name, size, count, out: `${head}${args}${tail}[REDACTED]${after}` });
        }
      }
    }
  });

  test("Markdown delimiters around a command option keep its password masked", () => {
    const wrappers: Array<[string, string]> = [["`", "`"], ["(", ")"], ["[", "]"], ["<", ">"], ["**", "**"], ["*", "*"], ["__", "__"], ["~~", "~~"], ['"', '"'], ["'", "'"],
      ["{", "}"], ["**`", "`**"], ["(`", "`)"], ["[`", "`](docs)"], ['\\"', '\\"'], ["$(", ")"], ["<code>", "</code>"]];
    const options = ["--user admin:%s", "-u admin:%s", "--user=admin:%s", "-u=admin:%s", "--proxy-user a:%s", "docker login -u me -p %s", "mysql -u root -p%s db",
      "sshpass -p %s ssh h", "redis-cli -a %s ping", "sqlcmd -S h -P %s", "openssl rsa -passin pass:%s"];
    const secret = expandToken("plain", 7, 18);
    for (const [opener, closer] of wrappers) {
      for (const option of options) {
        const text = `Run ${opener}${option.replace("%s", secret)}${closer} now`;
        const out = redactText(text);
        expect({ text, ok: !out.includes(secret) && out.startsWith(`Run ${opener}`) && out.split("[REDACTED]").length === 2 }).toEqual({ text, ok: true });
      }
    }
  });

  test("the round six review findings", () => {
    expect(redactText("docker login -u admin: -p pw123")).toBe("docker login -u admin: -p [REDACTED]");
    expect(redactText(`docker login -u admin:${"x".repeat(200)} -p pw123 reg.io`)).toBe("docker login -u [REDACTED] -p [REDACTED] reg.io");
    expect(redactText("Run `--user admin:hunter2` now")).toBe("Run `--user [REDACTED] now");
    expect(redactText("Run (--user=admin:hunter2)")).toBe("Run (--user=[REDACTED]");
    expect(redactText("**--user admin:hunter2**")).toBe("**--user [REDACTED]");
    expect(redactText('"--user admin:hunter2" ok')).toBe('"--user [REDACTED]" ok');
    expect(sanitizeText("Run `--user admin:hunter2` now")).toBe("Run `--user [REDACTED] now");
    expect(sanitizeText("docker login -u admin: -p pw123")).toBe("docker login -u admin: -p [REDACTED]");
  });

  test("a new line or a semicolon ends the command whose flags are checked", () => {
    const unchanged = "docker login reg.io\nls -p x\nmysql -u root -p\nredis-cli -h h -p 6379 ping\ndocker login reg.io; ls -p x && tar -a y";
    expect(redactText(unchanged)).toBe(unchanged);
    expect(redactText("mysql -u root\nls -pSECRETX")).toBe("mysql -u root\nls -pSECRETX");
  });

  const PUNCT_PASSWORDS = ["!!!", "***", "...", "---", "()", "<>", "$$", "#1", "+++", "%%", "~~", "{}", "[]", "?!", "a*b", "** **"];
  const PUNCT_FLAGS: Array<[string, string, string]> = [
    ["mysql -u root ", "p", " db"],
    ["mariadb ", "p", " -e x"],
    ["sshpass ", "p", " ssh host"],
    ["mongosh --host h ", "p", " x"],
    ["docker login -u me ", "p", " reg.io"],
    ["helm registry login reg.io ", "p", ""],
    ["redis-cli ", "a", " ping"],
    ["sqlcmd -S h ", "P", " -Q q"],
    ["ssh-keygen ", "N", " -f k"],
  ];
  test("a punctuation-only command password is masked exactly in every attached and separate form (tests/test_redact.py's sweep)", () => {
    const closers = new Set("`\"')]}>*_.,;:");
    for (const [command, flag, after] of PUNCT_FLAGS) {
      for (const password of PUNCT_PASSWORDS) {
        for (const quote of ["'", '"']) expect(redactText(`${command}-${flag}${quote}${password}${quote}${after}`)).toBe(`${command}-${flag}[REDACTED]${after}`);
        if (!password.includes(" ") && ![...password].every((c) => closers.has(c))) expect(redactText(`${command}-${flag}${password}${after}`)).toBe(`${command}-${flag}[REDACTED]${after}`);
        if (!command.startsWith("mysql") && !command.startsWith("mariadb")) {
          expect(redactText(`${command}-${flag} '${password}'${after}`)).toBe(`${command}-${flag} [REDACTED]${after}`);
          if (!password.includes(" ") && !password.startsWith("-")) expect(redactText(`${command}-${flag} ${password}${after}`)).toBe(`${command}-${flag} [REDACTED]${after}`);
        }
      }
    }
  });

  test("a flag word in prose is not a password but its glued punctuation is", () => {
    const prose = "run `mysql -p` now; use (mysql -p) now; **mysql -p** then; \"mysql -p\" ok; mysql \"-p\" and \"-p\" ok; '-p', '-u' for mysql; mysql **-p**, ok";
    expect(redactText(prose)).toBe(prose);
    expect(redactText("mysql -p!!! db")).toBe("mysql -p[REDACTED] db");
    expect(redactText("sshpass -p'&x' ssh host; sshpass -p\"&\" ssh h")).toBe("sshpass -p[REDACTED] ssh host; sshpass -p[REDACTED] ssh h");
  });

  test("unterminated and escaped-quoted command values, and a value that swallowed the next sensitive name", () => {
    expect(redactText("sshpass -p 'abc def")).toBe("sshpass -p [REDACTED]");
    expect(redactText('docker login -p \\"abc def\\" reg')).toBe("docker login -p [REDACTED] reg");
    expect(redactText('curl -u "admin:my pass" https://h')).toBe("curl -u [REDACTED] https://h");
    expect(redactText('run --token \\"--password hunter2')).toBe("run --token [REDACTED]"); // an unclosed quote runs to the end of the line
    expect(redactText('--api-key = {--password=\\"user:hunter2\\"')).toBe("--api-key [REDACTED] [REDACTED]");
  });

  test("1 MB command-line floods preserve redaction spans and mask the trailing password", () => {
    const units = ["mysql -u a ", "docker login x ", "`--user a:", "docker login -p ", "redis-cli -a ", "-passin pass:", "--cmd=mysql ", "-u=-u=", "x -u ", "-u  ",
      "mysql -p'", 'sshpass -p "', "docker login -u a: -p ", "sqlcmd -P ", "ssh-keygen -N ", "__-u a:", "(-u a:"];
    for (const unit of units) {
      const out = expectLargeRedaction(unit, "\npassword=hunter2", unit);
      expect({ unit, masked: out.endsWith("password=[REDACTED]") && !out.includes("hunter2") }).toEqual({ unit, masked: true });
    }
  });

  test("long input is bounded before masking: 100 KB of URL-credential lookalikes does not stall, the head survives", () => {
    const out = sanitizeText(`head ${"a://b:b:b:".repeat(10_000)}`, 40);
    expect(out.startsWith("head ")).toBe(true);
    expect(displayWidth(out)).toBeLessThanOrEqual(40);
  });

  test("the bound backs up to a word boundary, so a cut never leaves the start of a secret in view", () => {
    const out = sanitizeText(`${"w ".repeat(2040)}ghp_${"b".repeat(40)}`, 10_000);
    expect(out).not.toContain("ghp_");
    expect(out).not.toContain("bbbb");
  });

  test("agent-supplied activity is sanitized when it enters the model", () => {
    const model = createModel();
    progress(model, { id: "a", intent: "\x1b[31mread\x1b[0m token=ghp_" + "c".repeat(30) }, T0);
    const [row] = visibleRuns(model, T0);
    expect(row.activity).not.toContain("\x1b");
    expect(row.activity).not.toContain("ghp_c");
  });
});

describe("reducer: bus", () => {
  test("staggered parallel agents settle independently, each with its own elapsed", () => {
    const model = createModel();
    for (const id of ["a", "b", "c"]) progress(model, { id, agent: `agent-${id}`, call: "call-1", index: "abc".indexOf(id), durationMs: 0 }, T0);
    progress(model, { id: "a", agent: "agent-a", call: "call-1", index: 0, status: "completed", durationMs: 3000 }, T0 + 3 * SEC);
    // b and c are still running when a is already done
    const at = T0 + 10 * SEC;
    const rows = Object.fromEntries(visibleRuns(model, at).map((run) => [run.agent, run]));
    expect(rows["agent-a"].state).toBe("completed");
    expect(rows["agent-b"].state).toBe("running");
    expect(rows["agent-c"].state).toBe("running");
    const text = plain(model, 200, at).join("\n");
    expect(text).toContain("2 run");
    expect(text).toContain("1 done");
    // a is frozen at its own duration, b and c keep counting
    expect(text).toMatch(/agent-a\s+3s/);
    expect(text).toMatch(/agent-b\s+10s/);
    progress(model, { id: "b", agent: "agent-b", call: "call-1", index: 1, status: "failed", durationMs: 12000 }, T0 + 12 * SEC);
    const later = T0 + 30 * SEC;
    const after = Object.fromEntries(visibleRuns(model, later).map((run) => [run.agent, run]));
    expect(after["agent-b"].state).toBe("failed");
    expect(after["agent-c"].state).toBe("running");
    expect(plain(model, 200, later).join("\n")).toMatch(/agent-c\s+30s/);
  });

  test("lifecycle frames start a row at once (loading) and settle it", () => {
    const model = createModel();
    expect(ingestLifecycle(model, { id: "x", agent: "explore", status: "started", index: 0 }, T0)).toBe(true);
    expect(barState(model, T0)).toBe("loading");
    ingestLifecycle(model, { id: "x", agent: "explore", status: "completed", index: 0 }, T0 + 5 * SEC);
    expect(barState(model, T0 + 5 * SEC)).toBe("completed");
    expect(ingestLifecycle(model, { status: "bogus" }, T0)).toBe(false);
    expect(ingestProgress(model, { progress: { status: "weird" } }, T0)).toBe(false);
    expect(ingestProgress(model, null, T0)).toBe(false);
  });

  test("pending maps to queued, aborted to cancelled, retry state and failures show as activity", () => {
    const model = createModel();
    progress(model, { id: "q", status: "pending" }, T0);
    progress(model, { id: "c", status: "aborted" }, T0);
    ingestProgress(
      model,
      { index: 2, agent: "r", progress: { id: "r", index: 2, agent: "r", status: "running", durationMs: 0, toolCount: 0, retryState: { attempt: 2, maxAttempts: 5, delayMs: 100, errorMessage: "429", startedAtMs: 0 } } },
      T0,
    );
    ingestProgress(
      model,
      { index: 3, agent: "f", progress: { id: "f", index: 3, agent: "f", status: "running", durationMs: 0, toolCount: 0, retryFailure: { attempt: 5, errorMessage: "rate-limited" } } },
      T0,
    );
    const by = Object.fromEntries(visibleRuns(model, T0).map((run) => [run.key, run]));
    expect(by.q.state).toBe("queued");
    expect(by.c.state).toBe("cancelled");
    expect(by.r.activity).toBe("retry 2/5");
    expect(by.f.activity).toBe("blocked: rate-limited");
  });

  test("a settled id that starts again (a new wave reusing ids) is a fresh run", () => {
    const model = createModel();
    ingestLifecycle(model, { id: "0-Explore", agent: "explore", status: "started", index: 0 }, T0);
    progress(model, { id: "0-Explore", status: "completed", durationMs: 40_000 }, T0 + 40 * SEC);
    ingestLifecycle(model, { id: "0-Explore", agent: "explore", status: "started", index: 0 }, T0 + 50 * SEC);
    const [row] = visibleRuns(model, T0 + 51 * SEC);
    expect(row.state).toBe("starting");
    expect(plain(model, 200, T0 + 51 * SEC).join("\n")).toMatch(/explore\s+1s/);
  });

  test("a late running frame after completion does not resurrect the run", () => {
    const model = createModel();
    progress(model, { id: "z", durationMs: 0 }, T0);
    progress(model, { id: "z", status: "completed", durationMs: 20_000 }, T0 + 20 * SEC);
    progress(model, { id: "z", status: "running", durationMs: 19_900 }, T0 + 20 * SEC + 50);
    expect(visibleRuns(model, T0 + 21 * SEC)[0].state).toBe("completed");
  });
});

describe("reducer: sources", () => {
  test("the bus beats the task fallback: the same agent is one row, in the bus state", () => {
    const model = createModel();
    expect(ingestTaskSpawn(model, { tasks: [{ id: "be", agent: "a05-backend" }, { id: "fe", agent: "a06-frontend" }] }, "call-9", T0)).toBe(2);
    expect(visibleRuns(model, T0)).toHaveLength(2);
    expect(barState(model, T0)).toBe("loading");
    progress(model, { id: "be", agent: "a05-backend", call: "call-9", index: 0, tool: "edit", intent: "wire the thing", durationMs: 2000 }, T0 + 2 * SEC);
    progress(model, { id: "fe", agent: "a06-frontend", call: "call-9", index: 1, status: "pending" }, T0 + 2 * SEC);
    const rows = visibleRuns(model, T0 + 2 * SEC);
    expect(rows).toHaveLength(2);
    const be = rows.find((run) => run.agent === "a05-backend");
    expect(be?.state).toBe("running");
    expect(be?.activity).toBe("wire the thing");
    expect(rows.find((run) => run.agent === "a06-frontend")?.state).toBe("queued");
  });

  test("bus frames that name only the call and index find the fallback row", () => {
    const model = createModel();
    ingestTaskSpawn(model, { tasks: [{ agent: "a05-backend" }] }, "call-3", T0);
    progress(model, { id: "0-Backend", agent: "a05-backend", call: "call-3", index: 0, durationMs: 1000 }, T0 + SEC);
    expect(visibleRuns(model, T0 + SEC)).toHaveLength(1);
  });

  test("the task result settles rows nothing richer owns, never a detached or live-owned run", () => {
    const model = createModel();
    ingestTaskSpawn(model, { tasks: [{ id: "a", agent: "x" }, { id: "b", agent: "y" }, { id: "c", agent: "z" }] }, "call-1", T0);
    progress(model, { id: "b", agent: "y", call: "call-1", index: 1, detached: true, durationMs: 1000 }, T0 + SEC);
    ingestLive(model, live([{ run_id: "c.a1", task_id: "c", agent: "z", state: "running", elapsed_s: 1 }]), T0 + SEC);
    ingestTaskResult(model, "call-1", false, T0 + 2 * SEC);
    const by = Object.fromEntries(visibleRuns(model, T0 + 2 * SEC).map((run) => [run.agent, run.state]));
    expect(by).toEqual({ x: "completed", y: "running", z: "running" });
    ingestTaskSpawn(model, { tasks: [{ id: "d", agent: "w" }] }, "call-2", T0 + 3 * SEC);
    ingestTaskResult(model, "call-2", true, T0 + 4 * SEC);
    // an error for a row no subagent ever reported on: the call never ran (blocked, denied, aborted)
    expect(visibleRuns(model, T0 + 4 * SEC).find((run) => run.agent === "w")?.state).toBe("cancelled");
    ingestTaskResult(model, undefined, true, T0 + 4 * SEC);
  });

  test("a task item's `name` (omp's task id) aliases its row, so a bus run named that way is the same row", () => {
    const model = createModel();
    ingestTaskSpawn(model, { tasks: [{ name: "BackendWork", agent: "a05-backend" }] }, "call-n", T0);
    // the bus calls the run by the item's name and the agent something else: only the alias can join them
    progress(model, { id: "BackendWork", agent: "explore", durationMs: 1000 }, T0 + SEC);
    const rows = visibleRuns(model, T0 + SEC);
    expect(rows).toHaveLength(1);
    expect(rows[0].state).toBe("running");
  });

  test("a call that errors before any subagent reported is cancelled (it never ran); one the bus reported on fails", () => {
    const model = createModel();
    ingestTaskSpawn(model, { tasks: [{ name: "never", agent: "w" }, { name: "ran", agent: "v" }] }, "call-e", T0);
    progress(model, { id: "ran", agent: "v", durationMs: 1000 }, T0 + SEC);
    ingestTaskResult(model, "call-e", true, T0 + 2 * SEC);
    const by = Object.fromEntries(visibleRuns(model, T0 + 2 * SEC).map((run) => [run.agent, run.state]));
    expect(by).toEqual({ w: "cancelled", v: "failed" });
  });

  test("settleOrphanedSpawns cancels rows only the parent's own task call described, and nothing else", () => {
    const model = createModel();
    ingestTaskSpawn(model, { tasks: [{ name: "orphan", agent: "a05-backend" }, { name: "bus", agent: "a06-frontend" }, { name: "lv", agent: "a07-data" }] }, "call-o", T0);
    progress(model, { id: "bus", agent: "a06-frontend", durationMs: 1000 }, T0 + SEC);
    ingestLive(model, live([{ run_id: "lv.a1", task_id: "lv", agent: "a07-data", state: "running", elapsed_s: 1 }]), T0 + SEC);
    expect(settleOrphanedSpawns(model, T0 + 2 * SEC)).toBe(true);
    const by = Object.fromEntries(visibleRuns(model, T0 + 2 * SEC).map((run) => [run.agent, run.state]));
    expect(by).toEqual({ "a05-backend": "cancelled", "a06-frontend": "running", "a07-data": "running" });
    expect(settleOrphanedSpawns(model, T0 + 3 * SEC)).toBe(false);
    // the settled row leaves with its linger like any other
    expect(visibleRuns(model, T0 + 2 * SEC + 25 * SEC).map((run) => run.agent)).not.toContain("a05-backend");
  });

  test("a task input without ids or a tool call id still gets distinct rows", () => {
    const model = createModel();
    ingestTaskSpawn(model, { tasks: [{ agent: "a" }, { agent: "a" }] }, undefined, T0);
    ingestTaskSpawn(model, { tasks: [{ agent: "a" }] }, undefined, T0);
    expect(visibleRuns(model, T0)).toHaveLength(3);
    expect(ingestTaskSpawn(model, { nothing: true }, "c", T0)).toBe(0);
    expect(ingestTaskSpawn(model, { tasks: "no" }, "c", T0)).toBe(0);
  });

  test("live.json beats nothing: queued, waiting, stalled and handing-off runs appear with the slot limit", () => {
    const model = createModel();
    expect(
      ingestLive(
        model,
        live(
          [
            { run_id: "T1.a1", task_id: "T1", agent: "a05-backend", state: "running", elapsed_s: 72, activity: "edit swarm/x.py" },
            { run_id: "T2.a1", task_id: "T2", agent: "a06-frontend", state: "running", elapsed_s: 400, stalled: true, activity: "waiting on tool" },
            { run_id: "T3.a1", task_id: "T3", agent: "a07-data", state: "queued" },
            { run_id: "T4.a1", task_id: "T4", agent: "a08-qa", state: "waiting" },
            { run_id: "T5.a1", task_id: "T5", agent: "a09-reviewer", state: "handing_off", elapsed_s: 9 },
          ],
          { effective_parallel: 3, elapsed_s: 400.5 },
        ),
        T0,
      ),
    ).toBe(true);
    const lines = plain(model, 300, T0);
    expect(lines[0]).toContain("slots 3/3");
    expect(lines[0]).toContain("6m40s");
    const row = lines[1];
    for (const word of ["! STALL", "queued", "wait", "handoff", "a05-backend", "edit swarm/x.py"]) expect(row).toContain(word);
  });

  test("malformed documents are refused and unknown run states skipped", () => {
    const model = createModel();
    for (const bad of [null, [], {}, { schema: "swarm.live.v2", runs: [] }, { schema: "swarm.live.v1" }, "text"]) expect(ingestLive(model, bad, T0)).toBe(false);
    expect(ingestLive(model, live([{ run_id: "x", state: "exploding" }, { run_id: "y.a1", task_id: "y", agent: "ok", state: "running" }]), T0)).toBe(true);
    expect(visibleRuns(model, T0).map((run) => run.agent)).toEqual(["ok"]);
  });

  test("a retry (new run id for the same task) replaces the failed attempt; a vanished run loses its live part", () => {
    const model = createModel();
    ingestLive(model, live([{ run_id: "T1.a1", task_id: "T1", agent: "a05", state: "failed", elapsed_s: 30, error: "boom" }]), T0);
    ingestLive(model, live([{ run_id: "T1.a2", task_id: "T1", agent: "a05", state: "queued" }]), T0 + SEC);
    const rows = visibleRuns(model, T0 + SEC);
    expect(rows).toHaveLength(1);
    expect(rows[0].state).toBe("queued");
    ingestLive(model, live([]), T0 + 2 * SEC);
    expect(visibleRuns(model, T0 + 2 * SEC)).toHaveLength(0);
  });

  describe("several attempts of one task in one snapshot: the newest attempt wins", () => {
    const reworkDoc = (): Record<string, unknown> & { runs: Array<Record<string, unknown>> } =>
      JSON.parse(readFileSync(join(REPO_ROOT, "omp", "test", "fixtures", "live_rework_sample.json"), "utf8"));
    const byAgent = (model: SwarmBarModel, now: number) => Object.fromEntries(visibleRuns(model, now).map((run) => [run.agent, run.state]));

    test("real scheduler shape (retained earlier attempts, running rows before completed ones), both orders of appearance, counted once", () => {
      const forward = reworkDoc();
      const reversed = { ...reworkDoc(), runs: [...reworkDoc().runs].reverse() };
      for (const [order, doc] of [["producer order", forward], ["reversed", reversed]] as const) {
        const model = createModel();
        expect(ingestLive(model, doc, T0)).toBe(true);
        const rows = visibleRuns(model, T0);
        expect({ order, rows: rows.length }).toEqual({ order, rows: 3 });
        expect({ order, states: byAgent(model, T0) }).toEqual({ order, states: { "a05-backend": "running", "a06-frontend": "running", "a15-docs": "completed" } });
        expect(rows.find((run) => run.agent === "a06-frontend")?.parts.live?.runId).toBe("T-fe.a1.r1");
        expect(rows.find((run) => run.agent === "a05-backend")?.parts.live?.runId).toBe("T-be.a2");
        const [summary] = plain(model, 200, T0);
        expect(summary).toContain("2 run");
        expect(summary).toContain("1 done");
        expect(summary).toContain("0 fail");
      }
    });

    test("an older completed attempt never settles the row of its running rework, snapshot after snapshot", () => {
      const model = createModel();
      ingestLive(model, live([{ run_id: "T.a1", task_id: "T", agent: "a06", state: "running", elapsed_s: 5 }]), T0);
      // the rework starts: the file now lists the running rework FIRST and the completed original after it
      const doc = live([
        { run_id: "T.a1.r1", task_id: "T", agent: "a06", state: "running", elapsed_s: 6 },
        { run_id: "T.a1", task_id: "T", agent: "a06", state: "completed", elapsed_s: 5 },
      ]);
      for (const runs of [doc.runs, [...doc.runs].reverse()]) {
        ingestLive(model, { ...doc, runs }, T0 + SEC);
        expect(byAgent(model, T0 + SEC)).toEqual({ a06: "running" });
        expect(visibleRuns(model, T0 + SEC)).toHaveLength(1);
      }
      ingestLive(model, live([{ run_id: "T.a1.r1", task_id: "T", agent: "a06", state: "completed", elapsed_s: 9 }, { run_id: "T.a1", task_id: "T", agent: "a06", state: "completed", elapsed_s: 5 }]), T0 + 2 * SEC);
      expect(byAgent(model, T0 + 2 * SEC)).toEqual({ a06: "completed" });
    });

    test("attempt number outranks the rework index, the rework index outranks none, ties keep the earlier row", () => {
      const pick = (ids: string[]): string | undefined => {
        const model = createModel();
        ingestLive(model, live(ids.map((id) => ({ run_id: id, task_id: "T", agent: "a", state: "running" }))), T0);
        return visibleRuns(model, T0)[0]?.parts.live?.runId;
      };
      expect(pick(["T.a1.r3", "T.a2"])).toBe("T.a2");
      expect(pick(["T.a2", "T.a1.r3"])).toBe("T.a2");
      expect(pick(["T.a1", "T.a1.r2", "T.a1.r10"])).toBe("T.a1.r10");
      expect(pick(["T.a2.r1", "T.a2"])).toBe("T.a2.r1");
      expect(pick(["T.a1", "T.a1"])).toBe("T.a1");
    });

    test("distinct tasks never collapse, and rows without a task id stay per run id", () => {
      const model = createModel();
      ingestLive(model, live([{ run_id: "A.a1", task_id: "A", agent: "x", state: "running" }, { run_id: "B.a1", task_id: "B", agent: "y", state: "running" }, { run_id: "lone", agent: "z", state: "running" }]), T0);
      expect(byAgent(model, T0)).toEqual({ x: "running", y: "running", z: "running" });
    });
  });

  test("the same agent seen on the bus and in live.json is one row, with the live clock and stall flag", () => {
    const model = createModel();
    progress(model, { id: "weird-bus-id", agent: "a05-backend", durationMs: 5000 }, T0);
    ingestLive(model, live([{ run_id: "T7.a1", task_id: "T7", agent: "a05-backend", state: "running", elapsed_s: 60, stalled: true }]), T0 + SEC);
    const rows = visibleRuns(model, T0 + SEC);
    expect(rows).toHaveLength(1);
    expect(rows[0].stalled).toBe(true);
    expect(plain(model, 200, T0 + SEC).join("\n")).toMatch(/a05-backend\s+1m00s/);
    // fresh bus activity after the snapshot clears the stall
    progress(model, { id: "weird-bus-id", agent: "a05-backend", durationMs: 61_000, tool: "edit" }, T0 + 2 * SEC);
    expect(visibleRuns(model, T0 + 2 * SEC)[0].stalled).toBe(false);
  });

  test("a settled live run older than its linger at first read is never shown", () => {
    const model = createModel();
    ingestLive(model, live([{ run_id: "T1.a1", task_id: "T1", agent: "old-ok", state: "completed", elapsed_s: 9 }, { run_id: "T2.a1", task_id: "T2", agent: "old-bad", state: "failed", elapsed_s: 9 }]), T0, 60 * SEC);
    expect(visibleRuns(model, T0).map((run) => run.agent)).toEqual(["old-bad"]);
  });

  test("dropLive forgets only the live source", () => {
    const model = createModel();
    progress(model, { id: "b", agent: "bus-agent" }, T0);
    ingestLive(model, live([{ run_id: "T1.a1", task_id: "T1", agent: "live-agent", state: "running" }], { max_parallel: 8 }), T0);
    dropLive(model, T0);
    expect(visibleRuns(model, T0).map((run) => run.agent)).toEqual(["bus-agent"]);
    expect(model.slotLimit).toBeUndefined();
  });
});

describe("reducer: elapsed and clocks", () => {
  test("elapsed extrapolates with the consumer's monotonic clock and ignores a Date.now() jump", () => {
    const model = createModel();
    ingestLive(model, live([{ run_id: "T1.a1", task_id: "T1", agent: "a05", state: "running", elapsed_s: 10 }]), T0);
    const realNow = Date.now;
    try {
      Date.now = () => realNow() + 86_400_000; // the wall clock jumps a day forward
      expect(plain(model, 200, T0 + 5 * SEC).join("\n")).toMatch(/a05\s+15s/);
      Date.now = () => realNow() - 86_400_000 * 400; // and far back
      expect(plain(model, 200, T0 + 65 * SEC).join("\n")).toMatch(/a05\s+1m15s/);
    } finally {
      Date.now = realNow;
    }
  });

  test("a source with a smaller elapsed never makes the shown timer go backwards", () => {
    const model = createModel();
    progress(model, { id: "r", agent: "a", durationMs: 20_000 }, T0);
    ingestLive(model, live([{ run_id: "T1.a1", task_id: "T1", agent: "a", state: "running", elapsed_s: 5 }]), T0 + SEC);
    const [row] = visibleRuns(model, T0 + SEC);
    expect(plain(model, 200, T0 + SEC).join("\n")).toMatch(/a\s+21s/);
    expect(row.state).toBe("running");
  });

  test("elapsed freezes when the run settles", () => {
    const model = createModel();
    progress(model, { id: "r", agent: "a", durationMs: 0 }, T0);
    progress(model, { id: "r", agent: "a", status: "completed", durationMs: 8000 }, T0 + 8 * SEC);
    expect(plain(model, 200, T0 + 15 * SEC).join("\n")).toMatch(/a\s+8s/);
  });
});

describe("linger and waves", () => {
  test("completed rows leave after the linger, failed rows stay much longer", () => {
    const model = createModel();
    progress(model, { id: "ok", agent: "good", durationMs: 0 }, T0);
    progress(model, { id: "bad", agent: "bad", durationMs: 0 }, T0);
    progress(model, { id: "ok", agent: "good", status: "completed", durationMs: 1000 }, T0 + SEC);
    progress(model, { id: "bad", agent: "bad", status: "failed", durationMs: 1000 }, T0 + SEC);
    expect(visibleRuns(model, T0 + 20 * SEC).map((run) => run.agent)).toEqual(["bad", "good"]);
    expect(visibleRuns(model, T0 + 22 * SEC).map((run) => run.agent)).toEqual(["bad"]);
    expect(pruneExpired(model, T0 + 22 * SEC)).toBe(true);
    expect(nextExpiryMs(model, T0 + 22 * SEC)).toBe(600 * SEC - 21 * SEC);
    expect(pruneExpired(model, T0 + 22 * SEC)).toBe(false);
    expect(barState(model, T0 + 22 * SEC)).toBe("failed");
    expect(visibleRuns(model, T0 + 700 * SEC)).toHaveLength(0);
    expect(barState(model, T0 + 700 * SEC)).toBe("idle");
  });

  test("linger windows are configurable", () => {
    const model = createModel({ lingerMs: 2000, failLingerMs: 4000 });
    progress(model, { id: "ok", durationMs: 0 }, T0);
    progress(model, { id: "ok", status: "completed", durationMs: 100 }, T0);
    expect(visibleRuns(model, T0 + 1999)).toHaveLength(1);
    expect(visibleRuns(model, T0 + 2000)).toHaveLength(0);
  });

  test("the next wave clears the previous wave's rows, failed ones included", () => {
    const model = createModel();
    progress(model, { id: "bad", agent: "old-failure", durationMs: 0 }, T0);
    progress(model, { id: "bad", agent: "old-failure", status: "failed", durationMs: 1000 }, T0 + SEC);
    expect(barState(model, T0 + 30 * SEC)).toBe("failed");
    progress(model, { id: "new", agent: "next-wave", durationMs: 0 }, T0 + 30 * SEC);
    expect(visibleRuns(model, T0 + 31 * SEC).map((run) => run.agent)).toEqual(["next-wave"]);
    expect(barState(model, T0 + 31 * SEC)).toBe("active");
  });

  test("a live.json snapshot does not prune its own settled rows when it also starts a wave", () => {
    const model = createModel();
    ingestLive(model, live([{ run_id: "T1.a1", task_id: "T1", agent: "done-one", state: "completed", elapsed_s: 4 }, { run_id: "T2.a1", task_id: "T2", agent: "live-one", state: "running", elapsed_s: 1 }]), T0);
    expect(visibleRuns(model, T0).map((run) => run.agent).sort()).toEqual(["done-one", "live-one"]);
  });
});

describe("render: states at widths 20, 40, 80, 160", () => {
  function sample(): Record<string, SwarmBarModel> {
    const states: Record<string, SwarmBarModel> = {};
    states.idle = createModel();

    states.loading = createModel();
    ingestTaskSpawn(states.loading, { tasks: [{ id: "be", agent: "a05-backend" }] }, "c1", T0);

    states.active = createModel();
    progress(states.active, { id: "1", agent: "a05-backend", durationMs: 252_000, tool: "edit", intent: "wire the handover bridge" }, T0);
    progress(states.active, { id: "2", agent: "a06-frontend", index: 1, status: "pending" }, T0);
    ingestLive(states.active, live([{ run_id: "T9.a1", task_id: "T9", agent: "a07-data", state: "waiting" }], { effective_parallel: 4 }), T0);

    states.overflow = createModel();
    for (let i = 0; i < 9; i++) progress(states.overflow, { id: `r${i}`, agent: `agent-number-${i}`, index: i, durationMs: i * 1000, intent: "reading files" }, T0);
    progress(states.overflow, { id: "bad", agent: "a09-reviewer", index: 20, status: "failed", durationMs: 90_000 }, T0);

    states.completed = createModel();
    progress(states.completed, { id: "1", agent: "a05-backend", durationMs: 0 }, T0);
    progress(states.completed, { id: "1", agent: "a05-backend", status: "completed", durationMs: 61_000 }, T0 + 61 * SEC);

    states.failed = createModel();
    progress(states.failed, { id: "1", agent: "a05-backend", durationMs: 0 }, T0);
    progress(states.failed, { id: "1", agent: "a05-backend", status: "failed", durationMs: 3_700_000 }, T0 + 3700 * SEC);

    states.error = createModel();
    states.error.error = "live.json malformed";
    return states;
  }
  const at: Record<string, number> = { completed: T0 + 65 * SEC, failed: T0 + 3720 * SEC };

  test("each state reports its own name", () => {
    const states = sample();
    const got = Object.fromEntries(Object.entries(states).map(([name, model]) => [name, renderSwarmBarFrame(model, 160, undefined, at[name] ?? T0).state]));
    expect(got).toEqual({ idle: "idle", loading: "loading", active: "active", overflow: "overflow", completed: "completed", failed: "failed", error: "error" });
    expect(renderSwarmBarFrame(states.overflow, 40, undefined, T0).state).toBe("overflow");
  });

  test("at 160 columns every state draws its two rows: the summary, then the agents or the placeholder", () => {
    const rows = Object.fromEntries(Object.entries(sample()).map(([name, model]) => [name, plain(model, 160, at[name] ?? T0).map(visible)]));
    expect(rows).toEqual({
      idle: ["○ swarm idle", "no subagents running"],
      loading: ["● swarm 0s │ 1 run │ 0 queued │ 0 done │ 0 fail", "● start a05-backend     0s"],
      active: [
        "● swarm 4m12s │ 1 run │ 1 wait │ 1 queued │ 0 done │ 0 fail │ slots 1/4",
        "● a05-backend  4m12s wire the handover bridge │ ◌ wait a07-data     0s │ ○ queued a06-frontend     0s",
      ],
      overflow: [
        "● swarm 1m30s │ 9 run │ 0 queued │ 0 done │ 1 fail",
        "✕ FAIL a09-reviewer  1m30s │ ● agent-number-8     8s │ ● agent-number-7     7s │ ● agent-number-6     6s │ ● agent-number-5     5s │ +5 more",
      ],
      completed: ["✓ swarm 1m01s │ 0 run │ 0 queued │ 1 done │ 0 fail", "✓ a05-backend  1m01s"],
      failed: ["✕ swarm 1h01m │ 0 run │ 0 queued │ 0 done │ 1 fail", "✕ FAIL a05-backend  1h01m"],
      error: ["swarm: telemetry unavailable", "no subagents running"],
    });
  });

  test("narrower bars still lead with the identity and the worst row", () => {
    const heads: Record<string, string> = {
      idle: "○ swarm idle",
      loading: "● swarm 0s",
      active: "● swarm 4m12s",
      overflow: "● swarm 1m30s",
      completed: "✓ swarm 1m01s",
      failed: "✕ swarm 1h01m",
      error: "swarm: telemetry unavailable",
    };
    const firstRows: Record<string, string> = {
      idle: "no subagents running",
      loading: "● start a05-backend",
      active: "● a05-backend",
      overflow: "✕ FAIL a09-reviewer",
      completed: "✓ a05-backend",
      failed: "✕ FAIL a05-backend",
      error: "no subagents running",
    };
    for (const width of [40, 80, 160]) {
      for (const [name, model] of Object.entries(sample())) {
        const [row1, row2] = plain(model, width, at[name] ?? T0).map(visible);
        expect(row1.startsWith(heads[name]), `${name}@${width} row 1: ${row1}`).toBe(true);
        expect(row2.startsWith(firstRows[name]), `${name}@${width} row 2: ${row2}`).toBe(true);
      }
    }
  });

  test("every state, theme and width 1..300: exactly 2 rows of exactly the width and no cut escape, with and without token chips (I1, I2, I5)", () => {
    for (const [themeName, theme] of themeVariants) {
      for (const [name, base] of Object.entries(sample())) {
        for (const tokens of [undefined, FULL_SAVINGS]) {
          const model: SwarmBarModel = tokens ? { ...base, tokens } : base;
          for (let width = 1; width <= 300; width++) {
            const lines = renderSwarmBar(model, width, theme, at[name] ?? T0);
            if (lines.length !== 2 || lines.some((line) => displayWidth(line) !== width || hasStrayEscape(line))) {
              throw new Error(`${name}@${width} (${themeName}, ${tokens ? "tokens" : "no tokens"}): ${JSON.stringify(lines)}`);
            }
          }
        }
      }
    }
  }, 30_000);

  test("a powerline row starts with the open cap and ends with the close cap in every state from 12 columns; a transparent bar has no caps; under 12 columns rows are plain (I3)", () => {
    const { powerlineCapLeft: open, powerlineLeft: close, powerlineThinLeft: thin } = powerlineTheme.sep!;
    for (const [name, model] of Object.entries(sample())) {
      for (let width = 1; width <= 300; width++) {
        for (const row of renderSwarmBar(model, width, powerlineTheme, at[name] ?? T0).map(stripAnsi)) {
          const capped = row.startsWith(open) && row.endsWith(close);
          if (capped !== width >= 12 || (width < 12 && row.includes(thin))) throw new Error(`${name}@${width} (powerline): ${JSON.stringify(row)}`);
        }
        for (const row of renderSwarmBar(model, width, transparentTheme, at[name] ?? T0).map(stripAnsi)) {
          if (row.includes(open) || row.includes(close)) throw new Error(`${name}@${width} (transparent): ${JSON.stringify(row)}`);
        }
      }
    }
  });

  test("wide CJK and emoji agent text clips by display columns, never by code units: still exactly the width", () => {
    const model = createModel();
    progress(model, { id: "w", agent: "日本語エージェント名前長い", intent: "👨‍👩‍👧 読み込み中のファイル 🚀 とても長い説明テキスト".repeat(3) }, T0);
    for (let width = 1; width <= 120; width++) {
      for (const [themeName, theme] of themeVariants) {
        const lines = renderSwarmBar(model, width, theme, T0);
        if (lines.length !== 2 || lines.some((line) => displayWidth(line) !== width)) throw new Error(`cjk@${width} (${themeName}): ${JSON.stringify(lines)}`);
      }
    }
  });

  test("a width of 0 or less renders no rows in any state", () => {
    for (const [name, model] of Object.entries(sample())) {
      for (const width of [0, -1, -80]) {
        expect(renderSwarmBar(model, width, colorTheme, at[name] ?? T0), `${name}@${width}`).toEqual([]);
        expect(renderSwarmBarFrame(model, width, colorTheme, at[name] ?? T0).lines, `${name}@${width} frame`).toEqual([]);
      }
    }
  });

  test("idle keeps both rows: the idle marker and the placeholder, with the token chips when a snapshot exists", () => {
    const idle = sample().idle;
    for (const width of [40, 160]) {
      const frame = renderSwarmBarFrame(idle, width, undefined, T0);
      expect(frame, `idle@${width}`).toMatchObject({ state: "idle", shown: 0, hidden: 0 });
      expect(frame.lines.map(visible)).toEqual(["○ swarm idle", "no subagents running"]);
    }
    const measured: SwarmBarModel = { ...idle, tokens: savings() };
    const [row1, row2] = plain(measured, 160, T0);
    expect(visible(row1)).toMatch(/^○ swarm idle +before 10k · now 4k │ saved 6k$/);
    expect(visible(row2)).toBe("no subagents running");
    const [themed1, themed2] = renderSwarmBar(measured, 160, powerlineTheme, T0);
    expect(stripAnsi(themed1)).toContain("saved 6k");
    expect(stripAnsi(themed2)).toContain("no subagents running");
  });

  test("a telemetry error without rows keeps both rows: the warning, the idle placeholder, and the chips when a snapshot exists", () => {
    const error = sample().error;
    for (const width of [40, 160]) {
      const frame = renderSwarmBarFrame(error, width, undefined, T0);
      expect(frame, `error@${width}`).toMatchObject({ state: "error", shown: 0, hidden: 0 });
      expect(frame.lines.map(visible)).toEqual(["swarm: telemetry unavailable", "no subagents running"]);
    }
    const measured: SwarmBarModel = { ...error, tokens: savings() };
    const [row1, row2] = plain(measured, 160, T0);
    expect(visible(row1)).toMatch(/^swarm: telemetry unavailable +before 10k · now 4k │ saved 6k$/);
    expect(visible(row2)).toBe("no subagents running");
    // narrower: the warning is the identity of the row and the placeholder row is unchanged
    expect(visible(plain(measured, 40, T0)[0])).toMatch(/^swarm: telemetry unavailable/);
    expect(visible(plain(measured, 40, T0)[1])).toBe("no subagents running");
  });

  test("rows linger, then the bar goes back to the idle placeholder instead of disappearing", () => {
    const model = createModel();
    progress(model, { id: "ok", agent: "good", durationMs: 0 }, T0);
    progress(model, { id: "ok", agent: "good", status: "completed", durationMs: 1000 }, T0 + SEC);
    const lingering = renderSwarmBarFrame(model, 80, undefined, T0 + 5 * SEC);
    expect(lingering.state).toBe("completed");
    expect(visible(lingering.lines[1])).toContain("good");
    const after = renderSwarmBarFrame(model, 80, undefined, T0 + 22 * SEC);
    expect(after.state).toBe("idle");
    expect(after.lines.map(visible)).toEqual(["○ swarm idle", "no subagents running"]);
  });

  test("telemetry marker: alone when there are no rows, as a segment next to rows otherwise", () => {
    const model = createModel();
    model.error = "live.json stale";
    const alone = renderSwarmBar(model, 80, undefined, T0);
    expect(alone).toHaveLength(2);
    expect(visible(alone[0])).toBe("swarm: telemetry unavailable");
    progress(model, { id: "r", agent: "a05" }, T0);
    const withRows = renderSwarmBar(model, 160, undefined, T0);
    expect(withRows).toHaveLength(2);
    expect(withRows[0]).toContain("telemetry unavailable");
    expect(withRows[1]).toContain("a05");
  });
});

describe("render: overflow ordering and labels", () => {
  function mixed(): SwarmBarModel {
    const model = createModel();
    progress(model, { id: "q", agent: "k-queued", status: "pending", index: 0 }, T0);
    progress(model, { id: "r", agent: "j-running", index: 1, durationMs: 5000 }, T0);
    ingestLive(
      model,
      live([
        { run_id: "T1.a1", task_id: "T1", agent: "h-stalled", state: "running", elapsed_s: 300, stalled: true },
        { run_id: "T2.a1", task_id: "T2", agent: "i-waiting", state: "waiting" },
        { run_id: "T3.a1", task_id: "T3", agent: "g-timeout", state: "timed_out", elapsed_s: 600, error: "deadline" },
        { run_id: "T4.a1", task_id: "T4", agent: "f-failed", state: "failed", elapsed_s: 12, error: "boom" },
        { run_id: "T5.a1", task_id: "T5", agent: "l-done", state: "completed", elapsed_s: 30 },
        { run_id: "T6.a1", task_id: "T6", agent: "m-cancelled", state: "cancelled", elapsed_s: 3 },
      ]),
      T0,
    );
    return model;
  }

  test("rows are ordered failed, timed-out, stalled, running, waiting, queued, then settled", () => {
    const order = visibleRuns(mixed(), T0).map((run) => run.agent);
    expect(order).toEqual(["f-failed", "g-timeout", "h-stalled", "j-running", "i-waiting", "k-queued", "m-cancelled", "l-done"]);
    const line = plain(mixed(), 400, T0)[1];
    const positions = order.map((agent) => line.indexOf(agent));
    expect(positions.every((p) => p >= 0)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
  });

  test("every state carries a glyph and, where not self-evident, a word", () => {
    const line = plain(mixed(), 400, T0)[1];
    for (const label of ["✕ FAIL", "⏱ TIMEOUT", "! STALL", "◌ wait", "○ queued", "⊘ cancelled", "✓"]) expect(line).toContain(label);
    const handoff = createModel();
    ingestLive(handoff, live([{ run_id: "T1.a1", task_id: "T1", agent: "h", state: "handing_off", elapsed_s: 1 }]), T0);
    expect(plain(handoff, 200, T0)[1]).toContain("⇄ handoff");
    expect(plain(handoff, 200, T0)[0]).toContain("1 run");
    const starting = createModel();
    ingestLifecycle(starting, { id: "s", agent: "s", status: "started", index: 0 }, T0);
    expect(plain(starting, 200, T0)[1]).toContain("● start");
  });

  test("a finished swarm with cancelled rows and no failure is marked cancelled, not with a green check", () => {
    const model = createModel();
    ingestLive(
      model,
      live([
        { run_id: "T1.a1", task_id: "T1", agent: "a-done", state: "completed", elapsed_s: 30 },
        { run_id: "T2.a1", task_id: "T2", agent: "b-cancelled", state: "cancelled", elapsed_s: 3 },
      ]),
      T0,
    );
    const head = plain(model, 160, T0)[0];
    expect(head.startsWith("⊘ swarm")).toBe(true);
    expect(head).toContain("1 done");
    expect(head).toContain("1 cancel");
    const clean = createModel();
    ingestLive(clean, live([{ run_id: "T1.a1", task_id: "T1", agent: "a-done", state: "completed", elapsed_s: 30 }]), T0);
    expect(plain(clean, 160, T0)[0].startsWith("✓ swarm")).toBe(true);
    const failing = createModel();
    ingestLive(
      failing,
      live([
        { run_id: "T1.a1", task_id: "T1", agent: "a-fail", state: "failed", elapsed_s: 5, error: "boom" },
        { run_id: "T2.a1", task_id: "T2", agent: "b-cancelled", state: "cancelled", elapsed_s: 3 },
      ]),
      T0,
    );
    expect(plain(failing, 160, T0)[0].startsWith("✕ swarm")).toBe(true);  // a failure outranks a cancel
  });

  test("when rows do not fit the worst states stay and the rest become +N more", () => {
    const lines = plain(mixed(), 60, T0);
    expect(lines[1]).toContain("f-failed");
    expect(lines[1].trimEnd()).toMatch(/\+\d+ more$/);
    expect(lines[1]).not.toContain("l-done");
    const frame: BarFrame = renderSwarmBarFrame(mixed(), 60, undefined, T0);
    expect(frame.state).toBe("overflow");
    expect(frame.shown + frame.hidden).toBe(8);
    const count = Number(/\+(\d+) more/.exec(frame.lines[1])?.[1]);
    expect(count).toBe(frame.hidden);
  });

  test("when not even one row fits, `+N more` goes first and the row is clipped with an ellipsis, still exactly the width", () => {
    const frame = renderSwarmBarFrame(mixed(), 14, undefined, T0);
    expect(frame.lines).toHaveLength(2);
    expect(frame.lines.map((line) => displayWidth(line))).toEqual([14, 14]);
    expect(frame.lines[1]).not.toContain("more");
    expect(frame.lines[1].startsWith("✕ FAIL")).toBe(true);
    expect(frame.lines[1].endsWith("…")).toBe(true);
    expect(frame.shown).toBe(1);
  });

  test("the summary counts every state", () => {
    const [line1] = plain(mixed(), 400, T0);
    for (const part of ["2 run", "1 wait", "1 queued", "1 done", "1 fail", "1 timeout", "1 stall", "1 cancel"]) expect(line1).toContain(part);
  });

  test("timers are fixed width: the elapsed column does not move between 9s and 1m05s", () => {
    const model = createModel();
    progress(model, { id: "a", agent: "agent", durationMs: 9000, intent: "x" }, T0);
    const early = plain(model, 200, T0)[1];
    const later = plain(model, 200, T0 + 56 * SEC)[1];
    expect(early.indexOf(" x")).toBe(later.indexOf(" x"));
  });

  test("the spinner advances once a second and freezes under reduced motion", () => {
    const moving = createModel();
    const frozen = createModel({ reducedMotion: true });
    for (const model of [moving, frozen]) progress(model, { id: "a", agent: "agent" }, T0);
    const a = renderSwarmBar(moving, 120, colorTheme, 5 * SEC);
    const b = renderSwarmBar(moving, 120, colorTheme, 6 * SEC);
    expect(stripAnsi(a[0])).not.toBe(stripAnsi(b[0]));
    const f1 = renderSwarmBar(frozen, 120, colorTheme, 5 * SEC);
    const f2 = renderSwarmBar(frozen, 120, colorTheme, 6 * SEC);
    expect(stripAnsi(f1[1]).replace(/\d+s/, "")).toBe(stripAnsi(f2[1]).replace(/\d+s/, ""));
    expect(stripAnsi(f1[0])).toContain("●");
  });

  test("a theme that throws or lacks colors still renders plain text", () => {
    const throwing: BarTheme = {
      fg: () => {
        throw new Error("boom");
      },
      bold: () => {
        throw new Error("boom");
      },
      getSpinnerFrames: () => {
        throw new Error("boom");
      },
    };
    const lines = renderSwarmBar(mixed(), 200, throwing, T0);
    expect(lines).toHaveLength(2);
    expect(lines[1]).toContain("f-failed");
    for (const line of lines) expect(displayWidth(line)).toBe(200);
  });

  describe("row 1: priorities and token chips", () => {
    /** The mixed model with a telemetry error: every left segment the summary can have. */
    function flooded(): SwarmBarModel {
      const model = mixed();
      model.error = "live.json stale";
      return model;
    }

    test("summary segments leave in priority order as the row narrows: identity last, then run, failures and telemetry, queued, wait and done, cancel, slots", () => {
      const model = flooded();
      const render = (width: number) => plain(model, width, T0)[0];
      expectDropOrder("summary", render, MIXED_PIECES);
      expectAppearOrder("summary", render, [
        "● swarm 10m00s",
        "2 run",
        "1 fail",
        "1 timeout",
        "1 stall",
        "telemetry unavailable",
        "1 queued",
        "1 wait",
        "1 done",
        "1 cancel",
        "slots 2/4",
      ]);
    });

    test("the identity is the last thing standing, clipped with an ellipsis when it alone is too wide", () => {
      const model = flooded();
      expect(visible(plain(model, 14, T0)[0])).toBe("● swarm 10m00s");
      const clipped = plain(model, 8, T0)[0];
      expect(clipped.startsWith("● swarm")).toBe(true);
      expect(clipped.endsWith("…")).toBe(true);
      expect(displayWidth(clipped)).toBe(8);
    });

    test("with token chips on the right the same priorities rule both groups together", () => {
      const model: SwarmBarModel = { ...flooded(), tokens: FULL_SAVINGS };
      expectDropOrder("summary+chips", (width) => plain(model, width, T0)[0], [...MIXED_PIECES, ...CHIP_PIECES]);
    });

    test("no snapshot, no chips: nothing is invented, not even zeros", () => {
      const running = createModel();
      progress(running, { id: "a", agent: "a05" }, T0);
      for (const model of [createModel(), running, flooded()]) {
        for (const width of [60, 160, 300]) {
          expect(plain(model, width, T0)[0], `width ${width}`).not.toMatch(/\b(saved|before|cache|ttft|bill|payload|rtk|cost|query|turn|prefix|tools|graph)\b/);
        }
      }
      expect(visible(plain(createModel(), 160, T0)[0])).toBe("○ swarm idle");
    });

    test("a minimal snapshot shows what was measured and nothing else; a measured zero is shown as a zero", () => {
      const idle = (tokens: TokenSavingsSnapshot): SwarmBarModel => ({ ...createModel(), tokens });
      const chips = (model: SwarmBarModel) => visible(plain(model, 160, T0)[0]).replace(/^○ swarm idle\s+/, "");
      expect(chips(idle(savings()))).toBe("before 10k · now 4k │ saved 6k");
      expect(chips(idle(savings({ currentTokens: 10_000, savedTokens: 0 })))).toBe("before 10k · now 10k │ saved 0");
      // each half of the provider bill appears on its own
      expect(chips(idle(savings({ providerInput: 90_000 })))).toBe("bill 90k │ before 10k · now 4k │ saved 6k");
      expect(chips(idle(savings({ providerOutput: 50_000 })))).toBe("out 50k │ before 10k · now 4k │ saved 6k");
    });

    test("saved is the right-most chip and the right edge never moves with what the summary says", () => {
      const running = createModel();
      progress(running, { id: "a", agent: "a05", intent: "x".repeat(60) }, T0);
      const error = createModel();
      error.error = "live.json stale";
      const models: Record<string, SwarmBarModel> = { idle: createModel(), running, mixed: mixed(), error };
      const { powerlineLeft: close } = powerlineTheme.sep!;
      const column = (row: string): number => {
        const text = stripAnsi(row);
        expect(text, "saved is on screen").toContain("saved 6k");
        return displayWidth(text.slice(0, text.lastIndexOf("saved 6k")));
      };
      for (const width of [60, 80, 160, 300]) {
        for (const [name, model] of Object.entries(models)) {
          const measured: SwarmBarModel = { ...model, tokens: savings() };
          const [plainRow] = plain(measured, width, T0);
          expect(plainRow.endsWith("saved 6k"), `${name}@${width} plain: ${JSON.stringify(plainRow)}`).toBe(true);
          expect(column(plainRow), `${name}@${width} plain`).toBe(width - "saved 6k".length);
          const [capped] = renderSwarmBar(measured, width, powerlineTheme, T0);
          expect(stripAnsi(capped).endsWith(`saved 6k ${close}`), `${name}@${width} powerline: ${JSON.stringify(stripAnsi(capped))}`).toBe(true);
          expect(column(capped), `${name}@${width} powerline`).toBe(width - displayWidth(`saved 6k ${close}`));
        }
      }
    });

    test("chips leave in priority order as the row narrows, and saved survives longest", () => {
      const model: SwarmBarModel = { ...createModel(), tokens: FULL_SAVINGS };
      const render = (width: number) => plain(model, width, T0)[0];
      expectDropOrder("chips", render, [["○ swarm idle", 0], ...CHIP_PIECES]);
      expectAppearOrder("chips", render, [
        "saved 6k",
        "cache 62%",
        "prefix moved",
        "tools moved",
        "ttft 310ms",
        "before 10k · now 4k",
        "turn 900ms",
        "cost 12c",
        "rtk +1.5k",
        "query 40ms",
        "payload 156523c",
        "idle 1200ms",
        "bill 90k · out 50k",
        "graph ready",
      ]);
    });

    test("the chips are shown in every state a row can be in", () => {
      const running = createModel();
      progress(running, { id: "a", agent: "a05" }, T0);
      const error = createModel();
      error.error = "live.json stale";
      const failed = createModel();
      progress(failed, { id: "f", agent: "bad", status: "failed", durationMs: 1000 }, T0);
      for (const [name, model] of Object.entries({ idle: createModel(), running, error, failed, mixed: mixed() })) {
        const [row1, ...below] = plain({ ...model, tokens: FULL_SAVINGS }, 300, T0);
        expect(row1, name).toContain("cache 62%");
        expect(row1, name).toContain("saved 6k");
        expect(below.join("\n"), `${name} rows below the first carry no chips`).not.toContain("saved");
      }
    });

    test("provider bill, cache and cost values are never folded into saved", () => {
      const model: SwarmBarModel = { ...createModel(), tokens: FULL_SAVINGS };
      const row = plain(model, 300, T0)[0];
      expect(row.endsWith("saved 6k")).toBe(true); // baseline 10k - current 4k, nothing else
      expect(row).toContain("bill 90k · out 50k");
      expect(row).toContain("cache 62%");
      for (const leaked of ["77.7k", "33.3k", "saved 90k", "saved 140k"]) expect(row).not.toContain(leaked);
      // the provider numbers do not move saved either
      const other = savings({ providerInput: 1, providerOutput: 2, costCents: 3, cacheHitPct: 4 });
      expect(plain({ ...createModel(), tokens: other }, 300, T0)[0].endsWith("saved 6k")).toBe(true);
    });

    test("shownTokens: this session's snapshot, or any valid snapshot before the session id is known, never another session's", () => {
      const model = createModel();
      expect(shownTokens(model)).toBeUndefined();
      model.tokens = savings({ sessionId: "other" });
      expect(shownTokens(model)).toBe(model.tokens);
      expect(plain(model, 160, T0)[0]).toContain("saved 6k");
      model.sessionId = "mine";
      expect(shownTokens(model)).toBeUndefined();
      expect(plain(model, 160, T0)[0]).not.toContain("saved");
      expect(visible(plain(model, 160, T0)[0])).toBe("○ swarm idle");
      model.tokens = savings({ sessionId: "mine" });
      expect(shownTokens(model)).toBe(model.tokens);
      expect(plain(model, 160, T0)[0]).toContain("saved 6k");
      model.tokens = undefined;
      expect(shownTokens(model)).toBeUndefined();
    });

    test("chip colours: saved is green when it is a saving and dim at zero, a moved prefix is a warning", () => {
      const row = (tokens: TokenSavingsSnapshot) => renderSwarmBar({ ...createModel(), tokens }, 200, colorTheme, T0)[0];
      expect(row(savings())).toContain("\x1b[32msaved 6k\x1b[39m");
      expect(row(savings({ currentTokens: 10_000, savedTokens: 0 }))).toContain("\x1b[90msaved 0\x1b[39m");
      expect(row(savings({ prefixChanged: 1 }))).toContain("\x1b[33mprefix moved\x1b[39m");
      expect(row(savings())).not.toContain("prefix moved");
    });
  });
});

describe("header counts", () => {
  test("counts of live and failing work, and nothing once the failure ages out", () => {
    const model = createModel();
    progress(model, { id: "a", agent: "a" }, T0);
    progress(model, { id: "q", agent: "q", status: "pending", index: 1 }, T0);
    const live = renderSwarmBar(model, 200, undefined, T0)[0];
    expect(live).toContain("1 run");
    expect(live).toContain("1 queued");
    progress(model, { id: "a", agent: "a", status: "failed", durationMs: 1000 }, T0 + SEC);
    progress(model, { id: "q", agent: "q", status: "completed", durationMs: 1000, index: 1 }, T0 + SEC);
    const failed = renderSwarmBar(model, 200, undefined, T0 + SEC)[0];
    expect(failed).toContain("1 fail");
    expect(failed).toContain("0 run");
    expect(renderSwarmBar(model, 200, undefined, T0 + 700 * SEC)[0]).toContain("○ swarm idle");
  });
});

// ---------------------------------------------------------------------------------------------------------------------
// In-session specialists: rows read from the parent's `progress.inflightTaskDetails` (the A01 -> a02..a30 flow)
// ---------------------------------------------------------------------------------------------------------------------

/** One `AgentProgress` entry of `TaskToolDetails.progress`, shaped like omp's executor (pi-tui tools/task.ts). */
function nestedEntry(
  id: string,
  agent: string,
  status: string,
  durationMs: number,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    index: 0,
    id,
    agent,
    agentSource: "project",
    status,
    task: `assignment for ${id}`,
    assignment: `assignment for ${id}`,
    description: `lane ${id}`,
    recentTools: [],
    recentOutput: [],
    toolCount: 2,
    requests: 1,
    tokens: 500,
    cost: 0,
    durationMs,
    ...extra,
  };
}

/** A settled `SingleResult` of `TaskToolDetails.results`. */
function nestedResult(id: string, agent: string, durationMs: number, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    index: 0,
    id,
    agent,
    agentSource: "project",
    task: `assignment for ${id}`,
    exitCode: 0,
    output: "done",
    stderr: "",
    truncated: false,
    durationMs,
    tokens: 900,
    requests: 3,
    ...extra,
  };
}

function taskDetails(progressEntries: unknown, results: unknown = []): Record<string, unknown> {
  return { projectAgentsDir: null, results, totalDurationMs: 0, progress: progressEntries };
}

const A01_ID = "Orchestrate";

/** The `task:subagent:progress` frame of A01 (the main session's `task` call, `name`-keyed) carrying its in-flight `task` details. */
function a01(model: SwarmBarModel, details: unknown, now: number, extra: Record<string, unknown> = {}, status = "running"): boolean {
  return ingestProgress(
    model,
    {
      index: 0,
      agent: "a01-orchestrator",
      task: "run the plan",
      parentToolCallId: "call-A01",
      assignment: "run the plan",
      progress: {
        ...nestedEntry(A01_ID, "a01-orchestrator", status, 0, { currentTool: "task", toolCount: 1 }),
        inflightTaskDetails: details,
      },
    },
    now,
  );
}

function a01Spawned(): SwarmBarModel {
  const model = createModel({ reducedMotion: true });
  // the main session's own `task` call: `name`-keyed items, the id the bus then reports
  ingestTaskSpawn(model, { tasks: [{ name: A01_ID, agent: "a01-orchestrator", assignment: "run the plan" }] }, "call-A01", T0);
  return model;
}

const rowsOf = (model: SwarmBarModel, now: number) => visibleRuns(model, now).map((run) => `${run.agent}:${run.state}`);
const specialists = (model: SwarmBarModel, now: number) => visibleRuns(model, now).filter((run) => run.parentKey !== undefined);
const elapsedOf = (model: SwarmBarModel, agent: string, now: number): number => {
  const run = visibleRuns(model, now).find((row) => row.agent === agent);
  return run ? Math.floor(runElapsedS(run, now)) : -1;
};

describe("in-session specialists", () => {
  test("an A01 frame with 3 running specialists and 1 settled result renders them as 4 rows under the A01 row, each with its own timer", () => {
    const model = a01Spawned();
    const t = T0 + 5 * SEC;
    a01(
      model,
      taskDetails(
        [
          nestedEntry("be", "a05-backend", "running", 120_000, { index: 0, currentTool: "edit", currentToolArgs: "swarm/x.py" }),
          nestedEntry("fe", "a06-frontend", "running", 45_000, { index: 1, currentToolIntent: "Reading the tokens" }),
          nestedEntry("qa", "a08-qa", "running", 8_000, { index: 2, lastIntent: "Planning tests" }),
        ],
        [nestedResult("docs", "a15-docs", 30_000, { index: 3 })],
      ),
      t,
      { },
    );
    expect(specialists(model, t)).toHaveLength(4);
    expect(visibleRuns(model, t)).toHaveLength(5); // the A01 row stays: it is a running agent too
    expect(rowsOf(model, t)).toEqual([
      "a05-backend:running",
      "a06-frontend:running",
      "a08-qa:running",
      "a01-orchestrator:running",
      "a15-docs:completed",
    ]);
    // independent timers: first sight minus the producer duration, then the consumer's own clock
    expect(elapsedOf(model, "a05-backend", t)).toBe(120);
    expect(elapsedOf(model, "a06-frontend", t)).toBe(45);
    expect(elapsedOf(model, "a08-qa", t)).toBe(8);
    const later = t + 10 * SEC;
    expect(elapsedOf(model, "a05-backend", later)).toBe(130);
    expect(elapsedOf(model, "a06-frontend", later)).toBe(55);
    expect(elapsedOf(model, "a08-qa", later)).toBe(18);
    expect(elapsedOf(model, "a15-docs", later)).toBe(30);
    const frame = renderSwarmBarFrame(model, 160, undefined, later);
    expect(frame.lines[0]).toContain("4 run"); // A01 + 3 specialists
    expect(frame.lines[0]).toContain("1 done");
    expect(frame.lines[1]).toContain("a05-backend");
    expect(frame.lines[1]).toContain("edit swarm/");
  });

  test("specialists complete one after another across frames; a settled row keeps its final duration", () => {
    const model = a01Spawned();
    const entries = (be: string, fe: string, qa: string, beMs: number, feMs: number, qaMs: number) => [
      nestedEntry("be", "a05-backend", be, beMs, { index: 0 }),
      nestedEntry("fe", "a06-frontend", fe, feMs, { index: 1 }),
      nestedEntry("qa", "a08-qa", qa, qaMs, { index: 2 }),
    ];
    a01(model, taskDetails(entries("running", "running", "running", 1000, 1000, 1000)), T0 + SEC);
    expect(rowsOf(model, T0 + SEC).filter((row) => row.endsWith(":running"))).toHaveLength(4);
    a01(model, taskDetails(entries("running", "completed", "running", 21_000, 20_500, 21_000)), T0 + 21 * SEC);
    expect(rowsOf(model, T0 + 21 * SEC)).toContain("a06-frontend:completed");
    expect(elapsedOf(model, "a06-frontend", T0 + 30 * SEC)).toBe(20);
    a01(model, taskDetails(entries("completed", "completed", "failed", 40_000, 20_500, 38_000)), T0 + 40 * SEC);
    const settled = Object.fromEntries(specialists(model, T0 + 40 * SEC).map((run) => [run.agent, run.state]));
    expect(settled).toEqual({ "a05-backend": "completed", "a06-frontend": "completed", "a08-qa": "failed" });
    const frame = renderSwarmBarFrame(model, 160, undefined, T0 + 40 * SEC);
    expect(frame.lines[0]).toMatch(/1 run .*2 done .*1 fail/);
    // a stale running frame for a settled specialist is ignored
    a01(model, taskDetails(entries("running", "completed", "failed", 41_000, 20_500, 38_000)), T0 + 41 * SEC);
    expect(visibleRuns(model, T0 + 40 * SEC).find((run) => run.agent === "a05-backend")?.state).toBe("completed");
  });

  test("several lanes of the same agent stay distinct rows with their own timers and activity", () => {
    const model = a01Spawned();
    const t = T0 + SEC;
    a01(
      model,
      taskDetails([
        nestedEntry("be-1", "a05-backend", "running", 90_000, { index: 0, currentTool: "edit", currentToolArgs: "a.py" }),
        nestedEntry("be-2", "a05-backend", "running", 30_000, { index: 1, currentTool: "edit", currentToolArgs: "b.py" }),
        nestedEntry("be-3", "a05-backend", "pending", 0, { index: 2 }),
      ]),
      t,
    );
    const lanes = specialists(model, t);
    expect(lanes).toHaveLength(3);
    expect(new Set(lanes.map((run) => run.key)).size).toBe(3);
    expect(lanes.map((run) => run.activity)).toEqual(["edit a.py", "edit b.py", ""]);
    expect(lanes.map((run) => run.state)).toEqual(["running", "running", "queued"]);
    expect(lanes.map((run) => Math.floor(runElapsedS(run, t + 5 * SEC)))).toEqual([95, 35, 5]);
    const lines = renderSwarmBar(model, 200, undefined, t);
    expect(lines[0]).toContain("3 run"); // A01 + two running lanes
    expect(lines[0]).toContain("1 queued");
  });

  test("a repeated snapshot never shortens elapsed (the nested snapshot is only as fresh as A01's last update)", () => {
    const model = a01Spawned();
    const snap = taskDetails([nestedEntry("be", "a05-backend", "running", 10_000)]);
    a01(model, snap, T0);
    expect(elapsedOf(model, "a05-backend", T0)).toBe(10);
    // A01 emits frames for its own activity; the specialist's snapshot inside is the same stale one
    a01(model, snap, T0 + 7 * SEC);
    expect(elapsedOf(model, "a05-backend", T0 + 7 * SEC)).toBe(17);
    // a fresher snapshot that implies an earlier start would only lengthen it, a later start would not shorten it
    a01(model, taskDetails([nestedEntry("be", "a05-backend", "running", 12_000)]), T0 + 7 * SEC);
    expect(elapsedOf(model, "a05-backend", T0 + 7 * SEC)).toBe(17);
  });

  test("elapsed comes from the consumer's monotonic clock: a Date.now jump changes nothing", () => {
    const model = a01Spawned();
    const realNow = Date.now;
    try {
      a01(model, taskDetails([nestedEntry("be", "a05-backend", "running", 5000)]), T0);
      Date.now = () => realNow() + 90 * 24 * 3600 * 1000;
      a01(model, taskDetails([nestedEntry("be", "a05-backend", "running", 6000, { toolCount: 3 })]), T0 + SEC);
      expect(elapsedOf(model, "a05-backend", T0 + SEC)).toBe(6);
      Date.now = () => 0;
      expect(elapsedOf(model, "a05-backend", T0 + 4 * SEC)).toBe(9);
    } finally {
      Date.now = realNow;
    }
  });

  test("the parent's `task` call ending detaches the specialists nothing else describes; the parent's own end settles or drops them", () => {
    const model = a01Spawned();
    a01(model, taskDetails([nestedEntry("be", "a05-backend", "running", 1000), nestedEntry("fe", "a06-frontend", "pending", 0, { index: 1 })]), T0);
    // the call ended (omp clears the in-flight snapshot) and A01 is no longer inside a `task` call: no result was ever seen, and no
    // cancel either: a background job keeps running after its call returns
    ingestProgress(
      model,
      { index: 0, agent: "a01-orchestrator", parentToolCallId: "call-A01", progress: nestedEntry(A01_ID, "a01-orchestrator", "running", 30_000, { currentTool: "read" }) },
      T0 + 30 * SEC,
    );
    expect(specialists(model, T0 + 30 * SEC).map((run) => run.state)).toEqual(["running", "queued"]);
    expect(specialists(model, T0 + 30 * SEC).every((run) => isDetached(run))).toBe(true);

    // while A01 is still inside a (second, concurrent) task call, a frame without details leaves the others alone
    const model2 = a01Spawned();
    a01(model2, taskDetails([nestedEntry("be", "a05-backend", "running", 1000)]), T0);
    ingestProgress(
      model2,
      { index: 0, agent: "a01-orchestrator", parentToolCallId: "call-A01", progress: nestedEntry(A01_ID, "a01-orchestrator", "running", 2000, { currentTool: "task" }) },
      T0 + 2 * SEC,
    );
    expect(specialists(model2, T0 + 2 * SEC).map((run) => run.state)).toEqual(["running"]);
    expect(isDetached(specialists(model2, T0 + 2 * SEC)[0])).toBe(false);
    // and when A01 itself fails they settle with it
    a01(model2, undefined, T0 + 9 * SEC, {}, "failed");
    expect(specialists(model2, T0 + 9 * SEC).map((run) => run.state)).toEqual(["cancelled"]);

    // when A01 completes, the rows it never heard an end of are dropped: no state is invented for them
    const model3 = a01Spawned();
    a01(model3, taskDetails([nestedEntry("be", "a05-backend", "running", 1000)]), T0);
    a01(model3, undefined, T0 + 9 * SEC, {}, "completed");
    expect(specialists(model3, T0 + 9 * SEC)).toHaveLength(0);
    expect(rowsOf(model3, T0 + 9 * SEC)).toEqual(["a01-orchestrator:completed"]);
  });

  test("nested rows go with their parent's linger: settled specialists leave when the A01 row does", () => {
    const model = a01Spawned();
    a01(model, taskDetails([nestedEntry("be", "a05-backend", "completed", 9000)], []), T0 + 10 * SEC, {}, "running");
    a01(model, undefined, T0 + 11 * SEC, {}, "completed");
    expect(visibleRuns(model, T0 + 12 * SEC)).toHaveLength(2);
    expect(pruneExpired(model, T0 + 12 * SEC + 20 * SEC)).toBe(true);
    expect(model.runs.size).toBe(0);
    expect(model.aliases.size).toBe(0);
  });

  test("a specialist id reused by a later task call is a new row only when its clock restarted", () => {
    const model = a01Spawned();
    a01(model, taskDetails([nestedEntry("0-Work", "a05-backend", "running", 1000)]), T0);
    a01(model, taskDetails([nestedEntry("0-Work", "a05-backend", "completed", 60_000)]), T0 + 60 * SEC);
    a01(model, taskDetails([nestedEntry("0-Work", "a05-backend", "running", 61_000)]), T0 + 61 * SEC); // stale
    expect(specialists(model, T0 + 61 * SEC)[0].state).toBe("completed");
    a01(model, taskDetails([nestedEntry("0-Work", "a05-backend", "running", 2000)]), T0 + 80 * SEC); // new call, new clock
    const lanes = specialists(model, T0 + 80 * SEC);
    expect(lanes).toHaveLength(1);
    expect(lanes[0].state).toBe("running");
    expect(elapsedOf(model, "a05-backend", T0 + 80 * SEC)).toBe(2);
  });

  test("agent text from a nested entry is sanitized like any other", () => {
    const model = a01Spawned();
    const secret = `ghp_${"c".repeat(36)}`;
    a01(
      model,
      taskDetails([
        nestedEntry("be", "a05\x1b[31m-backend\r\nx", "running", 1000, { currentToolIntent: `\x1b[2Jpush with token=${secret}`, currentTool: "bash" }),
      ]),
      T0,
    );
    const [lane] = specialists(model, T0);
    expect(lane.agent).toBe("a05-backend x");
    expect(lane.activity).not.toContain(secret);
    expect(lane.activity).not.toContain("\x1b");
    expect(lane.activity).toContain("[REDACTED]");
  });
});

describe("in-session specialists: joined with live.json", () => {
  const liveRun = (taskId: string, agent: string, state: string, extra: Record<string, unknown> = {}) => ({
    run_id: `${taskId}.a1`,
    task_id: taskId,
    agent,
    state,
    elapsed_s: 50,
    activity: "store says",
    ...extra,
  });
  const be = (taskId: string, extra: Record<string, unknown> = {}) =>
    nestedEntry(`lane-${taskId}`, "a05-backend", "running", 40_000, {
      assignment: `Implement the handover module (task ${taskId}) in swarm/handover.py`,
      currentTool: "edit",
      currentToolArgs: `file of ${taskId}`,
      ...extra,
    });

  test("task id in the nested text: one row per specialist, whichever source arrives first", () => {
    for (const order of ["bus-first", "live-first"]) {
      const model = a01Spawned();
      const t = T0 + 5 * SEC;
      const details = taskDetails([be("T7f3a-be", { index: 0 }), be("T7f3a-fe", { index: 1, agent: "a05-backend" })]);
      const doc = live([liveRun("T7f3a-fe", "a05-backend", "waiting"), liveRun("T7f3a-be", "a05-backend", "running")]);
      if (order === "bus-first") {
        a01(model, details, t);
        ingestLive(model, doc, t);
      } else {
        ingestLive(model, doc, t);
        a01(model, details, t);
      }
      const lanes = specialists(model, t);
      expect(lanes).toHaveLength(2);
      const byTask = Object.fromEntries(lanes.map((run) => [run.parts.live?.taskId, run]));
      // the Task Store's identity and state, the bus' activity: lanes are not swapped even though the live rows list them in the other order
      expect(byTask["T7f3a-be"].state).toBe("running");
      expect(byTask["T7f3a-be"].activity).toBe("edit file of T7f3a-be");
      expect(byTask["T7f3a-fe"].state).toBe("waiting");
      expect(byTask["T7f3a-fe"].activity).toBe("edit file of T7f3a-fe");
      expect(model.aliases.get("T7f3a-be")).toBe(byTask["T7f3a-be"].key);
      expect(model.aliases.get("T7f3a-be.a1")).toBe(byTask["T7f3a-be"].key);
      expect(visibleRuns(model, t)).toHaveLength(3); // + A01
      const header = renderSwarmBar(model, 200, undefined, t)[0];
      expect(header).toContain("2 run"); // A01 and the running lane; the other lane waits
      expect(header).toContain("1 wait");
      // the next snapshot keeps updating the same rows
      ingestLive(model, live([liveRun("T7f3a-fe", "a05-backend", "running"), liveRun("T7f3a-be", "a05-backend", "completed")]), t + SEC);
      expect(specialists(model, t + SEC)).toHaveLength(2);
      expect(specialists(model, t + SEC).map((run) => run.state).sort()).toEqual(["completed", "running"]);
    }
  });

  test("no task id: exactly one unmatched row of the agent on each side joins by slug", () => {
    const model = a01Spawned();
    const t = T0 + SEC;
    a01(model, taskDetails([nestedEntry("fe", "a06-frontend", "running", 20_000, { currentTool: "read", currentToolArgs: "tokens.css" })]), t);
    ingestLive(model, live([liveRun("T9c1d-fe", "a06-frontend", "running")]), t);
    const lanes = specialists(model, t);
    expect(lanes).toHaveLength(1);
    expect(lanes[0].parts.live?.taskId).toBe("T9c1d-fe");
    expect(lanes[0].activity).toBe("read tokens.css");
    expect(visibleRuns(model, t)).toHaveLength(2);
  });

  test("ambiguous agent slug stays separate rows, and the summary never counts a specialist twice", () => {
    const model = a01Spawned();
    const t = T0 + SEC;
    a01(
      model,
      taskDetails([
        nestedEntry("be-1", "a05-backend", "running", 20_000, { index: 0 }),
        nestedEntry("be-2", "a05-backend", "running", 10_000, { index: 1 }),
      ]),
      t,
    );
    ingestLive(model, live([liveRun("T1111-be", "a05-backend", "running")]), t);
    // two nested lanes, one store row, no task id in the text: nothing joins
    expect(visibleRuns(model, t).filter((run) => run.agent === "a05-backend")).toHaveLength(3);
    // ...yet the swarm counts the agent's larger side once: A01 + 2 lanes
    expect(renderSwarmBar(model, 200, undefined, t)[0]).toContain("3 run");
    // a second store row (two lanes each side): still two specialists, not four
    ingestLive(model, live([liveRun("T1111-be", "a05-backend", "running"), liveRun("T2222-be", "a05-backend", "running")]), t + SEC);
    expect(renderSwarmBar(model, 200, undefined, t + SEC)[0]).toContain("3 run");
    // a third store row than nested lanes: the store knows more, it counts
    ingestLive(model, live([1, 2, 3].map((n) => liveRun(`T${n}${n}${n}${n}-be`, "a05-backend", "running"))), t + 2 * SEC);
    expect(renderSwarmBar(model, 200, undefined, t + 2 * SEC)[0]).toContain("4 run");
  });

  test("the nested text naming another task of the same agent blocks a slug join", () => {
    const model = a01Spawned();
    const t = T0 + SEC;
    a01(model, taskDetails([be("T7f3a-fe")]), t);
    ingestLive(model, live([liveRun("T7f3a-be", "a05-backend", "running")]), t);
    expect(specialists(model, t).filter((run) => run.agent === "a05-backend")).toHaveLength(1);
    expect(visibleRuns(model, t).filter((run) => run.agent === "a05-backend")).toHaveLength(2);
  });

  test("settled rows only join settled rows (a finished lane never claims the next lane's store row)", () => {
    const model = a01Spawned();
    a01(model, taskDetails([nestedEntry("be-old", "a05-backend", "completed", 30_000)]), T0);
    ingestLive(model, live([liveRun("T4444-be", "a05-backend", "running")]), T0 + SEC);
    const rows = visibleRuns(model, T0 + SEC).filter((run) => run.agent === "a05-backend");
    expect(rows.map((run) => run.state).sort()).toEqual(["completed", "running"]);
  });

  test("dropping live.json leaves the nested rows; the live part goes", () => {
    const model = a01Spawned();
    a01(model, taskDetails([be("T7f3a-be")]), T0);
    ingestLive(model, live([liveRun("T7f3a-be", "a05-backend", "waiting")]), T0);
    expect(specialists(model, T0)[0].state).toBe("waiting");
    dropLive(model, T0 + SEC);
    expect(specialists(model, T0 + SEC)).toHaveLength(1);
    expect(specialists(model, T0 + SEC)[0].parts.live).toBeUndefined();
    expect(specialists(model, T0 + SEC)[0].state).toBe("running");
  });
});

describe("in-session specialists: hostile payloads", () => {
  test("malformed inflightTaskDetails never throws and adds no rows", () => {
    const bad: unknown[] = [
      "text",
      42,
      null,
      [],
      [[]],
      {},
      { progress: "nope", results: 7 },
      { progress: { 0: nestedEntry("x", "a", "running", 1) }, results: {} },
      { progress: [null, 1, "s", [], {}, { id: 5 }, { id: "ok" }, { status: "running" }, { id: "x", status: "weird" }], results: [null, 3, "x", {}, [], { exitCode: "no" }] },
      { progress: [{ id: "n", status: "running", agent: { toString: () => { throw new Error("no"); } }, durationMs: "5", toolCount: "x", currentTool: 9 }] },
    ];
    for (const details of bad) {
      const model = a01Spawned();
      expect(() => a01(model, details, T0)).not.toThrow();
      expect(() => renderSwarmBar(model, 80, undefined, T0 + SEC)).not.toThrow();
      expect(specialists(model, T0).length).toBeLessThanOrEqual(2);
    }
    // a hostile entry with a usable id still shows as a row with safe defaults
    const model = a01Spawned();
    a01(model, taskDetails([{ id: "only-an-id", status: "running", durationMs: Number.NaN, toolCount: Infinity }]), T0);
    expect(specialists(model, T0).map((run) => run.state)).toEqual(["running"]);
    expect(Number.isFinite(elapsedOf(model, "task", T0 + SEC))).toBe(true);
  });

  test("an oversized payload is capped at 64 nested entries per frame", () => {
    const model = a01Spawned();
    const many = Array.from({ length: 5000 }, (_, i) => nestedEntry(`lane-${i}`, "a05-backend", "running", 1000, { index: i }));
    const results = Array.from({ length: 5000 }, (_, i) => nestedResult(`done-${i}`, "a15-docs", 1000, { index: i }));
    a01(model, taskDetails(many, results), T0);
    expect(specialists(model, T0)).toHaveLength(64);
    expect(model.runs.size).toBe(65);
    // results alone are capped too, and a settled result fills what progress leaves
    const model2 = a01Spawned();
    a01(model2, taskDetails(many.slice(0, 10), results), T0);
    expect(specialists(model2, T0)).toHaveLength(64);
    expect(specialists(model2, T0).filter((run) => run.state === "completed")).toHaveLength(54);
  });

  test("huge strings in nested entries are bounded before they are scanned or shown", () => {
    const model = a01Spawned();
    const big = "word ".repeat(200_000);
    a01(model, taskDetails([nestedEntry("be", "a05-backend", "running", 1000, { assignment: big, task: big, description: big, currentToolIntent: big })]), T0);
    const [lane] = specialists(model, T0);
    expect(displayWidth(lane.activity)).toBeLessThanOrEqual(120);
  });

  test("a frame without nested details behaves exactly as before", () => {
    const model = createModel();
    progress(model, { id: "a", agent: "explore" }, T0);
    expect(visibleRuns(model, T0)).toHaveLength(1);
    expect(visibleRuns(model, T0)[0].parentKey).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------------------------------------------------
// Review RV-F (late bar fixes): the REAL shapes. The in-session live.json is the Task Store's derived snapshot (agents `A02`),
// omp's nested progress says `a02-requirements`, and most specialists are background jobs (`details.async`).
// ---------------------------------------------------------------------------------------------------------------------

/** A document written by `swarm/telemetry.py derive_snapshot`: scripts/orch_plan.py then orch_status.py --transition (req completed, arch running, ux queued, nine waiting). */
const derivedDoc = (): Record<string, unknown> & { runs: Array<Record<string, unknown>> } =>
  JSON.parse(readFileSync(join(REPO_ROOT, "omp", "test", "fixtures", "live_derived_sample.json"), "utf8"));

/** `TaskToolDetails` as omp's `buildAsyncDetails` builds it (task/index.ts): `results` holds only the inline spawns, `progress` every spawn, `async` the background jobs. */
function asyncDetails(progressEntries: unknown[], state = "running"): Record<string, unknown> {
  return { projectAgentsDir: null, results: [], totalDurationMs: 12, progress: progressEntries, async: { state, jobId: "ApiImpl", type: "task" } };
}

/** An A01 frame with an arbitrary progress shape on top of the standard A01 entry. */
function a01Raw(model: SwarmBarModel, now: number, progressExtra: Record<string, unknown>, status = "running"): boolean {
  return ingestProgress(
    model,
    {
      index: 0,
      agent: "a01-orchestrator",
      task: "run the plan",
      parentToolCallId: "call-A01",
      progress: nestedEntry(A01_ID, "a01-orchestrator", status, now - T0, progressExtra),
    },
    now,
  );
}
/** The frame carrying the one `async` update of A01's `task` call. */
const a01Spawn = (model: SwarmBarModel, now: number, entries: unknown[]): boolean =>
  a01Raw(model, now, { currentTool: "task", inflightTaskDetails: asyncDetails(entries) });
/** A01's next frame: its `task` call returned (omp cleared `inflightTaskDetails` and `currentTool`) and it works on with another tool. */
const a01Cleared = (model: SwarmBarModel, now: number): boolean =>
  a01Raw(model, now, { currentTool: "bash", currentToolArgs: "python3 scripts/orch_status.py --transition" });

const lane = (taskId: string | undefined, agent: string, id: string, durationMs = 5000, extra: Record<string, unknown> = {}) =>
  nestedEntry(id, agent, "running", durationMs, { assignment: taskId ? `Work on ${taskId}: do the thing` : "do the thing", index: 0, ...extra });
const keyRows = (model: SwarmBarModel, now: number, key: string): BarRun[] => visibleRuns(model, now).filter((run) => agentKey(run.agent) === key);

describe("agent identity across sources", () => {
  test("the fixture is a real derived document: producer derived, agents named by Task Store id", () => {
    const doc = derivedDoc();
    expect(doc.producer).toBe("derived");
    expect(doc.runs.map((run) => run.agent)).toContain("A02");
    expect(doc.runs.every((run) => /^A\d\d$/.test(String(run.agent)))).toBe(true);
    expect(Object.fromEntries(doc.runs.map((run) => [run.agent, run.state])).A02).toBe("completed");
    expect(Object.fromEntries(doc.runs.map((run) => [run.agent, run.state])).A03).toBe("running");
  });

  test("agentKey parses either spelling and falls back to the lower-cased name", () => {
    expect(agentKey("A02")).toBe("a02");
    expect(agentKey("a02")).toBe("a02");
    expect(agentKey("a02-requirements")).toBe("a02");
    expect(agentKey("A15-Docs")).toBe("a15");
    expect(agentKey("  a05_backend ")).toBe("a05");
    expect(agentKey("a020")).toBe("a020");
    expect(agentKey("a2-x")).toBe("a2-x");
    expect(agentKey("Explore")).toBe("explore");
    expect(agentKey("task")).toBe("task");
    expect(agentKey("")).toBe("");
  });

  test("nested rows join the derived live.json by task id: one row per specialist, whichever source arrives first, no inflated counts", () => {
    for (const order of ["bus-first", "live-first"]) {
      const model = a01Spawned();
      const t = T0 + 5 * SEC;
      const entries = [lane("T38a7-req", "a02-requirements", "Requirements", 4000), lane("T38a7-arch", "a03-architect", "Architecture", 3000)];
      const sendFrame = () => a01Raw(model, t, { currentTool: "task", inflightTaskDetails: asyncDetails(entries) });
      if (order === "bus-first") {
        sendFrame();
        ingestLive(model, derivedDoc(), t);
      } else {
        ingestLive(model, derivedDoc(), t);
        sendFrame();
      }
      expect(keyRows(model, t, "a02")).toHaveLength(1);
      expect(keyRows(model, t, "a03")).toHaveLength(1);
      expect(keyRows(model, t, "a02")[0].parts.live?.taskId).toBe("T38a7-req");
      expect(keyRows(model, t, "a02")[0].agent).toBe("a02-requirements"); // the bus' name is shown, the store's identity is joined
      expect(keyRows(model, t, "a02")[0].state).toBe("completed");
      expect(keyRows(model, t, "a03")[0].state).toBe("running");
      expect(visibleRuns(model, t)).toHaveLength(14); // A01 + the store's 13 rows
      const header = renderSwarmBar(model, 240, undefined, t)[0];
      for (const part of ["2 run", "10 wait", "1 queued", "1 done"]) expect(header).toContain(part);
    }
  });

  test("no task id in the text: the one unmatched row of that agent on each side joins on the canonical key", () => {
    const model = a01Spawned();
    const t = T0 + 5 * SEC;
    ingestLive(model, derivedDoc(), t);
    a01Spawn(model, t, [lane(undefined, "a03-architect", "Architecture", 3000)]);
    expect(keyRows(model, t, "a03")).toHaveLength(1);
    expect(keyRows(model, t, "a03")[0].parentKey).toBeDefined();
    expect(keyRows(model, t, "a03")[0].parts.live?.taskId).toBe("T38a7-arch");
    expect(renderSwarmBar(model, 240, undefined, t)[0]).toContain("2 run");
  });

  test("a nested text naming another task of the same agent still blocks the slug join", () => {
    const model = a01Spawned();
    const t = T0 + 5 * SEC;
    ingestLive(model, derivedDoc(), t);
    a01Spawn(model, t, [lane("T38a7-other", "a03-architect", "Other", 3000)]);
    expect(keyRows(model, t, "a03")).toHaveLength(2);
    // the summary still counts the agent once: the store knows one A03 row
    expect(renderSwarmBar(model, 240, undefined, t)[0]).toContain("2 run");
  });

  test("a top-level bus row and the derived row of the same agent are one row (the twin check compares keys)", () => {
    const model = createModel({ reducedMotion: true });
    progress(model, { id: "Arch", agent: "a03-architect", status: "running", durationMs: 4000 }, T0);
    ingestLive(model, derivedDoc(), T0 + SEC);
    expect(keyRows(model, T0 + SEC, "a03")).toHaveLength(1);
    expect(keyRows(model, T0 + SEC, "a03")[0].parts.live).toBeDefined();
    expect(keyRows(model, T0 + SEC, "a03")[0].parts.bus).toBeDefined();
  });

  test("a joined derived row is not cancelled when A01's call returns: the live part protects it", () => {
    const model = a01Spawned();
    a01Spawn(model, T0 + 5 * SEC, [lane("T38a7-arch", "a03-architect", "Architecture", 3000), lane("T38a7-ux", "a04-ux-designer", "Ux", 3000)]);
    ingestLive(model, derivedDoc(), T0 + 5 * SEC);
    a01Cleared(model, T0 + 6 * SEC);
    const states = Object.fromEntries(specialists(model, T0 + 6 * SEC).map((run) => [agentKey(run.agent), run.state]));
    expect(states).toEqual({ a03: "running", a04: "queued" });
    expect(specialists(model, T0 + 6 * SEC).every((run) => !isDetached(run))).toBe(true);
  });
});

describe("background specialists (details.async)", () => {
  const spawnTwo = (model: SwarmBarModel, now: number) =>
    a01Spawn(model, now, [
      lane("T38a7-be", "a05-backend", "ApiImpl", 5000, { currentTool: "edit", currentToolArgs: "swarm/x.py" }),
      lane("T38a7-fe", "a06-frontend", "UiImpl", 2000, { index: 1 }),
    ]);

  test("the exact async details shape, then a cleared frame: the rows stay running, detached, and their clock keeps ticking", () => {
    const model = a01Spawned();
    spawnTwo(model, T0 + 5 * SEC);
    expect(rowsOf(model, T0 + 5 * SEC)).toContain("a05-backend:running");
    a01Cleared(model, T0 + 5200); // tool_execution_end of `task` cleared inflightTaskDetails
    expect(rowsOf(model, T0 + 5200)).toEqual(expect.arrayContaining(["a05-backend:running", "a06-frontend:running"]));
    expect(rowsOf(model, T0 + 5200).some((row) => row.endsWith(":cancelled"))).toBe(false);
    expect(specialists(model, T0 + 5200).every((run) => isDetached(run))).toBe(true);
    // many more A01 frames of its own work change nothing
    for (let s = 6; s < 60; s += 6) a01Cleared(model, T0 + s * SEC);
    expect(rowsOf(model, T0 + 60 * SEC).filter((row) => row.endsWith(":running"))).toHaveLength(3);
    expect(elapsedOf(model, "a05-backend", T0 + 65 * SEC)).toBe(elapsedOf(model, "a05-backend", T0 + 5 * SEC) + 60);
    const [summary, rowsLine] = renderSwarmBar(model, 220, undefined, T0 + 60 * SEC);
    expect(summary).toContain("3 run");
    expect(summary).not.toContain("cancel");
    expect(rowsLine).toMatch(/a05-backend\s+\d+m?\d*s~/);
    expect(rowsLine).toMatch(/a06-frontend\s+\d+m?\d*s~/);
    expect(rowsLine).toMatch(/a01-orchestrator\s+\d+m?\d*s(?!~)/);
    expect(renderSwarmBar(model, 220, undefined, T0 + 60 * SEC)[0]).toContain("3 run");
  });

  test("a frame without `async` names its rows as ordinary (blocking) specialists: not detached while the call is in flight", () => {
    const model = a01Spawned();
    a01(model, taskDetails([nestedEntry("be", "a05-backend", "running", 1000)]), T0);
    expect(isDetached(specialists(model, T0)[0])).toBe(false);
    a01(model, taskDetails([nestedEntry("be", "a05-backend", "running", 1000)]), T0 + SEC); // identical snapshot
    expect(isDetached(specialists(model, T0 + SEC)[0])).toBe(false);
    a01Cleared(model, T0 + 2 * SEC);
    expect(isDetached(specialists(model, T0 + 2 * SEC)[0])).toBe(true);
    expect(specialists(model, T0 + 2 * SEC)[0].state).toBe("running");
  });

  test("a second task call replaces the snapshot: the rows it does not list are detached, its own async rows are too", () => {
    const model = a01Spawned();
    spawnTwo(model, T0 + SEC);
    a01Cleared(model, T0 + 2 * SEC);
    a01Spawn(model, T0 + 10 * SEC, [lane("T38a7-qa", "a08-qa", "Qa", 100)]);
    expect(rowsOf(model, T0 + 10 * SEC).filter((row) => row.endsWith(":running"))).toHaveLength(4);
    expect(specialists(model, T0 + 10 * SEC).filter((run) => isDetached(run))).toHaveLength(3);
  });

  test("(a) a live.json row with a final state settles it and wins; while the file lists it, no `~`", () => {
    const model = a01Spawned();
    a01Spawn(model, T0 + 5 * SEC, [lane("T38a7-arch", "a03-architect", "Architecture", 3000)]);
    a01Cleared(model, T0 + 6 * SEC);
    expect(isDetached(specialists(model, T0 + 6 * SEC)[0])).toBe(true);
    ingestLive(model, derivedDoc(), T0 + 7 * SEC); // the store says A03 runs
    const [row] = specialists(model, T0 + 7 * SEC);
    expect(row.state).toBe("running");
    expect(isDetached(row)).toBe(false);
    expect(renderSwarmBar(model, 240, undefined, T0 + 7 * SEC)[1]).not.toContain("~");
    const done = derivedDoc();
    for (const run of done.runs) if (run.agent === "A03") Object.assign(run, { state: "completed", elapsed_s: 40 });
    ingestLive(model, done, T0 + 50 * SEC);
    expect(specialists(model, T0 + 50 * SEC)[0].state).toBe("completed");
    const failed = derivedDoc();
    const other = a01Spawned();
    a01Spawn(other, T0 + SEC, [lane("T38a7-arch", "a03-architect", "Architecture", 3000)]);
    a01Cleared(other, T0 + 2 * SEC);
    for (const run of failed.runs) if (run.agent === "A03") Object.assign(run, { state: "failed", error: "tests failed" });
    ingestLive(other, failed, T0 + 3 * SEC);
    expect(specialists(other, T0 + 3 * SEC)[0].state).toBe("failed");
  });

  test("(b) the parent's end: failed or cancelled settles `cancelled`, completed drops the row (never invents `done`)", () => {
    for (const [status, expected] of [["failed", ["cancelled", "cancelled"]], ["aborted", ["cancelled", "cancelled"]], ["completed", []]] as const) {
      const model = a01Spawned();
      spawnTwo(model, T0 + SEC);
      a01Cleared(model, T0 + 2 * SEC);
      expect(specialists(model, T0 + 2 * SEC)).toHaveLength(2);
      a01Raw(model, T0 + 30 * SEC, {}, status);
      const rows = specialists(model, T0 + 30 * SEC);
      expect({ status, states: rows.map((run) => run.state) }).toEqual({ status, states: [...expected] });
      expect(rows.some((run) => run.state === "completed")).toBe(false);
    }
  });

  test("(c) the evidence timeout (default 900 s) drops a detached row; any later frame or live.json read restarts the count", () => {
    const model = a01Spawned();
    spawnTwo(model, T0);
    a01Cleared(model, T0 + SEC);
    expect(pruneExpired(model, T0 + 899 * SEC)).toBe(false);
    expect(specialists(model, T0 + 899 * SEC)).toHaveLength(2);
    expect(pruneExpired(model, T0 + 900 * SEC)).toBe(true);
    expect(specialists(model, T0 + 900 * SEC)).toHaveLength(0);
    expect(rowsOf(model, T0 + 900 * SEC)).toEqual(["a01-orchestrator:running"]);

    // a frame that names the row again is evidence
    const again = a01Spawned();
    spawnTwo(again, T0);
    a01Cleared(again, T0 + SEC);
    a01Spawn(again, T0 + 800 * SEC, [lane("T38a7-be", "a05-backend", "ApiImpl", 800_000), lane("T38a7-fe", "a06-frontend", "UiImpl", 800_000, { index: 1 })]);
    a01Cleared(again, T0 + 801 * SEC);
    expect(pruneExpired(again, T0 + 1500 * SEC)).toBe(false);
    expect(pruneExpired(again, T0 + 1700 * SEC)).toBe(true);

    // so is a live.json read that lists it; once the file loses it the window counts from that read
    const joined = a01Spawned();
    a01Spawn(joined, T0, [lane("T38a7-arch", "a03-architect", "Architecture", 3000)]);
    a01Cleared(joined, T0 + SEC);
    ingestLive(joined, derivedDoc(), T0 + 800 * SEC);
    pruneExpired(joined, T0 + 1600 * SEC);
    expect(specialists(joined, T0 + 1600 * SEC)).toHaveLength(1); // the file still lists it: its liveness is the file's, not a timer
    dropLive(joined, T0 + 801 * SEC);
    expect(specialists(joined, T0 + 801 * SEC)).toHaveLength(1);
    expect(pruneExpired(joined, T0 + 800 * SEC + 899 * SEC)).toBe(false);
    expect(pruneExpired(joined, T0 + 800 * SEC + 900 * SEC)).toBe(true);

    // configurable per model
    const quick = createModel({ reducedMotion: true, detachedMs: 60_000 });
    ingestTaskSpawn(quick, { tasks: [{ name: A01_ID, agent: "a01-orchestrator", assignment: "run the plan" }] }, "call-A01", T0);
    spawnTwo(quick, T0);
    a01Cleared(quick, T0 + SEC);
    expect(pruneExpired(quick, T0 + 59 * SEC)).toBe(false);
    expect(pruneExpired(quick, T0 + 60 * SEC)).toBe(true);
  });
});

describe("terminal rows never regress", () => {
  test("a row the derived live.json settled keeps its state and frozen elapsed when the file loses it (deleted, stale, row gone)", () => {
    for (const how of ["dropLive", "row gone"]) {
      const model = a01Spawned();
      a01Spawn(model, T0 + 5 * SEC, [lane("T38a7-req", "a02-requirements", "Requirements", 4000)]);
      ingestLive(model, derivedDoc(), T0 + 6 * SEC); // A02 completed
      const [row] = specialists(model, T0 + 6 * SEC);
      expect(row.state).toBe("completed");
      const frozen = Math.floor(runElapsedS(row, T0 + 6 * SEC));
      if (how === "dropLive") dropLive(model, T0 + 20 * SEC);
      else ingestLive(model, { ...derivedDoc(), runs: [] }, T0 + 20 * SEC);
      const after = specialists(model, T0 + 20 * SEC)[0];
      expect({ how, state: after.state }).toEqual({ how, state: "completed" });
      expect(Math.floor(runElapsedS(after, T0 + 20 * SEC))).toBe(frozen);
      a01Cleared(model, T0 + 21 * SEC); // and A01's next frame does not turn it into cancelled either
      expect(specialists(model, T0 + 21 * SEC)[0].state).toBe("completed");
      expect(renderSwarmBar(model, 200, undefined, T0 + 21 * SEC)[0]).toMatch(/1 done/);
    }
  });
});

describe("activity text is built from bounded pieces", () => {
  test("a megabyte of tool arguments costs nothing per frame and the row still reads `tool args`", () => {
    const model = createModel({ reducedMotion: true });
    const huge = "x".repeat(1_000_000);
    for (let i = 0; i < 200; i++) {
      ingestProgress(
        model,
        { index: 0, agent: "a05-backend", parentToolCallId: "c", progress: { index: 0, id: "be", agent: "a05-backend", status: "running", task: "t", toolCount: i, durationMs: i, currentTool: "bash", currentToolArgs: huge } },
        T0 + i,
      );
    }
    const [row] = visibleRuns(model, T0 + 200);
    expect(row.activity.startsWith("bash xxxx")).toBe(true);
    expect(displayWidth(row.activity)).toBeLessThanOrEqual(120);
  });
});
