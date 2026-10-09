/** What the File Manager's folders are ordered by (#1770, and the folder number). */
export type FolderSortField = 'name' | 'activity' | 'number';

/** The number a folder sorts under: its own, else the digits its name starts
 *  with — "4016" or "003 Stübbe", from before numbers had a field of their
 *  own, belong in the same run as the folders numbered since. `null` when it
 *  has neither. */
export function folderSortNumber(folder: { number?: string | null; name: string }): string | null {
  const own = folder.number?.trim();
  if (own) return own;
  // The digits count only as a whole word, the rule the backend uses for a
  // name that already starts with its number: "4016" and "003 Stübbe" do,
  // "3D-Teile" does not.
  const leading = /^(\d+)(?![0-9A-Za-z])/.exec(folder.name.trim());
  return leading ? leading[1] : null;
}

/** Numbers compared as numbers where they are digits ("05" before "099",
 *  "4024" before "40100"), as text otherwise. */
export function compareFolderNumbers(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}
