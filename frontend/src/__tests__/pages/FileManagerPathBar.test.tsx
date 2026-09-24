/**
 * File Manager: the path bar as Explorer draws it.
 *
 * Two things the row of crumbs did not do, and the owner kept reaching for:
 *  - the empty space past the last crumb is an edit affordance. Clicking it
 *    (or F2) turns the chain into a text box holding the path, selected, so
 *    Ctrl+C takes it and typing replaces it. Enter resolves what was typed —
 *    case-insensitively, with either separator — and an unknown path has to
 *    stay on screen to be corrected rather than silently going somewhere else.
 *  - the `›` between two crumbs lists what sits inside the crumb to its left,
 *    so you can step from a deep folder straight to one of its uncles.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: () => <div data-testid="model-viewer-modal" />,
}));

const folder = (over: Record<string, unknown>) => ({
  parent_id: null,
  project_id: null,
  archive_id: null,
  project_name: null,
  archive_name: null,
  is_external: false,
  external_path: null,
  external_readonly: false,
  file_count: 0,
  latest_activity_at: null,
  children: [],
  ...over,
});

// Kunden › RAFI › N1125035, with a sibling at each level so a sideways step
// has somewhere to land.
const mockFolders = [
  folder({
    id: 1,
    name: 'Kunden',
    children: [
      folder({
        id: 2,
        name: 'RAFI',
        parent_id: 1,
        children: [folder({ id: 3, name: 'N1125035', parent_id: 2, file_count: 1 })],
      }),
      folder({ id: 4, name: 'Siemens', parent_id: 1 }),
    ],
  }),
  folder({ id: 5, name: 'Intern' }),
];

const file = (over: Record<string, unknown>) => ({
  file_size: 1024,
  file_type: '3mf',
  thumbnail_path: null,
  print_name: null,
  print_time_seconds: null,
  print_count: 0,
  duplicate_count: 0,
  created_by_id: null,
  created_by_username: null,
  created_at: '2024-01-01T00:00:00Z',
  fs_modified_at: null,
  is_external: false,
  folder_id: null,
  ...over,
});

const jobFile = file({ id: 11, filename: 'part.3mf', folder_id: 3 });

const mockStats = {
  total_files: 1,
  total_folders: 5,
  unfoldered_files: 0,
  unfoldered_external_files: 0,
  total_size_bytes: 1024,
  files_by_type: {},
  total_prints: 0,
  disk_free_bytes: 10737418240,
  disk_total_bytes: 107374182400,
  disk_used_bytes: 96636764160,
};

/** Every folder_id the page asked for a listing of, in order. */
let folderRequests: string[];

function useHandlers() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(mockFolders)),
    http.get('/api/v1/library/files', ({ request }) => {
      const params = new URL(request.url).searchParams;
      const folderId = params.get('folder_id');
      if (folderId) folderRequests.push(folderId);
      return HttpResponse.json(folderId === '3' ? [jobFile] : []);
    }),
    http.get('/api/v1/library/stats', () => HttpResponse.json(mockStats)),
    http.get('/api/v1/settings/', () =>
      HttpResponse.json({
        check_updates: false,
        check_printer_firmware: false,
        library_disk_warning_gb: 5,
        library_root_view: 'all',
      }),
    ),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/archives/', () => HttpResponse.json([])),
  );
}

const sidebar = () => within(screen.getByTestId('folder-sidebar'));
const pathBar = () => within(screen.getByTestId('library-path-bar'));

/**
 * The crumb row clips whatever leaves it, so a long chain can never push the
 * pane wider — which also clips away a menu rendered inside it: the list opens
 * below a one-line row, entirely outside the clip rect, and z-index does not
 * escape an overflow clip. jsdom applies no stylesheet and lays nothing out,
 * so that is invisible to a click here; what a test can pin is that the list
 * is not inside the element doing the clipping. The class list is the only
 * record of it without a layout engine.
 */
function clippingAncestorOf(el: HTMLElement): HTMLElement | null {
  for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
    if (node.classList.contains('overflow-hidden')) return node;
  }
  return null;
}

/** Select Kunden › RAFI › N1125035 through the tree and wait for the crumbs. */
async function standInTheJobFolder(user: ReturnType<typeof userEvent.setup>) {
  render(<FileManagerPage />);
  await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
  await user.click(sidebar().getByText('N1125035'));
  await waitFor(() => expect(pathBar().getByText('N1125035')).toBeInTheDocument());
  folderRequests = [];
}

describe('FileManagerPage — typing a path into the bar', () => {
  let user: ReturnType<typeof userEvent.setup>;

  beforeEach(() => {
    localStorage.clear();
    folderRequests = [];
    user = userEvent.setup();
    useHandlers();
  });

  afterEach(() => vi.clearAllMocks());

  it('turns the crumbs into an input holding the whole path, selected', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));

    const input = (await screen.findByLabelText('Edit path')) as HTMLInputElement;
    expect(input).toHaveValue('Kunden/RAFI/N1125035');
    // Selected on open, so Ctrl+C takes it and typing replaces it.
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe('Kunden/RAFI/N1125035'.length);
    expect(screen.queryByTestId('library-path-bar')).not.toBeInTheDocument();
  });

  it('opens the same input on F2 from the bar', async () => {
    await standInTheJobFolder(user);

    pathBar().getByLabelText('Edit path').focus();
    await user.keyboard('{F2}');

    expect(await screen.findByLabelText('Edit path')).toHaveValue('Kunden/RAFI/N1125035');
  });

  it('selects the folder a typed path names, on Enter', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.type(screen.getByLabelText('Edit path'), 'Kunden/Siemens{Enter}');

    await waitFor(() => expect(pathBar().getByText('Siemens')).toBeInTheDocument());
    expect(folderRequests).toContain('4');
  });

  it('does not care about case, and takes backslashes too', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.type(screen.getByLabelText('Edit path'), 'kunden\\SIEMENS{Enter}');

    await waitFor(() => expect(pathBar().getByText('Siemens')).toBeInTheDocument());
    expect(folderRequests).toContain('4');
  });

  it('tolerates a leading and a trailing separator', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.type(screen.getByLabelText('Edit path'), '/Kunden/Siemens/{Enter}');

    await waitFor(() => expect(pathBar().getByText('Siemens')).toBeInTheDocument());
  });

  it('reads an empty input as the root', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.keyboard('{Enter}');

    // Back at the plain root the bar has nothing left to draw and goes away,
    // which is how you can tell the empty input really navigated.
    await waitFor(() => expect(screen.queryByTestId('library-path-bar')).not.toBeInTheDocument());
    // The folder is still in the tree, of course — it is just no longer the
    // selected one, which is what the vanished bar says.
  });

  it('keeps an unknown path on screen, marks it invalid and stays put', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.type(screen.getByLabelText('Edit path'), 'Kunden/Nirgendwo{Enter}');

    const input = screen.getByLabelText('Edit path');
    // Still open, still holding the typo — it is one character from correct.
    expect(input).toHaveValue('Kunden/Nirgendwo');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(await screen.findByText('No such folder')).toBeInTheDocument();
    expect(folderRequests).toEqual([]);
  });

  it('restores the crumbs unchanged on Escape', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.type(screen.getByLabelText('Edit path'), 'Kunden');
    await user.keyboard('{Escape}');

    await waitFor(() => expect(screen.getByTestId('library-path-bar')).toBeInTheDocument());
    expect(pathBar().getByText('N1125035')).toBeInTheDocument();
    expect(folderRequests).toEqual([]);
  });

  it('hands focus back to the bar on Escape, rather than dropping it on <body>', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.keyboard('{Escape}');

    await waitFor(() => expect(screen.getByTestId('library-path-bar')).toBeInTheDocument());
    // The input unmounts with the whole bar; without a hand-back the browser
    // falls to <body> and the next Tab restarts at the top of the page.
    expect(pathBar().getByLabelText('Edit path')).toHaveFocus();
  });

  it('restores the crumbs on blur, so a click elsewhere never navigates', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getByLabelText('Edit path'));
    await user.clear(await screen.findByLabelText('Edit path'));
    await user.type(screen.getByLabelText('Edit path'), 'Kunden');
    await user.click(screen.getByPlaceholderText('Search files...'));

    await waitFor(() => expect(screen.getByTestId('library-path-bar')).toBeInTheDocument());
    expect(pathBar().getByText('N1125035')).toBeInTheDocument();
    expect(folderRequests).toEqual([]);
  });

  it('keeps the copy button beside the crumbs', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    await standInTheJobFolder(user);
    await user.click(pathBar().getByTitle('Copy path'));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Kunden/RAFI/N1125035'));
  });
});

describe('FileManagerPage — stepping sideways from a separator', () => {
  let user: ReturnType<typeof userEvent.setup>;

  beforeEach(() => {
    localStorage.clear();
    folderRequests = [];
    user = userEvent.setup();
    useHandlers();
  });

  afterEach(() => vi.clearAllMocks());

  it('lists the left crumb’s folders, and selects the one clicked', async () => {
    await standInTheJobFolder(user);

    // The second separator sits after "Kunden", so it lists Kunden's folders.
    await user.click(pathBar().getAllByLabelText('Folders at this level')[1]);
    const menu = within(await screen.findByRole('menu'));
    expect(menu.getByText('RAFI')).toBeInTheDocument();

    await user.click(menu.getByText('Siemens'));

    await waitFor(() => expect(pathBar().getByText('Siemens')).toBeInTheDocument());
    expect(folderRequests).toContain('4');
    // Stepped sideways, not up: RAFI is gone from the chain.
    expect(pathBar().queryByText('RAFI')).not.toBeInTheDocument();
  });

  it('lists the top-level folders on the separator after the root', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getAllByLabelText('Folders at this level')[0]);
    const menu = within(await screen.findByRole('menu'));
    expect(menu.getByText('Kunden')).toBeInTheDocument();

    await user.click(menu.getByText('Intern'));

    await waitFor(() => expect(pathBar().getByText('Intern')).toBeInTheDocument());
    expect(folderRequests).toContain('5');
  });

  it('opens the list outside the row that clips, so it is on screen at all', async () => {
    await standInTheJobFolder(user);

    await user.click(pathBar().getAllByLabelText('Folders at this level')[0]);
    const menu = await screen.findByRole('menu');

    // Inside the bar the list would be painted into the clipped strip below a
    // one-line row: present to this test, invisible and unclickable in a
    // browser. It hangs off <body> instead, pinned under its button.
    expect(screen.getByTestId('library-path-bar').contains(menu)).toBe(false);
    expect(clippingAncestorOf(menu)).toBeNull();
    expect(menu.parentElement).toBe(document.body);
  });

  it('opens the collapsed-middle list outside the clipper too', async () => {
    // Root plus four folders is the first chain that folds, so the ellipsis
    // menu — same row, same clip — only appears this deep.
    server.use(
      http.get('/api/v1/library/folders', () =>
        HttpResponse.json([
          folder({
            id: 1,
            name: 'Kunden',
            children: [
              folder({
                id: 2,
                name: 'RAFI',
                parent_id: 1,
                children: [
                  folder({
                    id: 3,
                    name: 'N1125035',
                    parent_id: 2,
                    children: [folder({ id: 6, name: 'Freigabe', parent_id: 3, file_count: 1 })],
                  }),
                ],
              }),
            ],
          }),
        ]),
      ),
    );
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(sidebar().getByText('Freigabe'));
    await waitFor(() => expect(pathBar().getByText('Freigabe')).toBeInTheDocument());

    await user.click(pathBar().getByLabelText('Show hidden folders'));
    const menu = await screen.findByRole('menu');

    expect(within(menu).getByText('Kunden')).toBeInTheDocument();
    expect(screen.getByTestId('library-path-bar').contains(menu)).toBe(false);
    expect(clippingAncestorOf(menu)).toBeNull();
  });

  it('walks the list with the arrow keys and closes on Escape', async () => {
    await standInTheJobFolder(user);

    const trigger = pathBar().getAllByLabelText('Folders at this level')[0];
    trigger.focus();
    await user.keyboard('{Enter}');

    const menu = await screen.findByRole('menu');
    // The list is in the tree's own order; opening focuses its first entry and
    // one ArrowDown reaches the second.
    const items = within(menu).getAllByRole('menuitem');
    await waitFor(() => expect(items[0]).toHaveFocus());
    await user.keyboard('{ArrowDown}');
    expect(items[1]).toHaveFocus();

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });
});
