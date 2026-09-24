/**
 * Tests for the FileManagerPage component.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { openInSlicer } from '../../utils/slicer';
import { setAuthToken } from '../../api/client';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

// Only the protocol-handler launch is stubbed — it would navigate the jsdom
// window. Everything else in the module is a pure predicate, so keep the real
// implementations: isSliceableFilename decides which rows even offer the
// action these tests click.
vi.mock('../../utils/slicer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/slicer')>()),
  openInSlicer: vi.fn(),
}));

vi.mock('../../components/SliceModal', () => ({
  SliceModal: ({ source }: { source: { filename: string } }) => (
    <div data-testid="slice-modal">{source.filename}</div>
  ),
}));

// The viewer pulls in three.js; the tests only care whether it opened.
vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: ({ title }: { title: string }) => <div data-testid="model-viewer-modal">{title}</div>,
}));

// Mock data
const mockFolders = [
  {
    id: 1,
    name: 'Functional Parts',
    parent_id: null,
    file_count: 5,
    project_id: null,
    archive_id: null,
    project_name: null,
    archive_name: null,
    // #2680: distinctive year so the folder-pane display test can assert on it
    // without colliding with the file mtimes below.
    latest_activity_at: '2031-04-05T10:00:00Z',
    children: [
      {
        id: 2,
        name: 'Brackets',
        parent_id: 1,
        file_count: 3,
        project_id: null,
        archive_id: null,
        project_name: null,
        archive_name: null,
        latest_activity_at: '2032-06-07T10:00:00Z',
        children: [],
      },
    ],
  },
  {
    id: 3,
    name: 'Art Projects',
    parent_id: null,
    file_count: 2,
    project_id: 1,
    archive_id: null,
    project_name: 'My Art Project',
    archive_name: null,
    // No activity timestamp — must render no date line rather than an
    // "Invalid Date" placeholder.
    latest_activity_at: null,
    children: [],
  },
];

const mockFiles = [
  {
    id: 1,
    filename: 'benchy.gcode.3mf',
    file_path: '/library/benchy.gcode.3mf',
    file_size: 1048576,
    file_type: '3mf',
    folder_id: null,
    thumbnail_path: '/thumbnails/1.png',
    print_name: 'Benchy',
    print_time_seconds: 3600,
    print_count: 5,
    duplicate_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    // #2680: real on-disk mtime in a distinctive year so the display test can
    // prove fs_modified_at is preferred over created_at (2024).
    fs_modified_at: '2030-06-15T12:00:00Z',
  },
  {
    id: 2,
    filename: 'bracket.stl',
    file_path: '/library/bracket.stl',
    file_size: 524288,
    file_type: 'stl',
    folder_id: null,
    thumbnail_path: null,
    print_name: null,
    print_time_seconds: null,
    print_count: 0,
    duplicate_count: 2,
    created_at: '2024-01-02T00:00:00Z',
  },
  {
    id: 3,
    filename: 'cube.gcode.3mf',
    file_path: '/library/cube.gcode.3mf',
    file_size: 2048576,
    file_type: '3mf',
    folder_id: null,
    thumbnail_path: '/thumbnails/3.png',
    print_name: 'Cube',
    print_time_seconds: 1800,
    print_count: 2,
    duplicate_count: 0,
    created_at: '2024-01-03T00:00:00Z',
  },
];

// Per-folder contents. The columns view lists every level's files, so each
// folder needs its own set — otherwise the same name shows up in two columns
// and the queries below cannot tell them apart.
const mockFolderFiles: Record<string, Record<string, unknown>[]> = {
  '1': [
    {
      id: 11,
      filename: 'spacer.3mf',
      file_path: '/library/functional/spacer.3mf',
      file_size: 131072,
      file_type: '3mf',
      folder_id: 1,
      thumbnail_path: null,
      print_name: 'Spacer',
      print_time_seconds: 900,
      print_count: 0,
      duplicate_count: 0,
      created_at: '2024-02-01T00:00:00Z',
    },
  ],
  '2': [
    {
      id: 12,
      filename: 'clamp.stl',
      file_path: '/library/functional/brackets/clamp.stl',
      file_size: 65536,
      file_type: 'stl',
      folder_id: 2,
      thumbnail_path: null,
      print_name: null,
      print_time_seconds: null,
      print_count: 0,
      duplicate_count: 0,
      created_at: '2024-02-02T00:00:00Z',
    },
    {
      id: 13,
      filename: 'hinge.3mf',
      file_path: '/library/functional/brackets/hinge.3mf',
      file_size: 98304,
      file_type: '3mf',
      folder_id: 2,
      thumbnail_path: null,
      print_name: 'Hinge',
      print_time_seconds: 1200,
      print_count: 1,
      duplicate_count: 0,
      created_at: '2024-02-03T00:00:00Z',
    },
  ],
  '3': [
    {
      id: 14,
      filename: 'vase.3mf',
      file_path: '/library/art/vase.3mf',
      file_size: 262144,
      file_type: '3mf',
      folder_id: 3,
      thumbnail_path: null,
      print_name: 'Vase',
      print_time_seconds: 5400,
      print_count: 0,
      duplicate_count: 0,
      created_at: '2024-02-04T00:00:00Z',
    },
  ],
};

// A request carrying folder_id wants that folder's own files; without one it
// is the root level (columns view) or the whole library ("All Files").
const filesForRequest = (request: Request) => {
  const folderId = new URL(request.url).searchParams.get('folder_id');
  return folderId ? (mockFolderFiles[folderId] ?? []) : mockFiles;
};

// A chain long enough to make the path bar collapse (root + four folders).
const deepFolders = [
  {
    id: 10,
    name: 'Kunden',
    parent_id: null,
    file_count: 0,
    project_id: null,
    archive_id: null,
    project_name: null,
    archive_name: null,
    latest_activity_at: null,
    children: [
      {
        id: 11,
        name: 'RAFI',
        parent_id: 10,
        file_count: 0,
        project_id: null,
        archive_id: null,
        project_name: null,
        archive_name: null,
        latest_activity_at: null,
        children: [
          {
            id: 12,
            name: 'N1125035',
            parent_id: 11,
            file_count: 0,
            project_id: null,
            archive_id: null,
            project_name: null,
            archive_name: null,
            latest_activity_at: null,
            children: [
              {
                id: 13,
                name: 'Revision A',
                parent_id: 12,
                file_count: 0,
                project_id: null,
                archive_id: null,
                project_name: null,
                archive_name: null,
                latest_activity_at: null,
                children: [],
              },
            ],
          },
        ],
      },
    ],
  },
];

const pathBar = () => within(screen.getByTestId('library-path-bar'));
// The crumb for the current location is plain text, not a link.
const currentCrumb = () => screen.getByTestId('library-path-bar').querySelector('[aria-current="page"]');
const sidebar = () => within(screen.getByTestId('folder-sidebar'));

const mockStats = {
  total_files: 10,
  total_folders: 3,
  total_size_bytes: 104857600,
  disk_free_bytes: 10737418240,
  disk_total_bytes: 107374182400,
};

describe('FileManagerPage', () => {
  beforeEach(() => {
    // Clear localStorage to ensure consistent view mode
    localStorage.clear();

    server.use(
      http.get('/api/v1/library/folders', () => {
        return HttpResponse.json(mockFolders);
      }),
      http.get('/api/v1/library/files', ({ request }) => {
        return HttpResponse.json(filesForRequest(request));
      }),
      http.get('/api/v1/library/stats', () => {
        return HttpResponse.json(mockStats);
      }),
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json({
          check_updates: false,
          check_printer_firmware: false,
          library_disk_warning_gb: 5,
        });
      }),
      http.post('/api/v1/library/folders', async ({ request }) => {
        const body = await request.json() as { name: string };
        return HttpResponse.json({ id: 4, name: body.name, parent_id: null, children: [] });
      }),
      http.delete('/api/v1/library/folders/:id', () => {
        return HttpResponse.json({ success: true });
      }),
      http.delete('/api/v1/library/files/:id', () => {
        return HttpResponse.json({ success: true });
      }),
      http.post('/api/v1/library/files/move', () => {
        return HttpResponse.json({ success: true });
      }),
      http.post('/api/v1/library/files/add-to-queue', () => {
        return HttpResponse.json({ added: [{ file_id: 1, queue_id: 1 }], errors: [] });
      }),
      http.get('/api/v1/projects/', () => {
        return HttpResponse.json([{ id: 1, name: 'Test Project', color: '#00ae42' }]);
      }),
      http.get('/api/v1/archives/', () => {
        return HttpResponse.json([{ id: 1, print_name: 'Test Archive', filename: 'test.3mf' }]);
      })
    );
  });

  describe('subfolder tiles (#3019)', () => {
    it('shows a selected folder\'s subfolders as tiles instead of the empty state', async () => {
      server.use(
        http.get('/api/v1/library/files', () => {
          return HttpResponse.json([]);
        })
      );
      render(<FileManagerPage />);

      // Select "Functional Parts" (only files are empty; it has a subfolder).
      await waitFor(() => { expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument(); });
      await userEvent.click(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts'));

      // Its child renders twice: in the expanded tree AND as a content tile.
      await waitFor(() => {
        expect(screen.getAllByText('Brackets').length).toBeGreaterThanOrEqual(2);
      });
      // The misleading empty state stays away — the folder is not empty, it
      // just holds no files.
      expect(screen.queryByText('Folder is empty')).not.toBeInTheDocument();
    });

    it('descends into a subfolder when its tile is clicked', async () => {
      server.use(
        http.get('/api/v1/library/files', () => {
          return HttpResponse.json([]);
        })
      );
      render(<FileManagerPage />);

      await waitFor(() => { expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument(); });
      await userEvent.click(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts'));
      await waitFor(() => {
        expect(screen.getAllByText('Brackets').length).toBeGreaterThanOrEqual(2);
      });
      // The tile is the last occurrence (tree renders first in the DOM).
      const tiles = screen.getAllByText('Brackets');
      await userEvent.click(tiles[tiles.length - 1]);

      // "Brackets" has no subfolders and no files — NOW the empty state is
      // the truthful answer.
      expect(await screen.findByText('Folder is empty')).toBeInTheDocument();
    });
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('File Manager')).toBeInTheDocument();
      });
    });

    it('renders the page description', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Organize and manage your print files')).toBeInTheDocument();
      });
    });

    it('shows New Folder button', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('New Folder')).toBeInTheDocument();
      });
    });

    it('shows Upload button', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });
    });
  });

  describe('stats display', () => {
    it('shows file count', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Files:')).toBeInTheDocument();
        expect(screen.getByText('10')).toBeInTheDocument();
      });
    });

    it('shows folder count', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Folders:')).toBeInTheDocument();
        // Folder count appears multiple places, just verify the label is present
        const foldersLabel = screen.getByText('Folders:');
        expect(foldersLabel.nextElementSibling?.textContent).toBe('3');
      });
    });

    it('shows total size', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Size:')).toBeInTheDocument();
        expect(screen.getByText('100.0 MB')).toBeInTheDocument();
      });
    });

    it('shows free space', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Free:')).toBeInTheDocument();
      });
    });
  });

  describe('folder sidebar', () => {
    it('shows All Files option', async () => {
      render(<FileManagerPage />);

      // Scoped to the sidebar: the path bar names the same root.
      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('All Files')).toBeInTheDocument();
      });
    });

    it('shows folder tree', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts')).toBeInTheDocument();
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Art Projects')).toBeInTheDocument();
      });
    });

    it('shows nested folders', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Brackets')).toBeInTheDocument();
      });
    });

    it('shows linked folder indicator', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        // Art Projects has a project_id
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Art Projects')).toBeInTheDocument();
      });
    });
  });

  describe('file display', () => {
    it('shows files in grid', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });
    });

    it('shows file type badges', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        // File type badges show uppercase type
        expect(screen.getAllByText('3MF').length).toBeGreaterThan(0);
        expect(screen.getAllByText('STL').length).toBeGreaterThan(0);
      });
    });

    it('shows print count', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Printed 5x')).toBeInTheDocument();
      });
    });

    it('shows duplicate badge', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        // Duplicate badge shows count, there may be multiple "2"s on the page
        // so we check that at least one element with "2" exists
        const elements = screen.getAllByText('2');
        expect(elements.length).toBeGreaterThan(0);
      });
    });
  });

  describe('view modes', () => {
    it('has grid view button', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByTitle('Grid view')).toBeInTheDocument();
      });
    });

    it('has list view button', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByTitle('List view')).toBeInTheDocument();
      });
    });

    it('can switch to list view', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      // Wait for files to load first
      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });

      // Both view mode buttons should be present and clickable
      const gridButton = screen.getByTitle('Grid view');
      const listButton = screen.getByTitle('List view');

      expect(gridButton).toBeInTheDocument();
      expect(listButton).toBeInTheDocument();

      // Click list view button - verify no errors occur
      await user.click(listButton);

      // Clicking grid button should also work
      await user.click(gridButton);

      // Verify files are still displayed after toggling
      expect(screen.getByText('Benchy')).toBeInTheDocument();
    });

    it('can switch to columns view and descend through folder columns', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });

      await user.click(screen.getByTitle('Column view'));

      // Scoped to the columns pane — the folder names also live in the tree
      // sidebar, so unscoped queries would double-match.
      const columns = within(screen.getByTestId('columns-view'));
      expect(columns.getByText('Functional Parts')).toBeInTheDocument();
      expect(columns.getByText('Art Projects')).toBeInTheDocument();
      // Root files render in the files pane.
      expect(columns.getByText('Benchy')).toBeInTheDocument();

      // Descend: clicking a folder opens its child column, and the level the
      // user just left keeps listing its own files.
      await user.click(columns.getByText('Functional Parts'));
      await waitFor(() => {
        expect(columns.getByText('Brackets')).toBeInTheDocument();
      });
      await waitFor(() => {
        expect(columns.getByText('Benchy')).toBeInTheDocument();
      });
      expect(within(screen.getByTestId('columns-files-pane')).getByText('Spacer')).toBeInTheDocument();

      // Leaf folder: selecting it adds no further column and the pane swaps to
      // its contents.
      await user.click(columns.getByText('Brackets'));
      await waitFor(() => {
        expect(within(screen.getByTestId('columns-files-pane')).getByText('clamp.stl')).toBeInTheDocument();
      });
    });

    it('hides folder columns while a search filters across folders', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      expect(columns.getByText('Functional Parts')).toBeInTheDocument();

      await user.type(screen.getByPlaceholderText('Search files...'), 'benchy');

      // Search results span every folder — the per-level columns disappear,
      // the matching file stays.
      await waitFor(() => {
        expect(columns.queryByText('Functional Parts')).not.toBeInTheDocument();
      });
      expect(columns.getByText('Benchy')).toBeInTheDocument();
    });

    it('navigates the columns with the arrow keys', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      // The highlight sits on the row, which also hosts the folder kebab —
      // the name button itself is unstyled.
      const activeClassOf = (name: string) =>
        columns.getByText(name).closest('[data-folder-id]')?.className ?? '';

      // The pane auto-focuses when the view opens, so keys work immediately.
      // Down from the root selects the first folder (name-sorted: Art
      // Projects), Down again moves to its sibling.
      await user.keyboard('{ArrowDown}');
      await waitFor(() => {
        expect(activeClassOf('Art Projects')).toContain('bg-bambu-green/20');
      });
      await user.keyboard('{ArrowDown}');
      await waitFor(() => {
        expect(activeClassOf('Functional Parts')).toContain('bg-bambu-green/20');
      });

      // Right descends into the first child folder…
      await user.keyboard('{ArrowRight}');
      await waitFor(() => {
        expect(activeClassOf('Brackets')).toContain('bg-bambu-green/20');
      });
      // …and Left climbs back up to the parent.
      await user.keyboard('{ArrowLeft}');
      await waitFor(() => {
        expect(activeClassOf('Functional Parts')).toContain('bg-bambu-green/20');
      });

      // Right on a folder without child folders moves the focus into the
      // files pane; Down walks the file rows. Wait for the refetched file
      // list before the second Right — during the folder switch the pane can
      // be momentarily empty.
      await user.keyboard('{ArrowRight}');
      await waitFor(() => {
        expect(activeClassOf('Brackets')).toContain('bg-bambu-green/20');
        expect(columns.getByText('clamp.stl')).toBeInTheDocument();
      });
      await user.keyboard('{ArrowRight}');
      await waitFor(() => {
        expect(columns.getByText('clamp.stl').closest('[title="clamp.stl"]')?.className).toContain('ring-1');
      });
      await user.keyboard('{ArrowDown}');
      await waitFor(() => {
        expect(columns.getByText('Hinge').closest('[title="Hinge"]')?.className).toContain('ring-1');
      });
      // Left leaves the files pane again.
      await user.keyboard('{ArrowLeft}');
      await waitFor(() => {
        expect(columns.getByText('Hinge').closest('[title="Hinge"]')?.className).not.toContain('ring-1');
      });
    });

    it('keeps the columns pane mounted while a folder\'s files load', async () => {
      const user = userEvent.setup();
      // First files request (initial load) resolves immediately, later ones
      // (the refetch a keyboard descent triggers) stay pending briefly.
      let filesCalls = 0;
      server.use(
        http.get('/api/v1/library/files', async ({ request }) => {
          filesCalls += 1;
          if (filesCalls > 1) await new Promise((resolve) => setTimeout(resolve, 150));
          return HttpResponse.json(filesForRequest(request));
        })
      );
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });
      await user.click(screen.getByTitle('Column view'));
      expect(screen.getByTestId('columns-view')).toBeInTheDocument();

      // Descend while the folder's file list is still in flight. Swapping
      // the pane for the global spinner here would strip its tabindex and,
      // in real browsers, drop keyboard focus to <body> for good.
      await user.keyboard('{ArrowDown}');
      expect(screen.getByTestId('columns-view')).toBeInTheDocument();

      await waitFor(() => {
        expect(within(screen.getByTestId('columns-files-pane')).getByText('Vase')).toBeInTheDocument();
      });
    });

    // #3020 follow-up: the files pane carries the list row's icon strip, so
    // the columns view offers every per-file action in the same order.
    const listRow = (name: string) => screen.getByText(name).closest('div[class*="cursor-pointer"]') as HTMLElement;
    const actionTitlesOf = (row: HTMLElement) =>
      Array.from(row.querySelector('[data-file-actions]')!.querySelectorAll('button')).map((b) => b.title);

    it('offers the same per-file actions as the list view, in the same order', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('List view'));
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());
      const listBenchy = actionTitlesOf(listRow('Benchy'));
      const listBracket = actionTitlesOf(listRow('bracket.stl'));
      expect(listBenchy).toEqual(['Print', '3D Preview', 'Download', 'File details', 'Rename', 'Copy path', 'Delete']);
      expect(listBracket).toContain('Generate Thumbnail');

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await waitFor(() => expect(columns.getByText('Benchy')).toBeInTheDocument());
      expect(actionTitlesOf(columns.getByText('Benchy').closest('[data-file-id]') as HTMLElement)).toEqual(listBenchy);
      expect(actionTitlesOf(columns.getByText('bracket.stl').closest('[data-file-id]') as HTMLElement)).toEqual(listBracket);
    });

    it('fires a row action without toggling the row selection', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      const row = columns.getByText('Benchy').closest('[data-file-id]') as HTMLElement;

      await user.click(within(row).getByTitle('Rename'));
      // The rename modal edits the base name; the extension is fixed.
      expect(await screen.findByDisplayValue('benchy')).toBeInTheDocument();
      expect(row.className).not.toContain('bg-bambu-green/10');

      await user.click(screen.getByText('Cancel'));
      await user.click(within(row).getByTitle('Delete'));
      expect(await screen.findByText('Delete File')).toBeInTheDocument();
    });

    it('reveals the focused row\'s actions and keeps the rest touch-reachable (#2865)', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      const stripWrapper = (name: string) =>
        (columns.getByText(name).closest('[data-file-id]') as HTMLElement).querySelector('[data-file-actions]')!.parentElement!;

      // Right from the root moves the focus into the files pane.
      await user.keyboard('{ArrowRight}');
      await waitFor(() => expect(stripWrapper('Benchy').className).not.toContain('opacity-0'));

      // Other rows hide the strip only for pointers that can hover — never a
      // bare opacity-0 — and stay out of the Tab order.
      expect(stripWrapper('bracket.stl').className).not.toMatch(/(^|\s)opacity-0(\s|$)/);
      expect(stripWrapper('bracket.stl').className).toContain('can-hover:opacity-0');
      expect(within(stripWrapper('bracket.stl')).getByTitle('Download')).toHaveAttribute('tabindex', '-1');
      expect(within(stripWrapper('Benchy')).getByTitle('Download')).toHaveAttribute('tabindex', '0');

      // Tab from the pane lands on the focused row's first action.
      await user.tab();
      expect(document.activeElement).toBe(within(stripWrapper('Benchy')).getByTitle('Print'));
    });

    it('reaches and triggers the focused file\'s actions from the keyboard', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.keyboard('{ArrowRight}');
      await waitFor(() => {
        expect(columns.getByText('Benchy').closest('[title="Benchy"]')?.className).toContain('ring-1');
      });
      const row = columns.getByText('Benchy').closest('[data-file-id]') as HTMLElement;

      // The context-menu key jumps into the strip; Left/Right walk it.
      await user.keyboard('{ContextMenu}');
      expect(document.activeElement).toBe(within(row).getByTitle('Print'));
      await user.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}{ArrowRight}');
      expect(document.activeElement).toBe(within(row).getByTitle('Rename'));
      await user.keyboard('{ArrowLeft}{ArrowLeft}');
      expect(document.activeElement).toBe(within(row).getByTitle('Download'));

      // Escape hands focus back to the pane, so the arrows walk rows again.
      await user.keyboard('{Escape}');
      expect(document.activeElement).toBe(screen.getByTestId('columns-view'));
      await user.keyboard('{ArrowDown}');
      await waitFor(() => {
        expect(columns.getByText('bracket.stl').closest('[title="bracket.stl"]')?.className).toContain('ring-1');
      });

      // Enter on a strip button activates that button, not the row.
      await user.keyboard('{Shift>}{F10}{/Shift}');
      const bracketRow = columns.getByText('bracket.stl').closest('[data-file-id]') as HTMLElement;
      expect(document.activeElement?.closest('[data-file-id]')).toBe(bracketRow);
      within(bracketRow).getByTitle('Rename').focus();
      await user.keyboard('{Enter}');
      // .stl is not a split-off extension, so the whole name is editable.
      expect(await screen.findByDisplayValue('bracket.stl')).toBeInTheDocument();
    });

    it('offers the tree\'s folder actions on folder rows', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      const row = columns.getByText('Art Projects').closest('[data-folder-id]') as HTMLElement;
      const kebab = within(row).getByTitle('Actions');
      // Unselected row: kebab hidden only for pointers that can hover (#2865).
      expect(kebab.closest('.flex-shrink-0')!.className).toContain('can-hover:opacity-0');

      await user.click(kebab);
      expect(within(row).getByRole('button', { name: 'Rename' })).toBeInTheDocument();
      // Art Projects is linked to a project, so the link entry offers a change.
      expect(within(row).getByRole('button', { name: 'Change Link...' })).toBeInTheDocument();
      expect(within(row).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
      // Opening the menu did not select the folder.
      expect(row.className).not.toContain('bg-bambu-green/20');

      await user.click(within(row).getByRole('button', { name: 'Rename' }));
      expect(await screen.findByDisplayValue('Art Projects')).toBeInTheDocument();
    });

    it('opens the selected folder\'s kebab from the keyboard', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.keyboard('{ArrowDown}');
      const row = columns.getByText('Art Projects').closest('[data-folder-id]') as HTMLElement;
      await waitFor(() => expect(row.className).toContain('bg-bambu-green/20'));
      // The selected row's kebab is the only folder control in the Tab order.
      expect(within(row).getByTitle('Actions')).toHaveAttribute('tabindex', '0');
      expect(
        within(columns.getByText('Functional Parts').closest('[data-folder-id]') as HTMLElement).getByTitle('Actions'),
      ).toHaveAttribute('tabindex', '-1');

      await user.keyboard('{ContextMenu}');
      expect(within(row).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
    });

    it('closes the folder kebab menu on a second click and keeps it opaque while open', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      const row = columns.getByText('Art Projects').closest('[data-folder-id]') as HTMLElement;
      const kebab = within(row).getByTitle('Actions');
      const wrapper = kebab.closest('[data-folder-actions]') as HTMLElement;
      expect(wrapper.className).toContain('can-hover:opacity-0');

      await user.click(kebab);
      expect(within(row).getByRole('button', { name: 'Rename' })).toBeInTheDocument();
      // The menu is a descendant of the hover-revealed wrapper: while it is
      // open the wrapper must not depend on hover/focus to stay visible.
      expect(wrapper.className).not.toContain('opacity-0');

      await user.click(kebab);
      expect(within(row).queryByRole('button', { name: 'Rename' })).not.toBeInTheDocument();
      expect(wrapper.className).toContain('can-hover:opacity-0');
    });

    it('hands focus back to the pane after the folder kebab', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.keyboard('{ArrowDown}');
      const artRow = columns.getByText('Art Projects').closest('[data-folder-id]') as HTMLElement;
      const functionalRow = columns.getByText('Functional Parts').closest('[data-folder-id]') as HTMLElement;
      await waitFor(() => expect(artRow.className).toContain('bg-bambu-green/20'));

      // Escape closes the menu and returns focus to the pane, so the arrows
      // keep walking rows instead of dying on the kebab.
      await user.keyboard('{ContextMenu}');
      expect(within(artRow).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
      expect(document.activeElement).toBe(within(artRow).getByTitle('Actions'));
      await user.keyboard('{Escape}');
      expect(within(artRow).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
      expect(document.activeElement).toBe(screen.getByTestId('columns-view'));
      await user.keyboard('{ArrowDown}');
      await waitFor(() => expect(functionalRow.className).toContain('bg-bambu-green/20'));
      await user.keyboard('{ArrowUp}');
      await waitFor(() => expect(artRow.className).toContain('bg-bambu-green/20'));

      // Down straight out of an open menu closes it and moves the selection.
      await user.keyboard('{ContextMenu}');
      expect(within(artRow).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
      await user.keyboard('{ArrowDown}');
      expect(within(artRow).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
      await waitFor(() => expect(functionalRow.className).toContain('bg-bambu-green/20'));
      expect(document.activeElement).toBe(screen.getByTestId('columns-view'));

      // Activating an entry with Enter must not strand focus on <body>.
      await user.keyboard('{ContextMenu}');
      await user.tab();
      expect(document.activeElement).toBe(within(functionalRow).getByRole('button', { name: 'Rename' }));
      await user.keyboard('{Enter}');
      expect(await screen.findByDisplayValue('Functional Parts')).toBeInTheDocument();
      expect(document.activeElement).not.toBe(document.body);
    });

    it('does not open the row preview when a strip icon is double-clicked', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      const row = columns.getByText('bracket.stl').closest('[data-file-id]') as HTMLElement;

      await user.dblClick(within(row).getByTitle('Rename'));
      expect(await screen.findByDisplayValue('bracket.stl')).toBeInTheDocument();
      expect(screen.queryByTestId('model-viewer-modal')).not.toBeInTheDocument();

      // The row itself still opens the viewer.
      await user.click(screen.getByText('Cancel'));
      await user.dblClick(columns.getByText('bracket.stl'));
      expect(await screen.findByTestId('model-viewer-modal')).toBeInTheDocument();
    });

    // Every column is that level's contents, folders AND files. Before this a
    // folder holding files but no subfolders looked empty until it was the
    // selection.
    const descendToBrackets = async (user: ReturnType<typeof userEvent.setup>) => {
      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.click(columns.getByText('Functional Parts'));
      await user.click(await columns.findByText('Brackets'));
      return columns;
    };

    it('lists an intermediate level\'s folders and its files in the same column', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await descendToBrackets(user);

      const level = within(screen.getByTestId('columns-level-folder-1'));
      expect(level.getByText('Brackets')).toBeInTheDocument();
      await waitFor(() => expect(level.getByText('Spacer')).toBeInTheDocument());

      // The root column lists the files that sit in no folder at all.
      const root = within(screen.getByTestId('columns-level-root'));
      await waitFor(() => expect(root.getByText('Benchy')).toBeInTheDocument());
      expect(root.getByText('Functional Parts')).toBeInTheDocument();
    });

    it('focuses a file in an intermediate column without moving the folder selection', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await descendToBrackets(user);
      const pane = within(screen.getByTestId('columns-files-pane'));
      await waitFor(() => expect(pane.getByText('clamp.stl')).toBeInTheDocument());

      const level = within(screen.getByTestId('columns-level-folder-1'));
      await user.click(await level.findByText('Spacer'));

      expect(level.getByText('Spacer').closest('[data-file-id]')?.className).toContain('ring-1');
      // Brackets is still the selection: its own files still fill the pane.
      expect(pane.getByText('clamp.stl')).toBeInTheDocument();
      expect(level.getByText('Brackets').closest('[data-folder-id]')?.className).toContain('bg-bambu-green/20');
    });

    it('opens the preview on double-click from an intermediate column', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await descendToBrackets(user);
      const level = within(screen.getByTestId('columns-level-folder-1'));
      await user.dblClick(await level.findByText('Spacer'));

      expect(await screen.findByTestId('model-viewer-modal')).toBeInTheDocument();
    });

    it('keeps a level\'s folders visible while its files are still loading', async () => {
      const user = userEvent.setup();
      server.use(
        http.get('/api/v1/library/files', async ({ request }) => {
          if (new URL(request.url).searchParams.get('folder_id') === '1') {
            await new Promise((resolve) => setTimeout(resolve, 200));
          }
          return HttpResponse.json(filesForRequest(request));
        })
      );
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await descendToBrackets(user);

      // The column paints its folders straight away and marks the pending file
      // list instead of blocking.
      const level = within(screen.getByTestId('columns-level-folder-1'));
      expect(level.getByText('Brackets')).toBeInTheDocument();
      expect(level.getByText('…')).toBeInTheDocument();
      await waitFor(() => expect(level.getByText('Spacer')).toBeInTheDocument());
    });

    it('leaves the rightmost pane as a leaf folder\'s contents', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.click(columns.getByText('Art Projects'));

      const pane = within(screen.getByTestId('columns-files-pane'));
      await waitFor(() => expect(pane.getByText('Vase')).toBeInTheDocument());
      // A leaf contributes no column of its own.
      expect(screen.queryByTestId('columns-level-folder-3')).not.toBeInTheDocument();
    });
  });

    it('walks off the last folder of a column into that same column files', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const root = screen.getByTestId('columns-level-root');
      const level = within(root);
      // The last folder of the column: ArrowDown has no sibling left, so the
      // column own files are what continues the list.
      const folderRows = Array.from(root.querySelectorAll('[data-folder-id]'));
      const lastFolder = folderRows[folderRows.length - 1] as HTMLElement;
      const lastName = lastFolder.querySelector('button')!.textContent!.replace(/\d+$/, '').trim();
      await user.click(within(lastFolder).getByTitle(lastName));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent(lastName));
      await waitFor(() => expect(level.getByText('Benchy')).toBeInTheDocument());

      await user.keyboard('{ArrowDown}');
      await waitFor(() =>
        expect(level.getByText('Benchy').closest('[title="Benchy"]')?.className).toContain('ring-1')
      );
      // That row action strip is now in the Tab order.
      const row = level.getByText('Benchy').closest('[data-file-id]') as HTMLElement;
      expect(within(row).getByTitle('Download')).toHaveAttribute('tabindex', '0');

      await user.keyboard('{ArrowDown}');
      await waitFor(() =>
        expect(level.getByText('bracket.stl').closest('[title="bracket.stl"]')?.className).toContain('ring-1')
      );

      // Up off the first file hands the focus back to the folders, and the
      // folder selection never moved.
      await user.keyboard('{ArrowUp}{ArrowUp}');
      await waitFor(() =>
        expect(level.getByText('Benchy').closest('[title="Benchy"]')?.className).not.toContain('ring-1')
      );
      expect(currentCrumb()).toHaveTextContent(lastName);
    });

  // The page chrome is one card: the select-all control and the selection
  // actions live in it, so a selection is never left without them.
  describe('selection chrome', () => {
    it('keeps the selection actions on screen when the selected pane is empty', async () => {
      const user = userEvent.setup();
      server.use(
        http.get('/api/v1/library/files', ({ request }) => {
          const folderId = new URL(request.url).searchParams.get('folder_id');
          // Functional Parts still holds its own file; Brackets is empty, so
          // the rightmost pane has nothing to render.
          if (folderId === '2') return HttpResponse.json([]);
          return HttpResponse.json(filesForRequest(request));
        })
      );
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.click(columns.getByText('Functional Parts'));
      await user.click(await columns.findByText('Brackets'));
      await waitFor(() =>
        expect(within(screen.getByTestId('columns-files-pane')).getByText('Folder is empty')).toBeInTheDocument()
      );

      // Tick a file in the parent column — the pane it belongs to is not the
      // selected folder's.
      const level = within(screen.getByTestId('columns-level-folder-1'));
      await user.click(await level.findByText('Spacer'));

      const actions = within(screen.getByTestId('selection-actions'));
      expect(actions.getByText('1 selected')).toBeInTheDocument();
      expect(actions.getByTitle(/Add or remove tags/)).toBeInTheDocument();
      expect(actions.getByText('Move')).toBeInTheDocument();
      expect(actions.getByText('Delete')).toBeInTheDocument();

      // …and Clear empties it again.
      await user.click(actions.getByText('Clear'));
      expect(screen.queryByTestId('selection-actions')).not.toBeInTheDocument();
    });

    // The id-based actions (Move / Delete / tags / Clear) never had a problem
    // here; the two that need the file OBJECT read the selected folder's query,
    // which an ancestor column's file is not in.
    it('offers Print and Group as versions for files ticked in an ancestor column', async () => {
      const user = userEvent.setup();
      const slicedInParent = [
        {
          id: 21,
          filename: 'plate_1.gcode.3mf',
          file_path: '/library/functional/plate_1.gcode.3mf',
          file_size: 262144,
          file_type: 'gcode.3mf',
          folder_id: 1,
          thumbnail_path: null,
          print_name: 'Plate One',
          print_time_seconds: 600,
          print_count: 0,
          duplicate_count: 0,
          created_at: '2024-03-01T00:00:00Z',
        },
        {
          id: 22,
          filename: 'plate_2.gcode.3mf',
          file_path: '/library/functional/plate_2.gcode.3mf',
          file_size: 262144,
          file_type: 'gcode.3mf',
          folder_id: 1,
          thumbnail_path: null,
          print_name: 'Plate Two',
          print_time_seconds: 700,
          print_count: 0,
          duplicate_count: 0,
          created_at: '2024-03-02T00:00:00Z',
        },
      ];
      server.use(
        http.get('/api/v1/library/files', ({ request }) => {
          const folderId = new URL(request.url).searchParams.get('folder_id');
          if (folderId === '1') return HttpResponse.json(slicedInParent);
          if (folderId === '2') return HttpResponse.json([]);
          return HttpResponse.json(filesForRequest(request));
        })
      );
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.click(columns.getByText('Functional Parts'));
      await user.click(await columns.findByText('Brackets'));

      // Brackets is the selected folder, so the main query holds nothing:
      // everything ticked below lives in the parent column.
      const level = within(screen.getByTestId('columns-level-folder-1'));
      await user.click(await level.findByText('Plate One'));

      const actions = () => within(screen.getByTestId('selection-actions'));
      expect(actions().getByText('1 selected')).toBeInTheDocument();
      expect(actions().getByText('Print')).toBeInTheDocument();

      // A second sliced file turns it into the cross-model print (#671) and
      // arms Group as versions.
      await user.click(level.getByText('Plate Two'));
      expect(actions().getByText('2 selected')).toBeInTheDocument();
      expect(actions().getByText('Print (2 alternatives)')).toBeInTheDocument();
      expect(actions().getByText('Group as versions')).toBeInTheDocument();
    });

    it('drops the selection when the folder changes, so no action points off screen', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.click(columns.getByText('Functional Parts'));
      const level = within(screen.getByTestId('columns-level-root'));
      await user.click(await level.findByText('Benchy'));
      expect(within(screen.getByTestId('selection-actions')).getByText('1 selected')).toBeInTheDocument();

      await user.click(columns.getByText('Art Projects'));
      await waitFor(() => expect(screen.queryByTestId('selection-actions')).not.toBeInTheDocument());
    });

    it('carries select-all inside the filter card instead of a bar of its own', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      const card = screen.getByTestId('library-filter-card');
      expect(within(card).getByText('Select All')).toBeInTheDocument();
      // The control leads the card, before the search input.
      const controls = Array.from(card.querySelectorAll('button, input'));
      expect(controls.indexOf(within(card).getByText('Select All').closest('button')!)).toBe(0);

      // Selecting turns it into Deselect all and opens the action row in the
      // same card — not a separate bordered bar.
      await user.click(within(card).getByText('Select All'));
      expect(within(card).getByText('Deselect All')).toBeInTheDocument();
      expect(card.contains(screen.getByTestId('selection-actions'))).toBe(true);
    });

    it('offers the search box in a folder that holds only subfolders', async () => {
      const user = userEvent.setup();
      server.use(
        http.get('/api/v1/library/files', ({ request }) => {
          const folderId = new URL(request.url).searchParams.get('folder_id');
          // Functional Parts is purely organisational here.
          if (folderId === '1') return HttpResponse.json([]);
          return HttpResponse.json(filesForRequest(request));
        })
      );
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(sidebar().getByText('Functional Parts'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Functional Parts'));

      // The folder search is the whole point of standing here — typing a
      // subfolder name must still be possible.
      const search = screen.getByPlaceholderText('Search files...');
      await user.type(search, 'Brack');
      expect(await screen.findByText('Folders (1)')).toBeInTheDocument();
    });

    it('names the tag catalogue and the assign action differently', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      expect(screen.getByText('Manage tags')).toBeInTheDocument();

      await user.click(within(screen.getByTestId('library-filter-card')).getByText('Select All'));
      expect(within(screen.getByTestId('selection-actions')).getByText('Assign tags')).toBeInTheDocument();
    });

    it('zips a multi-file selection and downloads a single file as itself', async () => {
      const user = userEvent.setup();
      const zipped: unknown[] = [];
      const singles: string[] = [];
      server.use(
        http.post('/api/v1/library/files/download-zip', async ({ request }) => {
          zipped.push(await request.json());
          return new HttpResponse(new Blob(['PK']), { headers: { 'Content-Type': 'application/zip' } });
        }),
        http.get('/api/v1/library/files/:id/download', ({ params }) => {
          singles.push(String(params.id));
          return new HttpResponse(new Blob(['x']));
        })
      );
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(within(screen.getByTestId('library-filter-card')).getByText('Select All'));
      await user.click(within(screen.getByTestId('selection-actions')).getByText('Download as ZIP'));
      await waitFor(() => expect(zipped).toHaveLength(1));
      expect((zipped[0] as { file_ids: number[] }).file_ids.length).toBeGreaterThan(1);
      expect(singles).toHaveLength(0);

      // One file is not worth an archive: it downloads as itself.
      await user.click(within(screen.getByTestId('library-filter-card')).getByText('Deselect All'));
      await user.type(screen.getByPlaceholderText('Search files...'), 'benchy');
      await user.click(within(screen.getByTestId('library-filter-card')).getByText('Select All'));
      await user.click(within(screen.getByTestId('selection-actions')).getByText('Download'));
      await waitFor(() => expect(singles).toHaveLength(1));
      expect(zipped).toHaveLength(1);
    });
  });

  describe('path bar', () => {
    it('renders the chain from the root down to the selected folder', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      // At the root the bar would hold one crumb repeating the view's own
      // name, so it is not drawn at all until there is a path to show.
      expect(screen.queryByTestId('library-path-bar')).not.toBeInTheDocument();

      await user.click(sidebar().getByText('Brackets'));

      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Brackets'));
      expect(pathBar().getByRole('button', { name: 'All Files' })).toBeInTheDocument();
      expect(pathBar().getByRole('button', { name: 'Functional Parts' })).toBeInTheDocument();
      // The current folder is text, not a link.
      expect(pathBar().queryByRole('button', { name: 'Brackets' })).not.toBeInTheDocument();
    });

    it('selects an ancestor when its crumb is clicked', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(sidebar().getByText('Brackets'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Brackets'));

      await user.click(pathBar().getByRole('button', { name: 'Functional Parts' }));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Functional Parts'));

      // Back at the root the bar has nothing left to say and goes away.
      await user.click(pathBar().getByRole('button', { name: 'All Files' }));
      await waitFor(() => expect(screen.queryByTestId('library-path-bar')).not.toBeInTheDocument());
    });

    it('keeps a root-plus-three chain whole rather than folding a crumb that fits', async () => {
      const user = userEvent.setup();
      server.use(http.get('/api/v1/library/folders', () => HttpResponse.json(deepFolders)));
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(await sidebar().findByText('N1125035'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('N1125035'));

      // All Files > Kunden > RAFI > N1125035 — every ancestor stays clickable.
      expect(pathBar().getByRole('button', { name: 'All Files' })).toBeInTheDocument();
      expect(pathBar().getByRole('button', { name: 'Kunden' })).toBeInTheDocument();
      expect(pathBar().getByRole('button', { name: 'RAFI' })).toBeInTheDocument();
      expect(pathBar().queryByRole('button', { name: 'Show hidden folders' })).not.toBeInTheDocument();
    });

    it('collapses a deep chain and lists the hidden ancestors in a menu', async () => {
      const user = userEvent.setup();
      server.use(http.get('/api/v1/library/folders', () => HttpResponse.json(deepFolders)));
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(await sidebar().findByText('Revision A'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Revision A'));

      // The first crumb and the last two stay on the bar…
      expect(pathBar().getByRole('button', { name: 'All Files' })).toBeInTheDocument();
      expect(pathBar().getByRole('button', { name: 'N1125035' })).toBeInTheDocument();
      expect(pathBar().queryByRole('button', { name: 'Kunden' })).not.toBeInTheDocument();
      expect(pathBar().queryByRole('button', { name: 'RAFI' })).not.toBeInTheDocument();

      // …the rest are behind the ellipsis. The list itself hangs off <body>:
      // the bar clips what leaves it, so a menu inside it is painted nowhere.
      await user.click(pathBar().getByRole('button', { name: 'Show hidden folders' }));
      const menu = within(screen.getByRole('menu'));
      expect(menu.getByRole('menuitem', { name: 'Kunden' })).toBeInTheDocument();
      expect(menu.getByRole('menuitem', { name: 'RAFI' })).toBeInTheDocument();

      await user.click(menu.getByRole('menuitem', { name: 'RAFI' }));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('RAFI'));
    });

    it('follows a selection made in the columns view', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.click(columns.getByText('Functional Parts'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Functional Parts'));

      await user.click(await columns.findByText('Brackets'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Brackets'));
    });
  });

  describe('folder sidebar toggle', () => {
    const toggle = () => screen.getByTestId('toggle-folder-sidebar');
    // localStorage is globally mocked in setup.ts and stores nothing, so the
    // stored preference is programmed per test.
    const getItemMock = localStorage.getItem as ReturnType<typeof vi.fn>;
    const setItemMock = localStorage.setItem as ReturnType<typeof vi.fn>;
    const storedHidden = (key: string) => (key === 'library-sidebar-hidden' ? 'true' : null);

    beforeEach(() => {
      getItemMock.mockReset();
      setItemMock.mockReset();
      getItemMock.mockReturnValue(null);
    });

    afterEach(() => {
      getItemMock.mockReset();
      setItemMock.mockReset();
    });

    const contentNav = () => within(screen.getByTestId('content-folder-nav'));

    it('hides the tree and hands folder navigation to the content area', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument();
      // Pressed = the sidebar is on screen, so the state matches the layout.
      expect(toggle()).toHaveAttribute('aria-pressed', 'true');
      // While the tree is there the content area does not repeat it.
      expect(screen.queryByTestId('content-folder-nav')).not.toBeInTheDocument();

      await user.click(toggle());

      expect(screen.queryByTestId('folder-sidebar')).not.toBeInTheDocument();
      expect(toggle()).toHaveAttribute('aria-pressed', 'false');
      // The files stay; the bar has nothing to draw at the root…
      expect(screen.getByText('Benchy')).toBeInTheDocument();
      expect(screen.queryByTestId('library-path-bar')).not.toBeInTheDocument();
      // …and the way DOWN is in the content area now.
      expect(contentNav().getByText('Functional Parts')).toBeInTheDocument();
      expect(contentNav().getByText('Art Projects')).toBeInTheDocument();

      await user.click(contentNav().getByText('Functional Parts'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Functional Parts'));
      expect(await screen.findByText('Spacer')).toBeInTheDocument();

      // The child of the folder we just entered is offered in turn.
      await user.click(contentNav().getByText('Brackets'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Brackets'));

      await user.click(toggle());
      expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument();
      expect(screen.queryByTestId('content-folder-nav')).not.toBeInTheDocument();
    });

    it('keeps the way down open in a folder that holds only subfolders', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      server.use(http.get('/api/v1/library/folders', () => HttpResponse.json(deepFolders)));
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(contentNav().getByText('Kunden'));
      // An organisational folder: no files of its own, so the content area is
      // its subfolders — which is exactly the way down the hidden sidebar took.
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Kunden'));

      expect(contentNav().getByText('RAFI')).toBeInTheDocument();
      await user.click(contentNav().getByText('RAFI'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('RAFI'));
      expect(contentNav().getByText('N1125035')).toBeInTheDocument();
    });

    it('offers the top-level buckets the hidden sidebar took with it', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      server.use(
        http.get('/api/v1/library/folders', () =>
          HttpResponse.json([
            ...mockFolders,
            {
              id: 99,
              name: 'NAS Library',
              parent_id: null,
              file_count: 200,
              project_id: null,
              archive_id: null,
              project_name: null,
              archive_name: null,
              is_external: true,
              external_readonly: false,
              external_path: '/mnt/nas',
              children: [],
            },
          ])
        )
      );
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      // Internal bucket: only the managed folders are offered.
      expect(contentNav().getByText('Functional Parts')).toBeInTheDocument();
      expect(contentNav().queryByText('NAS Library')).not.toBeInTheDocument();

      await user.click(contentNav().getByRole('button', { name: /External/ }));

      await waitFor(() => expect(contentNav().getByText('NAS Library')).toBeInTheDocument());
      expect(contentNav().queryByText('Functional Parts')).not.toBeInTheDocument();

      await user.click(contentNav().getByRole('button', { name: /All Files/ }));
      await waitFor(() => expect(contentNav().getByText('Functional Parts')).toBeInTheDocument());
      expect(contentNav().queryByText('NAS Library')).not.toBeInTheDocument();
    });

    it('lists the folders as rows in the list view', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('List view'));

      // Same folders, laid out as rows next to the file rows.
      expect(contentNav().getByText('Art Projects')).toBeInTheDocument();
      await user.click(contentNav().getByText('Functional Parts'));
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Functional Parts'));
      expect(contentNav().getByText('Brackets')).toBeInTheDocument();
    });

    // Hiding the tree must move folder management, not remove it: the
    // preference is persisted, so a capability lost here stays lost across
    // reloads with nothing on screen to connect the two.
    it('keeps rename, link and delete on the folders it draws', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      const tile = contentNav().getByText('Art Projects').closest('[data-folder-id]') as HTMLElement;
      await user.click(within(tile).getByTitle('Actions'));

      expect(within(tile).getByRole('button', { name: 'Rename' })).toBeInTheDocument();
      // Art Projects is linked to a project, so the link entry offers a change.
      expect(within(tile).getByRole('button', { name: 'Change Link...' })).toBeInTheDocument();
      expect(within(tile).getByRole('button', { name: 'Delete' })).toBeInTheDocument();

      // Opening the menu is not navigation: still at the root, where the bar
      // has nothing to draw.
      expect(screen.queryByTestId('library-path-bar')).not.toBeInTheDocument();

      await user.click(within(tile).getByRole('button', { name: 'Rename' }));
      expect(await screen.findByDisplayValue('Art Projects')).toBeInTheDocument();
    });

    it('keeps the folder kebab on the list-view rows too', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(screen.getByTitle('List view'));

      const row = contentNav().getByText('Functional Parts').closest('[data-folder-id]') as HTMLElement;
      await user.click(within(row).getByTitle('Actions'));
      expect(within(row).getByRole('button', { name: 'Rename' })).toBeInTheDocument();
      expect(within(row).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
    });

    // The icon is the only part of the control a sighted user reads, so it
    // must say the same thing as aria-pressed.
    it('draws the icon from the layout the sidebar is actually in', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      expect(toggle().querySelector('svg.lucide-panel-left-close')).toBeTruthy();

      await user.click(toggle());
      expect(toggle()).toHaveAttribute('aria-pressed', 'false');
      expect(toggle().querySelector('svg.lucide-panel-left-open')).toBeTruthy();
      expect(toggle().querySelector('svg.lucide-panel-left-close')).toBeNull();
    });

    it('remembers the choice across a remount', async () => {
      const user = userEvent.setup();
      const { unmount } = render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await user.click(toggle());
      expect(setItemMock).toHaveBeenCalledWith('library-sidebar-hidden', 'true');
      unmount();

      // What was written is what a fresh mount reads back.
      getItemMock.mockImplementation(storedHidden);
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());
      expect(screen.queryByTestId('folder-sidebar')).not.toBeInTheDocument();
      expect(toggle()).toHaveAttribute('aria-pressed', 'false');
    });

    it('draws every folder once when the tree is off, not twice', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      // The stand-in nav is the folders' one home while the tree is away; the
      // tiles used to repeat it, which put each folder on screen twice.
      expect(screen.getByTestId('content-folder-nav')).toBeInTheDocument();
      expect(screen.getAllByText('Functional Parts')).toHaveLength(1);

      await user.click(screen.getByTitle('List view'));
      expect(screen.getAllByText('Functional Parts')).toHaveLength(1);
    });

    it('hides the folder tiles on its own, without touching the tree', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      // The same folder is drawn in the tree and as a tile; the tile is the
      // one that is not inside the sidebar.
      const tiles = () =>
        screen
          .queryAllByText('Functional Parts')
          .filter((el) => !screen.getByTestId('folder-sidebar').contains(el));
      await waitFor(() => expect(tiles().length).toBeGreaterThan(0));

      await user.click(screen.getByTestId('toggle-folder-tiles'));

      await waitFor(() => expect(tiles()).toHaveLength(0));
      expect(setItemMock).toHaveBeenCalledWith('library-folder-tiles-hidden', 'true');
      // The tree is a separate choice and must not move with it.
      expect(screen.getByTestId('folder-sidebar')).toBeInTheDocument();
      expect(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts')).toBeInTheDocument();
    });

    it('works in the columns view too, where the tree is most redundant', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());
      expect(screen.queryByTestId('folder-sidebar')).not.toBeInTheDocument();

      await user.click(screen.getByTitle('Column view'));

      // The stored preference carries into this view rather than being
      // overridden by it: the columns are the way down, so the tree beside
      // them is the thing worth reclaiming the width from.
      expect(screen.queryByTestId('folder-sidebar')).not.toBeInTheDocument();
      expect(toggle()).not.toHaveAttribute('aria-disabled');
      expect(toggle()).toHaveAttribute('aria-pressed', 'false');

      await user.click(toggle());
      expect(setItemMock).toHaveBeenCalledWith('library-sidebar-hidden', 'false');
      expect(await screen.findByTestId('folder-sidebar')).toBeInTheDocument();
      expect(toggle()).toHaveAttribute('aria-pressed', 'true');
    });

    it('does not add the content folder nav to the columns view', async () => {
      const user = userEvent.setup();
      getItemMock.mockImplementation(storedHidden);
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());
      // Grid view with the tree off needs the stand-in; the columns do not.
      expect(screen.getByTestId('content-folder-nav')).toBeInTheDocument();

      await user.click(screen.getByTitle('Column view'));

      expect(screen.queryByTestId('content-folder-nav')).not.toBeInTheDocument();
    });

    it('keeps one accessible name and lets aria-pressed carry the state', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      // The name must not swap with the state — "Show folder sidebar, pressed"
      // announces the opposite of what is on screen.
      expect(screen.getByRole('button', { name: 'Folder sidebar' })).toBe(toggle());
      await user.click(toggle());
      expect(screen.getByRole('button', { name: 'Folder sidebar' })).toBe(toggle());
      // The tooltip still names the action a click performs.
      expect(toggle()).toHaveAttribute('title', 'Show folder sidebar');
    });
  });

  // The header carries eight controls at full permission and the labels are
  // long in several locales. Without these two classes the overflow is
  // absorbed by each Button breaking its own label over two or three lines,
  // which makes the header taller than a wrapped row would be.
  describe('header toolbar', () => {
    it('wraps whole buttons instead of breaking their labels', async () => {
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      const actions = screen.getByTestId('file-manager-actions');
      expect(actions.className).toContain('flex-wrap');

      for (const label of ['Generate Thumbnails', 'Link External', 'New Folder', 'Manage tags', 'Upload']) {
        const button = within(actions).getByText(label).closest('button')!;
        expect(button.className).toContain('whitespace-nowrap');
      }
    });
  });

  describe('move dialog', () => {
    const openMoveDialog = async (user: ReturnType<typeof userEvent.setup>) => {
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());
      await user.click(within(screen.getByTestId('library-filter-card')).getByText('Select All'));
      await user.click(within(screen.getByTestId('selection-actions')).getByText('Move'));
      return screen.getByTestId('move-folder-list');
    };

    // jsdom does no layout, so these assert the exact classes that do the
    // sizing. `flex-1 min-h-0` on the list bounds nothing by itself — it only
    // works because the modal root is a capped flex column — so the root's
    // classes are part of the contract, not an implementation detail.
    it('bounds the folder list by the viewport, not by a fixed 256px box', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      const list = await openMoveDialog(user);
      const root = screen.getByTestId('move-files-modal');

      expect(root.className).toContain('max-h-[90vh]');
      expect(root.className).toContain('flex-col');
      expect(root.className).not.toContain('max-w-sm');

      expect(list.className).toContain('flex-1');
      expect(list.className).toContain('min-h-0');
      // The list is the box that scrolls, and it has a cap of its own so a
      // tall screen does not stretch the dialog to the full 90vh.
      expect(list.className).toContain('overflow-y-auto');
      expect(list.className).toContain('max-h-[min(60vh,32rem)]');
      expect(list.className).not.toContain('max-h-64');
    });

    it('narrows the list as you type and keeps a hit visible under its parent', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      const list = await openMoveDialog(user);

      expect(within(list).getByText('Art Projects')).toBeInTheDocument();

      await user.type(screen.getByPlaceholderText('Filter folders...'), 'brack');

      // Case-insensitive hit plus the parent it is indented under; the
      // unrelated top-level folder goes.
      expect(within(list).getByText('Brackets')).toBeInTheDocument();
      expect(within(list).getByText('Functional Parts')).toBeInTheDocument();
      expect(within(list).queryByText('Art Projects')).not.toBeInTheDocument();
    });

    // The other direction of the same rule: narrowing to a customer has to
    // leave that customer's job folders on the list, because those are the
    // destinations the user is filtering in order to reach.
    it('keeps the subtree of a folder that matches', async () => {
      const user = userEvent.setup();
      server.use(http.get('/api/v1/library/folders', () => HttpResponse.json(deepFolders)));
      render(<FileManagerPage />);
      const list = await openMoveDialog(user);

      await user.type(screen.getByPlaceholderText('Filter folders...'), 'RAFI');

      // The ancestor that carries the hit…
      expect(within(list).getByText('Kunden')).toBeInTheDocument();
      expect(within(list).getByText('RAFI')).toBeInTheDocument();
      // …and everything under the hit, which is where the files actually go.
      expect(within(list).getByText('N1125035')).toBeInTheDocument();
      expect(within(list).getByText('Revision A')).toBeInTheDocument();
    });

    it('restores the full list when the filter is cleared', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      const list = await openMoveDialog(user);

      const filter = screen.getByPlaceholderText('Filter folders...');
      await user.type(filter, 'brack');
      expect(within(list).queryByText('Art Projects')).not.toBeInTheDocument();

      await user.clear(filter);
      expect(within(list).getByText('Art Projects')).toBeInTheDocument();
      expect(within(list).getByText('Functional Parts')).toBeInTheDocument();
      expect(within(list).getByText('Root (No Folder)')).toBeInTheDocument();
    });

    it('keeps the buttons rendered with a long folder list', async () => {
      const user = userEvent.setup();
      const manyFolders = Array.from({ length: 32 }, (_, i) => ({
        id: 100 + i,
        name: `Kunde ${i}`,
        parent_id: null,
        file_count: 0,
        project_id: null,
        archive_id: null,
        project_name: null,
        archive_name: null,
        latest_activity_at: null,
        children: [],
      }));
      server.use(http.get('/api/v1/library/folders', () => HttpResponse.json(manyFolders)));
      render(<FileManagerPage />);
      const list = await openMoveDialog(user);

      expect(within(list).getAllByRole('button')).toHaveLength(33); // root + 32
      const dialog = within(screen.getByTestId('move-files-modal'));
      expect(dialog.getByText('Cancel')).toBeInTheDocument();
      expect(dialog.getByText('Move')).toBeInTheDocument();

      // Rendered is not the same as reachable: the row of buttons must be the
      // part of the column that cannot be squeezed, or a long list pushes it
      // off the bottom of a short screen.
      expect(screen.getByTestId('move-dialog-actions').className).toContain('flex-shrink-0');
      // …and the list must be the only scrolling box in the dialog, so the
      // overflow lands there rather than on the dialog itself.
      const scrollers = screen
        .getByTestId('move-files-modal')
        .querySelectorAll('.overflow-y-auto');
      expect(scrollers).toHaveLength(1);
      expect(scrollers[0]).toBe(list);
    });

    it('arms Move only at a destination the filtered list is showing', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      const list = await openMoveDialog(user);
      const moveButton = () => within(screen.getByTestId('move-files-modal')).getByText('Move').closest('button')!;

      // The files are in the root, so the root row is the no-op destination.
      await user.type(screen.getByPlaceholderText('Filter folders...'), 'brack');
      expect(within(list).queryByText('Root (No Folder)')).not.toBeInTheDocument();
      // Nothing visible is picked — Move must not fall back to the root and
      // take the files out of every folder.
      expect(moveButton()).toBeDisabled();

      await user.click(within(list).getByText('Brackets'));
      expect(moveButton()).toBeEnabled();

      // Filtering the picked folder away disarms it again. The query has to
      // miss Brackets' ancestors too — a folder that matches keeps its subtree.
      await user.clear(screen.getByPlaceholderText('Filter folders...'));
      await user.type(screen.getByPlaceholderText('Filter folders...'), 'projects');
      expect(within(list).queryByText('Brackets')).not.toBeInTheDocument();
      expect(moveButton()).toBeDisabled();
    });

    it('marks the folder the ticked files live in, not the pane selection', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      // Stand in Brackets, but tick a file that lives in its parent.
      await user.click(screen.getByTitle('Column view'));
      const columns = within(screen.getByTestId('columns-view'));
      await user.click(columns.getByText('Functional Parts'));
      await user.click(await columns.findByText('Brackets'));
      const level = within(screen.getByTestId('columns-level-folder-1'));
      await user.click(await level.findByText('Spacer'));

      await user.click(within(screen.getByTestId('selection-actions')).getByText('Move'));
      const list = within(screen.getByTestId('move-folder-list'));

      // Spacer's own folder is the no-op destination…
      const source = list.getByText('Functional Parts');
      expect(source).toBeDisabled();
      expect(source).toHaveTextContent('(current)');
      // …and the folder the user is standing in is a legal one.
      const target = list.getByText('Brackets');
      expect(target).toBeEnabled();
      await user.click(target);
      expect(within(screen.getByTestId('move-files-modal')).getByText('Move').closest('button')!).toBeEnabled();
    });
  });

  describe('folder search results', () => {
    const typeSearch = async (user: ReturnType<typeof userEvent.setup>, query: string) => {
      await user.type(screen.getByPlaceholderText('Search files...'), query);
    };

    it('lists a matching folder with its path above the file results', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await typeSearch(user, 'brack');

      const section = within(await screen.findByTestId('folder-search-results'));
      expect(section.getByText('Folders (1)')).toBeInTheDocument();
      expect(section.getByText('Brackets')).toBeInTheDocument();
      // The ancestor chain, under the name.
      expect(section.getByText('Functional Parts')).toBeInTheDocument();
      // "brack" also matches bracket.stl, so both sections are counted.
      expect(section.getByText('Files (1)')).toBeInTheDocument();
      expect(screen.getByText('Folders: 1 · Files: 1 of 3')).toBeInTheDocument();
    });

    it('selects the folder and clears the query when a folder hit is clicked', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await typeSearch(user, 'brack');
      const section = within(await screen.findByTestId('folder-search-results'));
      await user.click(section.getByText('Brackets'));

      await waitFor(() => expect(screen.queryByTestId('folder-search-results')).not.toBeInTheDocument());
      expect(screen.getByPlaceholderText('Search files...')).toHaveValue('');
      await waitFor(() => expect(currentCrumb()).toHaveTextContent('Brackets'));
    });

    it('shows no Folders section when only files match', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await typeSearch(user, 'benchy');

      await waitFor(() => expect(screen.queryByText('Cube')).not.toBeInTheDocument());
      expect(screen.queryByTestId('folder-search-results')).not.toBeInTheDocument();
      expect(screen.getByText('Benchy')).toBeInTheDocument();
    });

    it('names both kinds when nothing matches', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      await typeSearch(user, 'zzzz');

      expect(await screen.findByText('No matching files or folders')).toBeInTheDocument();
      expect(screen.queryByTestId('folder-search-results')).not.toBeInTheDocument();
    });
  });

  describe('search and filter', () => {
    it('has search input', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByPlaceholderText('Search files...')).toBeInTheDocument();
      });
    });

    it('has type filter', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('All types')).toBeInTheDocument();
      });
    });

    it('has sort options', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        // Sort dropdown should show Name as default option (persisted to localStorage)
        expect(screen.getByDisplayValue('Name')).toBeInTheDocument();
      });
    });
  });

  describe('selection', () => {
    it('shows select all button', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Select All')).toBeInTheDocument();
      });
    });

    it('can select files', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });

      // Click on the file card to select it
      const fileCard = screen.getByText('Benchy').closest('div[class*="cursor-pointer"]');
      if (fileCard) {
        await user.click(fileCard);
      }

      await waitFor(() => {
        expect(screen.getByText('1 selected')).toBeInTheDocument();
      });
    });

    it('shows bulk actions when files selected', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Select All')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Select All'));

      await waitFor(() => {
        expect(screen.getByText('Move')).toBeInTheDocument();
        expect(screen.getByText('Delete')).toBeInTheDocument();
      });
    });
  });

  describe('new folder modal', () => {
    it('opens new folder modal', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('New Folder')).toBeInTheDocument();
      });

      await user.click(screen.getByText('New Folder'));

      await waitFor(() => {
        expect(screen.getByText('Folder Name')).toBeInTheDocument();
        expect(screen.getByPlaceholderText('e.g., Functional Parts')).toBeInTheDocument();
      });
    });

    it('can create a folder', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('New Folder')).toBeInTheDocument();
      });

      await user.click(screen.getByText('New Folder'));

      await waitFor(() => {
        expect(screen.getByPlaceholderText('e.g., Functional Parts')).toBeInTheDocument();
      });

      const input = screen.getByPlaceholderText('e.g., Functional Parts');
      await user.type(input, 'My New Folder');

      const createButton = screen.getByRole('button', { name: 'Create' });
      await user.click(createButton);

      // Modal should close after creation
      await waitFor(() => {
        expect(screen.queryByText('Folder Name')).not.toBeInTheDocument();
      });
    });
  });

  describe('empty state', () => {
    it('shows empty state when no files', async () => {
      // Folders empty too (#3019): with folders present the pane now shows
      // them as items instead of the empty state.
      server.use(
        http.get('/api/v1/library/files', () => {
          return HttpResponse.json([]);
        }),
        http.get('/api/v1/library/folders', () => {
          return HttpResponse.json([]);
        })
      );

      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('No files yet')).toBeInTheDocument();
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
      });
    });
  });

  describe('bulk-action print button', () => {
    // PR #1625 consolidated print actions: the old single-file-selected
    // "Schedule" button now opens the unified PrintModal (which carries
    // schedule options inside). The bulk-action toolbar shows a single
    // "Print" button only when exactly one sliced file is selected, and
    // hides it for multi-selection. The button is targeted by its accessible
    // name ("Print") + role to disambiguate from the file-card dropdown's
    // own Print entry, which stays collapsed unless its kebab is opened.
    it('shows a Print button in the bulk toolbar when one sliced file is selected', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });

      // Select a sliced file (benchy.gcode.3mf) by clicking on its card
      const fileCard = screen.getByText('Benchy').closest('div[class*="cursor-pointer"]');
      if (fileCard) {
        await user.click(fileCard);
      }

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /^Print$/ })).toBeInTheDocument();
      });
    });

    it('hides the bulk Print button when multiple files are selected', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Select All')).toBeInTheDocument();
      });

      // Select all files
      await user.click(screen.getByText('Select All'));

      await waitFor(() => {
        expect(screen.queryByRole('button', { name: /^Print$/ })).not.toBeInTheDocument();
      });
    });
  });

  describe('STL thumbnail generation', () => {
    it('shows Generate Thumbnails button', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Generate Thumbnails')).toBeInTheDocument();
      });
    });

    it('Generate Thumbnails button covers STL and PDF files (#2976)', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        const button = screen.getByTitle('Generate thumbnails for STL and PDF files without a preview');
        expect(button).toBeInTheDocument();
      });
    });

    it('can click Generate Thumbnails button', async () => {
      const user = userEvent.setup();

      server.use(
        http.post('/api/v1/library/generate-stl-thumbnails', () => {
          return HttpResponse.json({
            processed: 1,
            succeeded: 1,
            failed: 0,
            results: [{ file_id: 2, success: true }],
          });
        })
      );

      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Generate Thumbnails')).toBeInTheDocument();
      });

      const button = screen.getByText('Generate Thumbnails');
      await user.click(button);

      // Button should work without error
      await waitFor(() => {
        expect(screen.getByText('Generate Thumbnails')).toBeInTheDocument();
      });
    });

    it('shows STL file without thumbnail in file list', async () => {
      render(<FileManagerPage />);

      await waitFor(() => {
        // bracket.stl has no thumbnail_path
        expect(screen.getByText('bracket.stl')).toBeInTheDocument();
        expect(screen.getAllByText('STL').length).toBeGreaterThan(0);
      });
    });

    // Since #2976 the server renders PDF thumbnails too, so the per-file
    // action is offered for PDFs and stays hidden for types it cannot render.
    describe('per-file action for PDFs', () => {
      const pdfFile = {
        id: 40,
        filename: 'drawing.pdf',
        file_path: '/library/drawing.pdf',
        file_size: 4096,
        file_type: 'pdf',
        folder_id: null,
        thumbnail_path: null,
        print_name: null,
        print_time_seconds: null,
        print_count: 0,
        duplicate_count: 0,
        created_at: '2024-01-04T00:00:00Z',
      };
      const stepFile = { ...pdfFile, id: 41, filename: 'part.step', file_path: '/library/part.step', file_type: 'step' };

      beforeEach(() => {
        server.use(
          http.get('/api/v1/library/files', () => HttpResponse.json([...mockFiles, pdfFile, stepFile])),
        );
      });

      const openMenu = async (user: ReturnType<typeof userEvent.setup>, filename: string) => {
        const card = screen.getByText(filename).closest('.group') as HTMLElement;
        const kebab = card.querySelector('.lucide-ellipsis-vertical')?.closest('button') as HTMLButtonElement;
        await user.click(kebab);
        return card;
      };

      it('offers Generate Thumbnail in the card menu of a PDF', async () => {
        const user = userEvent.setup();
        server.use(
          http.post('/api/v1/library/generate-stl-thumbnails', async ({ request }) => {
            const body = (await request.json()) as { file_ids?: number[] };
            return HttpResponse.json({
              processed: 1,
              succeeded: 1,
              failed: 0,
              results: [{ file_id: body.file_ids?.[0], success: true }],
            });
          })
        );
        render(<FileManagerPage />);
        await waitFor(() => expect(screen.getByText('drawing.pdf')).toBeInTheDocument());

        const card = await openMenu(user, 'drawing.pdf');
        await user.click(within(card).getByText('Generate Thumbnail'));

        expect(await screen.findByText('Thumbnail generated')).toBeInTheDocument();
      });

      it('does not offer it for STEP, which only the browser can render', async () => {
        const user = userEvent.setup();
        render(<FileManagerPage />);
        await waitFor(() => expect(screen.getByText('part.step')).toBeInTheDocument());

        const card = await openMenu(user, 'part.step');
        expect(within(card).queryByText('Generate Thumbnail')).not.toBeInTheDocument();
      });

      it('offers the action in the list view strip of a PDF', async () => {
        const user = userEvent.setup();
        render(<FileManagerPage />);
        await waitFor(() => expect(screen.getByText('drawing.pdf')).toBeInTheDocument());

        await user.click(screen.getByRole('button', { name: /list/i }));

        // List rows are CSS grids; the row is the nearest grid ancestor.
        await waitFor(() => {
          const row = screen.getByText('drawing.pdf').closest('.grid') as HTMLElement;
          expect(within(row).getByTitle('Generate Thumbnail')).toBeInTheDocument();
        });
        const stepRow = screen.getByText('part.step').closest('.grid') as HTMLElement;
        expect(within(stepRow).queryByTitle('Generate Thumbnail')).not.toBeInTheDocument();
      });
    });
  });

  describe('upload modal (FileUploadModal)', () => {
    it('opens upload modal when Upload button is clicked', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Upload'));

      await waitFor(() => {
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
        expect(screen.getByText(/Drag & drop/)).toBeInTheDocument();
      });
    });

    it('closes upload modal when Cancel is clicked', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Upload'));

      await waitFor(() => {
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
      });

      await user.click(screen.getByRole('button', { name: 'Cancel' }));

      await waitFor(() => {
        expect(screen.queryByText('Upload Files')).not.toBeInTheDocument();
      });
    });

    it('shows 3MF extraction info when 3MF file is added', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Upload'));

      await waitFor(() => {
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
      });

      const threemfFile = new File(['content'], 'model.gcode.3mf', { type: 'application/octet-stream' });
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      expect(fileInput).toBeInTheDocument();

      await user.upload(fileInput, threemfFile);

      await waitFor(() => {
        expect(screen.getByText('3MF files detected')).toBeInTheDocument();
        expect(screen.getByText(/Printer model.*will be automatically extracted/i)).toBeInTheDocument();
      });
    });

    it('shows STL thumbnail option when STL file is added', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Upload'));

      await waitFor(() => {
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
      });

      const stlFile = new File(['solid test'], 'model.stl', { type: 'application/sla' });
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      expect(fileInput).toBeInTheDocument();

      await user.upload(fileInput, stlFile);

      await waitFor(() => {
        expect(screen.getByText('STL thumbnail generation')).toBeInTheDocument();
        expect(screen.getByText(/Thumbnails can be generated/i)).toBeInTheDocument();
      });
    });

    it('shows ZIP options when ZIP file is added', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Upload'));

      await waitFor(() => {
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
      });

      const zipFile = new File(['pk'], 'models.zip', { type: 'application/zip' });
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(fileInput, zipFile);

      await waitFor(() => {
        expect(screen.getByText('ZIP files detected')).toBeInTheDocument();
        expect(screen.getByText(/Preserve folder structure/)).toBeInTheDocument();
      });
    });

    it('can add a file via the file input', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Upload'));

      await waitFor(() => {
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
      });

      const file = new File(['content'], 'model.3mf', { type: 'application/octet-stream' });
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(fileInput, file);

      await waitFor(() => {
        expect(screen.getByText('model.3mf')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /Upload \(1\)/i })).toBeInTheDocument();
      });
    });

    it('uploads file and refreshes file list', async () => {
      server.use(
        http.post('/api/v1/library/files', () => {
          return HttpResponse.json({
            id: 10,
            filename: 'uploaded.3mf',
            file_type: '3mf',
            file_size: 1024,
            thumbnail_path: null,
            duplicate_of: null,
            metadata: null,
          });
        })
      );

      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Upload')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Upload'));

      await waitFor(() => {
        expect(screen.getByText('Upload Files')).toBeInTheDocument();
      });

      const file = new File(['content'], 'uploaded.3mf', { type: 'application/octet-stream' });
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(fileInput, file);

      const uploadButton = screen.getByRole('button', { name: /Upload \(1\)/i });
      await user.click(uploadButton);

      // Modal should auto-close after upload completes
      await waitFor(() => {
        expect(screen.queryByText('Upload Files')).not.toBeInTheDocument();
      });
    });
  });

  describe('authentication-based UI changes', () => {
    it('hides "Uploaded By" column and user filter when auth is disabled', async () => {
      // Mock auth disabled (default)
      server.use(
        http.get('*/api/v1/auth/status', () => {
          return HttpResponse.json({
            auth_enabled: false,
            requires_setup: false,
          });
        }),
        http.get('/api/v1/library/files', () => {
          return HttpResponse.json([
            {
              id: 1,
              filename: 'test.3mf',
              file_path: '/library/test.3mf',
              file_size: 1048576,
              file_type: '3mf',
              folder_id: null,
              thumbnail_path: null,
              print_name: 'Test File',
              print_time_seconds: 3600,
              print_count: 0,
              duplicate_count: 0,
              created_at: '2024-01-01T00:00:00Z',
              created_by_username: 'testuser',
            },
          ]);
        })
      );

      render(<FileManagerPage />);

      // Switch to list view to see the column headers
      await waitFor(() => {
        expect(screen.getByText('Test File')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const listViewButton = screen.getByRole('button', { name: /list/i });
      await user.click(listViewButton);

      // "Uploaded By" column header should not be present
      await waitFor(() => {
        expect(screen.queryByText('Uploaded By')).not.toBeInTheDocument();
      });

      // User filter dropdown should not be present
      expect(screen.queryByPlaceholderText('Filter by user')).not.toBeInTheDocument();
    });

    it('shows "Uploaded By" column and user filter when auth is enabled', async () => {
      // Mock auth enabled
      server.use(
        http.get('*/api/v1/auth/status', () => {
          return HttpResponse.json({
            auth_enabled: true,
            requires_setup: false,
          });
        }),
        http.get('/api/v1/library/files', () => {
          return HttpResponse.json([
            {
              id: 1,
              filename: 'test.3mf',
              file_path: '/library/test.3mf',
              file_size: 1048576,
              file_type: '3mf',
              folder_id: null,
              thumbnail_path: null,
              print_name: 'Test File',
              print_time_seconds: 3600,
              print_count: 0,
              duplicate_count: 0,
              created_at: '2024-01-01T00:00:00Z',
              created_by_username: 'testuser',
            },
          ]);
        }),
        http.get('/api/v1/users/', () => {
          return HttpResponse.json([
            { id: 1, username: 'testuser' },
            { id: 2, username: 'admin' },
          ]);
        })
      );

      render(<FileManagerPage />);

      // Switch to list view to see the column headers
      await waitFor(() => {
        expect(screen.getByText('Test File')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const listViewButton = screen.getByRole('button', { name: /list/i });
      await user.click(listViewButton);

      // "Uploaded By" column header should be present
      await waitFor(() => {
        expect(screen.getByText('Uploaded By')).toBeInTheDocument();
      });

      // User filter dropdown should be present
      expect(screen.getByPlaceholderText('Filter by user')).toBeInTheDocument();

      // Username should be displayed in the column
      expect(screen.getByText('testuser')).toBeInTheDocument();
    });
  });

  describe('folder tree collapse preference (#996)', () => {
    // localStorage is globally mocked in setup.ts (returns undefined by default),
    // so we program each test's getItem return value explicitly.
    const getItemMock = localStorage.getItem as ReturnType<typeof vi.fn>;
    const setItemMock = localStorage.setItem as ReturnType<typeof vi.fn>;

    beforeEach(() => {
      getItemMock.mockReset();
      setItemMock.mockReset();
    });

    // The mock is module-global, so an implementation left behind here would
    // silently change every later describe (e.g. collapsing the folder tree).
    afterEach(() => {
      getItemMock.mockReset();
      setItemMock.mockReset();
    });

    it('defaults to expanded (nested folders visible) when library-collapse-folders is unset', async () => {
      getItemMock.mockReturnValue(null);
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts')).toBeInTheDocument();
      });
      expect(within(screen.getByTestId('folder-sidebar')).getByText('Brackets')).toBeInTheDocument();
    });

    it('honors library-collapse-folders=true on load (nested folders hidden)', async () => {
      getItemMock.mockImplementation((key: string) =>
        key === 'library-collapse-folders' ? 'true' : null
      );
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts')).toBeInTheDocument();
      });
      expect(within(screen.getByTestId('folder-sidebar')).queryByText('Brackets')).not.toBeInTheDocument();
    });

    it('collapses nested folders and persists preference when Collapse is clicked', async () => {
      getItemMock.mockReturnValue(null);
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Brackets')).toBeInTheDocument();
      });

      // The preference lives in the toolbar's folder display menu now, not in
      // the sidebar header — see the "folder display menu" describe below.
      await user.click(screen.getByTestId('folder-display-menu'));
      await user.click(screen.getByRole('menuitemcheckbox', { name: 'Collapse folders by default' }));

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).queryByText('Brackets')).not.toBeInTheDocument();
      });
      expect(setItemMock).toHaveBeenCalledWith('library-collapse-folders', 'true');
    });

    it('re-expands nested folders and persists preference when Collapse is toggled off', async () => {
      getItemMock.mockImplementation((key: string) =>
        key === 'library-collapse-folders' ? 'true' : null
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts')).toBeInTheDocument();
      });
      expect(within(screen.getByTestId('folder-sidebar')).queryByText('Brackets')).not.toBeInTheDocument();

      await user.click(screen.getByTestId('folder-display-menu'));
      await user.click(screen.getByRole('menuitemcheckbox', { name: 'Collapse folders by default' }));

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Brackets')).toBeInTheDocument();
      });
      expect(setItemMock).toHaveBeenCalledWith('library-collapse-folders', 'false');
    });
  });

  describe('Internal / External top-level views (#1621)', () => {
    const externalMockFolders = [
      ...mockFolders,
      {
        id: 99,
        name: 'NAS Library',
        parent_id: null,
        file_count: 200,
        project_id: null,
        archive_id: null,
        project_name: null,
        archive_name: null,
        is_external: true,
        external_readonly: false,
        external_path: '/mnt/nas',
        children: [],
      },
    ];

    it('shows the External sidebar entry only when at least one external folder is linked', async () => {
      // Default mockFolders have no is_external entries → no External row.
      const { unmount } = render(<FileManagerPage />);
      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('All Files')).toBeInTheDocument();
      });
      expect(screen.queryByText('External')).not.toBeInTheDocument();
      unmount();

      // With an external folder linked, the row appears.
      server.use(
        http.get('/api/v1/library/folders', () => HttpResponse.json(externalMockFolders)),
      );
      render(<FileManagerPage />);
      await waitFor(() => {
        expect(screen.getByText('External')).toBeInTheDocument();
      });
    });

    it('sends internal_only=true by default ("All Files" = managed storage only)', async () => {
      const scopes: string[] = [];
      server.use(
        http.get('/api/v1/library/folders', () => HttpResponse.json(externalMockFolders)),
        http.get('/api/v1/library/files', ({ request }) => {
          const url = new URL(request.url);
          scopes.push(
            url.searchParams.get('internal_only') === 'true'
              ? 'internal'
              : url.searchParams.get('external_only') === 'true'
                ? 'external'
                : 'all',
          );
          return HttpResponse.json(mockFiles);
        }),
      );

      render(<FileManagerPage />);
      await waitFor(() => {
        expect(scopes).toContain('internal');
      });
    });

    it('switches to external_only=true when the External sidebar entry is clicked', async () => {
      const scopes: string[] = [];
      server.use(
        http.get('/api/v1/library/folders', () => HttpResponse.json(externalMockFolders)),
        http.get('/api/v1/library/files', ({ request }) => {
          const url = new URL(request.url);
          scopes.push(
            url.searchParams.get('internal_only') === 'true'
              ? 'internal'
              : url.searchParams.get('external_only') === 'true'
                ? 'external'
                : 'all',
          );
          return HttpResponse.json([]);
        }),
      );

      const { default: userEvent } = await import('@testing-library/user-event');
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await waitFor(() => expect(screen.getByText('External')).toBeInTheDocument());

      await user.click(screen.getByText('External'));

      await waitFor(() => {
        expect(scopes).toContain('external');
      });
    });
  });

  describe('"All Files" view (#1499)', () => {
    it('requests every file (include_root=false) so subfolder contents are visible', async () => {
      const rootFile = {
        id: 10,
        filename: 'root-file.3mf',
        file_path: '/library/root-file.3mf',
        file_size: 1024,
        file_type: '3mf',
        folder_id: null,
        thumbnail_path: null,
        print_name: 'Root File',
        print_time_seconds: 0,
        print_count: 0,
        duplicate_count: 0,
        created_at: '2024-01-01T00:00:00Z',
      };
      const nestedFile = {
        ...rootFile,
        id: 11,
        filename: 'nested-file.3mf',
        file_path: '/library/Functional Parts/nested-file.3mf',
        folder_id: 1,
        print_name: 'Nested File',
      };

      const includeRootValues: string[] = [];
      server.use(
        http.get('/api/v1/library/files', ({ request }) => {
          const url = new URL(request.url);
          const includeRoot = url.searchParams.get('include_root');
          includeRootValues.push(includeRoot ?? '');
          // Mirror the backend: include_root=false returns everything; true
          // returns only files with folder_id IS NULL.
          if (includeRoot === 'false') {
            return HttpResponse.json([rootFile, nestedFile]);
          }
          return HttpResponse.json([rootFile]);
        }),
      );

      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Root File')).toBeInTheDocument();
        expect(screen.getByText('Nested File')).toBeInTheDocument();
      });
      // Sanity-check: the buggy call would have sent include_root=true here.
      expect(includeRootValues).toContain('false');
    });
  });

  describe('last-modified date display (#2680)', () => {
    it('is hidden by default and revealed by the toolbar toggle', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(screen.getByText('Benchy')).toBeInTheDocument();
      });

      // Hidden by default.
      expect(screen.queryByText(/2030/)).not.toBeInTheDocument();

      // Toggle on via the toolbar button.
      await user.click(screen.getByTitle('Show modified dates'));

      // benchy carries fs_modified_at in 2030, which must be preferred over its
      // created_at (2024) — proving the real on-disk mtime drives the display.
      await waitFor(() => {
        expect(screen.getByText(/2030/)).toBeInTheDocument();
      });

      // Toggling off hides it again.
      await user.click(screen.getByTitle('Hide modified dates'));
      await waitFor(() => {
        expect(screen.queryByText(/2030/)).not.toBeInTheDocument();
      });
    });

    it('the same toggle reveals latest activity on folder rows, including nested ones', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => {
        expect(within(screen.getByTestId('folder-sidebar')).getByText('Functional Parts')).toBeInTheDocument();
      });

      expect(screen.queryByText(/2031/)).not.toBeInTheDocument();

      await user.click(screen.getByTitle('Show modified dates'));

      await waitFor(() => {
        expect(screen.getByText(/2031/)).toBeInTheDocument();
      });
      // Nested folders get it too — the prop must survive the recursion.
      expect(screen.getByText(/2032/)).toBeInTheDocument();

      // A folder with no activity timestamp renders nothing rather than an
      // "Invalid Date" string.
      const artRow = within(screen.getByTestId('folder-sidebar')).getByText('Art Projects').closest('div.group')!;
      expect(artRow.textContent).not.toMatch(/Invalid/);

      await user.click(screen.getByTitle('Hide modified dates'));
      await waitFor(() => {
        expect(screen.queryByText(/2031/)).not.toBeInTheDocument();
      });
    });
  });

  describe('slice action', () => {
    beforeEach(() => {
      vi.mocked(openInSlicer).mockClear();
      server.use(
        http.post('/api/v1/library/files/:id/slicer-token', () => HttpResponse.json({ token: 'test-token' })),
        // The only sliceable fixture is an STL, and since #3029 the desktop
        // handoff is only offered to a slicer whose protocol handler will
        // actually load one -- Bambu Studio's takes 3MF only. These tests are
        // about the handoff mechanics and the permission gate, not about which
        // slicer, so they run against OrcaSlicer. The Bambu Studio side is
        // covered by its own tests below.
        http.get('/api/v1/settings/', () => HttpResponse.json({ preferred_slicer: 'orcaslicer' })),
      );
    });

    afterEach(() => {
      // Permission tests set a token; clear it so it can't leak into the
      // list-view tests that follow (mirrors FileManagerFolderDelete.test.tsx).
      setAuthToken(null);
    });

    const openMenu = async (user: ReturnType<typeof userEvent.setup>, filename: string) => {
      const card = screen.getByText(filename).closest('.group') as HTMLElement;
      // Target the kebab (ellipsis) toggle specifically rather than the card's
      // first button — a button added ahead of the kebab would otherwise
      // break the menu-opening assumption.
      const kebab = card.querySelector('.lucide-ellipsis-vertical')?.closest('button') as HTMLButtonElement;
      await user.click(kebab);
      return card;
    };

    it('opens the desktop slicer when the slicer API is disabled', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      await user.click(within(card).getByText('Slice'));

      await waitFor(() => {
        expect(openInSlicer).toHaveBeenCalledWith(
          expect.stringContaining('/library/files/2/dl/test-token/'),
          'orcaslicer',
        );
      });
    });

    it('opens the in-app SliceModal when the slicer API is enabled', async () => {
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ use_slicer_api: true })),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      await user.click(within(card).getByText('Slice'));

      expect(await screen.findByTestId('slice-modal')).toBeInTheDocument();
      expect(openInSlicer).not.toHaveBeenCalled();
    });

    // #2846: the menu used to be an absolutely-positioned child of the card,
    // and the card clipped its own overflow. A bare STL card is only about
    // 270px tall -- thumbnail plus name and size -- which is shorter than the
    // seven-entry menu, so the top entry was cut off. That entry is Slice,
    // because Print is suppressed for an unsliced file. A 3MF card carries two
    // more metadata rows and was tall enough, which is why the report said 3MF
    // worked. Nothing about STL was special; the card was just the shortest.
    it('keeps the first menu entry out of the card so it cannot be clipped (#2846)', async () => {
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ use_slicer_api: true })),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      const menu = within(card).getByText('Slice').closest('.fixed');
      // Viewport-positioned, so no ancestor's overflow can cut it down.
      expect(menu).not.toBeNull();
      expect(within(menu as HTMLElement).getAllByRole('button')[0]).toHaveTextContent('Slice');
      // And the card itself no longer clips what its children draw.
      expect(card.className).not.toContain('overflow-hidden');
    });

    // #3029: Bambu Studio's protocol handler refuses anything that is not a
    // 3MF before it even fetches the URL -- "Download failed, unknown file
    // format." Offering the handoff anyway put an action on an STL card that
    // could only fail, with an error that blamed the file.
    it('hides the desktop handoff for an STL when the target is Bambu Studio', async () => {
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ preferred_slicer: 'bambu_studio' })),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      expect(within(card).queryByText('Slice')).not.toBeInTheDocument();
    });

    it('honours the open_in_slicer override over the preferred slicer', async () => {
      // The desktop target is its own setting (#1329), so it -- not
      // preferred_slicer, which drives the sidecar -- decides whether an STL
      // can be handed over at all.
      server.use(
        http.get('/api/v1/settings/', () =>
          HttpResponse.json({ preferred_slicer: 'bambu_studio', open_in_slicer: 'orcaslicer' }),
        ),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      await user.click(within(card).getByText('Slice'));

      await waitFor(() => {
        expect(openInSlicer).toHaveBeenCalledWith(expect.any(String), 'orcaslicer');
      });
    });

    it('still offers an STL to the in-app slicer with Bambu Studio as the desktop target', async () => {
      // The restriction is on the URL handoff, not on the file: the sidecar
      // slices an STL regardless of which desktop slicer is configured.
      server.use(
        http.get('/api/v1/settings/', () =>
          HttpResponse.json({ use_slicer_api: true, preferred_slicer: 'bambu_studio' }),
        ),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      await user.click(within(card).getByText('Slice'));

      expect(await screen.findByTestId('slice-modal')).toBeInTheDocument();
    });

    it('hides the slice item for already-sliced files', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('Benchy')).toBeInTheDocument());

      const card = await openMenu(user, 'Benchy');
      expect(within(card).queryByText('Slice')).not.toBeInTheDocument();
    });

    // Permission gating is the security-relevant half of the slice action: the
    // in-app API path needs library:upload, and the desktop handoff mirrors the
    // ownership check the slicer-token endpoint runs — library:read_all or
    // library:read_own. The legacy library:read is deliberately not accepted;
    // it satisfies neither the token endpoint nor the folder listing that gets
    // a user to this page at all.
    const mockAuthUser = (permissions: string[]) => {
      setAuthToken('test-token', 'session');
      server.use(
        http.get('*/api/v1/auth/status', () =>
          HttpResponse.json({ auth_enabled: true, requires_setup: false }),
        ),
        http.get('*/api/v1/auth/me', () =>
          HttpResponse.json({
            id: 7,
            username: 'operator1',
            is_admin: false,
            permissions,
          }),
        ),
        http.get('/api/v1/users/', () => HttpResponse.json([])),
      );
    };

    it('disables the Slice menu item without library:upload when the slicer API is enabled', async () => {
      mockAuthUser([]);
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ use_slicer_api: true })),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      const sliceItem = within(card).getByText('Slice').closest('button');
      expect(sliceItem).toBeDisabled();

      await user.click(sliceItem!);
      expect(openInSlicer).not.toHaveBeenCalled();
    });

    it('enables the Slice menu item with library:upload when the slicer API is enabled', async () => {
      mockAuthUser(['library:upload']);
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ use_slicer_api: true })),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      const sliceItem = within(card).getByText('Slice').closest('button');
      expect(sliceItem).not.toBeDisabled();
    });

    it('disables the Slice menu item without any library read permission for the desktop handoff', async () => {
      mockAuthUser(['library:upload']);
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      const sliceItem = within(card).getByText('Slice').closest('button');
      expect(sliceItem).toBeDisabled();

      await user.click(sliceItem!);
      expect(openInSlicer).not.toHaveBeenCalled();
    });

    it('enables the Slice menu item with library:read_own for the desktop handoff', async () => {
      mockAuthUser(['library:read_own']);
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      const sliceItem = within(card).getByText('Slice').closest('button');
      expect(sliceItem).not.toBeDisabled();
    });

    it('does not accept the legacy library:read for the desktop handoff', async () => {
      // require_ownership_permission(LIBRARY_READ_ALL, LIBRARY_READ_OWN) does no
      // legacy expansion, so this group 403s on the slicer-token endpoint.
      // Enabling the item would offer an action the server refuses, and the
      // failure would look like "no slicer installed" once the fallback URL is
      // handed over.
      mockAuthUser(['library:read']);
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const card = await openMenu(user, 'bracket.stl');
      const sliceItem = within(card).getByText('Slice').closest('button');
      expect(sliceItem).toBeDisabled();

      await user.click(sliceItem!);
      expect(openInSlicer).not.toHaveBeenCalled();
    });

    it('slices from the list-view button when the slicer API is disabled', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      await user.click(screen.getByTitle('List view'));
      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const row = screen.getByText('bracket.stl').closest('div[class*="cursor-pointer"]') as HTMLElement;
      await user.click(within(row).getByTitle('Slice'));

      await waitFor(() => {
        expect(openInSlicer).toHaveBeenCalledWith(
          expect.stringContaining('/library/files/2/dl/test-token/'),
          'orcaslicer',
        );
      });
    });

    it('slices from the list-view button into the in-app modal when the slicer API is enabled', async () => {
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ use_slicer_api: true })),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      await user.click(screen.getByTitle('List view'));
      await waitFor(() => expect(screen.getByText('bracket.stl')).toBeInTheDocument());

      const row = screen.getByText('bracket.stl').closest('div[class*="cursor-pointer"]') as HTMLElement;
      await user.click(within(row).getByTitle('Slice'));

      expect(await screen.findByTestId('slice-modal')).toBeInTheDocument();
      expect(openInSlicer).not.toHaveBeenCalled();
    });
  });
});
