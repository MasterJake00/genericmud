"""Read a soundpack's git repository over the network without cloning it.

git's own HTTP protocol (version 2) does both things the updater needs in a request or two.
It lists every file in the repository with its blob hash and no file contents (a shallow
"blob:none" fetch of one commit), and it fetches only the files that changed, by those hashes.
One request lists Erion's 7,640 files on gitlab.com. The forges' web APIs need 86 requests for
that, and gitlab.com answered them with a half-hour lockout for this address.

dulwich speaks the protocol. Every HTTP request it makes goes through genericMud's own checked
opener (see :mod:`genericmud.packs.vault`), so addresses and redirects are vetted the same way
as pack downloads. A server is held to limits on what it sends: the bytes, how slowly it may
send them, and how big its objects may grow once inflated, checked before anything is inflated,
since a few kilobytes of zlib or a delta can expand to gigabytes.
"""

from __future__ import annotations

import os
import tempfile
import time
import zlib
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
LISTING_SECONDS = 10 * 60  # the longest a listing may take
FETCH_SECONDS = 2 * 60 * 60  # the longest an update's downloads may take
MINIMUM_BYTES_PER_SECOND = 4 * 1024  # slower than this, after the first minute, is given up on
_GRACE_SECONDS = 60
_BLOBS_PER_REQUEST = 200  # files asked for in one request
_READ_STEP = 64 * 1024  # a response read whole is read this much at a time
_FILE_MODE_MASK = 0o170000
_DIRECTORY_MODE = 0o040000
_REGULAR_FILE_MODE = 0o100000
_OFS_DELTA = 6  # pack object types that are a difference from another object
_REF_DELTA = 7
_HASH_BYTES = 20
# A path the updater would write, or record in its baseline, mustn't hold these: line breaks
# would add lines to the baseline, and Windows can't name a file with the rest (a colon even
# writes to a hidden stream of another file).
_REFUSED_CHARACTERS = frozenset('<>:"\\|?*\x7f\x85\u2028\u2029') | frozenset(map(chr, range(32)))
_WINDOWS_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{number}" for number in range(10)]
    + [f"lpt{number}" for number in range(10)]
)


@dataclass(frozen=True)
class _Limits:
    """How big a pack's objects may grow once inflated: each one, and all of them together."""

    object_bytes: int
    total_bytes: int
    objects: int


# A listing is commits and folders: Erion's 7,640 files make 246 KB of them.
LISTING_LIMITS = _Limits(object_bytes=64 * 1024 * 1024, total_bytes=512 * 1024 * 1024,
                         objects=2_000_000)
# The biggest file in any vault pack is a 158 MB WAV.
FETCH_LIMITS = _Limits(object_bytes=512 * 1024 * 1024, total_bytes=16 * 1024 * 1024 * 1024,
                       objects=2_000_000)


class RemoteError(Exception):
    """The repository couldn't be read; the message says why, in words a player can act on."""


class _Refused(RemoteError):
    """The server understood the request and turned it down (a git protocol refusal)."""


@dataclass(frozen=True)
class RemoteTree:
    """One commit of a repository: its id, every regular file in it with its blob hash, and
    the paths left out because no file could safely be given that name."""

    commit: str
    files: dict[str, str]
    skipped: tuple[str, ...] = ()


class _Budget:
    """What the server may still send before the fetch is stopped: so many bytes, within so
    long, and never much slower than a trickle."""

    def __init__(
        self, limit: int, *, seconds: float = FETCH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.left = limit
        self._received = 0
        self._seconds = seconds
        self._clock = clock
        self._started = clock()

    def spend(self, count: int) -> None:
        self.left -= count
        self._received += count
        if self.left < 0:
            raise RemoteError("the repository sent more data than an update should need")
        elapsed = self._clock() - self._started
        if elapsed > self._seconds:
            raise RemoteError("the repository took too long to answer; try again later")
        if elapsed > _GRACE_SECONDS and self._received < elapsed * MINIMUM_BYTES_PER_SECOND:
            raise RemoteError("the repository is sending too slowly; try again later")


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
        if amount is not None:
            data = self._response.read(amount)
            self._budget.spend(len(data))
            return data
        # All of it: a piece at a time, so a server that never stops is stopped by the budget.
        pieces = []
        while piece := self._response.read(_READ_STEP):
            self._budget.spend(len(piece))
            pieces.append(piece)
        return b"".join(pieces)

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


def _objects(pack_file: Path, limits: _Limits) -> Iterable:
    """Every object in a pack file, deltas resolved, each with its id computed from its
    content (so an object is what its hash says it is). The pack is checked against
    ``limits`` first."""
    size = pack_file.stat().st_size
    if size == 0:
        return
    _check_sizes(pack_file, limits)
    with pack_file.open("rb") as handle:
        data = PackData.from_file(handle, SHA1, size=size)
        yield from PackInflater.for_pack_data(data)


def _check_sizes(pack_file: Path, limits: _Limits) -> None:
    """Refuse a pack whose objects would inflate past ``limits``, before any is inflated.

    Each object's header says how big it is; a delta says, in its first bytes, how big the
    object it rebuilds will be. dulwich holds an object whole in memory, and keeps to those
    sizes, so checking them bounds what it can be made to allocate. This reads the whole pack
    through zlib once, keeping nothing but each delta's first bytes.
    """
    too_big = "the repository sent a file bigger than any soundpack's"
    with pack_file.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"PACK":
            raise RemoteError("the repository sent something that isn't a git pack")
        count = int.from_bytes(header[8:12], "big")
        if count > limits.objects:
            raise RemoteError("the repository sent more files than any soundpack has")
        total = 0
        for _object in range(count):
            first = _byte(handle)
            kind = (first >> 4) & 7
            size = first & 0x0F
            shift = 4
            byte = first
            while byte & 0x80:
                byte = _byte(handle)
                size |= (byte & 0x7F) << shift
                shift += 7
            if kind == _OFS_DELTA:
                while _byte(handle) & 0x80:
                    pass
            elif kind == _REF_DELTA:
                handle.read(_HASH_BYTES)
            if size > limits.object_bytes:
                raise RemoteError(too_big)
            start = _inflate(handle, size)
            if kind in (_OFS_DELTA, _REF_DELTA):
                _base_size, offset = _varint(start, 0)
                size, _offset = _varint(start, offset)  # the rebuilt object's size
                if size > limits.object_bytes:
                    raise RemoteError(too_big)
            total += size
            if total > limits.total_bytes:
                raise RemoteError("the repository sent more data than an update should need")


def _byte(handle) -> int:
    data = handle.read(1)
    if not data:
        raise RemoteError("the repository sent a pack that stops short; try again later")
    return data[0]


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    """A delta header's size: seven bits a byte, low bits first."""
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise RemoteError("the repository sent a damaged pack; try again later")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, offset


def _inflate(handle, size: int) -> bytes:
    """Read one object's zlib data from ``handle``, leaving it just past the end of it, and
    return its first bytes. Inflates at most a little more than ``size``: more is refused."""
    inflater = zlib.decompressobj()
    produced = 0
    start = b""
    while not inflater.eof:
        chunk = handle.read(_READ_STEP)
        if not chunk:
            raise RemoteError("the repository sent a pack that stops short; try again later")
        data = chunk
        while data and not inflater.eof:
            try:
                out = inflater.decompress(data, _READ_STEP)
            except zlib.error as error:
                raise RemoteError("the repository sent a damaged pack; try again later") from error
            produced += len(out)
            if produced > size:
                raise RemoteError("the repository sent a damaged pack; try again later")
            if len(start) < 32:
                start += out[: 32 - len(start)]
            data = inflater.unconsumed_tail
        if inflater.eof:
            handle.seek(-len(inflater.unused_data), os.SEEK_CUR)
    if produced != size:
        raise RemoteError("the repository sent a damaged pack; try again later")
    return start


def _usable_name(name: str) -> bool:
    """Whether a file or folder in the repository can safely be given its name here: nothing
    that breaks a line or that Windows refuses, no name Windows keeps for a device, and no
    trailing dot or space, which Windows drops (so "settings.ini " would land on the
    player's settings.ini)."""
    if name in ("", ".", "..") or name.endswith((".", " ")):
        return False
    if _REFUSED_CHARACTERS.intersection(name):
        return False
    return name.split(".", 1)[0].strip().lower() not in _WINDOWS_DEVICE_NAMES


def list_tree(
    repo_url: str, *, max_bytes: int = LISTING_MAX_BYTES, seconds: float = LISTING_SECONDS
) -> RemoteTree:
    """Every regular file in the default branch's latest commit, with its blob hash.

    Refuses a listing with a folder missing: the updater deletes files that left the
    repository, so working from part of the list would delete real ones. A file or folder no
    file here could safely be named after is left out, and said so in ``skipped``.
    """
    budget = _Budget(max_bytes, seconds=seconds)
    client, path = _client(repo_url, budget)
    head = _head(client, path)
    objects: dict[bytes, object] = {}
    with tempfile.TemporaryDirectory(prefix="gm-git-") as scratch:
        pack_file = Path(scratch) / "listing.pack"
        _fetch(client, path, [head], pack_file, depth=1, filter_spec=b"blob:none")
        for item in _objects(pack_file, LISTING_LIMITS):
            if isinstance(item, (Commit, Tree)):
                objects[item.id] = item
    commit = objects.get(head)
    if not isinstance(commit, Commit):
        raise RemoteError("the repository didn't send its latest commit")
    files: dict[str, str] = {}
    skipped: list[str] = []
    folders = [(commit.tree, "")]
    while folders:
        tree_id, prefix = folders.pop()
        tree = objects.get(tree_id)
        if not isinstance(tree, Tree):
            raise RemoteError("the repository's file list arrived incomplete; try again later")
        for entry in tree.items():
            name = entry.path.decode("utf-8", "surrogateescape")
            kind = entry.mode & _FILE_MODE_MASK
            if kind not in (_DIRECTORY_MODE, _REGULAR_FILE_MODE):
                continue  # symlinks and submodules aren't pack files
            if "/" in name or not _usable_name(name):
                skipped.append(prefix + repr(name)[1:-1])
            elif kind == _DIRECTORY_MODE:
                folders.append((entry.sha, prefix + name + "/"))
            else:
                files[prefix + name] = entry.sha.decode("ascii")
    return RemoteTree(commit=head.decode("ascii"), files=files, skipped=tuple(skipped))


def fetch_blobs(
    repo_url: str,
    blob_ids: Iterable[str],
    destination: Path,
    *,
    max_bytes: int = FETCH_MAX_BYTES,
    seconds: float = FETCH_SECONDS,
    limits: _Limits = FETCH_LIMITS,
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
    budget = _Budget(max_bytes, seconds=seconds)
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
                _keep_blobs(pack_file, set(wanted), destination, landed, limits)
                break
            _keep_blobs(pack_file, set(batch), destination, landed, limits)
            if progress is not None:
                progress(len(landed), len(wanted))
    finally:
        pack_file.unlink(missing_ok=True)
    if progress is not None:
        progress(len(landed), len(wanted))
    return landed


def _keep_blobs(
    pack_file: Path, wanted: set[str], destination: Path, landed: dict, limits: _Limits
) -> None:
    """Write each wanted blob in the pack to ``destination`` under its hash."""
    for item in _objects(pack_file, limits):
        blob_id = item.id.decode("ascii")
        if not isinstance(item, Blob) or blob_id not in wanted:
            continue
        target = destination / blob_id
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(item.as_raw_string())
        os.replace(temporary, target)
        landed[blob_id] = target
