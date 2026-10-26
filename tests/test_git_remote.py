"""Reading a soundpack's git repository: the file list, and changed files by their hashes.

The repository here is a real one, made with dulwich and served by dulwich's own git web server
inside the test, so the client's requests run through the same code a real update uses, with no
network.
"""

from __future__ import annotations

import io
import time
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

    def without_trees_below_the_root(pack_file: Path):
        for item in real_objects(pack_file):
            if isinstance(item, Tree) and b"a.ogg" in [entry.path for entry in item.items()]:
                continue
            yield item

    monkeypatch.setattr(git_remote, "_objects", without_trees_below_the_root)
    with pytest.raises(git_remote.RemoteError, match="incomplete"):
        git_remote.list_tree(REPO_URL)
