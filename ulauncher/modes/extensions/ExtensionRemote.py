from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from os.path import basename, getmtime, isdir
from shutil import move, rmtree, which
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen, urlretrieve

from ulauncher.config import API_VERSION, PATHS
from ulauncher.modes.extensions import extension_finder
from ulauncher.modes.extensions.ExtensionDb import ExtensionDb, ExtensionRecord
from ulauncher.modes.extensions.ExtensionManifest import ExtensionIncompatibleWarning, ExtensionManifest
from ulauncher.utils.untar import untar
from ulauncher.utils.version import satisfies

logger = logging.getLogger()
db = ExtensionDb.load()


class ExtensionRemoteError(Exception):
    pass


class InvalidExtensionUrlWarning(Exception):
    pass


class ExtensionNetworkError(Exception):
    pass


class ExtensionRemote:
    def __init__(self, url: str):
        try:
            self._use_git = bool(which("git"))
            self.url = url.strip().lower()
            # Reformat to URL format if it's SSH
            if self.url.startswith("git@"):
                self.url = "git://" + self.url[4:].replace(":", "/")

            url_parts = urlparse(self.url)
            self.path = url_parts.path[1:]

            if url_parts.scheme in ("", "file"):
                assert isdir(url_parts.path)
                self.host = ""
                self.protocol = "file"
            else:
                if url_parts.scheme != "https":
                    logger.warning('Unsupported URL protocol: "%s". Will attempt to use HTTPS', url_parts.scheme)
                self.host = url_parts.netloc
                self.protocol = "https"

            assert self.path
            assert self.host or self.protocol == "file"

            if self.host in ("github.com", "gitlab.com", "codeberg.org"):
                # Sanitize URLs with known hosts and invalid trailing paths like /blob/master or /issues, /wiki etc
                user, repo, *_ = self.path.split("/", 2)
                if repo.endswith(".git"):
                    repo = repo[:-4]
                self.path = f"{user}/{repo}"
                self._use_git = False

            self.url = f"{self.protocol}://{self.host}/{self.path}"

        except Exception as e:
            msg = f"Invalid URL: {url}"
            raise InvalidExtensionUrlWarning(msg) from e

        self.extension_id = ".".join(
            [
                *reversed(self.host.split(".") if self.host else []),
                *self.path.split("/"),
            ]
        )
        self._dir = extension_finder.get_user_dir(self.extension_id)
        self._git_dir = f"{PATHS.USER_EXTENSIONS_DIR}/.git/{self.extension_id}.git"

    def _get_download_url(self, commit: str) -> str:
        if self.host == "gitlab.com":
            repo = self.path.split("/")[1]
            return f"{self.url}/-/archive/{commit}/{repo}-{commit}.tar.gz"
        return f"{self.url}/archive/{commit}.tar.gz"

    def _get_refs(self) -> dict[str, str]:
        refs = {}
        url = f"{self.url}.git" if self.host in ("github.com", "gitlab.com", "codeberg.org") else self.url
        try:
            if self._use_git:
                if isdir(self._git_dir):
                    subprocess.run(
                        [
                            "git",
                            f"--git-dir={self._git_dir}",
                            "fetch",
                            "origin",
                            "+refs/heads/*:refs/heads/*",
                            "--prune",
                            "--prune-tags",
                        ],
                        check=True,
                    )
                else:
                    os.makedirs(self._git_dir)
                    subprocess.run(["git", "clone", "--bare", url, self._git_dir], check=True)

                response = subprocess.check_output(["git", "ls-remote", self._git_dir]).decode().strip().split("\n")
            else:
                with urlopen(f"{url}/info/refs?service=git-upload-pack") as reader:
                    response = reader.read().decode().strip().split("\n")
            if response:
                if response[-1] == "0000":
                    # Convert "smart" response, to more readable "dumb" response
                    # See https://www.git-scm.com/docs/http-protocol#_discovering_references
                    response = [r.split("\x00")[0][8:] if r.startswith("0000") else r[4:] for r in response[1:-1]]

                for row in response:
                    commit, ref = row.split()
                    refs[basename(ref)] = commit

        except Exception as e:
            if isinstance(e, (HTTPError, URLError)):
                msg = f'Could not access repository resource "{self.url}"'
                raise ExtensionNetworkError(msg) from e

            logger.warning("Unexpected error fetching extension versions '%s' (%s: %s)", self.url, type(e).__name__, e)
            msg = f'Could not fetch reference "{ref}" for {self.url}.'
            raise ExtensionRemoteError(msg) from e

        return refs

    def get_compatible_hash(self) -> str:
        """
        Returns the commit hash for the highest compatible version, matching using branch names
        and tags names starting with "apiv", ex "apiv3" and "apiv3.2"
        New method for v6. The new behavior is intentionally undocumented because we still
        want extension devs to use the old way until Ulauncher 5/apiv2 is fully phased out
        """
        remote_refs = self._get_refs()
        compatible = {
            ref: sha for ref, sha in remote_refs.items() if ref.startswith("apiv") and satisfies(API_VERSION, ref[4:])
        }

        if compatible:
            return compatible[max(compatible)]

        # Try to get the commit ref for head, fallback on "HEAD" as a string as that can be used also
        return remote_refs.get("HEAD", "HEAD")

    def download(self, commit_hash=None, warn_if_overwrite=True):
        if not commit_hash:
            commit_hash = self.get_compatible_hash()
        output_dir_exists = isdir(self._dir)

        if output_dir_exists and warn_if_overwrite:
            logger.warning('Extension with URL "%s" is already installed. Overwriting', self.url)

        if self._use_git and isdir(self._git_dir):
            os.makedirs(self._dir, exist_ok=True)
            subprocess.run(
                ["git", f"--git-dir={self._git_dir}", f"--work-tree={self._dir}", "checkout", commit_hash, "."],
                check=True,
            )
            commit_timestamp = float(
                subprocess.check_output(
                    ["git", f"--git-dir={self._git_dir}", "show", "-s", "--format=%ct", commit_hash]
                )
                .decode()
                .strip()
            )

        else:
            with NamedTemporaryFile(suffix=".tar.gz", prefix="ulauncher_dl_") as tmp_file:
                urlretrieve(self._get_download_url(commit_hash), tmp_file.name)
                with TemporaryDirectory(prefix="ulauncher_ext_") as tmp_root_dir:
                    untar(tmp_file.name, tmp_root_dir)
                    subdirs = os.listdir(tmp_root_dir)
                    if len(subdirs) != 1:
                        msg = f"Invalid archive for {self.url}."
                        raise ExtensionRemoteError(msg)
                    tmp_dir = f"{tmp_root_dir}/{subdirs[0]}"
                    manifest = ExtensionManifest.load(f"{tmp_dir}/manifest.json")
                    if not satisfies(API_VERSION, manifest.api_version):
                        if not satisfies("2.0", manifest.api_version):
                            msg = f"{manifest.name} does not support Ulauncher API v{API_VERSION}."
                            raise ExtensionIncompatibleWarning(msg)
                        logger.warning("Falling back on using API 2.0 version for %s.", self.url)

                    if output_dir_exists:
                        rmtree(self._dir)
                    move(tmp_dir, self._dir)
            commit_timestamp = getmtime(self._dir)

        ext_record = ExtensionRecord.create(
            id=self.extension_id,
            commit_hash=commit_hash,
            commit_time=datetime.fromtimestamp(commit_timestamp).isoformat(),
            updated_at=datetime.now().isoformat(),
            url=self.url,
        )
        db.update({self.extension_id: ext_record})
        db.save()
