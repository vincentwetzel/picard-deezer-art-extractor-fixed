PLUGIN_NAME = "Deezer cover art CUSTOM DEBUG"
PLUGIN_AUTHOR = "Fabio Forni <livingsilver94>, modified by Vincent"
PLUGIN_DESCRIPTION = "Fetch cover arts from Deezer, dynamically probing for maximum CDN resolution"
PLUGIN_VERSION = "1.2.3-custom-dynamic-max"
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

from typing import Any, List, Optional
from urllib.parse import urlsplit
import json
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

picard.log.error("DEEZER LOADED FROM: %s", __file__)
picard.log.error("DEEZER CWD: %s", __import__("os").getcwd())

DEFAULT_SIMILARITY_THRESHOLD = 0.6

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

        picard.log.error("DEEZER PROVIDER __init__ CALLED")

        self.client = Client(self.album.tagger.webservice)
        self._has_url_relation = False
        self._retry_search = False
        self._finished = False

        self._network_manager = QtNet.QNetworkAccessManager()
        self._active_replies = []

    def queue_images(self):
        picard.log.error("DEEZER queue_images CALLED")

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

    def log_error(self, msg: Any, *args):
        picard.log.error(self._log_prefix + msg, *args)

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

            self.log_error("original Deezer URL from URL relation: %s", cover_url)
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

            self.log_error("Deezer search returned %s results", len(results))

            for result in results:
                if not isinstance(result, obj.Track):
                    continue

                result_artist = getattr(result.artist, "name", "")
                result_album = getattr(result.album, "title", "")

                self.log_error(
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

                self.log_error("matched Deezer artist: %s", result_artist)
                self.log_error("matched Deezer album: %s", result_album)
                self.log_error("original Deezer URL from search: %s", cover_url)

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
            request.setRawHeader(b"User-Agent", b"MusicBrainz Picard DeezerArt Custom/1.2.3")
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

        self.log_error("normalizing Deezer cover URL from %s: %s", source, original_url)

        self._resolve_to_cdn_url(
            original_url,
            lambda cdn_url: self._probe_best_cdn_cover(cdn_url, source),
        )

    def _resolve_to_cdn_url(self, url: str, callback):
        """
        The old plugin often gives URLs like:

            https://api.deezer.com/album/7927764/image?size=big

        The dynamic max-size rewrite only works on CDN URLs like:

            https://cdn-images.dzcdn.net/images/cover/hash/1000x1000-000000-80-0-0.jpg

        This function converts API image URLs into real CDN cover_xl URLs by querying:
            https://api.deezer.com/album/<id>
        """
        if is_deezer_cdn_url(url):
            self.log_error("already have Deezer CDN URL: %s", url)
            callback(url)
            return

        album_id = get_album_id_from_api_image_url(url)

        if not album_id:
            self.log_error("URL is not a CDN URL and no album ID could be parsed; using as-is: %s", url)
            callback(url)
            return

        api_url = f"https://api.deezer.com/album/{album_id}"
        self.log_error("resolving Deezer album API URL: %s", api_url)

        reply = self._network_manager.get(self._make_request(api_url))
        self._track_reply(reply)

        def on_finished(reply=reply, api_url=api_url):
            try:
                status = reply.attribute(QtNet.QNetworkRequest.HttpStatusCodeAttribute)

                if status != 200:
                    self.log_error(
                        "Deezer album API resolve failed: http=%s url=%s",
                        status,
                        api_url,
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

                self.log_error("resolved Deezer CDN URL: %s", cdn_url)
                callback(cdn_url)

            except Exception as exc:
                self.log_error("exception resolving Deezer CDN URL: %s", exc)
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

        self.log_error("generated %s Deezer cover candidates", len(candidates))

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

            final_url = best["url"]

            self.log_error("DEEZER BEST REQUESTED: %s", best["requested_size"])
            self.log_error(
                "DEEZER BEST ACTUAL: %sx%s",
                best["actual_width"],
                best["actual_height"],
            )
            self.log_error("DEEZER BEST BYTES: %s", best["byte_count"])
            self.log_error("DEEZER FINAL URL: %s", final_url)

            self.queue_put(CoverArtImage(final_url))
            self.log_error("queued Deezer cover using %s", source)
            self._finish_provider()

        for requested_size, candidate_url in candidates:
            self.log_error("DEEZER PROBING requested=%s url=%s", requested_size, candidate_url)

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
                        self.log_error(
                            "DEEZER PROBE FAILED requested=%s http=%s url=%s",
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
                        self.log_error(
                            "DEEZER PROBE DECODE FAILED requested=%s bytes=%s url=%s",
                            requested_size,
                            byte_count,
                            candidate_url,
                        )
                        return

                    width = image.width()
                    height = image.height()
                    actual_max = max(width, height)

                    self.log_error(
                        "DEEZER PROBE RESULT requested=%s actual=%sx%s bytes=%s url=%s",
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
                    self.log_error(
                        "DEEZER PROBE EXCEPTION requested=%s error=%s url=%s",
                        requested_size,
                        exc,
                        candidate_url,
                    )

                finally:
                    self._untrack_reply(reply)
                    reply.deleteLater()
                    finish_one()

            reply.finished.connect(on_finished)

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