# Dashboard Page Overrides

> **PROJECT:** Branch Radar
> **Generated:** 2026-08-18 21:12:56
> **Page Type:** Dashboard / Data View

> ⚠️ **IMPORTANT:** Rules in this file **override** the Master file (`design-system/MASTER.md`).
> Only deviations from the Master are documented here. For all other rules, refer to the Master.

---

## Page-Specific Rules

### Information Architecture

- Keep three primary work destinations: **动态**, **关注分支**, **设置**. Keep **更新日志** visually secondary while preserving its direct sidebar and mobile entry.
- The default page is a change inbox, not a presentation dashboard: status, actionable counts, filters, then the complete event stream.
- Codex, Lark, service details, and external writes belong in settings or an explicitly opened advanced flow.
- Preserve the event timeline as the page signature; avoid decorative radar apertures, duplicate event previews, and cumulative metrics that do not change a decision.
- The primary **立即检查** action always uses the literal safe payload. Codex and publish options require a separate advanced dialog and reset after use.
- Topbar actions are contextual: overview exposes safe check and advanced options; watched branches expose check-all only before selection; settings exposes connection refresh; changelog has no competing action.
- Healthy monitoring state collapses into one compact status strip. Expand explanatory copy only for empty, error, or pending-delivery states.
- The overview attention panel shows only actionable branches, recent changes, or pending delivery; never repeat the complete watch list.

### Layout Overrides

- **Max Width:** 1400px or full-width
- **Grid:** 12-column grid for data flexibility

### Spacing Overrides

- **Content Density:** High — optimize for information display
- Long watch lists keep the workspace switch, tag scope, search, and visible-list selection in one sticky command deck.

### Typography Overrides

- No overrides — use Master typography

### Color Overrides

- The shipped dashboard palette in `src/branch_radar/resources/web/app.css` is authoritative for this page; it intentionally replaces the generated Master palette.
- Brand asset colors are fixed tokens: rail `#101722`, tile `#172231`, radar ring `#526275`, branch `#EAF0F7`, baseline node `#7D9CFF`, and latest-signal node `#5ED0B2`.

### Component Overrides

- Avoid: Leave UI frozen with no feedback
- Avoid: Make dragging the only way to reorder resize or select
- Use one reusable Branch Radar SVG mark for the browser favicon and side-rail brand; it must remain legible at 16, 24, and 36px.
- Connected integrations show a compact identity row with service icon or initials, a safe account label, identity type/version, and a text status chip. Never expose emails, open IDs, document tokens, scopes, or raw CLI login output.
- Announce an integration refresh as one contextual status message instead of making every badge a competing live region.
- Keep at most five frequent repository tags visible on wide screens and fewer on narrow screens. Put the rest in a searchable “更多标签” disclosure.
- Repository headers own the single **编辑资料** entry. Branch overflow menus expose branch-scoped actions: **查看动态**, **立即检查**, and the visually separated **停止关注** action.
- Stopping a watch requires an explicit confirmation that explains retained history/cursors and untouched Git files; success is announced before the row disappears.
- Repository headers act as accessible disclosures: healthy repositories may stay collapsed, attention states open by default, search matches open temporarily, and explicit choices persist locally. A collapsed header must retain branch count, status, and last-check context.
- Light surfaces use mist white rather than pure white; dark surfaces use layered blue-charcoal rather than pure black. Both themes map through semantic tokens and preserve independent status-color contrast.
- Theme changes use one radial illumination transition from the invoking control, fall back cleanly where View Transitions are unavailable, and become immediate under reduced-motion preferences.
- After branch selection, show a persistent batch bar with selected count, clear, and check actions; hide the redundant topbar check while the batch bar is active.
- Long-running watch creation replaces the picker body with an in-dialog progress surface showing the current Git phase, elapsed time, selected scope, and a clear **收起到后台** escape route. Never rely on a toast behind a modal.
- English eyebrow copy belongs at page level. Card headings use direct Chinese labels without decorative duplicate captions.

---

## Page-Specific Components

- No unique components for this page

---

## Recommendations

- Effects: Hover tooltips, chart zoom on click, row highlighting on hover, smooth filter animations, data loading spinners
- Animation: Use skeleton screens or spinners
- Accessibility: Add buttons menus or tap-to-move controls and retain keyboard operation
