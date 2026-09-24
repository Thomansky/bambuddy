/**
 * File Manager: the folder ordering and display controls live in the toolbar.
 *
 * They used to sit in the folder sidebar's header, which meant switching the
 * sidebar off took them with it — while the ordering itself carried on driving
 * the content tiles, the columns and the path bar, and survived a reload. A
 * capability you cannot reach any more, with nothing on screen to say why.
 * They are one menu in the toolbar now, rendered whether the tree is on screen
 * or not, and the sidebar header is back to its title and its resize handle.
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
  file_count: 1,
  latest_activity_at: null,
  number: null,
  children: [],
  ...over,
});

// Alphabetical and by-activity disagree — Zulu was touched first, Alpha last —
// so a sort change is visible whichever of the two controls makes it.
const mockFolders = [
  folder({ id: 1, name: 'Alpha', latest_activity_at: '2024-06-01T00:00:00Z' }),
  folder({ id: 2, name: 'Zulu', latest_activity_at: '2024-01-01T00:00:00Z' }),
];

const mockStats = {
  total_files: 0,
  total_folders: 2,
  unfoldered_files: 0,
  unfoldered_external_files: 0,
  total_size_bytes: 0,
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

/** The two folder names in the order they stand in a given region. */
function orderIn(scope: HTMLElement) {
  return ['Alpha', 'Zulu']
    .map((name) => ({ name, el: within(scope).queryAllByText(name)[0] }))
    .filter((entry): entry is { name: string; el: HTMLElement } => Boolean(entry.el))
    .sort((a, b) =>
      a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
    )
    .map((entry) => entry.name);
}

const menuButton = () => screen.getByTestId('folder-display-menu');

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(menuButton());
  return screen.getByRole('menu');
}

describe('FileManagerPage — folder display menu', () => {
  let user: ReturnType<typeof userEvent.setup>;
  const getItemMock = localStorage.getItem as ReturnType<typeof vi.fn>;
  const setItemMock = localStorage.setItem as ReturnType<typeof vi.fn>;

  beforeEach(() => {
    getItemMock.mockReset();
    setItemMock.mockReset();
    getItemMock.mockReturnValue(null);
    user = userEvent.setup();
    useHandlers();
  });

  afterEach(() => {
    getItemMock.mockReset();
    setItemMock.mockReset();
    vi.clearAllMocks();
  });

  it('carries all four controls, and the sidebar header carries none of them', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    const header = screen.getByTestId('folder-sidebar').querySelector('.border-b') as HTMLElement;
    expect(within(header).getByText('Folders')).toBeInTheDocument();
    expect(within(header).queryByRole('button')).not.toBeInTheDocument();
    expect(within(header).queryByRole('combobox')).not.toBeInTheDocument();

    const menu = await openMenu(user);
    expect(within(menu).getByRole('menuitemradio', { name: 'By name' })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitemradio', { name: 'By recent activity' })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitemradio', { name: 'Ascending' })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitemradio', { name: 'Descending' })).toBeInTheDocument();
    expect(
      within(menu).getByRole('menuitemcheckbox', { name: 'Collapse folders by default' }),
    ).toBeInTheDocument();
    expect(
      within(menu).getByRole('menuitemcheckbox', { name: 'Enable text wrapping' }),
    ).toBeInTheDocument();
  });

  it('keeps the sort field and the sort direction in separate radio sets', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    const menu = await openMenu(user);
    // A menuitemradio is checked against the others in its group, so two
    // two-way choices need a group each. One set carrying both "By name" and
    // "Ascending" as checked describes neither of them, and a separator
    // between the pairs does not split the set.
    const groups = within(menu)
      .getAllByRole('group')
      .filter((group) => within(group).queryAllByRole('menuitemradio').length > 0);
    expect(groups).toHaveLength(2);
    for (const group of groups) {
      const radios = within(group).getAllByRole('menuitemradio');
      expect(radios).toHaveLength(2);
      expect(radios.filter((radio) => radio.getAttribute('aria-checked') === 'true')).toHaveLength(1);
    }
    expect(groups[0]).toHaveAttribute('aria-label', 'Sort folders');
    expect(within(groups[0]).getByRole('menuitemradio', { name: 'By name' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(groups[1]).toHaveAttribute('aria-label', 'Sort direction');
    expect(within(groups[1]).getByRole('menuitemradio', { name: 'Ascending' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });

  it('is in the toolbar with the sidebar shown and with it hidden', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    const toolbar = screen.getByTestId('file-manager-actions');
    expect(toolbar.contains(menuButton())).toBe(true);

    await user.click(screen.getByTestId('toggle-folder-sidebar'));
    await waitFor(() => expect(screen.queryByTestId('folder-sidebar')).not.toBeInTheDocument());

    expect(screen.getByTestId('file-manager-actions').contains(menuButton())).toBe(true);
    await openMenu(user);
    expect(screen.getByRole('menuitemradio', { name: 'Descending' })).toBeInTheDocument();
  });

  it('reorders the tree, the content tiles and the columns from one place', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    expect(orderIn(screen.getByTestId('folder-sidebar'))).toEqual(['Alpha', 'Zulu']);

    await openMenu(user);
    await user.click(screen.getByRole('menuitemradio', { name: 'Descending' }));

    await waitFor(() =>
      expect(orderIn(screen.getByTestId('folder-sidebar'))).toEqual(['Zulu', 'Alpha']),
    );
    expect(setItemMock).toHaveBeenCalledWith('library-folder-sort-direction', 'desc');

    // The columns view draws its folders in its own panes...
    await user.click(screen.getByTitle('Column view'));
    await waitFor(() => expect(screen.getByTestId('columns-view')).toBeInTheDocument());
    expect(orderIn(screen.getByTestId('columns-level-root'))).toEqual(['Zulu', 'Alpha']);

    // ...and the content-area tiles that stand in for the tree follow too.
    await user.click(screen.getByTitle('Grid view'));
    await user.click(screen.getByTestId('toggle-folder-sidebar'));
    await waitFor(() => expect(screen.getByTestId('content-folder-nav')).toBeInTheDocument());
    expect(orderIn(screen.getByTestId('content-folder-nav'))).toEqual(['Zulu', 'Alpha']);
  });

  it('reorders by recent activity when the sort field is changed', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    await openMenu(user);
    await user.click(screen.getByRole('menuitemradio', { name: 'By recent activity' }));

    await waitFor(() =>
      expect(orderIn(screen.getByTestId('folder-sidebar'))).toEqual(['Zulu', 'Alpha']),
    );
    expect(setItemMock).toHaveBeenCalledWith('library-folder-sort-field', 'activity');
  });

  it('round-trips the collapse and wrap preferences through localStorage', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    const menu = await openMenu(user);
    await user.click(within(menu).getByRole('menuitemcheckbox', { name: 'Collapse folders by default' }));
    expect(setItemMock).toHaveBeenCalledWith('library-collapse-folders', 'true');
    await user.click(within(menu).getByRole('menuitemcheckbox', { name: 'Enable text wrapping' }));
    expect(setItemMock).toHaveBeenCalledWith('library-wrap-folders', 'true');

    await waitFor(() =>
      expect(
        within(screen.getByRole('menu')).getByRole('menuitemcheckbox', {
          name: 'Collapse folders by default',
        }),
      ).toHaveAttribute('aria-checked', 'true'),
    );
  });

  it('reads the stored preferences back on load', async () => {
    getItemMock.mockImplementation((key: string) => {
      if (key === 'library-collapse-folders') return 'true';
      if (key === 'library-wrap-folders') return 'true';
      if (key === 'library-folder-sort-direction') return 'desc';
      if (key === 'library-folder-sort-field') return 'activity';
      return null;
    });
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    const menu = await openMenu(user);
    expect(within(menu).getByRole('menuitemradio', { name: 'By recent activity' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(within(menu).getByRole('menuitemradio', { name: 'Descending' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(
      within(menu).getByRole('menuitemcheckbox', { name: 'Collapse folders by default' }),
    ).toHaveAttribute('aria-checked', 'true');
    expect(
      within(menu).getByRole('menuitemcheckbox', { name: 'Enable text wrapping' }),
    ).toHaveAttribute('aria-checked', 'true');
  });

  it('opens with the keyboard, walks the entries with the arrows and closes on Escape', async () => {
    render(<FileManagerPage />);
    await waitFor(() => expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument());

    menuButton().focus();
    await user.keyboard('{Enter}');

    const menu = screen.getByRole('menu');
    const entries = within(menu).getAllByRole('menuitemradio');
    expect(document.activeElement).toBe(entries[0]);

    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(entries[1]);
    await user.keyboard('{ArrowUp}{ArrowUp}');
    expect(document.activeElement).toBe(
      within(menu).getAllByRole('menuitemcheckbox').at(-1),
    );

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument());
    expect(document.activeElement).toBe(menuButton());

    // Space opens it again.
    await user.keyboard(' ');
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });
});
