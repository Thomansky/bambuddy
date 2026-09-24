/**
 * File Manager path bar: it folds by width, not by depth.
 *
 * The bar used to hide the middle of any chain deeper than three, which is a
 * proxy for "does not fit" and a poor one — root plus three customer names
 * still overflows a narrow pane, and four short names collapse for nothing.
 * jsdom measures every box as zero, so these tests stub the layout metrics the
 * component reads; the last test leaves them alone to pin the depth fallback
 * that has to hold when nothing can be measured.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor, within, act } from '@testing-library/react';
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
  file_count: 1,
  latest_activity_at: null,
  number: null,
  children: [],
  ...over,
});

// Kunden > RAFI > N1125035 > Bauteile > Freigabe — five levels, so the depth
// rule and a measured fit give visibly different answers.
const mockFolders = [
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
            children: [
              folder({
                id: 4,
                name: 'Bauteile',
                parent_id: 3,
                children: [folder({ id: 5, name: 'Freigabe', parent_id: 4 })],
              }),
            ],
          }),
        ],
      }),
    ],
  }),
];

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

function useHandlers() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(mockFolders)),
    http.get('/api/v1/library/files', () => HttpResponse.json([])),
    http.get('/api/v1/library/stats', () => HttpResponse.json(mockStats)),
    http.get('/api/v1/settings/', () =>
      HttpResponse.json({
        check_updates: false,
        check_printer_firmware: false,
        library_disk_warning_gb: 5,
      }),
    ),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/archives/', () => HttpResponse.json([])),
  );
}

/** Ten px per rendered character: enough to make the arithmetic predictable
 *  without pretending to be a font. Boxes with no text (the ellipsis button,
 *  the copy button) get a flat 24. */
const PX_PER_CHAR = 10;
let containerWidth = 1000;
const originalDescriptors: Record<string, PropertyDescriptor | undefined> = {};

function stubLayout() {
  for (const prop of ['clientWidth', 'offsetWidth', 'offsetLeft'] as const) {
    originalDescriptors[prop] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, prop);
  }
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get: () => containerWidth,
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return (this.textContent ?? '').length * PX_PER_CHAR || 24;
    },
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetLeft', { configurable: true, get: () => 0 });
}

function restoreLayout() {
  for (const [prop, descriptor] of Object.entries(originalDescriptors)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, prop, descriptor);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop];
  }
}

/** ResizeObservers the page created, so a test can fire them by hand — the
 *  global mock in setup.ts never calls back. */
let resizeCallbacks: (() => void)[] = [];

const sidebar = () => within(screen.getByTestId('folder-sidebar'));
const pathBar = () => within(screen.getByTestId('library-path-bar'));

/** The crumb chain as it stands on screen, the root label excluded. */
function crumbNames() {
  const bar = screen.getByTestId('library-path-bar');
  return mockNames.filter((name) => within(bar).queryByText(name) !== null);
}
const mockNames = ['Kunden', 'RAFI', 'N1125035', 'Bauteile', 'Freigabe'];

/** Walk the tree down to the deepest folder and wait for the bar to follow. */
async function openDeepestFolder(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
  await user.click(sidebar().getByText('Freigabe'));
  await waitFor(() => expect(pathBar().getByText('Freigabe')).toBeInTheDocument());
}

describe('FileManagerPage — the path bar folds by width', () => {
  let user: ReturnType<typeof userEvent.setup>;

  beforeEach(() => {
    localStorage.clear();
    resizeCallbacks = [];
    containerWidth = 1000;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: () => void) {
          resizeCallbacks.push(callback);
        }
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    user = userEvent.setup();
    useHandlers();
  });

  afterEach(() => {
    restoreLayout();
    vi.clearAllMocks();
  });

  it('shows every crumb of a five-deep chain when the pane is wide enough', async () => {
    stubLayout();
    render(<FileManagerPage />);
    await openDeepestFolder(user);

    expect(crumbNames()).toEqual(mockNames);
    expect(pathBar().queryByTitle('Show hidden folders')).not.toBeInTheDocument();
  });

  it('folds the middle in a narrow pane and lists what it hid in the menu', async () => {
    containerWidth = 300;
    stubLayout();
    render(<FileManagerPage />);
    await openDeepestFolder(user);

    expect(crumbNames()).toEqual(['Bauteile', 'Freigabe']);

    await user.click(pathBar().getByTitle('Show hidden folders'));
    const menu = within(screen.getByRole('menu'));
    for (const name of ['Kunden', 'RAFI', 'N1125035']) {
      expect(menu.getByText(name)).toBeInTheDocument();
    }
  });

  it('folds only as much as it has to', async () => {
    containerWidth = 400;
    stubLayout();
    render(<FileManagerPage />);
    await openDeepestFolder(user);

    // 90 "All Files" + 24 copy button + 24 ellipsis + 80 + 80 + 80 = 378 fits
    // in 400; adding RAFI (40) does not, and neither does Kunden (60).
    expect(crumbNames()).toEqual(['N1125035', 'Bauteile', 'Freigabe']);
  });

  it('does not collapse a chain of four short names that fits', async () => {
    containerWidth = 1000;
    stubLayout();
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());
    await user.click(sidebar().getByText('Bauteile'));
    await waitFor(() => expect(pathBar().getByText('Bauteile')).toBeInTheDocument());

    // Four crumbs — the depth rule would have folded this one.
    expect(crumbNames()).toEqual(['Kunden', 'RAFI', 'N1125035', 'Bauteile']);
  });

  it('keeps the root and the last two crumbs when nothing fits at all', async () => {
    containerWidth = 40;
    stubLayout();
    render(<FileManagerPage />);
    await openDeepestFolder(user);

    expect(crumbNames()).toEqual(['Bauteile', 'Freigabe']);
    expect(pathBar().getByText('All Files')).toBeInTheDocument();
  });

  it('re-measures when the pane is resized', async () => {
    containerWidth = 300;
    stubLayout();
    render(<FileManagerPage />);
    await openDeepestFolder(user);
    expect(crumbNames()).toEqual(['Bauteile', 'Freigabe']);

    containerWidth = 1000;
    act(() => resizeCallbacks.forEach((fire) => fire()));

    await waitFor(() => expect(crumbNames()).toEqual(mockNames));
  });

  it('falls back to the depth rule when the widths cannot be measured', async () => {
    // No layout stub: jsdom answers 0 for every box, as it does for a pane
    // that is display:none. The bar has to stay sensible rather than fold
    // everything or nothing.
    render(<FileManagerPage />);
    await openDeepestFolder(user);

    expect(crumbNames()).toEqual(['Bauteile', 'Freigabe']);
    expect(pathBar().getByTitle('Show hidden folders')).toBeInTheDocument();
  });
});
