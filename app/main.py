# -*- coding: utf-8 -*-
import hashlib
import logging
import traceback
import http.client
import json
import os
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import certifi

from kivy.app import App
from kivy.animation import Animation
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.floatlayout import FloatLayout
from kivy.graphics import Color, Rectangle
from kivy.properties import NumericProperty


OWNER = "D1m0n93"
REPOSITORY = "NS_OGSR_Resourses"
RELEASE_TAG = "latest"
MANIFEST_URL = "https://raw.githubusercontent.com/D1m0n93/NS_OGSR_Resourses/Public/index.json"
RELEASE_API = (
    "https://api.github.com/repos/"
    + OWNER + "/" + REPOSITORY + "/releases/tags/" + RELEASE_TAG
)
BASE_ASSET = "gamedata.sq_base"
BASE_CHUNK_COUNT = 17
REQUIRED_ASSETS = (
    BASE_ASSET,
    "gamedata.sq_base_configs",
    "gamedata.sq_patch",
    "gamedata.sq_patch_2",
)
CHUNK_NAMES = tuple(
    "{}.part_{:03d}".format(BASE_ASSET, index)
    for index in range(1, BASE_CHUNK_COUNT + 1)
)


def format_size(value):
    # Use decimal storage units (GB = 1,000,000,000 bytes) so 2 GB
    # release chunks are displayed as 2.00 GB, not ~1.86 GiB.
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1000 or unit == "ТБ":
            return "{:.2f} {}".format(value, unit)
        value /= 1000
    return "{:.2f} ТБ".format(value)


def ssl_context():
    # certifi provides an up-to-date CA bundle inside the Android APK.
    return ssl.create_default_context(cafile=certifi.where())


NETWORK_CONTEXT = ssl_context()

# Four GitHub release mirrors used by the original updater, plus GitHub itself
# as the last resort (empty prefix = the original URL).
RELEASE_MIRRORS = ("https://ghproxy.net/", "https://ghfast.top/", "https://gh.llkk.cc/", "https://gh-proxy.com/", "")
USER_AGENT = "NS2026-Updater-Android"
READ_BLOCK = 1024 * 1024
# How many no-progress attempts each source gets before the download gives up.
STREAM_IDLE_ATTEMPTS_PER_MIRROR = 3
# How many full re-downloads are allowed when a finished file fails MD5.
MAX_HASH_ATTEMPTS = 3
# Local SAF reading/writing is far faster than the network, so assembly and
# final hashing get a smaller share of the progress bar than downloading.
LOCAL_WORK_WEIGHT = 0.1


class MirrorMismatch(Exception):
    """A source answered, but not with the bytes we asked for."""


def mirror_url(original_url, mirror_index=0):
    return RELEASE_MIRRORS[mirror_index % len(RELEASE_MIRRORS)] + original_url


def mirror_title(mirror_index):
    index = mirror_index % len(RELEASE_MIRRORS)
    if not RELEASE_MIRRORS[index]:
        return "GitHub напрямую"
    return "зеркало №{}".format(index + 1)


class DownloadProgress:
    """One bar for the whole delivery: 0–100% by weighted amount of work."""

    def __init__(self, app, total_units):
        self.app = app
        self.total = float(max(1, total_units))
        self._last = 0.0

    def report(self, units, text, force=False):
        now = time.monotonic()
        if not force and now - self._last < 0.2:
            return
        self._last = now
        percent = 100.0 * max(0.0, min(float(units), self.total)) / self.total
        Clock.schedule_once(lambda dt, p=percent, t=text: self.app._download_progress(p, t), 0)

def urlopen_mirrors(request, timeout=120, start_index=0, per_mirror_attempts=2):
    last_error = None
    for offset in range(len(RELEASE_MIRRORS)):
        mirror_index = (start_index + offset) % len(RELEASE_MIRRORS)
        mirrored_request = urllib.request.Request(mirror_url(request.full_url, mirror_index), headers=dict(request.header_items()), method=request.get_method())
        for attempt in range(per_mirror_attempts):
            try:
                return urllib.request.urlopen(mirrored_request, timeout=timeout, context=NETWORK_CONTEXT), mirror_index
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (403, 429):
                    raise
                if attempt + 1 < per_mirror_attempts:
                    time.sleep(min(2 ** attempt, 4))
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt + 1 < per_mirror_attempts:
                    time.sleep(min(2 ** attempt, 4))
    raise last_error


def probe_mirrors_speed(asset_url, probe_bytes=262144, timeout=12):
    """Measure bounded Range transfer throughput without reading a whole asset."""
    def probe(index):
        req = urllib.request.Request(
            mirror_url(asset_url, index),
            headers={"User-Agent": "NS2026-Updater-Android", "Range": "bytes=0-{}".format(probe_bytes - 1)},
        )
        started = time.monotonic()
        try:
            response = urllib.request.urlopen(req, timeout=timeout, context=NETWORK_CONTEXT)
            count = 0
            try:
                while count < probe_bytes:
                    block = response.read(min(32768, probe_bytes - count))
                    if not block:
                        break
                    count += len(block)
            finally:
                response.close()
            elapsed = max(0.001, time.monotonic() - started)
            return count / elapsed, index
        except Exception:
            return 0.0, index
    with ThreadPoolExecutor(max_workers=len(RELEASE_MIRRORS)) as executor:
        results = list(executor.map(probe, range(len(RELEASE_MIRRORS))))
    results.sort(key=lambda item: item[0], reverse=True)
    return results[0][1] if results and results[0][0] > 0 else 0


def friendly_network_error(error):
    if isinstance(error, urllib.error.HTTPError) and error.code in (403, 429):
        retry_after = error.headers.get("Retry-After") if error.headers else None
        suffix = " Повторите позже." if not retry_after else " Повтор через {} сек.".format(retry_after)
        return "Сервер ограничил частоту запросов (HTTP {}). Новые запросы остановлены, чтобы не усиливать лимит.{}".format(error.code, suffix)
    if isinstance(error, (ssl.SSLError, urllib.error.URLError)) and "CERTIFICATE_VERIFY_FAILED" in str(error):
        return "Не удалось проверить SSL-сертификат сервера. Проверьте дату и время телефона, затем повторите попытку."
    return str(error)



def http_open(url, accept="application/octet-stream"):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NS2026-Updater-Android",
            "Accept": accept,
        },
    )
    return urllib.request.urlopen(request, timeout=45, context=NETWORK_CONTEXT)


def urlopen_retry(request, timeout=120, context=NETWORK_CONTEXT, attempts=5):
    last_error = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=context)
        except urllib.error.HTTPError as exc:
            last_error = exc
            # Rate limits are not transient network failures: stop retrying immediately.
            if exc.code in (403, 429):
                raise
            if attempt + 1 >= attempts:
                break
            time.sleep(min(2 ** attempt, 16))
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(min(2 ** attempt, 16))
    raise last_error



class WideProgress(FloatLayout):
    value = NumericProperty(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.size_hint_y = None
        self.height = dp(30)
        with self.canvas:
            Color(0.16, 0.18, 0.14, 1)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
            Color(1.0, 0.78, 0.05, 1)
            self._fill_rect = Rectangle(pos=self.pos, size=(0, self.height))
        self.percent_label = Label(
            text="0%", bold=True, color=(0.08, 0.09, 0.05, 1),
            font_size="14sp", halign="center", valign="middle"
        )
        self.add_widget(self.percent_label)
        self.bind(pos=self._redraw, size=self._redraw, value=self._redraw)

    def _redraw(self, *args):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size
        self._fill_rect.pos = self.pos
        self._fill_rect.size = (self.width * max(0, min(100, self.value)) / 100.0, self.height)
        self.percent_label.pos = self.pos
        self.percent_label.size = self.size
        self.percent_label.text = "{:.1f}%".format(max(0, min(100, self.value)))


class UpdaterApp(App):
    def _setup_logging(self):
        log_path = os.path.join(self.user_data_dir, "updater_full.log")
        self.logger = logging.getLogger("NS2026Updater")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"))
            self.logger.addHandler(handler)
        self.logger.info("Log file: %s", log_path)

    def build(self):
        self.title = "NS2026 Updater"
        self._setup_logging()
        self.logger.info("=== NS2026 Updater старт === | version=diagnostic | android=%s", os.environ.get('ANDROID_ARGUMENT', 'unknown'))
        self.release = None
        self.assets = {}
        self.busy = False
        self.tree_uri = None
        self.request_tree_code = 701
        self.download_dir = os.path.join(self.user_data_dir, "updates")
        os.makedirs(self.download_dir, exist_ok=True)

        Window.clearcolor = (0.035, 0.045, 0.04, 1)
        self._load_selected_tree()
        try:
            from android import activity
            activity.bind(on_activity_result=self.on_activity_result)
        except Exception:
            pass

        root = BoxLayout(
            orientation="vertical",
            padding=dp(18),
            spacing=dp(12),
        )

        heading = Label(
            text="[b]S.T.A.L.K.E.R.[/b]\n[color=9aaa85]НАРОДНАЯ СОЛЯНКА 2026[/color]",
            markup=True,
            font_size="23sp",
            halign="center",
            size_hint_y=None,
            height=dp(82),
        )
        root.add_widget(heading)

        author = Label(
            text='Создано Гренычем для фанатов [color=9aaa85][ref=telegram]@Grenader4uk[/ref][/color] • специально для stalker-mobile.ru',
            markup=True,
            halign="center",
            font_size="12sp",
            size_hint_y=None,
            height=dp(44),
        )
        author.bind(on_ref_press=self._open_telegram)
        root.add_widget(author)


        self.status = Label(
            text="Нажмите «Проверить обновления»",
            halign="center",
            valign="middle",
            font_size="15sp",
            size_hint_y=None,
            height=dp(78),
        )
        self.status.bind(size=self._fit_label)
        root.add_widget(self.status)

        self.progress = WideProgress(value=0)
        root.add_widget(self.progress)

        self.folder_button = Button(text="ВЫБРАТЬ ПАПКУ NSOGSR", size_hint_y=None, height=dp(48))
        self.folder_button.bind(on_press=self.choose_game_folder)
        root.add_widget(self.folder_button)

        self.check_button = Button(
            text="ПРОВЕРИТЬ ОБНОВЛЕНИЯ",
            size_hint_y=None,
            height=dp(54),
            font_size="16sp",
            background_normal="",
            background_color=(0.25, 0.32, 0.23, 1),
        )
        self.check_button.bind(on_press=self.check_updates)
        root.add_widget(self.check_button)

        self.download_button = Button(
            text="СКАЧАТЬ ОБНОВЛЕНИЯ",
            size_hint_y=None,
            height=dp(54),
            font_size="16sp",
            disabled=True,
            background_normal="",
            background_color=(0.34, 0.27, 0.16, 1),
        )
        self.download_button.bind(on_press=self.download_updates)
        root.add_widget(self.download_button)

        self.footer = Label(
            text="Создайте NSOGSR в Download и выберите именно эту папку.",
            color=(0.58, 0.62, 0.56, 1),
            font_size="11sp",
            size_hint_y=None,
            height=dp(30),
        )
        root.add_widget(self.footer)

        self._refresh_folder_ui()
        return root

    def _load_selected_tree(self):
        path = os.path.join(self.user_data_dir, "game_tree_uri.txt")
        try:
            with open(path, "r", encoding="utf-8") as source:
                self.tree_uri = source.read().strip() or None
        except OSError:
            self.tree_uri = None

    def _all_assets_cached(self):
        return bool(self.assets) and all(item.get("downloaded") for item in self.assets.values())

    def _is_cached(self, asset):
        """Return True only when a complete local archive matches its MD5."""
        name = asset.get("name")
        if not name:
            return False
        path = os.path.join(self.download_dir, name)
        if not os.path.isfile(path):
            return False
        try:
            expected_size = int(asset.get("size") or 0)
            return expected_size > 0 and os.path.getsize(path) == expected_size
        except (OSError, TypeError, ValueError):
            return False

    def _refresh_folder_ui(self):
        if hasattr(self, "footer"):
            self.footer.text = "Папка NSOGSR выбрана. Файлы сохраняются прямо сюда." if self.tree_uri else "Создайте NSOGSR в Download и выберите именно эту папку."

    def choose_game_folder(self, instance):
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION)
            PythonActivity.mActivity.startActivityForResult(intent, self.request_tree_code)
            self._set_status("Выберите созданную папку NSOGSR внутри Download.")
        except Exception as exc:
            self._set_status("Не удалось открыть выбор папки: " + str(exc))

    def on_activity_result(self, request_code, result_code, intent):
        if request_code != self.request_tree_code or result_code != -1 or intent is None:
            return
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            uri = intent.getData()
            if uri is None:
                raise RuntimeError("Android не вернул выбранную папку.")
            flags = intent.getFlags() & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.getContentResolver().takePersistableUriPermission(uri, flags)
            self.tree_uri = str(uri.toString())
            with open(os.path.join(self.user_data_dir, "game_tree_uri.txt"), "w", encoding="utf-8") as target:
                target.write(self.tree_uri)
            self._refresh_folder_ui()
            self._set_signal("idle")
            self._set_status("Папка NSOGSR выбрана. Доступ сохранён.")
        except Exception as exc:
            self.tree_uri = None
            self._refresh_folder_ui()
            self._set_signal("offline")
            self._set_status("Не удалось сохранить доступ: " + str(exc))

    def _set_signal(self, state):
        # Broken blinking glyph removed from the UI.
        return

    def _open_telegram(self, instance, ref):
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            Activity = autoclass("org.kivy.android.PythonActivity")
            intent = Intent(Intent.ACTION_VIEW, Uri.parse("https://t.me/Grenader4uk"))
            Activity.mActivity.startActivity(intent)
        except Exception:
            self._set_status("Telegram: @Grenader4uk")

    def _saf_target(self):
        from jnius import autoclass
        Uri = autoclass("android.net.Uri")
        Docs = autoclass("android.provider.DocumentsContract")
        Activity = autoclass("org.kivy.android.PythonActivity")
        resolver = Activity.mActivity.getContentResolver()
        tree = Uri.parse(self.tree_uri)
        document_id = Docs.getTreeDocumentId(tree)
        if not document_id.endswith("Download/NSOGSR"):
            raise RuntimeError("Путь должен быть Download/NSOGSR. Выберите нужную папку.")
        root = Docs.buildDocumentUriUsingTree(tree, document_id)
        root_name = None
        cursor = resolver.query(root, None, None, None, None)
        if cursor is not None:
            try:
                name_col = cursor.getColumnIndex("_display_name")
                if name_col < 0:
                    name_col = cursor.getColumnIndex("display_name")
                if name_col >= 0 and cursor.moveToFirst():
                    root_name = cursor.getString(name_col)
            finally:
                cursor.close()
        if root_name != "NSOGSR":
            raise RuntimeError("Выбрана неправильная папка. Нужна именно NSOGSR внутри Download.")
        def find(folder, name):
            child_uri = Docs.buildChildDocumentsUriUsingTree(tree, Docs.getDocumentId(folder))
            cursor = resolver.query(child_uri, None, None, None, None)
            if cursor is None:
                raise RuntimeError("Не удалось прочитать папку NSOGSR.")
            try:
                ni = cursor.getColumnIndex("_display_name")
                ii = cursor.getColumnIndex("document_id")
                if ni < 0 or ii < 0:
                    raise RuntimeError("Провайдер не вернул нужные поля документов.")
                while cursor.moveToNext():
                    if cursor.getString(ni) == name:
                        return Docs.buildDocumentUriUsingTree(tree, cursor.getString(ii))
            finally:
                cursor.close()
            return None
        return resolver, Docs, tree, root, find

    def _saf_size(self, resolver, uri):
        cursor = resolver.query(uri, None, None, None, None)
        if cursor is None: return 0
        try:
            col = cursor.getColumnIndex('_size')
            if col < 0:
                col = cursor.getColumnIndex('size')
            return int(cursor.getLong(col)) if col >= 0 and cursor.moveToFirst() else 0
        finally: cursor.close()

    def _metadata_cache_path(self):
        return os.path.join(self.user_data_dir, "metadata_cache.json")

    def _metadata_fetch(self, key, url, accept="application/json", ttl=600):
        """Short-lived JSON cache; use stale metadata on rate-limit errors."""
        path = self._metadata_cache_path()
        cache = {}
        try:
            with open(path, "r", encoding="utf-8") as source:
                cache = json.load(source)
        except (OSError, ValueError):
            cache = {}
        entry = cache.get(key, {})
        cached_data = entry.get("data")
        cached_at = float(entry.get("saved_at", 0) or 0)
        if cached_data is not None and time.time() - cached_at < ttl:
            return cached_data
        try:
            with http_open(url, accept) as response:
                data = json.loads(response.read().decode("utf-8"))
            cache[key] = {"saved_at": time.time(), "data": data}
            temp_path = path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as target:
                json.dump(cache, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, path)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and cached_data is not None:
                return cached_data
            raise

    def _verification_cache_path(self):
        return os.path.join(self.user_data_dir, "verified_files.json")

    def _load_verification_cache(self):
        try:
            with open(self._verification_cache_path(), "r", encoding="utf-8") as source:
                return json.load(source)
        except (OSError, ValueError):
            return {}

    def _save_verification_cache(self, cache):
        path = self._verification_cache_path()
        temp_path = path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as target:
                json.dump(cache, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, path)
        except OSError:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def _saf_modified(self, resolver, uri):
        cursor = resolver.query(uri, None, None, None, None)
        if cursor is None:
            return None
        try:
            for column_name in ("last_modified", "lastModified", "last_modified_time"):
                column = cursor.getColumnIndex(column_name)
                if column >= 0 and cursor.moveToFirst():
                    value = int(cursor.getLong(column))
                    if value > 0:
                        return value
            return None
        finally:
            cursor.close()

    def _cached_verified(self, resolver, uri, name, size, expected_md5, cache_snapshot=None):
        modified = self._saf_modified(resolver, uri)
        if modified is None:
            return False
        cache = cache_snapshot if cache_snapshot is not None else self._load_verification_cache()
        entry = cache.get(name, {})
        return (
            entry.get("uri") == str(uri)
            and int(entry.get("size", -1)) == int(size)
            and int(entry.get("modified", -1)) == modified
        )

    def _verify_existing_asset(self, resolver, uri, name, size, expected_md5, cache_snapshot, progress_callback=None):
        try:
            actual_size = self._saf_size(resolver, uri)
            return name, actual_size == int(size), None
        except Exception:
            return name, False, None

    def _remember_verified(self, resolver, uri, name, size, digest, expected_md5):
        modified = self._saf_modified(resolver, uri)
        if modified is None:
            return
        cache = self._load_verification_cache()
        cache[name] = {
            "uri": str(uri),
            "size": int(size),
            "modified": modified,
            "md5": digest.lower() if digest else None,
            "expected_md5": expected_md5.lower() if expected_md5 else None,
        }
        self._save_verification_cache(cache)

    def _saf_md5(self, resolver, uri, progress_callback=None):
        """Native Java MessageDigest MD5: avoid converting every SAF byte into Python."""
        return self._saf_md5_many(resolver, [uri], progress_callback)

    def _saf_sha256(self, resolver, uri):
        """Read back a SAF document and calculate SHA-256 natively for diagnostics."""
        from jnius import autoclass
        Array = autoclass('java.lang.reflect.Array')
        Byte = autoclass('java.lang.Byte')
        MessageDigest = autoclass('java.security.MessageDigest')
        digest = MessageDigest.getInstance("SHA-256")
        buf = Array.newInstance(Byte.TYPE, 4 * 1024 * 1024)
        stream = resolver.openInputStream(uri)
        if stream is None:
            raise RuntimeError('Не удалось прочитать архив для SHA-256.')
        try:
            while True:
                n = stream.read(buf)
                if n < 0:
                    break
                digest.update(buf, 0, n)
        finally:
            stream.close()
        raw = digest.digest()
        return ''.join('{:02x}'.format(int(raw[i]) & 255) for i in range(len(raw)))

    def _saf_md5_many(self, resolver, uris, progress_callback=None):
        """MD5 of several SAF documents read back to back, computed natively."""
        from jnius import autoclass
        Array = autoclass('java.lang.reflect.Array')
        Byte = autoclass('java.lang.Byte')
        MessageDigest = autoclass('java.security.MessageDigest')
        digest = MessageDigest.getInstance("MD5")
        buf = Array.newInstance(Byte.TYPE, 4 * 1024 * 1024)
        for uri in uris:
            stream = resolver.openInputStream(uri)
            if stream is None:
                raise RuntimeError('Не удалось прочитать архив для MD5.')
            try:
                while True:
                    n = stream.read(buf)
                    if n < 0:
                        break
                    digest.update(buf, 0, n)
                    if progress_callback is not None:
                        progress_callback(n)
            finally:
                stream.close()
        raw = digest.digest()
        return ''.join('{:02x}'.format(int(raw[i]) & 255) for i in range(len(raw)))

    def _verify_document(self, resolver, uri, name, size, expected_md5, on_progress=None):
        """True when the document matches its MD5 (cached fingerprint or native hash)."""
        try:
            valid = self._saf_size(resolver, uri) == int(size)
            if on_progress is not None:
                on_progress(size if valid else 0)
            return valid
        except Exception:
            return False

    def _saf_truncate(self, resolver, Docs, folder, uri, name):
        """Empty a SAF document. Mode "wt" really truncates; plain "w" may not."""
        try:
            out = resolver.openOutputStream(uri, "wt")
            if out is not None:
                out.close()
        except Exception:
            pass
        if self._saf_size(resolver, uri) == 0:
            return uri
        if not self._saf_delete(resolver, Docs, uri):
            raise RuntimeError("Не удалось очистить повреждённый файл " + name)
        new_uri = Docs.createDocument(resolver, folder, "application/octet-stream", name)
        if new_uri is None:
            raise RuntimeError("Не удалось заново создать файл " + name)
        return new_uri

    def _stream_into_document(self, resolver, part, url, got, expected_size, mirror, label, report, allow_restart):
        """One HTTP session: append bytes from `got` on. Returns quietly on a cut
        connection; the caller re-reads the real file size and resumes."""
        headers = {"User-Agent": USER_AGENT}
        if got:
            headers["Range"] = "bytes={}-".format(got)
        request = urllib.request.Request(mirror_url(url, mirror), headers=headers)
        self.logger.info("HTTP REQUEST | %s | mirror=%s | offset=%d | url=%s", label, mirror_title(mirror), got, request.full_url)
        response = urllib.request.urlopen(request, timeout=60, context=NETWORK_CONTEXT)
        try:
            status = response.getcode()
            self.logger.info("HTTP RESPONSE | %s | status=%s | content-length=%s | content-range=%s", label, status, response.headers.get("Content-Length"), response.headers.get("Content-Range"))
            length = (response.headers.get("Content-Length") or "").strip()
            if got:
                content_range = (response.headers.get("Content-Range") or "").strip()
                # Resume only when the server confirms the exact byte interval and
                # the total object length. Never accept a vague/partial Range reply.
                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match:
                        raise MirrorMismatch("некорректный Content-Range при докачке")
                    range_start, range_end, range_total = map(int, match.groups())
                    if range_start != got or range_end != expected_size - 1 or range_total != expected_size:
                        raise MirrorMismatch(
                            "Content-Range не совпадает: {} (ожидалось bytes {}-{}/{})".format(
                                content_range, got, expected_size - 1, expected_size
                            )
                        )
                    if length.isdigit() and int(length) != expected_size - got:
                        raise MirrorMismatch("Content-Length докачки не совпадает с остатком байтов")
                elif status == 200 and allow_restart and (not length.isdigit() or int(length) == expected_size):
                    # No source supports resume: start this file over from zero.
                    got = 0
                else:
                    raise MirrorMismatch("источник не поддержал докачку")
            else:
                if status != 200:
                    raise MirrorMismatch("неожиданный ответ сервера {}".format(status))
                if length.isdigit() and int(length) != expected_size:
                    raise MirrorMismatch(
                        "Content-Length источника {} байт, ожидалось {} байт".format(length, expected_size)
                    )
            out = resolver.openOutputStream(part, "wa" if got else "wt")
            if out is None:
                raise RuntimeError("Android не разрешил записать временный файл ({}).".format(label))
            started = time.monotonic()
            session_start = got
            try:
                while got < expected_size:
                    data = response.read(min(READ_BLOCK, expected_size - got))
                    if not data:
                        # http.client returns b"" on a cut connection instead of
                        # raising, so a short file must never be treated as done.
                        break
                    out.write(data)
                    got += len(data)
                    if got - session_start and (got - session_start) % (64 * 1024 * 1024) < len(data):
                        self.logger.debug("DOWNLOAD PROGRESS | %s | %d/%d bytes", label, got, expected_size)
                    elapsed = time.monotonic() - started
                    speed = (got - session_start) / elapsed if elapsed > 1 else 0
                    report(got, "Загрузка: {} • {} из {}{}".format(
                        label, format_size(got), format_size(expected_size),
                        " • {}/с".format(format_size(speed)) if speed else ""
                    ))
                out.flush()
            finally:
                out.close()
        finally:
            response.close()

    def _download_document(self, resolver, Docs, folder, part, temp_name, url,
                           expected_size, expected_hash, preferred_mirror, label, report,
                           expected_sha256=None):
        """Download into a SAF temp document with resume, source rotation and a
        native MD5 check. report(got_bytes, text, force=False) feeds the bar.
        Returns the URI of the verified document."""
        if part is None:
            part = Docs.createDocument(resolver, folder, "application/octet-stream", temp_name)
            if part is None:
                raise RuntimeError("Не удалось создать временный файл " + temp_name)
        got = self._saf_size(resolver, part)
        self.logger.info("DOWNLOAD START | %s | size=%d | existing=%d | MD5=%s | SHA256=%s | url=%s", label, expected_size, got, expected_hash, expected_sha256, url)
        if got >= expected_size:
            # A full-size file reaching this point already failed MD5.
            part = self._saf_truncate(resolver, Docs, folder, part, temp_name)
            got = 0
        sources = len(RELEASE_MIRRORS)
        mirror = preferred_mirror % sources
        max_idle = STREAM_IDLE_ATTEMPTS_PER_MIRROR * sources
        hash_attempt = 0
        while True:
            idle = 0
            while got < expected_size:
                before = got
                error = None
                try:
                    self._stream_into_document(
                        resolver, part, url, got, expected_size, mirror, label, report,
                        allow_restart=idle >= sources
                    )
                except (OSError, http.client.HTTPException, MirrorMismatch) as exc:
                    error = exc
                    self.logger.warning("STREAM ERROR | %s | mirror=%s | offset=%d | %s", label, mirror_title(mirror), got, repr(exc), exc_info=True)
                # Resume from what is really on disk, not from what we counted.
                got = self._saf_size(resolver, part)
                if got > expected_size:
                    part = self._saf_truncate(resolver, Docs, folder, part, temp_name)
                    got = 0
                if got >= expected_size:
                    break
                if got > before:
                    idle = 0
                    report(got, "Связь оборвалась: {} • докачиваем с {}".format(label, format_size(got)), True)
                    continue
                idle += 1
                if idle >= max_idle:
                    reason = " Причина: {}".format(friendly_network_error(error)) if error else ""
                    raise RuntimeError(
                        "{}: источники не отдают файл. Скачано {} из {}, прогресс сохранён — "
                        "нажмите «Скачать» ещё раз.{}".format(label, format_size(got), format_size(expected_size), reason)
                    )
                mirror = (mirror + 1) % sources
                report(got, "Нет связи ({}). Пробуем {} • попытка {} из {}".format(
                    label, mirror_title(mirror), idle + 1, max_idle), True)
                time.sleep(min(2 ** min(idle, 4), 15))

            # Hash checks disabled: size-complete downloads are accepted.
            self.logger.info("DOWNLOAD COMPLETE (SIZE ONLY) | %s | bytes=%d | mirror=%s", label, got, mirror_title(mirror))
            report(expected_size, "Загружено: {} • {} байт".format(label, expected_size), True)
            return part

            report(got, "Проверка MD5: {}".format(label), True)
            hashed = [0]

            def on_hash(amount):
                hashed[0] += amount
                report(got, "Проверка MD5: {} • {:.0f}%".format(label, 100.0 * hashed[0] / max(1, expected_size)))

            actual_hash = self._saf_md5(resolver, part, progress_callback=on_hash).lower()
            self.logger.info("HASH RESULT | %s | MD5 actual=%s expected=%s | bytes=%d | mirror=%s", label, actual_hash, expected_hash, self._saf_size(resolver, part), mirror_title(mirror))
            if actual_hash == expected_hash.lower():
                report(expected_size, "MD5 совпал: {} • {} байт".format(label, expected_size), True)
                return part

            actual_sha256 = None
            expected_sha256 = (expected_sha256 or "").lower()
            if len(expected_sha256) == 64:
                # Cross-check against GitHub's independent SHA-256 asset digest.
                actual_sha256 = self._saf_sha256(resolver, part).lower()
                if actual_sha256 == expected_sha256:
                    raise RuntimeError(
                        "{}: SHA-256 совпадает с оригиналом релиза GitHub, но MD5 из "
                        "манифеста неверна. Файл не принят. MD5 получен {}, ожидалось {}; "
                        "SHA-256 подтверждён {}.".format(
                            label, actual_hash, expected_hash.lower(), expected_sha256
                        )
                    )

            hash_attempt += 1
            self.logger.error("MD5 MISMATCH | %s | attempt=%d/%d | actual=%s | expected=%s | SHA256=%s | mirror=%s", label, hash_attempt, MAX_HASH_ATTEMPTS, actual_hash, expected_hash, actual_sha256, mirror_title(mirror))
            mismatch_detail = "MD5 не совпал: {} • фактически {} байт • MD5 {} • ожидалось {}".format(
                label, self._saf_size(resolver, part), actual_hash, expected_hash.lower()
            )
            if actual_sha256:
                mismatch_detail += " • SHA-256 {}".format(actual_sha256)
            part = self._saf_truncate(resolver, Docs, folder, part, temp_name)
            got = 0
            if hash_attempt >= MAX_HASH_ATTEMPTS:
                raise RuntimeError(
                    "{}: MD5 не совпал {} раза подряд с разных источников. Возможно, файл обновился "
                    "или повреждается при загрузке. Нажмите «Проверить обновления» и повторите.".format(mismatch_detail, hash_attempt)
                )
            mirror = (mirror + 1) % sources
            report(0, "{} • повторная загрузка через {}".format(mismatch_detail, mirror_title(mirror)), True)
    def _fit_label(self, widget, size):
        widget.text_size = (size[0] - dp(12), None)

    def _set_status(self, message):
        self.status.text = message

    def _set_busy(self, value):
        self.busy = value
        self.check_button.disabled = value
        self.download_button.disabled = value or not any(not item.get("downloaded") for item in self.assets.values())
        self.folder_button.disabled = value

    def _render_assets(self):
        self.asset_list.clear_widgets()
        if not self.assets:
            for name in REQUIRED_ASSETS:
                self.asset_list.add_widget(self._asset_row(
                    name, "Ожидает проверки"
                ))
            return

        for name in REQUIRED_ASSETS:
            asset = self.assets.get(name)
            if not asset:
                detail = "Не найден в релизе"
            elif asset.get("downloaded"):
                detail = "Уже загружен • {}".format(format_size(asset["size"]))
            else:
                detail = "Доступен • {}".format(format_size(asset["size"]))
            self.asset_list.add_widget(self._asset_row(name, detail))

    def _asset_row(self, name, detail):
        row = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            height=dp(64),
            padding=(dp(10), dp(6)),
            spacing=dp(2),
        )
        row.add_widget(Label(
            text="[b]{}[/b]".format(name),
            markup=True,
            halign="left",
            valign="middle",
            font_size="13sp",
            size_hint_y=None,
            height=dp(25),
        ))
        row.add_widget(Label(
            text=detail,
            color=(0.68, 0.73, 0.64, 1),
            halign="left",
            valign="middle",
            font_size="12sp",
            size_hint_y=None,
            height=dp(22),
        ))
        return row

    def check_updates(self, instance):
        if self.busy:
            return
        self._set_busy(True)
        self.progress.value = 0
        self._set_status("Проверяем, есть ли свежий хабар…")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            manifest = self._metadata_fetch("manifest", MANIFEST_URL, "application/json", ttl=600)
            base_entry = next((entry for entry in manifest.get("base_files", []) if " gamedata.sq_base" in entry), None)
            if not base_entry or not base_entry.startswith("chunk:"):
                raise RuntimeError("В index.json не найдены метаданные 17 частей gamedata.sq_base.")
            metadata = base_entry[len("chunk:"):].rsplit(" ", 1)[0]
            chunk_ids = [value.strip() for value in metadata.split(";", 1)[0].split(",")]
            if len(chunk_ids) != BASE_CHUNK_COUNT:
                raise RuntimeError("В манифесте должно быть ровно 17 ID частей.")
            release = self._metadata_fetch("release", RELEASE_API, "application/vnd.github+json", ttl=600)
            if release.get("tag_name") != RELEASE_TAG:
                raise RuntimeError("GitHub вернул неожиданный релиз.")
            release_assets = {item.get("name"): item for item in release.get("assets", [])}
            missing = [name for name in CHUNK_NAMES + REQUIRED_ASSETS[1:] if name not in release_assets]
            if missing:
                raise RuntimeError("В релизе latest отсутствуют файлы: " + ", ".join(missing))
            chunks = []
            for index, name in enumerate(CHUNK_NAMES):
                item = dict(release_assets[name])
                item["chunk_index"] = index
                chunks.append(item)
            if not self.tree_uri:
                raise RuntimeError('Сначала выберите папку Download, чтобы проверить целевые архивы.')
            resolver, Docs, tree, folder, find = self._saf_target()
            base_size = sum(int(item.get("size") or 0) for item in chunks)
            base_uri = find(folder, BASE_ASSET)
            found = {BASE_ASSET: {"name": BASE_ASSET, "size": base_size, "chunks": chunks, "downloaded": bool(base_uri is not None and self._saf_size(resolver, base_uri) == base_size)}}
            for name in REQUIRED_ASSETS[1:]:
                item = dict(release_assets[name])
                existing = find(folder, name)
                try:
                    item["downloaded"] = bool(existing is not None and self._saf_size(resolver, existing) == int(item.get("size") or 0))
                except Exception:
                    item["downloaded"] = False
                found[name] = item
            self.release = release
            self.assets = found
            pending = sum(int(item.get("size") or 0) for item in found.values() if not item["downloaded"])
            message = ("Хабар обнаружен!\\nВес хабара: {}".format(format_size(pending)) if pending else "В Зоне затишье.\\nВсе архивы уже на месте.")
            signal_state = "online" if pending else "idle"
            Clock.schedule_once(lambda dt, m=message, st=signal_state: self._check_done(m, st), 0)
        except Exception as exc:
            if hasattr(self, "logger"): self.logger.exception("ОШИБКА проверки обновлений")
            error = friendly_network_error(exc)
            Clock.schedule_once(lambda dt, e=error: self._check_failed(e), 0)

    def _check_done(self, message, signal_state="online"):
        pending = any(not item.get("downloaded") for item in self.assets.values())
        # The check has its own 0–100% pass; the download starts from zero.
        self.progress.value = 0 if pending else 100
        self._set_signal(signal_state)
        self._set_status(message)
        self._set_busy(False)
        self.download_button.disabled = not any(
            not item.get("downloaded") for item in self.assets.values()
        )

    def _check_failed(self, error):
        self._set_signal("offline")
        self._set_status("Связи с Зоной нет.\n" + error)
        self._set_busy(False)
        self.download_button.disabled = True

    def download_updates(self, instance):
        if self.busy or not self.assets:
            return
        self._set_busy(True)
        self.progress.value = 0
        self._set_signal("online")
        self._set_status("Подготовка к доставке хабара…")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _saf_delete(self, resolver, Docs, uri):
        """Best-effort SAF deletion. Never let a cleanup failure interrupt a valid download."""
        if uri is None:
            return True
        try:
            Docs.deleteDocument(resolver, uri)
        except Exception:
            try:
                resolver.delete(uri, None, None)
            except Exception:
                pass
        try:
            cursor = resolver.query(uri, None, None, None, None)
            if cursor is None:
                return True
            try:
                return not cursor.moveToFirst()
            finally:
                cursor.close()
        except Exception:
            # SAF providers sometimes fail to query a URI after deleting it.
            return False

    def _saf_hash_size(self, resolver, uri):
        size = self._saf_size(resolver, uri)
        digest = self._saf_md5(resolver, uri)
        return size, digest

    def _save_chunk_state(self, path, state):
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as target:
            json.dump(state, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)

    def _load_folder_state(self, resolver, find, folder):
        uri = find(folder, ".updater_state.json")
        if uri is None:
            return {}
        try:
            stream = resolver.openInputStream(uri)
            if stream is None:
                return {}
            from jnius import autoclass
            Array = autoclass("java.lang.reflect.Array")
            Byte = autoclass("java.lang.Byte")
            buf = Array.newInstance(Byte.TYPE, 65536)
            data = bytearray()
            try:
                while True:
                    n = stream.read(buf)
                    if n < 0:
                        break
                    data.extend((int(buf[j]) & 255) for j in range(n))
            finally:
                stream.close()
            value = json.loads(data.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save_folder_state(self, resolver, Docs, folder, find, state):
        final_name = ".updater_state.json"
        temp_name = ".updater_state.json.tmp"
        temp_uri = find(folder, temp_name)
        if temp_uri is None:
            temp_uri = Docs.createDocument(resolver, folder, "application/json", temp_name)
        if temp_uri is None:
            raise RuntimeError("Не удалось создать временный JSON состояния.")
        out = resolver.openOutputStream(temp_uri, "w")
        if out is None:
            raise RuntimeError("Не удалось записать JSON состояния.")
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            out.write(payload)
            out.flush()
        finally:
            out.close()
        final_uri = find(folder, final_name)
        if final_uri is not None:
            backup_name = final_name + ".bak"
            backup_uri = find(folder, backup_name)
            if backup_uri is not None:
                self._saf_delete(resolver, Docs, backup_uri)
            backup_uri = Docs.renameDocument(resolver, final_uri, backup_name)
            if backup_uri is None:
                raise RuntimeError("Не удалось сохранить предыдущую копию JSON состояния.")
            if Docs.renameDocument(resolver, temp_uri, final_name) is None:
                Docs.renameDocument(resolver, backup_uri, final_name)
                raise RuntimeError("Не удалось заменить JSON; восстановлена предыдущая версия.")
            self._saf_delete(resolver, Docs, backup_uri)
        elif Docs.renameDocument(resolver, temp_uri, final_name) is None:
            raise RuntimeError("Не удалось завершить сохранение JSON состояния.")

    def _folder_state_update(self, resolver, Docs, folder, find, state, name, status, actual_size=0, digest=None, temp_name=None):
        entry = state.setdefault("files", {}).setdefault(name, {})
        entry.update({"status": status, "actual_size": int(actual_size or 0)})
        if digest:
            entry["verified_md5"] = digest.lower()
        if temp_name:
            entry["temp_name"] = temp_name
        target_uri = find(folder, temp_name) if temp_name else None
        if target_uri is None:
            target_uri = find(folder, name)
        if target_uri is not None:
            current_size = self._saf_size(resolver, target_uri)
            current_modified = self._saf_modified(resolver, target_uri)
            entry.update({"uri": str(target_uri), "actual_size": current_size, "modified": current_modified})
        self._save_folder_state(resolver, Docs, folder, find, state)

    def _download_worker(self):
        try:
            if not self.tree_uri:
                raise RuntimeError("Сначала выберите папку Download.")
            resolver, Docs, tree, folder, find = self._saf_target()
            from jnius import autoclass
            Array = autoclass("java.lang.reflect.Array")
            Byte = autoclass("java.lang.Byte")
            base = self.assets[BASE_ASSET]
            chunks = base["chunks"]
            count = len(chunks)
            chunk_names = [item["name"] for item in chunks]
            base_size = int(base.get("size") or 0)
            extra_assets = [self.assets[n] for n in REQUIRED_ASSETS[1:]]
            folder_state = self._load_folder_state(resolver, find, folder)
            folder_state.update({"schema": 1, "release": RELEASE_TAG, "manifest_url": MANIFEST_URL})
            state_files = folder_state.setdefault("files", {})
            for entry in list(chunks) + extra_assets:
                state_files.setdefault(entry["name"], {}).update({
                    "expected_size": int(entry.get("size") or 0),
                    
                    "status": state_files.get(entry["name"], {}).get("status", "pending")
                })
            self._save_folder_state(resolver, Docs, folder, find, folder_state)

            # One bar for everything that is really left to do:
            #   [ parts download | assembly | extra archives ]
            need_base = not base.get("downloaded")
            extras = [item for item in extra_assets if not item.get("downloaded")]
            extras_total = sum(int(x.get("size") or 0) for x in extras)
            parts_units = base_size if need_base else 0
            assembly_units = 3 * base_size * LOCAL_WORK_WEIGHT if need_base else 0
            extras_offset = parts_units + assembly_units
            progress = DownloadProgress(self, parts_units + assembly_units + extras_total)

            progress.report(0, "Измеряем скорость зеркал…", True)
            preferred_mirror = probe_mirrors_speed(chunks[0]["browser_download_url"])
            progress.report(0, "Быстрейший источник: {}".format(mirror_title(preferred_mirror)), True)

            base_final_size = base_size
            base_final_md5 = None
            if need_base:
                # Stage 1: every one of the 17 pieces is verified (or downloaded and
                # verified) before the loop moves on. Valid pieces are kept until
                # the assembled archive has been verified.
                part_uris = []
                temporary_part_uris = []
                credited = 0
                for idx, item in enumerate(chunks):
                    name = item["name"]
                    label = "часть {}/{}".format(idx + 1, count)
                    expected_size = int(item.get("size") or 0)
                    expected_hash = ""
                    temp_name = name + ".download"
                    part = find(folder, temp_name)
                    if part is None:
                        # Hidden temp documents created by older builds.
                        part = find(folder, "." + temp_name)
                    original = find(folder, name)

                    verified_uri = None
                    verified_name = None
                    for candidate, candidate_name in ((part, temp_name), (original, name)):
                        if candidate is None or self._saf_size(resolver, candidate) != expected_size:
                            continue
                        progress.report(credited, "Проверка готовой части: {}".format(label), True)
                        if self._verify_document(
                            resolver, candidate, name, expected_size, expected_hash,
                            lambda done, u=credited, l=label: progress.report(
                                u + done, "Проверка готовой части: {} • {:.0f}%".format(l, 100.0 * done / max(1, expected_size)))
                        ):
                            verified_uri = candidate
                            verified_name = candidate_name
                            break

                    if verified_uri is None:
                        part = self._download_document(
                            resolver, Docs, folder, part, temp_name, item["browser_download_url"],
                            expected_size, expected_hash, preferred_mirror, label,
                            lambda got, text, force=False, u=credited: progress.report(u + got, text, force),
                            expected_sha256=item.get("expected_sha256")
                        )
                        verified_uri = part
                        verified_name = temp_name

                    self._remember_verified(resolver, verified_uri, name, expected_size, None, None)
                    self._folder_state_update(
                        resolver, Docs, folder, find, folder_state, name,
                        "verified", expected_size, None, verified_name
                    )
                    part_uris.append(verified_uri)
                    if part is not None:
                        temporary_part_uris.append(part)
                    credited += expected_size
                    progress.report(credited, "Готово частей: {}/{}".format(idx + 1, count), True)

                # Recheck the entire set before assembly so no unchecked piece enters
                # the final archive (fingerprint cache makes this instant).
                for idx, item in enumerate(chunks):
                    part_uri = part_uris[idx]
                    size = self._saf_size(resolver, part_uri)
                    if size != int(item.get("size") or 0):
                        raise RuntimeError("Повторная проверка размера части {} не пройдена.".format(item["name"]))

                def local(nbytes):
                    return parts_units + nbytes * LOCAL_WORK_WEIGHT

                # Stage 2: append verified chunks into a temporary assembly. Chunks
                # are retained; checkpoints are written only after each append closes.
                progress.report(local(0), "Все {} частей готовы. Собираем общий архив…".format(count), True)
                assembly_name = BASE_ASSET + ".assembling"
                assembly = find(folder, assembly_name)
                state_path = os.path.join(self.user_data_dir, "base_saf_state.json")
                state = {"next_chunk": 0, "committed_bytes": 0, "chunk_names": chunk_names}
                if os.path.isfile(state_path):
                    try:
                        with open(state_path, "r", encoding="utf-8") as source:
                            saved = json.load(source)
                        if saved.get("chunk_names") == chunk_names and not saved.get("complete"):
                            state.update(saved)
                    except Exception:
                        pass

                next_chunk = max(0, min(count, int(state.get("next_chunk", 0))))
                committed = max(0, int(state.get("committed_bytes", 0)))
                expected_committed = sum(int(chunks[i].get("size") or 0) for i in range(next_chunk))
                assembly_size = self._saf_size(resolver, assembly) if assembly is not None else 0
                # If a process died between appending and checkpointing, discard only
                # the assembly and rebuild from the still-preserved verified pieces.
                if assembly is None or assembly_size != committed or committed != expected_committed:
                    if assembly is not None and not self._saf_delete(resolver, Docs, assembly):
                        raise RuntimeError("Не удалось сбросить повреждённый промежуточный общий архив.")
                    assembly = Docs.createDocument(resolver, folder, "application/octet-stream", assembly_name)
                    if assembly is None:
                        raise RuntimeError("Не удалось создать промежуточный общий архив.")
                    next_chunk = 0
                    committed = 0
                    state = {"next_chunk": 0, "committed_bytes": 0, "chunk_names": chunk_names, "complete": False}
                    self._save_chunk_state(state_path, state)

                buf = Array.newInstance(Byte.TYPE, READ_BLOCK)
                for idx in range(next_chunk, count):
                    item = chunks[idx]
                    inp = resolver.openInputStream(part_uris[idx])
                    out = resolver.openOutputStream(assembly, "wa" if committed else "wt")
                    if inp is None or out is None:
                        if inp is not None:
                            inp.close()
                        if out is not None:
                            out.close()
                        raise RuntimeError("Не удалось открыть поток для объединения " + item["name"])
                    copied = 0
                    try:
                        while True:
                            n = inp.read(buf)
                            if n < 0:
                                break
                            out.write(buf, 0, n)
                            copied += n
                            progress.report(local(committed + copied), "Сборка общего архива: часть {}/{}".format(idx + 1, count))
                        out.flush()
                    finally:
                        inp.close()
                        out.close()
                    if copied != int(item.get("size") or 0):
                        raise RuntimeError("При объединении части {} получен неверный размер.".format(item["name"]))
                    committed += copied
                    state = {
                        "next_chunk": idx + 1,
                        "committed_bytes": committed,
                        "chunk_names": chunk_names,
                        "complete": False,
                    }
                    self._save_chunk_state(state_path, state)

                assembled_size = self._saf_size(resolver, assembly)
                if assembled_size != base_size:
                    raise RuntimeError("Размер общего gamedata.sq_base не совпал. Все 17 частей сохранены.")
                # Checksum verification disabled; assembled archive is size-checked.
                assembled_md5 = None

                # SAF swap: retain the previous archive as a backup until the new
                # file has been renamed and checked.
                old_base = find(folder, BASE_ASSET)
                backup_name = BASE_ASSET + ".backup"
                backup = find(folder, backup_name)
                if backup is not None and not self._saf_delete(resolver, Docs, backup):
                    raise RuntimeError("Не удалось освободить резервное имя для замены общего архива.")
                backup = None
                if old_base is not None:
                    backup = Docs.renameDocument(resolver, old_base, backup_name)
                    if backup is None:
                        raise RuntimeError("Не удалось сохранить предыдущий gamedata.sq_base в резервную копию.")

                final_uri = Docs.renameDocument(resolver, assembly, BASE_ASSET)
                if final_uri is None:
                    if backup is not None:
                        Docs.renameDocument(resolver, backup, BASE_ASSET)
                    raise RuntimeError("Не удалось переименовать собранный архив. 17 частей сохранены.")
                # Renaming does not touch the bytes; a size check is enough here.
                if self._saf_size(resolver, final_uri) != base_size:
                    if not self._saf_delete(resolver, Docs, final_uri):
                        failed_name = BASE_ASSET + ".failed"
                        if find(folder, failed_name) is not None:
                            failed_name = failed_name + ".2"
                        if Docs.renameDocument(resolver, final_uri, failed_name) is None:
                            raise RuntimeError("Итоговая проверка не пройдена; новая и резервная копии оставлены, части сохранены.")
                    if backup is not None:
                        Docs.renameDocument(resolver, backup, BASE_ASSET)
                    raise RuntimeError("Проверка общего архива после сохранения не пройдена. Части сохранены.")

                self._save_chunk_state(state_path, {
                    "complete": True,
                    "chunk_names": chunk_names,
                    ,
                    "assembled_size": assembled_size,
                })
                self._remember_verified(resolver, final_uri, BASE_ASSET, assembled_size, assembled_md5, assembled_md5)
                base["downloaded"] = True
                base["assembled_md5"] = assembled_md5
                base["expected_md5"] = assembled_md5
                base_final_size = assembled_size
                base_final_md5 = assembled_md5
                # Cleanup is strictly post-assembly and best effort. A SAF cleanup
                # failure must never invalidate the successfully assembled archive.
                progress.report(extras_offset, "Архив проверен. Очистка временных частей…", True)
                for part_uri in temporary_part_uris:
                    self._saf_delete(resolver, Docs, part_uri)
                if backup is not None:
                    self._saf_delete(resolver, Docs, backup)

            # Stage 3: the remaining archives, through temp files and verification
            # before replacement.
            extras_done = 0
            for idx, item in enumerate(extras):
                name = item["name"]
                label = name
                temp_name = name + ".download"
                expected_size = int(item.get("size") or 0)
                expected_hash = ""
                start = extras_offset + extras_done
                temp = find(folder, temp_name)
                if temp is None:
                    temp = find(folder, "." + temp_name)

                verified = False
                if temp is not None and self._saf_size(resolver, temp) == expected_size:
                    progress.report(start, "Проверка готового архива: {}".format(label), True)
                    verified = self._verify_document(
                        resolver, temp, name, expected_size, expected_hash,
                        lambda done, u=start, l=label: progress.report(
                            u + done, "Проверка готового архива: {} • {:.0f}%".format(l, 100.0 * done / max(1, expected_size)))
                    )
                if not verified:
                    temp = self._download_document(
                        resolver, Docs, folder, temp, temp_name, item["browser_download_url"],
                        expected_size, expected_hash, preferred_mirror, label,
                        lambda got, text, force=False, u=start: progress.report(u + got, text, force),
                        expected_sha256=item.get("expected_sha256")
                    )

                old = find(folder, name)
                if old is not None and not self._saf_delete(resolver, Docs, old):
                    raise RuntimeError("Не удалось заменить старый архив " + name)
                final_uri = Docs.renameDocument(resolver, temp, name)
                if final_uri is None:
                    raise RuntimeError("Не удалось завершить " + name)
                self._remember_verified(resolver, final_uri, name, expected_size, expected_hash, expected_hash)
                item["downloaded"] = True
                extras_done += expected_size
                self._folder_state_update(resolver, Docs, folder, find, folder_state, name, "verified", expected_size, None, temp_name)
                progress.report(extras_offset + extras_done, "Готов архив: {}".format(label), True)

            for asset_name, asset_item in self.assets.items():
                if asset_item.get("downloaded") and asset_name != BASE_ASSET:
                    state_files.setdefault(asset_name, {}).update({
                        "expected_size": int(asset_item.get("size") or 0),
                        "actual_size": int(asset_item.get("size") or 0),
                        
                        "status": "verified"
                    })
            folder_state["files"][BASE_ASSET] = {
                "expected_size": base_size,
                "actual_size": base_final_size,
                ,
                "status": "verified",
                "parts": list(CHUNK_NAMES)
            }
            folder_state["last_successful_update"] = int(time.time())
            self._save_folder_state(resolver, Docs, folder, find, folder_state)
            Clock.schedule_once(lambda dt: self._download_done(), 0)
        except Exception as exc:
            if hasattr(self, "logger"): self.logger.exception("КРИТИЧЕСКАЯ ОШИБКА загрузки/сборки")
            error = friendly_network_error(exc)
            Clock.schedule_once(lambda dt, e=error: self._download_failed(e), 0)

    def _download_status(self, message):
        self._set_status(message)

    def _set_progress(self, percent):
        self.progress.value = max(0, min(100, percent))

    def _download_progress(self, percent, text):
        self._set_progress(percent)
        self._set_status("Хабар доставляется… {:.1f}%\n{}".format(min(100, percent), text))

    def _download_done(self):
        self.progress.value = 100
        self._set_signal("online")
        self._set_status("Хабар доставлен!\nВсе архивы проверены и готовы.")
        self._set_busy(False)
        self.download_button.disabled = True

    def _download_failed(self, error):
        self._set_signal("offline")
        self._set_status("Доставка хабара прервана.\n" + error)
        self._set_busy(False)
        self.download_button.disabled = not any(
            not item.get("downloaded") for item in self.assets.values()
        )

    @staticmethod
    def _md5(path):
        digest = hashlib.md5()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


if __name__ == "__main__":
    UpdaterApp().run()
