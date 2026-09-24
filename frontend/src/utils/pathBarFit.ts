/**
 * How much of a library path chain the path bar can show.
 *
 * The bar must never wrap to a second line and never push its pane wider, and
 * it used to serve that by folding everything but the last two crumbs as soon
 * as the chain was deeper than three. Depth is a poor proxy for width: a root
 * plus three folders named after customers still overflows a narrow pane,
 * while four short names fit with room to spare. So the caller measures and
 * asks this how many of the trailing crumbs fit.
 */

/** Crumbs kept whatever the width — the last two, next to the root. Below
 *  this the bar would stop saying where you are at all. */
export const PATH_BAR_MIN_CRUMBS = 2;

export interface PathBarFitInput {
  /** Usable width of the bar, in px. */
  available: number;
  /** Gap the bar puts between two neighbouring items, in px. */
  gap: number;
  /** Width of the root crumb. */
  rootWidth: number;
  /** Width of the ellipsis button that stands in for the folded crumbs. */
  ellipsisWidth: number;
  /** Width of every folder crumb, in path order, separator included. */
  crumbWidths: number[];
  /** Width of the trailing non-folder crumb (the "No folder" leaf), or 0. */
  leafWidth: number;
  /** Width of whatever else the bar always shows, e.g. the copy button. */
  reservedWidth: number;
}

/**
 * The number of trailing folder crumbs that fit, or `null` when nothing could
 * be measured — jsdom reports every width as zero, and so does a pane that is
 * `display: none`. `null` means "no answer", not "nothing fits", so the caller
 * can fall back to the depth rule instead of folding everything or nothing.
 */
export function fitPathCrumbs({
  available,
  gap,
  rootWidth,
  ellipsisWidth,
  crumbWidths,
  leafWidth,
  reservedWidth,
}: PathBarFitInput): number | null {
  const total = crumbWidths.length;
  if (available <= 0 || rootWidth <= 0) return null;
  if (total > 0 && crumbWidths.every((width) => width <= 0)) return null;

  const floor = Math.min(PATH_BAR_MIN_CRUMBS, total);
  const fixed =
    rootWidth +
    (leafWidth > 0 ? leafWidth + gap : 0) +
    (reservedWidth > 0 ? reservedWidth + gap : 0);

  for (let count = total; count > floor; count -= 1) {
    let used = fixed + (count < total ? ellipsisWidth + gap : 0);
    for (let i = total - count; i < total; i += 1) used += crumbWidths[i] + gap;
    if (used <= available) return count;
  }
  return floor;
}
