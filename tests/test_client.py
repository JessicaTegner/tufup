import json
import logging
import os
import pathlib
import shutil
import subprocess
import tarfile
from typing import Optional
import unittest
from unittest.mock import Mock, patch

from requests.auth import HTTPBasicAuth
import tuf.api.exceptions
from tuf.ngclient import TargetFile

from tests import TempDirTestCase, TEST_REPO_DIR
from tufup.client import AuthRequestsFetcher, Client, PurgeManifest
from tufup.common import TargetMeta
from tufup.utils.platform_specific import ON_WINDOWS

ROOT_FILENAME = 'root.json'
TARGETS_FILENAME = 'targets.json'
SNAPSHOT_FILENAME = 'snapshot.json'
TIMESTAMP_FILENAME = 'timestamp.json'
ON_GITHUB = os.getenv('GITHUB_ACTIONS')


class ClientTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        # directory where the parent application is installed (e.g.
        # %PROGRAMFILES%\MyApp or %LOCALAPPDATA%\Programs\MyApp on Windows 10)
        # https://docs.microsoft.com/en-us/windows/win32/msi/installation-context
        self.app_install_dir = self.temp_dir_path / 'programs' / 'example'
        self.app_install_dir.mkdir(parents=True)
        # directories must be created by parent application
        self.metadata_dir = self.temp_dir_path / 'metadata'
        self.target_dir = self.temp_dir_path / 'targets'
        self.metadata_dir.mkdir()
        self.target_dir.mkdir()
        # parent application must be shipped with root metadata, and must
        # ensure it is placed in the metadata_dir (without version in filename)
        shutil.copy(
            src=TEST_REPO_DIR / 'metadata' / ('1.' + ROOT_FILENAME),
            dst=self.metadata_dir / ROOT_FILENAME,
        )
        # kwargs for client initializer
        self.client_kwargs = dict(
            app_name='example_app',
            app_install_dir=self.app_install_dir,
            current_version='1.0',
            metadata_dir=self.metadata_dir,
            metadata_base_url='http://localhost:8000/metadata/',
            target_dir=self.target_dir,
            target_base_url='http://localhost:8000/targets/',
            session_auth={'http://localhost:8000': ('username', 'password')},
        )

    def mock_download_metadata(
        self, rolename: str, length: int, version: Optional[int] = None
    ) -> bytes:
        if rolename == 'root':
            # indicate current root is newest version
            raise tuf.api.exceptions.DownloadHTTPError(status_code=404, message='')
        # read from the test repo dir, instead of actually downloading
        filename = f'{rolename}.json'
        if version:
            filename = f'{version}.{filename}'
        file_path = TEST_REPO_DIR / 'metadata' / filename
        return file_path.read_bytes()

    def get_refreshed_client(self):
        # refresh to load targets metadata (mock to prevent actual download)
        client = Client(**self.client_kwargs)
        with patch.object(client, '_download_metadata', self.mock_download_metadata):
            client.refresh()
        # ensure current archive exists (dummy)
        shutil.copy(
            src=TEST_REPO_DIR / 'targets' / client.current_archive.path.name,
            dst=client.current_archive_local_path,
        )
        return client

    def populate_refreshed_client(self, client):
        # directly use target files from test repo as downloaded files
        client.downloaded_target_files = {
            target_meta: TEST_REPO_DIR / 'targets' / str(target_meta)
            for target_meta in client.trusted_target_metas
            if target_meta.is_patch and str(target_meta.version) in ['2.0', '3.0rc0']
        }
        # specify new archive (normally done in _check_updates)
        archives = [
            tp
            for tp in client.trusted_target_metas
            if tp.is_archive and str(tp.version) == '3.0rc0'
        ]
        client.new_archive_info = client.get_targetinfo(archives[-1])
        client.new_archive_local_path = pathlib.Path(
            client.target_dir, client.new_archive_info.path
        )
        return client

    def test_init_no_metadata(self):
        # cannot initialize without root metadata file
        (self.metadata_dir / ROOT_FILENAME).unlink()
        with self.assertRaises(FileNotFoundError):
            Client(**self.client_kwargs)

    def test_init(self):
        client = Client(**self.client_kwargs)
        # Client is a subclass of tuf.ngclient.Updater, so it automatically
        # loads and verifies the local root metadata file
        self.assertTrue(client._trusted_set.root)
        # other metadata is not available yet
        for role_name in ['targets', 'snapshot', 'timestamp']:
            # see python-tuf #2250
            self.assertNotIn(role_name, client._trusted_set)

    def test_trusted_target_metas(self):
        client = self.get_refreshed_client()
        self.assertTrue(client.trusted_target_metas)

    def test_get_targetinfo(self):
        client = self.get_refreshed_client()
        target_path_str = 'example_app-1.0.tar.gz'
        target_meta = TargetMeta(target_path=target_path_str)
        for target_path in [target_path_str, target_meta]:
            target_info = client.get_targetinfo(target_path=target_path)
            with self.subTest(msg=target_path):
                self.assertIsInstance(target_info, TargetFile)

    def test_updates_available(self):
        client = Client(**self.client_kwargs)
        # test check_for_updates not called
        with self.assertLogs(logger='tufup.client', level=logging.WARNING) as cm:
            self.assertFalse(client.updates_available)
        self.assertIn('check_for_updates', cm.output[0])
        # test check_for_updates called and update available
        client.new_targets = {'dummy': None}
        self.assertTrue(client.updates_available)

    def test_download_and_apply_update(self):
        # just for completeness...
        mock_download = Mock(return_value=True)
        mock_apply = Mock(return_value=True)
        mock_install = Mock()
        with patch.multiple(
            Client, _download_updates=mock_download, _apply_updates=mock_apply
        ):
            client = self.get_refreshed_client()
            client.new_targets = {'dummy': None}
            client.download_and_apply_update(install=mock_install)
        self.assertTrue(mock_download.called)
        self.assertTrue(mock_apply.called)
        self.assertIn(mock_install, mock_apply.call_args.kwargs.values())

    def test_check_for_updates(self):
        # expectations (based on targets in tests/data/repository):
        # - pre=None: only full releases are included, so finds 2.0 patch
        # - pre='a': finds all, but total patch size exceeds archive size
        # - pre='b': there is no 'b' release, so this finds same as 'rc'
        # - pre='rc': finds 2.0 and 3.0rc0, total patch size smaller than archive
        client = self.get_refreshed_client()
        with patch.object(client, 'refresh', Mock()):
            for pre, expected in [(None, 1), ('a', 1), ('b', 2), ('rc', 2)]:
                with self.subTest(msg=pre):
                    self.assertTrue(client.check_for_updates(pre=pre))
                    self.assertEqual(expected, len(client.new_targets))
                    if pre == 'a':
                        self.assertTrue(
                            all(item.is_archive for item in client.new_targets.keys())
                        )
                    else:
                        self.assertTrue(
                            all(item.is_patch for item in client.new_targets.keys())
                        )

    def test_check_for_updates_already_up_to_date(self):
        self.client_kwargs['current_version'] = '4.0a0'
        client = self.get_refreshed_client()
        with patch.object(client, 'refresh', Mock()):
            self.assertFalse(client.check_for_updates(pre='a'))

    def test_check_for_updates_current_archive_missing(self):
        client = self.get_refreshed_client()
        # remove current archive dummy
        client.current_archive_local_path.unlink()
        with patch.object(client, 'refresh', Mock()):
            for pre in [None, 'a', 'b', 'rc']:
                self.assertTrue(client.check_for_updates(pre=pre))
                target_meta = next(iter(client.new_targets.keys()))
                self.assertTrue(target_meta.is_archive)

    def test__download_updates(self):
        client = Client(**self.client_kwargs)
        client.new_targets = {Mock(): Mock()}
        for cached_path, downloaded_path in [('cached', None), (None, 'downloaded')]:
            with patch.multiple(
                client,
                find_cached_target=Mock(return_value=cached_path),
                download_target=Mock(return_value=downloaded_path),
            ):
                self.assertTrue(client._download_updates(progress_hook=None))
                local_path = next(iter(client.downloaded_target_files.values()))
                if cached_path:
                    self.assertEqual(cached_path, str(local_path))
                else:
                    self.assertEqual(downloaded_path, str(local_path))

    def test__apply_updates(self):
        client = self.populate_refreshed_client(client=self.get_refreshed_client())
        # test confirmation
        mock_install = Mock()
        with patch('builtins.input', Mock(return_value='y')):
            client._apply_updates(install=mock_install, skip_confirmation=False)
        self.assertTrue(any(client.extract_dir.iterdir()))
        self.assertTrue(mock_install.called)
        # test skip confirmation
        mock_install = Mock()
        client._apply_updates(install=mock_install, skip_confirmation=True)
        mock_install.assert_called()

    def test__extract_archive_with_purge(self):
        temp_extract_dir = self.temp_dir_path / 'tufup-extract-dir'
        temp_extract_dir.mkdir()
        self.client_kwargs['extract_dir'] = temp_extract_dir
        self.client_kwargs['purge_extract_dir'] = True
        client = self.populate_refreshed_client(client=self.get_refreshed_client())
        # ensure new archive exists in targets dir(dummy)
        shutil.copy(
            src=TEST_REPO_DIR / 'targets' / client.new_archive_local_path.name,
            dst=client.new_archive_local_path,
        )
        # test extract
        client._extract_archive()
        self.assertTrue(any(client.extract_dir.iterdir()))
        # a purge manifest file should now exist
        purge_manifest = PurgeManifest(dir_to_purge=client.extract_dir)
        self.assertTrue(purge_manifest.file_path.exists())


class AuthRequestsFetcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_auth = {
            'https://example.net': None,
            'https://example.com': ('username', 'password'),
            'https://example.org': HTTPBasicAuth(username='x', password='y'),
            'http://localhost:8000': ('username', 'password'),
        }

    def test_init(self):
        # drop-in replacement for default RequestsFetcher, without args
        self.assertTrue(AuthRequestsFetcher())
        # if authentication is required, specify arg
        fetcher = AuthRequestsFetcher(session_auth=self.session_auth)
        self.assertEqual(self.session_auth, fetcher.session_auth)

    def test__get_session(self):
        fetcher = AuthRequestsFetcher(session_auth=self.session_auth)
        for scheme_and_server, auth in self.session_auth.items():
            url = scheme_and_server + '/some/path?query=something'
            with self.subTest(msg=url):
                session = fetcher._get_session(url=url)
                self.assertEqual(auth, session.auth)

    @unittest.skipIf(condition=ON_GITHUB, reason='external dependency')
    def test_fetch_basic_auth(self):
        # kind of an integration test, as it connects to an external server...
        scheme_and_server = 'https://httpbin.org'
        user = 'me'
        passwd = 'mypassword'
        url = f'{scheme_and_server}/basic-auth/{user}/{passwd}'
        session_auth = {
            scheme_and_server: HTTPBasicAuth(username=user, password=passwd)
        }
        fetcher = AuthRequestsFetcher(session_auth=session_auth)
        fetcher.socket_timeout = 30  # in case httpbin.org is slow to respond
        # we don't have direct access to the response, so we'll just check
        # that RequestsFetcher.fetch() doesn't raise an error, such as a
        # status "401 Unauthorized" or "403 Forbidden"
        try:
            fetcher.fetch(url=url)
        except tuf.api.exceptions.DownloadHTTPError as e:
            self.fail(msg=f'fetch() raised unexpected HTTPError: {e}')

    def test_attach_progress_hook(self):
        mock_hook = Mock()
        bytes_expected = 10
        fetcher = AuthRequestsFetcher()
        fetcher.attach_progress_hook(hook=mock_hook, bytes_expected=bytes_expected)
        bytes_new = 1
        bytes_downloaded = 0
        while bytes_downloaded < bytes_expected:
            bytes_downloaded += bytes_new
            fetcher._progress(bytes_new=bytes_new)
            mock_hook.assert_called_with(
                bytes_downloaded=bytes_downloaded, bytes_expected=bytes_expected
            )

    def test__chunks_without_progress_hook(self):
        chunk_size = 10
        chunk_count = 10
        chunks = [b'x' * chunk_size] * chunk_count

        def mock_iter_content(*args):
            yield from chunks

        mock_response = Mock(iter_content=mock_iter_content, close=Mock())
        fetcher = AuthRequestsFetcher()
        fetcher.chunk_size = chunk_size
        # _chunks should work even if attach_progress_hook was not called
        try:
            for __ in fetcher._chunks(response=mock_response):
                pass
        except Exception as e:
            self.fail(msg=f'_chunks raised an unexpected exception: {e}')

    def test__chunks_with_progress_hook(self):
        chunk_size = 10
        chunk_count = 10
        chunks = [b'x' * chunk_size] * chunk_count

        def mock_iter_content(*args):
            yield from chunks

        mock_response = Mock(iter_content=mock_iter_content, close=Mock())
        fetcher = AuthRequestsFetcher()
        fetcher.chunk_size = chunk_size
        # test custom progress hook
        mock_hook = Mock()
        bytes_expected = chunk_size * chunk_count
        fetcher.attach_progress_hook(hook=mock_hook, bytes_expected=bytes_expected)
        for __ in fetcher._chunks(response=mock_response):
            pass
        self.assertEqual(chunk_count, mock_hook.call_count)


class PurgeManifestTests(TempDirTestCase):
    def test_init(self):
        self.assertTrue(PurgeManifest(dir_to_purge='some/path'))

    def test_file_path_property(self):
        self.assertTrue(PurgeManifest(dir_to_purge='some/path').file_path)

    def test_read_from_file(self):
        purge_manifest = PurgeManifest(dir_to_purge=self.temp_dir_path)
        self.assertIsNone(purge_manifest.read_from_file())
        # write dummy manifest file
        purge_manifest.file_path.write_text('[]')
        # test
        self.assertEqual([], purge_manifest.read_from_file())

    def test_write_to_file(self):
        purge_manifest = PurgeManifest(dir_to_purge=self.temp_dir_path)
        manifest = ['some.file']
        purge_manifest.write_to_file(manifest=manifest)
        self.assertEqual(manifest, json.loads(purge_manifest.file_path.read_text()))

    def test_purge_no_manifest_file(self):
        # prepare
        dir_to_purge = self.temp_dir_path
        dummy_file_path = dir_to_purge.joinpath('dummy.file')
        dummy_file_path.touch()
        purge_manifest = PurgeManifest(dir_to_purge=dir_to_purge)
        # test
        purge_manifest.purge()
        self.assertTrue(dummy_file_path.exists())

    def test_purge(self):
        # create dummy files
        dir_to_purge = self.temp_dir_path
        subdir = dir_to_purge / 'subdir'
        subdir.mkdir()
        readonly_file = dir_to_purge / 'readonly.dummy'
        items_to_purge = [
            readonly_file,
            dir_to_purge / 'some.dummy',
            subdir / 'other.dummy',
            subdir,
        ]
        items_to_keep = [dir_to_purge / 'file.to.keep']
        for item in items_to_purge + items_to_keep:
            if not item.exists():
                item.touch()
        # make sure readonly file is readonly
        readonly_file.chmod(0o444)  # could also use touch(mode=0o444)
        self.assertFalse(os.access(readonly_file, os.W_OK))
        # write manifest file manually (when writing the manifest from a .tar.gz
        # archive, each dir and each item in that dir is listed, recursively,
        # so we also need to include both subdir and the items inside subdir here)
        purge_manifest = PurgeManifest(dir_to_purge=dir_to_purge)
        manifest = [
            str(item.relative_to(dir_to_purge))
            for item in items_to_purge + [purge_manifest.file_path]
        ]
        purge_manifest.file_path.write_text(json.dumps(manifest))
        # test
        purge_manifest.purge()
        for path in items_to_purge:
            self.assertFalse(path.exists())
        for path in items_to_keep:
            self.assertTrue(path.exists())

    def test_create_from_archive(self):
        # create dummy files, including some hidden
        dir_to_purge = self.temp_dir_path
        hidden_subdir = dir_to_purge / '.hidden'
        hidden_subdir.mkdir()
        if ON_WINDOWS:
            # https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/attrib
            subprocess.run(['attrib', '+h', str(hidden_subdir)], check=True)
        dummy_file_in_hidden_subdir = hidden_subdir / 'other.dummy'
        dummy_file_in_hidden_subdir.touch()
        dummy_file = dir_to_purge / 'some.dummy'
        dummy_file.touch()
        # create dummy archive
        archive_path = self.temp_dir_path / 'dummy_archive.tar.gz'
        # todo: should create tar using shutil.make_archive, like in make_gztar_archive, to test handling of "." and "./"
        with tarfile.open(archive_path, mode='w:gz') as tar:
            for path in [dummy_file, hidden_subdir]:
                tar.add(
                    name=path, arcname=path.relative_to(dir_to_purge), recursive=True
                )
        # test
        purge_manifest = PurgeManifest(dir_to_purge=dir_to_purge)
        purge_manifest.create_from_archive(archive=archive_path)
        self.assertTrue(purge_manifest.file_path.exists())
        # load manifest data manually (normally we would use read_from_file)
        manifest = json.loads(purge_manifest.file_path.read_text())
        self.assertEqual(
            {
                item.relative_to(dir_to_purge).as_posix()
                for item in [
                    purge_manifest.file_path,
                    hidden_subdir,
                    dummy_file,
                    dummy_file_in_hidden_subdir,
                ]
            },
            set(manifest),
        )
