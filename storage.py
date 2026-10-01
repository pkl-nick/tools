"""
Artifact storage: listing photos, profile photos and database backups.

On Azure, files go to Blob Storage. The app signs in with its managed
identity (no storage key in settings); AZURE_STORAGE_CONNECTION_STRING is
also accepted for local testing against a real account. Without either,
files go to local disk, which is what development and tests use.

    AZURE_STORAGE_ACCOUNT   storage account name, e.g. nradls
    MEDIA_CONTAINER         photos (default toolshare-media)
    BACKUP_CONTAINER        database backups (default toolshare-backups)

Keys look like "listings/<uuid>.jpg". They are unique, so served files can be cached forever.
"""

import os
import uuid
from typing import Optional, Tuple

MEDIA_CONTAINER = os.getenv('MEDIA_CONTAINER', 'toolshare-media')
BACKUP_CONTAINER = os.getenv('BACKUP_CONTAINER', 'toolshare-backups')


def new_key(prefix: str, ext: str) -> str:
    return f'{prefix}/{uuid.uuid4().hex}.{ext.lower().lstrip(".")}'


class LocalStorage:
    kind = 'local'

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, key))
        if not path.startswith(self.root + os.sep):
            raise ValueError('Invalid storage key')
        return path

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return key

    def get(self, key: str) -> Optional[Tuple[bytes, Optional[str]]]:
        try:
            path = self._path(key)
        except ValueError:
            return None
        if not os.path.isfile(path):
            return None
        with open(path, 'rb') as f:
            return f.read(), None

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except (FileNotFoundError, ValueError):
            pass

    def list(self, prefix: str = ''):
        """[(key, size_bytes, modified_iso)], newest first"""
        import datetime
        out = []
        base = self._path(prefix) if prefix else self.root
        if os.path.isdir(base):
            for dirpath, _, files in os.walk(base):
                for name in files:
                    full = os.path.join(dirpath, name)
                    st = os.stat(full)
                    out.append((os.path.relpath(full, self.root).replace(os.sep, '/'), st.st_size,
                                datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')))
        return sorted(out, key=lambda r: r[2], reverse=True)

    def describe(self) -> str:
        return f'local disk ({self.root})'


class BlobStorage:
    kind = 'blob'

    def __init__(self, container: str, account: str = '', connection_string: str = ''):
        from azure.storage.blob import BlobServiceClient
        if connection_string:
            service = BlobServiceClient.from_connection_string(connection_string)
        else:
            from azure.identity import DefaultAzureCredential
            service = BlobServiceClient(f'https://{account}.blob.core.windows.net',
                                        credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))
        self.account = service.account_name
        self.container_name = container
        self.container = service.get_container_client(container)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        from azure.storage.blob import ContentSettings
        self.container.upload_blob(key, data, overwrite=True,
                                   content_settings=ContentSettings(content_type=content_type))
        return key

    def get(self, key: str) -> Optional[Tuple[bytes, Optional[str]]]:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            downloader = self.container.download_blob(key)
        except ResourceNotFoundError:
            return None
        return downloader.readall(), downloader.properties.content_settings.content_type

    def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            self.container.delete_blob(key)
        except ResourceNotFoundError:
            pass

    def list(self, prefix: str = ''):
        """[(key, size_bytes, modified_iso)], newest first"""
        out = [(b.name, b.size, b.last_modified.isoformat(timespec='seconds'))
               for b in self.container.list_blobs(name_starts_with=prefix or None)]
        return sorted(out, key=lambda r: r[2], reverse=True)

    def describe(self) -> str:
        return f'Azure Blob Storage ({self.account}/{self.container_name})'


_stores = {}


def blob_configured() -> bool:
    return bool(os.getenv('AZURE_STORAGE_CONNECTION_STRING') or os.getenv('AZURE_STORAGE_ACCOUNT'))


def get_store(container: str, local_root: str):
    """Blob container when configured, otherwise a local folder. Cached per container."""
    cache_key = (container, local_root, blob_configured())
    if cache_key not in _stores:
        if blob_configured():
            _stores[cache_key] = BlobStorage(container, account=os.getenv('AZURE_STORAGE_ACCOUNT', ''),
                                             connection_string=os.getenv('AZURE_STORAGE_CONNECTION_STRING', ''))
        else:
            _stores[cache_key] = LocalStorage(local_root)
    return _stores[cache_key]
