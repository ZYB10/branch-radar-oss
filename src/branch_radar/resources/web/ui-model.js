(function exposeBranchRadarUIModel(root, factory) {
  "use strict";

  const model = factory();
  if (typeof module === "object" && module.exports) module.exports = model;
  if (root) root.BranchRadarUIModel = model;
})(typeof globalThis === "object" ? globalThis : this, function createBranchRadarUIModel() {
  "use strict";

  const SIZE_THRESHOLDS = Object.freeze({ medium: 200, large: 2000 });
  const CANVAS_ZOOM_LIMITS = Object.freeze({ min: 0.08, max: 4 });
  const CANVAS_LOD_THRESHOLDS = Object.freeze({ authors: 0.72, details: 1.42, hysteresis: 0.08 });

  function finiteNonNegative(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : 0;
  }

  function countValue(value) {
    return Math.floor(finiteNonNegative(value));
  }

  function eventIdentifier(value) {
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }

  function runIdentifier(value) {
    if (Number.isSafeInteger(value) && value > 0) return value;
    if (typeof value !== "string") return null;
    const normalized = value.trim();
    return normalized && normalized.length <= 200 ? normalized : null;
  }

  function stringList(value, limit = 24) {
    if (!Array.isArray(value)) return [];
    const seen = new Set();
    const result = [];
    for (const item of value) {
      let text = "";
      if (typeof item === "string") text = item;
      else if (item && typeof item === "object") {
        text = item.label || item.title || item.name || item.description || item.reason || "";
      }
      text = typeof text === "string" ? text.trim() : "";
      if (!text || text.length > 240 || seen.has(text)) continue;
      seen.add(text);
      result.push(text);
      if (result.length >= limit) break;
    }
    return result;
  }

  function changeTotal(valueOrStats) {
    if (valueOrStats && typeof valueOrStats === "object") {
      const explicit = Number(valueOrStats.changes);
      if (Number.isFinite(explicit) && explicit >= 0) return Math.floor(explicit);
      return countValue(valueOrStats.insertions) + countValue(valueOrStats.deletions);
    }
    return countValue(valueOrStats);
  }

  function classifyChangeSize(valueOrStats) {
    const total = changeTotal(valueOrStats);
    let key = "small";
    let label = "小";
    let level = 1;
    if (total >= SIZE_THRESHOLDS.large) {
      key = "large";
      label = "大";
      level = 3;
    } else if (total >= SIZE_THRESHOLDS.medium) {
      key = "medium";
      label = "中";
      level = 2;
    }
    return {
      key,
      label,
      level,
      total,
      ratio: total <= 0 ? 0 : Math.min(1, Math.log10(total + 1) / 4),
    };
  }

  function normalizeEventMetadata(event) {
    const value = event && typeof event === "object" ? event : {};
    const sourceStats = value.stats && typeof value.stats === "object" ? value.stats : {};
    const stats = {
      commits: countValue(sourceStats.commits),
      files: countValue(sourceStats.files),
      insertions: countValue(sourceStats.insertions),
      deletions: countValue(sourceStats.deletions),
      changes: changeTotal(sourceStats),
    };
    return {
      authors: stringList(value.authors, 8),
      categories: stringList(value.categories, 8),
      signals: stringList(value.signals, 6),
      stats,
      size: classifyChangeSize(stats),
    };
  }

  function signalLaneKey(event) {
    const value = event && typeof event === "object" ? event : {};
    const repositoryId = eventIdentifier(value.repository_id ?? value.repositoryId);
    const branchId = eventIdentifier(value.branch_id ?? value.branchId);
    const repository = typeof value.repository === "string" ? value.repository.trim() : "";
    const branch = typeof value.branch === "string" ? value.branch.trim() : "";
    return [
      repositoryId === null ? `repository:${repository}` : `repository-id:${repositoryId}`,
      branchId === null ? `branch:${branch}` : `branch-id:${branchId}`,
    ].join("\u0000");
  }

  function parseTimeMs(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function detectedEventTimeMs(event) {
    if (!event || typeof event !== "object") return null;
    return parseTimeMs(event.detected_at);
  }

  function eventTimeline(event) {
    const value = event && typeof event === "object" ? event : {};
    const timeline = value.timeline && typeof value.timeline === "object" ? value.timeline : {};
    const detectedTimeMs = detectedEventTimeMs(value);
    const tipCommittedAtMs = parseTimeMs(timeline.tip_committed_at ?? timeline.tipCommittedAt);
    const firstCommittedAtMs = parseTimeMs(timeline.first_committed_at ?? timeline.firstCommittedAt);
    const lastCommittedAtMs = parseTimeMs(timeline.last_committed_at ?? timeline.lastCommittedAt);
    const usesTipCommit = timeline.basis === "tip_commit" && tipCommittedAtMs !== null;
    const authors = (Array.isArray(timeline.authors) ? timeline.authors : [])
      .slice(0, 32)
      .map((entry) => {
        const author = entry && typeof entry === "object" ? entry : {};
        const name = typeof author.name === "string" ? author.name.trim() : "";
        if (!name) return null;
        return {
          name,
          firstCommittedAtMs: parseTimeMs(author.first_committed_at ?? author.firstCommittedAt),
          lastCommittedAtMs: parseTimeMs(author.last_committed_at ?? author.lastCommittedAt),
          sampledCommits: countValue(author.sampled_commits ?? author.sampledCommits),
        };
      })
      .filter(Boolean);
    return {
      detectedTimeMs,
      timeMs: usesTipCommit ? tipCommittedAtMs : detectedTimeMs,
      timeBasis: usesTipCommit ? "tip_commit" : "detected",
      tipCommittedAtMs,
      firstCommittedAtMs,
      lastCommittedAtMs,
      commitRangeComplete: timeline.range_complete === true || timeline.rangeComplete === true,
      authors,
    };
  }

  function eventTimeMs(event) {
    return eventTimeline(event).timeMs;
  }

  function buildSignalFieldModel(events, options = {}) {
    const nowValue = Number(options.nowMs);
    const nowMs = Number.isFinite(nowValue) ? nowValue : Date.now();
    const requestedHours = Number(options.rangeHours);
    const rangeHours = Number.isFinite(requestedHours) && requestedHours > 0
      ? Math.min(requestedHours, 24 * 31)
      : 24;
    const startMs = nowMs - (rangeHours * 60 * 60 * 1000);
    const requestedGap = Number(options.minGapRatio);
    const minGapRatio = Number.isFinite(requestedGap)
      ? Math.max(0.01, Math.min(0.25, requestedGap))
      : 0.065;
    const requestedMaxSlots = Number(options.maxSlots);
    const maxSlots = Number.isFinite(requestedMaxSlots)
      ? Math.max(1, Math.min(8, Math.floor(requestedMaxSlots)))
      : 4;
    const seenIds = new Set();
    const normalized = [];
    let hiddenCount = 0;

    (Array.isArray(events) ? events : []).forEach((event) => {
      if (!event || typeof event !== "object") return;
      const id = eventIdentifier(event.id);
      if (id === null || seenIds.has(id)) return;
      seenIds.add(id);
      const timeMs = detectedEventTimeMs(event);
      if (timeMs !== null && timeMs < startMs) {
        hiddenCount += 1;
        return;
      }
      normalized.push({
        event,
        id,
        key: signalLaneKey(event),
        repository: typeof event.repository === "string" ? event.repository : "",
        branch: typeof event.branch === "string" ? event.branch : "",
        timeMs,
        checkRunId: runIdentifier(event.check_run_id ?? event.checkRunId),
      });
    });

    const runGroups = new Map();
    normalized.forEach((item) => {
      if (item.checkRunId === null || item.timeMs === null) return;
      const key = String(item.checkRunId);
      if (!runGroups.has(key)) runGroups.set(key, []);
      runGroups.get(key).push(item);
    });
    const runTimes = new Map();
    runGroups.forEach((items, key) => {
      if (items.length < 2) return;
      runTimes.set(key, Math.round(items.reduce((total, item) => total + item.timeMs, 0) / items.length));
    });

    const positionFor = (item) => {
      const alignedTime = item.checkRunId === null ? null : runTimes.get(String(item.checkRunId));
      const timeMs = alignedTime ?? item.timeMs ?? startMs;
      return Math.max(0, Math.min(1, (timeMs - startMs) / Math.max(1, nowMs - startMs)));
    };

    const laneMap = new Map();
    normalized.forEach((item) => {
      const positioned = { ...item, position: positionFor(item), slot: 0 };
      if (!laneMap.has(item.key)) {
        laneMap.set(item.key, {
          key: item.key,
          repository: item.repository,
          branch: item.branch,
          newestMs: item.timeMs ?? Number.NEGATIVE_INFINITY,
          events: [],
          slots: 1,
        });
      }
      const lane = laneMap.get(item.key);
      lane.newestMs = Math.max(lane.newestMs, item.timeMs ?? Number.NEGATIVE_INFINITY);
      lane.events.push(positioned);
    });

    const lanes = Array.from(laneMap.values());
    lanes.forEach((lane) => {
      lane.events.sort((left, right) => {
        if (left.position !== right.position) return left.position - right.position;
        if (left.timeMs !== right.timeMs) return (left.timeMs ?? Number.NEGATIVE_INFINITY) - (right.timeMs ?? Number.NEGATIVE_INFINITY);
        return left.id - right.id;
      });
      const slotEnds = [];
      lane.events.forEach((item) => {
        let slot = slotEnds.findIndex((position) => item.position - position >= minGapRatio);
        if (slot < 0 && slotEnds.length < maxSlots) slot = slotEnds.length;
        if (slot < 0) {
          slot = slotEnds.reduce(
            (best, position, index) => position < slotEnds[best] ? index : best,
            0,
          );
        }
        slotEnds[slot] = item.position;
        item.slot = slot;
      });
      lane.slots = Math.max(1, slotEnds.length);
    });
    lanes.sort((left, right) => {
      if (left.newestMs !== right.newestMs) return right.newestMs - left.newestMs;
      const repositoryOrder = left.repository.localeCompare(right.repository);
      return repositoryOrder || left.branch.localeCompare(right.branch);
    });

    const focusEvents = lanes.flatMap((lane) => lane.events).sort((left, right) => {
      const leftTime = left.timeMs ?? Number.NEGATIVE_INFINITY;
      const rightTime = right.timeMs ?? Number.NEGATIVE_INFINITY;
      return rightTime - leftTime || right.id - left.id;
    });
    const scanBands = Array.from(runTimes, ([id, timeMs]) => ({
      id,
      timeMs,
      count: runGroups.get(id).length,
      position: Math.max(0, Math.min(1, (timeMs - startMs) / Math.max(1, nowMs - startMs))),
    })).sort((left, right) => left.position - right.position || String(left.id).localeCompare(String(right.id)));

    return {
      startMs,
      endMs: nowMs,
      rangeHours,
      hiddenCount,
      lanes,
      events: focusEvents,
      scanBands,
    };
  }

  function canvasTopologyReference(value) {
    if (typeof value !== "string") return null;
    const normalized = value.trim();
    return normalized && normalized.length <= 128 ? normalized : null;
  }

  function branchCanvasRepositoryKey(event) {
    const value = event && typeof event === "object" ? event : {};
    const repositoryId = eventIdentifier(value.repository_id ?? value.repositoryId);
    const repository = typeof value.repository === "string" ? value.repository.trim() : "";
    return repositoryId === null ? `repository:${repository}` : `repository-id:${repositoryId}`;
  }

  function branchCanvasTopology(event) {
    const value = event && typeof event === "object" ? event : {};
    const topology = value.topology && typeof value.topology === "object" ? value.topology : {};
    return {
      fromOid: canvasTopologyReference(
        value.from_oid ?? value.fromOid ?? value.old_sha ?? topology.from_oid ?? topology.from,
      ),
      toOid: canvasTopologyReference(
        value.to_oid ?? value.toOid ?? value.new_sha ?? topology.to_oid ?? topology.to,
      ),
      mergeBaseOid: canvasTopologyReference(
        value.merge_base_oid ?? value.mergeBaseOid ?? value.merge_base
          ?? topology.merge_base_oid ?? topology.merge_base,
      ),
    };
  }

  function canvasNumber(value, fallback, minimum, maximum) {
    const number = Number(value);
    const fallbackNumber = Number(fallback);
    const safe = Number.isFinite(number)
      ? number
      : (Number.isFinite(fallbackNumber) ? fallbackNumber : minimum);
    return Math.max(minimum, Math.min(maximum, safe));
  }

  function authorSegmentKey(authors) {
    if (!authors.length) return "unknown";
    return authors.slice().sort((left, right) => left.localeCompare(right)).join("\u0000");
  }

  function canvasTimePosition(timeMs, axisStartMs, axisEndMs, paddingX, timelineWidth) {
    const fallback = axisStartMs;
    const sourceTimeMs = Number.isFinite(timeMs) ? timeMs : fallback;
    const plottedTimeMs = Math.max(axisStartMs, Math.min(axisEndMs, sourceTimeMs));
    const position = (plottedTimeMs - axisStartMs) / Math.max(1, axisEndMs - axisStartMs);
    return {
      plottedTimeMs,
      position,
      x: paddingX + (position * timelineWidth),
      timeClamped: Number.isFinite(timeMs) && plottedTimeMs !== timeMs,
    };
  }

  function positionedAuthorTimeline(item, axisStartMs, axisEndMs, paddingX, timelineWidth) {
    return item.timelineAuthors.map((author) => {
      const rangeIsValid = author.firstCommittedAtMs !== null
        && author.lastCommittedAtMs !== null
        && author.firstCommittedAtMs <= author.lastCommittedAtMs;
      const fallbackTimeMs = item.timeMs ?? item.plottedTimeMs;
      const startTimeMs = rangeIsValid ? author.firstCommittedAtMs : fallbackTimeMs;
      const endTimeMs = rangeIsValid ? author.lastCommittedAtMs : fallbackTimeMs;
      const start = canvasTimePosition(startTimeMs, axisStartMs, axisEndMs, paddingX, timelineWidth);
      const end = canvasTimePosition(endTimeMs, axisStartMs, axisEndMs, paddingX, timelineWidth);
      return {
        name: author.name,
        startMs: startTimeMs,
        endMs: endTimeMs,
        startX: start.x,
        endX: end.x,
        centerX: (start.x + end.x) / 2,
        width: Math.max(18, end.x - start.x),
        sampledCommits: author.sampledCommits,
        usesCommitTimeline: rangeIsValid,
        fallbackBasis: rangeIsValid ? null : item.timeBasis,
        timeClamped: start.timeClamped || end.timeClamped,
      };
    });
  }

  function authorRangeForItem(item, author) {
    const timeline = item.authorTimeline.find((candidate) => candidate.name === author);
    if (timeline) return timeline;
    return {
      name: author,
      startMs: item.timeMs ?? item.plottedTimeMs,
      endMs: item.timeMs ?? item.plottedTimeMs,
      startX: item.x,
      endX: item.x,
      centerX: item.x,
      width: 18,
      sampledCommits: 0,
      usesCommitTimeline: false,
      fallbackBasis: item.timeBasis,
      timeClamped: item.timeClamped,
    };
  }

  function branchCanvasAuthorSegments(events) {
    const segments = [];
    events.forEach((item) => {
      const authors = item.metadata.authors.slice();
      const key = authorSegmentKey(authors);
      const itemRanges = authors.length
        ? authors.map((author) => authorRangeForItem(item, author))
        : [authorRangeForItem(item, "")];
      const itemStartMs = Math.min(...itemRanges.map((range) => range.startMs));
      const itemEndMs = Math.max(...itemRanges.map((range) => range.endMs));
      const itemStartX = Math.min(...itemRanges.map((range) => range.startX));
      const itemEndX = Math.max(...itemRanges.map((range) => range.endX));
      let segment = segments.at(-1);
      if (!segment || segment.authorKey !== key) {
        segment = {
          key: `${item.laneKey}\u0000author-segment:${segments.length}`,
          authorKey: key,
          authors,
          mixed: authors.length !== 1,
          statsScope: "events",
          eventIds: [],
          eventCount: 0,
          commitCount: 0,
          stats: { commits: 0, files: 0, insertions: 0, deletions: 0, changes: 0 },
          startMs: itemStartMs,
          endMs: itemEndMs,
          startX: itemStartX,
          endX: itemEndX,
          authorRangeMap: new Map(),
        };
        segments.push(segment);
      }
      segment.eventIds.push(item.id);
      segment.eventCount += 1;
      segment.startMs = Math.min(segment.startMs, itemStartMs);
      segment.endMs = Math.max(segment.endMs, itemEndMs);
      segment.startX = Math.min(segment.startX, itemStartX);
      segment.endX = Math.max(segment.endX, itemEndX);
      itemRanges.forEach((range) => {
        const existing = segment.authorRangeMap.get(range.name);
        if (!existing) {
          segment.authorRangeMap.set(range.name, { ...range });
          return;
        }
        existing.startMs = Math.min(existing.startMs, range.startMs);
        existing.endMs = Math.max(existing.endMs, range.endMs);
        existing.startX = Math.min(existing.startX, range.startX);
        existing.endX = Math.max(existing.endX, range.endX);
        existing.sampledCommits += range.sampledCommits;
        existing.usesCommitTimeline = existing.usesCommitTimeline && range.usesCommitTimeline;
        existing.fallbackBasis = existing.fallbackBasis === range.fallbackBasis
          ? existing.fallbackBasis
          : "mixed";
        existing.timeClamped = existing.timeClamped || range.timeClamped;
      });
      for (const field of ["commits", "files", "insertions", "deletions", "changes"]) {
        segment.stats[field] += countValue(item.metadata.stats[field]);
      }
      segment.commitCount = segment.stats.commits;
    });
    return segments.map((segment) => {
      const authorRanges = Array.from(segment.authorRangeMap.values()).map((range) => ({
        ...range,
        centerX: (range.startX + range.endX) / 2,
        width: Math.max(18, range.endX - range.startX),
      }));
      const result = {
        ...segment,
        authorRanges,
        centerX: (segment.startX + segment.endX) / 2,
        width: Math.max(18, segment.endX - segment.startX),
      };
      delete result.authorRangeMap;
      return result;
    });
  }

  /**
   * Build a deterministic world-space model for the branch evolution canvas.
   *
   * Detection time controls membership in the requested window. Horizontal
   * position uses a verified tip commit time when available, so an old commit
   * discovered after an offline period remains visible at its factual time.
   * Cross-branch edges require a unique explicit `source.to_oid ===
   * target.from_oid` match inside the same repository. A missing or ambiguous
   * match deliberately produces no edge: branch names and nearby timestamps are
   * never treated as ancestry evidence.
   */
  function buildBranchCanvasModel(events, options = {}) {
    const nowValue = Number(options.nowMs);
    const nowMs = Number.isFinite(nowValue) ? nowValue : Date.now();
    const requestedHours = Number(options.rangeHours);
    const rangeHours = Number.isFinite(requestedHours) && requestedHours > 0
      ? Math.min(requestedHours, 24 * 366)
      : 24;
    const detectionStartMs = nowMs - (rangeHours * 60 * 60 * 1000);
    const paddingX = canvasNumber(options.paddingX, 96, 24, 480);
    const pixelsPerHour = canvasNumber(options.pixelsPerHour, 64, 8, 320);
    const minimumTimelineWidth = canvasNumber(options.minimumTimelineWidth, 880, 320, 8_000);
    const maximumTimelineWidth = canvasNumber(
      options.maximumTimelineWidth,
      12_000,
      minimumTimelineWidth,
      120_000,
    );
    const laneHeight = canvasNumber(options.laneHeight, 92, 56, 240);
    const repositoryHeaderHeight = canvasNumber(options.repositoryHeaderHeight, 42, 24, 120);
    const repositoryGap = canvasNumber(options.repositoryGap, 30, 0, 160);
    const topPadding = canvasNumber(options.topPadding, 54, 0, 320);
    const bottomPadding = canvasNumber(options.bottomPadding, 54, 0, 320);
    const seenIds = new Set();
    const normalized = [];
    let hiddenCount = 0;

    (Array.isArray(events) ? events : []).forEach((event) => {
      if (!event || typeof event !== "object") return;
      const id = eventIdentifier(event.id);
      if (id === null || seenIds.has(id)) return;
      seenIds.add(id);
      const timeline = eventTimeline(event);
      if (timeline.detectedTimeMs !== null && timeline.detectedTimeMs < detectionStartMs) {
        hiddenCount += 1;
        return;
      }
      const repository = typeof event.repository === "string" ? event.repository : "";
      const branch = typeof event.branch === "string" ? event.branch : "";
      const repositoryId = eventIdentifier(event.repository_id ?? event.repositoryId);
      const branchId = eventIdentifier(event.branch_id ?? event.branchId);
      const repositoryKey = branchCanvasRepositoryKey(event);
      const laneKey = signalLaneKey(event);
      normalized.push({
        event,
        id,
        repositoryId,
        branchId,
        repositoryKey,
        laneKey,
        repository,
        branch,
        detectedTimeMs: timeline.detectedTimeMs,
        timeMs: timeline.timeMs,
        timeBasis: timeline.timeBasis,
        tipCommittedAtMs: timeline.tipCommittedAtMs,
        firstCommittedAtMs: timeline.firstCommittedAtMs,
        lastCommittedAtMs: timeline.lastCommittedAtMs,
        commitRangeComplete: timeline.commitRangeComplete,
        timelineAuthors: timeline.authors,
        y: 0,
        metadata: normalizeEventMetadata(event),
        topology: branchCanvasTopology(event),
      });
    });

    const axisCandidates = [detectionStartMs, nowMs];
    normalized.forEach((item) => {
      if (item.timeMs !== null) axisCandidates.push(item.timeMs);
      if (item.firstCommittedAtMs !== null && item.lastCommittedAtMs !== null
        && item.firstCommittedAtMs <= item.lastCommittedAtMs) {
        axisCandidates.push(item.firstCommittedAtMs, item.lastCommittedAtMs);
      }
      item.timelineAuthors.forEach((author) => {
        if (author.firstCommittedAtMs !== null && author.lastCommittedAtMs !== null
          && author.firstCommittedAtMs <= author.lastCommittedAtMs) {
          axisCandidates.push(author.firstCommittedAtMs, author.lastCommittedAtMs);
        }
      });
    });
    const oldestAxisMs = nowMs - (366 * 24 * 60 * 60 * 1000);
    const latestAxisMs = nowMs + (24 * 60 * 60 * 1000);
    const axisStartMs = Math.max(Math.min(...axisCandidates), oldestAxisMs);
    const axisEndMs = Math.max(
      axisStartMs + 1,
      Math.min(Math.max(...axisCandidates), latestAxisMs),
    );
    const axisHours = (axisEndMs - axisStartMs) / (60 * 60 * 1000);
    const timelineWidth = Math.min(
      maximumTimelineWidth,
      Math.max(minimumTimelineWidth, axisHours * pixelsPerHour),
    );
    normalized.forEach((item) => {
      const positioned = canvasTimePosition(
        item.timeMs,
        axisStartMs,
        axisEndMs,
        paddingX,
        timelineWidth,
      );
      item.plottedTimeMs = positioned.plottedTimeMs;
      item.position = positioned.position;
      item.x = positioned.x;
      item.timeClamped = positioned.timeClamped;
      item.outsideRequestedRange = item.timeMs !== null
        && (item.timeMs < detectionStartMs || item.timeMs > nowMs);
      item.authorTimeline = positionedAuthorTimeline(
        item,
        axisStartMs,
        axisEndMs,
        paddingX,
        timelineWidth,
      );
      delete item.timelineAuthors;
    });

    const repositoryMap = new Map();
    normalized.forEach((item) => {
      if (!repositoryMap.has(item.repositoryKey)) {
        repositoryMap.set(item.repositoryKey, {
          key: item.repositoryKey,
          id: item.repositoryId,
          repository: item.repository,
          newestMs: item.timeMs ?? Number.NEGATIVE_INFINITY,
          laneMap: new Map(),
        });
      }
      const repository = repositoryMap.get(item.repositoryKey);
      repository.newestMs = Math.max(repository.newestMs, item.timeMs ?? Number.NEGATIVE_INFINITY);
      if (!repository.laneMap.has(item.laneKey)) {
        repository.laneMap.set(item.laneKey, {
          key: item.laneKey,
          id: item.branchId,
          repositoryKey: item.repositoryKey,
          repository: item.repository,
          branch: item.branch,
          newestMs: item.timeMs ?? Number.NEGATIVE_INFINITY,
          events: [],
        });
      }
      const lane = repository.laneMap.get(item.laneKey);
      lane.newestMs = Math.max(lane.newestMs, item.timeMs ?? Number.NEGATIVE_INFINITY);
      lane.events.push(item);
    });

    const repositories = Array.from(repositoryMap.values()).sort((left, right) => {
      if (left.id !== null && right.id !== null && left.id !== right.id) return left.id - right.id;
      if ((left.id === null) !== (right.id === null)) return left.id === null ? 1 : -1;
      return left.repository.localeCompare(right.repository) || left.key.localeCompare(right.key);
    });
    const lanes = [];
    let nextY = topPadding;
    repositories.forEach((repository) => {
      const repositoryY = nextY;
      nextY += repositoryHeaderHeight;
      const repositoryLanes = Array.from(repository.laneMap.values()).sort((left, right) => {
        if (left.id !== null && right.id !== null && left.id !== right.id) return left.id - right.id;
        if ((left.id === null) !== (right.id === null)) return left.id === null ? 1 : -1;
        return left.branch.localeCompare(right.branch) || left.key.localeCompare(right.key);
      });
      repositoryLanes.forEach((lane) => {
        lane.events.sort((left, right) => {
          const leftTime = left.timeMs ?? Number.NEGATIVE_INFINITY;
          const rightTime = right.timeMs ?? Number.NEGATIVE_INFINITY;
          return leftTime - rightTime || left.id - right.id;
        });
        lane.y = nextY + (laneHeight / 2);
        lane.height = laneHeight;
        lane.events.forEach((item) => { item.y = lane.y; });
        lane.authorSegments = branchCanvasAuthorSegments(lane.events);
        lanes.push(lane);
        nextY += laneHeight;
      });
      repository.y = repositoryY;
      repository.height = nextY - repositoryY;
      repository.lanes = repositoryLanes;
      delete repository.laneMap;
      nextY += repositoryGap;
    });

    const targetIndex = new Map();
    normalized.forEach((item) => {
      const oid = item.topology.toOid;
      if (!oid) return;
      const key = `${item.repositoryKey}\u0000${oid}`;
      if (!targetIndex.has(key)) targetIndex.set(key, []);
      targetIndex.get(key).push(item);
    });
    const edges = [];
    let ambiguousEdgeCount = 0;
    normalized.forEach((target) => {
      const fromOid = target.topology.fromOid;
      if (!fromOid) return;
      const key = `${target.repositoryKey}\u0000${fromOid}`;
      const candidates = (targetIndex.get(key) || []).filter((source) => {
        if (source.id === target.id) return false;
        if (source.detectedTimeMs === null || target.detectedTimeMs === null) {
          return source.id < target.id;
        }
        if (source.detectedTimeMs !== target.detectedTimeMs) {
          return source.detectedTimeMs < target.detectedTimeMs;
        }
        return source.id < target.id;
      });
      if (candidates.length !== 1) {
        if (candidates.length > 1 && candidates.some((source) => source.laneKey !== target.laneKey)) {
          ambiguousEdgeCount += 1;
        }
        return;
      }
      const source = candidates[0];
      if (source.laneKey === target.laneKey) return;
      edges.push({
        key: `${source.id}->${target.id}`,
        kind: "branch",
        repositoryKey: target.repositoryKey,
        fromEventId: source.id,
        toEventId: target.id,
        fromLaneKey: source.laneKey,
        toLaneKey: target.laneKey,
        from: { x: source.x, y: source.y },
        to: { x: target.x, y: target.y },
      });
    });
    edges.sort((left, right) => left.to.x - right.to.x || left.toEventId - right.toEventId);

    const worldHeight = Math.max(
      topPadding + repositoryHeaderHeight + laneHeight + bottomPadding,
      nextY - (repositories.length ? repositoryGap : 0) + bottomPadding,
    );
    const focusEvents = normalized.slice().sort((left, right) => {
      const leftTime = left.timeMs ?? Number.NEGATIVE_INFINITY;
      const rightTime = right.timeMs ?? Number.NEGATIVE_INFINITY;
      return rightTime - leftTime || right.id - left.id;
    });
    const currentTime = canvasTimePosition(
      nowMs,
      axisStartMs,
      axisEndMs,
      paddingX,
      timelineWidth,
    );
    return {
      startMs: axisStartMs,
      endMs: axisEndMs,
      axisStartMs,
      axisEndMs,
      detectionStartMs,
      detectionEndMs: nowMs,
      rangeHours,
      hiddenCount,
      repositories,
      lanes,
      events: focusEvents,
      edges,
      ambiguousEdgeCount,
      latestX: paddingX + timelineWidth,
      nowX: currentTime.x,
      bounds: {
        x: 0,
        y: 0,
        width: timelineWidth + (paddingX * 2),
        height: worldHeight,
      },
    };
  }

  function clampCanvasZoom(value, options = {}) {
    const first = Number(options.min);
    const second = Number(options.max);
    const rawMin = Number.isFinite(first) && first > 0 ? first : CANVAS_ZOOM_LIMITS.min;
    const rawMax = Number.isFinite(second) && second > 0 ? second : CANVAS_ZOOM_LIMITS.max;
    const minimum = Math.min(rawMin, rawMax);
    const maximum = Math.max(rawMin, rawMax);
    const fallback = Number(options.fallback);
    const zoom = Number(value);
    const safe = Number.isFinite(zoom) && zoom > 0
      ? zoom
      : (Number.isFinite(fallback) && fallback > 0 ? fallback : 1);
    return Math.max(minimum, Math.min(maximum, safe));
  }

  function canvasLod(value, previous = null, options = {}) {
    const zoom = clampCanvasZoom(value, options);
    const authors = canvasNumber(options.authors, CANVAS_LOD_THRESHOLDS.authors, 0.05, 10);
    const details = Math.max(
      authors + 0.05,
      canvasNumber(options.details, CANVAS_LOD_THRESHOLDS.details, 0.1, 12),
    );
    const hysteresis = canvasNumber(
      options.hysteresis,
      CANVAS_LOD_THRESHOLDS.hysteresis,
      0,
      Math.max(0, Math.min(authors, details - authors) / 2),
    );
    if (previous === "overview") {
      if (zoom >= details + hysteresis) return "details";
      return zoom >= authors + hysteresis ? "authors" : "overview";
    }
    if (previous === "authors") {
      if (zoom < authors - hysteresis) return "overview";
      return zoom >= details + hysteresis ? "details" : "authors";
    }
    if (previous === "details") {
      if (zoom < authors - hysteresis) return "overview";
      return zoom < details - hysteresis ? "authors" : "details";
    }
    if (zoom < authors) return "overview";
    return zoom < details ? "authors" : "details";
  }

  function zoomAroundPoint(transform, value, point, options = {}) {
    const current = transform && typeof transform === "object" ? transform : {};
    const currentZoom = clampCanvasZoom(current.zoom, options);
    const nextZoom = clampCanvasZoom(value, options);
    const anchorX = Number.isFinite(Number(point?.x)) ? Number(point.x) : 0;
    const anchorY = Number.isFinite(Number(point?.y)) ? Number(point.y) : 0;
    const currentX = Number.isFinite(Number(current.x)) ? Number(current.x) : 0;
    const currentY = Number.isFinite(Number(current.y)) ? Number(current.y) : 0;
    const worldX = (anchorX - currentX) / currentZoom;
    const worldY = (anchorY - currentY) / currentZoom;
    return {
      x: anchorX - (worldX * nextZoom),
      y: anchorY - (worldY * nextZoom),
      zoom: nextZoom,
    };
  }

  function canvasBounds(value) {
    const bounds = value && typeof value === "object" ? value : {};
    return {
      x: Number.isFinite(Number(bounds.x)) ? Number(bounds.x) : 0,
      y: Number.isFinite(Number(bounds.y)) ? Number(bounds.y) : 0,
      width: Math.max(1, Number.isFinite(Number(bounds.width)) ? Number(bounds.width) : 1),
      height: Math.max(1, Number.isFinite(Number(bounds.height)) ? Number(bounds.height) : 1),
    };
  }

  function canvasViewport(value) {
    const viewport = value && typeof value === "object" ? value : {};
    return {
      width: Math.max(1, Number.isFinite(Number(viewport.width)) ? Number(viewport.width) : 1),
      height: Math.max(1, Number.isFinite(Number(viewport.height)) ? Number(viewport.height) : 1),
    };
  }

  function fitCanvasTransform(boundsValue, viewportValue, options = {}) {
    const bounds = canvasBounds(boundsValue);
    const viewport = canvasViewport(viewportValue);
    const padding = canvasNumber(options.padding, 42, 0, Math.min(viewport.width, viewport.height) / 2);
    const usableWidth = Math.max(1, viewport.width - (padding * 2));
    const usableHeight = Math.max(1, viewport.height - (padding * 2));
    const zoom = clampCanvasZoom(Math.min(usableWidth / bounds.width, usableHeight / bounds.height), options);
    return {
      x: ((viewport.width - (bounds.width * zoom)) / 2) - (bounds.x * zoom),
      y: ((viewport.height - (bounds.height * zoom)) / 2) - (bounds.y * zoom),
      zoom,
    };
  }

  function latestCanvasTransform(boundsValue, viewportValue, options = {}) {
    const bounds = canvasBounds(boundsValue);
    const viewport = canvasViewport(viewportValue);
    const padding = canvasNumber(options.padding, 42, 0, Math.min(viewport.width, viewport.height) / 2);
    const zoom = clampCanvasZoom(options.zoom, options);
    const latestValue = Number(options.latestX);
    const latestX = Number.isFinite(latestValue) ? latestValue : bounds.x + bounds.width;
    const targetYValue = Number(options.targetY);
    const hasTargetY = options.targetY !== null
      && options.targetY !== undefined
      && options.targetY !== ""
      && Number.isFinite(targetYValue);
    return {
      x: viewport.width - padding - (latestX * zoom),
      y: hasTargetY
        ? (viewport.height / 2) - (targetYValue * zoom)
        : ((viewport.height - (bounds.height * zoom)) / 2) - (bounds.y * zoom),
      zoom,
    };
  }

  function normalizeSummary(value) {
    if (!value || typeof value !== "object") return null;
    const title = typeof value.title === "string" ? value.title.trim() : "";
    const overview = typeof value.overview === "string" ? value.overview.trim() : "";
    const source = typeof value.source === "string" ? value.source.trim() : "";
    const summary = {
      title,
      overview,
      themes: stringList(value.themes, 8),
      priorities: stringList(value.priorities, 8),
      connections: stringList(value.connections, 8),
      completenessNote: typeof (value.completeness_note ?? value.completenessNote) === "string"
        ? (value.completeness_note ?? value.completenessNote).trim()
        : "",
      source,
    };
    return title || overview || summary.themes.length || summary.priorities.length
      || summary.connections.length || summary.completenessNote ? summary : null;
  }

  function validTimestamp(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    return Number.isFinite(Date.parse(value)) ? value : null;
  }

  function normalizeEventReferences(value) {
    if (!Array.isArray(value)) return [];
    const result = [];
    const seen = new Set();
    for (const item of value) {
      if (!item || typeof item !== "object") continue;
      const rawId = eventIdentifier(item.id);
      if (rawId === null) continue;
      const key = String(rawId);
      if (seen.has(key)) continue;
      seen.add(key);
      result.push({
        id: rawId,
        repository: typeof item.repository === "string" ? item.repository : "",
        branch: typeof item.branch === "string" ? item.branch : "",
        kind: typeof item.kind === "string" ? item.kind : "",
        title: typeof item.title === "string" ? item.title : "",
        categories: stringList(item.categories, 6),
      });
      if (result.length >= 100) break;
    }
    return result;
  }

  function normalizeBlockedBranches(value) {
    if (!Array.isArray(value)) return [];
    const result = [];
    for (const item of value) {
      if (!item || typeof item !== "object") continue;
      const repository = typeof item.repository === "string" ? item.repository.trim().slice(0, 180) : "";
      const branch = typeof item.branch === "string" ? item.branch.trim().slice(0, 240) : "";
      const branchId = eventIdentifier(item.branch_id ?? item.branchId);
      const repositoryId = eventIdentifier(item.repository_id ?? item.repositoryId);
      if (!repository && !branch && branchId === null && repositoryId === null) continue;
      result.push({
        branchId,
        repositoryId,
        repository,
        branch,
        status: typeof item.status === "string" ? item.status.trim().slice(0, 80) : "",
        error: typeof item.error === "string" ? item.error.trim().slice(0, 500) : "",
      });
      if (result.length >= 50) break;
    }
    return result;
  }

  function normalizeCheckRunSummary(run) {
    const value = run && typeof run === "object" ? run : {};
    const startedAt = validTimestamp(value.started_at ?? value.startedAt);
    const finishedAt = validTimestamp(value.finished_at ?? value.finishedAt);
    const startedMs = startedAt ? Date.parse(startedAt) : NaN;
    const finishedMs = finishedAt ? Date.parse(finishedAt) : NaN;
    const rawEventIds = Array.isArray(value.event_ids)
      ? value.event_ids
      : (Array.isArray(value.eventIds) ? value.eventIds : []);
    const eventIds = [];
    const seenEventIds = new Set();
    rawEventIds.forEach((eventId) => {
      const normalizedId = eventIdentifier(eventId);
      if (normalizedId === null) return;
      const key = String(normalizedId);
      if (seenEventIds.has(key)) return;
      seenEventIds.add(key);
      eventIds.push(normalizedId);
    });
    const counts = value.counts && typeof value.counts === "object" ? value.counts : {};
    const publication = value.publication && typeof value.publication === "object" ? value.publication : {};
    const publicationBacklog = publication.backlog && typeof publication.backlog === "object"
      ? publication.backlog
      : {};
    const scope = value.scope && typeof value.scope === "object" ? value.scope : {};
    const options = value.options && typeof value.options === "object" ? value.options : {};
    const id = runIdentifier(value.id);
    const acknowledgedAt = validTimestamp(value.acknowledged_at ?? value.acknowledgedAt);
    const pending = typeof value.pending === "boolean" ? value.pending : id !== null && !acknowledgedAt;
    return {
      id,
      status: typeof value.status === "string" ? value.status : "",
      startedAt,
      finishedAt,
      acknowledgedAt,
      actionable: Boolean(value.actionable),
      pending,
      isPending: pending,
      durationMs: Number.isFinite(startedMs) && Number.isFinite(finishedMs) && finishedMs >= startedMs
        ? finishedMs - startedMs
        : null,
      scope: {
        kind: ["all", "repository", "branches"].includes(scope.kind) ? scope.kind : "all",
        repositoryId: scope.repository_id ?? scope.repositoryId ?? null,
        requestedBranchIds: Array.isArray(scope.requested_branch_ids)
          ? scope.requested_branch_ids.slice(0, 500)
          : (Array.isArray(scope.requestedBranchIds) ? scope.requestedBranchIds.slice(0, 500) : []),
        actualBranchIds: Array.isArray(scope.actual_branch_ids)
          ? scope.actual_branch_ids.slice(0, 500)
          : (Array.isArray(scope.actualBranchIds) ? scope.actualBranchIds.slice(0, 500) : []),
      },
      options: {
        fetch: Boolean(options.fetch),
        useCodex: Boolean(options.use_codex ?? options.useCodex),
        publish: Boolean(options.publish),
      },
      counts: {
        requested: countValue(counts.requested),
        checked: countValue(counts.checked),
        changed: countValue(counts.changed),
        unchanged: countValue(counts.unchanged),
        baselines: countValue(counts.baselines),
        blocked: countValue(counts.blocked),
        errors: countValue(counts.errors),
        events: countValue(counts.events),
      },
      eventIds,
      events: normalizeEventReferences(value.events),
      eventsTruncated: Boolean(value.events_truncated ?? value.eventsTruncated),
      blocked: normalizeBlockedBranches(value.blocked),
      blockedTruncated: Boolean(value.blocked_truncated ?? value.blockedTruncated),
      publication: {
        requested: countValue(publication.requested),
        succeeded: countValue(publication.succeeded),
        failed: countValue(publication.failed),
        unknown: countValue(publication.unknown),
        truncated: Boolean(publication.truncated),
        backlog: {
          succeeded: countValue(publicationBacklog.succeeded),
          failed: countValue(publicationBacklog.failed),
          unknown: countValue(publicationBacklog.unknown),
        },
      },
      summary: normalizeSummary(value.summary),
      summaryWarning: typeof (value.summary_warning ?? value.summaryWarning) === "string"
        ? (value.summary_warning ?? value.summaryWarning).trim()
        : "",
      error: typeof value.error === "string" ? value.error.trim().slice(0, 1000) : "",
    };
  }

  function editableTarget(target) {
    if (!target || typeof target !== "object") return false;
    const node = target.nodeType === 3 ? target.parentElement : target;
    if (!node) return false;
    const tag = typeof node.tagName === "string" ? node.tagName.toUpperCase() : "";
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return true;
    if (node.isContentEditable) return true;
    return typeof node.closest === "function" && Boolean(node.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])"));
  }

  function shouldHandleShortcut(event, context = {}) {
    if (!event || event.defaultPrevented || event.repeat || event.isComposing) return false;
    if (event.metaKey || event.ctrlKey || event.altKey) return false;
    if (context.dialogOpen || context.menuOpen || context.editable || editableTarget(event.target)) return false;
    const rawKey = String(event.key || "");
    if (event.shiftKey && rawKey !== "?") return false;
    const key = rawKey.toLowerCase();
    return ["/", "r", "j", "k", "?"].includes(key);
  }

  function moveEventSelection(ids, currentId, direction, options = {}) {
    if (!Array.isArray(ids) || !ids.length) return null;
    const values = ids.filter((id) => id !== null && id !== undefined && id !== "");
    if (!values.length) return null;
    const step = Number(direction) < 0 ? -1 : 1;
    const currentIndex = values.findIndex((id) => String(id) === String(currentId));
    if (currentIndex < 0) return step < 0 ? values.at(-1) : values[0];
    const nextIndex = currentIndex + step;
    if (options.wrap === false) return values[Math.max(0, Math.min(values.length - 1, nextIndex))];
    return values[(nextIndex + values.length) % values.length];
  }

  function moveListboxActiveIndex(itemCount, currentIndex, key) {
    const count = Math.max(0, Math.floor(Number(itemCount) || 0));
    if (!count) return null;
    const current = Number.isInteger(currentIndex)
      ? Math.max(0, Math.min(count - 1, currentIndex))
      : 0;
    if (key === "Home") return 0;
    if (key === "End") return count - 1;
    if (key === "ArrowDown") return Math.min(count - 1, current + 1);
    if (key === "ArrowUp") return Math.max(0, current - 1);
    return current;
  }

  function prioritizeBranchNames(branches, priorities, limit = 200) {
    const maximum = Math.max(0, Math.floor(Number(limit) || 0));
    if (!maximum) return [];
    const seen = new Set();
    const values = [];
    [...(Array.isArray(priorities) ? priorities : []), ...(Array.isArray(branches) ? branches : [])]
      .forEach((value) => {
        if (typeof value !== "string" || !value || seen.has(value) || values.length >= maximum) return;
        seen.add(value);
        values.push(value);
      });
    return values;
  }

  const RECENT_EVENT_WINDOW_MS = 24 * 60 * 60 * 1000;

  function shortSha(value) {
    return value ? String(value).slice(0, 10) : "—";
  }

  function watchBranchId(watch) {
    const value = Number(watch?.id ?? watch?.branch_id);
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  }

  /** Status label and modifier for one watched branch, worst reason first. */
  function branchStatus(watch) {
    if (!watch) return ["正常", ""];
    if (watch.error) return ["检查失败", "is-error"];
    if (watch.baseline_pending) return ["等待基线", "is-pending"];
    if (!watch.present) return ["分支不存在", "is-warning"];
    return ["正常", ""];
  }

  /** Status for a repository, taken from the branch in the worst state. */
  function groupWatchStatus(watches) {
    const values = Array.isArray(watches) ? watches : [];
    if (values.some((watch) => watch.error || !watch.present)) {
      return ["需要处理", "is-error"];
    }
    if (values.some((watch) => watch.baseline_pending)) return ["等待基线", "is-pending"];
    return ["正常", ""];
  }

  function repositoryDisplayName(group) {
    return group?.alias || group?.name;
  }

  function repositoryGroupKey(group) {
    const id = group?.id;
    return id === null || id === undefined ? `name:${group?.name}` : `id:${id}`;
  }

  /** Group watches by repository, preserving the order they arrived in. */
  function groupWatches(watches) {
    const groups = new Map();
    (Array.isArray(watches) ? watches : []).forEach((watch) => {
      const key = String(watch.repository_id ?? `name:${watch.repository}`);
      if (!groups.has(key)) {
        groups.set(key, {
          id: watch.repository_id ?? null,
          name: watch.repository || "未命名仓库",
          alias: watch.repository_alias || null,
          tags: Array.isArray(watch.repository_tags) ? watch.repository_tags : [],
          watches: [],
        });
      }
      groups.get(key).watches.push(watch);
    });
    return Array.from(groups.values());
  }

  function latestValue(watches, field) {
    let latest = null;
    (Array.isArray(watches) ? watches : []).forEach((watch) => {
      const value = watch?.[field];
      if (value && (!latest || value > latest)) latest = value;
    });
    return latest;
  }

  /** Latest of several timestamps, ignoring anything unparseable. */
  function latestTimestamp(...values) {
    return values.filter(Boolean).reduce((latest, value) => {
      if (!latest) return value;
      const candidateTime = new Date(value).getTime();
      const latestTime = new Date(latest).getTime();
      if (!Number.isFinite(candidateTime)) return latest;
      if (!Number.isFinite(latestTime) || candidateTime > latestTime) return value;
      return latest;
    }, null);
  }

  function watchKey(repository, branch) {
    return `${repository}\u0000${branch}`;
  }

  /**
   * Keys of branches that changed inside the recent window.
   *
   * `nowMs` is a parameter rather than a call to Date.now so the window is
   * testable and so a render cannot drift mid-frame.
   */
  function recentEventWatchKeys(events, nowMs = Date.now()) {
    const threshold = nowMs - RECENT_EVENT_WINDOW_MS;
    const keys = new Set();
    (Array.isArray(events) ? events : []).forEach((event) => {
      const detected = new Date(event?.detected_at || "").getTime();
      if (!Number.isFinite(detected) || detected < threshold) return;
      keys.add(watchKey(event.repository, event.branch));
    });
    return keys;
  }

  /**
   * Why a branch is worth a reader's attention, or null.
   *
   * `isRecentlyAdded` is injected because "recently added" is session state
   * the console owns, not something derivable from the watch itself.
   */
  function watchAttentionReason(watch, recentKeys, isRecentlyAdded = () => false) {
    if (!watch) return null;
    if (watch.error) return "检查失败";
    if (watch.baseline_pending) return "等待基线";
    if (!watch.present) return "分支不存在";
    if (recentKeys && recentKeys.has(watchKey(watch.repository, watch.branch))) {
      return "最近变化";
    }
    if (isRecentlyAdded(watch)) return "刚刚添加";
    return null;
  }

  function repositoryNeedsAttention(group, isRecentlyAdded = () => false) {
    return (group?.watches || []).some((watch) => (
      watch.error || watch.baseline_pending || !watch.present || isRecentlyAdded(watch)
    ));
  }

  /** Split a free-text tag field, dropping blanks and case-insensitive repeats. */
  function parseRepositoryTags(value) {
    const tags = [];
    const seen = new Set();
    String(value ?? "").split(/[,，、]/).forEach((item) => {
      const tag = item.trim();
      const key = tag.toLocaleLowerCase("zh-CN");
      if (!tag || seen.has(key)) return;
      seen.add(key);
      tags.push(tag);
    });
    return tags;
  }

  function isEventNewForRun(eventOrId, run) {
    const event = eventOrId && typeof eventOrId === "object" ? eventOrId : null;
    if (event && typeof event.is_new === "boolean") return event.is_new;
    const normalized = normalizeCheckRunSummary(run);
    if (!normalized.isPending) return false;
    const eventId = event ? event.id : eventOrId;
    if (event && event.check_run_id !== null && event.check_run_id !== undefined
      && String(event.check_run_id) === String(normalized.id)) return true;
    return normalized.eventIds.some((candidate) => String(candidate) === String(eventId));
  }

  return Object.freeze({
    SIZE_THRESHOLDS,
    classifyChangeSize,
    buildSignalFieldModel,
    buildBranchCanvasModel,
    clampCanvasZoom,
    canvasLod,
    zoomAroundPoint,
    fitCanvasTransform,
    latestCanvasTransform,
    normalizeEventMetadata,
    normalizeCheckRunSummary,
    shouldHandleShortcut,
    moveEventSelection,
    moveListboxActiveIndex,
    prioritizeBranchNames,
    isEventNewForRun,
    RECENT_EVENT_WINDOW_MS,
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
  });
});
