import { useState, useRef, useCallback, useMemo, useEffect, lazy, Suspense } from 'react';
import { createPortal } from 'react-dom';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  FolderOpen,
  Loader2,
  Plus,
  Upload,
  Trash2,
  Download,
  ExternalLink,
  MoreVertical,
  ChevronRight,
  FolderPlus,
  FileBox,
  Clock,
  CalendarClock,
  HardDrive,
  File,
  MoveRight,
  CheckSquare,
  Square,
  Layers,
  LayoutGrid,
  List,
  Search,
  SortAsc,
  SortDesc,
  AlertTriangle,
  Filter,
  X,
  Link2,
  Unlink,
  Archive as ArchiveIcon,
  Briefcase,
  Cog,
  Play,
  Printer,
  Pencil,
  Image,
  User,
  Box,
  RefreshCw,
  Lock,
  FolderSymlink,
  Tag as TagIcon,
  FileText,
  FileSpreadsheet,
  Mail,
  Columns,
  ChevronRight as ChevronRightIcon,
  Info,
  Globe,
  StickyNote,
  Camera,
  Eye,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Copy,
  FolderX,
  type LucideIcon,
} from 'lucide-react';
import { ApiError, api } from '../api/client';
import type {
  LibraryFolderTree,
  LibraryFileListItem,
  LibraryFolderCreate,
  LibraryFolderUpdate,
  ExternalFolderCreate,
  AppSettings,
  Archive,
  Permission,
  PendingPreviewThumbnail,
} from '../api/client';
import { Button } from '../components/Button';
import { PreviewThumbnailBatch } from '../components/PreviewThumbnailBatch';
import { ConfirmModal } from '../components/ConfirmModal';
import { ContextMenu, type ContextMenuItem } from '../components/ContextMenu';
import { PrintModal } from '../components/PrintModal';
import { ModelViewerModal } from '../components/ModelViewerModal';
import { SliceModal } from '../components/SliceModal';
import { RunWithPipelineModal } from '../components/RunWithPipelineModal';
import { BulkTagsPickerModal } from '../components/BulkTagsPickerModal';
import { FileUploadModal } from '../components/FileUploadModal';
import { FolderNumber } from '../components/FolderNumber';
import { FolderReadmePanel } from '../components/FolderReadmePanel';
import { LibraryTagsModal } from '../components/LibraryTagsModal';
import { LibraryFileDetailsModal } from '../components/LibraryFileDetailsModal';
import { PurgeOldFilesModal } from '../components/PurgeOldFilesModal';
import { CopyButton, copyTextToClipboard } from '../components/CopyButton';
import { useToast } from '../contexts/ToastContext';
import { usePageFileDrop } from '../hooks/usePageFileDrop';
import { useAuth } from '../contexts/AuthContext';
import { formatDuration, parseUTCDate, formatDate } from '../utils/date';
import { formatFileSize } from '../utils/file';
import { folderLabel, folderText } from '../utils/folder';
import { assignableProjects } from '../utils/projectTree';
import { openInSlicer, resolveDesktopSlicer, type SlicerType } from '../utils/slicer';
import { isSlicedLibraryFile, isSliceableLibraryFile } from '../utils/libraryFiles';

type SortField = 'name' | 'date' | 'size' | 'type' | 'prints';
type SortDirection = 'asc' | 'desc';
type TFunction = (key: string, options?: Record<string, unknown>) => string;

// Document previews (#2976) are code-split: pdf.js and the spreadsheet
// parsers only load when a preview is actually opened.
const PdfPreviewModal = lazy(() =>
  import('../components/PdfPreviewModal').then((m) => ({ default: m.PdfPreviewModal }))
);
const SpreadsheetPreviewModal = lazy(() =>
  import('../components/SpreadsheetPreviewModal').then((m) => ({ default: m.SpreadsheetPreviewModal }))
);
const MsgPreviewModal = lazy(() =>
  import('../components/MsgPreviewModal').then((m) => ({ default: m.MsgPreviewModal }))
);
const ImagePreviewModal = lazy(() =>
  import('../components/ImagePreviewModal').then((m) => ({ default: m.ImagePreviewModal }))
);

function isSpreadsheetType(fileType: string): boolean {
  return fileType === 'csv' || fileType === 'xlsx' || fileType === 'ods';
}

function isStepType(fileType: string): boolean {
  return fileType === 'step' || fileType === 'stp';
}

// Mirrors IMAGE_EXTENSIONS in routes/library.py - the types the server both
// stores and renders a thumbnail for, and so the ones that get an image icon.
const IMAGE_TYPES = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tiff', 'tif']);

// The subset ImagePreviewModal can actually show: it hands the bytes to an
// <img>, and outside Safari no browser decodes TIFF. Offering the preview
// would download up to 50 MB only to report "cannot be previewed", so TIFF
// keeps its server-rendered thumbnail and no preview (#2976).
const PREVIEWABLE_IMAGE_TYPES = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']);

function isImageType(fileType: string): boolean {
  return IMAGE_TYPES.has(fileType.toLowerCase());
}

function isPreviewableImageType(fileType: string): boolean {
  return PREVIEWABLE_IMAGE_TYPES.has(fileType.toLowerCase());
}

type PreviewKind = 'gcode' | 'model' | 'pdf' | 'msg' | 'spreadsheet' | 'image';

// The one table of which preview a file opens (#2976), null for a file that
// has none. `openPreview` dispatches on it, `isPreviewableLibraryFile` asks
// whether there is one and `isModelPreview` whether it is one of the 3D ones:
// a type added here reaches all three at once.
function previewKind(file: LibraryFileListItem): PreviewKind | null {
  const type = file.file_type;
  if (isSlicedLibraryFile(file)) return 'gcode';
  if (type === '3mf' || type === 'stl' || isStepType(type)) return 'model';
  if (type === 'pdf') return 'pdf';
  if (type === 'msg') return 'msg';
  if (isSpreadsheetType(type)) return 'spreadsheet';
  if (isPreviewableImageType(type)) return 'image';
  return null;
}

// Which files have a preview at all: what a double-click opens, and what the
// toolbar's Preview button appears for (#2976). Sliced files go to the
// full-page gcode viewer, everything else to a modal.
function isPreviewableLibraryFile(file: LibraryFileListItem): boolean {
  return previewKind(file) !== null;
}

// Spread onto a card/row subtree that is not "the row": its own controls must
// neither toggle the selection nor open the preview. `dblclick` is a separate
// native event from `click`, so stopping the click alone still lets the second
// click of a double-click reach the row's onDoubleClick (#2976).
const stopRowActivation = {
  onClick: (e: React.MouseEvent) => e.stopPropagation(),
  onDoubleClick: (e: React.MouseEvent) => e.stopPropagation(),
};

// Whether the preview is the 3D one, which has its own menu label.
function isModelPreview(file: LibraryFileListItem): boolean {
  const kind = previewKind(file);
  return kind === 'gcode' || kind === 'model';
}

// The one type -> icon table, used by the grid card, the list row, the
// columns view and the preview entry of the per-file menu. null is a type
// with no icon of its own; each caller supplies its own fallback.
function fileTypeIcon(fileType: string): LucideIcon | null {
  if (fileType === 'pdf') return FileText;
  if (fileType === 'msg') return Mail;
  if (isSpreadsheetType(fileType)) return FileSpreadsheet;
  if (isImageType(fileType)) return Image;
  return null;
}

function documentPreviewIcon(fileType: string) {
  const Icon = fileTypeIcon(fileType) ?? Image;
  return <Icon className="w-4 h-4" />;
}

// Placeholder for a file with no thumbnail. csv/xlsx/ods/msg never get a
// server-rendered one, so for those this icon is all the grid, the list and
// the columns view have to tell them apart (#2976).
function FileTypePlaceholderIcon({ fileType, className }: { fileType: string; className: string }) {
  const Icon = fileTypeIcon(fileType) ?? FileBox;
  return <Icon className={className} />;
}

// Types the server renders thumbnails for itself, so the batch button and the
// per-file "Generate thumbnail" action apply (STL via trimesh, PDF via
// pypdfium2 - #2976). Mirrors SERVER_THUMBNAIL_TYPES in routes/library.py.
function hasServerThumbnail(fileType: string): boolean {
  return fileType === 'stl' || fileType === 'pdf';
}

// The one type -> badge colour table, in the two variants the views need:
// 'tinted' for the list row and the columns view, 'solid' for the grid card's
// badge, which sits on top of the thumbnail. Sliced output shares the gcode
// blue so users see at a glance that the file is already sliced and ready to
// print (#1543). Both variants are spelled out in full because Tailwind only
// keeps class names it can read in the source.
function fileTypeBadgeClass(fileType: string, variant: 'tinted' | 'solid' = 'tinted'): string {
  const solid = variant === 'solid';
  if (fileType === '3mf') return solid ? 'bg-bambu-green/90 text-white' : 'bg-bambu-green/20 text-bambu-green';
  if (fileType === 'gcode' || fileType === 'gcode.3mf') {
    return solid ? 'bg-blue-500/90 text-white' : 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400';
  }
  if (fileType === 'stl') {
    return solid ? 'bg-purple-500/90 text-white' : 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400';
  }
  if (isStepType(fileType)) {
    return solid ? 'bg-amber-500/90 text-white' : 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400';
  }
  if (fileType === 'pdf') {
    return solid ? 'bg-red-500/90 text-white' : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400';
  }
  if (isSpreadsheetType(fileType)) {
    return solid ? 'bg-teal-500/90 text-white' : 'bg-teal-100 dark:bg-teal-500/20 text-teal-700 dark:text-teal-400';
  }
  if (fileType === 'msg') {
    return solid ? 'bg-sky-500/90 text-white' : 'bg-sky-100 dark:bg-sky-500/20 text-sky-700 dark:text-sky-400';
  }
  if (isImageType(fileType)) {
    return solid ? 'bg-indigo-500/90 text-white' : 'bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400';
  }
  return solid ? 'bg-bambu-gray/90 text-white' : 'bg-bambu-gray/20 text-bambu-gray';
}

// Search / type / user filtering plus the sort, shared by the main listing and
// by each Miller column's file list so a column never sorts differently from
// the pane beside it. `query` matches the filename and the embedded print name.
function filterAndSortFiles(
  files: LibraryFileListItem[],
  opts: {
    query?: string;
    filterType: string;
    filterUsername: string;
    sortField: SortField;
    sortDirection: SortDirection;
  },
): LibraryFileListItem[] {
  let result = [...files];

  const query = opts.query?.trim().toLowerCase();
  if (query) {
    result = result.filter(
      (f) =>
        f.filename.toLowerCase().includes(query) ||
        (f.print_name && f.print_name.toLowerCase().includes(query))
    );
  }

  if (opts.filterType !== 'all') {
    result = result.filter((f) => f.file_type === opts.filterType);
  }

  const userQuery = opts.filterUsername.trim().toLowerCase();
  if (userQuery) {
    result = result.filter(
      (f) => f.created_by_username && f.created_by_username.toLowerCase().includes(userQuery)
    );
  }

  result.sort((a, b) => {
    let comparison = 0;
    switch (opts.sortField) {
      case 'name':
        comparison = (a.print_name || a.filename).localeCompare(b.print_name || b.filename);
        break;
      case 'date':
        // #2680: sort by real on-disk mtime (matches `ls -t`), falling back to
        // the DB created_at for managed uploads that have no filesystem mtime.
        comparison =
          (parseUTCDate(a.fs_modified_at ?? a.created_at)?.getTime() ?? 0) -
          (parseUTCDate(b.fs_modified_at ?? b.created_at)?.getTime() ?? 0);
        break;
      case 'size':
        comparison = a.file_size - b.file_size;
        break;
      case 'type':
        comparison = a.file_type.localeCompare(b.file_type);
        break;
      case 'prints':
        comparison = a.print_count - b.print_count;
        break;
    }
    return opts.sortDirection === 'asc' ? comparison : -comparison;
  });

  return result;
}

/** The key of the running-number series order folders draw from. */
const FOLDER_SERIES_KEY = 'library_folder';

// New Folder Modal
interface NewFolderModalProps {
  parentId: number | null;
  onClose: () => void;
  onSave: (data: LibraryFolderCreate) => void;
  isLoading: boolean;
  t: TFunction;
}

function NewFolderModal({ parentId, onClose, onSave, isLoading, t }: NewFolderModalProps) {
  const [name, setName] = useState('');
  // On by default: in the workflow this exists for, every new folder is an
  // order and every order gets a number. Unticking it is the exception.
  const [assignNumber, setAssignNumber] = useState(true);

  const { data: allSeries } = useQuery({
    queryKey: ['number-series'],
    queryFn: () => api.getNumberSeries(),
  });
  // Only an enabled series has anything to offer; with it off the dialog looks
  // exactly as it did before the feature.
  const folderSeries = allSeries?.find((s) => s.key === FOLDER_SERIES_KEY && s.enabled);
  const willBeNumbered = Boolean(folderSeries) && assignNumber;

  // An order folder that is only a number is the normal case here, so a name
  // is required only when no number is coming with it.
  const canSubmit = Boolean(name.trim()) || willBeNumbered;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    onSave({ name: name.trim(), parent_id: parentId, use_number_series: willBeNumbered });
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-sm border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('fileManager.newFolder')}</h2>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-white mb-1">
              {t('fileManager.folderName')}
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green"
              placeholder={t('fileManager.folderNamePlaceholder')}
              autoFocus
            />
          </div>
          {folderSeries && (
            <div>
              <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
                <input
                  type="checkbox"
                  checked={assignNumber}
                  onChange={(e) => setAssignNumber(e.target.checked)}
                  className="w-4 h-4 accent-bambu-green"
                />
                {t('fileManager.assignNumber')}
              </label>
              <p className="mt-1 text-xs text-bambu-gray font-mono">
                {t('fileManager.nextNumber', { value: folderSeries.preview })}
              </p>
            </div>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!canSubmit || isLoading}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.create')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// External Folder Modal
interface ExternalFolderModalProps {
  onClose: () => void;
  onSave: (data: ExternalFolderCreate) => void;
  isLoading: boolean;
  t: TFunction;
}

function ExternalFolderModal({ onClose, onSave, isLoading, t }: ExternalFolderModalProps) {
  const [name, setName] = useState('');
  const [path, setPath] = useState('');
  const [readonly, setReadonly] = useState(true);
  const [showHidden, setShowHidden] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSave({
      name: name.trim(),
      external_path: path.trim(),
      readonly,
      show_hidden: showHidden,
    });
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <FolderSymlink className="w-5 h-5 text-bambu-green" />
            {t('fileManager.linkExternalFolder')}
          </h2>
          <p className="text-sm text-bambu-gray mt-1">{t('fileManager.linkExternalFolderDescription')}</p>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-white mb-1">
              {t('fileManager.folderName')}
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green"
              placeholder={t('fileManager.externalFolderNamePlaceholder')}
              autoFocus
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-white mb-1">
              {t('fileManager.externalPath')}
            </label>
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green font-mono text-sm"
              placeholder="/mnt/nas/3d-prints"
              required
            />
            <p className="text-xs text-bambu-gray mt-1">{t('fileManager.externalPathHelp')}</p>
          </div>
          <div className="space-y-2">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={readonly}
                onChange={(e) => setReadonly(e.target.checked)}
                className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
              />
              <span className="text-sm text-white">{t('fileManager.readOnly')}</span>
              <span className="text-xs text-bambu-gray">({t('fileManager.readOnlyHelp')})</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={showHidden}
                onChange={(e) => setShowHidden(e.target.checked)}
                className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
              />
              <span className="text-sm text-white">{t('fileManager.showHiddenFiles')}</span>
            </label>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!name.trim() || !path.trim() || isLoading}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('fileManager.linkFolder')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// FAT32/exFAT-illegal chars rejected by Bambu Studio (#1540). Mirrors the
// backend validator in backend/app/utils/filename.py — keep in sync.
const INVALID_FILENAME_CHARS = '<>:"/\\|?*';

function findInvalidFilenameChar(name: string): string | null {
  for (const ch of name) {
    if (INVALID_FILENAME_CHARS.includes(ch)) return ch;
    if (ch.charCodeAt(0) < 0x20) return ch;
  }
  return null;
}

// Rename Modal
interface RenameModalProps {
  type: 'file' | 'folder';
  currentName: string;
  /** Folders only: the running number, edited in its own field so a rename
   *  can never drop it. */
  currentNumber?: string | null;
  onClose: () => void;
  onSave: (newName: string, newNumber?: string | null, nameChanged?: boolean) => void;
  isLoading: boolean;
  t: TFunction;
}

function RenameModal({ type, currentName, currentNumber, onClose, onSave, isLoading, t }: RenameModalProps) {
  // For files, separate the extension so users can only edit the base name
  // Handle compound extensions like .gcode.3mf
  const fileExtension = type === 'file' ? (currentName.match(/(\.gcode\.3mf|\.3mf|\.gcode)$/i)?.[1] ?? '') : '';
  const baseName = type === 'file' && fileExtension ? currentName.slice(0, -fileExtension.length) : currentName;
  const [name, setName] = useState(baseName);
  const [number, setNumber] = useState(currentNumber ?? '');

  const invalidChar = type === 'file' ? findInvalidFilenameChar(name) : null;
  const filenameError = invalidChar
    ? t('fileManager.invalidFilenameChar', { char: invalidChar })
    : null;

  const trimmedNumber = number.trim();
  const numberChanged = type === 'folder' && trimmedNumber !== (currentNumber ?? '');
  const nameChanged = name.trim() !== baseName;
  // A folder that carries a number may be left nameless — that is what an
  // order folder looks like before anyone types a customer onto it.
  const nameRequired = type === 'file' || !trimmedNumber;
  const canSubmit =
    (!nameRequired || Boolean(name.trim())) && (nameChanged || numberChanged) && !filenameError;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    const fullName = type === 'file' ? name.trim() + fileExtension : name.trim();
    // `undefined` on either side leaves that field alone: an order folder can
    // be nothing but a number, and re-sending its empty name would be refused.
    onSave(fullName, numberChanged ? trimmedNumber || null : undefined, nameChanged);
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-sm border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{type === 'file' ? t('fileManager.renameFile') : t('fileManager.renameFolder')}</h2>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-white mb-1" htmlFor="rename-name">
              {t('common.name')}
            </label>
            <div className={`flex items-center bg-bambu-dark border rounded focus-within:border-bambu-green ${filenameError ? 'border-red-500' : 'border-bambu-dark-tertiary'}`}>
              <input
                id="rename-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="flex-1 bg-transparent px-3 py-2 text-white placeholder-bambu-gray focus:outline-none min-w-0"
                autoFocus
                required={nameRequired}
              />
              {fileExtension && (
                <span className="pr-3 text-bambu-gray text-sm select-none whitespace-nowrap">{fileExtension}</span>
              )}
            </div>
            {filenameError && (
              <p className="mt-1 text-xs text-red-700 dark:text-red-400">{filenameError}</p>
            )}
          </div>
          {type === 'folder' && (
            <div>
              <label className="block text-sm font-medium text-white mb-1" htmlFor="folder-number">
                {t('fileManager.folderNumber')}
              </label>
              <input
                id="folder-number"
                type="text"
                maxLength={32}
                value={number}
                onChange={(e) => setNumber(e.target.value)}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green font-mono"
              />
            </div>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!canSubmit || isLoading}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.rename')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// Move Files Modal
interface MoveFilesModalProps {
  folders: LibraryFolderTree[];
  selectedFiles: number[];
  // The folder the selected files actually sit in, not the folder the page has
  // selected: every column can tick a file, so the two differ. `undefined` when
  // the selection spans several folders and no single row is "current".
  currentFolderId: number | null | undefined;
  onClose: () => void;
  onMove: (folderId: number | null) => void;
  isLoading: boolean;
  t: TFunction;
}

function MoveFilesModal({ folders, selectedFiles, currentFolderId, onClose, onMove, isLoading, t }: MoveFilesModalProps) {
  const [targetFolder, setTargetFolder] = useState<number | null>(null);
  const [folderFilter, setFolderFilter] = useState('');

  type MoveTarget = { id: number | null; name: string; number?: string | null; depth: number };

  const flattenFolders = (items: LibraryFolderTree[], depth = 0): MoveTarget[] => {
    const result: MoveTarget[] = [];
    for (const item of items) {
      // The number travels with the row: an order folder is often nothing but
      // its number, and a row drawn from the name alone would be blank.
      result.push({ id: item.id, name: item.name, number: item.number, depth });
      if (item.children.length > 0) {
        result.push(...flattenFolders(item.children, depth + 1));
      }
    }
    return result;
  };

  // A folder survives the filter when its own name matches or one of its
  // descendants does: dropping the parent of a hit would leave the hit
  // indented under nothing. A folder that matches itself keeps its whole
  // subtree — typing a customer's name is how you narrow to that customer's
  // jobs, and those job folders are the destinations, so filtering them out
  // would leave only the level above the one the user wants.
  const query = folderFilter.trim().toLowerCase();
  // The number counts as a match too: typing the number off the quote is how
  // an order folder is found, and a folder that has no name has nothing else
  // to type.
  const rowMatches = (item: LibraryFolderTree): boolean =>
    folderText(item).toLowerCase().includes(query);
  const matchesQuery = (item: LibraryFolderTree): boolean =>
    rowMatches(item) || item.children.some(matchesQuery);
  const filterTree = (items: LibraryFolderTree[]): LibraryFolderTree[] =>
    items
      .filter(matchesQuery)
      .map((item) =>
        rowMatches(item) ? item : { ...item, children: filterTree(item.children) }
      );

  const rootEntry: MoveTarget = { id: null, name: t('fileManager.rootNoFolder'), depth: 0 };
  const flatFolders = query
    ? [
        ...(rootEntry.name.toLowerCase().includes(query) ? [rootEntry] : []),
        ...flattenFolders(filterTree(folders)),
      ]
    : [rootEntry, ...flattenFolders(folders)];

  // Move may only fire at a row the list is offering right now. The filter can
  // drop the highlighted row — including the "Root (No Folder)" one the dialog
  // opens on — and firing at an invisible target would silently take the files
  // out of every folder.
  const targetSelectable = flatFolders.some(
    (folder) => folder.id === targetFolder && folder.id !== currentFolderId
  );

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      {/* Grows with the window instead of staying a 384x256 peephole, but
          stays a flex column so the header and the buttons keep their place
          on a short laptop screen and only the list scrolls. */}
      <div
        className="bg-bambu-dark-secondary rounded-lg w-full max-w-lg max-h-[90vh] flex flex-col border border-bambu-dark-tertiary"
        data-testid="move-files-modal"
      >
        <div className="p-4 border-b border-bambu-dark-tertiary flex-shrink-0">
          <h2 className="text-lg font-semibold text-white">{t('fileManager.moveFiles', { count: selectedFiles.length })}</h2>
        </div>
        <div className="p-4 flex flex-col gap-4 min-h-0 flex-1">
          <input
            type="text"
            value={folderFilter}
            onChange={(e) => setFolderFilter(e.target.value)}
            placeholder={t('fileManager.folderFilter.placeholder')}
            aria-label={t('fileManager.folderFilter.placeholder')}
            autoFocus
            className="w-full flex-shrink-0 bg-bambu-dark border border-bambu-dark-tertiary rounded px-3 py-2 text-sm text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green"
          />
          <div className="flex-1 min-h-0 max-h-[min(60vh,32rem)] overflow-y-auto space-y-1" data-testid="move-folder-list">
            {flatFolders.map((folder) => (
              <button
                key={folder.id ?? 'root'}
                onClick={() => setTargetFolder(folder.id)}
                disabled={folder.id === currentFolderId}
                className={`w-full text-left px-3 py-2 rounded transition-colors flex items-center gap-2 ${
                  targetFolder === folder.id
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : folder.id === currentFolderId
                    ? 'opacity-50 cursor-not-allowed text-bambu-gray'
                    : 'hover:bg-bambu-dark text-white'
                }`}
                style={{ paddingLeft: `${12 + folder.depth * 16}px` }}
                title={folderLabel(folder)}
              >
                <FolderOpen className="w-4 h-4" />
                <FolderNumber number={folder.number} t={t} />
                {folder.name}
                {folder.id === currentFolderId && <span className="text-xs text-bambu-gray ml-auto">({t('fileManager.current')})</span>}
              </button>
            ))}
            {flatFolders.length === 0 && (
              <p className="text-sm text-bambu-gray text-center py-4">{t('fileManager.folderFilter.noMatches')}</p>
            )}
          </div>
          <div className="flex justify-end gap-2 pt-2 flex-shrink-0" data-testid="move-dialog-actions">
            <Button type="button" variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={() => onMove(targetFolder)}
              disabled={isLoading || !targetSelectable}
              title={targetSelectable ? undefined : t('fileManager.folderFilter.pickTarget')}
            >
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.move')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Link Folder Modal
interface LinkFolderModalProps {
  folder: LibraryFolderTree;
  onClose: () => void;
  onLink: (update: LibraryFolderUpdate) => void;
  isLoading: boolean;
  t: TFunction;
}

function LinkFolderModal({ folder, onClose, onLink, isLoading, t }: LinkFolderModalProps) {
  const [linkType, setLinkType] = useState<'project' | 'archive'>('project');
  const [selectedId, setSelectedId] = useState<number | null>(
    folder.project_id || folder.archive_id || null
  );

  // Initialize linkType based on existing link
  useState(() => {
    if (folder.archive_id) setLinkType('archive');
  });

  // Archived projects are left out, bar the one this folder is already linked
  // to -- dropping that one would leave the folder looking unlinked while the
  // link is still there (#2888).
  const { data: projects } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.getProjects(),
    select: (rows) =>
      assignableProjects([...rows].sort((a, b) => a.name.localeCompare(b.name)), folder.project_id),
  });

  const { data: archives } = useQuery({
    queryKey: ['archives-for-link'],
    queryFn: () => api.getArchives(undefined, undefined, 100),
  });

  const handleSave = () => {
    if (linkType === 'project') {
      onLink({
        project_id: selectedId,
        archive_id: 0, // Unlink archive
      });
    } else {
      onLink({
        project_id: 0, // Unlink project
        archive_id: selectedId,
      });
    }
  };

  const handleUnlink = () => {
    onLink({
      project_id: 0,
      archive_id: 0,
    });
  };

  const isLinked = folder.project_id || folder.archive_id;

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md max-h-[90vh] flex flex-col border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary flex items-center justify-between flex-shrink-0">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <Link2 className="w-5 h-5 text-bambu-green" />
            {t('fileManager.linkFolder')}
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded">
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-4 flex flex-col min-h-0 flex-1">
          <p className="text-sm text-bambu-gray">
            {t('fileManager.linkFolderDescription', { name: folderLabel(folder) })}
          </p>

          {/* Link type selector */}
          <div className="flex gap-2">
            <button
              onClick={() => { setLinkType('project'); setSelectedId(null); }}
              className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
                linkType === 'project'
                  ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                  : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'
              }`}
            >
              <Briefcase className="w-4 h-4" />
              {t('fileManager.project')}
            </button>
            <button
              onClick={() => { setLinkType('archive'); setSelectedId(null); }}
              className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
                linkType === 'archive'
                  ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                  : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'
              }`}
            >
              <ArchiveIcon className="w-4 h-4" />
              {t('fileManager.archive')}
            </button>
          </div>

          {/* Selection list */}
          <div className="flex-1 min-h-0 max-h-[min(60vh,32rem)] overflow-y-auto space-y-1 bg-bambu-dark rounded-lg p-2">
            {linkType === 'project' ? (
              projects && projects.length > 0 ? (
                projects.map((project) => (
                  <button
                    key={project.id}
                    onClick={() => setSelectedId(project.id)}
                    className={`w-full text-left px-3 py-2 rounded transition-colors flex items-center gap-2 ${
                      selectedId === project.id
                        ? 'bg-bambu-green/20 text-bambu-green'
                        : 'hover:bg-bambu-dark-tertiary text-white'
                    }`}
                  >
                    <div
                      className="w-3 h-3 rounded-full flex-shrink-0"
                      style={{ backgroundColor: project.color || '#00ae42' }}
                    />
                    <span className="truncate">{project.name}</span>
                  </button>
                ))
              ) : (
                <p className="text-sm text-bambu-gray text-center py-4">{t('fileManager.noProjectsFound')}</p>
              )
            ) : (
              archives && archives.length > 0 ? (
                archives.map((archive: Archive) => (
                  <button
                    key={archive.id}
                    onClick={() => setSelectedId(archive.id)}
                    className={`w-full text-left px-3 py-2 rounded transition-colors flex items-center gap-2 ${
                      selectedId === archive.id
                        ? 'bg-bambu-green/20 text-bambu-green'
                        : 'hover:bg-bambu-dark-tertiary text-white'
                    }`}
                  >
                    <FileBox className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                    <span className="truncate">{archive.print_name || archive.filename}</span>
                  </button>
                ))
              ) : (
                <p className="text-sm text-bambu-gray text-center py-4">{t('fileManager.noArchivesFound')}</p>
              )
            )}
          </div>
        </div>

        <div className="p-4 border-t border-bambu-dark-tertiary flex justify-between flex-shrink-0">
          {isLinked && (
            <Button variant="danger" onClick={handleUnlink} disabled={isLoading}>
              <Unlink className="w-4 h-4 mr-2" />
              {t('fileManager.unlink')}
            </Button>
          )}
          <div className={`flex gap-2 ${!isLinked ? 'ml-auto' : ''}`}>
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button onClick={handleSave} disabled={!selectedId || isLoading}>
              {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : t('fileManager.link')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Folder Tree Item
interface FolderTreeItemProps {
  folder: LibraryFolderTree;
  selectedFolderId: number | null;
  onSelect: (id: number | null) => void;
  onDownloadFolder: (folder: LibraryFolderTree) => void;
  onDelete: (id: number) => void;
  onLink: (folder: LibraryFolderTree) => void;
  onRename: (folder: LibraryFolderTree) => void;
  onCopyPath: (folder: LibraryFolderTree) => void;
  depth?: number;
  wrapNames?: boolean;
  defaultExpanded?: boolean;
  showModified?: boolean;
  hasPermission: (permission: Permission) => boolean;
  t: TFunction;
}

// Folder kebab: Rename / Link / Delete. Shared by the tree sidebar and the
// columns view so both offer exactly the same entries and permission gates.
// Rendered through `ContextMenu` (position: fixed, anchored to the button)
// rather than an absolutely positioned dropdown, because both hosts scroll —
// a dropdown inside an overflow-y-auto column gets clipped at its edge.
interface FolderActionsMenuProps {
  folder: LibraryFolderTree;
  onDownloadFolder: (folder: LibraryFolderTree) => void;
  onDelete: (id: number) => void;
  onLink: (folder: LibraryFolderTree) => void;
  onRename: (folder: LibraryFolderTree) => void;
  onCopyPath: (folder: LibraryFolderTree) => void;
  hasPermission: (permission: Permission) => boolean;
  // Hide the kebab until its `group` row is hovered or focused — only for
  // pointers that can hover (#2865). The menu is a DOM descendant, so the
  // wrapper stays opaque while it is open: browsers that don't focus a button
  // on click (Safari) would otherwise fade the open menu out with the row.
  revealOnHover?: boolean;
  tabIndex?: number;
  t: TFunction;
}

function FolderActionsMenu({ folder, onDownloadFolder, onDelete, onLink, onRename, onCopyPath, hasPermission, revealOnHover = false, tabIndex, t }: FolderActionsMenuProps) {
  const [menuAnchor, setMenuAnchor] = useState<{ x: number; y: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const closeMenu = () => {
    setMenuAnchor(null);
    // A menu entry activated by Enter unmounts with the menu; hand focus back
    // to the kebab rather than letting it drop to <body>. Focus that already
    // moved elsewhere (click-outside, the columns pane) is left alone.
    if (rootRef.current?.contains(document.activeElement)) buttonRef.current?.focus();
  };
  const hasChildren = folder.children.length > 0;
  const isLinked = folder.project_id || folder.archive_id;
  const isExternal = folder.is_external;
  // #1781: users with only library:delete_own may delete empty, unlinked,
  // non-external folders. The backend enforces the same rule and additionally
  // counts trashed files (invisible here), so a 403 can still come back.
  const canDeleteFolder =
    hasPermission('library:delete_all') ||
    (hasPermission('library:delete_own') && folder.file_count === 0 && !hasChildren && !isExternal && !isLinked);
  const deleteDisabledTooltip = canDeleteFolder
    ? undefined
    : hasPermission('library:delete_own') && !isExternal && !isLinked
      ? t('fileManager.onlyEmptyFoldersDeletable')
      : t('fileManager.noPermissionDeleteFolder');
  const canRename = hasPermission('library:update_all');

  const items: ContextMenuItem[] = [
    {
      label: t('common.rename'),
      icon: <Pencil className="w-3.5 h-3.5" />,
      onClick: () => onRename(folder),
      disabled: !canRename,
      title: !canRename ? t('fileManager.noPermissionRenameFolder') : undefined,
    },
    {
      label: isLinked ? t('fileManager.changeLink') : t('fileManager.linkTo'),
      icon: <Link2 className="w-3.5 h-3.5" />,
      onClick: () => onLink(folder),
      disabled: !canRename,
      title: !canRename ? t('fileManager.noPermissionLinkFolder') : undefined,
    },
    {
      // Subfolders ride along: a job folder is the drawing, the STEP and the
      // quote, and they are rarely all on one level.
      label: t('fileManager.downloadFolder'),
      icon: <Download className="w-3.5 h-3.5" />,
      onClick: () => onDownloadFolder(folder),
      disabled: !hasPermission('library:read') || folder.file_count === 0,
      title: folder.file_count === 0 ? t('fileManager.folderHasNoFiles') : undefined,
    },
    {
      // An external folder copies its real on-disk path — that is the string
      // the farm pastes into Explorer or the slicer, and the whole reason the
      // entry earns its place; a managed folder has only its library path.
      label: t('fileManager.copyPath'),
      icon: <Copy className="w-3.5 h-3.5" />,
      onClick: () => onCopyPath(folder),
    },
    {
      label: t('common.delete'),
      icon: <Trash2 className="w-3.5 h-3.5" />,
      onClick: () => onDelete(folder.id),
      danger: true,
      disabled: !canDeleteFolder,
      title: deleteDisabledTooltip,
    },
  ];

  return (
    <div
      ref={rootRef}
      className={`relative flex-shrink-0 flex items-center transition-opacity ${
        revealOnHover && !menuAnchor ? 'can-hover:opacity-0 group-hover:opacity-100 group-focus-within:opacity-100' : ''
      }`}
      data-folder-actions
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        // The menu has no arrow-key navigation; Up/Down mean "leave it".
        if (menuAnchor && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) closeMenu();
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        tabIndex={tabIndex}
        onClick={(e) => {
          if (menuAnchor) {
            closeMenu();
            return;
          }
          const rect = e.currentTarget.getBoundingClientRect();
          setMenuAnchor({ x: rect.left, y: rect.bottom + 4 });
        }}
        className="p-1 rounded hover:bg-bambu-dark-tertiary"
        title={t('common.actions')}
      >
        <MoreVertical className="w-3.5 h-3.5 text-bambu-gray" />
      </button>
      {menuAnchor && (
        <ContextMenu x={menuAnchor.x} y={menuAnchor.y} items={items} onClose={closeMenu} anchorRef={buttonRef} />
      )}
    </div>
  );
}

function FolderTreeItem({ folder, selectedFolderId, onSelect, onDownloadFolder, onDelete, onLink, onRename, onCopyPath, depth = 0, wrapNames = false, defaultExpanded = true, showModified = false, hasPermission, t }: FolderTreeItemProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const hasChildren = folder.children.length > 0;
  const isLinked = folder.project_id || folder.archive_id;
  const isExternal = folder.is_external;

  return (
    <div>
      <div
        className={`group flex items-center gap-1 px-2 py-1.5 rounded cursor-pointer transition-colors ${
          selectedFolderId === folder.id
            ? 'bg-bambu-green/20 text-bambu-green'
            : 'hover:bg-bambu-dark text-white'
        }`}
        style={{ paddingLeft: `${8 + depth * 12}px` }}
        onClick={() => onSelect(folder.id)}
      >
        {hasChildren ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className="p-0.5 hover:bg-bambu-dark-tertiary rounded"
          >
            <ChevronRight className={`w-3.5 h-3.5 transition-transform ${expanded ? 'rotate-90' : ''}`} />
          </button>
        ) : (
          <div className="w-4.5" />
        )}
        {isExternal ? (
          <FolderSymlink className="w-4 h-4 text-purple-600 dark:text-purple-400 flex-shrink-0" />
        ) : (
          <FolderOpen className="w-4 h-4 text-bambu-green flex-shrink-0" />
        )}
        <div className="flex-1 min-w-0">
          <span className="flex items-center gap-1.5 min-w-0 text-sm" title={folderLabel(folder)}>
            <FolderNumber number={folder.number} t={t} />
            <span className={wrapNames ? 'break-all' : 'truncate'}>{folder.name}</span>
          </span>
          {/* #2680 follow-up: the same toolbar toggle that shows dates on file
              cards also shows them here. This is `latest_activity_at` — the
              newest timestamp among the folder itself, its files and its
              subfolders (the value "sort by recent activity" orders on) — not
              the folder's own on-disk mtime, hence the distinct label. */}
          {showModified && folder.latest_activity_at && (
            <span className="mt-0.5 flex items-center gap-1 text-xs text-bambu-gray" title={t('fileManager.lastActivity')}>
              <CalendarClock className="w-3 h-3 flex-shrink-0" />
              <span className="truncate">{formatDate(folder.latest_activity_at)}</span>
            </span>
          )}
        </div>
        {/* Link indicator - clickable to change link */}
        {isLinked && (
          <button
            onClick={(e) => { e.stopPropagation(); onLink(folder); }}
            className="flex-shrink-0 flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400 hover:bg-blue-200 dark:hover:bg-blue-500/30 transition-colors"
            title={`${folder.project_name ? `Project: ${folder.project_name}` : `Archive: ${folder.archive_name}`} (click to change)`}
          >
            <Link2 className="w-3 h-3" />
            {folder.project_name ? (
              <Briefcase className="w-3 h-3" />
            ) : (
              <ArchiveIcon className="w-3 h-3" />
            )}
          </button>
        )}
        {/* Read-only indicator for external folders */}
        {isExternal && folder.external_readonly && (
          <span title={t('fileManager.readOnly')}>
            <Lock className="w-3 h-3 text-amber-600 dark:text-amber-400 flex-shrink-0" />
          </span>
        )}
        {folder.file_count > 0 && (
          <span className="flex-shrink-0 text-xs text-bambu-gray">{folder.file_count}</span>
        )}
        {/* Quick link button - always visible for unlinked folders */}
        {!isLinked && !isExternal && (
          <button
            onClick={(e) => { e.stopPropagation(); onLink(folder); }}
            className="flex-shrink-0 p-1 rounded hover:bg-bambu-dark-tertiary"
            title={t('fileManager.linkToProjectOrArchive')}
          >
            <Link2 className="w-3.5 h-3.5 text-bambu-gray hover:text-bambu-green" />
          </button>
        )}
        <FolderActionsMenu
          folder={folder}
          onDownloadFolder={onDownloadFolder}
          onDelete={onDelete}
          onLink={onLink}
          onRename={onRename}
          onCopyPath={onCopyPath}
          hasPermission={hasPermission}
          revealOnHover={!wrapNames}
          t={t}
        />
      </div>
      {hasChildren && expanded && (
        <div>
          {folder.children.map((child) => (
            <FolderTreeItem
              key={child.id}
              folder={child}
              selectedFolderId={selectedFolderId}
              onSelect={onSelect}
              onDownloadFolder={onDownloadFolder}
              onDelete={onDelete}
              onLink={onLink}
              onRename={onRename}
              onCopyPath={onCopyPath}
              depth={depth + 1}
              wrapNames={wrapNames}
              defaultExpanded={defaultExpanded}
              showModified={showModified}
              hasPermission={hasPermission}
              t={t}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// File Card
interface FileCardProps {
  file: LibraryFileListItem;
  isSelected: boolean;
  onSelect: (id: number) => void;
  onDelete: (id: number) => void;
  onDownload: (id: number) => void;
  onPrint?: (file: LibraryFileListItem) => void;
  onSlice?: (file: LibraryFileListItem) => void;
  onOpenInSlicer?: (file: LibraryFileListItem) => void;
  onRunPipeline?: (file: LibraryFileListItem) => void;
  useSlicerApi?: boolean;
  // Which slicer the desktop handoff targets. Decides whether an STL or STEP
  // gets a Slice action at all: Bambu Studio's protocol handler takes 3MF only
  // (#3029), so offering one there would only ever fail.
  desktopSlicer: SlicerType;
  canSlice?: boolean;
  onPreview?: (file: LibraryFileListItem) => void;
  onRename?: (file: LibraryFileListItem) => void;
  onDetails?: (file: LibraryFileListItem) => void;
  onGenerateThumbnail?: (file: LibraryFileListItem) => void;
  onCopyPath?: (file: LibraryFileListItem) => void;
  onTagClick?: (tagId: number) => void;
  thumbnailVersion?: number;
  hasPermission: (permission: Permission) => boolean;
  canModify: (resource: 'queue' | 'archives' | 'library', action: 'update' | 'delete' | 'reprint', createdById: number | null | undefined) => boolean;
  authEnabled: boolean;
  showModified: boolean;
  t: TFunction;
}

function FileCard({ file, isSelected, onSelect, onDelete, onDownload, onPrint, onSlice, onOpenInSlicer, onRunPipeline, useSlicerApi, desktopSlicer, canSlice, onPreview, onRename, onDetails, onGenerateThumbnail, onCopyPath, onTagClick, thumbnailVersion, hasPermission, canModify, authEnabled, showModified, t }: FileCardProps) {
  // Viewport coordinates rather than a flag, because the menu is rendered by
  // `ContextMenu` at `position: fixed` and anchored to the button (#2846). The
  // card it belongs to is only ~270px tall for a bare STL, which is shorter
  // than the seven-entry menu, so a menu positioned inside the card had its
  // top entry -- Slice -- cut off. The archive card menu works the same way.
  const [menuAnchor, setMenuAnchor] = useState<{ x: number; y: number } | null>(null);

  const canPreview3d = hasPermission('library:read');
  const canRename = canModify('library', 'update', file.created_by_id);
  const canDelete = canModify('library', 'delete', file.created_by_id);

  const menuItems: ContextMenuItem[] = [];
  if (onPrint && isSlicedLibraryFile(file)) {
    menuItems.push({
      label: t('common.print'),
      // The action stays visually distinct now that the menu component styles
      // its own labels; only the icon carries the accent.
      icon: <Printer className="w-4 h-4 text-bambu-green" />,
      onClick: () => onPrint(file),
      disabled: !hasPermission('queue:create'),
      title: !hasPermission('queue:create') ? t('fileManager.noPermissionAddToQueue') : undefined,
    });
  }
  if (isSliceableLibraryFile(file, !!useSlicerApi, desktopSlicer) && (useSlicerApi ? onSlice : onOpenInSlicer)) {
    menuItems.push({
      label: t('slice.action'),
      icon: useSlicerApi ? <Cog className="w-4 h-4" /> : <ExternalLink className="w-4 h-4" />,
      onClick: () => { if (useSlicerApi) onSlice?.(file); else onOpenInSlicer?.(file); },
      disabled: !canSlice,
      title: !canSlice ? (useSlicerApi ? t('fileManager.noPermissionSlice') : t('fileManager.noPermissionDownload')) : undefined,
    });
  }
  if (onRunPipeline && useSlicerApi && isSliceableLibraryFile(file, true, desktopSlicer)) {
    menuItems.push({
      label: t('library.runWithPipeline.actionLabel'),
      icon: <Play className="w-4 h-4" />,
      onClick: () => onRunPipeline(file),
      disabled: !hasPermission('pipelines:run'),
      title: !hasPermission('pipelines:run') ? t('library.runWithPipeline.noPermission') : undefined,
    });
  }
  if (onPreview && isPreviewableLibraryFile(file)) {
    const modelPreview = isModelPreview(file);
    menuItems.push({
      label: modelPreview ? t('fileManager.preview3d') : t('fileManager.preview.open'),
      icon: modelPreview ? <Box className="w-4 h-4" /> : documentPreviewIcon(file.file_type),
      onClick: () => onPreview(file),
      disabled: !canPreview3d,
      title: !canPreview3d ? t('fileManager.noPermissionPreview') : undefined,
    });
  }
  menuItems.push({
    label: t('common.download'),
    icon: <Download className="w-4 h-4" />,
    onClick: () => onDownload(file.id),
    disabled: !hasPermission('library:read'),
    title: !hasPermission('library:read') ? t('fileManager.noPermissionDownload') : undefined,
  });
  if (onRename) {
    menuItems.push({
      label: t('common.rename'),
      icon: <Pencil className="w-4 h-4" />,
      onClick: () => onRename(file),
      disabled: !canRename,
      title: !canRename ? t('fileManager.noPermissionRenameFile') : undefined,
    });
  }
  if (onDetails) {
    menuItems.push({
      label: t('fileManager.details.title'),
      icon: <Info className="w-4 h-4" />,
      onClick: () => onDetails(file),
      disabled: !canPreview3d,
      title: !canPreview3d ? t('fileManager.noPermissionPreview') : undefined,
    });
  }
  if (file.external_url) {
    menuItems.push({
      label: t('fileManager.details.openLink'),
      icon: <Globe className="w-4 h-4" />,
      onClick: () => window.open(file.external_url!, '_blank'),
    });
  }
  if (onGenerateThumbnail && hasServerThumbnail(file.file_type)) {
    menuItems.push({
      label: t('fileManager.generateThumbnail'),
      icon: <Image className="w-4 h-4" />,
      onClick: () => onGenerateThumbnail(file),
      disabled: !canRename,
      title: !canRename ? t('fileManager.noPermissionGenerateThumbnail') : undefined,
    });
  }
  if (onCopyPath) {
    menuItems.push({
      label: t('fileManager.copyPath'),
      icon: <Copy className="w-4 h-4" />,
      onClick: () => onCopyPath(file),
    });
  }
  menuItems.push({
    label: t('common.delete'),
    icon: <Trash2 className="w-4 h-4" />,
    onClick: () => onDelete(file.id),
    danger: true,
    disabled: !canDelete,
    title: !canDelete ? t('fileManager.noPermissionDeleteFile') : undefined,
  });

  return (
    <div
      className={`group relative bg-bambu-dark-secondary rounded-lg border transition-all cursor-pointer ${
        isSelected
          ? 'border-bambu-green ring-1 ring-bambu-green'
          : 'border-bambu-dark-tertiary hover:border-bambu-green/50'
      }`}
      onClick={() => onSelect(file.id)}
      // Double-click opens the preview (#2976). The two clicks that precede it
      // toggle the selection twice, so the selection is left as it was.
      onDoubleClick={() => onPreview?.(file)}
    >
      {/* Thumbnail */}
      <div className="aspect-square bg-bambu-dark flex items-center justify-center overflow-hidden rounded-t-lg">
        {file.thumbnail_path ? (
          <img
            src={`${api.getLibraryFileThumbnailUrl(file.id)}${thumbnailVersion ? ((api.getLibraryFileThumbnailUrl(file.id).includes('?') ? '&' : '?') + `v=${thumbnailVersion}`) : ''}`}
            alt={file.filename}
            className="w-full h-full object-cover"
          />
        ) : (
          <FileTypePlaceholderIcon fileType={file.file_type} className="w-12 h-12 text-bambu-gray/30" />
        )}
        {/* File type badge */}
        <div className={`absolute top-2 right-2 text-xs px-1.5 py-0.5 rounded font-medium ${fileTypeBadgeClass(file.file_type, 'solid')}`}>
          {file.file_type.toUpperCase()}
        </div>
      </div>

      {/* Info */}
      <div className="p-3">
        <h3 className="text-sm font-medium text-white truncate" title={file.print_name || file.filename}>
          {file.print_name || file.filename}
        </h3>
        <div className="flex items-center gap-3 mt-1 text-xs text-bambu-gray">
          <span>{formatFileSize(file.file_size)}</span>
          {file.print_time_seconds && (
            <span className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {formatDuration(file.print_time_seconds)}
            </span>
          )}
        </div>
        {file.sliced_for_model && (
          <div className="mt-1 text-xs text-bambu-gray flex items-center gap-1">
            <Printer className="w-3 h-3" />
            {file.sliced_for_model}
          </div>
        )}
        {/* Counts the whole group, including members in other folders (#671 /
            #2570) — printing this file will offer all of them. */}
        {(file.variant_count ?? 0) > 1 && (
          <div className="mt-1 text-xs text-bambu-green flex items-center gap-1">
            <Layers className="w-3 h-3" />
            {t('fileManager.variants.badge', { count: file.variant_count })}
          </div>
        )}
        {file.print_count > 0 && (
          <div className="mt-1 text-xs text-bambu-green">
            {t('fileManager.printedCount', { count: file.print_count })}
          </div>
        )}
        {/* Metadata indicators (#3077): link, notes, photos. The link opens in
            a new tab like the archive card's globe; the others open Details. */}
        {(file.external_url || file.has_notes || (file.photo_count ?? 0) > 0) && (
          <div className="mt-1 flex items-center gap-2 text-xs text-bambu-gray" {...stopRowActivation}>
            {file.external_url && (
              <a
                href={file.external_url}
                target="_blank"
                rel="noopener noreferrer"
                className="p-0.5 rounded hover:text-bambu-green"
                title={t('fileManager.details.openLink')}
                aria-label={t('fileManager.details.openLink')}
              >
                <Globe className="w-3.5 h-3.5" />
              </a>
            )}
            {file.has_notes && (
              <button
                type="button"
                onClick={() => onDetails?.(file)}
                className="p-0.5 rounded hover:text-bambu-green"
                title={t('fileManager.details.hasNotes')}
                aria-label={t('fileManager.details.hasNotes')}
              >
                <StickyNote className="w-3.5 h-3.5" />
              </button>
            )}
            {(file.photo_count ?? 0) > 0 && (
              <button
                type="button"
                onClick={() => onDetails?.(file)}
                className="flex items-center gap-0.5 p-0.5 rounded hover:text-bambu-green"
                title={t('fileManager.details.photoCount', { count: file.photo_count })}
                aria-label={t('fileManager.details.photoCount', { count: file.photo_count })}
              >
                <Camera className="w-3.5 h-3.5" />
                <span>{file.photo_count}</span>
              </button>
            )}
          </div>
        )}
        {authEnabled && file.created_by_username && (
          <div className="mt-1 text-xs text-bambu-gray flex items-center gap-1">
            <User className="w-3 h-3" />
            {file.created_by_username}
          </div>
        )}
        {/* #2680: last-modified date, toggled from the toolbar. Uses the real
            on-disk mtime when known, else the DB created_at. */}
        {showModified && (
          <div className="mt-1 text-xs text-bambu-gray flex items-center gap-1" title={t('fileManager.lastModified')}>
            <CalendarClock className="w-3 h-3" />
            {formatDate(file.fs_modified_at ?? file.created_at)}
          </div>
        )}
        {(file.tags?.length ?? 0) > 0 && (
          <div className="mt-2 flex flex-wrap gap-1" {...stopRowActivation}>
            {file.tags!.map((tg) => (
              <button
                key={tg.id}
                type="button"
                onClick={() => onTagClick?.(tg.id)}
                className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] bg-bambu-green/10 text-bambu-green hover:bg-bambu-green/20 transition-colors max-w-full"
                title={tg.name}
              >
                <TagIcon className="w-2.5 h-2.5 flex-shrink-0" />
                <span className="truncate">{tg.name}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Actions - hover-revealed with a mouse, always there without one (#2865) */}
      <div className="absolute bottom-2 right-2 transition-opacity can-hover:opacity-0 group-hover:opacity-100 group-focus-within:opacity-100" {...stopRowActivation}>
        <button
          onClick={(e) => {
            // No open/close toggle: the menu's own outside-mousedown handler
            // has already dismissed it by the time this click lands.
            const rect = e.currentTarget.getBoundingClientRect();
            setMenuAnchor({ x: rect.left, y: rect.bottom + 4 });
          }}
          className="p-1.5 rounded bg-bambu-dark-secondary/90 hover:bg-bambu-dark-tertiary"
        >
          <MoreVertical className="w-4 h-4 text-bambu-gray" />
        </button>
        {menuAnchor && (
          <ContextMenu x={menuAnchor.x} y={menuAnchor.y} items={menuItems} onClose={() => setMenuAnchor(null)} />
        )}
      </div>

      {/* Selection checkbox - hover-revealed with a mouse, always there without one (#2865) */}
      <div className={`absolute top-2 left-2 w-5 h-5 rounded border-2 flex items-center justify-center transition-all ${
        isSelected
          ? 'bg-bambu-green border-bambu-green'
          : 'border-white/30 bg-black/30 can-hover:opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
      }`}>
        {isSelected && <div className="w-2 h-2 bg-white rounded-sm" />}
      </div>
    </div>
  );
}

// Per-file icon strip: Print, Slice, Run with pipeline, Preview (3D or
// document), Download, Details, Rename, Generate thumbnail, Delete. The list row and the columns view render
// the same strip so the two stay identical in order, icons and gating; the
// grid card packs the same actions into its kebab menu instead.
interface FileActionStripProps {
  file: LibraryFileListItem;
  onPrint: (file: LibraryFileListItem) => void;
  onSlice: (file: LibraryFileListItem) => void;
  onOpenInSlicer: (file: LibraryFileListItem) => void;
  onRunPipeline: (file: LibraryFileListItem) => void;
  useSlicerApi: boolean;
  desktopSlicer: SlicerType;
  canSlice: boolean;
  onPreview: (file: LibraryFileListItem) => void;
  onDetails: (file: LibraryFileListItem) => void;
  onDownload: (id: number) => void;
  onRename: (file: LibraryFileListItem) => void;
  onGenerateThumbnail: (file: LibraryFileListItem) => void;
  onCopyPath: (file: LibraryFileListItem) => void;
  thumbnailPending: boolean;
  onDelete: (id: number) => void;
  hasPermission: (permission: Permission) => boolean;
  canModify: (resource: 'queue' | 'archives' | 'library', action: 'update' | 'delete' | 'reprint', createdById: number | null | undefined) => boolean;
  // Roving tabindex for the columns view: only the focused row's buttons take
  // part in the Tab order, so Tab from the pane lands on that row's actions.
  tabIndex?: number;
  t: TFunction;
}

function FileActionStrip({ file, onPrint, onSlice, onOpenInSlicer, onRunPipeline, useSlicerApi, desktopSlicer, canSlice, onPreview, onDetails, onDownload, onRename, onGenerateThumbnail, onCopyPath, thumbnailPending, onDelete, hasPermission, canModify, tabIndex, t }: FileActionStripProps) {
  const canRename = canModify('library', 'update', file.created_by_id);
  const canDelete = canModify('library', 'delete', file.created_by_id);
  return (
    <div
      className="flex items-center gap-1"
      data-file-actions
      onClick={(e) => e.stopPropagation()}
      // The columns row opens the viewer on double-click; two clicks on an
      // icon must not bubble up as one.
      onDoubleClick={(e) => e.stopPropagation()}
    >
      {isSlicedLibraryFile(file) && (
        <button
          tabIndex={tabIndex}
          onClick={() => hasPermission('queue:create') && onPrint(file)}
          className={`p-1.5 rounded transition-colors ${
            hasPermission('queue:create')
              ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          title={hasPermission('queue:create') ? t('common.print') : t('fileManager.noPermissionAddToQueue')}
          disabled={!hasPermission('queue:create')}
        >
          <Printer className="w-4 h-4" />
        </button>
      )}
      {isSliceableLibraryFile(file, useSlicerApi, desktopSlicer) && (
        <button
          tabIndex={tabIndex}
          onClick={() => {
            if (!canSlice) return;
            (useSlicerApi ? onSlice : onOpenInSlicer)(file);
          }}
          className={`p-1.5 rounded transition-colors ${
            canSlice
              ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          title={canSlice ? t('slice.action') : (useSlicerApi ? t('fileManager.noPermissionSlice') : t('fileManager.noPermissionDownload'))}
          disabled={!canSlice}
        >
          {useSlicerApi ? <Cog className="w-4 h-4" /> : <ExternalLink className="w-4 h-4" />}
        </button>
      )}
      {useSlicerApi && isSliceableLibraryFile(file, true, desktopSlicer) && (
        <button
          tabIndex={tabIndex}
          onClick={() => hasPermission('pipelines:run') && onRunPipeline(file)}
          className={`p-1.5 rounded transition-colors ${
            hasPermission('pipelines:run')
              ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          title={hasPermission('pipelines:run') ? t('library.runWithPipeline.actionLabel') : t('library.runWithPipeline.noPermission')}
          disabled={!hasPermission('pipelines:run')}
        >
          <Play className="w-4 h-4" />
        </button>
      )}
      {isModelPreview(file) && (
        <button
          tabIndex={tabIndex}
          onClick={() => hasPermission('library:read') && onPreview(file)}
          className={`p-1.5 rounded transition-colors ${
            hasPermission('library:read')
              ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          title={hasPermission('library:read') ? t('fileManager.preview3d') : t('fileManager.noPermissionPreview')}
          disabled={!hasPermission('library:read')}
        >
          <Box className="w-4 h-4" />
        </button>
      )}
      {!isModelPreview(file) && isPreviewableLibraryFile(file) && (
        <button
          tabIndex={tabIndex}
          onClick={() => hasPermission('library:read') && onPreview(file)}
          className={`p-1.5 rounded transition-colors ${
            hasPermission('library:read')
              ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          title={hasPermission('library:read') ? t('fileManager.preview.open') : t('fileManager.noPermissionPreview')}
          disabled={!hasPermission('library:read')}
        >
          {documentPreviewIcon(file.file_type)}
        </button>
      )}
      <button
        tabIndex={tabIndex}
        onClick={() => hasPermission('library:read') && onDownload(file.id)}
        className={`p-1.5 rounded transition-colors ${
          hasPermission('library:read')
            ? 'hover:bg-bambu-dark text-bambu-gray hover:text-white'
            : 'text-bambu-gray/50 cursor-not-allowed'
        }`}
        title={hasPermission('library:read') ? t('common.download') : t('fileManager.noPermissionDownload')}
        disabled={!hasPermission('library:read')}
      >
        <Download className="w-4 h-4" />
      </button>
      <button
        tabIndex={tabIndex}
        onClick={() => hasPermission('library:read') && onDetails(file)}
        className={`p-1.5 rounded transition-colors ${
          hasPermission('library:read')
            ? 'hover:bg-bambu-dark text-bambu-gray hover:text-white'
            : 'text-bambu-gray/50 cursor-not-allowed'
        }`}
        title={hasPermission('library:read') ? t('fileManager.details.title') : t('fileManager.noPermissionPreview')}
        disabled={!hasPermission('library:read')}
      >
        <Info className="w-4 h-4" />
      </button>
      <button
        tabIndex={tabIndex}
        onClick={() => canRename && onRename(file)}
        className={`p-1.5 rounded transition-colors ${
          canRename
            ? 'hover:bg-bambu-dark text-bambu-gray hover:text-white'
            : 'text-bambu-gray/50 cursor-not-allowed'
        }`}
        title={canRename ? t('common.rename') : t('fileManager.noPermissionRenameFile')}
        disabled={!canRename}
      >
        <Pencil className="w-4 h-4" />
      </button>
      {hasServerThumbnail(file.file_type) && (
        <button
          tabIndex={tabIndex}
          onClick={() => canRename && onGenerateThumbnail(file)}
          className={`p-1.5 rounded transition-colors ${
            canRename
              ? 'hover:bg-bambu-dark text-bambu-gray hover:text-bambu-green'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          title={canRename ? t('fileManager.generateThumbnail') : t('fileManager.noPermissionGenerateThumbnail')}
          disabled={thumbnailPending || !canRename}
        >
          <Image className="w-4 h-4" />
        </button>
      )}
      <button
        tabIndex={tabIndex}
        onClick={() => onCopyPath(file)}
        className="p-1.5 rounded transition-colors hover:bg-bambu-dark text-bambu-gray hover:text-white"
        title={t('fileManager.copyPath')}
      >
        <Copy className="w-4 h-4" />
      </button>
      <button
        tabIndex={tabIndex}
        onClick={() => canDelete && onDelete(file.id)}
        className={`p-1.5 rounded transition-colors ${
          canDelete
            ? 'hover:bg-bambu-dark text-bambu-gray hover:text-red-700 dark:hover:text-red-400'
            : 'text-bambu-gray/50 cursor-not-allowed'
        }`}
        title={canDelete ? t('common.delete') : t('fileManager.noPermissionDeleteFile')}
        disabled={!canDelete}
      >
        <Trash2 className="w-4 h-4" />
      </button>
    </div>
  );
}

// One row of a Miller column's file list. Extracted so every column renders
// its files with the same markup, action strip and double-click behaviour the
// selected folder's pane has always used.
interface ColumnFileRowProps {
  file: LibraryFileListItem;
  isSelected: boolean;
  isFocused: boolean;
  showModified: boolean;
  thumbnailVersion?: number;
  onSelect: (file: LibraryFileListItem) => void;
  onOpen: (file: LibraryFileListItem) => void;
  actionProps: Omit<FileActionStripProps, 'file' | 'tabIndex'>;
  t: TFunction;
}

function ColumnFileRow({ file, isSelected, isFocused, showModified, thumbnailVersion, onSelect, onOpen, actionProps, t }: ColumnFileRowProps) {
  const thumbnailUrl = api.getLibraryFileThumbnailUrl(file.id);
  return (
    <div
      data-file-id={file.id}
      onClick={() => onSelect(file)}
      onDoubleClick={() => onOpen(file)}
      className={`group flex items-center gap-3 px-3 py-2 cursor-pointer transition-colors ${
        isSelected ? 'bg-bambu-green/10' : 'hover:bg-bambu-dark'
      } ${isFocused ? 'ring-1 ring-inset ring-bambu-green/60' : ''}`}
      title={file.print_name || file.filename}
    >
      <div className={`w-4 h-4 rounded border-2 flex-shrink-0 flex items-center justify-center ${
        isSelected ? 'bg-bambu-green border-bambu-green' : 'border-bambu-gray/50'
      }`}>
        {isSelected && <div className="w-1.5 h-1.5 bg-white rounded-sm" />}
      </div>
      <div className="w-10 h-10 rounded bg-bambu-dark flex-shrink-0 overflow-hidden flex items-center justify-center">
        {file.thumbnail_path ? (
          <img
            src={`${thumbnailUrl}${thumbnailVersion ? ((thumbnailUrl.includes('?') ? '&' : '?') + `v=${thumbnailVersion}`) : ''}`}
            alt=""
            className="w-full h-full object-cover"
          />
        ) : (
          <FileTypePlaceholderIcon fileType={file.file_type} className="w-5 h-5 text-bambu-gray/50" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-white truncate">{file.print_name || file.filename}</div>
        <div className="mt-0.5 flex items-center gap-2 text-xs text-bambu-gray">
          <span className={`px-1.5 py-px rounded font-medium ${fileTypeBadgeClass(file.file_type)}`}>
            {file.file_type.toUpperCase()}
          </span>
          <span>{formatFileSize(file.file_size)}</span>
          {file.print_count > 0 && <span>{file.print_count}x</span>}
          {showModified && (
            <span className="flex items-center gap-1 truncate" title={t('fileManager.lastModified')}>
              <CalendarClock className="w-3 h-3 flex-shrink-0" />
              {formatDate(file.fs_modified_at ?? file.created_at)}
            </span>
          )}
        </div>
      </div>
      {/* Same strip as the list row. Hover-revealed with a mouse, always there
          without one (#2865) and on the focused / selected row so the keyboard
          can reach it. */}
      <div
        className={`flex-shrink-0 transition-opacity ${
          isFocused || isSelected ? '' : 'can-hover:opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
        }`}
      >
        <FileActionStrip file={file} {...actionProps} tabIndex={isFocused ? 0 : -1} />
      </div>
    </div>
  );
}

// Resolve a typed path — `Kunden/RAFI/N1125035` — against the folder tree.
// Explorer's rules: either separator, a leading or trailing one, and case does
// not matter. Returns the folder's id, null for the root (an empty path), and
// undefined when no such folder exists.
function resolveTypedPath(tree: LibraryFolderTree[], typed: string): number | null | undefined {
  const segments = typed.split(/[\\/]+/).map((part) => part.trim()).filter(Boolean);
  if (segments.length === 0) return null;
  let level = tree;
  let found: LibraryFolderTree | undefined;
  for (const segment of segments) {
    found = level.find((folder) => folder.name.toLowerCase() === segment.toLowerCase());
    if (!found) return undefined;
    level = found.children;
  }
  return found!.id;
}

// Both of the path bar's menus hang off a button inside the crumb row, and
// that row clips what leaves it (`overflow-hidden`, so a long chain can never
// push the pane wider). An in-tree popover is clipped away by exactly that: it
// opens below a one-line row, i.e. entirely outside the clip rect, and z-index
// does not escape an overflow clip — the list is in the DOM, visible to a test
// that does no layout, and painted nowhere. So it goes through a portal to
// <body>, pinned under its button with `position: fixed`, the way the project
// cards' hover preview escapes their rounded-corner clip (#1155).
const PATH_MENU_WIDTH = 288; // max-w-[18rem]: the widest the list gets
const PATH_MENU_GAP = 4;
const PATH_MENU_EDGE = 8;

function pathMenuPosition(anchor: HTMLElement | null) {
  const rect = anchor?.getBoundingClientRect();
  if (!rect) return { left: PATH_MENU_EDGE, top: PATH_MENU_GAP };
  // Keep the list on screen when the crumb it hangs off sits near the edge.
  const room = window.innerWidth - PATH_MENU_WIDTH - PATH_MENU_EDGE;
  return { left: Math.max(PATH_MENU_EDGE, Math.min(rect.left, room)), top: rect.bottom + PATH_MENU_GAP };
}

interface PathBarFolderMenuProps {
  anchorRef: React.RefObject<HTMLElement | null>;
  menuRef: React.RefObject<HTMLDivElement | null>;
  folders: LibraryFolderTree[];
  onSelect: (id: number) => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void;
  t: TFunction;
}

function PathBarFolderMenu({ anchorRef, menuRef, folders, onSelect, onKeyDown, t }: PathBarFolderMenuProps) {
  const [pos, setPos] = useState(() => pathMenuPosition(anchorRef.current));

  // Viewport coordinates go stale as soon as anything scrolls or resizes; the
  // capture phase catches a scrolling pane as well as the window itself.
  useEffect(() => {
    const place = () => setPos(pathMenuPosition(anchorRef.current));
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [anchorRef]);

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      onKeyDown={onKeyDown}
      style={{ left: pos.left, top: pos.top }}
      className="fixed z-[60] min-w-[10rem] max-w-[18rem] max-h-72 overflow-y-auto py-1 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary shadow-xl"
    >
      {folders.map((folder) => (
        <button
          key={folder.id}
          type="button"
          role="menuitem"
          onClick={() => onSelect(folder.id)}
          aria-label={folderLabel(folder)}
          title={folderLabel(folder)}
          className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-sm text-white hover:bg-bambu-dark transition-colors"
        >
          <FolderOpen className="w-3.5 h-3.5 flex-shrink-0 text-bambu-green" />
          <FolderNumber number={folder.number} t={t} />
          <span className="truncate">{folder.name}</span>
        </button>
      ))}
    </div>,
    document.body,
  );
}

// The `›` between two crumbs, which also lists what sits inside the crumb to
// its left — Explorer's sideways step, from a deep folder straight to one of
// its uncles without walking up first. With nothing to list it stays the plain
// glyph it was.
interface PathSeparatorProps {
  folders: LibraryFolderTree[];
  onSelectFolder: (id: number) => void;
  t: TFunction;
}

function PathSeparator({ folders, onSelectFolder, t }: PathSeparatorProps) {
  const [open, setOpen] = useState(false);
  // A span, not a div: the separator renders inside a crumb's <span>, which
  // may only hold phrasing content. The list itself is portalled out.
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e: MouseEvent) => {
      // The list hangs off <body>, so "inside" is either half of the pair —
      // without the menu half, the mousedown on an entry would close the list
      // before its own click could fire.
      const target = e.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open]);

  // Focus the list as it opens, so Enter and then the arrow keys walk it
  // without a second Tab.
  useEffect(() => {
    if (open) menuRef.current?.querySelector('button')?.focus();
  }, [open]);

  const glyph = <ChevronRightIcon className="w-3.5 h-3.5 flex-shrink-0 text-bambu-gray/60" aria-hidden="true" />;
  if (folders.length === 0) return glyph;

  const walk = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
      return;
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const items = Array.from(menuRef.current?.querySelectorAll('button') ?? []);
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.key === 'ArrowDown' ? at + 1 : at - 1;
    items[(next + items.length) % items.length]?.focus();
  };

  return (
    <span className="inline-flex flex-shrink-0">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('fileManager.pathBar.siblings')}
        title={t('fileManager.pathBar.siblings')}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center px-0.5 py-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
      >
        {glyph}
      </button>
      {open && (
        <PathBarFolderMenu
          anchorRef={triggerRef}
          menuRef={menuRef}
          folders={folders}
          onSelect={(id) => {
            setOpen(false);
            onSelectFolder(id);
          }}
          onKeyDown={walk}
          t={t}
        />
      )}
    </span>
  );
}

// Explorer-style path bar: the chain from the library root down to the
// selected folder, above the file list in every view mode. Every ancestor is
// a button that selects it; the current folder is plain text. The bar must
// never wrap or push the pane wider, so a long chain keeps the root and the
// last two crumbs and folds the rest into a menu. Clicking past the last crumb
// — or F2 anywhere in the bar — swaps the crumbs for the path as text, the
// other half of what Explorer does here.
interface PathBarProps {
  rootLabel: string;
  rootIsExternal: boolean;
  path: LibraryFolderTree[];
  // The bucket's top-level folders: what a typed path resolves against, and
  // what the separator after the root lists.
  tree: LibraryFolderTree[];
  // A location inside the root that is not a folder — the root's "No folder"
  // listing, or its "Recent" start page. Shown as the trailing crumb, which
  // turns the root crumb into the way back out.
  leafLabel?: string;
  onSelectRoot: () => void;
  onSelectFolder: (id: number) => void;
  t: TFunction;
}

function PathBar({
  rootLabel,
  rootIsExternal,
  path,
  tree,
  leafLabel,
  onSelectRoot,
  onSelectFolder,
  t,
}: PathBarProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState(false);
  const [typed, setTyped] = useState('');
  const [unknown, setUnknown] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const editAffordanceRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);

  useEffect(() => {
    if (!menuOpen) return;
    const handlePointerDown = (e: MouseEvent) => {
      // Both halves: the list itself hangs off <body>, not off the trigger.
      const target = e.target as Node;
      if (!menuTriggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [menuOpen]);

  // Leaving the box must not throw the user's place away. The input unmounts
  // with the whole bar, so without this the browser falls back to <body> and
  // the next Tab restarts at the top of the page; Escape and a resolved path
  // both put focus back on the affordance that opened the box.
  useEffect(() => {
    if (editing || !returnFocus.current) return;
    returnFocus.current = false;
    editAffordanceRef.current?.focus();
  }, [editing]);

  const pathText = path.map((folder) => folder.name).join('/');

  // Selected on open, so Ctrl+C takes the path and typing replaces it — the
  // copy route for people who reach for the keyboard rather than the button.
  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const startEditing = () => {
    setTyped(pathText);
    setUnknown(false);
    setEditing(true);
  };

  // Closing by keyboard, which owes the keyboard its place back. A blur does
  // not: focus has already gone somewhere the user picked.
  const stopEditing = () => {
    returnFocus.current = true;
    setEditing(false);
  };

  const submit = () => {
    const resolved = resolveTypedPath(tree, typed);
    if (resolved === undefined) {
      // Nothing matched: keep what was typed on screen and say so. Navigating
      // anyway or clearing the box would both throw away the correction the
      // user is one character away from making.
      setUnknown(true);
      return;
    }
    stopEditing();
    if (resolved === null) onSelectRoot();
    else onSelectFolder(resolved);
  };

  if (editing) {
    return (
      <div data-testid="library-path-editor" className="mb-3 min-w-0">
        <label htmlFor="library-path-input" className="sr-only">
          {t('fileManager.pathBar.editPath')}
        </label>
        <input
          id="library-path-input"
          ref={inputRef}
          type="text"
          autoFocus
          value={typed}
          aria-invalid={unknown}
          aria-describedby={unknown ? 'library-path-error' : undefined}
          onChange={(e) => {
            setTyped(e.target.value);
            setUnknown(false);
          }}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              submit();
            } else if (e.key === 'Escape') {
              e.preventDefault();
              stopEditing();
            }
          }}
          className={`w-full px-2 py-1 rounded bg-bambu-dark border text-sm text-white focus:outline-none ${
            unknown ? 'border-red-500 focus:border-red-500' : 'border-bambu-dark-tertiary focus:border-bambu-green'
          }`}
        />
        {unknown && (
          <p id="library-path-error" role="alert" className="mt-1 text-xs text-red-400">
            {t('fileManager.pathBar.noSuchFolder')}
          </p>
        )}
      </div>
    );
  }

  // Keep the first element and the last two; everything between them moves
  // into the ellipsis menu. Root plus three folders still fits on any pane we
  // target, so folding only starts beyond that — collapsing a chain that fits
  // hides an ancestor for nothing.
  const collapsed = path.length > 3;
  const hidden = collapsed ? path.slice(0, path.length - 2) : [];
  const visible = collapsed ? path.slice(path.length - 2) : path;
  // What the separator in front of the crumb at full-path index `index` lists:
  // the contents of the crumb to its left, the bucket's top level at index 0.
  const levelBefore = (index: number) => (index <= 0 ? tree : path[index - 1].children);
  const crumbClass = 'flex items-center gap-1.5 min-w-0 px-1.5 py-1 rounded transition-colors';
  const rootIcon = rootIsExternal ? (
    <FolderSymlink className="w-4 h-4 flex-shrink-0 text-purple-600 dark:text-purple-400" />
  ) : (
    <FileBox className="w-4 h-4 flex-shrink-0 text-bambu-green" />
  );

  return (
    <nav
      aria-label={t('fileManager.pathBar.label')}
      data-testid="library-path-bar"
      onKeyDown={(e) => {
        if (e.key === 'F2') {
          e.preventDefault();
          startEditing();
        }
      }}
      className="flex items-center gap-0.5 mb-3 min-w-0 overflow-hidden whitespace-nowrap text-sm"
    >
      {path.length === 0 && !leafLabel ? (
        <span aria-current="page" className={`${crumbClass} text-white font-medium`}>
          {rootIcon}
          <span className="truncate">{rootLabel}</span>
        </span>
      ) : (
        <button
          type="button"
          onClick={onSelectRoot}
          aria-label={rootLabel}
          title={rootLabel}
          className={`${crumbClass} text-bambu-gray hover:text-white hover:bg-bambu-dark`}
        >
          {rootIcon}
          <span className="truncate">{rootLabel}</span>
        </button>
      )}
      {collapsed && (
        <>
          <PathSeparator folders={levelBefore(0)} onSelectFolder={onSelectFolder} t={t} />
          <div className="flex-shrink-0">
            <button
              ref={menuTriggerRef}
              type="button"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label={t('fileManager.pathBar.showHidden')}
              title={t('fileManager.pathBar.showHidden')}
              onClick={() => setMenuOpen((open) => !open)}
              className="flex items-center px-1.5 py-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
            >
              <MoreHorizontal className="w-4 h-4" />
            </button>
            {menuOpen && (
              <PathBarFolderMenu
                anchorRef={menuTriggerRef}
                menuRef={menuRef}
                folders={hidden}
                onSelect={(id) => {
                  setMenuOpen(false);
                  onSelectFolder(id);
                }}
                t={t}
              />
            )}
          </div>
        </>
      )}
      {visible.map((folder, i) => {
        const isCurrent = !leafLabel && i === visible.length - 1;
        const fullIndex = collapsed ? path.length - 2 + i : i;
        return (
          <span key={folder.id} className="flex items-center gap-0.5 min-w-0">
            <PathSeparator folders={levelBefore(fullIndex)} onSelectFolder={onSelectFolder} t={t} />
            {isCurrent ? (
              <span aria-current="page" title={folderLabel(folder)} className={`${crumbClass} text-white font-medium`}>
                <FolderNumber number={folder.number} t={t} />
                <span className="truncate">{folder.name}</span>
              </span>
            ) : (
              <button
                type="button"
                onClick={() => onSelectFolder(folder.id)}
                aria-label={folderLabel(folder)}
                title={folderLabel(folder)}
                className={`${crumbClass} text-bambu-gray hover:text-white hover:bg-bambu-dark`}
              >
                <FolderNumber number={folder.number} t={t} />
                <span className="truncate">{folder.name}</span>
              </button>
            )}
          </span>
        );
      })}
      {leafLabel && (
        <span className="flex items-center gap-0.5 min-w-0">
          <PathSeparator folders={levelBefore(path.length)} onSelectFolder={onSelectFolder} t={t} />
          <span aria-current="page" title={leafLabel} className={`${crumbClass} text-white font-medium`}>
            <span className="truncate">{leafLabel}</span>
          </span>
        </span>
      )}
      {/* The empty stretch past the last crumb, the way Explorer does it:
          click it (or press Enter on it, or F2 anywhere in the bar) and the
          crumbs become the path as text. */}
      <button
        ref={editAffordanceRef}
        type="button"
        onClick={startEditing}
        aria-label={t('fileManager.pathBar.editPath')}
        title={t('fileManager.pathBar.editPath')}
        className="flex-1 self-stretch min-w-[1.5rem] rounded cursor-text hover:bg-bambu-dark/50 transition-colors"
      />
      {/* The chain as text, for Explorer / the slicer / a mail. The root has
          no path, so at the root there is nothing to copy. */}
      {path.length > 0 && (
        <CopyButton
          value={pathText}
          titleKey="fileManager.copyPath"
          copiedTitleKey="fileManager.toast.pathCopied"
          className="ml-1 flex-shrink-0 p-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
        />
      )}
    </nav>
  );
}

// Folder navigation for the content area, rendered when the folder sidebar is
// switched off. The path bar only ever walks up, and neither the grid nor the
// list draws a folder, so without this the hidden sidebar takes the top-level
// buckets and every way down with it.
interface ContentFolderNavProps {
  folders: LibraryFolderTree[];
  variant: 'grid' | 'list';
  showBuckets: boolean;
  bucketIsExternal: boolean;
  atRoot: boolean;
  onSelectFolder: (id: number) => void;
  onSelectBucket: (view: 'internal' | 'external') => void;
  onDownloadFolder: (folder: LibraryFolderTree) => void;
  onCopyPath: (folder: LibraryFolderTree) => void;
  onDeleteFolder: (id: number) => void;
  onLinkFolder: (folder: LibraryFolderTree) => void;
  onRenameFolder: (folder: LibraryFolderTree) => void;
  hasPermission: (permission: Permission) => boolean;
  t: TFunction;
}

function ContentFolderNav({
  folders,
  variant,
  showBuckets,
  bucketIsExternal,
  atRoot,
  onSelectFolder,
  onSelectBucket,
  onDownloadFolder,
  onCopyPath,
  onDeleteFolder,
  onLinkFolder,
  onRenameFolder,
  hasPermission,
  t,
}: ContentFolderNavProps) {
  if (!showBuckets && folders.length === 0) return null;

  const folderIcon = (folder: LibraryFolderTree) =>
    folder.is_external ? (
      <FolderSymlink className="w-4 h-4 flex-shrink-0 text-purple-600 dark:text-purple-400" />
    ) : (
      <FolderOpen className="w-4 h-4 flex-shrink-0 text-bambu-green" />
    );

  const bucketClass = (active: boolean) =>
    `inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-sm border transition-colors ${
      active
        ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/40'
        : 'bg-bambu-dark-secondary text-bambu-gray border-bambu-dark-tertiary hover:text-white hover:border-bambu-green/40'
    }`;

  return (
    // Matches the sidebar's own `hidden lg:flex`: below lg the folders are a
    // <select> that is always on screen, and the toggle does not exist there.
    <nav
      aria-label={t('fileManager.folders')}
      data-testid="content-folder-nav"
      className="hidden lg:block mb-4"
    >
      {showBuckets && (
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <button
            type="button"
            onClick={() => onSelectBucket('internal')}
            aria-pressed={atRoot && !bucketIsExternal}
            className={bucketClass(atRoot && !bucketIsExternal)}
          >
            <FileBox className="w-4 h-4 flex-shrink-0" />
            {t('fileManager.allFiles')}
          </button>
          <button
            type="button"
            onClick={() => onSelectBucket('external')}
            aria-pressed={atRoot && bucketIsExternal}
            className={bucketClass(atRoot && bucketIsExternal)}
          >
            <FolderSymlink className="w-4 h-4 flex-shrink-0" />
            {t('fileManager.allExternal')}
          </button>
        </div>
      )}
      {/* The same kebab the tree row carries. Without it, switching the
          sidebar off would take Rename / Link / Delete with it — a persisted
          preference must not remove a capability, only move it. */}
      {folders.length > 0 &&
        (variant === 'grid' ? (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-3">
            {folders.map((folder) => (
              <div
                key={folder.id}
                data-folder-id={folder.id}
                className="group flex items-center gap-1 pr-1 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary hover:bg-bambu-dark hover:border-bambu-green/40 transition-colors"
              >
                <button
                  type="button"
                  onClick={() => onSelectFolder(folder.id)}
                  title={folderLabel(folder)}
                  className="min-w-0 flex-1 flex items-center gap-2 px-3 py-2.5 text-left"
                >
                  {folderIcon(folder)}
                  <FolderNumber number={folder.number} t={t} />
                  <span className="min-w-0 flex-1 truncate text-sm text-white">{folder.name}</span>
                  {folder.file_count > 0 && (
                    <span className="text-xs text-bambu-gray flex-shrink-0">{folder.file_count}</span>
                  )}
                </button>
                <FolderActionsMenu
                  folder={folder}
                  onDownloadFolder={onDownloadFolder}
                  onCopyPath={onCopyPath}
                  onDelete={onDeleteFolder}
                  onLink={onLinkFolder}
                  onRename={onRenameFolder}
                  hasPermission={hasPermission}
                  revealOnHover
                  t={t}
                />
              </div>
            ))}
          </div>
        ) : (
          <div className="bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary divide-y divide-bambu-dark-tertiary overflow-hidden">
            {folders.map((folder) => (
              <div
                key={folder.id}
                data-folder-id={folder.id}
                className="group flex items-center gap-1 pr-2 hover:bg-bambu-dark transition-colors"
              >
                <button
                  type="button"
                  onClick={() => onSelectFolder(folder.id)}
                  title={folderLabel(folder)}
                  className="min-w-0 flex-1 flex items-center gap-3 px-3 py-2 text-left"
                >
                  {folderIcon(folder)}
                  <FolderNumber number={folder.number} t={t} />
                  <span className="min-w-0 flex-1 truncate text-sm text-white">{folder.name}</span>
                  {folder.file_count > 0 && (
                    <span className="text-xs text-bambu-gray flex-shrink-0">{folder.file_count}</span>
                  )}
                </button>
                <FolderActionsMenu
                  folder={folder}
                  onDownloadFolder={onDownloadFolder}
                  onCopyPath={onCopyPath}
                  onDelete={onDeleteFolder}
                  onLink={onLinkFolder}
                  onRename={onRenameFolder}
                  hasPermission={hasPermission}
                  revealOnHover
                  t={t}
                />
              </div>
            ))}
          </div>
        ))}
    </nav>
  );
}

// The chain of folders from a top-level folder down to `folderId`, or null
// when the id is not in the tree. Backs "Copy path", which needs the ancestors
// a file list item does not carry.
function findFolderChain(items: LibraryFolderTree[], folderId: number): LibraryFolderTree[] | null {
  for (const item of items) {
    if (item.id === folderId) return [item];
    const deeper = findFolderChain(item.children, folderId);
    if (deeper) return [item, ...deeper];
  }
  return null;
}

// Join an external folder's real path with a filename in that path's own
// flavour — a linked Windows share reads `C:\Jobs\RAFI`, and pasting a
// forward slash onto it back into Explorer is exactly the failure this
// feature exists to remove.
function joinExternalPath(dir: string, filename: string): string {
  const sep = dir.includes('\\') ? '\\' : '/';
  return dir.endsWith(sep) ? `${dir}${filename}` : `${dir}${sep}${filename}`;
}

// Which of the three "Copy path" confirmations to show. A library path, the
// real directory of an external folder and the real path of a file inside one
// are three different strings to paste, so the toast may not name the wrong
// kind — an Explorer paste of a "folder path" that is really a file fails.
const PATH_COPIED_TOAST = {
  library: 'fileManager.toast.pathCopied',
  folder: 'fileManager.toast.folderPathCopied',
  file: 'fileManager.toast.filePathCopied',
} as const;

// Stable stand-in for the flat listing the folders-first root never requests.
const NO_FILES: LibraryFileListItem[] = [];

export function FileManagerPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission, hasAnyPermission, canModify, authEnabled } = useAuth();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  // Read folder ID from URL query parameter
  const folderIdFromUrl = searchParams.get('folder');
  const initialFolderId = folderIdFromUrl ? parseInt(folderIdFromUrl, 10) : null;

  // State
  const [selectedFolderId, setSelectedFolderId] = useState<number | null>(initialFolderId);
  // Which top-level pseudo-view the sidebar shows when no specific folder is
  // selected: "internal" = files in Bambuddy's managed storage, "external" =
  // combined view across every linked external folder (#1621). Per-folder
  // selection bypasses this (selectedFolderId !== null disables the filter).
  const [topLevelView, setTopLevelView] = useState<'internal' | 'external'>('internal');
  // The root's "No folder" entry has been opened: a location inside the root,
  // not a filter, so leaving the root drops it. Only reachable while the root
  // lists folders instead of every file.
  const [showUnfoldered, setShowUnfoldered] = useState(false);
  // The root crumb has been used to step out of the recent start page into the
  // flat listing it names. A location inside the root as well, so leaving the
  // root drops it and coming back lands on the start page again.
  const [showAllAtRoot, setShowAllAtRoot] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<number[]>([]);
  const [showNewFolderModal, setShowNewFolderModal] = useState(false);
  const [showExternalFolderModal, setShowExternalFolderModal] = useState(false);
  const [showMoveModal, setShowMoveModal] = useState(false);
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [droppedFiles, setDroppedFiles] = useState<File[]>([]);
  const [showPurgeModal, setShowPurgeModal] = useState(false);
  // Tag UI state (#1268). selectedTagIds is the AND-style filter applied to
  // the listing; setting it bypasses folder scoping on the server so
  // "every toy" works regardless of which folder is currently selected.
  const [showTagsModal, setShowTagsModal] = useState(false);
  const [showBulkTagsModal, setShowBulkTagsModal] = useState(false);
  const [selectedTagIds, setSelectedTagIds] = useState<number[]>([]);
  const [linkFolder, setLinkFolder] = useState<LibraryFolderTree | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<{ type: 'file' | 'folder' | 'bulk'; id: number; count?: number } | null>(null);
  const [printFile, setPrintFile] = useState<LibraryFileListItem | null>(null);
  const [sliceFile, setSliceFile] = useState<LibraryFileListItem | null>(null);
  // Slicer Pipelines (#1425 PR B) — file gets "Run with pipeline" action.
  const [runPipelineFile, setRunPipelineFile] = useState<LibraryFileListItem | null>(null);
  const [renameItem, setRenameItem] = useState<{
    type: 'file' | 'folder';
    id: number;
    name: string;
    number?: string | null;
  } | null>(null);
  const [thumbnailVersions, setThumbnailVersions] = useState<Record<number, number>>({});
  const [viewerFile, setViewerFile] = useState<LibraryFileListItem | null>(null);
  const [pdfPreviewFile, setPdfPreviewFile] = useState<LibraryFileListItem | null>(null);
  const [sheetPreviewFile, setSheetPreviewFile] = useState<LibraryFileListItem | null>(null);
  const [msgPreviewFile, setMsgPreviewFile] = useState<LibraryFileListItem | null>(null);
  const [imagePreviewFile, setImagePreviewFile] = useState<LibraryFileListItem | null>(null);
  const [detailsFile, setDetailsFile] = useState<LibraryFileListItem | null>(null);
  const [viewMode, setViewMode] = useState<'grid' | 'list' | 'columns'>(() => {
    return (localStorage.getItem('library-view-mode') as 'grid' | 'list' | 'columns') || 'grid';
  });
  // Miller-columns keyboard navigation (#3020): the pane itself is focusable
  // and arrow keys walk the hierarchy. The focused file is tracked separately
  // from the checkbox selection so Space can toggle a file without the arrow
  // keys hijacking multi-select.
  const columnsViewRef = useRef<HTMLDivElement>(null);
  const [columnsFocusedFileId, setColumnsFocusedFileId] = useState<number | null>(null);
  const [wrapFolderNames, setWrapFolderNames] = useState(() => {
    return localStorage.getItem('library-wrap-folders') === 'true';
  });
  const [collapseFoldersByDefault, setCollapseFoldersByDefault] = useState(() => {
    return localStorage.getItem('library-collapse-folders') === 'true';
  });
  // Folder tree sort (#1770). 'name' = alphabetical (the prior behaviour);
  // 'activity' = most recent file activity inside the folder first. Persisted
  // independently from the file-side sort so each can be tuned to taste.
  const [folderSortField, setFolderSortField] = useState<'name' | 'activity'>(() => {
    const saved = localStorage.getItem('library-folder-sort-field');
    return saved === 'activity' ? 'activity' : 'name';
  });
  const [folderSortDirection, setFolderSortDirection] = useState<'asc' | 'desc'>(() => {
    const saved = localStorage.getItem('library-folder-sort-direction');
    return saved === 'desc' ? 'desc' : 'asc';
  });

  // Resizable sidebar state
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = localStorage.getItem('library-sidebar-width');
    return saved ? parseInt(saved, 10) : 256; // Default w-64 = 256px
  });
  const [isResizing, setIsResizing] = useState(false);
  const sidebarRef = useRef<HTMLDivElement>(null);

  // The tree can be switched off entirely to give a deep customer/job tree the
  // full width. The content area then grows its own folder navigation
  // (ContentFolderNav) and the path bar covers getting back up. Defaults to
  // shown.
  const [sidebarHidden, setSidebarHidden] = useState(() => {
    return localStorage.getItem('library-sidebar-hidden') === 'true';
  });

  // The folders of the current level are drawn twice on a wide screen: in the
  // tree on the left and as tiles above the files. Which of the two you want is
  // a matter of taste, so it is a switch rather than a rule — independent of
  // the sidebar, because somebody may well want the tiles and no tree.
  const [folderTilesHidden, setFolderTilesHidden] = useState(() => {
    try {
      return localStorage.getItem('library-folder-tiles-hidden') === 'true';
    } catch {
      return false;
    }
  });

  const handleToggleFolderTiles = useCallback(() => {
    setFolderTilesHidden((prev) => {
      const next = !prev;
      try {
        localStorage.setItem('library-folder-tiles-hidden', String(next));
      } catch {
        // A browser that refuses storage still toggles, it just forgets.
      }
      return next;
    });
  }, []);

  // Handle sidebar resize
  useEffect(() => {
    if (!isResizing) return;

    // Prevent text selection during resize
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';

    const handleMouseMove = (e: MouseEvent) => {
      if (!sidebarRef.current) return;
      const containerRect = sidebarRef.current.parentElement?.getBoundingClientRect();
      if (!containerRect) return;
      // Calculate new width based on mouse position relative to container
      const newWidth = e.clientX - containerRect.left;
      // Clamp between 200px and 500px
      const clampedWidth = Math.min(500, Math.max(200, newWidth));
      setSidebarWidth(clampedWidth);
    };

    const handleMouseUp = () => {
      setIsResizing(false);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      // Save to localStorage
      localStorage.setItem('library-sidebar-width', String(sidebarWidth));
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
  }, [isResizing, sidebarWidth]);

  // Filter and sort state (persist sort preferences to localStorage)
  const [searchQuery, setSearchQuery] = useState('');
  const [filterType, setFilterType] = useState<string>('all');
  const [filterUsername, setFilterUsername] = useState('');
  const [sortField, setSortField] = useState<SortField>(() => {
    const saved = localStorage.getItem('library-sort-field');
    return (saved as SortField) || 'name';
  });
  const [sortDirection, setSortDirection] = useState<SortDirection>(() => {
    const saved = localStorage.getItem('library-sort-direction');
    return (saved as SortDirection) || 'asc';
  });
  // Show/hide the last-modified date on each file card (#2680). Persisted.
  const [showModified, setShowModified] = useState<boolean>(
    () => localStorage.getItem('library-show-modified') === 'true'
  );

  // Update selectedFolderId when URL parameter changes (e.g., navigating from Project or Archive page)
  useEffect(() => {
    const folderParam = searchParams.get('folder');
    if (folderParam) {
      const newFolderId = parseInt(folderParam, 10);
      setSelectedFolderId(newFolderId);
    }
  }, [searchParams]);

  // Queries
  const { data: settings, isLoading: settingsLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings() as Promise<AppSettings>,
  });

  const preferredSlicer: SlicerType = resolveDesktopSlicer(settings?.open_in_slicer, settings?.preferred_slicer);

  const handleOpenInSlicer = useCallback(async (file: LibraryFileListItem) => {
    try {
      const { token } = await api.createLibrarySlicerToken(file.id);
      const path = api.getLibrarySlicerDownloadUrl(file.id, token, file.filename);
      openInSlicer(`${window.location.origin}${path}`, preferredSlicer);
    } catch {
      // Fallback to direct URL (works when auth is disabled). With auth on the
      // slicer may then hit a 401, so surface the failure instead of making a
      // permission denial look identical to "no slicer installed".
      showToast(t('fileManager.toast.openInSlicerFailed'), 'error');
      const path = api.getLibraryFileDownloadUrl(file.id);
      openInSlicer(`${window.location.origin}${path}`, preferredSlicer);
    }
  }, [preferredSlicer, showToast, t]);

  // Slice permission: API mode needs upload rights, the desktop handoff is a
  // download. Each mirrors what the backend enforces on the endpoint that
  // branch actually calls, so the UI never offers an action the server refuses.
  //
  // Deliberately NOT accepting the legacy `library:read` on the handoff branch.
  // It looks like the safe back-compat term to include, but the slicer-token
  // endpoint gates on require_ownership_permission(LIBRARY_READ_ALL,
  // LIBRARY_READ_OWN), and neither that dependency nor User.has_permission
  // expands the legacy name — so a group holding only `library:read` gets a 403
  // there. It cannot reach this page to find out either: GET /library/folders
  // gates on the same pair. Accepting it here would only enable a menu item
  // that fails, and the `library:read` -> `library:read_own` migration in
  // core/database.py runs only over the groups named in DEFAULT_GROUPS, so a
  // custom role that still carries it is genuinely stuck rather than silently
  // upgraded.
  const canSlice = useCallback(() => {
    if (settings?.use_slicer_api) {
      return hasPermission('library:upload');
    }
    return hasAnyPermission('library:read_all', 'library:read_own');
  }, [settings?.use_slicer_api, hasPermission, hasAnyPermission]);
  const { data: folders, isLoading: foldersLoading } = useQuery({
    queryKey: ['library-folders'],
    queryFn: () => api.getLibraryFolders(),
  });

  // Recursive folder tree sort (#1770). Applies the same comparator to the
  // top-level list AND to each level of `children`, so sort order is uniform
  // at every depth of nesting. When sorting by activity, the comparator falls
  // back to a created-at fallback for folders with no files (`latest_activity_at`
  // is null) so they stay grouped at the end / start of the bucket instead of
  // randomly interspersed.
  const sortedFolders = useMemo(() => {
    if (!folders) return folders;
    const sortLevel = (items: LibraryFolderTree[]): LibraryFolderTree[] => {
      const sorted = [...items].sort((a, b) => {
        let comparison = 0;
        if (folderSortField === 'name') {
          comparison = a.name.localeCompare(b.name);
        } else {
          // activity: newest first on 'desc', oldest first on 'asc'.
          // Folders with no activity timestamp sort to the end regardless
          // of direction so an empty folder doesn't elbow a recently-used one.
          const aTs = a.latest_activity_at ? new Date(a.latest_activity_at).getTime() : null;
          const bTs = b.latest_activity_at ? new Date(b.latest_activity_at).getTime() : null;
          if (aTs === null && bTs === null) {
            comparison = a.name.localeCompare(b.name);
          } else if (aTs === null) {
            return 1;
          } else if (bTs === null) {
            return -1;
          } else {
            comparison = aTs - bTs;
          }
        }
        return folderSortDirection === 'asc' ? comparison : -comparison;
      });
      return sorted.map((f) => ({ ...f, children: sortLevel(f.children) }));
    };
    return sortLevel(folders);
  }, [folders, folderSortField, folderSortDirection]);

  // Trash count for the header badge (#1008). Empty/error are silently treated
  // as zero so a broken trash endpoint doesn't break the File Manager.
  const { data: trashCount } = useQuery({
    queryKey: ['library-trash-count'],
    queryFn: async () => {
      try {
        const res = await api.listLibraryTrash(1, 0);
        return res.total;
      } catch {
        return 0;
      }
    },
    staleTime: 30_000,
  });

  // #1268: when a folder is selected and the user has typed a search query,
  // ask the server to expand the result to every descendant folder so the
  // client-side filter can match files in subfolders too. Without this the
  // listing is just the immediate children and "robot.3mf" two levels deep
  // is invisible from the parent. Only kicks in for folder-scoped views —
  // root and the internal/external pseudo-nodes already return the union.
  const searchExpandsSubfolders = selectedFolderId !== null && searchQuery.trim().length > 0;
  // The tag filter overrides folder scoping server-side (#1268 design call),
  // so the FE query key includes it as a peer of folder/topLevelView. Sorted
  // so the cache hits regardless of the order tags were toggled.
  const tagFilterKey = useMemo(() => [...selectedTagIds].sort((a, b) => a - b), [selectedTagIds]);
  // Tag catalog — needed to resolve names for the active-filter chip bar.
  // Cheap query, shared with LibraryTagsModal / BulkTagsPickerModal via the
  // same queryKey so they all invalidate together on tag CRUD.
  const { data: tagCatalog = [] } = useQuery({
    queryKey: ['library-tags'],
    queryFn: api.getLibraryTags,
  });
  const tagsById = useMemo(() => {
    const map = new Map<number, string>();
    for (const t of tagCatalog) map.set(t.id, t.name);
    return map;
  }, [tagCatalog]);
  // Prune the active filter when a tag is removed from the catalog so the
  // listing never stalls on a phantom id. Skipped while the catalog query is
  // still settling (empty array on first paint) — otherwise the user's filter
  // gets cleared the moment the page mounts.
  useEffect(() => {
    if (tagCatalog.length === 0) return;
    setSelectedTagIds((prev) => {
      const next = prev.filter((id) => tagsById.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [tagCatalog.length, tagsById]);

  const toggleTagFilter = useCallback((tagId: number) => {
    setSelectedTagIds((prev) =>
      prev.includes(tagId) ? prev.filter((id) => id !== tagId) : [...prev, tagId],
    );
  }, []);

  // The root shows one of three things: every file in the library — no folder,
  // no project and no tag filter narrows the query, so the server answers with
  // all of it — or its own top-level folders, or the files most recently added
  // or changed. A search or a tag filter means "look everywhere", so both
  // restore the flat listing whatever the setting says.
  const rootQueryOverridden = searchQuery.trim().length > 0 || selectedTagIds.length > 0;
  // "recent" is the only one of the three that is a window rather than a whole
  // listing, so two more things drop it back to the flat one. A type or user
  // filter, because the page applies those to the rows it holds: over a capped
  // window that answers "no STL files" for a library full of them, one control
  // away from the search box that looks everywhere. And the root crumb, which
  // says "All Files" — at the recent root it is the only way out of the start
  // page, since every other piece of state it clears is already clear.
  // `folders` needs neither: its listings are complete, so filtering them here
  // is the whole truth, and its root crumb genuinely leaves "No folder".
  const rootWindowFiltered = filterType !== 'all' || filterUsername.trim().length > 0;
  const configuredRootView = settings?.library_root_view ?? 'all';
  const rootViewSetting =
    configuredRootView === 'recent' && (rootWindowFiltered || showAllAtRoot) ? 'all' : configuredRootView;
  const rootView = selectedFolderId === null && !rootQueryOverridden ? rootViewSetting : 'all';
  const rootListsFolders = rootView === 'folders';
  // The start page: the newest files across the whole library, ordered and
  // capped by the server.
  const rootRecentView = rootView === 'recent';
  // Inside the root, "No folder": the files that belong to no folder at all.
  const rootUnfolderedView = rootListsFolders && showUnfoldered;
  // Folder tiles only — the whole point is not to ask the server for the rows.
  const rootTilesOnly = rootListsFolders && !showUnfoldered;
  // Until the setting has arrived, the root cannot know which of the two it is,
  // and firing the all-files request meanwhile would defeat the whole thing —
  // the rows would already be on the wire by the time the answer says not to
  // ask for them. Only the root waits; a selected folder is unaffected.
  const rootAwaitingSetting = selectedFolderId === null && settingsLoading;

  const { data: fetchedFiles, isLoading: filesQueryLoading } = useQuery({
    queryKey: [
      'library-files',
      selectedFolderId,
      topLevelView,
      searchExpandsSubfolders,
      tagFilterKey,
      rootUnfolderedView,
      rootRecentView,
    ],
    // When a specific folder is selected we list its contents directly; when
    // no folder is selected the topLevelView pseudo-node decides whether the
    // server scopes the result to internal-managed-storage files or to the
    // union of every external folder (#1621). include_root stays false so the
    // listing still descends into subfolders (regression guard from #1499) —
    // except in the root's "No folder" view, which is exactly that listing.
    queryFn: () =>
      api.getLibraryFiles(
        selectedFolderId,
        rootUnfolderedView,
        undefined,
        selectedFolderId === null ? topLevelView : undefined,
        searchExpandsSubfolders,
        tagFilterKey,
        rootRecentView,
      ),
    enabled: !rootTilesOnly && !rootAwaitingSetting,
  });
  // An empty list rather than `undefined` while the request is deliberately not
  // made, so the empty state below reads "no files" and not "still loading".
  const files = rootTilesOnly ? NO_FILES : fetchedFiles;
  // Waiting for the setting is still loading, not "no files".
  const filesLoading = filesQueryLoading || rootAwaitingSetting;

  const { data: stats } = useQuery({
    queryKey: ['library-stats'],
    queryFn: () => api.getLibraryStats(),
  });

  // Get users for the username filter autocomplete -- names only (#1894)
  const { data: users } = useQuery({
    queryKey: ['users', 'slim'],
    queryFn: () => api.getUsersSlim(),
  });

  // Get unique file types for filter dropdown
  const fileTypes = useMemo(() => {
    if (!files) return [];
    const types = new Set(files.map((f) => f.file_type));
    return Array.from(types).sort();
  }, [files]);

  // Filter and sort files
  const filteredAndSortedFiles = useMemo(
    () =>
      filterAndSortFiles(files ?? [], {
        query: searchQuery,
        filterType,
        filterUsername,
        // The recent root IS an order: the server picked its rows by recency
        // and cut them off, so any other sort would show an arbitrary window
        // in an arbitrary order. The controls say so by being disabled.
        sortField: rootRecentView ? 'date' : sortField,
        sortDirection: rootRecentView ? 'desc' : sortDirection,
      }),
    [files, searchQuery, filterType, filterUsername, sortField, sortDirection, rootRecentView],
  );

  // Check if disk space is low
  const isDiskSpaceLow = useMemo(() => {
    if (!stats || !settings) return false;
    const thresholdBytes = (settings.library_disk_warning_gb || 5) * 1024 * 1024 * 1024;
    return stats.disk_free_bytes < thresholdBytes;
  }, [stats, settings]);

  // Mutations
  const createFolderMutation = useMutation({
    mutationFn: (data: LibraryFolderCreate) => api.createLibraryFolder(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      // The create just consumed a number, so the series the New Folder dialog
      // reads its "next number" from has moved. Without this the cached row
      // stays fresh for App.tsx's staleTime and the dialog promises a number
      // the next folder will not get — which is the number the operator writes
      // on the quote.
      queryClient.invalidateQueries({ queryKey: ['number-series'] });
      setShowNewFolderModal(false);
      showToast(t('fileManager.toast.folderCreated'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const createExternalFolderMutation = useMutation({
    mutationFn: async (data: ExternalFolderCreate) => {
      const folder = await api.createExternalFolder(data);
      // Auto-scan after creation
      await api.scanExternalFolder(folder.id);
      return folder;
    },
    onSuccess: (folder) => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      setShowExternalFolderModal(false);
      setSelectedFolderId(folder.id);
      showToast(t('fileManager.toast.externalFolderLinked'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const scanExternalFolderMutation = useMutation({
    mutationFn: (folderId: number) => api.scanExternalFolder(folderId),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      showToast(t('fileManager.toast.folderScanned', { added: result.added, removed: result.removed }), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const deleteFolderMutation = useMutation({
    mutationFn: (id: number) => api.deleteLibraryFolder(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      if (selectedFolderId === deleteConfirm?.id) {
        setSelectedFolderId(null);
      }
      setDeleteConfirm(null);
      showToast(t('fileManager.toast.folderDeleted'), 'success');
    },
    onError: (error: Error) => {
      setDeleteConfirm(null);
      showToast(error.message, 'error');
    },
  });

  const deleteFileMutation = useMutation({
    mutationFn: (id: number) => api.deleteLibraryFile(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash-count'] });
      setSelectedFiles((prev) => prev.filter((id) => id !== deleteConfirm?.id));
      setDeleteConfirm(null);
      showToast(t('fileManager.toast.fileDeleted'), 'success');
    },
    onError: (error: Error) => {
      setDeleteConfirm(null);
      showToast(error.message, 'error');
    },
  });

  // "These files are the same job for different printers" (#671 / #2570).
  // Durable, unlike the ad-hoc selection the Print button uses: once grouped,
  // printing any member offers the others without re-selecting them.
  const groupAsVersionsMutation = useMutation({
    mutationFn: (fileIds: number[]) =>
      api.createVariantGroup(fileIds.map((id) => ({ library_file_id: id }))),
    onSuccess: (group) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      showToast(t('fileManager.variants.grouped', { count: group.members.length }), 'success');
      setSelectedFiles([]);
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const bulkDeleteMutation = useMutation({
    mutationFn: (fileIds: number[]) => api.bulkDeleteLibrary(fileIds, []),
    onSuccess: (_, fileIds) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash-count'] });
      showToast(t('fileManager.toast.filesDeleted', { count: fileIds.length }), 'success');
      setSelectedFiles([]);
      setDeleteConfirm(null);
    },
    onError: (error: Error) => {
      setDeleteConfirm(null);
      showToast(error.message, 'error');
    },
  });

  const moveFilesMutation = useMutation({
    mutationFn: ({ fileIds, folderId }: { fileIds: number[]; folderId: number | null }) =>
      api.moveLibraryFiles(fileIds, folderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      // A move is the one operation that changes how many files sit in no
      // folder at all, and the folders-first root reads that count from the
      // stats to decide whether to offer "No folder". Without this the files
      // moved to the root have no entry to appear under until a reload.
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      setSelectedFiles([]);
      setShowMoveModal(false);
      showToast(t('fileManager.toast.filesMoved'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const updateFolderMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: LibraryFolderUpdate }) =>
      api.updateLibraryFolder(id, data),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      // Invalidate project/archive folder queries so other pages see the update
      queryClient.invalidateQueries({ queryKey: ['project-folders'] });
      queryClient.invalidateQueries({ queryKey: ['archive-folders'] });
      setLinkFolder(null);
      const isUnlink = variables.data.project_id === 0 && variables.data.archive_id === 0;
      showToast(isUnlink ? t('fileManager.toast.folderUnlinked') : t('fileManager.toast.folderLinked'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const renameFileMutation = useMutation({
    mutationFn: ({ id, filename }: { id: number; filename: string }) =>
      api.updateLibraryFile(id, { filename }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      setRenameItem(null);
      showToast(t('fileManager.toast.fileRenamed'), 'success');
    },
    onError: (error: Error) => {
      setRenameItem(null);
      showToast(error.message, 'error');
    },
  });

  const renameFolderMutation = useMutation({
    mutationFn: ({ id, name, number }: { id: number; name?: string; number?: string | null }) =>
      // Each field is sent only when the dialog changed it — the backend reads
      // "sent but null" as "clear it" and a missing key as "leave it alone",
      // so spelling the number out on every rename would drop it.
      api.updateLibraryFolder(id, {
        ...(name === undefined ? {} : { name }),
        ...(number === undefined ? {} : { number }),
      }),
    onSuccess: () => {
      // Invalidate both folders and files - files may display folder info
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      setRenameItem(null);
      showToast(t('fileManager.toast.folderRenamed'), 'success');
    },
    onError: (error: Error) => {
      // The only 409 this route answers is a folder number that is already
      // taken, and the backend's detail is English. Leave the dialog open so
      // the number can be corrected instead of retyping the rename.
      const taken = error instanceof ApiError && error.status === 409;
      if (!taken) setRenameItem(null);
      showToast(taken ? t('fileManager.folderNumberTaken') : error.message, 'error');
    },
  });

  // What the server-side batch leaves behind: previews only a browser can
  // draw. The run starts when that batch reports back, and drives the hidden
  // renderer mounted at the bottom of the page (#2976).
  const [previewBatch, setPreviewBatch] = useState<PendingPreviewThumbnail[]>([]);
  const [previewBatchProgress, setPreviewBatchProgress] = useState<{ done: number; total: number } | null>(null);

  const startPreviewBatch = useCallback(async () => {
    if (!hasAnyPermission('library:update_own', 'library:update_all')) return;
    try {
      const pending = await api.listPendingPreviewThumbnails();
      if (pending.length === 0) return;
      setPreviewBatchProgress({ done: 0, total: pending.length });
      setPreviewBatch(pending);
    } catch {
      // The server batch already reported its own result; the browser half is
      // an extra and stays silent when the list cannot be fetched.
    }
  }, [hasAnyPermission]);

  const batchThumbnailMutation = useMutation({
    mutationFn: () => api.batchGenerateStlThumbnails({ all_missing: true }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      // Update thumbnail versions for cache busting
      if (result.succeeded > 0) {
        const now = Date.now();
        const newVersions: Record<number, number> = {};
        result.results.forEach((r) => {
          if (r.success) {
            newVersions[r.file_id] = now;
          }
        });
        setThumbnailVersions((prev) => ({ ...prev, ...newVersions }));
      }
      void startPreviewBatch();
      if (result.succeeded > 0 && result.failed === 0) {
        showToast(t('fileManager.toast.thumbnailsGenerated', { count: result.succeeded }), 'success');
      } else if (result.succeeded > 0 && result.failed > 0) {
        showToast(t('fileManager.toast.thumbnailsGeneratedPartial', { succeeded: result.succeeded, failed: result.failed }), 'success');
      } else if (result.processed === 0) {
        showToast(t('fileManager.toast.noStlMissingThumbnails'), 'info');
      } else {
        showToast(t('fileManager.toast.failedToGenerateThumbnails', { error: result.results[0]?.error || 'Unknown error' }), 'error');
      }
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const singleThumbnailMutation = useMutation({
    mutationFn: (fileId: number) => api.batchGenerateStlThumbnails({ file_ids: [fileId] }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      // Update thumbnail version for cache busting
      if (result.succeeded > 0) {
        const fileId = result.results[0]?.file_id;
        if (fileId) {
          setThumbnailVersions((prev) => ({ ...prev, [fileId]: Date.now() }));
        }
        showToast(t('fileManager.toast.thumbnailGenerated'), 'success');
      } else {
        showToast(t('fileManager.toast.failedToGenerateThumbnail', { error: result.results[0]?.error || 'Unknown error' }), 'error');
      }
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  // The one way into a preview (#2976): the kebab entry, the action-strip
  // icon, a double-click on the card, row or columns entry, the columns
  // view's Enter key and the toolbar button all end up here. Sliced files
  // open the full-page gcode viewer the archive card uses; everything else
  // opens the modal for its type. A file with no preview does nothing.
  const openPreview = useCallback((file: LibraryFileListItem) => {
    if (!hasPermission('library:read')) return;
    const kind = previewKind(file);
    if (!kind) return;
    // A Record rather than a chain of ifs: a preview kind added without a
    // branch here is a type error instead of a Preview button that silently
    // does nothing.
    const open: Record<PreviewKind, () => void> = {
      gcode: () => navigate(`/gcode-viewer?library_file=${file.id}`),
      model: () => setViewerFile(file),
      pdf: () => setPdfPreviewFile(file),
      msg: () => setMsgPreviewFile(file),
      spreadsheet: () => setSheetPreviewFile(file),
      image: () => setImagePreviewFile(file),
    };
    open[kind]();
  }, [hasPermission, navigate]);

  // The toolbar's Preview button acts on one file, so it is offered only for
  // a single previewable selection.
  const previewSelection = useMemo(() => {
    if (!files || selectedFiles.length !== 1) return null;
    const file = files.find((f) => f.id === selectedFiles[0]);
    return file && isPreviewableLibraryFile(file) ? file : null;
  }, [files, selectedFiles]);

  // The clicked file's variant group, so printing one member offers the rest
  // without the user re-selecting them (#2570).
  const { data: printFileGroup } = useQuery({
    queryKey: ['variant-group', printFile?.variant_group_id],
    queryFn: () => api.getVariantGroup(printFile!.variant_group_id!),
    enabled: !!printFile?.variant_group_id,
  });

  // Handlers
  const handleFileSelect = useCallback((id: number) => {
    // Always toggle selection (multi-select by default)
    setSelectedFiles((prev) => {
      return prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
    });
  }, []);

  const handleSelectAll = useCallback(() => {
    if (filteredAndSortedFiles.length > 0) {
      setSelectedFiles(filteredAndSortedFiles.map((f) => f.id));
    }
  }, [filteredAndSortedFiles]);

  const handleDeselectAll = useCallback(() => {
    setSelectedFiles([]);
  }, []);

  const handleUploadComplete = () => {
    queryClient.invalidateQueries({ queryKey: ['library-files'] });
    queryClient.invalidateQueries({ queryKey: ['library-folders'] });
    queryClient.invalidateQueries({ queryKey: ['library-stats'] });
  };

  // Page-wide drag-and-drop upload (#1510). Disabled when the user lacks
  // library:upload so a non-uploader can't accidentally show the overlay,
  // and also disabled while the upload modal itself is open so drags into
  // the modal's own drop zone don't bubble up and flash the page overlay
  // behind it.
  const canUpload = hasPermission('library:upload');
  const { isDraggingOver, dragHandlers } = usePageFileDrop({
    disabled: !canUpload || showUploadModal,
    onFiles: (files) => {
      setDroppedFiles(files);
      setShowUploadModal(true);
    },
  });

  // Returns the snapshot callback the preview components call with their
  // first render, or undefined when nothing should be persisted — the file
  // already has a thumbnail, or the user may not update it (#2976).
  const previewSnapshotHandler = useCallback(
    (file: LibraryFileListItem): ((blob: Blob) => void) | undefined => {
      if (file.thumbnail_path) return undefined;
      if (!canModify('library', 'update', file.created_by_id)) return undefined;
      return (blob: Blob) => {
        api
          .uploadLibraryPreviewThumbnail(file.id, blob)
          .then((res) => {
            if (res.updated) {
              setThumbnailVersions((prev) => ({ ...prev, [file.id]: (prev[file.id] || 0) + 1 }));
              queryClient.invalidateQueries({ queryKey: ['library-files'] });
            }
          })
          .catch(() => {
            // Thumbnail persistence is best-effort; the preview already rendered.
          });
      };
    },
    [canModify, queryClient]
  );

  const handleDownload = (id: number) => {
    api.downloadLibraryFile(id).catch((err) => {
      console.error('Library file download failed:', err);
    });
  };

  // One selected file is still a plain download — wrapping a single 3MF in a
  // ZIP only costs the user an unpacking step. Several become one archive.
  // The server's message is shown verbatim: it is the one that names the cap
  // that was hit and by how much.
  const handleDownloadSelection = () => {
    const ids = [...selectedFiles];
    if (ids.length === 0) return;
    const request = ids.length === 1 ? api.downloadLibraryFile(ids[0]) : api.downloadLibraryFilesZip(ids);
    request.catch((err) => {
      console.error('Library bulk download failed:', err);
      showToast(err instanceof Error ? err.message : t('fileManager.toast.downloadFailed'), 'error');
    });
  };

  const handleDownloadFolder = (folder: LibraryFolderTree) => {
    api.downloadLibraryFolderZip(folder.id).catch((err) => {
      console.error('Library folder download failed:', err);
      showToast(err instanceof Error ? err.message : t('fileManager.toast.downloadFailed'), 'error');
    });
  };

  const handleDeleteConfirm = () => {
    if (!deleteConfirm) return;
    if (deleteConfirm.type === 'file') {
      deleteFileMutation.mutate(deleteConfirm.id);
    } else if (deleteConfirm.type === 'folder') {
      deleteFolderMutation.mutate(deleteConfirm.id);
    } else if (deleteConfirm.type === 'bulk') {
      bulkDeleteMutation.mutate(selectedFiles);
    }
  };

  const isDeleting = deleteFolderMutation.isPending || deleteFileMutation.isPending || bulkDeleteMutation.isPending;

  const handleViewModeChange = (mode: 'grid' | 'list' | 'columns') => {
    setViewMode(mode);
    localStorage.setItem('library-view-mode', mode);
  };

  const handleToggleSidebar = useCallback(() => {
    setSidebarHidden((prev) => {
      const next = !prev;
      localStorage.setItem('library-sidebar-hidden', String(next));
      return next;
    });
  }, []);

  // The columns view is where the tree is most redundant — the first column
  // lists the same folders — so that is the view where switching it off buys
  // the most width. It used to be the one view where the toggle did nothing.
  const folderSidebarVisible = !sidebarHidden;

  // "Copy path" (tree and columns kebab, the file actions, the path bar). An
  // external folder copies the real directory it was linked from — the string
  // that goes back into Explorer or the slicer — while a managed one has only
  // its library path. They are not interchangeable, so the toast says which of
  // the three landed on the clipboard; a real path ending in a filename is not
  // a directory and must not be confirmed as one.
  const copyPath = useCallback(
    async (value: string, kind: 'library' | 'folder' | 'file') => {
      if (!(await copyTextToClipboard(value))) return;
      showToast(t(PATH_COPIED_TOAST[kind]), 'success');
    },
    [showToast, t],
  );

  const libraryPathOf = useCallback(
    (folderId: number | null) =>
      folderId === null || !folders
        ? ''
        : (findFolderChain(folders, folderId) ?? []).map((folder) => folder.name).join('/'),
    [folders],
  );

  const handleCopyFolderPath = useCallback(
    (folder: LibraryFolderTree) => {
      if (folder.is_external && folder.external_path) copyPath(folder.external_path, 'folder');
      else copyPath(libraryPathOf(folder.id), 'library');
    },
    [copyPath, libraryPathOf],
  );

  const handleCopyFilePath = useCallback(
    (file: LibraryFileListItem) => {
      const parent =
        file.folder_id !== null && folders ? findFolderChain(folders, file.folder_id)?.at(-1) : undefined;
      if (parent?.is_external && parent.external_path) {
        copyPath(joinExternalPath(parent.external_path, file.filename), 'file');
        return;
      }
      const dir = libraryPathOf(file.folder_id);
      copyPath(dir ? `${dir}/${file.filename}` : file.filename, 'library');
    },
    [copyPath, folders, libraryPathOf],
  );

  // Shared by the list row and the columns view (see FileActionStrip).
  const fileActionProps = {
    onPrint: setPrintFile,
    onSlice: setSliceFile,
    onOpenInSlicer: handleOpenInSlicer,
    onRunPipeline: setRunPipelineFile,
    useSlicerApi: settings?.use_slicer_api ?? false,
    desktopSlicer: preferredSlicer,
    canSlice: canSlice(),
    onPreview: openPreview,
    onDetails: setDetailsFile,
    onDownload: handleDownload,
    onRename: (f: LibraryFileListItem) => setRenameItem({ type: 'file', id: f.id, name: f.filename }),
    onGenerateThumbnail: (f: LibraryFileListItem) => singleThumbnailMutation.mutate(f.id),
    onCopyPath: handleCopyFilePath,
    thumbnailPending: singleThumbnailMutation.isPending,
    onDelete: (id: number) => setDeleteConfirm({ type: 'file', id }),
    hasPermission,
    canModify,
    t,
  };

  const isLoading = foldersLoading || filesLoading;

  // Find the selected folder in the tree to check external status
  const selectedFolder = useMemo(() => {
    if (!selectedFolderId || !folders) return null;
    const findFolder = (items: LibraryFolderTree[]): LibraryFolderTree | null => {
      for (const item of items) {
        if (item.id === selectedFolderId) return item;
        const found = findFolder(item.children);
        if (found) return found;
      }
      return null;
    };
    return findFolder(folders);
  }, [selectedFolderId, folders]);

  // Direct subfolders of the current level, rendered as regular items in the
  // content pane (#3019) — folders first, then files, the way every file
  // explorer works. Before this, a folder holding only subfolders showed the
  // "folder is empty" state and descending was possible only in the tree.
  // Resolved from sortedFolders so the pane follows the tree's sort order;
  // the root level shows the current top-level bucket (internal/external).
  const visibleSubfolders = useMemo(() => {
    if (!sortedFolders) return [];
    if (selectedFolderId === null) {
      return sortedFolders.filter((f) => Boolean(f.is_external) === (topLevelView === 'external'));
    }
    const findFolder = (items: LibraryFolderTree[]): LibraryFolderTree | null => {
      for (const item of items) {
        if (item.id === selectedFolderId) return item;
        const found = findFolder(item.children);
        if (found) return found;
      }
      return null;
    };
    return findFolder(sortedFolders)?.children ?? [];
  }, [sortedFolders, selectedFolderId, topLevelView]);

  // The tiles disappear while a search or tag filter is active: those views
  // list matches from every descendant folder, so per-folder navigation
  // would sit beside results it doesn't scope. "No folder" is the same case
  // from the other side — it is defined as the files those very folders do
  // not hold, so showing them inside it would contradict its own crumb.
  // With the tree switched off, ContentFolderNav already draws this level's
  // folders — and it carries the kebab and the bucket switch the tree used to.
  // Drawing the tiles as well put every folder on screen twice, in exactly the
  // two views that have both.
  const contentNavDrawsFolders = !folderSidebarVisible && viewMode !== 'columns';
  const showFolderTiles =
    !folderTilesHidden &&
    !contentNavDrawsFolders &&
    visibleSubfolders.length > 0 &&
    !searchQuery.trim() &&
    selectedTagIds.length === 0 &&
    !rootUnfolderedView;

  // "No folder" sits beside the root's folder tiles, and only when there is
  // something behind it. The count rides along on the stats the header already
  // fetches — asking the listing endpoint whether it would return anything is
  // the very query this view exists to avoid.
  const unfolderedCount =
    (topLevelView === 'external' ? stats?.unfoldered_external_files : stats?.unfoldered_files) ?? 0;
  const showNoFolderEntry = rootTilesOnly && unfolderedCount > 0;

  // The chain of folders from a top-level folder down to the selected one.
  // Selection is the single source of truth — clicking a folder anywhere just
  // moves selectedFolderId, and the path (and with it the path bar and the set
  // of visible columns) is re-derived from the sorted tree.
  const folderPath = useMemo(() => {
    if (!sortedFolders || selectedFolderId === null) return [] as LibraryFolderTree[];
    const path: LibraryFolderTree[] = [];
    const walk = (items: LibraryFolderTree[]): boolean => {
      for (const item of items) {
        path.push(item);
        if (item.id === selectedFolderId) return true;
        if (walk(item.children)) return true;
        path.pop();
      }
      return false;
    };
    walk(sortedFolders);
    return path;
  }, [sortedFolders, selectedFolderId]);

  // Which half of the tree the current location lives in: the selected path's
  // bucket when there is one, otherwise the sidebar's top-level choice. Drives
  // both the path bar's root crumb and the columns view's first column.
  const currentBucketIsExternal =
    folderPath.length > 0 ? Boolean(folderPath[0].is_external) : topLevelView === 'external';

  // The current bucket's top-level folders. A typed path in the path bar
  // resolves against these, and its root separator lists them.
  const bucketRootFolders = useMemo(
    () => (sortedFolders ?? []).filter((f) => Boolean(f.is_external) === currentBucketIsExternal),
    [sortedFolders, currentBucketIsExternal],
  );

  // The folders one level below where the user is standing — the bucket's
  // top-level folders at the root, otherwise the selected folder's children.
  // This is what the content area offers when the sidebar is switched off.
  const currentFolderChildren = useMemo(() => {
    if (selectedFolderId === null) return bucketRootFolders;
    return folderPath[folderPath.length - 1]?.children ?? [];
  }, [bucketRootFolders, selectedFolderId, folderPath]);

  // One column per level: the top-level bucket, then the children of each
  // folder along the path. `folderId` is the folder the column is the inside
  // of (null = the library root), which is what its file list is keyed on.
  // Leaf folders contribute no column — the files pane to the right is their
  // content.
  const folderColumns = useMemo(() => {
    const rootItems = (sortedFolders ?? []).filter((f) => Boolean(f.is_external) === currentBucketIsExternal);
    const cols: { key: string; folderId: number | null; items: LibraryFolderTree[]; activeId: number | null }[] = [
      { key: 'root', folderId: null, items: rootItems, activeId: folderPath[0]?.id ?? null },
    ];
    folderPath.forEach((node, i) => {
      if (node.children.length > 0) {
        cols.push({
          key: `folder-${node.id}`,
          folderId: node.id,
          items: node.children,
          activeId: folderPath[i + 1]?.id ?? null,
        });
      }
    });
    return cols;
  }, [sortedFolders, folderPath, currentBucketIsExternal]);

  // While a search or tag filter is active the file list spans every matching
  // descendant folder, so per-level folder columns would lie about scope —
  // hide them and let the files pane take the full width.
  const columnsFilterActive = searchQuery.trim().length > 0 || selectedTagIds.length > 0;

  // Every column lists its own level's files, not just the rightmost pane: a
  // folder that holds files and no subfolders used to look empty until it was
  // the selection. One query per rendered level, keyed on that level's folder
  // id so walking back up a path is instant. Only levels the view actually
  // renders are fetched, and the level that IS the current selection is
  // skipped — the pane on the right already lists exactly those files. The
  // exception is the folders-first root, where that pane lists nothing at all:
  // its column keeps its own files, which are only the unfoldered ones.
  const columnFileLevels = useMemo(() => {
    if (viewMode !== 'columns' || columnsFilterActive) return [];
    return folderColumns
      .filter((col) => col.folderId !== selectedFolderId || rootTilesOnly)
      .map((col) => ({
        key: col.key,
        folderId: col.folderId,
        scope:
          col.folderId === null
            ? currentBucketIsExternal
              ? ('external' as const)
              : ('internal' as const)
            : undefined,
      }));
  }, [viewMode, columnsFilterActive, folderColumns, selectedFolderId, currentBucketIsExternal, rootTilesOnly]);

  const columnFileQueries = useQueries({
    queries: columnFileLevels.map((level) => ({
      queryKey: ['library-files', 'column', level.folderId, level.scope ?? null],
      // include_root only means anything for the root column, where "this
      // level's files" are the ones that sit in no folder at all. Under a
      // folder id the server already answers with that folder's own files.
      queryFn: () =>
        api.getLibraryFiles(level.folderId, level.folderId === null, undefined, level.scope, false, []),
    })),
  });

  // Keyed by column so the render stays a lookup. Deliberately not memoised:
  // useQueries hands back a fresh array every render, so a memo would recompute
  // anyway.
  const columnFiles = new Map<string, { files: LibraryFileListItem[]; loading: boolean }>();
  columnFileLevels.forEach((level, i) => {
    const query = columnFileQueries[i];
    columnFiles.set(level.key, {
      files: filterAndSortFiles(query?.data ?? [], { filterType, filterUsername, sortField, sortDirection }),
      loading: Boolean(query?.isPending),
    });
  });

  // The list the arrow keys walk: the one that actually holds the focused
  // file, which may be an intermediate column rather than the selected
  // folder's pane.
  const focusedFileColumnList =
    columnsFocusedFileId === null
      ? undefined
      : columnFileLevels
          .map((level) => columnFiles.get(level.key)?.files)
          .find((list) => list?.some((f) => f.id === columnsFocusedFileId));
  const focusedFileList = focusedFileColumnList ?? filteredAndSortedFiles;

  // The ticked rows as file objects. The main query only ever holds the
  // selected folder's files, but the columns view lets a file be ticked in any
  // ancestor column, so anything that needs more than the bare id has to look
  // at those levels too — otherwise the actions that read the file (Print,
  // Group as versions) find nothing and silently disappear.
  const selectedFileObjects = (() => {
    if (selectedFiles.length === 0) return [];
    const byId = new Map<number, LibraryFileListItem>();
    for (const file of files ?? []) byId.set(file.id, file);
    for (const entry of columnFiles.values()) {
      for (const file of entry.files) byId.set(file.id, file);
    }
    return selectedFiles
      .map((id) => byId.get(id))
      .filter((file): file is LibraryFileListItem => file !== undefined);
  })();

  const selectedSlicedFiles = selectedFileObjects.filter(isSlicedLibraryFile);

  // Where the selected files actually live. A file can be ticked in any
  // column, so this is not the same as selectedFolderId — the move dialog
  // needs the files' own folder to know which row is the no-op destination.
  // `undefined` when the selection spans several folders or none of the rows
  // is on screen any more: nothing is then marked current.
  const selectionSourceFolderId = (() => {
    if (selectedFileObjects.length === 0) return undefined;
    const folderIds = new Set<number | null>(
      selectedFileObjects.map((file) => file.folder_id ?? null)
    );
    return folderIds.size === 1 ? [...folderIds][0] : undefined;
  })();

  // Candidates for a cross-model print (#671), or undefined for an ordinary one.
  // An explicit multi-selection wins over the group: the user just said, in this
  // action, which files they meant.
  const printVariantFiles = (() => {
    if (!printFile) return undefined;
    if (selectedSlicedFiles.length > 1) {
      return selectedSlicedFiles.map(f => ({
        id: f.id,
        filename: f.filename,
        sliced_for_model: f.sliced_for_model,
      }));
    }
    if (printFileGroup && printFileGroup.members.length > 1) {
      return printFileGroup.members.map(m => ({
        id: m.library_file_id,
        filename: m.filename,
        sliced_for_model: m.target_model,
      }));
    }
    return undefined;
  })();

  // Folder name and number matches for the active search. The tree is already
  // in memory, so this needs no endpoint; a tree that has not loaded simply
  // matches nothing. `path` is the ancestor chain, shown under the name.
  // Looking a job up by the number on its quote is the whole reason the number
  // exists, so it is searched exactly like the name.
  const folderSearchMatches = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query || !sortedFolders) return [];
    const matches: { folder: LibraryFolderTree; path: string[] }[] = [];
    const walk = (items: LibraryFolderTree[], ancestors: string[]) => {
      for (const item of items) {
        const hit =
          item.name.toLowerCase().includes(query) ||
          (item.number ?? '').toLowerCase().includes(query);
        if (hit) matches.push({ folder: item, path: ancestors });
        walk(item.children, [...ancestors, folderLabel(item)]);
      }
    };
    walk(sortedFolders, []);
    return matches;
  }, [sortedFolders, searchQuery]);

  // The search/filter card also carries the select-all control and the
  // selection actions, so it has to outlive the file list. An organisational
  // folder holding nothing but subfolders is precisely where the folder search
  // is needed, and a selection made in another column must keep its actions on
  // screen even when the selected folder's own pane is empty. The folders-first
  // root keeps it unconditionally: it never lists files, and a search is the
  // documented way back to the flat listing — a library with no folders yet
  // would otherwise lose the search box that restores it.
  const showFilterCard =
    (files?.length ?? 0) > 0 ||
    (sortedFolders?.length ?? 0) > 0 ||
    searchQuery.trim().length > 0 ||
    selectedFiles.length > 0 ||
    rootListsFolders;

  // A selection belongs to the folder it was made in. Every column can tick a
  // file now, so a selection that survived a folder change would leave Move /
  // Delete pointing at rows that are no longer anywhere on screen.
  useEffect(() => {
    setSelectedFiles([]);
  }, [selectedFolderId]);

  // Descending into a folder leaves the root, and with it both of the places
  // inside it: the "No folder" listing and the step out of the start page.
  useEffect(() => {
    if (selectedFolderId === null) return;
    setShowUnfoldered(false);
    setShowAllAtRoot(false);
  }, [selectedFolderId]);

  const rootCrumbLabel = currentBucketIsExternal ? t('fileManager.allExternal') : t('fileManager.allFiles');

  const selectFolderFromChrome = (folderId: number) => {
    setColumnsFocusedFileId(null);
    setSelectedFolderId(folderId);
  };

  const selectPathRoot = () => {
    setColumnsFocusedFileId(null);
    setShowUnfoldered(false);
    // Standing on the start page already: the crumb says "All Files", so it
    // shows them. Without this it would set the state it is already in and the
    // one way out of the recent view would do nothing.
    if (rootRecentView) setShowAllAtRoot(true);
    setTopLevelView(currentBucketIsExternal ? 'external' : 'internal');
    setSelectedFolderId(null);
  };

  // The two top-level buckets, same as the sidebar's own entries. Without the
  // sidebar these are the only way to cross between managed storage and the
  // linked external folders.
  const selectTopLevelBucket = (view: 'internal' | 'external') => {
    setColumnsFocusedFileId(null);
    setShowUnfoldered(false);
    setTopLevelView(view);
    setSelectedFolderId(null);
  };

  // A folder hit jumps to the folder and drops the query — the user was
  // looking for the folder itself, not for a filtered view of it.
  const selectSearchedFolder = (folderId: number) => {
    setSearchQuery('');
    selectFolderFromChrome(folderId);
  };

  // Double-click / Enter on a file row: sliced output opens in the gcode
  // viewer, model files in the 3D viewer, anything else has no preview.
  const openColumnFile = (file: LibraryFileListItem) => {
    if (isSlicedLibraryFile(file)) navigate(`/gcode-viewer?library_file=${file.id}`);
    else if (file.file_type === '3mf' || file.file_type === 'stl') setViewerFile(file);
  };

  // Focus the columns pane when the view opens so arrow keys work right away,
  // and drop the file focus whenever the folder (and with it the file list)
  // changes. A modal or the search box taking focus naturally mutes the
  // pane's key handling — no explicit guards needed.
  useEffect(() => {
    if (viewMode === 'columns') columnsViewRef.current?.focus({ preventScroll: true });
  }, [viewMode]);
  useEffect(() => {
    setColumnsFocusedFileId(null);
  }, [selectedFolderId, viewMode]);
  // Keep the keyboard-driven selection scrolled into view. Deliberately an
  // effect keyed on the ids, NOT per-render ref callbacks: those re-run on
  // every commit and would snap a manually scrolled column back to the
  // highlighted row whenever anything re-renders. scrollIntoView is guarded
  // because jsdom doesn't implement it.
  useEffect(() => {
    if (viewMode !== 'columns' || selectedFolderId === null) return;
    columnsViewRef.current?.querySelector(`[data-folder-id="${selectedFolderId}"]`)?.scrollIntoView?.({ block: 'nearest' });
  }, [selectedFolderId, viewMode]);
  useEffect(() => {
    if (viewMode !== 'columns' || columnsFocusedFileId === null) return;
    columnsViewRef.current?.querySelector(`[data-file-id="${columnsFocusedFileId}"]`)?.scrollIntoView?.({ block: 'nearest' });
  }, [columnsFocusedFileId, viewMode]);

  // Finder-style keys: Up/Down move within the current column, Right descends
  // (into the first child folder, then into the files), Left ascends, Enter
  // opens the focused file's preview, Space toggles its selection.
  //
  // Actions: the focused file's icon strip is the only one in the Tab order
  // (roving tabindex), so Tab from the pane lands on it; the context-menu
  // key (or Shift+F10) jumps there directly, or opens the selected folder's
  // kebab while no file is focused. Inside a strip Left/Right walk the
  // buttons, Enter/Space activate one natively and Escape returns to the pane.
  const focusColumnsActions = () => {
    const root = columnsViewRef.current;
    if (!root) return;
    if (columnsFocusedFileId !== null) {
      root
        .querySelector<HTMLButtonElement>(`[data-file-id="${columnsFocusedFileId}"] [data-file-actions] button:not([disabled])`)
        ?.focus();
    } else if (selectedFolderId !== null) {
      const kebab = root.querySelector<HTMLButtonElement>(`[data-folder-id="${selectedFolderId}"] [data-folder-actions] button`);
      // Focus first so Tab continues into the menu that the click opens.
      kebab?.focus();
      kebab?.click();
    }
  };
  const handleColumnsKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const target = e.target as HTMLElement;
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable) return;
    if (target.closest('[data-folder-actions]')) {
      // A folder kebab (and the menu it opens) handles its own keys, except
      // that Escape and Up/Down hand focus back to the pane — otherwise the
      // arrows are dead until the user clicks or Shift+Tabs out of the kebab.
      if (e.key === 'Escape') {
        e.preventDefault();
        columnsViewRef.current?.focus({ preventScroll: true });
        return;
      }
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      columnsViewRef.current?.focus({ preventScroll: true });
    }
    const strip = target.closest<HTMLElement>('[data-file-actions]');
    if (strip) {
      if (e.key === 'Enter' || e.key === ' ') return;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault();
        const buttons = Array.from(strip.querySelectorAll<HTMLButtonElement>('button:not([disabled])'));
        buttons[buttons.indexOf(target as HTMLButtonElement) + (e.key === 'ArrowRight' ? 1 : -1)]?.focus();
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        columnsViewRef.current?.focus({ preventScroll: true });
        return;
      }
      // Up/Down move the row focus below; DOM focus must leave the old row's
      // strip with it, or the next Enter would fire that row's button.
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') columnsViewRef.current?.focus({ preventScroll: true });
    }
    if (e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey)) {
      e.preventDefault();
      focusColumnsActions();
      return;
    }
    const inFiles = columnsFocusedFileId !== null;
    const selectedNode = folderPath[folderPath.length - 1];
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowUp': {
        e.preventDefault();
        const dir = e.key === 'ArrowDown' ? 1 : -1;
        if (inFiles || columnsFilterActive) {
          // findIndex yields -1 for a vanished focus id — ArrowDown then
          // lands on index 0, ArrowUp on -2 → no-op, both intended.
          const idx = focusedFileList.findIndex((f) => f.id === columnsFocusedFileId);
          const next = focusedFileList[idx + dir];
          if (next) setColumnsFocusedFileId(next.id);
          else if (dir === -1 && idx === 0 && focusedFileColumnList) {
            // Off the top of a column's files: back onto that same column's
            // folders, which is where ArrowDown came from.
            setColumnsFocusedFileId(null);
          }
        } else if (selectedFolderId === null) {
          if (dir === 1 && folderColumns[0].items.length > 0) {
            setSelectedFolderId(folderColumns[0].items[0].id);
          }
        } else {
          const col = folderColumns[folderPath.length - 1];
          const idx = col ? col.items.findIndex((f) => f.id === selectedFolderId) : -1;
          const next = idx === -1 ? undefined : col.items[idx + dir];
          if (next) setSelectedFolderId(next.id);
          else if (dir === 1 && idx !== -1) {
            // Past the last folder the column's own files continue the list,
            // exactly as they read on screen — the only way a keyboard
            // reaches an intermediate column's rows at all.
            const colFiles = col ? columnFiles.get(col.key)?.files : undefined;
            if (colFiles?.length) setColumnsFocusedFileId(colFiles[0].id);
          }
        }
        break;
      }
      case 'ArrowRight': {
        e.preventDefault();
        if (inFiles) break;
        if (!columnsFilterActive && selectedNode && selectedNode.children.length > 0) {
          setSelectedFolderId(selectedNode.children[0].id);
        } else if (filteredAndSortedFiles.length > 0) {
          setColumnsFocusedFileId(filteredAndSortedFiles[0].id);
        }
        break;
      }
      case 'ArrowLeft': {
        e.preventDefault();
        if (inFiles) {
          setColumnsFocusedFileId(null);
          break;
        }
        // With a filter active the folder columns are hidden — don't move a
        // selection the user can't see.
        if (columnsFilterActive) break;
        if (folderPath.length > 1) {
          setSelectedFolderId(folderPath[folderPath.length - 2].id);
        } else if (folderPath.length === 1) {
          // Ascending past the top level: keep the bucket the user was
          // navigating — the tree can select a folder from either bucket
          // without touching topLevelView, and falling back to a stale one
          // would teleport the root column to the other folder set.
          setTopLevelView(folderPath[0].is_external ? 'external' : 'internal');
          setSelectedFolderId(null);
        } else if (selectedFolderId !== null) {
          // Selection not in the tree (e.g. a deep link to a deleted
          // folder): give the keyboard an escape hatch back to the root.
          setSelectedFolderId(null);
        }
        break;
      }
      // Enter and Space preventDefault BEFORE the inFiles check: DOM focus
      // may still sit on the last-clicked folder button, and the browser's
      // default activation would re-click it — snapping the selection back
      // to a folder the arrows have long left (Space would scroll the page).
      case 'Enter': {
        e.preventDefault();
        if (!inFiles) {
          // Enter means "open the selected thing": on a folder that is
          // descending, exactly like ArrowRight.
          if (!columnsFilterActive && selectedNode && selectedNode.children.length > 0) {
            setSelectedFolderId(selectedNode.children[0].id);
          } else if (filteredAndSortedFiles.length > 0) {
            setColumnsFocusedFileId(filteredAndSortedFiles[0].id);
          }
          break;
        }
        const file = focusedFileList.find((f) => f.id === columnsFocusedFileId);
        if (file) openColumnFile(file);
        break;
      }
      case ' ': {
        e.preventDefault();
        if (!inFiles) break;
        if (columnsFocusedFileId !== null) handleFileSelect(columnsFocusedFileId);
        break;
      }
    }
  };

  return (
    <div
      className="p-4 md:p-8 min-h-[calc(100vh-64px)] lg:h-[calc(100vh-64px)] flex flex-col relative"
      {...dragHandlers}
    >
      {/* Drag & Drop Overlay — page-wide file upload (#1510) */}
      {isDraggingOver && (
        <div className="fixed inset-0 z-50 bg-bambu-dark/90 flex items-center justify-center pointer-events-none">
          <div className="border-4 border-dashed border-bambu-green rounded-xl p-12 text-center">
            <Upload className="w-16 h-16 mx-auto mb-4 text-bambu-green" />
            <p className="text-2xl font-semibold text-white mb-2">{t('fileManager.dropFilesHere')}</p>
            <p className="text-bambu-gray">{t('fileManager.releaseToUpload')}</p>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <FolderOpen className="w-7 h-7 text-bambu-green" />
            {t('fileManager.title')}
          </h1>
          <p className="text-bambu-gray mt-1">
            {t('fileManager.subtitle')}
          </p>
        </div>
        {/* The row must wrap. It carries eight controls at full permission and
            the labels are long in several locales; without `flex-wrap` the
            overflow is absorbed by each Button breaking its own label over two
            or three lines, which makes the header taller than a wrapped row
            would and reads as broken. `whitespace-nowrap` on the buttons is
            the other half: it keeps a label from being the thing that gives. */}
        <div className="flex flex-wrap items-center justify-end gap-2" data-testid="file-manager-actions">
          {/* View mode toggle */}
          <div className="flex items-center bg-bambu-dark rounded-lg p-1">
            <button
              onClick={() => handleViewModeChange('grid')}
              className={`p-1.5 rounded transition-colors ${
                viewMode === 'grid' ? 'bg-bambu-dark-secondary text-white' : 'text-bambu-gray hover:text-white'
              }`}
              title={t('fileManager.gridView')}
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
            <button
              onClick={() => handleViewModeChange('list')}
              className={`p-1.5 rounded transition-colors ${
                viewMode === 'list' ? 'bg-bambu-dark-secondary text-white' : 'text-bambu-gray hover:text-white'
              }`}
              title={t('fileManager.listView')}
            >
              <List className="w-4 h-4" />
            </button>
            <button
              onClick={() => handleViewModeChange('columns')}
              className={`p-1.5 rounded transition-colors ${
                viewMode === 'columns' ? 'bg-bambu-dark-secondary text-white' : 'text-bambu-gray hover:text-white'
              }`}
              title={t('fileManager.columnsView')}
            >
              <Columns className="w-4 h-4" />
            </button>
          </div>
          {/* Sidebar toggle. Only offered from lg upwards, which is where the
              tree exists at all — below it the folders are a <select>.
              Toggle-button pattern: the name stays constant and aria-pressed
              carries the state, so "pressed" means the sidebar is on screen —
              which in the columns view it is, whatever the stored preference
              says. The title still names the action the click performs. */}
          {/* Only where there are tiles to hide: the columns view draws its
              folders in its own panes, and a control that does nothing there
              is worse than no control. */}
          {viewMode !== 'columns' && (
            <button
              type="button"
              onClick={handleToggleFolderTiles}
              aria-pressed={!folderTilesHidden}
              aria-label={t('fileManager.folderTilesToggle.label')}
              title={folderTilesHidden ? t('fileManager.folderTilesToggle.show') : t('fileManager.folderTilesToggle.hide')}
              data-testid="toggle-folder-tiles"
              className="hidden md:flex items-center p-2 rounded-lg bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
            >
              <LayoutGrid className={`w-4 h-4 ${folderTilesHidden ? 'opacity-50' : ''}`} />
            </button>
          )}
          <button
            type="button"
            onClick={handleToggleSidebar}
            aria-pressed={folderSidebarVisible}
            aria-label={t('fileManager.sidebarToggle.label')}
            title={folderSidebarVisible ? t('fileManager.sidebarToggle.hide') : t('fileManager.sidebarToggle.show')}
            data-testid="toggle-folder-sidebar"
            className="hidden md:flex items-center p-2 rounded-lg bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
          >
            {folderSidebarVisible ? <PanelLeftClose className="w-4 h-4" /> : <PanelLeftOpen className="w-4 h-4" />}
          </button>
          <Button
            variant="secondary"
            className="whitespace-nowrap"
            onClick={() => batchThumbnailMutation.mutate()}
            disabled={
              batchThumbnailMutation.isPending ||
              previewBatch.length > 0 ||
              !hasAnyPermission('library:update_own', 'library:update_all')
            }
            title={!hasAnyPermission('library:update_own', 'library:update_all') ? t('fileManager.noPermissionGenerateThumbnail') : t('fileManager.generateThumbnailsForMissing')}
          >
            {batchThumbnailMutation.isPending || previewBatch.length > 0 ? (
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
            ) : (
              <Image className="w-4 h-4 mr-2" />
            )}
            {previewBatch.length > 0 && previewBatchProgress
              ? t('fileManager.renderingPreviews', {
                  done: previewBatchProgress.done,
                  total: previewBatchProgress.total,
                })
              : t('fileManager.generateThumbnails')}
          </Button>
          <Button
            variant="secondary"
            className="whitespace-nowrap"
            onClick={() => setShowExternalFolderModal(true)}
            disabled={!hasPermission('library:upload')}
            title={!hasPermission('library:upload') ? t('fileManager.noPermissionCreateFolder') : t('fileManager.linkExternalFolder')}
          >
            <FolderSymlink className="w-4 h-4 mr-2" />
            {t('fileManager.linkExternal')}
          </Button>
          <Button
            variant="secondary"
            className="whitespace-nowrap"
            onClick={() => setShowNewFolderModal(true)}
            disabled={!hasPermission('library:upload')}
            title={!hasPermission('library:upload') ? t('fileManager.noPermissionCreateFolder') : undefined}
          >
            <FolderPlus className="w-4 h-4 mr-2" />
            {t('fileManager.newFolder')}
          </Button>
          <Button
            variant="secondary"
            className="whitespace-nowrap"
            onClick={() => setShowTagsModal(true)}
            title={t('fileManager.tags.manageTitle')}
          >
            <TagIcon className="w-4 h-4 mr-2" />
            {t('fileManager.tags.manage')}
          </Button>
          {hasPermission('library:purge') && (
            <Button
              variant="secondary"
              className="whitespace-nowrap"
              onClick={() => setShowPurgeModal(true)}
              title={t('libraryPurge.headerTooltip')}
            >
              <Trash2 className="w-4 h-4 mr-2" />
              {t('libraryPurge.headerButton')}
            </Button>
          )}
          {(hasAnyPermission('library:delete_own', 'library:delete_all')) && (
            <Link
              to="/files/trash"
              className="inline-flex items-center whitespace-nowrap px-3 py-1.5 text-sm rounded bg-bambu-dark-secondary text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
              title={t('libraryTrash.headerTooltip')}
            >
              <Trash2 className="w-4 h-4 mr-2" />
              {t('libraryTrash.headerButton')}
              {typeof trashCount === 'number' && trashCount > 0 && (
                <span className="ml-1.5 px-1.5 py-0.5 text-xs rounded-full bg-bambu-green/20 text-bambu-green">
                  {trashCount}
                </span>
              )}
            </Link>
          )}
          <Button
            className="whitespace-nowrap"
            onClick={() => setShowUploadModal(true)}
            disabled={!hasPermission('library:upload')}
            title={!hasPermission('library:upload') ? t('fileManager.noPermissionUpload') : undefined}
          >
            <Upload className="w-4 h-4 mr-2" />
            {t('common.upload')}
          </Button>
        </div>
      </div>

      {/* Disk space warning */}
      {isDiskSpaceLow && stats && settings && (
        <div className="flex items-center gap-3 mb-4 p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg">
          <AlertTriangle className="w-5 h-5 text-amber-500 flex-shrink-0" />
          <div className="flex-1">
            <p className="text-sm text-amber-500 font-medium">{t('fileManager.lowDiskSpaceWarning')}</p>
            <p className="text-xs text-amber-500/80">
              {t('fileManager.lowDiskSpaceDetails', { free: formatFileSize(stats.disk_free_bytes), total: formatFileSize(stats.disk_total_bytes), threshold: settings.library_disk_warning_gb })}
            </p>
          </div>
        </div>
      )}

      {/* Stats bar */}
      {stats && (
        <div className="flex flex-wrap items-center gap-3 sm:gap-6 mb-6 p-3 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary">
          <div className="flex items-center gap-2 text-sm">
            <File className="w-4 h-4 text-bambu-green" />
            <span className="text-bambu-gray">{t('fileManager.files')}:</span>
            <span className="text-white font-medium">{stats.total_files}</span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <FolderOpen className="w-4 h-4 text-blue-600 dark:text-blue-400" />
            <span className="text-bambu-gray">{t('fileManager.folders')}:</span>
            <span className="text-white font-medium">{stats.total_folders}</span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <HardDrive className="w-4 h-4 text-amber-600 dark:text-amber-400" />
            <span className="text-bambu-gray">{t('fileManager.size')}:</span>
            <span className="text-white font-medium">{formatFileSize(stats.total_size_bytes)}</span>
          </div>
          <div className="flex items-center gap-2 text-sm sm:ml-auto">
            <span className="text-bambu-gray">{t('fileManager.free')}:</span>
            <span className={`font-medium ${isDiskSpaceLow ? 'text-amber-500' : 'text-white'}`}>
              {formatFileSize(stats.disk_free_bytes)}
            </span>
          </div>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 flex flex-col lg:flex-row gap-4 lg:gap-6 min-h-0">
        {/* Mobile folder selector */}
        <div className="lg:hidden">
          <select
            value={selectedFolderId !== null ? String(selectedFolderId) : `__top:${topLevelView}`}
            onChange={(e) => {
              const v = e.target.value;
              if (v.startsWith('__top:')) {
                selectTopLevelBucket(v.slice('__top:'.length) as 'internal' | 'external');
              } else {
                setSelectedFolderId(parseInt(v, 10));
              }
            }}
            className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg px-3 py-2.5 text-white focus:outline-none focus:border-bambu-green"
          >
            <option value="__top:internal">📁 {t('fileManager.allFiles')}</option>
            {folders?.some((f) => f.is_external) && (
              <option value="__top:external">🔗 {t('fileManager.allExternal')}</option>
            )}
            {sortedFolders && (() => {
              // Flatten folder tree for mobile selector
              const flattenFolders = (items: LibraryFolderTree[], depth = 0): { id: number; name: string; number: string | null; fileCount: number; depth: number }[] => {
                const result: { id: number; name: string; number: string | null; fileCount: number; depth: number }[] = [];
                for (const item of items) {
                  result.push({ id: item.id, name: item.name, number: item.number, fileCount: item.file_count, depth });
                  if (item.children.length > 0) {
                    result.push(...flattenFolders(item.children, depth + 1));
                  }
                }
                return result;
              };
              return flattenFolders(sortedFolders).map((folder) => (
                <option key={folder.id} value={folder.id}>
                  {'│ '.repeat(folder.depth)}📂 {folderText(folder)} {folder.fileCount > 0 ? `(${folder.fileCount})` : ''}
                </option>
              ));
            })()}
          </select>
        </div>

        {/* Folder sidebar - resizable, hidden on mobile, and switchable off
            from the toolbar. Body left at its original indentation so the
            toggle stays a two-line diff in a file several branches touch. */}
        {folderSidebarVisible && (
        <div
          ref={sidebarRef}
          data-testid="folder-sidebar"
          className="hidden lg:flex flex-shrink-0 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary overflow-hidden flex-col relative"
          style={{ width: `${sidebarWidth}px` }}
        >
          {/* Resize handle - drag to resize, double-click to reset */}
          <div
            className={`absolute right-0 top-0 bottom-0 w-1.5 cursor-col-resize z-10 group/resize flex items-center justify-center transition-colors ${
              isResizing ? 'bg-bambu-green' : 'hover:bg-bambu-green/50'
            }`}
            onMouseDown={(e) => {
              e.preventDefault();
              setIsResizing(true);
            }}
            onDoubleClick={() => {
              setSidebarWidth(256); // Reset to default w-64
              localStorage.setItem('library-sidebar-width', '256');
            }}
            title={t('fileManager.dragToResizeTooltip')}
          >
            {/* Grip dots */}
            <div className={`flex flex-col gap-1 opacity-0 group-hover/resize:opacity-100 transition-opacity ${isResizing ? 'opacity-100' : ''}`}>
              <div className="w-0.5 h-0.5 rounded-full bg-white/70" />
              <div className="w-0.5 h-0.5 rounded-full bg-white/70" />
              <div className="w-0.5 h-0.5 rounded-full bg-white/70" />
            </div>
          </div>
          <div className="p-3 border-b border-bambu-dark-tertiary flex items-center justify-between">
            <h2 className="text-sm font-medium text-white">{t('fileManager.folders')}</h2>
            <div className="flex items-center gap-1">
              {/* Folder tree sort (#1770). Dropdown drives the comparator;
                  direction button flips asc/desc. Both persist to localStorage
                  on change so the choice survives reloads. */}
              <select
                value={folderSortField}
                onChange={(e) => {
                  const v = e.target.value === 'activity' ? 'activity' : 'name';
                  setFolderSortField(v);
                  localStorage.setItem('library-folder-sort-field', v);
                }}
                className="text-xs px-1 py-0.5 rounded bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray focus:outline-none focus:border-bambu-green"
                title={t('fileManager.folderSort')}
                aria-label={t('fileManager.folderSort')}
              >
                <option value="name">{t('fileManager.folderSortByName')}</option>
                <option value="activity">{t('fileManager.folderSortByActivity')}</option>
              </select>
              <button
                onClick={() => {
                  const newValue = folderSortDirection === 'asc' ? 'desc' : 'asc';
                  setFolderSortDirection(newValue);
                  localStorage.setItem('library-folder-sort-direction', newValue);
                }}
                className="text-bambu-gray hover:text-white hover:bg-bambu-dark p-1 rounded transition-colors"
                title={folderSortDirection === 'asc' ? t('fileManager.ascending') : t('fileManager.descending')}
                aria-label={folderSortDirection === 'asc' ? t('fileManager.ascending') : t('fileManager.descending')}
              >
                {folderSortDirection === 'asc' ? <SortAsc className="w-3.5 h-3.5" /> : <SortDesc className="w-3.5 h-3.5" />}
              </button>
              <button
                onClick={() => {
                  const newValue = !collapseFoldersByDefault;
                  setCollapseFoldersByDefault(newValue);
                  localStorage.setItem('library-collapse-folders', String(newValue));
                }}
                className={`text-xs px-1.5 py-0.5 rounded transition-colors ${
                  collapseFoldersByDefault
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : 'text-bambu-gray hover:text-white hover:bg-bambu-dark'
                }`}
                title={collapseFoldersByDefault ? t('fileManager.expandFoldersByDefault') : t('fileManager.collapseFoldersByDefault')}
              >
                {t('fileManager.collapse')}
              </button>
              <button
                onClick={() => {
                  const newValue = !wrapFolderNames;
                  setWrapFolderNames(newValue);
                  localStorage.setItem('library-wrap-folders', String(newValue));
                }}
                className={`text-xs px-1.5 py-0.5 rounded transition-colors ${
                  wrapFolderNames
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : 'text-bambu-gray hover:text-white hover:bg-bambu-dark'
                }`}
                title={wrapFolderNames ? t('fileManager.disableTextWrapping') : t('fileManager.enableTextWrapping')}
              >
                {t('fileManager.wrap')}
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {/* All Files = the user's own uploaded / managed-storage files
                only. External folders are surfaced separately below to keep
                a linked NAS from drowning the user's own uploads (#1621). */}
            <div
              className={`flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer transition-colors ${
                selectedFolderId === null && topLevelView === 'internal'
                  ? 'bg-bambu-green/20 text-bambu-green'
                  : 'hover:bg-bambu-dark text-white'
              }`}
              onClick={() => selectTopLevelBucket('internal')}
            >
              <FileBox className="w-4 h-4" />
              <span className="text-sm">{t('fileManager.allFiles')}</span>
            </div>

            {/* External (combined) — only shown when at least one external
                folder is linked. Single folder users don't need a combined
                view; clicking the individual folder is just as fast. */}
            {folders?.some((f) => f.is_external) && (
              <div
                className={`flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer transition-colors ${
                  selectedFolderId === null && topLevelView === 'external'
                    ? 'bg-bambu-green/20 text-bambu-green'
                    : 'hover:bg-bambu-dark text-white'
                }`}
                onClick={() => selectTopLevelBucket('external')}
              >
                <FolderSymlink className="w-4 h-4 text-purple-600 dark:text-purple-400" />
                <span className="text-sm">{t('fileManager.allExternal')}</span>
              </div>
            )}

            {/* Folder tree — re-key on the collapse toggle so flipping it
                remounts every FolderTreeItem, which re-reads defaultExpanded
                and makes the preference take effect immediately. */}
            {sortedFolders?.map((folder) => (
              <FolderTreeItem
                key={`${folder.id}-${collapseFoldersByDefault ? 'c' : 'e'}`}
                folder={folder}
                selectedFolderId={selectedFolderId}
                onSelect={setSelectedFolderId}
                onDownloadFolder={handleDownloadFolder}
                onDelete={(id) => setDeleteConfirm({ type: 'folder', id })}
                onLink={setLinkFolder}
                onRename={(f) => setRenameItem({ type: 'folder', id: f.id, name: f.name, number: f.number })}
                onCopyPath={handleCopyFolderPath}
                wrapNames={wrapFolderNames}
                defaultExpanded={!collapseFoldersByDefault}
                showModified={showModified}
                hasPermission={hasPermission}
                t={t}
              />
            ))}
          </div>
        </div>
        )}

        {/* Files area + README rail (#2520 item 2). On wide screens the
            README docks as a collapsible right-hand column (rendered after
            the files column, below) so it no longer steals vertical space
            from the file list; on narrow screens it stacks above the list
            via `order-first` and the page itself scrolls. */}
        <div className="flex-1 flex flex-col lg:flex-row min-w-0 min-h-0 gap-4 lg:gap-6">
        <div className="flex-1 flex flex-col min-w-0 min-h-0">
          {/* Tag filter rail (#1268). Lists every catalog tag as a togglable
              chip — active chips are filled green and show an X, inactive
              chips are outlined and toggle ON when clicked. Clicking an active
              chip removes it from the filter. Hidden entirely when the
              catalog is empty so brand-new installs don't see a stray rail. */}
          {tagCatalog.length > 0 && (
            <div className="mb-3 flex flex-wrap items-center gap-2 p-2 sm:p-3 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary">
              <span className="text-xs text-bambu-gray font-medium shrink-0">
                {t('fileManager.tags.filterLabel')}
              </span>
              {tagCatalog.map((tg) => {
                const active = selectedTagIds.includes(tg.id);
                return (
                  <button
                    key={tg.id}
                    type="button"
                    onClick={() => toggleTagFilter(tg.id)}
                    className={
                      active
                        ? 'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-bambu-green/20 text-bambu-green border border-bambu-green/40 hover:bg-bambu-green/30 transition-colors'
                        : 'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-bambu-dark text-bambu-gray border border-bambu-dark-tertiary hover:text-white hover:border-bambu-green/40 transition-colors'
                    }
                    title={tg.name}
                  >
                    <TagIcon className="w-3 h-3" />
                    <span>{tg.name}</span>
                    {active && <X className="w-3 h-3" />}
                  </button>
                );
              })}
              {selectedTagIds.length > 0 && (
                <button
                  type="button"
                  onClick={() => setSelectedTagIds([])}
                  className="ml-auto text-xs text-bambu-gray hover:text-white shrink-0"
                >
                  {t('fileManager.tags.clearAll')}
                </button>
              )}
            </div>
          )}
          {/* External folder info bar */}
          {selectedFolder?.is_external && (
            <div className="flex items-center gap-3 mb-4 p-3 bg-purple-50 dark:bg-purple-500/10 border border-purple-300 dark:border-purple-500/30 rounded-lg">
              <FolderSymlink className="w-5 h-5 text-purple-600 dark:text-purple-400 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-purple-700 dark:text-purple-300">{t('fileManager.externalFolder')}</span>
                  {selectedFolder.external_readonly && (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400 flex items-center gap-1">
                      <Lock className="w-3 h-3" />
                      {t('fileManager.readOnly')}
                    </span>
                  )}
                </div>
                <p className="text-xs text-bambu-gray truncate font-mono" title={selectedFolder.external_path || ''}>
                  {selectedFolder.external_path}
                </p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => selectedFolderId && scanExternalFolderMutation.mutate(selectedFolderId)}
                disabled={scanExternalFolderMutation.isPending}
                title={t('fileManager.scanFolder')}
              >
                {scanExternalFolderMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
                <span className="ml-1.5">{t('fileManager.scanFolder')}</span>
              </Button>
            </div>
          )}
          {/* Search, Filter, Sort toolbar - sticky on mobile for easier access */}
          {showFilterCard && (
            <div
              className="flex flex-wrap items-center gap-2 sm:gap-3 mb-4 p-2 sm:p-3 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary sticky top-0 z-10 lg:static"
              data-testid="library-filter-card"
            >
              {/* Select all / Deselect all leads the card: the page used to
                  spend a whole bordered bar on this one button. */}
              {filteredAndSortedFiles.length > 0 &&
                (selectedFiles.length === filteredAndSortedFiles.length && selectedFiles.length > 0 ? (
                  <Button variant="secondary" size="sm" onClick={handleDeselectAll}>
                    <Square className="w-4 h-4 sm:mr-1" />
                    <span className="hidden sm:inline">{t('fileManager.deselectAll')}</span>
                  </Button>
                ) : (
                  <Button variant="secondary" size="sm" onClick={handleSelectAll}>
                    <CheckSquare className="w-4 h-4 sm:mr-1" />
                    <span className="hidden sm:inline">{t('fileManager.selectAll')}</span>
                  </Button>
                ))}

              {/* Search */}
              <div className="relative w-full sm:w-auto sm:flex-1 sm:max-w-xs">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray" />
                <input
                  type="text"
                  placeholder={t('fileManager.searchFiles')}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full pl-9 pr-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-sm text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green"
                />
                {searchExpandsSubfolders && (
                  <span
                    className="absolute -bottom-4 left-0 text-[10px] text-bambu-gray whitespace-nowrap"
                    title={t('fileManager.searchSubfoldersHint')}
                  >
                    {t('fileManager.searchSubfoldersHint')}
                  </span>
                )}
              </div>

              {/* Type filter */}
              <div className="flex items-center gap-2">
                <Filter className="w-4 h-4 text-bambu-gray hidden sm:block" />
                <select
                  value={filterType}
                  onChange={(e) => setFilterType(e.target.value)}
                  className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-bambu-green"
                >
                  <option value="all">{t('fileManager.allTypes')}</option>
                  {fileTypes.map((type) => (
                    <option key={type} value={type}>
                      {type.toUpperCase()}
                    </option>
                  ))}
                </select>
              </div>

              {/* Username filter with autocomplete - only show when auth is enabled */}
              {authEnabled && (
                <div className="relative">
                  <input
                    type="text"
                    placeholder={t('fileManager.filterByUser', { defaultValue: 'Filter by user' })}
                    value={filterUsername}
                    onChange={(e) => setFilterUsername(e.target.value)}
                    list="usernames-list"
                    className={`w-32 sm:w-40 px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-sm text-white placeholder-bambu-gray focus:outline-none focus:border-bambu-green ${filterUsername ? 'pr-7' : ''}`}
                    style={filterUsername ? { WebkitAppearance: 'none', MozAppearance: 'textfield' } : undefined}
                  />
                  {filterUsername && (
                    <button
                      onClick={() => setFilterUsername('')}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white z-10"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  )}
                  <datalist id="usernames-list">
                    {users?.map((user) => (
                      <option key={user.id} value={user.username} />
                    ))}
                  </datalist>
                </div>
              )}

              {/* Sort */}
              <div className="flex items-center gap-2">
                <select
                  value={rootRecentView ? 'date' : sortField}
                  disabled={rootRecentView}
                  title={rootRecentView ? t('fileManager.sortedByRecent') : undefined}
                  onChange={(e) => {
                    const newField = e.target.value as SortField;
                    setSortField(newField);
                    localStorage.setItem('library-sort-field', newField);
                  }}
                  className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-bambu-green disabled:opacity-50"
                >
                  <option value="name">{t('common.name')}</option>
                  <option value="date">{t('common.date')}</option>
                  <option value="size">{t('fileManager.size')}</option>
                  <option value="type">{t('common.type')}</option>
                  <option value="prints">{t('fileManager.prints')}</option>
                </select>
                <button
                  onClick={() => setSortDirection((d) => {
                    const newDir = d === 'asc' ? 'desc' : 'asc';
                    localStorage.setItem('library-sort-direction', newDir);
                    return newDir;
                  })}
                  disabled={rootRecentView}
                  className="p-1.5 rounded bg-bambu-dark border border-bambu-dark-tertiary hover:border-bambu-green transition-colors disabled:opacity-50"
                  title={
                    rootRecentView
                      ? t('fileManager.sortedByRecent')
                      : sortDirection === 'asc'
                        ? t('fileManager.ascending')
                        : t('fileManager.descending')
                  }
                >
                  {!rootRecentView && sortDirection === 'asc' ? (
                    <SortAsc className="w-4 h-4 text-white" />
                  ) : (
                    <SortDesc className="w-4 h-4 text-white" />
                  )}
                </button>
                <button
                  onClick={() => setShowModified((v) => {
                    const next = !v;
                    localStorage.setItem('library-show-modified', String(next));
                    return next;
                  })}
                  className={`p-1.5 rounded bg-bambu-dark border transition-colors ${
                    showModified ? 'border-bambu-green text-bambu-green' : 'border-bambu-dark-tertiary text-white hover:border-bambu-green'
                  }`}
                  title={showModified ? t('fileManager.hideModified') : t('fileManager.showModified')}
                  aria-pressed={showModified}
                >
                  <CalendarClock className="w-4 h-4" />
                </button>
              </div>

              {/* Results count */}
              {(searchQuery || filterType !== 'all' || filterUsername) && (
                <span className="text-sm text-bambu-gray hidden sm:inline">
                  {searchQuery.trim()
                    ? t('fileManager.search.resultsCount', {
                        showing: filteredAndSortedFiles.length,
                        total: files?.length ?? 0,
                        folders: folderSearchMatches.length,
                      })
                    : t('fileManager.resultsCount', {
                        showing: filteredAndSortedFiles.length,
                        total: files?.length ?? 0,
                      })}
                </span>
              )}

              {/* Selection actions: a wrapped second row of this same card
                  rather than a bar of its own. `w-full` makes the flex row
                  break, and living here keeps the actions reachable for a
                  selection ticked in a column whose folder is not the
                  selected one — that pane can be empty. */}
              {selectedFiles.length > 0 && (
                <div
                  className="w-full flex flex-wrap items-center gap-2 pt-2 border-t border-bambu-dark-tertiary"
                  data-testid="selection-actions"
                >
                  <span className="text-sm text-bambu-gray">
                    {t('fileManager.selected', { count: selectedFiles.length })}
                  </span>
                  <div className="hidden sm:block flex-1" />
                  {previewSelection && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => openPreview(previewSelection)}
                      disabled={!hasPermission('library:read')}
                      title={!hasPermission('library:read') ? t('fileManager.noPermissionPreview') : undefined}
                    >
                      <Eye className="w-4 h-4 sm:mr-1" />
                      <span className="hidden sm:inline">{t('fileManager.preview.open')}</span>
                    </Button>
                  )}
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleDownloadSelection}
                    disabled={!hasPermission('library:read')}
                    title={!hasPermission('library:read') ? t('fileManager.noPermissionPreview') : undefined}
                  >
                    <Download className="w-4 h-4 sm:mr-1" />
                    <span className="hidden sm:inline">
                      {selectedFiles.length > 1 ? t('fileManager.downloadZip') : t('common.download')}
                    </span>
                  </Button>
                  {/* Print used to disappear the moment a second sliced file was
                      selected. Selecting several is now how you say "same job,
                      different printers" (#671) — one queue item, whichever
                      machine frees up first. */}
                  {selectedSlicedFiles.length >= 1 && (
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => setPrintFile(selectedSlicedFiles[0])}
                      disabled={!hasPermission('queue:create')}
                      title={!hasPermission('queue:create') ? t('fileManager.noPermissionAddToQueue') : undefined}
                    >
                      <Printer className="w-4 h-4 sm:mr-1" />
                      <span className="hidden sm:inline">
                        {selectedSlicedFiles.length > 1
                          ? t('fileManager.variants.printAlternatives', { count: selectedSlicedFiles.length })
                          : t('common.print')}
                      </span>
                    </Button>
                  )}
                  {selectedSlicedFiles.length >= 2 && !selectedSlicedFiles.some(f => f.variant_group_id) && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => groupAsVersionsMutation.mutate(selectedSlicedFiles.map(f => f.id))}
                      disabled={
                        groupAsVersionsMutation.isPending
                        || !hasAnyPermission('library:update_own', 'library:update_all')
                      }
                      title={t('fileManager.variants.groupTooltip')}
                    >
                      <Layers className="w-4 h-4 sm:mr-1" />
                      <span className="hidden sm:inline">{t('fileManager.variants.groupAction')}</span>
                    </Button>
                  )}
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setShowMoveModal(true)}
                    disabled={!hasAnyPermission('library:update_own', 'library:update_all')}
                    title={!hasAnyPermission('library:update_own', 'library:update_all') ? t('fileManager.noPermissionMoveFiles') : undefined}
                  >
                    <MoveRight className="w-4 h-4 sm:mr-1" />
                    <span className="hidden sm:inline">{t('common.move')}</span>
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setShowBulkTagsModal(true)}
                    disabled={!hasAnyPermission('library:update_own', 'library:update_all')}
                    title={!hasAnyPermission('library:update_own', 'library:update_all') ? t('fileManager.tags.noPermission') : t('fileManager.tags.bulkTooltip')}
                  >
                    <TagIcon className="w-4 h-4 sm:mr-1" />
                    <span className="hidden sm:inline">{t('fileManager.tags.tagAction')}</span>
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => {
                      if (selectedFiles.length === 1) {
                        setDeleteConfirm({ type: 'file', id: selectedFiles[0] });
                      } else {
                        setDeleteConfirm({ type: 'bulk', id: 0, count: selectedFiles.length });
                      }
                    }}
                    disabled={!hasAnyPermission('library:delete_own', 'library:delete_all')}
                    title={!hasAnyPermission('library:delete_own', 'library:delete_all') ? t('fileManager.noPermissionDeleteFiles') : undefined}
                  >
                    <Trash2 className="w-4 h-4 sm:mr-1" />
                    <span className="hidden sm:inline">{t('common.delete')}</span>
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleDeselectAll}
                  >
                    <X className="w-4 h-4 sm:mr-1" />
                    <span className="hidden sm:inline">{t('common.clear')}</span>
                  </Button>
                </div>
              )}
            </div>
          )}

          {/* Path bar: where you are, and one click back to any ancestor.
              Rendered in every view mode, driven by the same selection the
              sidebar, the tiles and the columns all write to — but not at the
              root with nothing below it, where the one crumb it would draw
              only repeats the name of the view you are already looking at and
              costs a row for it. */}
          {(folderPath.length > 0 || rootUnfolderedView || rootRecentView) && (
          <PathBar
            rootLabel={rootCrumbLabel}
            rootIsExternal={currentBucketIsExternal}
            path={folderPath}
            tree={bucketRootFolders}
            leafLabel={
              rootUnfolderedView
                ? t('fileManager.noFolder')
                : rootRecentView
                  ? t('fileManager.recentCrumb')
                  : undefined
            }
            onSelectRoot={selectPathRoot}
            onSelectFolder={selectFolderFromChrome}
            t={t}
          />
          )}

          {/* With the tree switched off the content area carries the way down
              (and the bucket switch) — the path bar only ever walks up. The
              columns view needs none of it: its own panes are the way down. */}
          {!folderSidebarVisible && viewMode !== 'columns' && (
            <ContentFolderNav
              folders={currentFolderChildren}
              variant={viewMode === 'list' ? 'list' : 'grid'}
              showBuckets={Boolean(folders?.some((f) => f.is_external))}
              bucketIsExternal={currentBucketIsExternal}
              atRoot={selectedFolderId === null}
              onSelectFolder={selectFolderFromChrome}
              onSelectBucket={selectTopLevelBucket}
              onDownloadFolder={handleDownloadFolder}
              onCopyPath={handleCopyFolderPath}
              onDeleteFolder={(id) => setDeleteConfirm({ type: 'folder', id })}
              onLinkFolder={setLinkFolder}
              onRenameFolder={(f) => setRenameItem({ type: 'folder', id: f.id, name: f.name, number: f.number })}
              hasPermission={hasPermission}
              t={t}
            />
          )}

          {/* Folder hits for the active search, above the file results. A
              folder whose name matched used to be invisible, which is exactly
              wrong when the thing you are looking for IS a folder. */}
          {folderSearchMatches.length > 0 && (
            <div className="mb-4" data-testid="folder-search-results">
              <h3 className="mb-2 text-sm font-medium text-white">
                {t('fileManager.search.foldersHeading', { count: folderSearchMatches.length })}
              </h3>
              <div className="bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary divide-y divide-bambu-dark-tertiary overflow-hidden">
                {folderSearchMatches.map(({ folder, path }) => (
                  <button
                    key={folder.id}
                    type="button"
                    onClick={() => selectSearchedFolder(folder.id)}
                    className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-bambu-dark transition-colors"
                  >
                    {folder.is_external ? (
                      <FolderSymlink className="w-5 h-5 flex-shrink-0 text-purple-600 dark:text-purple-400" />
                    ) : (
                      <FolderOpen className="w-5 h-5 flex-shrink-0 text-bambu-green" />                    )}
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5 min-w-0 text-sm text-white">
                        <FolderNumber number={folder.number} t={t} />
                        <span className="truncate">{folder.name}</span>
                      </span>
                      <span className="block text-xs text-bambu-gray truncate">
                        {path.length > 0
                          ? path.join(' › ')
                          : folder.is_external
                            ? t('fileManager.allExternal')
                            : t('fileManager.allFiles')}
                      </span>
                    </span>
                    {folder.file_count > 0 && (
                      <span className="text-xs text-bambu-gray flex-shrink-0">{folder.file_count}</span>
                    )}
                  </button>
                ))}
              </div>
              <h3 className="mt-4 mb-2 text-sm font-medium text-white">
                {t('fileManager.search.filesHeading', { count: filteredAndSortedFiles.length })}
              </h3>
              {filteredAndSortedFiles.length === 0 && (
                <p className="text-sm text-bambu-gray">{t('fileManager.noMatchingFiles')}</p>
              )}
            </div>
          )}

          {/* File grid/list. The columns view is exempt from the full-pane
              loading swap: every keyboard descent into an uncached folder
              flips filesLoading for one round-trip, and replacing the pane
              would strip its tabindex mid-keystroke — the focus-fixup rule
              then drops focus to <body> and kills keyboard navigation. The
              pane stays mounted and only its files pane shows the spinner. */}
          {isLoading && viewMode !== 'columns' ? (
            <div className="flex-1 flex items-center justify-center">
              <div className="flex flex-col items-center gap-3">
                <Loader2 className="w-8 h-8 animate-spin text-bambu-green" />
                <p className="text-sm text-bambu-gray">{t('fileManager.loadingFiles')}</p>
              </div>
            </div>
          ) : viewMode === 'columns' ? (
            /* Miller columns (macOS-Finder style): one column per folder level,
               the rightmost pane lists the selected folder's files. Unlike the
               grid/list branches this renders even for an "empty" folder — an
               empty files pane next to navigable folder columns is the whole
               point of the view. */
            <div
              ref={columnsViewRef}
              tabIndex={0}
              aria-label={t('fileManager.columnsView')}
              onKeyDown={handleColumnsKeyDown}
              className="flex-1 min-h-0 bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary overflow-x-auto outline-none focus-visible:ring-1 focus-visible:ring-bambu-green/50"
              data-testid="columns-view"
            >
              <div className="h-full min-h-[16rem] flex divide-x divide-bambu-dark-tertiary">
                {!columnsFilterActive && folderColumns.map((col) => {
                  const level = columnFiles.get(col.key);
                  // A column carrying file rows needs the room the files pane
                  // has: checkbox, thumbnail and the eight-icon action strip
                  // leave nothing for the name at the folder-only width. A
                  // column that only lists folders keeps that narrow width.
                  const wide = Boolean(level && (level.loading || level.files.length > 0));
                  return (
                  <div
                    key={col.key}
                    data-testid={`columns-level-${col.key}`}
                    className={`${wide ? 'w-[26rem]' : 'w-64'} flex-shrink-0 overflow-y-auto py-1`}
                  >
                    {col.items.map((folder) => {
                      const isSelectedFolder = selectedFolderId === folder.id;
                      return (
                        /* The row is a div, not the button itself: the kebab
                           is a button of its own and buttons don't nest. */
                        <div
                          key={folder.id}
                          data-folder-id={folder.id}
                          className={`group flex items-center pr-1.5 transition-colors ${
                            col.activeId === folder.id
                              ? 'bg-bambu-green/20 text-bambu-green'
                              : isSelectedFolder
                                ? 'bg-bambu-green/10 text-white'
                                : 'text-white hover:bg-bambu-dark'
                          }`}
                        >
                          <button
                            type="button"
                            // Roving tabindex: the pane is the keyboard surface
                            // (arrows walk the folders), so Tab skips the names
                            // and goes straight to the selected row's kebab and
                            // on to the focused file's actions.
                            tabIndex={-1}
                            onClick={() => {
                              // Clear the file focus here too: re-clicking the
                              // already-selected folder bails out of the state
                              // update, so the clearing effect would not run and
                              // the arrow keys would stay stuck in the files pane.
                              setColumnsFocusedFileId(null);
                              setSelectedFolderId(folder.id);
                            }}
                            className="flex-1 min-w-0 flex items-center gap-2 pl-3 pr-1 py-2.5 text-left text-sm"
                            title={folderLabel(folder)}
                          >
                            {folder.is_external ? (
                              <FolderSymlink className="w-5 h-5 flex-shrink-0 text-purple-600 dark:text-purple-400" />
                            ) : (
                              <FolderOpen className="w-5 h-5 flex-shrink-0 text-bambu-green" />
                            )}
                            <FolderNumber number={folder.number} t={t} />
                            <span className="flex-1 truncate">{folder.name}</span>
                            {(folder.project_id || folder.archive_id) && (
                              <Link2 className="w-3.5 h-3.5 flex-shrink-0 text-blue-700 dark:text-blue-400" />
                            )}
                            {folder.is_external && folder.external_readonly && (
                              <Lock className="w-3.5 h-3.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
                            )}
                            {folder.file_count > 0 && (
                              <span className="text-xs text-bambu-gray flex-shrink-0">{folder.file_count}</span>
                            )}
                            {folder.children.length > 0 && (
                              <ChevronRightIcon className="w-4 h-4 flex-shrink-0 text-bambu-gray" />
                            )}
                          </button>
                          {/* Kebab: hover-revealed with a mouse, always there
                              without one (#2865) and on the selected row. */}
                          <FolderActionsMenu
                            folder={folder}
                            onDownloadFolder={handleDownloadFolder}
                            onDelete={(id) => setDeleteConfirm({ type: 'folder', id })}
                            onLink={setLinkFolder}
                            onRename={(f) => setRenameItem({ type: 'folder', id: f.id, name: f.name, number: f.number })}
                            onCopyPath={handleCopyFolderPath}
                            hasPermission={hasPermission}
                            revealOnHover={!isSelectedFolder}
                            tabIndex={isSelectedFolder ? 0 : -1}
                            t={t}
                          />
                        </div>
                      );
                    })}
                    {level?.loading && (
                      <div className="px-3 py-2 text-sm text-bambu-gray" aria-hidden="true">…</div>
                    )}
                    {level && !level.loading && level.files.map((file) => (
                      <ColumnFileRow
                        key={file.id}
                        file={file}
                        isSelected={selectedFiles.includes(file.id)}
                        isFocused={columnsFocusedFileId === file.id}
                        showModified={showModified}
                        thumbnailVersion={thumbnailVersions[file.id]}
                        onSelect={(f) => {
                          // Focusing a file in an intermediate column must not
                          // move the folder selection out from under it.
                          setColumnsFocusedFileId(f.id);
                          handleFileSelect(f.id);
                        }}
                        onOpen={openColumnFile}
                        actionProps={fileActionProps}
                        t={t}
                      />
                    ))}
                  </div>
                  );
                })}
                {/* Files pane — still the selected folder's contents, so a
                    leaf folder's column layout is exactly what it was. */}
                <div className="flex-1 min-w-[28rem] overflow-y-auto py-1" data-testid="columns-files-pane">
                  {filesLoading ? (
                    <div className="h-full flex items-center justify-center">
                      <Loader2 className="w-5 h-5 animate-spin text-bambu-green" />
                    </div>
                  ) : filteredAndSortedFiles.length === 0 ? (
                    <div className="h-full flex items-center justify-center px-4 text-sm text-bambu-gray text-center">
                      {(files?.length ?? 0) > 0
                        ? t('fileManager.noMatchingFiles')
                        : selectedFolderId !== null
                          ? t('fileManager.folderIsEmpty')
                          : rootTilesOnly
                            ? t('fileManager.pickAFolder')
                            : topLevelView === 'external'
                              ? t('fileManager.externalIsEmpty')
                              : t('fileManager.noFilesYet')}
                    </div>
                  ) : (
                    filteredAndSortedFiles.map((file) => (
                      <ColumnFileRow
                        key={file.id}
                        file={file}
                        isSelected={selectedFiles.includes(file.id)}
                        isFocused={columnsFocusedFileId === file.id}
                        showModified={showModified}
                        thumbnailVersion={thumbnailVersions[file.id]}
                        onSelect={(f) => {
                          setColumnsFocusedFileId(f.id);
                          handleFileSelect(f.id);
                        }}
                        onOpen={openColumnFile}
                        actionProps={fileActionProps}
                        t={t}
                      />
                    ))                  )}
                </div>
              </div>
            </div>
          ) : files?.length === 0 && !showFolderTiles && !showNoFolderEntry && folderSearchMatches.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center">
              <div className="p-4 bg-bambu-dark rounded-2xl mb-4">
                <FileBox className="w-12 h-12 text-bambu-gray/50" />
              </div>
              <h3 className="text-lg font-medium text-white mb-2">
                {selectedFolderId !== null
                  ? t('fileManager.folderIsEmpty')
                  : topLevelView === 'external'
                    ? t('fileManager.externalIsEmpty')
                    : t('fileManager.noFilesYet')}
              </h3>
              <p className="text-bambu-gray text-center max-w-md mb-6">
                {selectedFolderId !== null
                  ? t('fileManager.folderEmptyDescription')
                  : topLevelView === 'external'
                    ? t('fileManager.externalEmptyDescription')
                    : t('fileManager.noFilesDescription')}
              </p>
              <Button
                onClick={() => setShowUploadModal(true)}
                disabled={!hasPermission('library:upload')}
                title={!hasPermission('library:upload') ? t('fileManager.noPermissionUpload') : undefined}
              >
                <Plus className="w-4 h-4 mr-2" />
                {t('fileManager.uploadFiles')}
              </Button>
            </div>
          ) : (files?.length ?? 0) > 0 && filteredAndSortedFiles.length === 0 && folderSearchMatches.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center">
              <div className="p-4 bg-bambu-dark rounded-2xl mb-4">
                <Search className="w-12 h-12 text-bambu-gray/50" />
              </div>
              {/* A search looks for folders too, so say so; a plain type or
                  user filter still only ever hides files. */}
              <h3 className="text-lg font-medium text-white mb-2">
                {searchQuery.trim() ? t('fileManager.search.noMatches') : t('fileManager.noMatchingFiles')}
              </h3>
              <p className="text-bambu-gray text-center max-w-md mb-6">
                {searchQuery.trim()
                  ? t('fileManager.search.noMatchesDescription')
                  : t('fileManager.noMatchingFilesDescription')}
              </p>
              <Button variant="secondary" onClick={() => { setSearchQuery(''); setFilterType('all'); }}>
                {t('fileManager.clearFilters')}
              </Button>
            </div>
          ) : viewMode === 'grid' ? (
            <div className="flex-1 lg:overflow-y-auto">
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4">
                {/* Folders first, as regular grid items (#3019) — clicking one
                    selects it exactly like clicking it in the tree. */}
                {showFolderTiles && visibleSubfolders.map((folder) => (
                  <button
                    key={`folder-${folder.id}`}
                    onClick={() => setSelectedFolderId(folder.id)}
                    className="group relative bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary hover:border-bambu-green/50 transition-all cursor-pointer text-left"
                  >
                    <div className="aspect-square bg-bambu-dark flex items-center justify-center rounded-t-lg">
                      {folder.is_external ? (
                        <FolderSymlink className="w-16 h-16 text-purple-600/60 dark:text-purple-400/60" />
                      ) : (
                        <FolderOpen className="w-16 h-16 text-bambu-green/60" />
                      )}
                    </div>
                    <div className="p-3">
                      <h3
                        className="flex items-center gap-1.5 min-w-0 text-sm font-medium text-white"
                        title={folderLabel(folder)}
                      >
                        <FolderNumber number={folder.number} t={t} />
                        <span className="truncate">{folder.name}</span>
                      </h3>
                      <div className="mt-1 text-xs text-bambu-gray">
                        {folder.file_count > 0 ? folder.file_count : ' '}
                      </div>
                    </div>
                  </button>
                ))}
                {/* The files that belong to no folder — the one thing the
                    folders-first root would otherwise hide. */}
                {showNoFolderEntry && (
                  <button
                    data-testid="no-folder-entry"
                    onClick={() => setShowUnfoldered(true)}
                    className="group relative bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary hover:border-bambu-green/50 transition-all cursor-pointer text-left"
                  >
                    <div className="aspect-square bg-bambu-dark flex items-center justify-center rounded-t-lg">
                      <FolderX className="w-16 h-16 text-bambu-gray/50" />
                    </div>
                    <div className="p-3">
                      <h3 className="text-sm font-medium text-white truncate">{t('fileManager.noFolder')}</h3>
                      <div className="mt-1 text-xs text-bambu-gray">{unfolderedCount}</div>
                    </div>
                  </button>
                )}
                {filteredAndSortedFiles.map((file) => (
                  <FileCard
                    key={file.id}
                    file={file}
                    isSelected={selectedFiles.includes(file.id)}
                    t={t}
                    onSelect={handleFileSelect}
                    onDelete={(id) => setDeleteConfirm({ type: 'file', id })}
                    onDownload={handleDownload}
                    onPrint={setPrintFile}
                    onSlice={setSliceFile}
                    onOpenInSlicer={handleOpenInSlicer}
                    desktopSlicer={preferredSlicer}
                    onRunPipeline={setRunPipelineFile}
                    useSlicerApi={settings?.use_slicer_api ?? false}
                    canSlice={canSlice()}
                    onPreview={openPreview}
                    onRename={(f) => setRenameItem({ type: 'file', id: f.id, name: f.filename })}
                    onDetails={setDetailsFile}
                    onGenerateThumbnail={(f) => singleThumbnailMutation.mutate(f.id)}
                    onCopyPath={handleCopyFilePath}
                    onTagClick={toggleTagFilter}
                    thumbnailVersion={thumbnailVersions[file.id]}
                    hasPermission={hasPermission}
                    canModify={canModify}
                    authEnabled={authEnabled}
                    showModified={showModified}
                  />
                ))}
              </div>
            </div>
          ) : (
            <div className="flex-1 lg:overflow-y-auto">
              {/* The wrapper has overflow-x-auto so a narrow viewport scrolls
                  horizontally instead of clipping the actions column off the
                  right edge. The previous `overflow-hidden` was there for the
                  rounded corners but also swallowed any content the actions
                  column couldn't fit (#1325 follow-up reported in chat). */}
              <div className="bg-bambu-dark-secondary rounded-lg border border-bambu-dark-tertiary overflow-x-auto">
                {/* List header - hidden on mobile, show simplified on small screens.
                    Trailing actions column is fixed at 252px (sliced 3MF = 8 icons
                    ~252px). It used to be `min-content`, but header + body are sibling
                    grids that compute `min-content` independently — the header's empty
                    trailing div resolved to 0px, leaving body columns shifted left of
                    their headers. Fixed width keeps header and body in lockstep. */}
                <div className={`hidden sm:grid ${authEnabled ? 'grid-cols-[auto_1fr_120px_100px_100px_100px_minmax(0,200px)_252px]' : 'grid-cols-[auto_1fr_100px_100px_100px_minmax(0,200px)_252px]'} gap-4 px-4 py-2 bg-bambu-dark-secondary border-b border-bambu-dark-tertiary text-xs text-bambu-gray font-medium`}>
                  <div className="w-6" />
                  <div>{t('common.name')}</div>
                  {authEnabled && <div>{t('fileManager.uploadedBy', { defaultValue: 'Uploaded By' })}</div>}
                  <div>{t('common.type')}</div>
                  <div>{t('fileManager.size')}</div>
                  <div>{t('fileManager.prints')}</div>
                  <div>{t('fileManager.tags.title')}</div>
                  <div />
                </div>
                {/* Folder rows first, as regular list items (#3019). */}
                {showFolderTiles && visibleSubfolders.map((folder) => (
                  <div
                    key={`folder-${folder.id}`}
                    className={`grid ${authEnabled ? 'grid-cols-[auto_1fr_120px_100px_100px_100px_minmax(0,200px)_252px]' : 'grid-cols-[auto_1fr_100px_100px_100px_minmax(0,200px)_252px]'} gap-4 px-4 py-3 items-center border-b border-bambu-dark-tertiary cursor-pointer hover:bg-bambu-dark/50 transition-colors`}
                    onClick={() => setSelectedFolderId(folder.id)}
                  >
                    <div className="w-6" />
                    <div className="flex items-center gap-2 min-w-0">
                      {folder.is_external ? (
                        <FolderSymlink className="w-4 h-4 text-purple-600 dark:text-purple-400 flex-shrink-0" />
                      ) : (
                        <FolderOpen className="w-4 h-4 text-bambu-green flex-shrink-0" />
                      )}
                      <FolderNumber number={folder.number} t={t} />
                      <span className="text-sm text-white truncate" title={folderLabel(folder)}>{folder.name}</span>
                    </div>
                    {authEnabled && <div />}
                    <div />
                    <div className="text-sm text-bambu-gray">{folder.file_count > 0 ? folder.file_count : ''}</div>
                    <div />
                    <div />
                    <div />
                  </div>
                ))}
                {/* Same entry as the grid's tile: the files in no folder. */}
                {showNoFolderEntry && (
                  <div
                    data-testid="no-folder-entry"
                    role="button"
                    tabIndex={0}
                    className={`grid ${authEnabled ? 'grid-cols-[auto_1fr_120px_100px_100px_100px_minmax(0,200px)_252px]' : 'grid-cols-[auto_1fr_100px_100px_100px_minmax(0,200px)_252px]'} gap-4 px-4 py-3 items-center border-b border-bambu-dark-tertiary cursor-pointer hover:bg-bambu-dark/50 transition-colors`}
                    onClick={() => setShowUnfoldered(true)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setShowUnfoldered(true); } }}
                  >
                    <div className="w-6" />
                    <div className="flex items-center gap-2 min-w-0">
                      <FolderX className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                      <span className="text-sm text-white truncate">{t('fileManager.noFolder')}</span>
                    </div>
                    {authEnabled && <div />}
                    <div />
                    <div className="text-sm text-bambu-gray">{unfolderedCount}</div>
                    <div />
                    <div />
                    <div />
                  </div>
                )}
                {/* List rows */}
                {filteredAndSortedFiles.map((file) => (
                  <div
                    key={file.id}
                    className={`grid ${authEnabled ? 'grid-cols-[auto_1fr_120px_100px_100px_100px_minmax(0,200px)_252px]' : 'grid-cols-[auto_1fr_100px_100px_100px_minmax(0,200px)_252px]'} gap-4 px-4 py-3 items-center border-b border-bambu-dark-tertiary last:border-b-0 cursor-pointer hover:bg-bambu-dark/50 transition-colors ${
                      selectedFiles.includes(file.id) ? 'bg-bambu-green/10' : ''
                    }`}
                    onClick={() => handleFileSelect(file.id)}
                    // Double-click opens the preview (#2976), as in the grid.
                    onDoubleClick={() => openPreview(file)}
                  >
                    {/* Checkbox */}
                    <div className={`w-5 h-5 rounded border-2 flex items-center justify-center ${
                      selectedFiles.includes(file.id)
                        ? 'bg-bambu-green border-bambu-green'
                        : 'border-bambu-gray/50'
                    }`}>
                      {selectedFiles.includes(file.id) && <div className="w-2 h-2 bg-white rounded-sm" />}
                    </div>
                    {/* Name with thumbnail */}
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="relative group/thumb">
                        <div className="w-10 h-10 rounded bg-bambu-dark flex-shrink-0 overflow-hidden">
                          {file.thumbnail_path ? (
                            <img
                              src={`${api.getLibraryFileThumbnailUrl(file.id)}${thumbnailVersions[file.id] ? ((api.getLibraryFileThumbnailUrl(file.id).includes('?') ? '&' : '?') + `v=${thumbnailVersions[file.id]}`) : ''}`}
                              alt=""
                              className="w-full h-full object-cover"
                            />
                          ) : (
                            <div className="w-full h-full flex items-center justify-center">
                              <FileTypePlaceholderIcon fileType={file.file_type} className="w-5 h-5 text-bambu-gray/50" />
                            </div>
                          )}
                        </div>
                        {/* Hover preview */}
                        {file.thumbnail_path && (
                          <div className="absolute left-0 top-full mt-2 z-50 hidden group-hover/thumb:block">
                            <div className="w-48 h-48 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary shadow-xl overflow-hidden">
                              <img
                                src={`${api.getLibraryFileThumbnailUrl(file.id)}${thumbnailVersions[file.id] ? ((api.getLibraryFileThumbnailUrl(file.id).includes('?') ? '&' : '?') + `v=${thumbnailVersions[file.id]}`) : ''}`}
                                alt={file.filename}
                                className="w-full h-full object-contain"
                              />
                            </div>
                          </div>
                        )}
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className="text-sm text-white truncate">{file.print_name || file.filename}</span>
                          {/* Metadata indicators (#3077), same set as the card. */}
                          {file.external_url && (
                            <a
                              href={file.external_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              onClick={(e) => e.stopPropagation()}
                              className="flex-shrink-0 text-bambu-gray hover:text-bambu-green"
                              title={t('fileManager.details.openLink')}
                              aria-label={t('fileManager.details.openLink')}
                            >
                              <Globe className="w-3.5 h-3.5" />
                            </a>
                          )}
                          {file.has_notes && (
                            <span className="flex-shrink-0 text-bambu-gray" title={t('fileManager.details.hasNotes')} aria-label={t('fileManager.details.hasNotes')}>
                              <StickyNote className="w-3.5 h-3.5" />
                            </span>
                          )}
                          {(file.photo_count ?? 0) > 0 && (
                            <span className="flex-shrink-0 flex items-center gap-0.5 text-xs text-bambu-gray" title={t('fileManager.details.photoCount', { count: file.photo_count })}>
                              <Camera className="w-3.5 h-3.5" />
                              {file.photo_count}
                            </span>
                          )}
                        </div>
                        {/* #2680: last-modified date under the name, toggled from
                            the toolbar. Real on-disk mtime when known, else created_at. */}
                        {showModified && (
                          <div className="text-xs text-bambu-gray flex items-center gap-1 mt-0.5" title={t('fileManager.lastModified')}>
                            <CalendarClock className="w-3 h-3 flex-shrink-0" />
                            <span className="truncate">{formatDate(file.fs_modified_at ?? file.created_at)}</span>
                          </div>
                        )}
                      </div>
                    </div>
                    {/* Uploaded By - only show when auth is enabled */}
                    {authEnabled && (
                      <div className="text-sm text-bambu-gray flex items-center gap-1">
                        {file.created_by_username ? (
                          <>
                            <User className="w-3 h-3" />
                            <span className="truncate">{file.created_by_username}</span>
                          </>
                        ) : (
                          '-'
                        )}
                      </div>
                    )}
                    {/* Type */}
                    <div>
                      <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${fileTypeBadgeClass(file.file_type)}`}>
                        {file.file_type.toUpperCase()}
                      </span>
                    </div>
                    {/* Size */}
                    <div className="text-sm text-bambu-gray">{formatFileSize(file.file_size)}</div>
                    {/* Prints */}
                    <div className="text-sm text-bambu-gray">{file.print_count > 0 ? `${file.print_count}x` : '-'}</div>
                    {/* Tags (#1268) — clickable chips push into the active
                        filter; minmax(0,200px) on the column lets the cell
                        shrink/wrap on narrow viewports without pushing the
                        Actions cell off-screen. */}
                    <div className="min-w-0" {...stopRowActivation}>
                      {!file.tags || file.tags.length === 0 ? (
                        <span className="text-xs text-bambu-gray/50">-</span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {file.tags.map((tg) => (
                            <button
                              key={tg.id}
                              type="button"
                              onClick={() => toggleTagFilter(tg.id)}
                              className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] bg-bambu-green/10 text-bambu-green hover:bg-bambu-green/20 transition-colors max-w-full"
                              title={tg.name}
                            >
                              <TagIcon className="w-2.5 h-2.5 flex-shrink-0" />
                              <span className="truncate">{tg.name}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    {/* Actions */}
                    <FileActionStrip file={file} {...fileActionProps} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
          {/* README rail — collapsible right column on lg+, stacks on top
              on mobile. See the files-area wrapper comment above (#2520). */}
          {selectedFolderId !== null && <FolderReadmePanel folderId={selectedFolderId} />}
        </div>
      </div>

      {/* Modals */}
      {showNewFolderModal && (
        <NewFolderModal
          parentId={selectedFolderId}
          onClose={() => setShowNewFolderModal(false)}
          onSave={(data) => createFolderMutation.mutate(data)}
          isLoading={createFolderMutation.isPending}
          t={t}
        />
      )}

      {showExternalFolderModal && (
        <ExternalFolderModal
          onClose={() => setShowExternalFolderModal(false)}
          onSave={(data) => createExternalFolderMutation.mutate(data)}
          isLoading={createExternalFolderMutation.isPending}
          t={t}
        />
      )}

      {showMoveModal && folders && (
        <MoveFilesModal
          folders={folders}
          selectedFiles={selectedFiles}
          currentFolderId={selectionSourceFolderId}
          onClose={() => setShowMoveModal(false)}
          onMove={(folderId) => moveFilesMutation.mutate({ fileIds: selectedFiles, folderId })}
          isLoading={moveFilesMutation.isPending}
          t={t}
        />
      )}

      {showUploadModal && (
        <FileUploadModal
          folderId={selectedFolderId}
          onClose={() => {
            setShowUploadModal(false);
            setDroppedFiles([]);
          }}
          onUploadComplete={handleUploadComplete}
          initialFiles={droppedFiles.length > 0 ? droppedFiles : undefined}
        />
      )}

      {showPurgeModal && (
        <PurgeOldFilesModal onClose={() => setShowPurgeModal(false)} />
      )}

      <LibraryTagsModal
        open={showTagsModal}
        onClose={() => setShowTagsModal(false)}
        onPickTag={(tagId) => {
          if (!selectedTagIds.includes(tagId)) {
            setSelectedTagIds((prev) => [...prev, tagId]);
          }
        }}
      />

      <BulkTagsPickerModal
        open={showBulkTagsModal}
        fileIds={selectedFiles}
        onClose={() => setShowBulkTagsModal(false)}
      />

      {linkFolder && (
        <LinkFolderModal
          folder={linkFolder}
          onClose={() => setLinkFolder(null)}
          onLink={(data) => updateFolderMutation.mutate({ id: linkFolder.id, data })}
          isLoading={updateFolderMutation.isPending}
          t={t}
        />
      )}

      {deleteConfirm && (
        <ConfirmModal
          title={
            deleteConfirm.type === 'folder'
              ? t('fileManager.deleteFolder')
              : deleteConfirm.type === 'bulk'
              ? t('fileManager.deleteFilesCount', { count: deleteConfirm.count })
              : t('fileManager.deleteFile')
          }
          message={
            deleteConfirm.type === 'folder'
              ? t('fileManager.deleteFolderConfirm')
              : deleteConfirm.type === 'bulk'
              ? t('fileManager.deleteFilesConfirm', { count: deleteConfirm.count })
              : t('fileManager.deleteFileConfirm')
          }
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={isDeleting}
          loadingText={t('fileManager.deleting')}
          onConfirm={handleDeleteConfirm}
          onCancel={() => setDeleteConfirm(null)}
        />
      )}

      {/* Held back until the variant group has loaded. The modal reads its
          candidate list once, on mount, so opening before the group arrives
          would show a single-file print for a file that has alternatives. */}
      {printFile && (!printFile.variant_group_id || printFileGroup !== undefined) && (
        <PrintModal
          mode="create"
          libraryFileId={printVariantFiles?.[0]?.id ?? printFile.id}
          variantFiles={printVariantFiles}
          // Naming a cross-model job after one of its files reads as though the
          // others aren't part of it.
          archiveName={
            printVariantFiles && printVariantFiles.length > 1
              ? `${printVariantFiles[0].filename} ${t('common.plusNMore', { count: printVariantFiles.length - 1 })}`
              : printFile.print_name || printFile.filename
          }
          onClose={() => setPrintFile(null)}
          onSuccess={() => {
            setPrintFile(null);
            setSelectedFiles([]);
            queryClient.invalidateQueries({ queryKey: ['library-files'] });
            queryClient.invalidateQueries({ queryKey: ['queue'] });
            queryClient.invalidateQueries({ queryKey: ['archives'] });
          }}
        />
      )}

      {sliceFile && (
        <SliceModal
          source={{ kind: 'libraryFile', id: sliceFile.id, filename: sliceFile.filename }}
          onClose={() => setSliceFile(null)}
        />
      )}

      {runPipelineFile && (
        <RunWithPipelineModal
          source={{ kind: 'libraryFile', id: runPipelineFile.id, filename: runPipelineFile.filename }}
          onClose={() => setRunPipelineFile(null)}
        />
      )}

      {viewerFile && (
        <ModelViewerModal
          libraryFileId={viewerFile.id}
          title={viewerFile.print_name || viewerFile.filename}
          fileType={viewerFile.file_type}
          onClose={() => setViewerFile(null)}
          // STEP has no server-side renderer; persist the first client render
          // as the grid thumbnail (#2976).
          onSnapshot={isStepType(viewerFile.file_type) ? previewSnapshotHandler(viewerFile) : undefined}
          onSliceWithBambuddy={
            // Only offer in-app slicing on files the SliceModal can actually
            // handle (matches the file-row Cog visibility check at :2127).
            isSliceableLibraryFile(viewerFile, true, preferredSlicer) && hasPermission('library:upload')
              ? () => {
                  const f = viewerFile;
                  setViewerFile(null);
                  setSliceFile(f);
                }
              : undefined
          }
        />
      )}

      {previewBatch.length > 0 && (
        <PreviewThumbnailBatch
          files={previewBatch}
          onProgress={(done, total) => setPreviewBatchProgress({ done, total })}
          onDone={(succeeded) => {
            setPreviewBatch([]);
            setPreviewBatchProgress(null);
            if (succeeded > 0) {
              queryClient.invalidateQueries({ queryKey: ['library-files'] });
              setThumbnailVersions((prev) => {
                const now = Date.now();
                const next = { ...prev };
                previewBatch.forEach((f) => {
                  next[f.id] = now;
                });
                return next;
              });
              showToast(t('fileManager.toast.previewsRendered', { count: succeeded }), 'success');
            }
          }}
        />
      )}

      {(pdfPreviewFile || sheetPreviewFile || msgPreviewFile || imagePreviewFile) && (
        <Suspense fallback={null}>
          {pdfPreviewFile && (
            <PdfPreviewModal
              libraryFileId={pdfPreviewFile.id}
              filename={pdfPreviewFile.print_name || pdfPreviewFile.filename}
              fileSize={pdfPreviewFile.file_size}
              onClose={() => setPdfPreviewFile(null)}
              onSnapshot={previewSnapshotHandler(pdfPreviewFile)}
            />
          )}
          {sheetPreviewFile && (
            <SpreadsheetPreviewModal
              libraryFileId={sheetPreviewFile.id}
              filename={sheetPreviewFile.print_name || sheetPreviewFile.filename}
              fileType={sheetPreviewFile.file_type}
              fileSize={sheetPreviewFile.file_size}
              onClose={() => setSheetPreviewFile(null)}
              onSnapshot={previewSnapshotHandler(sheetPreviewFile)}
            />
          )}
          {msgPreviewFile && (
            <MsgPreviewModal
              libraryFileId={msgPreviewFile.id}
              filename={msgPreviewFile.print_name || msgPreviewFile.filename}
              fileSize={msgPreviewFile.file_size}
              onClose={() => setMsgPreviewFile(null)}
              onSnapshot={previewSnapshotHandler(msgPreviewFile)}
            />
          )}
          {imagePreviewFile && (
            <ImagePreviewModal
              libraryFileId={imagePreviewFile.id}
              filename={imagePreviewFile.print_name || imagePreviewFile.filename}
              fileSize={imagePreviewFile.file_size}
              onClose={() => setImagePreviewFile(null)}
            />
          )}
        </Suspense>
      )}

      {detailsFile && (
        <LibraryFileDetailsModal
          file={detailsFile}
          canEdit={canModify('library', 'update', detailsFile.created_by_id)}
          onClose={() => setDetailsFile(null)}
        />
      )}

      {renameItem && (
        <RenameModal
          type={renameItem.type}
          currentName={renameItem.name}
          currentNumber={renameItem.number}
          onClose={() => setRenameItem(null)}
          onSave={(newName, newNumber, nameChanged) => {
            if (renameItem.type === 'file') {
              renameFileMutation.mutate({ id: renameItem.id, filename: newName });
            } else {
              renameFolderMutation.mutate({
                id: renameItem.id,
                name: nameChanged ? newName : undefined,
                number: newNumber,
              });
            }
          }}
          isLoading={renameFileMutation.isPending || renameFolderMutation.isPending}
          t={t}
        />
      )}
    </div>
  );
}
