/**
 * CSV for the maintenance logbook export.
 *
 * Semicolon-separated with a byte-order mark: that is what a German Excel
 * opens straight into columns with the umlauts intact, and every other
 * spreadsheet reads it too. Every field is quoted, so a note with a line
 * break or a semicolon stays one cell.
 */

// Excel reads the file as UTF-8 only when it starts with this.
const BYTE_ORDER_MARK = String.fromCharCode(0xfeff);

export function toCsv(header: string[], rows: string[][]): string {
  const quote = (value: string) => `"${value.replace(/"/g, '""')}"`;
  const lines = [header, ...rows].map((row) => row.map(quote).join(';'));
  return `${BYTE_ORDER_MARK}${lines.join('\r\n')}\r\n`;
}

/** A local timestamp that sorts as text: 2026-09-27 15:42. */
export function sortableLocalTime(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** wartungsbuch-h2s-08-2026-09-27.csv — the printer part only when filtered to one. */
export function logbookFileName(base: string, printerName: string | null, date: Date): string {
  const slug = (printerName ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  const day = sortableLocalTime(date).slice(0, 10);
  return `${[base, slug, day].filter(Boolean).join('-')}.csv`;
}
