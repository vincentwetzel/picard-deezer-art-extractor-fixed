PLUGIN_NAME = "Deezer cover art SMART MAX"
PLUGIN_AUTHOR = "Fabio Forni <livingsilver94>, modified by Vincent"
PLUGIN_DESCRIPTION = (
    "Fetch cover art from Deezer, dynamically probe for maximum CDN resolution, "
    "and avoid overwriting better existing artwork."
)
PLUGIN_VERSION = "1.2.4-smart-max"
PLUGIN_API_VERSIONS = [
    "2.5",
    "2.6",
    "2.7",
    "2.8",
    "2.9",
    "2.10",
    "2.11",
    "2.12",
    "2.13",
]
PLUGIN_LICENSE = "GPL-3.0-or-later"
PLUGIN_LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"

from typing import Any, List, Optional, Dict
from urllib.parse import urlsplit
import json
import os
import re

import picard
from picard import config
from picard.coverart import providers
from picard.coverart.image import CoverArtImage
from picard.util.astrcmp import astrcmp
from PyQt5 import QtNetwork as QtNet
from PyQt5 import QtGui
from PyQt5 import QtCore

from .deezer import Client, SearchOptions, obj
from .options import Ui_Form

__version__ = PLUGIN_VERSION


DEFAULT_SIMILARITY_THRESHOLD = 0.6

# Good practical coverage for Deezer's CDN behavior.
# The script validates ACTUAL decoded image dimensions, so it will not be fooled
# if a 4000x4000 URL silently returns a 1200x1200 image.
DEEZER_TEST_SIZES = [
    4000,
    3000,
    2500,
    2400,
    2200,
    2000,
    1900,
    1800,
    1600,
    1500,
    1400,
    1300,
    1200,
    1000,
    800,
    500,
    250,
]

LOCAL_ART_FILENAMES = [
    "folder.jpg",
    "cover.jpg",
    "front.jpg",
    "album.jpg",
    "Folder.jpg",
    "Cover.jpg",
    "Front.jpg",
    "Album.jpg",
]


def is_similar(str1: str, str2: str, min_similarity: float = DEFAULT_SIMILARITY_THRESHOLD) -> bool:
    if not str1 or not str2:
        return False

    str1_l = str1.lower()
    str2_l = str2.lower()

    if str1_l in str2_l or str2_l in str1_l:
        return True

    return astrcmp(str1_l, str2_l) >= min_similarity


def is_deezer_url(url: str) -> bool:
    return "deezer.com" in urlsplit(url).netloc


def is_deezer_cdn_url(url: str) -> bool:
    return "cdn-images.dzcdn.net" in urlsplit(url).netloc


def deezer_url_for_size(url: str, size: int) -> str:
    if not url:
        return url

    return re.sub(
        r"/\d+x\d+-([0-9a-fA-F]{6})-\d+-0-0\.jpg$",
        rf"/{size}x{size}-\1-100-0-0.jpg",
        url,
    )


def get_album_id_from_api_image_url(url: str) -> Optional[str]:
    match = re.search(r"https?://api\.deezer\.com/album/(\d+)/image", url)
    if match:
        return match.group(1)
    return None


def make_empty_art_info() -> Dict[str, Any]:
    return {
        "source": "",
        "path": "",
        "width": 0,
        "height": 0,
        "actual_max": 0,
        "byte_count": 0,
    }


def local_art_is_better_or_equal(existing: Dict[str, Any], deezer: Dict[str, Any]) -> bool:
    """
    Prefer existing art if it is larger, or if it ties on dimensions.
    This avoids needless churn and avoids replacing user-selected art with
    an equivalent Deezer image.
    """
    existing_max = existing.get("actual_max", 0)
    deezer_max = deezer.get("actual_max", 0)

    if existing_max <= 0:
        return False

    if existing_max > deezer_max:
        return True

    if existing_max == deezer_max:
        existing_bytes = existing.get("byte_count", 0)
        deezer_bytes = deezer.get("byte_count", 0)

        # If existing is same dimensions and equal/larger bytes, definitely keep it.
        # Even if bytes are smaller, still prefer existing because it may be user-curated.
        return True

    return False


class OptionsPage(providers.ProviderOptions):
    NAME = "Deezer"
    TITLE = "Deezer"
    options = [
        config.TextOption("setting", "deezerart_size", obj.CoverSize.BIG.value),
        config.FloatOption("setting", "deezerart_min_similarity", DEFAULT_SIMILARITY_THRESHOLD),
    ]
    _options_ui = Ui_Form

    def load(self):
        for s in obj.CoverSize:
            self.ui.size.addItem(str(s.name).title(), userData=s.value)

        self.ui.size.setCurrentIndex(self.ui.size.findData(config.setting["deezerart_size"]))
        self.ui.min_similarity.setValue(int(config.setting["deezerart_min_similarity"] * 100))

    def save(self):
        config.setting["deezerart_size"] = self.ui.size.currentData()
        config.setting["deezerart_min_similarity"] = float(self.ui.min_similarity.value()) / 100.0


class Provider(providers.CoverArtProvider):
    NAME = "Deezer"
    OPTIONS = OptionsPage
    _log_prefix = "Deezerart: "

    def __init__(self, coverart):
        super().__init__(coverart)

        self.client = Client(self.album.tagger.webservice)
        self._has_url_relation = False
        self._retry_search = False
        self._finished = False

        self._network_manager = QtNet.QNetworkAccessManager()
        self._active_replies = []

    def queue_images(self):
        self.log_debug("queue_images called")

        self.match_url_relations(["free streaming"], self._url_callback)

        if not self._has_url_relation:
            if not self._retry_search:
                search_opts = SearchOptions(
                    artist=self._artist(),
                    album=self.metadata["album"],
                )
            else:
                try:
                    track = self.release["media"][0]["tracks"][1]["title"]
                except (IndexError, KeyError):
                    self.error("cannot find a track name to retry a search. No cover found")
                    return self.FINISHED

                search_opts = SearchOptions(
                    artist=self._artist(),
                    track=track,
                )

            self.client.advanced_search(search_opts, self._queue_from_search)

        self.album._requests += 1
        return self.WAIT

    def error(self, msg):
        super().error(self._log_prefix + msg)

    def log_debug(self, msg: Any, *args):
        picard.log.debug(self._log_prefix + msg, *args)

    def log_info(self, msg: Any, *args):
        picard.log.info(self._log_prefix + msg, *args)

    def _url_callback(self, url: str):
        if is_deezer_url(url):
            self._has_url_relation = True
            self.client.obj_from_url(url, self._queue_from_url)

    def _configured_cover_size(self):
        try:
            return obj.CoverSize(config.setting["deezerart_size"])
        except Exception:
            return obj.CoverSize.BIG

    def _queue_from_url(self, album: obj.APIObject, error: QtNet.QNetworkReply.NetworkError):
        try:
            if error:
                self.error("could not get Deezer API object: {}".format(error))
                self._finish_provider()
                return

            if not isinstance(album, obj.Album):
                self.error("API object is not an album")
                self._finish_provider()
                return

            cover_url = album.cover_url(self._configured_cover_size())

            self.log_debug("original Deezer URL from URL relation: %s", cover_url)
            self._queue_best_available_cover(cover_url, "URL relation")

        except Exception as exc:
            self.error("unexpected error while handling URL relation: {}".format(exc))
            self._finish_provider()

    def _queue_from_search(
        self,
        results: List[obj.APIObject],
        error: Optional[QtNet.QNetworkReply.NetworkError],
    ):
        try:
            if error:
                self.error("could not fetch search results: {}".format(error))
                self._finish_provider()
                return

            if len(results) == 0:
                if self._retry_search:
                    self.error("no results found")
                    self._finish_provider()
                    return

                self._retry_search = True
                self.album._requests -= 1
                self.queue_images()
                return

            artist = self._artist()
            album = self.metadata["album"]
            min_similarity = config.setting["deezerart_min_similarity"]

            self.log_debug("Deezer search returned %s results", len(results))

            for result in results:
                if not isinstance(result, obj.Track):
                    continue

                result_artist = getattr(result.artist, "name", "")
                result_album = getattr(result.album, "title", "")

                self.log_debug(
                    "checking Deezer result: artist=%r album=%r",
                    result_artist,
                    result_album,
                )

                if not is_similar(artist, result_artist, min_similarity):
                    self.log_debug(
                        "artist similarity below threshold: %r ~ %r",
                        artist,
                        result_artist,
                    )
                    continue

                if not is_similar(album, result_album, min_similarity):
                    self.log_debug(
                        "album similarity below threshold: %r ~ %r",
                        album,
                        result_album,
                    )
                    continue

                cover_url = result.album.cover_url(self._configured_cover_size())

                self.log_debug("matched Deezer artist: %s", result_artist)
                self.log_debug("matched Deezer album: %s", result_album)
                self.log_debug("original Deezer URL from search: %s", cover_url)

                self._queue_best_available_cover(cover_url, "Deezer search")
                return

            self.error("no result matched the criteria")
            self._finish_provider()

        except Exception as exc:
            self.error("unexpected error while handling search results: {}".format(exc))
            self._finish_provider()

    def _make_request(self, url: str):
        request = QtNet.QNetworkRequest(QtCore.QUrl(url))

        try:
            request.setRawHeader(b"User-Agent", b"MusicBrainz Picard DeezerArt SmartMax/1.2.4")
        except Exception:
            pass

        return request

    def _track_reply(self, reply):
        self._active_replies.append(reply)

    def _untrack_reply(self, reply):
        try:
            self._active_replies.remove(reply)
        except ValueError:
            pass

    def _queue_best_available_cover(self, original_url: str, source: str):
        if not original_url:
            self.error("no cover URL returned by Deezer")
            self._finish_provider()
            return

        self.log_debug("normalizing Deezer cover URL from %s: %s", source, original_url)

        self._resolve_to_cdn_url(
            original_url,
            lambda cdn_url: self._probe_best_cdn_cover(cdn_url, source),
        )

    def _resolve_to_cdn_url(self, url: str, callback):
        """
        Convert:
            https://api.deezer.com/album/<id>/image?size=big

        into a real CDN URL from:
            https://api.deezer.com/album/<id>
        """
        if is_deezer_cdn_url(url):
            self.log_debug("already have Deezer CDN URL: %s", url)
            callback(url)
            return

        album_id = get_album_id_from_api_image_url(url)

        if not album_id:
            self.log_debug("URL is not CDN and no album ID could be parsed; using as-is: %s", url)
            callback(url)
            return

        api_url = f"https://api.deezer.com/album/{album_id}"
        self.log_debug("resolving Deezer album API URL: %s", api_url)

        reply = self._network_manager.get(self._make_request(api_url))
        self._track_reply(reply)

        def on_finished(reply=reply, api_url=api_url):
            try:
                status = reply.attribute(QtNet.QNetworkRequest.HttpStatusCodeAttribute)

                if status != 200:
                    self.error(
                        "Deezer album API resolve failed: http={} url={}".format(status, api_url)
                    )
                    callback(url)
                    return

                raw = bytes(reply.readAll()).decode("utf-8", errors="replace")
                data = json.loads(raw)

                cdn_url = (
                    data.get("cover_xl")
                    or data.get("cover_big")
                    or data.get("cover_medium")
                    or data.get("cover_small")
                    or data.get("cover")
                    or url
                )

                self.log_debug("resolved Deezer CDN URL: %s", cdn_url)
                callback(cdn_url)

            except Exception as exc:
                self.error("exception resolving Deezer CDN URL: {}".format(exc))
                callback(url)

            finally:
                self._untrack_reply(reply)
                reply.deleteLater()

        reply.finished.connect(on_finished)

    def _probe_best_cdn_cover(self, cdn_url: str, source: str):
        if not cdn_url:
            self.error("no CDN cover URL available")
            self._finish_provider()
            return

        candidates = []
        seen = set()

        for size in DEEZER_TEST_SIZES:
            candidate = deezer_url_for_size(cdn_url, size)

            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append((size, candidate))

        if cdn_url not in seen:
            candidates.append((0, cdn_url))

        if not candidates:
            self.error("no Deezer cover candidates generated")
            self._finish_provider()
            return

        self.log_debug("generated %s Deezer cover candidates", len(candidates))

        best = {
            "url": cdn_url,
            "requested_size": 0,
            "actual_width": 0,
            "actual_height": 0,
            "actual_max": 0,
            "byte_count": 0,
        }

        pending = {"count": len(candidates)}

        def finish_one():
            pending["count"] -= 1

            if pending["count"] > 0:
                return

            self.log_info(
                "best Deezer cover: requested=%s actual=%sx%s bytes=%s url=%s",
                best["requested_size"],
                best["actual_width"],
                best["actual_height"],
                best["byte_count"],
                best["url"],
            )

            existing = self._get_best_existing_art()

            if local_art_is_better_or_equal(existing, best):
                self.log_info(
                    "skipping Deezer art because existing art is better or equal: "
                    "source=%s actual=%sx%s bytes=%s path=%s",
                    existing.get("source", ""),
                    existing.get("width", 0),
                    existing.get("height", 0),
                    existing.get("byte_count", 0),
                    existing.get("path", ""),
                )
                self._finish_provider()
                return

            self.queue_put(CoverArtImage(best["url"]))
            self.log_info("queued Deezer cover using %s", source)
            self._finish_provider()

        for requested_size, candidate_url in candidates:
            self.log_debug("probing Deezer requested=%s url=%s", requested_size, candidate_url)

            reply = self._network_manager.get(self._make_request(candidate_url))
            self._track_reply(reply)

            def on_finished(
                reply=reply,
                requested_size=requested_size,
                candidate_url=candidate_url,
            ):
                try:
                    status = reply.attribute(QtNet.QNetworkRequest.HttpStatusCodeAttribute)

                    if status != 200:
                        self.log_debug(
                            "probe failed requested=%s http=%s url=%s",
                            requested_size,
                            status,
                            candidate_url,
                        )
                        return

                    data = bytes(reply.readAll())
                    byte_count = len(data)

                    image = QtGui.QImage()
                    loaded = image.loadFromData(data)

                    if not loaded or image.isNull():
                        self.log_debug(
                            "probe decode failed requested=%s bytes=%s url=%s",
                            requested_size,
                            byte_count,
                            candidate_url,
                        )
                        return

                    width = image.width()
                    height = image.height()
                    actual_max = max(width, height)

                    self.log_debug(
                        "probe result requested=%s actual=%sx%s bytes=%s url=%s",
                        requested_size,
                        width,
                        height,
                        byte_count,
                        candidate_url,
                    )

                    if (
                        actual_max > best["actual_max"]
                        or (
                            actual_max == best["actual_max"]
                            and byte_count > best["byte_count"]
                        )
                    ):
                        best["url"] = candidate_url
                        best["requested_size"] = requested_size
                        best["actual_width"] = width
                        best["actual_height"] = height
                        best["actual_max"] = actual_max
                        best["byte_count"] = byte_count

                except Exception as exc:
                    self.error(
                        "probe exception requested={} error={} url={}".format(
                            requested_size,
                            exc,
                            candidate_url,
                        )
                    )

                finally:
                    self._untrack_reply(reply)
                    reply.deleteLater()
                    finish_one()

            reply.finished.connect(on_finished)

    def _get_best_existing_art(self) -> Dict[str, Any]:
        best = make_empty_art_info()

        for file in self._iter_album_files():
            embedded = self._get_best_embedded_art_for_file(file)

            if embedded["actual_max"] > best["actual_max"]:
                best = embedded

            folder_art = self._get_best_folder_art_for_file(file)

            if folder_art["actual_max"] > best["actual_max"]:
                best = folder_art

        return best

    def _iter_album_files(self):
        """
        Picard internals vary a bit by version. Try several safe ways of getting
        files associated with the current album.
        """
        try:
            for file in self.album.iterfiles():
                yield file
            return
        except Exception:
            pass

        try:
            files = getattr(self.album, "files", [])
            for file in files:
                yield file
            return
        except Exception:
            pass

        try:
            for track in self.album.tracks:
                for file in getattr(track, "files", []):
                    yield file
        except Exception:
            return

    def _get_filename_for_file(self, file) -> str:
        for attr in ("filename", "name"):
            value = getattr(file, attr, None)
            if value:
                return value

        try:
            return file.filename
        except Exception:
            return ""

    def _get_best_folder_art_for_file(self, file) -> Dict[str, Any]:
        best = make_empty_art_info()
        filename = self._get_filename_for_file(file)

        if not filename:
            return best

        directory = os.path.dirname(filename)

        if not directory or not os.path.isdir(directory):
            return best

        for art_name in LOCAL_ART_FILENAMES:
            path = os.path.join(directory, art_name)

            if not os.path.isfile(path):
                continue

            info = self._inspect_image_file(path, source="folder-art")

            if info["actual_max"] > best["actual_max"]:
                best = info

        return best

    def _inspect_image_file(self, path: str, source: str) -> Dict[str, Any]:
        info = make_empty_art_info()
        info["source"] = source
        info["path"] = path

        try:
            image = QtGui.QImage(path)

            if image.isNull():
                return info

            byte_count = os.path.getsize(path)

            info["width"] = image.width()
            info["height"] = image.height()
            info["actual_max"] = max(image.width(), image.height())
            info["byte_count"] = byte_count

            self.log_debug(
                "found existing %s image: %sx%s bytes=%s path=%s",
                source,
                info["width"],
                info["height"],
                byte_count,
                path,
            )

        except Exception as exc:
            self.log_debug("could not inspect image file %s: %s", path, exc)

        return info

    def _get_best_embedded_art_for_file(self, file) -> Dict[str, Any]:
        """
        Try to read embedded artwork already present in Picard's loaded metadata.
        This is intentionally defensive because Picard/mutagen objects vary by
        format and Picard version.
        """
        best = make_empty_art_info()
        best["source"] = "embedded-art"

        try:
            metadata = getattr(file, "metadata", None)

            if metadata is None:
                return best

            image_sources = []

            # Common Picard metadata image containers.
            for attr in ("images", "artwork", "covers"):
                value = getattr(metadata, attr, None)
                if value:
                    image_sources.append(value)

            # Some Picard metadata objects expose images like dict values.
            try:
                value = metadata.get("~picture")
                if value:
                    image_sources.append(value)
            except Exception:
                pass

            for source in image_sources:
                candidates = []

                if isinstance(source, (list, tuple)):
                    candidates.extend(source)
                else:
                    candidates.append(source)

                for candidate in candidates:
                    data = self._extract_image_bytes(candidate)

                    if not data:
                        continue

                    image = QtGui.QImage()
                    loaded = image.loadFromData(data)

                    if not loaded or image.isNull():
                        continue

                    width = image.width()
                    height = image.height()
                    actual_max = max(width, height)
                    byte_count = len(data)

                    if (
                        actual_max > best["actual_max"]
                        or (
                            actual_max == best["actual_max"]
                            and byte_count > best["byte_count"]
                        )
                    ):
                        best["width"] = width
                        best["height"] = height
                        best["actual_max"] = actual_max
                        best["byte_count"] = byte_count
                        best["path"] = self._get_filename_for_file(file)

            if best["actual_max"] > 0:
                self.log_debug(
                    "found existing embedded image: %sx%s bytes=%s file=%s",
                    best["width"],
                    best["height"],
                    best["byte_count"],
                    best["path"],
                )

        except Exception as exc:
            self.log_debug("could not inspect embedded artwork: %s", exc)

        return best

    def _extract_image_bytes(self, candidate) -> Optional[bytes]:
        """
        Best-effort extraction from common Picard/mutagen/Picture-like objects.
        """
        if candidate is None:
            return None

        if isinstance(candidate, bytes):
            return candidate

        for attr in ("data", "_data"):
            try:
                value = getattr(candidate, attr, None)
                if isinstance(value, bytes):
                    return value
            except Exception:
                pass

        try:
            if hasattr(candidate, "get_data"):
                value = candidate.get_data()
                if isinstance(value, bytes):
                    return value
        except Exception:
            pass

        try:
            if hasattr(candidate, "data"):
                value = candidate.data
                if isinstance(value, bytes):
                    return value
        except Exception:
            pass

        return None

    def _finish_provider(self):
        if getattr(self, "_finished", False):
            return

        self._finished = True

        try:
            self.album._requests -= 1
        except Exception:
            pass

        self.next_in_queue()

    def _artist(self) -> str:
        try:
            return self.metadata.getraw("~albumartists")[0]
        except Exception:
            return self.metadata["albumartist"] or self.metadata["artist"] or ""


providers.register_cover_art_provider(Provider)
