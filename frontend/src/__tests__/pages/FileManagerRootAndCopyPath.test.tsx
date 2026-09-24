/**
 * File Manager: "Copy path", and the root that lists folders instead of every file.
 *
 * Two requests from a print farm that keeps customer jobs in deep trees:
 *  - a path has to be gettable as text, because the same job is also opened in
 *    Explorer and in the slicer. An external folder must yield its REAL path —
 *    the library path is useless outside Bambuddy — and the toast has to say
 *    which of the two landed on the clipboard.
 *  - selecting the root used to issue `getLibraryFiles(null, false, …)`, which
 *    with no folder, project or tag filter means "every file in the library".
 *    With `library_root_lists_all_files` off that request must not be made at
 *    all; the assertions below watch the msw handler, not the DOM, because
 *    fetching-then-hiding would pass a DOM-only check.
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

const mockFolders = [
  folder({
    id: 1,
    name: 'Kunden',
    file_count: 0,
    children: [
      folder({
        id: 2,
        name: 'RAFI',
        parent_id: 1,
        file_count: 1,
        children: [folder({ id: 3, name: 'N1125035', parent_id: 2, file_count: 1 })],
      }),
    ],
  }),
  folder({
    id: 9,
    name: 'NAS Prints',
    file_count: 1,
    is_external: true,
    external_path: '/mnt/nas/prints',
    external_readonly: true,
  }),
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

const rootFile = file({ id: 10, filename: 'loose.3mf' });
const jobFile = file({ id: 11, filename: 'part.3mf', folder_id: 3 });
const nasFile = file({ id: 12, filename: 'nas.3mf', folder_id: 9, is_external: true });

const mockStats = {
  total_files: 3,
  total_folders: 4,
  unfoldered_files: 1,
  unfoldered_external_files: 0,
  total_size_bytes: 104857600,
  files_by_type: {},
  total_prints: 0,
  disk_free_bytes: 10737418240,
  disk_total_bytes: 107374182400,
  disk_used_bytes: 96636764160,
};

/** Every /library/files query string the page issued, newest last. */
let fileRequests: URLSearchParams[];
/** The all-files listing: no folder_id, include_root=false, no tag/search scoping. */
const allFilesRequests = () =>
  fileRequests.filter((p) => !p.has('folder_id') && p.get('include_root') === 'false');

/** What /library/stats answers right now — a move changes it mid-test. */
let statsState = mockStats;

function useHandlers(settings: Record<string, unknown> = {}, stats = mockStats) {
  statsState = stats;
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(mockFolders)),
    http.get('/api/v1/library/files', ({ request }) => {
      const params = new URL(request.url).searchParams;
      fileRequests.push(params);
      const folderId = params.get('folder_id');
      if (folderId === '3') return HttpResponse.json([jobFile]);
      if (folderId === '9') return HttpResponse.json([nasFile]);
      if (folderId) return HttpResponse.json([]);
      if (params.get('include_root') === 'true') return HttpResponse.json([rootFile]);
      return HttpResponse.json([rootFile, jobFile, nasFile]);
    }),
    http.get('/api/v1/library/stats', () => HttpResponse.json(statsState)),
    http.get('/api/v1/settings/', () =>
      HttpResponse.json({
        check_updates: false,
        check_printer_firmware: false,
        library_disk_warning_gb: 5,
        ...settings,
      }),
    ),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/archives/', () => HttpResponse.json([])),
  );
}

const sidebar = () => within(screen.getByTestId('folder-sidebar'));
const pathBar = () => within(screen.getByTestId('library-path-bar'));

/** Open a folder kebab in the tree and return the menu it rendered. */
async function openFolderMenu(user: ReturnType<typeof userEvent.setup>, name: string) {
  const row = sidebar().getByText(name).closest('.group') as HTMLElement;
  await user.click(within(row).getByTitle('Actions'));
  return screen.getByText('Copy path');
}

/** Open a file card's kebab in the grid and return its "Copy path" entry. */
async function openFileMenu(user: ReturnType<typeof userEvent.setup>, filename: string) {
  const card = screen.getByText(filename).closest('.group') as HTMLElement;
  await user.click(within(card).getAllByRole('button').at(-1)!);
  return screen.findByText('Copy path');
}

describe('FileManagerPage — Copy path', () => {
  let writeText: ReturnType<typeof vi.fn>;
  let user: ReturnType<typeof userEvent.setup>;
  let originalClipboard: PropertyDescriptor | undefined;
  let originalIsSecureContext: PropertyDescriptor | undefined;

  beforeEach(() => {
    localStorage.clear();
    fileRequests = [];
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    originalIsSecureContext = Object.getOwnPropertyDescriptor(window, 'isSecureContext');
    // userEvent.setup() installs a clipboard stub of its own, so the real stub
    // has to go on after it or every assertion here watches the wrong spy.
    user = userEvent.setup();
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    useHandlers();
  });

  afterEach(() => {
    if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard);
    if (originalIsSecureContext) Object.defineProperty(window, 'isSecureContext', originalIsSecureContext);
    vi.clearAllMocks();
  });

  it('copies the crumb chain with / separators and without the root label', async () => {
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(sidebar().getByText('N1125035'));
    await waitFor(() => expect(pathBar().getByText('N1125035')).toBeInTheDocument());

    await user.click(pathBar().getByTitle('Copy path'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Kunden/RAFI/N1125035'));
  });

  it('offers no copy control at the root, where there is no path', async () => {
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByTestId('library-path-bar')).toBeInTheDocument());
    expect(pathBar().queryByTitle('Copy path')).not.toBeInTheDocument();
  });

  it('copies a managed folder its library path, and says so', async () => {
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(sidebar().getByText('Kunden'));
    await user.click(await openFolderMenu(user, 'RAFI'));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Kunden/RAFI'));
    expect(await screen.findByText('Path copied')).toBeInTheDocument();
  });

  it('copies an external folder its real external_path, and says which one that is', async () => {
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(await openFolderMenu(user, 'NAS Prints'));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('/mnt/nas/prints'));
    expect(await screen.findByText('Folder path copied')).toBeInTheDocument();
    expect(screen.queryByText('Path copied')).not.toBeInTheDocument();
  });

  it('appends the filename to the library path for a file in a managed folder', async () => {
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(sidebar().getByText('N1125035'));
    await waitFor(() => expect(screen.getByText('part.3mf')).toBeInTheDocument());

    // Grid: the card's kebab menu.
    await user.click(await openFileMenu(user, 'part.3mf'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Kunden/RAFI/N1125035/part.3mf'));
  });

  it('appends the filename to the real path for a file in an external folder', async () => {
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(sidebar().getByText('NAS Prints'));
    await waitFor(() => expect(screen.getByText('nas.3mf')).toBeInTheDocument());

    await user.click(await openFileMenu(user, 'nas.3mf'));

    // The real path the farm pastes into Explorer, not the library name chain.
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('/mnt/nas/prints/nas.3mf'));
    // …and the confirmation names a file, because that is what is on the
    // clipboard: pasting it where a directory is expected does not work.
    expect(await screen.findByText('File path copied')).toBeInTheDocument();
    expect(screen.queryByText('Folder path copied')).not.toBeInTheDocument();
  });

  it('joins a linked Windows share in its own path flavour', async () => {
    const share = folder({
      id: 20,
      name: 'Werkstatt',
      file_count: 1,
      is_external: true,
      external_path: 'C:\\Jobs\\RAFI',
    });
    const shareFile = file({ id: 21, filename: 'jig.3mf', folder_id: 20, is_external: true });
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([share])),
      http.get('/api/v1/library/files', () => HttpResponse.json([shareFile])),
    );
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(sidebar().getByText('Werkstatt'));
    await waitFor(() => expect(screen.getByText('jig.3mf')).toBeInTheDocument());

    await user.click(await openFileMenu(user, 'jig.3mf'));

    // A forward slash appended to a Windows path is exactly the paste this
    // feature exists to remove.
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('C:\\Jobs\\RAFI\\jig.3mf'));
  });

  it('does not throw and shows no confirmation when no clipboard API is available', async () => {
    Object.defineProperty(window, 'isSecureContext', { value: false, configurable: true });
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    const execCommand = vi.fn().mockReturnValue(false);
    const original = document.execCommand;
    document.execCommand = execCommand;

    try {
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
      await user.click(await openFolderMenu(user, 'NAS Prints'));

      await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'));
      expect(screen.queryByText('Folder path copied')).not.toBeInTheDocument();
      // The off-screen textarea the fallback uses is cleaned up either way.
      expect(document.querySelectorAll('textarea').length).toBe(0);
    } finally {
      document.execCommand = original;
    }
  });
});

describe('FileManagerPage — what the root lists', () => {
  beforeEach(() => {
    localStorage.clear();
    fileRequests = [];
  });

  afterEach(() => vi.clearAllMocks());

  it('still lists every file at the root while the setting is on (today’s behaviour)', async () => {
    useHandlers({ library_root_lists_all_files: true });
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getByText('loose.3mf')).toBeInTheDocument());
    expect(screen.getByText('part.3mf')).toBeInTheDocument();
    expect(screen.getByText('nas.3mf')).toBeInTheDocument();
    expect(allFilesRequests().length).toBeGreaterThan(0);
  });

  it('issues no all-files request at all with the setting off, and shows the folders', async () => {
    useHandlers({ library_root_lists_all_files: false });
    render(<FileManagerPage />);

    // The tiles are the content pane now — the folder name renders in the
    // sidebar and again as a tile.
    await waitFor(() => expect(screen.getAllByText('Kunden').length).toBeGreaterThanOrEqual(2));
    expect(screen.queryByText('part.3mf')).not.toBeInTheDocument();
    expect(allFilesRequests()).toHaveLength(0);
  });

  it('offers "No folder" only when unfoldered files exist, and opens exactly their listing', async () => {
    useHandlers({ library_root_lists_all_files: false });
    const user = userEvent.setup();
    render(<FileManagerPage />);

    const entry = await screen.findByTestId('no-folder-entry');
    await user.click(entry);

    await waitFor(() => expect(screen.getByText('loose.3mf')).toBeInTheDocument());
    // Opened via include_root=true — the unfoldered files, not the whole library.
    expect(fileRequests.some((p) => p.get('include_root') === 'true' && !p.has('folder_id'))).toBe(true);
    expect(allFilesRequests()).toHaveLength(0);
  });

  it('leaves the folder tiles out of "No folder", which is defined as what they do not hold', async () => {
    useHandlers({ library_root_lists_all_files: false });
    const user = userEvent.setup();
    render(<FileManagerPage />);

    // Each top-level folder renders twice at the root: sidebar entry + tile.
    await waitFor(() => expect(screen.getAllByText('Kunden').length).toBeGreaterThanOrEqual(2));
    const atRoot = screen.getAllByText('Kunden').length;

    await user.click(await screen.findByTestId('no-folder-entry'));
    await waitFor(() => expect(screen.getByText('loose.3mf')).toBeInTheDocument());

    // The crumb says "No folder" — the pane under it must not still be
    // offering the very folders the view excludes.
    expect(pathBar().getByText('No folder')).toBeInTheDocument();
    expect(screen.getAllByText('Kunden')).toHaveLength(atRoot - 1);
    expect(screen.queryByTestId('no-folder-entry')).not.toBeInTheDocument();
  });

  it('keeps the search box at the root of a library that has no folders yet', async () => {
    // Search is the documented way back to the flat listing with the setting
    // off. A library with no folder rows used to lose the whole toolbar here,
    // which made that override unreachable without a reload.
    useHandlers({ library_root_lists_all_files: false });
    server.use(http.get('/api/v1/library/folders', () => HttpResponse.json([])));
    const user = userEvent.setup();
    render(<FileManagerPage />);

    expect(await screen.findByTestId('no-folder-entry')).toBeInTheDocument();
    expect(allFilesRequests()).toHaveLength(0);

    await user.type(screen.getByPlaceholderText('Search files...'), 'part');

    await waitFor(() => expect(screen.getByText('part.3mf')).toBeInTheDocument());
    expect(allFilesRequests().length).toBeGreaterThan(0);
  });

  it('offers "No folder" for the files a move just took out of every folder', async () => {
    // The entry is driven entirely by the stats count and the root issues no
    // file listing of its own, so a move that leaves ['library-stats'] cached
    // makes the files it just moved unreachable until a reload.
    useHandlers({ library_root_lists_all_files: false }, { ...mockStats, unfoldered_files: 0 });
    let moved = false;
    server.use(
      http.get('/api/v1/library/files', ({ request }) => {
        const params = new URL(request.url).searchParams;
        fileRequests.push(params);
        if (params.get('folder_id') === '3') return HttpResponse.json(moved ? [] : [jobFile]);
        if (params.has('folder_id')) return HttpResponse.json([]);
        if (params.get('include_root') === 'true') return HttpResponse.json(moved ? [jobFile] : []);
        return HttpResponse.json([rootFile, jobFile, nasFile]);
      }),
      http.post('/api/v1/library/files/move', () => {
        moved = true;
        statsState = { ...mockStats, unfoldered_files: 1 };
        return HttpResponse.json({ status: 'success', moved: 1 });
      }),
    );
    const user = userEvent.setup();
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getAllByText('Kunden').length).toBeGreaterThanOrEqual(2));
    expect(screen.queryByTestId('no-folder-entry')).not.toBeInTheDocument();

    await user.click(sidebar().getByText('N1125035'));
    await waitFor(() => expect(screen.getByText('part.3mf')).toBeInTheDocument());
    await user.click(within(screen.getByTestId('library-filter-card')).getByText('Select All'));
    await user.click(within(screen.getByTestId('selection-actions')).getByText('Move'));
    await user.click(within(screen.getByTestId('move-folder-list')).getByText('Root (No Folder)'));
    await user.click(within(screen.getByTestId('move-files-modal')).getByText('Move'));
    await waitFor(() => expect(screen.queryByTestId('move-files-modal')).not.toBeInTheDocument());

    await user.click(pathBar().getByText('All Files'));

    expect(await screen.findByTestId('no-folder-entry')).toBeInTheDocument();
    expect(allFilesRequests()).toHaveLength(0);
  });

  it('hides "No folder" when nothing sits outside a folder', async () => {
    useHandlers({ library_root_lists_all_files: false }, { ...mockStats, unfoldered_files: 0 });
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getAllByText('Kunden').length).toBeGreaterThanOrEqual(2));
    expect(screen.queryByTestId('no-folder-entry')).not.toBeInTheDocument();
  });

  it('brings the flat listing back for a search at the root, setting off', async () => {
    useHandlers({ library_root_lists_all_files: false });
    const user = userEvent.setup();
    render(<FileManagerPage />);

    await waitFor(() => expect(screen.getAllByText('Kunden').length).toBeGreaterThanOrEqual(2));
    expect(allFilesRequests()).toHaveLength(0);

    await user.type(screen.getByPlaceholderText('Search files...'), 'part');

    await waitFor(() => expect(screen.getByText('part.3mf')).toBeInTheDocument());
    expect(allFilesRequests().length).toBeGreaterThan(0);
  });

  it('keeps the library statistics counting the whole library either way', async () => {
    // Not the listing's length: the header comes from getLibraryStats() and
    // must still say 3 files / 4 folders while the root shows only 2 tiles.
    const statsBar = () => screen.getByText('Files:').closest('div')!.parentElement!;

    useHandlers({ library_root_lists_all_files: false });
    const { unmount } = render(<FileManagerPage />);
    await waitFor(() => expect(screen.getAllByText('Kunden').length).toBeGreaterThanOrEqual(2));
    const foldersFirst = statsBar().textContent;
    expect(foldersFirst).toContain('Files:3');
    expect(foldersFirst).toContain('Folders:4');
    unmount();

    fileRequests = [];
    useHandlers({ library_root_lists_all_files: true });
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByText('loose.3mf')).toBeInTheDocument());
    expect(statsBar().textContent).toBe(foldersFirst);
  });
});
