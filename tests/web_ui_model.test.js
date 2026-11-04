"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const webRoot = path.join(__dirname, "..", "src", "branch_radar", "resources", "web");
const i18n = require(path.join(webRoot, "i18n.js"));

const {
  branchStatus,
  groupWatchStatus,
  groupWatches,
  latestTimestamp,
  latestValue,
  parseRepositoryTags,
  recentEventWatchKeys,
  repositoryDisplayName,
  repositoryGroupKey,
  repositoryNeedsAttention,
  shortSha,
  watchAttentionReason,
  watchBranchId,
  watchKey,
  buildBranchCanvasModel,
  buildSignalFieldModel,
  canvasLod,
  clampCanvasZoom,
  classifyChangeSize,
  fitCanvasTransform,
  isEventNewForRun,
  latestCanvasTransform,
  moveListboxActiveIndex,
  moveEventSelection,
  normalizeCheckRunSummary,
  normalizeEventMetadata,
  prioritizeBranchNames,
  shouldHandleShortcut,
  zoomAroundPoint,
} = require("../src/branch_radar/resources/web/ui-model.js");

function shortcutEvent(key, target = { tagName: "DIV", isContentEditable: false }) {
  return {
    key,
    target,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
  };
}

function shortcutContext(overrides = {}) {
  return {
    view: "overview",
    dialogOpen: false,
    menuOpen: false,
    editable: false,
    ...overrides,
  };
}

test("classifyChangeSize applies stable boundaries and derives change totals", () => {
  const empty = classifyChangeSize(0);
  assert.equal(empty.key, "small");
  assert.equal(empty.label, "小");
  assert.equal(empty.total, 0);
  assert.equal(empty.level, 1);
  assert.equal(empty.ratio, 0);

  const cases = [
    [0, "small"],
    [49, "small"],
    [50, "small"],
    [199, "small"],
    [200, "medium"],
    [499, "medium"],
    [500, "medium"],
    [1_999, "medium"],
    [2_000, "large"],
    [Number.MAX_SAFE_INTEGER, "large"],
  ];
  for (const [total, expected] of cases) {
    assert.equal(classifyChangeSize(total).key, expected, String(total));
  }

  assert.equal(classifyChangeSize({ insertions: 125, deletions: 75 }).total, 200);
  assert.equal(
    classifyChangeSize({ changes: 300, insertions: 2_000, deletions: 2_000 }).total,
    300,
  );
  assert.equal(classifyChangeSize({ insertions: -10, deletions: Number.NaN }).total, 0);
});

test("normalizeEventMetadata sanitizes and deduplicates compact event metadata", () => {
  const metadata = normalizeEventMetadata({
    authors: ["  Alice  ", "Alice", "", null, "Bob"],
    categories: ["api", " deployment ", "api", 42],
    signals: ["breaking change", " breaking change ", "需要迁移", undefined],
    stats: {
      commits: 3,
      files: 12,
      insertions: 125,
      deletions: 75,
    },
  });

  assert.deepEqual(metadata.authors, ["Alice", "Bob"]);
  assert.deepEqual(metadata.categories, ["api", "deployment"]);
  assert.deepEqual(metadata.signals, ["breaking change", "需要迁移"]);
  assert.equal(metadata.stats.commits, 3);
  assert.equal(metadata.stats.files, 12);
  assert.equal(metadata.stats.insertions, 125);
  assert.equal(metadata.stats.deletions, 75);
  assert.equal(metadata.size.key, "medium");
  assert.equal(metadata.size.total, 200);
});

test("normalizeEventMetadata is safe for missing and invalid values", () => {
  const metadata = normalizeEventMetadata({
    authors: "Alice",
    categories: null,
    signals: {},
    stats: { commits: -1, files: Number.POSITIVE_INFINITY },
  });

  assert.deepEqual(metadata.authors, []);
  assert.deepEqual(metadata.categories, []);
  assert.deepEqual(metadata.signals, []);
  assert.equal(metadata.stats.commits, 0);
  assert.equal(metadata.stats.files, 0);
  assert.equal(metadata.size.total, 0);
});

test("buildSignalFieldModel groups identity-safe lanes and orders recent signals", () => {
  const nowMs = Date.parse("2026-08-25T12:00:00Z");
  const model = buildSignalFieldModel([
    { id: 4, repository_id: 9, branch_id: 14, repository: "app", branch: "main", detected_at: "2026-08-25T11:40:00Z" },
    { id: 2, repository_id: 7, branch_id: 11, repository: "app", branch: "main", detected_at: "2026-08-25T10:00:00Z" },
    { id: 3, repository_id: 7, branch_id: 11, repository: "app", branch: "main", detected_at: "2026-08-25T11:00:00Z" },
    { id: 3, repository_id: 7, branch_id: 11, repository: "duplicate", branch: "ignored", detected_at: "2026-08-25T11:30:00Z" },
    { id: 1, repository_id: 7, branch_id: 12, repository: "app", branch: "release", detected_at: "2026-08-23T11:00:00Z" },
  ], { nowMs, rangeHours: 24 });

  assert.equal(model.lanes.length, 2, "same labels with different numeric ids remain separate");
  assert.deepEqual(model.events.map((item) => item.id), [4, 3, 2]);
  assert.deepEqual(model.lanes[1].events.map((item) => item.id), [2, 3]);
  assert.equal(model.hiddenCount, 1);
  assert.equal(model.startMs, Date.parse("2026-08-24T12:00:00Z"));
  assert.equal(model.endMs, nowMs);
});

test("buildSignalFieldModel aligns shared check runs and separates collisions", () => {
  const nowMs = Date.parse("2026-08-25T12:00:00Z");
  const model = buildSignalFieldModel([
    { id: 1, repository_id: 1, branch_id: 1, repository: "one", branch: "main", detected_at: "2026-08-25T11:00:00Z", check_run_id: "run-1" },
    { id: 2, repository_id: 2, branch_id: 2, repository: "two", branch: "main", detected_at: "2026-08-25T11:00:30Z", check_run_id: "run-1" },
    { id: 3, repository_id: 1, branch_id: 1, repository: "one", branch: "main", detected_at: "2026-08-25T11:01:00Z" },
  ], { nowMs, rangeHours: 24, minGapRatio: 0.05 });

  assert.equal(model.scanBands.length, 1);
  const aligned = model.lanes.flatMap((lane) => lane.events).filter((item) => [1, 2].includes(item.id));
  assert.equal(aligned[0].position, aligned[1].position);
  const crowdedLane = model.lanes.find((lane) => lane.repository === "one");
  assert.equal(crowdedLane.slots, 2);
  assert.deepEqual(crowdedLane.events.map((item) => item.slot), [0, 1]);
});

test("buildSignalFieldModel keeps malformed times visible but orders them last", () => {
  const model = buildSignalFieldModel([
    { id: 2, repository: "app", branch: "main", detected_at: "not-a-time" },
    { id: 1, repository: "app", branch: "main", detected_at: "2026-08-25T11:00:00Z" },
  ], { nowMs: Date.parse("2026-08-25T12:00:00Z"), rangeHours: 24 });

  assert.deepEqual(model.events.map((item) => item.id), [1, 2]);
  assert.equal(model.lanes[0].events[0].id, 2);
  assert.equal(model.lanes[0].events[0].position, 0);
});

test("buildSignalFieldModel keeps the range boundary, clamps future events, and only bands shared runs", () => {
  const nowMs = Date.parse("2026-08-25T12:00:00Z");
  const model = buildSignalFieldModel([
    { id: 1, repository: "app", branch: "main", detected_at: "2026-08-24T12:00:00Z", check_run_id: "solo" },
    { id: 2, repository: "app", branch: "main", detected_at: "2026-08-25T12:30:00Z" },
  ], { nowMs, rangeHours: 24 });

  assert.deepEqual(model.events.map((item) => item.id), [2, 1]);
  const laneEvents = model.lanes.flatMap((lane) => lane.events);
  assert.equal(laneEvents.find((item) => item.id === 1).position, 0);
  assert.equal(laneEvents.find((item) => item.id === 2).position, 1);
  assert.deepEqual(model.scanBands, []);
});

test("buildSignalFieldModel bounds dense lane height", () => {
  const events = Array.from({ length: 12 }, (_, index) => ({
    id: index + 1,
    repository: "app",
    branch: "main",
    detected_at: `2026-08-25T11:00:${String(index).padStart(2, "0")}Z`,
  }));
  const model = buildSignalFieldModel(events, {
    nowMs: Date.parse("2026-08-25T12:00:00Z"),
    rangeHours: 24,
    maxSlots: 4,
  });

  assert.equal(model.lanes[0].slots, 4);
  assert.ok(model.lanes[0].events.every((item) => item.slot >= 0 && item.slot < 4));
});

test("buildBranchCanvasModel lays time left-to-right and groups consecutive author segments", () => {
  const nowMs = Date.parse("2026-08-25T12:00:00Z");
  const model = buildBranchCanvasModel([
    {
      id: 3,
      repository_id: 7,
      branch_id: 11,
      repository: "app",
      branch: "main",
      detected_at: "2026-08-25T11:30:00Z",
      authors: ["Bob"],
      stats: { commits: 1, files: 2, insertions: 5, deletions: 1 },
    },
    {
      id: 2,
      repository_id: 7,
      branch_id: 11,
      repository: "app",
      branch: "main",
      detected_at: "2026-08-25T11:00:00Z",
      authors: ["Alice"],
      stats: { commits: 2, files: 3, insertions: 20, deletions: 4 },
    },
    {
      id: 1,
      repository_id: 7,
      branch_id: 11,
      repository: "app",
      branch: "main",
      detected_at: "2026-08-25T10:00:00Z",
      authors: [" Alice "],
      stats: { commits: 1, files: 1, insertions: 10, deletions: 2 },
    },
    {
      id: 4,
      repository_id: 9,
      branch_id: 14,
      repository: "app",
      branch: "main",
      detected_at: "2026-08-25T11:40:00Z",
      authors: ["Carol", "Dan"],
      stats: { commits: 2, files: 4, insertions: 12, deletions: 3 },
    },
    {
      id: 5,
      repository_id: 7,
      branch_id: 12,
      repository: "app",
      branch: "release",
      detected_at: "2026-08-23T11:00:00Z",
      authors: ["Old"],
    },
  ], {
    nowMs,
    rangeHours: 24,
    paddingX: 100,
    pixelsPerHour: 10,
    minimumTimelineWidth: 320,
  });

  assert.equal(model.repositories.length, 2, "numeric repository identity wins over duplicate labels");
  assert.deepEqual(model.repositories.map((repository) => repository.id), [7, 9], "lane order stays identity-stable as newer events arrive");
  assert.equal(model.lanes.length, 2);
  assert.equal(model.hiddenCount, 1);
  assert.deepEqual(model.events.map((item) => item.id), [4, 3, 2, 1]);

  const main = model.lanes.find((lane) => lane.repositoryKey === "repository-id:7");
  assert.deepEqual(main.events.map((item) => item.id), [1, 2, 3]);
  assert.ok(main.events[0].x < main.events[1].x);
  assert.ok(main.events[1].x < main.events[2].x);
  assert.deepEqual(main.authorSegments.map((segment) => segment.authors), [["Alice"], ["Bob"]]);
  assert.deepEqual(main.authorSegments[0].eventIds, [1, 2]);
  assert.equal(main.authorSegments[0].commitCount, 3);
  assert.equal(main.authorSegments[0].stats.changes, 36);
  assert.equal(main.authorSegments[0].statsScope, "events");

  const mixed = model.lanes.find((lane) => lane.repositoryKey === "repository-id:9")
    .authorSegments[0];
  assert.equal(mixed.mixed, true);
  assert.equal(mixed.statsScope, "events", "event totals are not presented as per-author attribution");
  assert.ok(model.bounds.width > model.latestX);
  assert.ok(model.bounds.height > 0);
});

test("buildBranchCanvasModel plots reliable tip commit time while preserving detection time", () => {
  const detectedAt = "2026-08-25T11:30:00Z";
  const tipCommittedAt = "2026-08-25T08:15:00Z";
  const model = buildBranchCanvasModel([{
    id: 1,
    repository: "app",
    branch: "main",
    detected_at: detectedAt,
    timeline: {
      basis: "tip_commit",
      tip_committed_at: tipCommittedAt,
      first_committed_at: "2026-08-25T07:45:00Z",
      last_committed_at: tipCommittedAt,
      range_complete: true,
    },
  }, {
    id: 2,
    repository: "app",
    branch: "main",
    detected_at: "2026-08-25T09:00:00Z",
  }], {
    nowMs: Date.parse("2026-08-25T12:00:00Z"),
    rangeHours: 24,
  });

  const actual = model.events.find((item) => item.id === 1);
  const detectionFallback = model.events.find((item) => item.id === 2);
  assert.equal(actual.detectedTimeMs, Date.parse(detectedAt));
  assert.equal(actual.timeMs, Date.parse(tipCommittedAt));
  assert.equal(actual.plottedTimeMs, Date.parse(tipCommittedAt));
  assert.equal(actual.timeBasis, "tip_commit");
  assert.equal(actual.firstCommittedAtMs, Date.parse("2026-08-25T07:45:00Z"));
  assert.equal(actual.lastCommittedAtMs, Date.parse(tipCommittedAt));
  assert.equal(actual.commitRangeComplete, true);
  assert.ok(actual.x < detectionFallback.x, "the event follows commit time, not its later detection time");
});

test("buildBranchCanvasModel falls back to detection for unsupported or invalid tip timelines", () => {
  const detectedAt = "2026-08-25T10:00:00Z";
  const model = buildBranchCanvasModel([{
    id: 1,
    repository: "app",
    branch: "invalid-tip",
    detected_at: detectedAt,
    timeline: { basis: "tip_commit", tip_committed_at: "not-a-time" },
  }, {
    id: 2,
    repository: "app",
    branch: "unsupported-basis",
    detected_at: detectedAt,
    timeline: { basis: "detected", tip_committed_at: "2026-08-24T10:00:00Z" },
  }], {
    nowMs: Date.parse("2026-08-25T12:00:00Z"),
    rangeHours: 24,
  });

  model.events.forEach((item) => {
    assert.equal(item.timeMs, Date.parse(detectedAt));
    assert.equal(item.detectedTimeMs, Date.parse(detectedAt));
    assert.equal(item.timeBasis, "detected");
  });
});

test("buildBranchCanvasModel keeps old commits discovered inside the detection window", () => {
  const nowMs = Date.parse("2026-08-25T12:00:00Z");
  const model = buildBranchCanvasModel([{
    id: 1,
    repository: "app",
    branch: "main",
    detected_at: "2026-08-25T11:59:00Z",
    timeline: {
      basis: "tip_commit",
      tip_committed_at: "2020-01-01T00:00:00Z",
    },
  }, {
    id: 2,
    repository: "app",
    branch: "hidden-even-with-new-tip",
    detected_at: "2026-08-23T11:59:00Z",
    timeline: {
      basis: "tip_commit",
      tip_committed_at: "2026-08-25T11:58:00Z",
    },
  }], { nowMs, rangeHours: 24, paddingX: 100 });

  assert.deepEqual(model.events.map((item) => item.id), [1]);
  assert.equal(model.hiddenCount, 1, "the selected window is evaluated on detection time");
  assert.equal(model.events[0].timeBasis, "tip_commit");
  assert.equal(model.events[0].outsideRequestedRange, true);
  assert.equal(model.events[0].timeClamped, true);
  assert.equal(model.events[0].x, 100);
  assert.equal(model.axisStartMs, nowMs - (366 * 24 * 60 * 60 * 1000));
  assert.ok(model.bounds.width <= 12000 + 200, "an absurd commit clock cannot create an unbounded world");
});

test("buildBranchCanvasModel bounds implausible future commit clocks", () => {
  const nowMs = Date.parse("2026-08-25T12:00:00Z");
  const model = buildBranchCanvasModel([{
    id: 1,
    repository: "app",
    branch: "future-clock",
    detected_at: "2026-08-25T11:59:00Z",
    timeline: { basis: "tip_commit", tip_committed_at: "2099-01-01T00:00:00Z" },
  }], { nowMs, rangeHours: 24 });
  const item = model.events[0];

  assert.equal(model.axisEndMs, nowMs + (24 * 60 * 60 * 1000));
  assert.equal(item.plottedTimeMs, model.axisEndMs);
  assert.equal(item.timeClamped, true);
  assert.equal(item.outsideRequestedRange, true);
  assert.equal(item.x, model.latestX);
});

test("buildBranchCanvasModel positions author ranges by commit time with honest fallbacks", () => {
  const model = buildBranchCanvasModel([{
    id: 1,
    repository: "app",
    branch: "main",
    detected_at: "2026-08-25T11:30:00Z",
    authors: ["Alice"],
    stats: { commits: 3, files: 4, insertions: 20, deletions: 5 },
    timeline: {
      basis: "tip_commit",
      tip_committed_at: "2026-08-25T11:00:00Z",
      authors: [{
        name: "Alice",
        first_committed_at: "2026-08-25T08:00:00Z",
        last_committed_at: "2026-08-25T10:00:00Z",
        sampled_commits: 3,
      }],
    },
  }, {
    id: 2,
    repository: "app",
    branch: "main",
    detected_at: "2026-08-25T11:45:00Z",
    authors: ["Bob"],
    stats: { commits: 1, files: 1, insertions: 2, deletions: 1 },
    timeline: {
      basis: "tip_commit",
      tip_committed_at: "2026-08-25T11:40:00Z",
      authors: [{
        name: "Bob",
        first_committed_at: "invalid",
        last_committed_at: "2026-08-25T11:35:00Z",
        sampled_commits: 1,
      }],
    },
  }], {
    nowMs: Date.parse("2026-08-25T12:00:00Z"),
    rangeHours: 24,
  });

  const [aliceSegment, bobSegment] = model.lanes[0].authorSegments;
  const aliceRange = aliceSegment.authorRanges[0];
  const bobRange = bobSegment.authorRanges[0];
  const aliceEvent = model.events.find((item) => item.id === 1);
  const bobEvent = model.events.find((item) => item.id === 2);
  assert.equal(aliceRange.usesCommitTimeline, true);
  assert.equal(aliceRange.startMs, Date.parse("2026-08-25T08:00:00Z"));
  assert.equal(aliceRange.endMs, Date.parse("2026-08-25T10:00:00Z"));
  assert.equal(aliceRange.sampledCommits, 3);
  assert.equal(aliceRange.fallbackBasis, null);
  assert.ok(aliceRange.startX < aliceRange.endX);
  assert.ok(aliceRange.centerX < aliceEvent.x);
  assert.equal(aliceSegment.statsScope, "events");
  assert.equal(aliceSegment.stats.changes, 25, "line totals remain scoped to the event");

  assert.equal(bobRange.usesCommitTimeline, false, "a partial invalid range is not presented as fact");
  assert.equal(bobRange.fallbackBasis, "tip_commit");
  assert.equal(bobRange.startX, bobEvent.x);
  assert.equal(bobRange.endX, bobEvent.x);
});

test("buildBranchCanvasModel draws cross-branch edges only from unique explicit topology", () => {
  const oidA = "a".repeat(40);
  const oidB = "b".repeat(40);
  const oidC = "c".repeat(40);
  const nowMs = Date.parse("2026-08-25T12:00:00Z");
  const model = buildBranchCanvasModel([
    {
      id: 1,
      repository_id: 1,
      branch_id: 10,
      repository: "app",
      branch: "main",
      detected_at: "2026-08-25T10:00:00Z",
      from_oid: oidA,
      to_oid: oidB,
    },
    {
      id: 2,
      repository_id: 1,
      branch_id: 11,
      repository: "app",
      branch: "feature",
      detected_at: "2026-08-25T11:00:00Z",
      topology: { from_oid: oidB, to_oid: oidC },
    },
    {
      id: 3,
      repository_id: 1,
      branch_id: 12,
      repository: "app",
      branch: "nearby-but-unproven",
      detected_at: "2026-08-25T11:00:01Z",
    },
    {
      id: 4,
      repository_id: 2,
      branch_id: 20,
      repository: "other",
      branch: "feature",
      detected_at: "2026-08-25T11:10:00Z",
      from_oid: oidB,
      to_oid: oidC,
    },
  ], { nowMs, rangeHours: 24 });

  assert.equal(model.edges.length, 1);
  assert.deepEqual(
    [model.edges[0].fromEventId, model.edges[0].toEventId],
    [1, 2],
  );
  assert.notEqual(model.edges[0].fromLaneKey, model.edges[0].toLaneKey);
  assert.equal(model.edges[0].repositoryKey, "repository-id:1");
  assert.ok(model.edges[0].from.x < model.edges[0].to.x);
  assert.equal(model.ambiguousEdgeCount, 0);
});

test("buildBranchCanvasModel keeps explicit ancestry when commit clocks run backward", () => {
  const shared = "b".repeat(40);
  const model = buildBranchCanvasModel([{
    id: 1,
    repository_id: 1,
    branch_id: 10,
    repository: "app",
    branch: "main",
    detected_at: "2026-08-25T10:00:00Z",
    to_oid: shared,
    timeline: { basis: "tip_commit", tip_committed_at: "2026-08-25T11:00:00Z" },
  }, {
    id: 2,
    repository_id: 1,
    branch_id: 11,
    repository: "app",
    branch: "feature",
    detected_at: "2026-08-25T10:30:00Z",
    topology: { from_oid: shared, to_oid: "c".repeat(40) },
    timeline: { basis: "tip_commit", tip_committed_at: "2026-08-25T09:00:00Z" },
  }], { nowMs: Date.parse("2026-08-25T12:00:00Z"), rangeHours: 24 });

  assert.equal(model.edges.length, 1);
  assert.deepEqual(
    [model.edges[0].fromEventId, model.edges[0].toEventId],
    [1, 2],
  );
  assert.ok(model.edges[0].from.x > model.edges[0].to.x, "clock skew stays visible instead of erasing verified topology");
});

test("buildBranchCanvasModel refuses ambiguous or same-lane ancestry guesses", () => {
  const shared = "d".repeat(40);
  const sameLane = "f".repeat(40);
  const sharedWithOwnLane = "1".repeat(40);
  const model = buildBranchCanvasModel([
    { id: 1, repository_id: 1, branch_id: 1, repository: "app", branch: "main", detected_at: "2026-08-25T09:00:00Z", to_oid: shared },
    { id: 2, repository_id: 1, branch_id: 2, repository: "app", branch: "release", detected_at: "2026-08-25T09:30:00Z", to_oid: shared },
    { id: 3, repository_id: 1, branch_id: 3, repository: "app", branch: "feature", detected_at: "2026-08-25T10:00:00Z", from_oid: shared, to_oid: "e".repeat(40) },
    { id: 4, repository_id: 1, branch_id: 1, repository: "app", branch: "main", detected_at: "2026-08-25T10:30:00Z", to_oid: sameLane },
    { id: 5, repository_id: 1, branch_id: 1, repository: "app", branch: "main", detected_at: "2026-08-25T11:00:00Z", from_oid: sameLane, to_oid: "0".repeat(40) },
    { id: 6, repository_id: 1, branch_id: 1, repository: "app", branch: "main", detected_at: "2026-08-25T11:05:00Z", to_oid: sharedWithOwnLane },
    { id: 7, repository_id: 1, branch_id: 2, repository: "app", branch: "release", detected_at: "2026-08-25T11:06:00Z", to_oid: sharedWithOwnLane },
    { id: 8, repository_id: 1, branch_id: 1, repository: "app", branch: "main", detected_at: "2026-08-25T11:10:00Z", from_oid: sharedWithOwnLane, to_oid: "2".repeat(40) },
  ], { nowMs: Date.parse("2026-08-25T12:00:00Z"), rangeHours: 24 });

  assert.deepEqual(model.edges, []);
  assert.equal(model.ambiguousEdgeCount, 2);
});

test("canvas zoom clamps, uses semantic levels, and keeps threshold hysteresis", () => {
  assert.equal(clampCanvasZoom(0), 1);
  assert.equal(clampCanvasZoom(0.01), 0.08);
  assert.equal(clampCanvasZoom(99), 4);
  assert.equal(clampCanvasZoom(3, { min: 2, max: 1 }), 2);

  assert.equal(canvasLod(0.5), "overview");
  assert.equal(canvasLod(0.9), "authors");
  assert.equal(canvasLod(1.8), "details");
  assert.equal(canvasLod(0.75, "overview"), "overview", "small threshold noise does not reveal authors");
  assert.equal(canvasLod(0.81, "overview"), "authors");
  assert.equal(canvasLod(0.68, "authors"), "authors", "small threshold noise does not hide authors");
  assert.equal(canvasLod(0.63, "authors"), "overview");
  assert.equal(canvasLod(1.36, "details"), "details", "detail labels remain stable inside hysteresis");
  assert.equal(canvasLod(1.33, "details"), "authors");
});

test("zoomAroundPoint preserves the world coordinate beneath its anchor", () => {
  const current = { x: -200, y: -100, zoom: 1 };
  const anchor = { x: 400, y: 300 };
  const next = zoomAroundPoint(current, 2, anchor);
  assert.deepEqual(next, { x: -800, y: -500, zoom: 2 });
  assert.equal((anchor.x - current.x) / current.zoom, (anchor.x - next.x) / next.zoom);
  assert.equal((anchor.y - current.y) / current.zoom, (anchor.y - next.y) / next.zoom);

  const clamped = zoomAroundPoint(current, 100, anchor);
  assert.equal(clamped.zoom, 4);
  assert.equal((anchor.x - clamped.x) / clamped.zoom, 600);
});

test("fit and latest canvas transforms are finite, centered, and align the newest edge", () => {
  const bounds = { x: 0, y: 0, width: 1_000, height: 500 };
  const viewport = { width: 500, height: 300 };
  const fit = fitCanvasTransform(bounds, viewport, { padding: 20, min: 0.1, max: 4 });
  assert.equal(fit.zoom, 0.46);
  assert.equal(fit.x, 20);
  assert.equal(fit.y, 35);

  const latest = latestCanvasTransform(bounds, viewport, {
    latestX: 900,
    zoom: 1,
    padding: 20,
  });
  assert.deepEqual(latest, { x: -420, y: -100, zoom: 1 });
  assert.equal((900 * latest.zoom) + latest.x, 480);

  const anchored = latestCanvasTransform(bounds, viewport, {
    latestX: 900,
    targetY: 400,
    zoom: 1,
    padding: 20,
  });
  assert.deepEqual(anchored, { x: -420, y: -250, zoom: 1 });
  assert.equal((400 * anchored.zoom) + anchored.y, viewport.height / 2);

  for (const value of Object.values(fitCanvasTransform({}, {}))) {
    assert.equal(Number.isFinite(value), true);
  }
});

test("shouldHandleShortcut accepts global commands outside editable UI", () => {
  for (const key of ["/", "r", "j", "k"]) {
    assert.equal(shouldHandleShortcut(shortcutEvent(key), shortcutContext()), true, key);
  }
  const help = shortcutEvent("?");
  help.shiftKey = true;
  assert.equal(shouldHandleShortcut(help, shortcutContext()), true, "shift+?");
  assert.equal(shouldHandleShortcut(shortcutEvent("x"), shortcutContext()), false);
});

test("shouldHandleShortcut protects native editing and layered interactions", () => {
  const editableTargets = [
    { tagName: "INPUT", isContentEditable: false },
    { tagName: "TEXTAREA", isContentEditable: false },
    { tagName: "SELECT", isContentEditable: false },
    { tagName: "DIV", isContentEditable: true },
    { tagName: "SPAN", isContentEditable: false, closest: () => ({}) },
  ];
  for (const target of editableTargets) {
    assert.equal(
      shouldHandleShortcut(shortcutEvent("r", target), shortcutContext()),
      false,
      target.tagName,
    );
  }

  assert.equal(
    shouldHandleShortcut(shortcutEvent("r"), shortcutContext({ editable: true })),
    false,
  );
  assert.equal(
    shouldHandleShortcut(shortcutEvent("r"), shortcutContext({ dialogOpen: true })),
    false,
  );
  assert.equal(
    shouldHandleShortcut(shortcutEvent("r"), shortcutContext({ menuOpen: true })),
    false,
  );

  for (const modifier of ["altKey", "ctrlKey", "metaKey", "shiftKey"]) {
    const event = shortcutEvent("r");
    event[modifier] = true;
    assert.equal(shouldHandleShortcut(event, shortcutContext()), false, modifier);
  }

  const repeated = shortcutEvent("r");
  repeated.repeat = true;
  assert.equal(shouldHandleShortcut(repeated, shortcutContext()), false, "repeat");

  const composing = shortcutEvent("r");
  composing.isComposing = true;
  assert.equal(shouldHandleShortcut(composing, shortcutContext()), false, "composing");

  const prevented = shortcutEvent("r");
  prevented.defaultPrevented = true;
  assert.equal(shouldHandleShortcut(prevented, shortcutContext()), false, "prevented");
});

test("moveEventSelection moves, wraps, and tolerates an empty or stale selection", () => {
  const ids = [11, 12, 13];
  assert.equal(moveEventSelection(ids, 12, 1), 13);
  assert.equal(moveEventSelection(ids, 12, -1), 11);
  assert.equal(moveEventSelection(ids, 13, 1), 11);
  assert.equal(moveEventSelection(ids, 11, -1), 13);
  assert.equal(moveEventSelection(ids, 13, 1, { wrap: false }), 13);
  assert.equal(moveEventSelection(ids, 11, -1, { wrap: false }), 11);
  assert.equal(moveEventSelection(ids, 999, 1), 11);
  assert.equal(moveEventSelection(ids, 999, -1), 13);
  assert.equal(moveEventSelection([], null, 1), null);
});

test("moveListboxActiveIndex supports bounded arrow and edge navigation", () => {
  assert.equal(moveListboxActiveIndex(3, 1, "ArrowDown"), 2);
  assert.equal(moveListboxActiveIndex(3, 2, "ArrowDown"), 2);
  assert.equal(moveListboxActiveIndex(3, 1, "ArrowUp"), 0);
  assert.equal(moveListboxActiveIndex(3, 0, "ArrowUp"), 0);
  assert.equal(moveListboxActiveIndex(3, 1, "Home"), 0);
  assert.equal(moveListboxActiveIndex(3, 1, "End"), 2);
  assert.equal(moveListboxActiveIndex(3, -10, "noop"), 0);
  assert.equal(moveListboxActiveIndex(0, 0, "ArrowDown"), null);
});

test("normalizeCheckRunSummary provides a deterministic persisted-run view", () => {
  const runId = "ffeeddccbbaa99887766554433221100";
  const run = normalizeCheckRunSummary({
    id: runId,
    status: "completed_with_errors",
    actionable: true,
    pending: true,
    started_at: "2026-08-21T02:00:00Z",
    finished_at: "2026-08-21T02:00:03.250Z",
    acknowledged_at: null,
    scope: {
      kind: "branches",
      repository_id: 9,
      requested_branch_ids: [101, 102, 103, 104],
      actual_branch_ids: [101, 102, 103, 104],
    },
    options: { publish: true, use_codex: false },
    counts: {
      requested: 4,
      checked: 4,
      changed: 2,
      unchanged: 1,
      baselines: 0,
      blocked: 1,
      errors: 0,
      events: 2,
    },
    event_ids: [31, 32, 31, null],
    publication: {
      requested: 2,
      succeeded: 1,
      failed: 1,
      unknown: 0,
      truncated: true,
      backlog: { succeeded: 3, failed: 1, unknown: 2 },
    },
    summary: {
      title: "本轮有两项变化",
      overview: "API 与部署配置同时变化。",
      themes: ["API", "部署", "API"],
      priorities: ["先确认兼容性"],
      connections: ["两个仓库共同调整接口"],
      completeness_note: "1 个分支被阻塞。",
      source: "rules",
    },
    summary_warning: "综述未覆盖被阻塞分支",
  });

  assert.equal(run.id, runId);
  assert.equal(run.status, "completed_with_errors");
  assert.equal(run.actionable, true);
  assert.equal(run.isPending, true);
  assert.equal(run.startedAt, "2026-08-21T02:00:00Z");
  assert.equal(run.finishedAt, "2026-08-21T02:00:03.250Z");
  assert.equal(run.durationMs, 3_250);
  assert.deepEqual(run.scope, {
    kind: "branches",
    repositoryId: 9,
    requestedBranchIds: [101, 102, 103, 104],
    actualBranchIds: [101, 102, 103, 104],
  });
  assert.deepEqual(run.options, { fetch: false, useCodex: false, publish: true });
  assert.deepEqual(run.counts, {
    requested: 4,
    checked: 4,
    changed: 2,
    unchanged: 1,
    baselines: 0,
    blocked: 1,
    errors: 0,
    events: 2,
  });
  assert.deepEqual(run.eventIds, [31, 32]);
  assert.deepEqual(run.publication, {
    requested: 2,
    succeeded: 1,
    failed: 1,
    unknown: 0,
    truncated: true,
    backlog: { succeeded: 3, failed: 1, unknown: 2 },
  });
  assert.deepEqual(run.summary.themes, ["API", "部署"]);
  assert.equal(run.summary.completenessNote, "1 个分支被阻塞。");
  assert.equal(run.summaryWarning, "综述未覆盖被阻塞分支");
});

test("normalizeCheckRunSummary contains malformed optional data", () => {
  const run = normalizeCheckRunSummary({
    id: "run-1",
    status: "running",
    started_at: "not-a-date",
    finished_at: "2026-08-21T02:00:00Z",
    counts: { checked: -2, changed: Number.POSITIVE_INFINITY },
    event_ids: "31",
    publication: null,
    summary: { title: "", themes: "API" },
    error: { message: "must not leak object values" },
  });

  assert.equal(run.durationMs, null);
  assert.equal(run.counts.checked, 0);
  assert.equal(run.counts.changed, 0);
  assert.deepEqual(run.eventIds, []);
  assert.deepEqual(run.publication, {
    requested: 0,
    succeeded: 0,
    failed: 0,
    unknown: 0,
    truncated: false,
    backlog: { succeeded: 0, failed: 0, unknown: 0 },
  });
  assert.equal(run.summary, null);
  assert.equal(run.error, "");
});

test("normalizeCheckRunSummary retains the backend-redacted failure message", () => {
  const run = normalizeCheckRunSummary({
    id: "abcdefabcdefabcdefabcdefabcdefab",
    status: "failed",
    finished_at: "2026-08-21T02:00:00Z",
    error: " 远程连接失败 ",
  });

  assert.equal(run.status, "failed");
  assert.equal(run.error, "远程连接失败");
});

test("normalizeCheckRunSummary preserves historical publication backlog idempotently", () => {
  const run = normalizeCheckRunSummary({
    id: "0123456789abcdef0123456789abcdef",
    status: "reconcile_required",
    publication: {
      backlog: { succeeded: 4, failed: 2, unknown: 1 },
    },
  });

  assert.deepEqual(run.publication.backlog, { succeeded: 4, failed: 2, unknown: 1 });
  assert.deepEqual(
    normalizeCheckRunSummary(run).publication.backlog,
    { succeeded: 4, failed: 2, unknown: 1 },
  );
});

test("normalizeCheckRunSummary does not mark a quiet latest run as pending", () => {
  const run = normalizeCheckRunSummary({
    id: "11112222333344445555666677778888",
    status: "succeeded",
    actionable: false,
    pending: false,
    acknowledged_at: null,
    counts: { checked: 12, unchanged: 12, events: 0 },
  });

  assert.equal(run.actionable, false);
  assert.equal(run.isPending, false);
});

test("normalizeCheckRunSummary preserves totals when event references are truncated", () => {
  const run = normalizeCheckRunSummary({
    id: "00112233445566778899aabbccddeeff",
    status: "succeeded",
    acknowledged_at: null,
    counts: { events: 205 },
    event_ids: [501],
    events: [{ id: 501, repository: "api", branch: "main", title: "更新接口" }],
    events_truncated: true,
  });

  assert.equal(run.id, "00112233445566778899aabbccddeeff");
  assert.equal(run.counts.events, 205);
  assert.equal(run.events.length, 1);
  assert.deepEqual(run.eventIds, [501]);
  assert.equal(run.eventsTruncated, true);
});

test("normalizeCheckRunSummary bounds blocked branch details for the result panel", () => {
  const run = normalizeCheckRunSummary({
    id: "99990000aaaabbbbccccddddeeeeffff",
    status: "completed_with_errors",
    actionable: true,
    pending: true,
    blocked: [
      {
        branch_id: 71,
        repository_id: 7,
        repository: " api ",
        branch: " main ",
        status: "blocked",
        error: " 远程引用读取超时 ",
      },
      null,
      {},
    ],
    blocked_truncated: true,
  });

  assert.deepEqual(run.blocked, [{
    branchId: 71,
    repositoryId: 7,
    repository: "api",
    branch: "main",
    status: "blocked",
    error: "远程引用读取超时",
  }]);
  assert.equal(run.blockedTruncated, true);
});

test("isEventNewForRun respects authoritative flags and run-membership fallback", () => {
  const run = normalizeCheckRunSummary({
    id: 17,
    status: "succeeded",
    event_ids: [31, 32],
    acknowledged_at: null,
  });
  assert.equal(isEventNewForRun(32, run), true);
  assert.equal(isEventNewForRun({ id: 31, is_new: true }, run), true);
  assert.equal(isEventNewForRun({ id: 31, is_new: false }, run), false);
  assert.equal(isEventNewForRun({ id: 99, is_new: true }, run), true);
  assert.equal(isEventNewForRun({ id: 99 }, run), false);
  assert.equal(isEventNewForRun({ id: 99, check_run_id: 17 }, run), true);

  const acknowledged = normalizeCheckRunSummary({
    id: 17,
    status: "succeeded",
    event_ids: [31],
    acknowledged_at: "2026-08-21T02:05:00Z",
  });
  assert.equal(acknowledged.isPending, false);
  assert.equal(isEventNewForRun({ id: 31 }, acknowledged), false);
});

test("isEventNewForRun keeps fallback membership isolated across pending runs", () => {
  const first = normalizeCheckRunSummary({
    id: 20,
    status: "succeeded",
    event_ids: [41, 42],
    acknowledged_at: null,
  });
  const second = normalizeCheckRunSummary({
    id: 21,
    status: "succeeded",
    event_ids: [43],
    acknowledged_at: null,
  });

  const event = { id: 43, check_run_id: 21, is_new: true };
  assert.equal(isEventNewForRun(event, first), true);
  assert.equal(isEventNewForRun(event, second), true);

  const fallbackEvent = { id: 43, check_run_id: 21 };
  assert.equal(isEventNewForRun(fallbackEvent, first), false);
  assert.equal(isEventNewForRun(fallbackEvent, second), true);
});

test("i18n registers static copy in Chinese, switches both ways, and skips user data", () => {
  const previousDocument = global.document;
  const previousNodeFilter = global.NodeFilter;
  const previousStorage = global.localStorage;
  const stored = new Map();
  const attributes = new Map([["placeholder", "搜索事件"], ["aria-label", "搜索仓库、分支、贡献者或摘要"]]);
  const regularParent = { closest: () => null };
  const skippedParent = { closest: (selector) => selector.includes("data-i18n-skip") ? skippedParent : null };
  const textNode = { nodeType: 3, data: "  正在查看 ", parentElement: regularParent };
  const userTextNode = { nodeType: 3, data: "设置", parentElement: skippedParent };
  const input = {
    nodeType: 1,
    ownerDocument: null,
    hasAttribute: (name) => attributes.has(name),
    getAttribute: (name) => attributes.get(name),
    setAttribute: (name, value) => attributes.set(name, value),
    closest: () => null,
  };
  const nodes = [textNode, input, userTextNode];
  const fakeDocument = {
    nodeType: 9,
    documentElement: {},
    querySelectorAll: () => [],
    createTreeWalker: () => {
      let index = 0;
      return { nextNode: () => nodes[index++] || null };
    },
  };
  input.ownerDocument = fakeDocument;
  global.document = fakeDocument;
  global.NodeFilter = { SHOW_ELEMENT: 1, SHOW_TEXT: 4 };
  global.localStorage = {
    getItem: (key) => stored.get(key) || null,
    setItem: (key, value) => stored.set(key, value),
  };

  try {
    i18n.setLocale("zh-CN", { persist: false, announce: false });
    i18n.registerDOM(fakeDocument);
    i18n.setLocale("en", { announce: false });
    assert.equal(fakeDocument.documentElement.lang, "en");
    assert.equal(textNode.data, "  Viewing ");
    assert.equal(attributes.get("placeholder"), "Search events");
    assert.equal(attributes.get("aria-label"), "Search repositories, branches, contributors, or summaries");
    assert.equal(userTextNode.data, "设置", "user-provided text stays raw inside data-i18n-skip");
    assert.equal(stored.get(i18n.STORAGE_KEY), "en");

    i18n.setLocale("zh-CN", { persist: false, announce: false });
    assert.equal(textNode.data, "  正在查看 ");
    assert.equal(attributes.get("placeholder"), "搜索事件");
    assert.equal(attributes.get("aria-label"), "搜索仓库、分支、贡献者或摘要");
    assert.equal(userTextNode.data, "设置");
  } finally {
    i18n.setLocale("zh-CN", { persist: false, announce: false });
    global.document = previousDocument;
    global.NodeFilter = previousNodeFilter;
    global.localStorage = previousStorage;
  }
});

test("i18n prefers a stored choice and otherwise follows the browser language", () => {
  assert.equal(i18n.detectLocale("zh-CN", "en-US"), "zh-CN");
  assert.equal(i18n.detectLocale(null, "en-GB"), "en");
  assert.equal(i18n.detectLocale(null, "zh-Hans-CN"), "zh-CN");
});

test("i18n translates controlled dynamic copy while preserving interpolated user data", () => {
  i18n.setLocale("en", { persist: false, announce: false });
  assert.equal(i18n.t("  设置 "), "  Settings ");
  assert.equal(
    i18n.t("选择 {repository} 的分支", { repository: "设置" }),
    "Choose branches for 设置",
  );
  const controlled = [
    "正在克隆仓库",
    "选择扫描目录",
    "克隆失败",
    "克隆任务记录已失效，请重新粘贴仓库地址后重试。",
    "未找到本地 checkout，自动克隆目前仅支持 GitHub。",
    "只扫描已配置的 {count} 个目录。",
    "事件详情不可用",
    "已有任务正在运行，请等待当前任务完成。",
    "分支信号场",
    "本轮新事件",
    "当前时间范围内没有变化",
    "最近 7 天没有匹配的变化；可调整筛选，或等待新的变化。",
    "查看完整分析",
    "分支演化画布",
    "画布时间范围",
    "专注画布",
    "适应全部",
    "基于当前已加载记录",
    "作者变化",
    "{count} 个变化段",
    "最近 {shown} / 共 {total} 个变化段",
    "{count} 个变化段中的修改",
    "最近 {shown} / 共 {total} 个变化段中的修改",
    "{count} 条分支轨道",
    "{repository} 分支变化",
    "小地图；点击移动当前视口",
    "当前详情中没有可展示的作者提交；历史可能已被裁剪。",
    "缩放级别 {level}",
    "展开 {author} 的修改",
    "收起 {author} 的修改",
    "退出专注模式",
    "检测时间从左到右。沿真实证据展开枝杈，缩放时逐步显露作者与修改。",
    "拖动或滚轮浏览；捏合、Ctrl + 滚轮或加减键缩放。画布仅依据当前已加载记录，证据不足处不会猜测连线。",
    "事件整体共 {files} 个文件，新增 {insertions} 行、删除 {deletions} 行；这些总量不归因给单个作者。",
  ];
  controlled.forEach((key) => assert.doesNotMatch(i18n.t(key), /[\u3400-\u9fff]/u, key));
  i18n.setLocale("zh-CN", { persist: false, announce: false });
});

test("clone task shows Git's actual phase percentage without inventing overall progress", () => {
  const appSource = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");
  const start = appSource.indexOf("function cloneActualProgressPercent(");
  const end = appSource.indexOf("\n\nfunction cloneTaskRepository(", start);
  assert.ok(start >= 0 && end > start);
  const source = appSource.slice(start, end);
  const actualProgress = Function(`"use strict"; ${source}; return cloneActualProgressPercent;`)();
  assert.equal(actualProgress({ status: "running", progress: { phase: "download", percent: 73 } }), 73);
  assert.equal(actualProgress({ status: "running", progress: { phase: "verify", percent: 12 } }), 12);
  assert.equal(actualProgress({ status: "succeeded", progress: {} }), 100);
  assert.equal(actualProgress({ status: "running", progress: {} }), null);
  assert.equal(actualProgress({ status: "running", progress: { percent: 140 } }), 100);
});

test("branch picker keeps current and default branches inside the 200 item cap", () => {
  const remoteBranches = Array.from({ length: 4_631 }, (_, index) => `refs/archive-${index}`);
  const values = prioritizeBranchNames(remoteBranches, ["main", "develop", "main"], 200);
  assert.equal(values.length, 200);
  assert.deepEqual(values.slice(0, 2), ["main", "develop"]);
  assert.equal(values.filter((value) => value === "main").length, 1);
  assert.equal(values.at(-1), "refs/archive-197");
});

test("unknown Chinese backend progress falls back to controlled English UI copy", () => {
  const appSource = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");
  const start = appSource.indexOf("function localizedProgressValue(");
  const end = appSource.indexOf("\n\nfunction localizedJobProgress(", start);
  assert.ok(start >= 0 && end > start);
  const source = appSource.slice(start, end);
  const controlled = Function("t", "i18n", `"use strict"; ${source}; return localizedProgressValue;`)(
    (key, variables) => i18n.t(key, variables),
    i18n,
  );
  i18n.setLocale("en", { persist: false, announce: false });
  assert.equal(
    controlled("核对第 1/2 个所选分支。", "已核对 {current}/{total} 个分支。", { current: 1, total: 2 }),
    "Checked 1/2 branches.",
  );
  assert.equal(controlled("Repository ready", "正在准备仓库"), "Repository ready");
  i18n.setLocale("zh-CN", { persist: false, announce: false });
});

test("frontend keeps data raw, loads i18n first, and exposes the safe clone contract", () => {
  const appSource = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");
  const html = fs.readFileSync(path.join(webRoot, "index.html"), "utf8");
  assert.match(appSource, /function element[\s\S]*?value\.textContent = String\(text\)/);
  assert.match(appSource, /const value = element\("button", className, label\)/);
  assert.match(appSource, /button\(tag, "repository-tag-option"/);
  assert.ok(html.indexOf("/assets/i18n.js") < html.indexOf("/assets/app.js"));
  assert.match(html, /data-language-toggle data-i18n-skip/);
  assert.match(appSource, /"\/api\/v1\/repository-clones"/);
  assert.match(appSource, /startCloneRequest\(\{[\s\S]*url: preview\.url,[\s\S]*parent: parentValue,[\s\S]*directoryName/);
  assert.match(appSource, /body: \{[\s\S]*url: submission\.url,[\s\S]*parent: submission\.parent,[\s\S]*directory_name: submission\.directoryName/);
  assert.match(appSource, /\/jobs\/\$\{encodeURIComponent\(job\.id\)\}\/cancel/);
  assert.match(appSource, /remoteHost === "github\.com"/);
  assert.match(appSource, /item\.canonical_remote\.toLowerCase\(\) === canonical\.toLowerCase\(\)/);
  assert.match(appSource, /CLONE_JOB_STORAGE_KEY/);
  assert.match(appSource, /async function reconcileStoredCloneJob\(\{ force = false \} = \{\}\)/);
  const rememberStart = appSource.indexOf("function rememberCloneJob(");
  const rememberEnd = appSource.indexOf("\n\nfunction clearStoredCloneJob(", rememberStart);
  assert.ok(rememberStart >= 0 && rememberEnd > rememberStart);
  assert.doesNotMatch(appSource.slice(rememberStart, rememberEnd), /\burl\b/, "clone recovery must not persist remote URLs");
  assert.ok((appSource.match(/cloneRequestInFlight\(\)/g) || []).length >= 4);
  assert.doesNotMatch(html, /id="clone-progress"/);
  assert.match(html, /id="task-details"/);
  assert.match(html, /id="task-cancel-button"/);
  assert.match(appSource, /if \(closeConfirmation && dialog\.open\) closeDialog\("clone-dialog"\)/);
  assert.match(appSource, /state\.cloneTerminalJob = job;[\s\S]*?rememberCloneJob\(job\);[\s\S]*?renderCloneTask\(job\)/);
  assert.match(appSource, /join\(i18n\.locale\(\) === "en" \? ", " : "、"\)/);
});

test("clone destination uses an accessible custom listbox without HTML path rendering", () => {
  const appSource = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");
  const html = fs.readFileSync(path.join(webRoot, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(webRoot, "app.css"), "utf8");
  const renderStart = appSource.indexOf("function renderCloneParentPicker(");
  const renderEnd = appSource.indexOf("\n\nfunction bindCloneParentPicker(", renderStart);
  const renderSource = appSource.slice(renderStart, renderEnd);

  assert.match(html, /id="clone-parent-trigger"[^>]*role="combobox"[^>]*aria-haspopup="listbox"[^>]*aria-expanded="false"[^>]*aria-controls="clone-parent-listbox"/);
  assert.match(html, /id="clone-parent-listbox"[^>]*role="listbox"[^>]*aria-labelledby="clone-parent-label"[^>]*hidden/);
  assert.match(html, /id="clone-parent"[^>]*tabindex="-1"[^>]*aria-hidden="true"/);
  assert.ok(renderStart >= 0 && renderEnd > renderStart);
  assert.doesNotMatch(renderSource, /innerHTML/);
  assert.match(renderSource, /element\("span", "clone-parent-option-path", root\)/);
  assert.match(renderSource, /option\.setAttribute\("aria-selected"/);
  assert.match(appSource, /\["ArrowDown", "ArrowUp", "Home", "End"\]/);
  assert.match(appSource, /\["Enter", " "\]/);
  assert.match(appSource, /event\.key === "Tab"[\s\S]*?closeCloneParentPicker\(\)/);
  assert.match(appSource, /event\.key === "Escape"[\s\S]*?event\.preventDefault\(\)[\s\S]*?event\.stopPropagation\(\)/);
  assert.match(appSource, /setAttribute\("aria-activedescendant", active\.id\)/);
  assert.match(appSource, /if \(!picker\.contains\(event\.target\)\) closeCloneParentPicker\(\)/);
  assert.match(css, /\.clone-parent-listbox \{[^}]*max-height:[^}]*overflow: auto/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
});

test("background task spinner remains geometrically centered in its state icon", () => {
  const css = fs.readFileSync(path.join(webRoot, "app.css"), "utf8");
  assert.match(css, /\.task-state-icon \{[^}]*display: grid;[^}]*place-items: center;/);
  assert.doesNotMatch(css, /\.task-bar strong, \.task-bar span \{[^}]*display: block;/);
  assert.match(css, /\.task-spinner \{[^}]*display: block;/);
});

test("monitoring settings expose one accessible bilingual GET and PUT contract", () => {
  const appSource = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");
  const html = fs.readFileSync(path.join(webRoot, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(webRoot, "app.css"), "utf8");

  assert.match(html, /id="monitoring-card"[^>]*aria-labelledby="monitoring-title"/);
  assert.match(html, /id="monitoring-form"[^>]*novalidate[^>]*aria-busy="true"/);
  assert.match(html, /id="monitoring-save-status"[^>]*role="status"[^>]*aria-live="polite"[^>]*tabindex="-1"/);
  assert.match(html, /id="monitoring-enabled"[^>]*type="checkbox"[^>]*aria-describedby="monitoring-enabled-copy"/);
  assert.match(html, /id="monitoring-notifications"[^>]*type="checkbox"[^>]*aria-describedby="monitoring-notifications-copy"/);
  assert.match(html, /id="monitoring-quiet-start"[^>]*type="time"[^>]*aria-describedby="monitoring-quiet-copy monitoring-quiet-error"/);
  assert.match(html, /id="monitoring-custom-interval"[^>]*hidden/);
  assert.match(html, /id="monitoring-frequency-error"[^>]*role="alert"/);
  for (const interval of [300, 900, 1800, 3600]) {
    assert.match(html, new RegExp(`name="monitoring-interval" value="${interval}"`));
  }

  assert.equal((appSource.match(/\/api\/v1\/monitoring/g) || []).length, 1, "the endpoint stays centralized");
  assert.match(appSource, /const monitoringApi = Object\.freeze\(\{[\s\S]*?read\(\)[\s\S]*?save\(payload\)[\s\S]*?submitJob\(MONITORING_ENDPOINT, payload,[\s\S]*?method: "PUT"/);
  assert.match(appSource, /if \(job\.kind === "monitoring"\)[\s\S]*?loadMonitoringSettings\(\{ force: true, preserveDraft \}\)/);
  assert.match(appSource, /interval_seconds: Number\.isSafeInteger\(interval\) && interval > 0 \? interval : MONITORING_DEFAULT_INTERVAL/);
  assert.match(appSource, /monitoringCustomIntervalCopy\(value\.interval_seconds\)/);
  assert.match(appSource, /notifications\.disabled = !available \|\| !state\.monitoring\?\.notifications_supported/);
  assert.match(appSource, /interval_seconds:[\s\S]*?notifications_enabled:[\s\S]*?quiet_hours:[\s\S]*?locale:/);
  assert.match(css, /\.switch-control \{[^}]*width: 52px;[^}]*height: 44px;/);
  assert.match(css, /@media \(max-width: 560px\)[\s\S]*?\.monitoring-frequency-options \{ grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/);

  i18n.setLocale("en", { persist: false, announce: false });
  assert.equal(i18n.t("主动监控"), "Active monitoring");
  assert.equal(i18n.t("保存监控设置"), "Save monitoring settings");
  assert.equal(i18n.t("勿扰时段"), "Quiet hours");
  assert.equal(i18n.t("正在应用监控设置"), "Applying monitoring settings");
  i18n.setLocale("zh-CN", { persist: false, announce: false });
});

test("recent activity renders as an accessible responsive branch evolution canvas", () => {
  const appSource = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");
  const html = fs.readFileSync(path.join(webRoot, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(webRoot, "app.css"), "utf8");

  assert.match(html, /class="signal-range-switch" role="group" aria-label="按检查时间筛选发现范围"/);
  assert.match(html, /data-signal-range="24"[^>]*aria-pressed="true"[^>]*aria-controls="activity-list"/);
  assert.match(html, /data-signal-range="168"[^>]*aria-pressed="false"[^>]*aria-controls="activity-list"/);
  assert.match(html, /id="activity-list" class="branch-canvas" role="region" aria-label="按提交时间优先排列的分支演化画布" aria-describedby="branch-canvas-hint"[^>]*aria-busy="true" tabindex="0"/);
  assert.match(html, /id="branch-author-inspector" aria-live="polite" hidden/);
  assert.match(html, /id="rail-collapse-button"[^>]*aria-expanded="true"[^>]*aria-controls="primary-navigation"/);
  for (const id of ["zoom-out", "zoom-in", "fit", "latest", "focus"]) {
    assert.match(html, new RegExp(`id="branch-canvas-${id}"`));
  }
  assert.match(html, /发现范围按检查时间筛选；横轴优先显示 Git 提交时间，取不到可靠时间时会明确标记并回退/);
  assert.match(appSource, /state\.canvasModel\.nowX \?\? state\.canvasModel\.latestX/);
  assert.match(appSource, /targetX: newestDiscovery\?\.x,[\s\S]*?targetY: newestDiscovery\?\.y/);

  assert.match(appSource, /uiModel\.buildBranchCanvasModel\(values, \{/);
  assert.match(appSource, /uiModel\.canvasLod\(transform\.zoom, previousLod\)/);
  assert.match(appSource, /uiModel\.zoomAroundPoint\(/);
  assert.match(appSource, /uiModel\.fitCanvasTransform\(/);
  assert.match(appSource, /uiModel\.latestCanvasTransform\(/);
  assert.match(appSource, /button\.setAttribute\("aria-controls", "branch-author-inspector"\)/);
  assert.match(appSource, /button\.setAttribute\("aria-expanded", expanded \? "true" : "false"\)/);
  assert.match(appSource, /segment\.eventCount > eventIds\.length[\s\S]*?最近 \{shown\} \/ 共 \{total\} 个变化段/);
  assert.match(appSource, /totalEvents > details\.length[\s\S]*?最近 \{shown\} \/ 共 \{total\} 个变化段中的修改/);
  assert.match(appSource, /querySelectorAll\("#activity-list \.branch-event-node\[data-event-id\]"\)/);
  assert.match(appSource, /sort\(\(left, right\) => Number\(left\.dataset\.eventOrder\) - Number\(right\.dataset\.eventOrder\)\)/);
  assert.match(appSource, /querySelectorAll\("\[data-signal-range\]"\)/);
  assert.match(appSource, /viewport\.addEventListener\("pointerdown"/);
  assert.match(appSource, /viewport\.addEventListener\("wheel"[\s\S]*?\{ passive: false \}\)/);
  assert.match(appSource, /localStorage\.setItem\(RAIL_COLLAPSED_STORAGE_KEY/);
  assert.match(appSource, /if \(state\.canvasFocused\)[\s\S]*?setCanvasFocus\(false\)/);
  assert.match(appSource, /这些总量不归因给单个作者/);

  assert.match(css, /\.canvas-icon-button, \.canvas-focus-button \{[^}]*min-width: 44px;[^}]*min-height: 44px;/);
  assert.match(css, /\.branch-canvas \{[^}]*overflow: hidden;[^}]*touch-action: none;/);
  assert.match(css, /\.branch-canvas\.is-lod-overview \.canvas-author-cluster/);
  assert.match(css, /\.branch-canvas\.is-lod-authors \.canvas-event-card/);
  assert.match(css, /\.branch-canvas\.is-lod-detail \.canvas-event-card \{ opacity: 1;/);
  assert.match(css, /body\.rail-collapsed \{ --rail-width: 76px;/);
  assert.match(css, /body\.canvas-focus \.side-rail,[^{]+\{ display: none !important;/);
  assert.match(css, /@media \(max-width: 560px\)[\s\S]*?\.branch-canvas-controls \{ display: grid;/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
});

test("changelog retains every historical release with complete English fields", () => {
  const changelog = JSON.parse(fs.readFileSync(path.join(webRoot, "changelog.json"), "utf8"));
  assert.deepEqual(changelog.releases.map((release) => release.date), [
    "2026-08-25",
    "2026-08-27",
    "2026-08-25",
    "2026-08-24",
    "2026-08-21",
    "2026-08-20",
    "2026-08-18",
  ]);
  assert.equal(changelog.releases.filter((release) => release.current).length, 1);
  assert.equal(changelog.releases[0].current, true);
  assert.equal(changelog.releases[0].version, "0.3.0");
  assert.equal(changelog.releases[0].entry_type, undefined);
  assert.equal(changelog.releases[1].version, "0.3.0");
  assert.equal(changelog.releases[1].entry_type, "update");
  assert.equal(changelog.releases[1].current, false);
  assert.equal(changelog.releases[2].version, "0.2.0");
  assert.equal(changelog.releases[2].entry_type, "update");
  assert.equal(changelog.releases[2].current, false);
  changelog.releases.forEach((release) => {
    assert.ok(release.headline_en, `${release.date} headline_en`);
    assert.ok(release.summary_en, `${release.date} summary_en`);
    release.changes.forEach((change) => {
      assert.ok(change.type_en, `${release.date} type_en`);
      assert.equal(change.items_en.length, change.items.length, `${release.date} items_en`);
      change.items_en.forEach((item) => assert.doesNotMatch(item, /[\u3400-\u9fff]/u));
    });
  });
});

test("branch and repository status report the worst reason, in a fixed order", () => {
  assert.deepEqual(branchStatus({ error: "boom", present: true }), ["检查失败", "is-error"]);
  // An errored branch is reported as failed even while its baseline is pending.
  assert.deepEqual(
    branchStatus({ error: "boom", baseline_pending: true, present: false }),
    ["检查失败", "is-error"],
  );
  assert.deepEqual(branchStatus({ baseline_pending: true, present: true }), ["等待基线", "is-pending"]);
  assert.deepEqual(branchStatus({ present: false }), ["分支不存在", "is-warning"]);
  assert.deepEqual(branchStatus({ present: true }), ["正常", ""]);
  assert.deepEqual(branchStatus(undefined), ["正常", ""]);

  assert.deepEqual(groupWatchStatus([{ present: true }, { present: false }]), ["需要处理", "is-error"]);
  assert.deepEqual(
    groupWatchStatus([{ present: true }, { present: true, baseline_pending: true }]),
    ["等待基线", "is-pending"],
  );
  assert.deepEqual(groupWatchStatus([{ present: true }]), ["正常", ""]);
  assert.deepEqual(groupWatchStatus(null), ["正常", ""]);
});

test("watch grouping keeps arrival order and separates repositories without ids", () => {
  const groups = groupWatches([
    { repository_id: 2, repository: "api", branch: "main", repository_tags: ["core"] },
    { repository_id: 1, repository: "web", branch: "main" },
    { repository_id: 2, repository: "api", branch: "release" },
    { repository: "loose-a", branch: "main" },
    { repository: "loose-b", branch: "main" },
  ]);
  assert.deepEqual(groups.map((group) => group.name), ["api", "web", "loose-a", "loose-b"]);
  assert.deepEqual(groups[0].watches.map((watch) => watch.branch), ["main", "release"]);
  assert.deepEqual(groups[0].tags, ["core"]);
  // A repository with no id still gets its own group rather than merging.
  assert.equal(groups[2].id, null);
  assert.notEqual(repositoryGroupKey(groups[2]), repositoryGroupKey(groups[3]));
  assert.equal(repositoryGroupKey(groups[0]), "id:2");
  assert.equal(repositoryDisplayName({ alias: "Platform", name: "api" }), "Platform");
  assert.equal(repositoryDisplayName({ name: "api" }), "api");
  assert.deepEqual(groupWatches(null), []);
});

test("recent-change keys use the supplied clock, not the wall clock", () => {
  const now = Date.parse("2026-08-25T12:00:00Z");
  const events = [
    { repository: "api", branch: "main", detected_at: "2026-08-25T11:00:00Z" },
    { repository: "web", branch: "main", detected_at: "2026-08-20T11:00:00Z" },
    { repository: "old", branch: "main", detected_at: "not a date" },
  ];
  const keys = recentEventWatchKeys(events, now);
  assert.ok(keys.has(watchKey("api", "main")));
  assert.ok(!keys.has(watchKey("web", "main")));
  assert.ok(!keys.has(watchKey("old", "main")));
  // Exactly on the boundary still counts as recent.
  assert.ok(recentEventWatchKeys(
    [{ repository: "edge", branch: "main", detected_at: "2026-08-24T12:00:00Z" }],
    now,
  ).has(watchKey("edge", "main")));
});

test("attention reasons rank ahead of recency and honour injected session state", () => {
  const recent = recentEventWatchKeys(
    [{ repository: "api", branch: "main", detected_at: "2026-08-25T11:00:00Z" }],
    Date.parse("2026-08-25T12:00:00Z"),
  );
  const healthy = { repository: "api", branch: "main", present: true };
  assert.equal(watchAttentionReason(healthy, recent), "最近变化");
  assert.equal(watchAttentionReason({ ...healthy, error: "boom" }, recent), "检查失败");
  assert.equal(
    watchAttentionReason({ repository: "web", branch: "main", present: true }, recent),
    null,
  );
  // "Recently added" is console session state, so it has to be injected.
  assert.equal(
    watchAttentionReason(
      { repository: "web", branch: "main", present: true },
      recent,
      () => true,
    ),
    "刚刚添加",
  );
  assert.equal(
    repositoryNeedsAttention({ watches: [{ present: true }] }),
    false,
  );
  assert.equal(
    repositoryNeedsAttention({ watches: [{ present: true }] }, () => true),
    true,
  );
});

test("tag parsing accepts every separator people type and drops repeats", () => {
  assert.deepEqual(parseRepositoryTags("core, api、backend；x"), ["core", "api", "backend；x"]);
  assert.deepEqual(parseRepositoryTags("Core，core，CORE"), ["Core"]);
  assert.deepEqual(parseRepositoryTags("  ,  , "), []);
  assert.deepEqual(parseRepositoryTags(null), []);
});

test("latest-value helpers ignore blanks and unparseable timestamps", () => {
  const watches = [
    { last_checked_at: "2026-08-20T00:00:00Z", last_success_at: null },
    { last_checked_at: "2026-08-25T00:00:00Z", last_success_at: "2026-08-21T00:00:00Z" },
    { last_checked_at: null },
  ];
  assert.equal(latestValue(watches, "last_checked_at"), "2026-08-25T00:00:00Z");
  assert.equal(latestValue(watches, "last_success_at"), "2026-08-21T00:00:00Z");
  assert.equal(latestValue([], "last_checked_at"), null);
  assert.equal(
    latestTimestamp("2026-08-20T00:00:00Z", null, "2026-08-25T00:00:00Z"),
    "2026-08-25T00:00:00Z",
  );
  assert.equal(latestTimestamp("nonsense", "2026-08-25T00:00:00Z"), "2026-08-25T00:00:00Z");
  assert.equal(latestTimestamp(null, undefined), null);
});

test("sha shortening and watch ids reject values that are not usable", () => {
  assert.equal(shortSha("a".repeat(40)), "aaaaaaaaaa");
  assert.equal(shortSha(""), "—");
  assert.equal(shortSha(null), "—");
  assert.equal(watchBranchId({ id: 7 }), 7);
  assert.equal(watchBranchId({ branch_id: 9 }), 9);
  assert.equal(watchBranchId({ id: 0 }), null);
  assert.equal(watchBranchId({ id: -1 }), null);
  assert.equal(watchBranchId({ id: "abc" }), null);
  assert.equal(watchBranchId(undefined), null);
});

test("the app.js fallback model answers everything the real model exports", () => {
  const appSource = fs.readFileSync(path.join(webRoot, "app.js"), "utf8");
  const fallback = appSource.slice(
    appSource.indexOf("const uiModel = globalThis.BranchRadarUIModel ||"),
  );
  const body = fallback.slice(0, fallback.indexOf("\nconst i18n"));
  const model = require(path.join(webRoot, "ui-model.js"));
  const missing = Object.keys(model).filter((name) => !body.includes(`${name}:`));
  // A page whose ui-model.js failed to load must still render, not throw.
  assert.deepEqual(missing, [], "fallback model is missing entries");
});
