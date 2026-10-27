"""Reading a soundpack's git repository: the file list, and changed files by their hashes.

The repository here is a real one, made with dulwich and served by dulwich's own git web server
inside the test, so the client's requests run through the same code a real update uses, with no
network.
"""

from __future__ import annotations

import hashlib
import io
import time
import zlib
from email.message import Message
from pathlib import Path

import pytest
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import MemoryRepo
from dulwich.server import DictBackend
from dulwich.web import make_wsgi_chain

from genericmud.packs import git_remote, git_sync

REPO_URL = "https://forge.example/pack.git"


def _repository(files: dict[str, bytes]) -> MemoryRepo:
    """A repository whose default branch holds ``files`` (path -> contents)."""
    repo = MemoryRepo()
    store = repo.object_store

    def tree_for(entries: dict[str, object]) -> bytes:
        tree = Tree()
        for name, value in sorted(entries.items()):
            if isinstance(value, dict):
                tree.add(name.encode(), 0o040000, tree_for(value))
            else:
                blob = Blob.from_string(value)
                store.add_object(blob)
                tree.add(name.encode(), 0o100644, blob.id)
        store.add_object(tree)
        return tree.id

    nested: dict[str, object] = {}
    for path, data in files.items():
        folder = nested
        *parents, name = path.split("/")
        for part in parents:
            folder = folder.setdefault(part, {})
        folder[name] = data
    commit = Commit()
    commit.tree = tree_for(nested)
    commit.author = commit.committer = b"Pack Author <author@example.com>"
    commit.author_time = commit.commit_time = int(time.time())
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"pack files"
    store.add_object(commit)
    repo.refs[b"refs/heads/main"] = commit.id
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    return repo


class _Served:
    """A urllib-style response from the in-process git server."""

    def __init__(self, url: str, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = Message()
        for name, value in headers:
            self.headers[name] = value
        self._body = io.BytesIO(body)
        self._url = url

    def read(self, amount: int | None = None) -> bytes:
        return self._body.read() if amount is None else self._body.read(amount)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        pass


@pytest.fixture
def serve(monkeypatch):
    """Route the client's requests to a git server holding the given repository."""
    requests: list[str] = []

    def install(repo: MemoryRepo) -> list[str]:
        app = make_wsgi_chain(DictBackend({"/pack.git": repo}))

        def open_checked(request):
            url = request.full_url
            requests.append(url)
            path, _, query = url.removeprefix("https://forge.example").partition("?")
            body = request.data or b""
            environ = {
                "REQUEST_METHOD": request.get_method(),
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": request.get_header("Content-type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.url_scheme": "https",
                "SERVER_NAME": "forge.example",
                "SERVER_PORT": "443",
            }
            for name, value in request.header_items():
                environ["HTTP_" + name.upper().replace("-", "_")] = value
            answer: dict = {}
            written: list[bytes] = []

            def start_response(status, headers, _exc_info=None):
                answer["status"] = int(status.split()[0])
                answer["headers"] = headers
                return written.append  # dulwich's server writes its replies through this

            chunks = list(app(environ, start_response))
            body_out = b"".join(written) + b"".join(chunks)
            return _Served(url, answer["status"], answer["headers"], body_out)

        monkeypatch.setattr(git_remote.vault, "open_checked", open_checked)
        return requests

    return install


def test_the_file_list_comes_with_each_files_hash(serve):
    files = {"worlds/pack.mcl": b"<world/>", "sounds/hit.ogg": b"OggS...", "README": b"hi"}
    serve(_repository(files))
    tree = git_remote.list_tree(REPO_URL)
    assert tree.files == {path: git_sync.blob_sha(data) for path, data in files.items()}
    assert len(tree.commit) == 40


def _advertising_blobs(repo: MemoryRepo, *contents: bytes) -> MemoryRepo:
    """Let the test server hand out these files by their hashes, as GitHub and GitLab do
    (dulwich's server only gives out what a ref points at)."""
    for data in contents:
        sha = git_sync.blob_sha(data).encode()
        repo.refs[b"refs/blobs/" + sha] = sha
    return repo


def test_changed_files_are_fetched_by_hash_and_checked(serve, tmp_path):
    files = {"a.lua": b"one", "b.lua": b"two", "c/d.ogg": b"three"}
    requests = serve(_advertising_blobs(_repository(files), b"two", b"three"))
    wanted = [git_sync.blob_sha(b"two"), git_sync.blob_sha(b"three")]
    landed = git_remote.fetch_blobs(REPO_URL, wanted, tmp_path)
    assert sorted(landed) == sorted(wanted)
    assert {landed[sha].read_bytes() for sha in wanted} == {b"two", b"three"}
    assert not list(tmp_path.glob("*.part")) and not list(tmp_path.glob("*.pack"))
    assert len([url for url in requests if url.endswith("/git-upload-pack")]) == 1  # no fallback


def test_a_server_that_wont_give_files_by_hash_still_gives_the_latest_commit(serve, tmp_path):
    files = {"a.lua": b"one", "b.lua": b"two"}
    serve(_repository(files))  # dulwich's server refuses files by hash
    wanted = [git_sync.blob_sha(b"two")]
    landed = git_remote.fetch_blobs(REPO_URL, wanted, tmp_path)
    assert landed[wanted[0]].read_bytes() == b"two"
    assert not list(tmp_path.glob("*.pack"))


def test_a_server_that_sends_too_much_is_stopped(serve):
    serve(_repository({"big.ogg": b"x" * 100_000}))
    with pytest.raises(git_remote.RemoteError, match="more data than an update"):
        git_remote.list_tree(REPO_URL, max_bytes=50)


def test_only_https_repositories_are_read():
    with pytest.raises(git_remote.RemoteError, match="https"):
        git_remote.list_tree("http://forge.example/pack.git")


def test_a_busy_host_says_so_in_plain_words(monkeypatch):
    from urllib.error import HTTPError

    def refuse(request):
        raise HTTPError(request.full_url, 429, "Too Many Requests", Message(), None)

    monkeypatch.setattr(git_remote.vault, "open_checked", refuse)
    with pytest.raises(git_remote.RemoteError, match="busy"):
        git_remote.list_tree(REPO_URL)


def test_the_listing_is_refused_when_a_folder_is_missing(serve, monkeypatch):
    # The updater deletes files that left the repository, so a partial list mustn't pass.
    serve(_repository({"sounds/a.ogg": b"a", "sounds/b.ogg": b"b"}))
    real_objects = git_remote._objects

    def without_trees_below_the_root(pack_file: Path, limits):
        for item in real_objects(pack_file, limits):
            if isinstance(item, Tree) and b"a.ogg" in [entry.path for entry in item.items()]:
                continue
            yield item

    monkeypatch.setattr(git_remote, "_objects", without_trees_below_the_root)
    with pytest.raises(git_remote.RemoteError, match="incomplete"):
        git_remote.list_tree(REPO_URL)


class _Endless:
    """A response that never stops sending."""

    status = 200
    headers = Message()

    def read(self, amount=None):
        return b"x" * (amount or 1_000_000)

    def geturl(self):
        return REPO_URL

    def close(self):
        pass


def test_reading_a_whole_response_stops_at_the_budget():
    # dulwich reads some replies whole; one that never ends used to be read until memory ran
    # out, the budget only counting it afterwards.
    response = git_remote._Response(_Endless(), REPO_URL, git_remote._Budget(1_000_000))
    with pytest.raises(git_remote.RemoteError, match="more data"):
        response.read()


def test_a_server_that_trickles_or_dawdles_is_given_up_on():
    now = [0.0]
    trickle = git_remote._Budget(10**9, seconds=600, clock=lambda: now[0])
    for _second in range(60):
        now[0] += 1
        trickle.spend(1)  # a byte a second is fine for the first minute
    now[0] += 1
    with pytest.raises(git_remote.RemoteError, match="too slowly"):
        trickle.spend(1)
    now[0] = 0.0
    steady = git_remote._Budget(10**12, seconds=600, clock=lambda: now[0])
    now[0] = 601
    with pytest.raises(git_remote.RemoteError, match="too long"):
        steady.spend(10**9)


def test_a_file_that_would_inflate_past_the_limit_is_refused_before_inflating(
    serve, tmp_path, monkeypatch
):
    # Three megabytes of zeros compress to a few kilobytes: the byte budget doesn't see them.
    zeros = b"\0" * (3 * 1024 * 1024)
    serve(_advertising_blobs(_repository({"big.ogg": zeros}), zeros))

    def never(*_args, **_kwargs):
        raise AssertionError("the pack was inflated before its sizes were checked")

    monkeypatch.setattr(git_remote.PackData, "from_file", never)
    limits = git_remote._Limits(object_bytes=1024 * 1024, total_bytes=1 << 30, objects=1000)
    with pytest.raises(git_remote.RemoteError, match="bigger than"):
        git_remote.fetch_blobs(REPO_URL, [git_sync.blob_sha(zeros)], tmp_path, limits=limits)


def _pack_object(kind: int, payload: bytes, base: bytes = b"") -> bytes:
    """One object of a git pack: its type and size header, a delta's base, zlib data."""
    size = len(payload)
    header = bytearray([(kind << 4) | (size & 0x0F)])
    size >>= 4
    while size:
        header[-1] |= 0x80
        header.append(size & 0x7F)
        size >>= 7
    return bytes(header) + base + zlib.compress(payload)


def _delta_size(size: int) -> bytes:
    out = bytearray()
    while True:
        byte = size & 0x7F
        size >>= 7
        out.append(byte | (0x80 if size else 0))
        if not size:
            return bytes(out)


def test_a_delta_that_would_rebuild_something_huge_is_refused(tmp_path):
    # A delta says how big the object it rebuilds is; a few bytes of copy instructions can
    # make that gigabytes out of a small file.
    base = _pack_object(3, b"x" * 100)
    delta = _pack_object(6, _delta_size(100) + _delta_size(2**31), base=bytes([len(base)]))
    body = b"PACK" + (2).to_bytes(4, "big") + (2).to_bytes(4, "big") + base + delta
    pack_file = tmp_path / "huge.pack"
    pack_file.write_bytes(body + hashlib.sha1(body).digest())
    with pytest.raises(git_remote.RemoteError, match="bigger than"):
        git_remote._check_sizes(pack_file, git_remote.FETCH_LIMITS)


def test_names_no_file_could_safely_have_are_left_out(serve):
    # A line break would add lines to the baseline, a colon writes to a hidden part of another
    # file on Windows, and Windows drops a trailing space ("settings.ini " is settings.ini).
    files = {
        "good.ogg": b"g", "bad\nname.ogg": b"b", "settings.ini ": b"s", "con.ogg": b"c",
        "a:b.ogg": b"a", "odd:folder/inner.ogg": b"i",
    }
    serve(_repository(files))
    tree = git_remote.list_tree(REPO_URL)
    assert set(tree.files) == {"good.ogg"}
    assert len(tree.skipped) == 5

