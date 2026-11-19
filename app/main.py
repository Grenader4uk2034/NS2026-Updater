# -*- coding: utf-8 -*-
import hashlib
import json
import os
import shutil
import ssl
import threading
import urllib.error
import urllib.request

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
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return "{:.2f} {}".format(value, unit)
        value /= 1024
    return "{:.2f} ТБ".format(value)


def ssl_context():
    # certifi provides an up-to-date CA bundle inside the Android APK.
    return ssl.create_default_context(cafile=certifi.where())


NETWORK_CONTEXT = ssl_context()


def friendly_network_error(error):
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


class UpdaterApp(App):
    def build(self):
        self.title = "NS2026 Updater"
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

        self.progress = ProgressBar(max=100, value=0, size_hint_y=None, height=dp(8))
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
        expected = (asset.get("expected_md5") or asset.get("assembled_md5") or "").lower()
        if len(expected) != 32:
            return False
        try:
            expected_size = int(asset.get("size") or 0)
            if expected_size and os.path.getsize(path) != expected_size:
                return False
            return self._md5(path).lower() == expected
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

    def _saf_md5(self, resolver, uri):
        from jnius import autoclass
        Array = autoclass('java.lang.reflect.Array'); Byte = autoclass('java.lang.Byte')
        stream = resolver.openInputStream(uri)
        if stream is None: raise RuntimeError('Не удалось прочитать архив для MD5.')
        digest = hashlib.md5(); buf = Array.newInstance(Byte.TYPE, 65536)
        try:
            while True:
                n = stream.read(buf)
                if n < 0: break
                digest.update(bytes((int(buf[i]) & 255) for i in range(n)))
        finally: stream.close()
        return digest.hexdigest()
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
            with http_open(MANIFEST_URL, "application/json") as response:
                manifest = json.loads(response.read().decode("utf-8"))

            base_entry = next(
                (entry for entry in manifest.get("base_files", [])
                 if " gamedata.sq_base" in entry),
                None,
            )
            if not base_entry or not base_entry.startswith("chunk:"):
                raise RuntimeError("В index.json не найдены метаданные 17 частей gamedata.sq_base.")
            metadata = base_entry[len("chunk:"):].rsplit(" ", 1)[0]
            id_text, hash_text = metadata.split(";", 1)
            chunk_ids = [value.strip() for value in id_text.split(",")]
            chunk_hashes = [value.strip().lower() for value in hash_text.split(",")]
            if len(chunk_ids) != BASE_CHUNK_COUNT or len(chunk_hashes) != BASE_CHUNK_COUNT:
                raise RuntimeError("В манифесте должно быть ровно 17 ID и 17 MD5.")
            if any(len(value) != 32 for value in chunk_hashes):
                raise RuntimeError("Манифест содержит некорректную MD5 одной из частей.")

            expected_hashes = {}
            for entry in manifest.get("update_files", []):
                parts = entry.split(" ", 1)
                if len(parts) != 2:
                    continue
                hashes = parts[0].split(";")
                filename = parts[1].replace("\\\\", "/").split("/")[-1]
                if filename in REQUIRED_ASSETS[1:] and hashes:
                    digest = hashes[0].lower()
                    if len(digest) == 32 and len(hashes) > 1 and hashes[1].lower() == digest:
                        expected_hashes[filename] = digest
            missing_hashes = [name for name in REQUIRED_ASSETS[1:] if name not in expected_hashes]
            if missing_hashes:
                raise RuntimeError("В index.json отсутствуют корректные MD5: " + ", ".join(missing_hashes))

            with http_open(RELEASE_API, "application/vnd.github+json") as response:
                release = json.loads(response.read().decode("utf-8"))
            if release.get("tag_name") != RELEASE_TAG:
                raise RuntimeError("GitHub вернул неожиданный релиз.")

            release_assets = {item.get("name"): item for item in release.get("assets", [])}
            missing_chunks = [name for name in CHUNK_NAMES if name not in release_assets]
            missing_extra = [name for name in REQUIRED_ASSETS[1:] if name not in release_assets]
            if missing_chunks or missing_extra:
                missing = missing_chunks + missing_extra
                raise RuntimeError("В релизе latest отсутствуют файлы: " + ", ".join(missing))

            chunks = []
            for index, name in enumerate(CHUNK_NAMES):
                item = dict(release_assets[name])
                item["expected_md5"] = chunk_hashes[index]
                item["chunk_index"] = index
                chunks.append(item)

            if not self.tree_uri:
                raise RuntimeError('Сначала выберите папку Download, чтобы проверить целевые архивы.')
            resolver, Docs, tree, folder, find = self._saf_target()
            state_path = os.path.join(self.user_data_dir, 'base_saf_state.json')
            base_uri = find(folder, BASE_ASSET)
            assembled_md5 = None
            base_size = sum(int(item.get('size') or 0) for item in chunks)
            if base_uri is not None and os.path.isfile(state_path):
                try:
                    with open(state_path,'r',encoding='utf-8') as sf: saved=json.load(sf)
                    if saved.get('complete') and saved.get('chunks_md5') == chunk_hashes and self._saf_size(resolver,base_uri) == base_size:
                        actual=self._saf_md5(resolver,base_uri)
                        if actual == saved.get('assembled_md5'): assembled_md5=actual
                except Exception: assembled_md5=None
            base_asset={'name':BASE_ASSET,'size':base_size,'chunks':chunks,'expected_md5':assembled_md5,'assembled_md5':assembled_md5,'downloaded':bool(assembled_md5)}
            found={BASE_ASSET:base_asset}
            for name in REQUIRED_ASSETS[1:]:
                item=dict(release_assets[name]); item['expected_md5']=expected_hashes[name]
                existing=find(folder,name); item['downloaded']=False
                if existing is not None:
                    try:
                        item['downloaded']=(self._saf_size(resolver,existing)==int(item.get('size') or 0) and self._saf_md5(resolver,existing)==item['expected_md5'])
                    except Exception: item['downloaded']=False
                found[name]=item
            self.release = release
            self.assets = found
            total = sum(item["size"] for item in found.values())
            pending = sum(item["size"] for item in found.values() if not item["downloaded"])
            if pending > 0:
                message = "Хабар обнаружен!\nВес хабара: {}".format(format_size(pending))
                signal_state = "online"
            else:
                message = "В Зоне затишье.\\nВсе архивы уже на месте."
                signal_state = "idle"
            Clock.schedule_once(lambda dt, m=message, st=signal_state: self._check_done(m, st), 0)
        except Exception as exc:
            error = friendly_network_error(exc)
            Clock.schedule_once(lambda dt, e=error: self._check_failed(e), 0)

    def _check_done(self, message, signal_state="online"):
        self._set_signal(signal_state)
        self._set_status(message)
        self._set_busy(False)
        self.download_button.disabled = not any(
            not item.get("downloaded") for item in self.assets.values()
        )

    def _check_failed(self, error):
        self._set_signal("offline")
        self._set_status("Связи с Зоной нет.\\n" + error)
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
            hashes = [item["expected_md5"].lower() for item in chunks]
            base_size = int(base.get("size") or 0)

            if not base.get("downloaded"):
                # Stage 1: download and validate all 17 pieces. Keep every valid piece
                # until the assembled archive has been verified successfully.
                downloaded_bytes = 0
                for idx, item in enumerate(chunks):
                    name = item["name"]
                    Clock.schedule_once(
                        lambda dt, i=idx + 1, n=name: self._download_status(
                            "Проверка части {}/17: {}".format(i, n)
                        ), 0
                    )
                    temp_name = "." + name + ".download"
                    part = find(folder, temp_name)
                    expected_size = int(item.get("size") or 0)
                    expected_hash = item["expected_md5"].lower()
                    offset = self._saf_size(resolver, part) if part is not None else 0
    
                    if offset == expected_size and expected_size > 0:
                        try:
                            part_valid = self._saf_md5(resolver, part).lower() == expected_hash
                        except Exception:
                            part_valid = False
                        if part_valid:
                            downloaded_bytes += expected_size
                            pct = 75.0 * downloaded_bytes / max(1, base_size)
                            Clock.schedule_once(lambda dt, p=pct, n=name, r=expected_size: self._download_progress(p, n, r), 0)
                            Clock.schedule_once(lambda dt, i=idx+1: self._set_status("Проверено частей: {}/17".format(i)), 0)
                            continue
                        if not self._saf_delete(resolver, Docs, part):
                            raise RuntimeError("Android не разрешил удалить повреждённую временную часть {}.".format(name))
                        part = None
                        offset = 0
    
                    if offset > expected_size:
                        if not self._saf_delete(resolver, Docs, part):
                            raise RuntimeError("Android не разрешил убрать повреждённую временную часть {}.".format(name))
                        part = None
                        offset = 0
    
                    digest = hashlib.md5()
                    if offset and part is not None:
                        Clock.schedule_once(
                            lambda dt, i=idx + 1, n=name, size=offset: self._download_status(
                                "Восстановление {} • проверка {} байт".format(n, size)
                            ), 0
                        )
                        inp = resolver.openInputStream(part)
                        if inp is None:
                            raise RuntimeError("Не удалось прочитать частично скачанную часть " + name)
                        buf = Array.newInstance(Byte.TYPE, 65536)
                        try:
                            while True:
                                n = inp.read(buf)
                                if n < 0:
                                    break
                                digest.update(bytes((int(buf[j]) & 255) for j in range(n)))
                        finally:
                            inp.close()
    
                    headers = {"User-Agent": "NS2026-Updater-Android"}
                    if offset:
                        headers["Range"] = "bytes={}-".format(offset)
                    Clock.schedule_once(
                        lambda dt, i=idx + 1, n=name, off=offset: self._download_status(
                            "Подключение к серверу • часть {}/17 {}{}".format(
                                i, n, " (продолжение)" if off else ""
                            )
                        ), 0
                    )
                    request = urllib.request.Request(item["browser_download_url"], headers=headers)
                    response = urllib.request.urlopen(request, timeout=60, context=NETWORK_CONTEXT)
                    status_code = getattr(response, "status", response.getcode())
                    if offset and status_code != 206:
                        response.close()
                        # Server ignored Range; restart this piece from byte zero.
                        digest = hashlib.md5()
                        offset = 0
                        request = urllib.request.Request(
                            item["browser_download_url"],
                            headers={"User-Agent": "NS2026-Updater-Android"},
                        )
                        response = urllib.request.urlopen(request, timeout=60, context=NETWORK_CONTEXT)
                    Clock.schedule_once(
                        lambda dt, i=idx + 1, n=name: self._download_status(
                            "Загрузка части {}/17: {}".format(i, n)
                        ), 0
                    )
                    if part is None:
                        part = Docs.createDocument(resolver, folder, "application/octet-stream", temp_name)
                    out = resolver.openOutputStream(part, "wa" if offset else "w")
                    if out is None:
                        response.close()
                        raise RuntimeError("Не удалось записать временную часть " + name)
                    got = offset
                    try:
                        with response:
                            while True:
                                data = response.read(1024 * 1024)
                                if not data:
                                    break
                                out.write(data)
                                digest.update(data)
                                got += len(data)
                                pct = 75.0 * (downloaded_bytes + got - offset) / max(1, base_size)
                                Clock.schedule_once(lambda dt, p=pct, n=name, r=got: self._download_progress(p, n, r), 0)
                        out.flush()
                    finally:
                        out.close()
    
                Clock.schedule_once(
                    lambda dt, i=idx + 1, n=name: self._download_status(
                        "Проверка загруженной части {}/17: {}".format(i, n)
                    ), 0
                )
                if got != expected_size:
                    raise RuntimeError("Размер части {} не совпал. Частичный файл сохранён для повторной загрузки.".format(name))
                if digest.hexdigest().lower() != expected_hash:
                    raise RuntimeError("MD5 части {} не совпал. Повреждённая часть оставлена до следующей проверки.".format(name))
                downloaded_bytes += expected_size
                Clock.schedule_once(lambda dt, i=idx+1: self._set_status("Проверено частей: {}/17".format(i)), 0)
    
                # Recheck the entire set before assembly so no unchecked piece enters
                # the final archive.
                part_uris = []
                for idx, item in enumerate(chunks):
                    Clock.schedule_once(
                        lambda dt, i=idx + 1, n=item["name"]: self._download_status(
                            "Контроль частей {}/17: {}".format(i, n)
                        ), 0
                    )
                    part_uri = find(folder, "." + item["name"] + ".download")
                    if part_uri is None:
                        raise RuntimeError("Не найдена скачанная часть " + item["name"])
                    size, digest = self._saf_hash_size(resolver, part_uri)
                    if size != int(item.get("size") or 0) or digest.lower() != item["expected_md5"].lower():
                        raise RuntimeError("Повторная проверка части {} не пройдена.".format(item["name"]))
                    part_uris.append(part_uri)
    
                # Stage 2: append verified chunks into a temporary assembly. Chunks
                # are retained; checkpoints are written only after each append closes.
                Clock.schedule_once(lambda dt: self._set_status("Все 17 частей готовы. Собираем общий архив…"), 0)
                assembly_name = "." + BASE_ASSET + ".assembling"
                assembly = find(folder, assembly_name)
                state_path = os.path.join(self.user_data_dir, "base_saf_state.json")
                state = {"next_chunk": 0, "committed_bytes": 0, "chunks_md5": hashes}
                if os.path.isfile(state_path):
                    try:
                        with open(state_path, "r", encoding="utf-8") as source:
                            saved = json.load(source)
                        if saved.get("chunks_md5") == hashes and not saved.get("complete"):
                            state.update(saved)
                    except Exception:
                        pass
    
                next_chunk = max(0, min(len(chunks), int(state.get("next_chunk", 0))))
                committed = max(0, int(state.get("committed_bytes", 0)))
                expected_committed = sum(int(chunks[i].get("size") or 0) for i in range(next_chunk))
                assembly_size = self._saf_size(resolver, assembly) if assembly is not None else 0
                # If a process died between appending and checkpointing, discard only
                # the assembly and rebuild from the still-preserved verified pieces.
                if assembly is None or assembly_size != committed or committed != expected_committed:
                    if assembly is not None and not self._saf_delete(resolver, Docs, assembly):
                        raise RuntimeError("Не удалось сбросить повреждённый промежуточный общий архив.")
                    assembly = Docs.createDocument(resolver, folder, "application/octet-stream", assembly_name)
                    next_chunk = 0
                    committed = 0
                    state = {"next_chunk": 0, "committed_bytes": 0, "chunks_md5": hashes, "complete": False}
                    self._save_chunk_state(state_path, state)
    
                for idx in range(next_chunk, len(chunks)):
                    item = chunks[idx]
                    Clock.schedule_once(
                        lambda dt, i=idx + 1, n=item["name"]: self._download_status(
                            "Объединение частей {}/17: {}".format(i, n)
                        ), 0
                    )
                    inp = resolver.openInputStream(part_uris[idx])
                    out = resolver.openOutputStream(assembly, "wa" if committed else "w")
                    if inp is None or out is None:
                        if inp is not None:
                            inp.close()
                        if out is not None:
                            out.close()
                        raise RuntimeError("Не удалось открыть поток для объединения " + item["name"])
                    buf = Array.newInstance(Byte.TYPE, 1024 * 1024)
                    copied = 0
                    try:
                        while True:
                            n = inp.read(buf)
                            if n < 0:
                                break
                            out.write(buf, 0, n)
                            copied += n
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
                        "chunks_md5": hashes,
                        "complete": False,
                    }
                    self._save_chunk_state(state_path, state)
                    pct = 75.0 + 20.0 * committed / max(1, base_size)
                    Clock.schedule_once(lambda dt, p=pct, n=item["name"], r=committed: self._download_progress(p, n, r), 0)
                    Clock.schedule_once(lambda dt, i=idx+1: self._set_status("Объединено частей: {}/17".format(i)), 0)
    
                Clock.schedule_once(
                    lambda dt: self._download_status("Финальная проверка общего архива…"), 0
                )
                assembled_size, assembled_md5 = self._saf_hash_size(resolver, assembly)
                if assembled_size != base_size:
                    raise RuntimeError("Размер общего gamedata.sq_base не совпал. Все 17 частей сохранены.")
                # Independently hash the exact byte stream of all 17 source pieces.
                # The assembled file must match this concatenation byte-for-byte.
                expected_assembly_digest = hashlib.md5()
                for idx, part_uri in enumerate(part_uris):
                    Clock.schedule_once(
                        lambda dt, i=idx + 1: self._download_status(
                            "Сверка общего архива с частью {}/17".format(i)
                        ), 0
                    )
                    stream = resolver.openInputStream(part_uri)
                    if stream is None:
                        raise RuntimeError("Не удалось повторно прочитать часть для итоговой проверки.")
                    buf = Array.newInstance(Byte.TYPE, 65536)
                    try:
                        while True:
                            n = stream.read(buf)
                            if n < 0:
                                break
                            expected_assembly_digest.update(bytes((int(buf[j]) & 255) for j in range(n)))
                    finally:
                        stream.close()
                if expected_assembly_digest.hexdigest() != assembled_md5:
                    raise RuntimeError("Общий архив не совпадает с последовательным объединением 17 частей. Части сохранены.")
                # SAF swap: retain the previous archive as a backup until the new
                # file has been renamed and verified.
                old_base = find(folder, BASE_ASSET)
                backup_name = "." + BASE_ASSET + ".backup"
                backup = find(folder, backup_name)
                if backup is not None and not self._saf_delete(resolver, Docs, backup):
                    raise RuntimeError("Не удалось освободить резервное имя для замены общего архива.")
                if old_base is not None:
                    backup_uri = Docs.renameDocument(resolver, old_base, backup_name)
                    if backup_uri is None:
                        raise RuntimeError("Не удалось сохранить предыдущий gamedata.sq_base в резервную копию.")
                    backup = backup_uri
    
                final_uri = Docs.renameDocument(resolver, assembly, BASE_ASSET)
                if final_uri is None:
                    if backup is not None:
                        Docs.renameDocument(resolver, backup, BASE_ASSET)
                    raise RuntimeError("Не удалось переименовать собранный архив. 17 частей сохранены.")
                final_size, final_md5 = self._saf_hash_size(resolver, final_uri)
                if final_size != base_size or final_md5 != assembled_md5:
                    if not self._saf_delete(resolver, Docs, final_uri):
                        failed_name = "." + BASE_ASSET + ".failed"
                        if find(folder, failed_name) is not None:
                            failed_name = failed_name + ".2"
                        failed_uri = Docs.renameDocument(resolver, final_uri, failed_name)
                        if failed_uri is None:
                            raise RuntimeError("Итоговая проверка не пройдена; новая и резервная копии оставлены, части сохранены.")
                    if backup is not None:
                        Docs.renameDocument(resolver, backup, BASE_ASSET)
                    raise RuntimeError("Проверка общего архива после сохранения не пройдена. Части сохранены.")
    
                self._save_chunk_state(state_path, {
                    "complete": True,
                    "chunks_md5": hashes,
                    "assembled_md5": assembled_md5,
                    "assembled_size": assembled_size,
                })
                base["downloaded"] = True
                # Cleanup is now strictly post-assembly and best effort. A SAF cleanup
                # failure must never invalidate the successfully assembled archive.
                Clock.schedule_once(
                    lambda dt: self._download_status("Архив проверен. Очистка временных частей…"), 0
                )
                for idx, item in enumerate(chunks):
                    self._saf_delete(resolver, Docs, part_uris[idx])
                if backup is not None:
                    self._saf_delete(resolver, Docs, backup)
    
                # Stage 3: download the remaining archives, using temp files and
            # verification before replacement. These are not required to rebuild
            # the already verified base archive.
            extras = [self.assets[n] for n in REQUIRED_ASSETS[1:] if not self.assets[n].get("downloaded")]
            extra_total = max(1, sum(int(x.get("size") or 0) for x in extras))
            extra_done = 0
            for item in extras:
                name = item["name"]
                Clock.schedule_once(
                    lambda dt, n=name: self._download_status("Дополнительный архив: {}".format(n)), 0
                )
                temp_name = "." + name + ".download"
                temp = find(folder, temp_name)
                expected_size = int(item.get("size") or 0)
                expected_hash = item["expected_md5"].lower()
                offset = self._saf_size(resolver, temp) if temp is not None else 0
                digest = hashlib.md5()
                if offset == expected_size and offset > 0:
                    try:
                        temp_valid = self._saf_md5(resolver, temp).lower() == expected_hash
                    except Exception:
                        temp_valid = False
                    if temp_valid:
                        old = find(folder, name)
                        if old is not None:
                            if not self._saf_delete(resolver, Docs, old):
                                raise RuntimeError("Не удалось заменить старый архив " + name)
                        if Docs.renameDocument(resolver, temp, name) is None:
                            raise RuntimeError("Не удалось завершить " + name)
                        item["downloaded"] = True
                        extra_done += expected_size
                        continue
                    if not self._saf_delete(resolver, Docs, temp):
                        raise RuntimeError("Android не разрешил удалить повреждённый временный файл " + name)
                    temp = None
                    offset = 0

                if offset > expected_size:
                    if not self._saf_delete(resolver, Docs, temp):
                        raise RuntimeError("Android не разрешил удалить повреждённый временный файл " + name)
                    temp = None
                    offset = 0
                if offset and temp is not None:
                    inp = resolver.openInputStream(temp)
                    if inp is None:
                        raise RuntimeError("Не удалось прочитать незавершённый архив " + name)
                    buf = Array.newInstance(Byte.TYPE, 65536)
                    try:
                        while True:
                            n = inp.read(buf)
                            if n < 0:
                                break
                            digest.update(bytes((int(buf[j]) & 255) for j in range(n)))
                    finally:
                        inp.close()
                headers = {"User-Agent": "NS2026-Updater-Android"}
                if offset:
                    headers["Range"] = "bytes={}-".format(offset)
                request = urllib.request.Request(item["browser_download_url"], headers=headers)
                response = urllib.request.urlopen(request, timeout=120, context=NETWORK_CONTEXT)
                status_code = getattr(response, "status", response.getcode())
                if offset and status_code != 206:
                    response.close()
                    digest = hashlib.md5()
                    offset = 0
                    request = urllib.request.Request(item["browser_download_url"], headers={"User-Agent": "NS2026-Updater-Android"})
                    response = urllib.request.urlopen(request, timeout=120, context=NETWORK_CONTEXT)
                if temp is None:
                    temp = Docs.createDocument(resolver, folder, "application/octet-stream", temp_name)
                out = resolver.openOutputStream(temp, "wa" if offset else "w")
                if out is None:
                    response.close()
                    raise RuntimeError("Не удалось записать " + name)
                got = offset
                try:
                    with response:
                        while True:
                            data = response.read(1024 * 1024)
                            if not data:
                                break
                            out.write(data)
                            digest.update(data)
                            got += len(data)
                            pct = 95.0 + 5.0 * (extra_done + got - offset) / extra_total
                            Clock.schedule_once(lambda dt, p=pct, n=name, r=got: self._download_progress(p, n, r), 0)
                    out.flush()
                finally:
                    out.close()
                if got != expected_size:
                    raise RuntimeError("Размер файла {} не совпал. Незавершённый файл сохранён.".format(name))
                if digest.hexdigest().lower() != expected_hash:
                    raise RuntimeError("MD5 файла {} не совпал. Файл сохранён для повторной проверки.".format(name))
                old = find(folder, name)
                if old is not None:
                    if not self._saf_delete(resolver, Docs, old):
                        raise RuntimeError("Не удалось заменить старый архив " + name)
                if Docs.renameDocument(resolver, temp, name) is None:
                    raise RuntimeError("Не удалось завершить " + name)
                item["downloaded"] = True
                extra_done += got

            Clock.schedule_once(lambda dt: self._download_done(), 0)
        except Exception as exc:
            error = friendly_network_error(exc)
            Clock.schedule_once(lambda dt, e=error: self._download_failed(e), 0)
    def _download_status(self, message):
        self._set_status(message)

    def _download_progress(self, percent, name, received):
        self.progress.value = min(100, percent)
        self._set_status("Хабар доставляется… {:.0f}% • {}".format(min(100, percent), name))

    def _download_done(self):
        self.progress.value = 100
        self._set_signal("online")
        self._set_status("Хабар доставлен!\\nВсе архивы проверены и готовы.")
        self._set_busy(False)
        self.download_button.disabled = True

    def _download_failed(self, error):
        self._set_signal("offline")
        self._set_status("Доставка хабара прервана.\\n" + error)
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
