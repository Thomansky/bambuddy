/**
 * Naming a library folder for the screen.
 *
 * An order folder is filed under a running number while the enquiry is still
 * an enquiry, and in this workflow it very often carries nothing else — so a
 * label built from the name alone comes out empty, and two orders on screen
 * become indistinguishable blank rows.
 */

export interface NamedFolder {
  name: string;
  number?: string | null;
}

/** What to call a folder in a tooltip or an aria-label. The number stands in
 *  for a missing name rather than leaving the control unlabelled. */
export function folderLabel(folder: NamedFolder): string {
  return folder.name || folder.number || '';
}

/** Number and name as one string, for the places that can only take text — an
 *  `<option>`, an interpolated sentence, a filter predicate. The number leads,
 *  the way the badge draws it. */
export function folderText(folder: NamedFolder): string {
  return [folder.number, folder.name].filter(Boolean).join(' ');
}
