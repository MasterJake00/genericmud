"""Read a soundpack's git repository over the network without cloning it.

git's own HTTP protocol (version 2) does both things the updater needs in a request or two.
It lists every file in the repository with its blob hash and no file contents (a shallow
"blob:none" fetch of one commit), and it fetches only the files that changed, by those hashes.
One request lists Erion's 7,640 files on gitlab.com. The forges' web APIs need 86 requests for
that, and gitlab.com answered them with a half-hour lockout for this address.

dulwich speaks the protocol. Every HTTP request it makes goes through genericMud's own checked
opener (see :mod:`genericmud.packs.vault`), so addresses and redirects are vetted the same way
as pack downloads, and the bytes a fetch may return are capped.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from dulwich.client import AbstractHttpGitClient, default_user_agent_string
from dulwich.errors import GitProtocolError
from dulwich.object_format import SHA1
from dulwich.object_store import ObjectStoreGraphWalker
from dulwich.objects import Blob, Commit, Tree
from dulwich.pack import PackData, PackInflater

from genericmud.packs import vault

LISTING_MAX_BYTES = 64 * 1024 * 1024  # a blob-less listing is trees only: Erion's is 246 KB
FETCH_MAX_BYTES = 4 * 1024 * 1024 * 1024  # the most one update may download
_BLOBS_PER_REQUEST = 200  # files asked for in one request
_FILE_MODE_MASK = 0o170000
_DIRECTORY_MODE = 0o040000
_REGULAR_FILE_MODE = 0o100000


class RemoteError(Exception):
    """The repository couldn't be read; the message says why, in words a player can act on."""


class _Refused(RemoteError):
    """The server understood the request and turned it down (a git protocol refusal)."""


@dataclass(frozen=True)
class RemoteTree:
    """One commit of a repository: its id, and every regular file in it with its blob hash."""

    commit: str
    files: dict[str, str]


class _Budget:
    """How many more bytes the server may send before the fetch is stopped."""

    def __init__(self, limit: int) -> None:
        self.left = limit

    def spend(self, count: int) -> None:
        self.left -= count
        if self.left < 0:
            raise RemoteError("the repository sent more data than an update should need")


class _Response:
    """The response shape dulwich reads, over a urllib response."""

    def __init__(self, response, url: str, budget: _Budget) -> None:
        self._response = response
        self._budget = budget
        self.status = response.status
        self.headers = response.headers
        self.content_type = response.headers.get("Content-Type")
        final = response.geturl()
        self.redirect_location = final if final and final != url else None

    def read(self, amount: int | None = None) -> bytes:
        data = self._response.read(amount) if amount is not None else self._response.read()
        self._budget.spend(len(data))
        return data

    def geturl(self) -> str | None:
        return self._response.geturl()

    def close(self) -> None:
        self._response.close()


class _CheckedHttpClient(AbstractHttpGitClient):
    """dulwich's smart-HTTP client, sending its requests through genericMud's checked opener."""

    def __init__(self, base_url: str, budget: _Budget) -> None:
        # Thin packs allowed, as every git client asks (some servers insist): a thin pack can
        # only lean on objects the client says it has, and this one never says it has any.
        super().__init__(base_url)
        self._budget = budget

    def _http_request(self, url, headers=None, data=None, raise_for_status=True):
        if data is not None and not isinstance(data, bytes):
            data = b"".join(data)
        request_headers = dict(headers or {})
        # Git hosts answer git clients; ChatMUD's GitLab never answers anything else.
        request_headers["User-Agent"] = f"{default_user_agent_string()} genericMud"
        request = Request(url, data=data, headers=request_headers)
        try:
            response = vault.open_checked(request)
        except HTTPError as error:
            raise RemoteError(_http_problem(error.code)) from error
        except vault.BlockedUrl as error:
            raise RemoteError(f"the repository's address isn't allowed: {error}") from error
        except (URLError, OSError) as error:
            raise RemoteError(f"couldn't reach the repository: {error}") from error
        wrapped = _Response(response, url, self._budget)
        return wrapped, wrapped.read


def _http_problem(status: int) -> str:
    if status == 429:
        return "the repository's host is busy (too many requests); try again in a while"
    if status in (401, 403, 404):
        return "the repository isn't there, or isn't public"
    return f"the repository's host answered with an error ({status}); try again later"


def _client(repo_url: str, budget: _Budget) -> tuple[_CheckedHttpClient, str]:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RemoteError("a soundpack repository has to be an https address")
    base = f"{parsed.scheme}://{parsed.netloc}/"
    return _CheckedHttpClient(base, budget), parsed.path


def _head(client: _CheckedHttpClient, path: str) -> bytes:
    """The commit the repository's default branch points at."""
    try:
        result = client.get_refs(path, protocol_version=2)
    except GitProtocolError as error:
        raise RemoteError(f"the repository didn't answer as git does: {error}") from error
    refs = getattr(result, "refs", result)
    head = refs.get(b"HEAD")
    if head is None:
        raise RemoteError("the repository has no default branch")
    return head


def _fetch(
    client: _CheckedHttpClient, path: str, wants: list[bytes], pack_file: Path, **options
) -> None:
    """Ask for ``wants`` and write the pack the server sends to ``pack_file``."""
    with pack_file.open("wb") as handle:
        try:
            client.fetch_pack(
                path,
                lambda _refs, depth=None: list(wants),
                graph_walker=ObjectStoreGraphWalker([], lambda _sha: []),
                pack_data=handle.write,
                protocol_version=2,
                **options,
            )
        except GitProtocolError as error:
            raise _Refused(f"the repository refused the request: {error}") from error


def _objects(pack_file: Path) -> Iterable:
    """Every object in a pack file, deltas resolved, each with its id computed from its
    content (so an object is what its hash says it is)."""
    size = pack_file.stat().st_size
    if size == 0:
        return
    with pack_file.open("rb") as handle:
        data = PackData.from_file(handle, SHA1, size=size)
        yield from PackInflater.for_pack_data(data)


def list_tree(repo_url: str, *, max_bytes: int = LISTING_MAX_BYTES) -> RemoteTree:
    """Every regular file in the default branch's latest commit, with its blob hash.

    Refuses a listing with a folder missing: the updater deletes files that left the
    repository, so working from part of the list would delete real ones.
    """
    budget = _Budget(max_bytes)
    client, path = _client(repo_url, budget)
    head = _head(client, path)
    objects: dict[bytes, object] = {}
    with tempfile.TemporaryDirectory(prefix="gm-git-") as scratch:
        pack_file = Path(scratch) / "listing.pack"
        _fetch(client, path, [head], pack_file, depth=1, filter_spec=b"blob:none")
        for item in _objects(pack_file):
            if isinstance(item, (Commit, Tree)):
                objects[item.id] = item
    commit = objects.get(head)
    if not isinstance(commit, Commit):
        raise RemoteError("the repository didn't send its latest commit")
    files: dict[str, str] = {}
    folders = [(commit.tree, "")]
    while folders:
        tree_id, prefix = folders.pop()
        tree = objects.get(tree_id)
        if not isinstance(tree, Tree):
            raise RemoteError("the repository's file list arrived incomplete; try again later")
        for entry in tree.items():
            name = entry.path.decode("utf-8", "surrogateescape")
            kind = entry.mode & _FILE_MODE_MASK
            if kind == _DIRECTORY_MODE:
                folders.append((entry.sha, prefix + name + "/"))
            elif kind == _REGULAR_FILE_MODE:
                files[prefix + name] = entry.sha.decode("ascii")
            # Symlinks and submodules aren't pack files; they're left out.
    return RemoteTree(commit=head.decode("ascii"), files=files)


def fetch_blobs(
    repo_url: str,
    blob_ids: Iterable[str],
    destination: Path,
    *,
    max_bytes: int = FETCH_MAX_BYTES,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Path]:
    """Download the blobs ``blob_ids`` into ``destination``, one file per blob named by its
    hash, and return where each landed. A blob is only written once its content hashes to the
    id that was asked for; one that never arrives is missing from the result."""
    wanted = sorted(set(blob_ids))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if not wanted:
        return {}
    budget = _Budget(max_bytes)
    client, path = _client(repo_url, budget)
    landed: dict[str, Path] = {}
    pack_file = destination / "fetch.pack"
    try:
        for start in range(0, len(wanted), _BLOBS_PER_REQUEST):
            batch = wanted[start:start + _BLOBS_PER_REQUEST]
            try:
                _fetch(client, path, [blob_id.encode("ascii") for blob_id in batch], pack_file)
            except _Refused:
                # Some servers only hand out what a branch points at, not files by their hash.
                # The latest commit, without history, has every file; take the wanted ones.
                head = _head(client, path)
                _fetch(client, path, [head], pack_file, depth=1)
                _keep_blobs(pack_file, set(wanted), destination, landed)
                break
            _keep_blobs(pack_file, set(batch), destination, landed)
            if progress is not None:
                progress(len(landed), len(wanted))
    finally:
        pack_file.unlink(missing_ok=True)
    if progress is not None:
        progress(len(landed), len(wanted))
    return landed


def _keep_blobs(pack_file: Path, wanted: set[str], destination: Path, landed: dict) -> None:
    """Write each wanted blob in the pack to ``destination`` under its hash."""
    for item in _objects(pack_file):
        blob_id = item.id.decode("ascii")
        if not isinstance(item, Blob) or blob_id not in wanted:
            continue
        target = destination / blob_id
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(item.as_raw_string())
        os.replace(temporary, target)
        landed[blob_id] = target
