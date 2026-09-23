"""Integration tests for the library bulk-ZIP download endpoints."""

import io
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import pytest
from httpx import AsyncClient

from backend.app.core.config import settings as app_settings

FILES_ZIP_URL = "/api/v1/library/files/download-zip"


def _folder_zip_url(folder_id: int, recursive: bool | None = None) -> str:
    url = f"/api/v1/library/folders/{folder_id}/download-zip"
    if recursive is not None:
        url += f"?recursive={str(recursive).lower()}"
    return url


def _open_zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


class TestLibraryZipDownload:
    """POST /library/files/download-zip and GET /library/folders/{id}/download-zip."""

    @pytest.fixture
    def library_root(self, monkeypatch, tmp_path) -> Path:
        """Point the library's path helpers at a throwaway data dir."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        return tmp_path

    @pytest.fixture
    def file_factory(self, db_session, library_root):
        """Create a LibraryFile row, by default with real bytes behind it."""
        counter = [0]

        async def _create(
            filename: str,
            content: bytes = b"payload",
            *,
            folder_id: int | None = None,
            created_by_id: int | None = None,
            on_disk: bool = True,
            file_path: str | None = None,
        ):
            from backend.app.models.library import LibraryFile

            counter[0] += 1
            relative = file_path if file_path is not None else f"library/files/{counter[0]}.bin"
            if on_disk and file_path is None:
                target = library_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

            row = LibraryFile(
                filename=filename,
                file_path=relative,
                file_type=filename.rsplit(".", 1)[-1][:10],
                file_size=len(content),
                folder_id=folder_id,
                created_by_id=created_by_id,
            )
            db_session.add(row)
            await db_session.commit()
            await db_session.refresh(row)
            return row

        return _create

    @pytest.fixture
    def folder_factory(self, db_session):
        async def _create(name: str, parent_id: int | None = None):
            from backend.app.models.library import LibraryFolder

            folder = LibraryFolder(name=name, parent_id=parent_id)
            db_session.add(folder)
            await db_session.commit()
            await db_session.refresh(folder)
            return folder

        return _create

    # ---------- POST /files/download-zip ----------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_zip_contains_exactly_the_requested_files(self, async_client: AsyncClient, file_factory):
        """The archive opens with zipfile and holds every requested file's bytes."""
        drawing = await file_factory("drawing.pdf", b"pdf-bytes")
        model = await file_factory("part.3mf", b"3mf-bytes")

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [drawing.id, model.id]})

        with _open_zip(response) as archive:
            assert archive.namelist() == ["drawing.pdf", "part.3mf"]
            assert archive.read("drawing.pdf") == b"pdf-bytes"
            assert archive.read("part.3mf") == b"3mf-bytes"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_zip_members_are_stored_not_deflated(self, async_client: AsyncClient, file_factory):
        """3MF/STEP/JPEG are already compressed; deflating them only costs the Pi CPU."""
        model = await file_factory("part.3mf", b"3mf-bytes" * 100)

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [model.id]})

        with _open_zip(response) as archive:
            assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_zip_filename_header_names_the_bundle_and_the_day(self, async_client: AsyncClient, file_factory):
        model = await file_factory("part.3mf")

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [model.id]})

        assert response.status_code == 200
        today = datetime.now().strftime("%Y-%m-%d")
        assert f'filename="bambuddy-files-{today}.zip"' in response.headers["content-disposition"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_row_missing_on_disk_is_skipped_not_fatal(self, async_client: AsyncClient, file_factory):
        """One dead row must not cost the user the other files."""
        alive = await file_factory("kept.pdf", b"kept")
        dead = await file_factory("gone.pdf", b"gone", on_disk=False)

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [alive.id, dead.id]})

        with _open_zip(response) as archive:
            assert archive.namelist() == ["kept.pdf"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_all_skipped_request_is_404(self, async_client: AsyncClient, file_factory):
        dead = await file_factory("gone.pdf", on_disk=False)

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [dead.id, 999999]})

        assert response.status_code == 404
        assert "available" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_empty_file_ids_is_rejected(self, async_client: AsyncClient):
        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": []})

        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_colliding_names_get_a_counter_suffix(self, async_client: AsyncClient, file_factory):
        """A ZIP may repeat a name, but extractors then silently overwrite."""
        first = await file_factory("drawing.pdf", b"first")
        second = await file_factory("drawing.pdf", b"second")

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [first.id, second.id]})

        with _open_zip(response) as archive:
            assert archive.namelist() == ["drawing.pdf", "drawing (2).pdf"]
            assert archive.read("drawing.pdf") == b"first"
            assert archive.read("drawing (2).pdf") == b"second"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stored_name_with_traversal_cannot_escape_the_archive(
        self, async_client: AsyncClient, file_factory, tmp_path
    ):
        """A ``../`` in the stored filename must not become a path in the archive."""
        evil = await file_factory("../../etc/passwd", b"nope")

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [evil.id]})

        destination = tmp_path / "extracted"
        with _open_zip(response) as archive:
            (entry,) = archive.namelist()
            # One component: no separator to introduce a level, and no leading
            # ``..`` to climb out of one.
            assert "/" not in entry and "\\" not in entry
            assert not entry.startswith("..")
            assert archive.read(entry) == b"nope"
            archive.extractall(destination)

        written = [path for path in destination.rglob("*") if path.is_file()]
        assert [path.parent for path in written] == [destination]
        assert written[0].read_bytes() == b"nope"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stored_path_escaping_base_dir_is_skipped(self, async_client: AsyncClient, file_factory):
        """A row whose path resolves outside base_dir is dropped, not served."""
        good = await file_factory("kept.pdf", b"kept")
        escaped = await file_factory("escaped.pdf", b"x", file_path="../escaped.pdf")

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [good.id, escaped.id]})

        with _open_zip(response) as archive:
            assert archive.namelist() == ["kept.pdf"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_file_cap_answers_413_naming_the_cap(self, async_client: AsyncClient, file_factory, monkeypatch):
        monkeypatch.setattr("backend.app.api.routes.library.ZIP_MAX_FILES", 2)
        ids = [(await file_factory(f"part-{n}.3mf")).id for n in range(3)]

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": ids})

        assert response.status_code == 413
        detail = response.json()["detail"]
        assert "3 requested" in detail
        assert "limit is 2" in detail

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_byte_cap_answers_413_naming_the_cap(self, async_client: AsyncClient, file_factory, monkeypatch):
        monkeypatch.setattr("backend.app.api.routes.library.ZIP_MAX_TOTAL_BYTES", 10)
        big = await file_factory("part.3mf", b"x" * 64)

        response = await async_client.post(FILES_ZIP_URL, json={"file_ids": [big.id]})

        assert response.status_code == 413
        detail = response.json()["detail"]
        assert "64 B requested" in detail
        assert "limit is 10 B" in detail

    # ---------- GET /folders/{id}/download-zip ----------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_zip_keeps_the_subfolder_structure(
        self, async_client: AsyncClient, folder_factory, file_factory
    ):
        root = await folder_factory("RAFI")
        job = await folder_factory("N1125035", parent_id=root.id)
        await file_factory("quote.pdf", b"quote", folder_id=root.id)
        await file_factory("drawing.pdf", b"drawing", folder_id=job.id)

        response = await async_client.get(_folder_zip_url(root.id))

        with _open_zip(response) as archive:
            assert sorted(archive.namelist()) == ["RAFI/N1125035/drawing.pdf", "RAFI/quote.pdf"]
            assert archive.read("RAFI/N1125035/drawing.pdf") == b"drawing"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_zip_non_recursive_keeps_only_the_top_level(
        self, async_client: AsyncClient, folder_factory, file_factory
    ):
        root = await folder_factory("RAFI")
        job = await folder_factory("N1125035", parent_id=root.id)
        await file_factory("quote.pdf", b"quote", folder_id=root.id)
        await file_factory("drawing.pdf", b"drawing", folder_id=job.id)

        response = await async_client.get(_folder_zip_url(root.id, recursive=False))

        with _open_zip(response) as archive:
            assert archive.namelist() == ["RAFI/quote.pdf"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_zip_unknown_folder_is_404(self, async_client: AsyncClient, library_root):
        response = await async_client.get(_folder_zip_url(999999))

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_zip_without_downloadable_files_is_404(
        self, async_client: AsyncClient, folder_factory, file_factory
    ):
        folder = await folder_factory("Empty")
        await file_factory("gone.pdf", folder_id=folder.id, on_disk=False)

        response = await async_client.get(_folder_zip_url(folder.id))

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_zip_umlauts_survive_the_header_and_the_entries(
        self, async_client: AsyncClient, folder_factory, file_factory
    ):
        folder = await folder_factory("Kundenaufträge Müller")
        await file_factory("Prüfbericht.pdf", b"report", folder_id=folder.id)

        response = await async_client.get(_folder_zip_url(folder.id))

        disposition = response.headers["content-disposition"]
        assert "filename*=UTF-8''" in disposition
        encoded = disposition.split("filename*=UTF-8''", 1)[1]
        today = datetime.now().strftime("%Y-%m-%d")
        assert unquote(encoded) == f"Kundenaufträge Müller-{today}.zip"
        with _open_zip(response) as archive:
            assert archive.namelist() == ["Kundenaufträge Müller/Prüfbericht.pdf"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_zip_file_cap_answers_413(
        self, async_client: AsyncClient, folder_factory, file_factory, monkeypatch
    ):
        monkeypatch.setattr("backend.app.api.routes.library.ZIP_MAX_FILES", 1)
        folder = await folder_factory("RAFI")
        await file_factory("a.pdf", folder_id=folder.id)
        await file_factory("b.pdf", folder_id=folder.id)

        response = await async_client.get(_folder_zip_url(folder.id))

        assert response.status_code == 413
        assert "limit is 1" in response.json()["detail"]


class TestLibraryZipDownloadOwnership:
    """A ZIP may never carry a file its requester could not have downloaded singly."""

    @pytest.fixture
    def library_root(self, monkeypatch, tmp_path) -> Path:
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        return tmp_path

    @pytest.fixture
    async def auth_setup(self, db_session):
        from sqlalchemy import select

        from backend.app.core.auth import create_access_token, get_password_hash
        from backend.app.models.group import Group
        from backend.app.models.settings import Settings
        from backend.app.models.user import User

        db_session.add(Settings(key="auth_enabled", value="true"))
        await db_session.commit()

        operator_group = (await db_session.execute(select(Group).where(Group.name == "Operators"))).scalar_one()
        password_hash = get_password_hash("password")

        owner = User(username="zip_owner", password_hash=password_hash, is_active=True)
        owner.groups.append(operator_group)
        stranger = User(username="zip_stranger", password_hash=password_hash, is_active=True)
        stranger.groups.append(operator_group)
        db_session.add_all([owner, stranger])
        await db_session.commit()

        return {
            "owner": owner,
            "stranger": stranger,
            "owner_token": create_access_token(data={"sub": owner.username}),
        }

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_zip_omits_a_file_the_caller_may_not_read(
        self, async_client: AsyncClient, db_session, auth_setup, library_root
    ):
        from backend.app.models.library import LibraryFile

        rows = []
        for name, owner_id in (("mine.pdf", auth_setup["owner"].id), ("theirs.pdf", auth_setup["stranger"].id)):
            relative = f"library/files/{name}"
            target = library_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(name.encode())
            row = LibraryFile(
                filename=name,
                file_path=relative,
                file_type="pdf",
                file_size=len(name),
                created_by_id=owner_id,
            )
            db_session.add(row)
            rows.append(row)
        await db_session.commit()
        for row in rows:
            await db_session.refresh(row)

        response = await async_client.post(
            FILES_ZIP_URL,
            json={"file_ids": [rows[0].id, rows[1].id]},
            headers={"Authorization": f"Bearer {auth_setup['owner_token']}"},
        )

        with _open_zip(response) as archive:
            assert archive.namelist() == ["mine.pdf"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_zip_of_someone_elses_files_is_404(
        self, async_client: AsyncClient, db_session, auth_setup, library_root
    ):
        from backend.app.models.library import LibraryFile, LibraryFolder

        folder = LibraryFolder(name="Theirs")
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)

        relative = "library/files/theirs.pdf"
        target = library_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"theirs")
        db_session.add(
            LibraryFile(
                filename="theirs.pdf",
                file_path=relative,
                file_type="pdf",
                file_size=6,
                folder_id=folder.id,
                created_by_id=auth_setup["stranger"].id,
            )
        )
        await db_session.commit()

        response = await async_client.get(
            _folder_zip_url(folder.id),
            headers={"Authorization": f"Bearer {auth_setup['owner_token']}"},
        )

        assert response.status_code == 404
