/**
 * The running number an order folder is filed under.
 *
 * The number is born on the folder while the enquiry is still an enquiry, and
 * an order folder is very often nothing but that number — so every place that
 * draws a folder has to draw it, or the row comes out blank and two orders on
 * screen cannot be told apart.
 */

/** Same structural shape the pages that draw folders already pass around, so
 *  both a page-local `t` and i18next's own satisfy it. */
type TFunction = (key: string, options?: Record<string, unknown>) => string;

/** The folder's running number, drawn before the name the way the project
 *  number sits next to the project name. Its own element, never part of the
 *  name: a number baked into a name would not survive a rename, and this one
 *  is what the quote and the invoice are filed under.
 *
 *  Drawn to be read at a glance — it is what an order is looked up by: the
 *  text colour rather than the muted grey, semibold with even-width digits,
 *  and an accent outline that sets it apart from the name on every row
 *  background, hovered or selected. */
export function FolderNumber({ number, t }: { number?: string | null; t: TFunction }) {
  if (!number) return null;
  return (
    <span
      data-testid="folder-number"
      title={t('fileManager.folderNumber')}
      className="text-[13px] leading-4 font-mono font-semibold tabular-nums px-1.5 py-0.5 rounded border border-bambu-green/60 bg-bambu-green/10 text-white whitespace-nowrap flex-shrink-0"
    >
      {number}
    </span>
  );
}
