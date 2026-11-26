/**
 * Consumer-facing titles and plain-English blurbs for the home starter
 * chooser (M25.5). Keeps catalogue pack bytes unchanged; presentation only.
 *
 * Titles match the plan verbatim. Each description is ≤20 words and avoids
 * JSON/seed jargon, venue pair codes, and glued Educational-name typos.
 */

export type ConsumerStarterCard = {
  id: string;
  title: string;
  description: string;
};

/** Stable order for the three home cards. */
export const CONSUMER_STARTER_CARD_ORDER: ReadonlyArray<string> = [
  "starter-dual-ma-crossover",
  "starter-mean-reversion-band",
  "starter-range-breakout",
];

export const CONSUMER_STARTER_CARDS: Readonly<
  Record<string, ConsumerStarterCard>
> = {
  "starter-dual-ma-crossover": {
    id: "starter-dual-ma-crossover",
    title: "Dual moving average",
    description:
      "Buys when a short average rises above a long one; sells when it falls back below.",
  },
  "starter-mean-reversion-band": {
    id: "starter-mean-reversion-band",
    title: "Mean reversion",
    description:
      "Buys when price dips below its recent average and sells when it returns.",
  },
  "starter-range-breakout": {
    id: "starter-range-breakout",
    title: "Range breakout",
    description:
      "Buys when price pushes above a recent high range and exits if it fades.",
  },
};

export type StruggleCopy = {
  id: string;
  title: string;
  /** Market conditions where this shape tends to do poorly. No numbers. */
  struggle: string;
};

/** The three home starters in card order, then the built-in modes. */
export const STRUGGLE_COPY_ORDER: ReadonlyArray<string> = [
  ...CONSUMER_STARTER_CARD_ORDER,
  "mode-dca",
  "mode-grid",
  "mode-rebalance",
];

/**
 * When each starter and built-in mode tends to struggle, read from what its
 * pack's rules do (entry/exit, stop, long-only, buys only while flat). Plain
 * conditions, never a performance figure or a forecast.
 */
export const STRUGGLE_COPY: Readonly<Record<string, StruggleCopy>> = {
  "starter-dual-ma-crossover": {
    id: "starter-dual-ma-crossover",
    title: "Dual moving average",
    struggle:
      "Choppy, sideways markets. The averages keep crossing, so it buys late, sells late and pays fees on each flip.",
  },
  "starter-mean-reversion-band": {
    id: "starter-mean-reversion-band",
    title: "Mean reversion",
    struggle:
      "Steady declines. Price can keep sliding below its average, so a dip it buys may fall further until the stop sells.",
  },
  "starter-range-breakout": {
    id: "starter-range-breakout",
    title: "Range breakout",
    struggle:
      "False breakouts. Price pokes above the recent range and then falls back, so it buys near the top and exits lower.",
  },
  "mode-dca": {
    id: "mode-dca",
    title: "DCA",
    struggle:
      "Long declines. It buys while flat and then holds without adding more, so a fall runs to the stop before it buys again.",
  },
  "mode-grid": {
    id: "mode-grid",
    title: "Grid",
    struggle:
      "Strong trends. In a steady fall the lows it buys keep falling until the stop sells; in a steady rise it may never buy.",
  },
  "mode-rebalance": {
    id: "mode-rebalance",
    title: "Rebalance",
    struggle:
      "Sideways markets near its average. Price crosses back and forth, so it buys and sells often and pays fees each time.",
  },
};

const EDUCATIONAL_SUFFIX = /\s*\(educational example\)\s*$/i;
const EDUCATIONAL_PREFIX = /^Educational example\.\s*/i;

/**
 * Strip educational-example suffixes/prefixes without gluing the leftover
 * name into an Educational typo (never concatenate "Educational").
 */
export function plainStarterLabel(raw: string): string {
  return raw.replace(EDUCATIONAL_SUFFIX, "").trim();
}

export function plainStarterDescription(raw: string): string {
  return raw.replace(EDUCATIONAL_PREFIX, "").trim();
}

export function wordCount(text: string): number {
  const trimmed = text.trim();
  if (!trimmed) {
    return 0;
  }
  return trimmed.split(/\s+/).length;
}

export function consumerCardForExample(example: {
  id: string;
  label: string;
  description: string;
}): ConsumerStarterCard {
  const known = CONSUMER_STARTER_CARDS[example.id];
  if (known) {
    return known;
  }
  return {
    id: example.id,
    title: plainStarterLabel(example.label),
    description: plainStarterDescription(example.description),
  };
}

/**
 * Consumer chooser shows the three known cards in plan order when present
 * in the catalogue; never invents rows that the catalogue omitted.
 */
export function orderConsumerStarterExamples<T extends { id: string }>(
  examples: ReadonlyArray<T>,
): T[] {
  const byId = new Map(examples.map((example) => [example.id, example]));
  const ordered: T[] = [];
  for (const id of CONSUMER_STARTER_CARD_ORDER) {
    const row = byId.get(id);
    if (row) {
      ordered.push(row);
    }
  }
  return ordered;
}
