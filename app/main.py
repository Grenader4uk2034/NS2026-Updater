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

        root.add_widget(Label(
            text="ЦЕНТР ОБНОВЛЕНИЯ",
            color=(0.72, 0.78, 0.66, 1),
            font_size="16sp",
            size_hint_y=None,
            height=dp(28),
        ))

        self.status = Label(
            text="Источник: GitHub / {}\nНажмите «Проверить обновления»".format(REPOSITORY),
            halign="center",
            valign="middle",
            font_size="14sp",
            size_hint_y=None,
            height=dp(62),
        )
        self.status.bind(size=self._fit_label)
        root.add_widget(self.status)

        self.asset_list = BoxLayout(
            orientation="vertical",
            spacing=dp(8),
            size_hint_y=None,
        )
        self.asset_list.bind(minimum_height=self.asset_list.setter("height"))

        scroll = ScrollView(do_scroll_x=False)
        scroll.add_widget(self.asset_list)
        root.add_widget(scroll)

        self.progress = ProgressBar(max=100, value=0, size_hint_y=None, height=dp(8))
        root.add_widget(self.progress)

        self.folder_button = Button(text="ВЫБРАТЬ ПАПКУ DOWNLOAD", size_hint_y=None, height=dp(48))
        self.folder_button.bind(on_press=self.choose_game_folder)
        root.add_widget(self.folder_button)

        self.install_button = Button(text="СОХРАНИТЬ В ВЫБРАННУЮ ПАПКУ", size_hint_y=None, height=dp(54), disabled=True)
        self.install_button.bind(on_press=self.install_updates)
        root.add_widget(self.install_button)

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
            text="Укажите папку Download на телефоне. Программа создаст NC OGSR автоматически.",
            color=(0.58, 0.62, 0.56, 1),
            font_size="11sp",
            size_hint_y=None,
            height=dp(30),
        )
        root.add_widget(self.footer)

        self._render_assets()
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
            self.footer.text = "Папка Download выбрана. Папка NC OGSR будет создана при сохранении." if self.tree_uri else "Папка Download не выбрана"
        if hasattr(self, "install_button"):
            self.install_button.disabled = self.busy or not self.tree_uri or not self._all_assets_cached()

    def choose_game_folder(self, instance):
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION)
            PythonActivity.mActivity.startActivityForResult(intent, self.request_tree_code)
            self._set_status("Выберите папку Download на телефоне.")
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
            self._set_status("Папка Download выбрана. Доступ сохранён.")
        except Exception as exc:
            self.tree_uri = None
            self._refresh_folder_ui()
            self._set_status("Не удалось сохранить доступ: " + str(exc))

    def install_updates(self, instance):
        if self.busy or not self.tree_uri or not self._all_assets_cached():
            return
        self._set_busy(True)
        self.progress.value = 0
        self._set_status("Установка архивов в выбранную папку…")
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self):
        """Stage every archive first; keep all backups until every promotion succeeds."""
        from_uri = None
        staged = {}
        backups = {}
        promoted = []
        resolver = None
        Docs = None

        try:
            from jnius import autoclass
            Uri = autoclass("android.net.Uri")
            Docs = autoclass("android.provider.DocumentsContract")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            FileInputStream = autoclass("java.io.FileInputStream")
            JavaArray = autoclass("java.lang.reflect.Array")
            JavaByte = autoclass("java.lang.Byte")
            activity = PythonActivity.mActivity
            resolver = activity.getContentResolver()
            tree = Uri.parse(self.tree_uri)
            tree_id = Docs.getTreeDocumentId(tree)
            parent = Docs.buildDocumentUriUsingTree(tree, tree_id)
            children_uri = Docs.buildChildDocumentsUriUsingTree(tree, tree_id)

            def find_child(filename):
                cursor = resolver.query(children_uri, None, None, None, None)
                if cursor is None:
                    raise RuntimeError("Не удалось прочитать выбранную папку.")
                try:
                    name_col = cursor.getColumnIndex("_display_name")
                    id_col = cursor.getColumnIndex("document_id")
                    if name_col < 0 or id_col < 0:
                        raise RuntimeError("Провайдер папки не вернул имя или ID файла.")
                    while cursor.moveToNext():
                        if cursor.getString(name_col) == filename:
                            return Docs.buildDocumentUriUsingTree(
                                tree, cursor.getString(id_col)
                            )
                finally:
                    cursor.close()
                return None

            # The user selects Download once. Create/use Download/NC OGSR
            # automatically and place the four finished archives inside it.
            output_folder = find_child("NC OGSR")
            if output_folder is None:
                output_folder = Docs.createDocument(
                    resolver, parent, "vnd.android.document/directory", "NC OGSR"
                )
            if output_folder is None:
                raise RuntimeError("Не удалось автоматически создать папку NC OGSR внутри Download.")
            parent = output_folder
            children_uri = Docs.buildChildDocumentsUriUsingTree(
                tree, Docs.getDocumentId(output_folder)
            )

            def rename_checked(uri, new_name, description):
                renamed = Docs.renameDocument(resolver, uri, new_name)
                if renamed is None:
                    raise RuntimeError(description + ": провайдер не поддержал переименование.")
                result = find_child(new_name)
                if result is None:
                    raise RuntimeError(description + ": файл после переименования не найден.")
                return result

            # Phase 1: copy and size-check all new files without touching originals.
            for index, name in enumerate(REQUIRED_ASSETS):
                asset = self.assets[name]
                local_path = os.path.join(self.download_dir, name)
                if not self._is_cached(asset):
                    raise RuntimeError("Архив не прошёл проверку: " + name)

                temp_name = "." + name + ".ns2026.tmp"
                if find_child(temp_name) is not None:
                    raise RuntimeError(
                        "Найден временный файл от предыдущей установки: " + temp_name
                        + ". Удалите его вручную или выберите папку без старых временных файлов."
                    )
                temp_uri = Docs.createDocument(
                    resolver, parent, "application/octet-stream", temp_name
                )
                if temp_uri is None:
                    raise RuntimeError("Не удалось создать временный файл: " + name)

                staged[name] = {"uri": temp_uri, "temp_name": temp_name}
                output = resolver.openOutputStream(temp_uri, "w")
                if output is None:
                    raise RuntimeError("Нет доступа на запись: " + name)
                source = FileInputStream(local_path)
                copied = 0
                try:
                    buffer = JavaArray.newInstance(JavaByte.TYPE, 65536)
                    while True:
                        count = source.read(buffer)
                        if count < 0:
                            break
                        output.write(buffer, 0, count)
                        copied += count
                    output.flush()
                finally:
                    source.close()
                    output.close()

                # Read staged SAF document back and verify its manifest MD5.
                expected_digest = (asset.get("expected_md5") or asset.get("assembled_md5") or "").lower()
                if len(expected_digest) != 32:
                    raise RuntimeError("В манифесте нет MD5 для " + name)
                if len(expected_digest) == 32:
                    verify_stream = resolver.openInputStream(temp_uri)
                    if verify_stream is None:
                        raise RuntimeError("Не удалось проверить записанный файл: " + name)
                    verified_digest = hashlib.md5()
                    verified_size = 0
                    try:
                        verify_buffer = JavaArray.newInstance(JavaByte.TYPE, 65536)
                        while True:
                            count = verify_stream.read(verify_buffer)
                            if count < 0:
                                break
                            verified_digest.update(bytes(
                                (int(verify_buffer[i]) & 0xff)
                                for i in range(count)
                            ))
                            verified_size += count
                    finally:
                        verify_stream.close()

                    actual_digest = verified_digest.hexdigest().lower()
                    if actual_digest != expected_digest:
                        raise RuntimeError(
                            "MD5 записанного файла не совпал: " + name
                        )

                percent = (index + 1) * 35.0 / len(REQUIRED_ASSETS)
                Clock.schedule_once(
                    lambda dt, p=percent, n=name:
                        self._install_progress(p, "Подготовлен: " + n), 0
                )

            # Phase 2: move every existing original to a backup. Do not delete
            # any backup until all three new archives have been promoted.
            for name in REQUIRED_ASSETS:
                old_uri = find_child(name)
                if old_uri is None:
                    continue
                backup_name = name + ".ns2026.bak"
                if find_child(backup_name) is not None:
                    raise RuntimeError(
                        "Уже существует резервная копия " + backup_name
                        + ". Она сохранена; автоматическая установка остановлена."
                    )
                backup_uri = Docs.renameDocument(resolver, old_uri, backup_name)
                if backup_uri is None:
                    raise RuntimeError(
                        "Не удалось сохранить прежний архив " + name
                        + ": провайдер не поддержал переименование."
                    )
                # Record the backup immediately so even a later query failure
                # cannot leave the original stranded under its backup name.
                backups[name] = backup_uri
                if find_child(backup_name) is None:
                    raise RuntimeError(
                        "Резервная копия не найдена после переименования: " + backup_name
                    )

            # Phase 3: promote staged files. Track each success for rollback.
            for index, name in enumerate(REQUIRED_ASSETS):
                installed_uri = Docs.renameDocument(
                    resolver, staged[name]["uri"], name
                )
                if installed_uri is None:
                    raise RuntimeError(
                        "Не удалось установить " + name
                        + ": провайдер не поддержал переименование."
                    )
                # Record promotion before querying the provider, so rollback
                # can remove the new file even if the subsequent query fails.
                staged[name]["installed_uri"] = installed_uri
                promoted.append(name)
                if find_child(name) is None:
                    raise RuntimeError(
                        "Файл не найден после установки: " + name
                    )
                percent = 35.0 + (index + 1) * 65.0 / len(REQUIRED_ASSETS)
                Clock.schedule_once(
                    lambda dt, p=percent, n=name:
                        self._install_progress(p, "Установлен: " + n), 0
                )

            # Commit point: only now may backups be removed.
            cleanup_errors = []
            for name, backup_uri in backups.items():
                try:
                    if not Docs.deleteDocument(resolver, backup_uri):
                        cleanup_errors.append(name + ".ns2026.bak")
                except Exception:
                    cleanup_errors.append(name + ".ns2026.bak")

            if cleanup_errors:
                Clock.schedule_once(
                    lambda dt, files=", ".join(cleanup_errors):
                        self._install_done_with_backups(files), 0
                )
            else:
                Clock.schedule_once(lambda dt: self._install_done(), 0)

        except Exception as exc:
            rollback_errors = []
            if resolver is not None and Docs is not None:
                # Remove newly installed files first, then restore every original.
                for name in reversed(promoted):
                    try:
                        current = find_child(name)
                        if current is not None and not Docs.deleteDocument(resolver, current):
                            rollback_errors.append("не удалось удалить новый " + name)
                    except Exception as rollback_exc:
                        rollback_errors.append(name + ": " + str(rollback_exc))

                for name, backup_uri in reversed(list(backups.items())):
                    try:
                        if find_child(name) is not None:
                            rollback_errors.append(
                                "оригинал " + name + " уже существует; резервная копия сохранена"
                            )
                            continue
                        rename_checked(
                            backup_uri, name, "Не удалось восстановить " + name
                        )
                    except Exception as rollback_exc:
                        rollback_errors.append(name + ": " + str(rollback_exc))

                # Remove only temporary files; never delete backups on failure.
                for name, info in staged.items():
                    try:
                        if name not in promoted:
                            temp_uri = find_child(info["temp_name"])
                            if temp_uri is not None:
                                Docs.deleteDocument(resolver, temp_uri)
                    except Exception as rollback_exc:
                        rollback_errors.append(
                            "временный файл " + name + ": " + str(rollback_exc)
                        )

            error = str(exc)
            if rollback_errors:
                error += "\\nОШИБКИ ОТКАТА: " + "; ".join(rollback_errors)
                error += "\\nРезервные копии .ns2026.bak не удаляйте."
            Clock.schedule_once(lambda dt, e=error: self._install_failed(e), 0)

    def _install_progress(self, percent, name):
        self.progress.value = percent
        self._set_status("Установлен: " + name)

    def _install_done(self):
        self.progress.value = 100
        self._set_status("Готово. Четыре архива установлены в выбранную папку.")
        self._set_busy(False)
        self._refresh_folder_ui()

    def _install_done_with_backups(self, files):
        self.progress.value = 100
        self._set_status(
            "Все четыре архива установлены. Старые копии сохранены: "
            + files + ". Удалите их вручную после проверки игры."
        )
        self._set_busy(False)
        self._refresh_folder_ui()

    def _install_failed(self, error):
        self._set_status("Ошибка установки: " + error)
        self._set_busy(False)
        self._refresh_folder_ui()

    def _fit_label(self, widget, size):
        widget.text_size = (size[0] - dp(12), None)

    def _set_status(self, message):
        self.status.text = message

    def _set_busy(self, value):
        self.busy = value
        self.check_button.disabled = value
        self.download_button.disabled = value or not any(not item.get("downloaded") for item in self.assets.values())
        self.folder_button.disabled = value
        self.install_button.disabled = value or not self.tree_uri or not self._all_assets_cached()

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
        self._set_status("Подключение к GitHub…")
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

            base_path = os.path.join(self.download_dir, BASE_ASSET)
            state_path = base_path + ".state"
            assembled_md5 = None
            if os.path.isfile(base_path) and os.path.isfile(state_path):
                try:
                    with open(state_path, "r", encoding="utf-8") as state_file:
                        state = json.load(state_file)
                    if state.get("complete") and state.get("chunks_md5") == chunk_hashes:
                        assembled_md5 = self._md5(base_path)
                        if assembled_md5 != state.get("assembled_md5"):
                            assembled_md5 = None
                except Exception:
                    assembled_md5 = None

            base_size = sum(int(item.get("size") or 0) for item in chunks)
            base_asset = {
                "name": BASE_ASSET,
                "size": base_size,
                "chunks": chunks,
                "expected_md5": assembled_md5,
                "assembled_md5": assembled_md5,
                "downloaded": bool(assembled_md5),
            }
            found = {BASE_ASSET: base_asset}
            for name in REQUIRED_ASSETS[1:]:
                item = dict(release_assets[name])
                item["expected_md5"] = expected_hashes[name]
                item["downloaded"] = self._is_cached(item)
                found[name] = item

            self.release = release
            self.assets = found
            total = sum(item["size"] for item in found.values())
            pending = sum(item["size"] for item in found.values() if not item["downloaded"])
            release_name = release.get("name") or release.get("tag_name")
            message = ("Релиз: {}\\n17 частей основного архива + 3 дополнительных файла\\nВсего: {} • К загрузке: {}").format(
                release_name, format_size(total), format_size(pending)
            )
            Clock.schedule_once(lambda dt: self._check_done(message), 0)
        except Exception as exc:
            error = friendly_network_error(exc)
            Clock.schedule_once(lambda dt, e=error: self._check_failed(e), 0)

    def _check_done(self, message):
        self._render_assets()
        self._set_status(message)
        self._set_busy(False)
        self.download_button.disabled = not any(
            not item.get("downloaded") for item in self.assets.values()
        )

    def _check_failed(self, error):
        self._set_status("Не удалось проверить обновления:\n" + error)
        self._set_busy(False)
        self.download_button.disabled = True

    def download_updates(self, instance):
        if self.busy or not self.assets:
            return
        self._set_busy(True)
        self.progress.value = 0
        self._set_status("Подготовка загрузки…")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        try:
            base = self.assets[BASE_ASSET]
            chunks = base["chunks"]
            base_path = os.path.join(self.download_dir, BASE_ASSET)
            assembling = base_path + ".assembling"
            state_path = base_path + ".state"
            chunk_hashes = [item["expected_md5"] for item in chunks]

            if not base.get("downloaded"):
                # A journal records only fully verified and appended parts. If the
                # process died during append, truncate to the last committed byte.
                state = {"next_chunk": 0, "committed_bytes": 0, "chunks_md5": chunk_hashes}
                if os.path.isfile(assembling) and os.path.isfile(state_path):
                    try:
                        with open(state_path, "r", encoding="utf-8") as source:
                            saved = json.load(source)
                        if saved.get("chunks_md5") == chunk_hashes and not saved.get("complete"):
                            state.update(saved)
                    except Exception:
                        pass
                next_chunk = int(state.get("next_chunk", 0))
                committed = int(state.get("committed_bytes", 0))
                if next_chunk < 0 or next_chunk > len(chunks) or committed < 0:
                    next_chunk, committed = 0, 0
                mode = "r+b" if os.path.isfile(assembling) else "w+b"
                with open(assembling, mode) as output:
                    output.truncate(committed)
                    output.seek(committed)
                    total_bytes = sum(int(item.get("size") or 0) for item in chunks)
                    completed_bytes = committed
    
                    for index in range(next_chunk, len(chunks)):
                        item = chunks[index]
                        name = item["name"]
                        temporary = os.path.join(self.download_dir, name + ".part")
                        expected = item["expected_md5"]
                        url = item.get("browser_download_url")
                        if not url:
                            raise RuntimeError("У части {} нет ссылки загрузки.".format(name))
    
                        # Keep a resumable .part file. Resume only when the server
                        # confirms HTTP 206; otherwise restart that part safely.
                        offset = os.path.getsize(temporary) if os.path.isfile(temporary) else 0
                        headers = {"User-Agent": "NS2026-Updater-Android"}
                        if offset:
                            headers["Range"] = "bytes={}-".format(offset)
                        request = urllib.request.Request(url, headers=headers)
                        response = urllib.request.urlopen(request, timeout=120, context=NETWORK_CONTEXT)
                        if offset and getattr(response, "status", response.getcode()) != 206:
                            response.close()
                            offset = 0
                            request = urllib.request.Request(
                                url, headers={"User-Agent": "NS2026-Updater-Android"}
                            )
                            response = urllib.request.urlopen(request, timeout=120, context=NETWORK_CONTEXT)
    
                        digest = hashlib.md5()
                        if offset:
                            with open(temporary, "rb") as existing:
                                for old_chunk in iter(lambda: existing.read(1024 * 1024), b""):
                                    digest.update(old_chunk)
                        received = offset
                        with response:
                            with open(temporary, "ab" if offset else "wb") as part_file:
                                while True:
                                    data = response.read(1024 * 1024)
                                    if not data:
                                        break
                                    part_file.write(data)
                                    digest.update(data)
                                    received += len(data)
                                    percent = (completed_bytes + received - offset) * 100.0 / max(1, total_bytes)
                                    Clock.schedule_once(
                                        lambda dt, p=percent, n=name, r=received:
                                            self._download_progress(p, n, r), 0
                                    )
                                part_file.flush()
                                os.fsync(part_file.fileno())
    
                        if digest.hexdigest().lower() != expected:
                            try:
                                os.remove(temporary)
                            except OSError:
                                pass
                            raise RuntimeError("MD5 не совпал для {}. Часть удалена; запустите загрузку повторно.".format(name))
    
                        # Append only after the entire part has passed MD5.
                        with open(temporary, "rb") as part_file:
                            shutil.copyfileobj(part_file, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                        committed += received
                        completed_bytes += received - offset
                        state = {
                            "next_chunk": index + 1,
                            "committed_bytes": committed,
                            "chunks_md5": chunk_hashes,
                            "complete": False,
                        }
                        with open(state_path, "w", encoding="utf-8") as journal:
                            json.dump(state, journal)
                            journal.flush()
                            os.fsync(journal.fileno())
                        os.remove(temporary)
                        Clock.schedule_once(
                            lambda dt, n=index + 1:
                                self._set_status("Проверено и добавлено частей: {}/17".format(n)), 0
                        )
    
                assembled_md5 = self._md5(assembling)
                os.replace(assembling, base_path)
                state = {
                    "next_chunk": len(chunks),
                    "committed_bytes": os.path.getsize(base_path),
                    "chunks_md5": chunk_hashes,
                    "assembled_md5": assembled_md5,
                    "complete": True,
                }
                with open(state_path, "w", encoding="utf-8") as journal:
                    json.dump(state, journal)
                base["assembled_md5"] = assembled_md5
                base["expected_md5"] = assembled_md5
                base["downloaded"] = True
    
    
            # Download and verify the three independent archives afterwards.
            extras = [self.assets[name] for name in REQUIRED_ASSETS[1:] if not self.assets[name].get("downloaded")]
            extras_total = sum(int(item["size"]) for item in extras)
            extras_done = 0
            for item in extras:
                name = item["name"]
                target = os.path.join(self.download_dir, name)
                temporary = target + ".part"
                url = item.get("browser_download_url")
                if not url:
                    raise RuntimeError("У файла {} нет ссылки загрузки.".format(name))
                request = urllib.request.Request(url, headers={"User-Agent": "NS2026-Updater-Android"})
                digest = hashlib.md5()
                received = 0
                with urllib.request.urlopen(request, timeout=120, context=NETWORK_CONTEXT) as response:
                    with open(temporary, "wb") as output:
                        while True:
                            data = response.read(1024 * 1024)
                            if not data:
                                break
                            output.write(data)
                            digest.update(data)
                            received += len(data)
                            percent = 90 + (extras_done + received) * 10.0 / max(1, extras_total)
                            Clock.schedule_once(
                                lambda dt, p=percent, n=name, r=received:
                                    self._download_progress(p, n, r), 0
                            )
                        output.flush()
                        os.fsync(output.fileno())
                if digest.hexdigest().lower() != item["expected_md5"]:
                    os.remove(temporary)
                    raise RuntimeError("MD5 не совпал для {}.".format(name))
                os.replace(temporary, target)
                item["downloaded"] = True
                extras_done += received

            Clock.schedule_once(lambda dt: self._download_done(), 0)
        except Exception as exc:
            error = friendly_network_error(exc)
            Clock.schedule_once(lambda dt, e=error: self._download_failed(e), 0)

    def _download_progress(self, percent, name, received):
        self.progress.value = min(100, percent)
        self._set_status(
            "Загрузка: {}\nПолучено: {}".format(name, format_size(received))
        )

    def _download_done(self):
        self.progress.value = 100
        self._render_assets()
        self._set_status("Загрузка завершена. Сформированы четыре архива; 17 частей проверены и объединены.")
        self._set_busy(False)
        self.download_button.disabled = True

    def _download_failed(self, error):
        self._set_status("Ошибка загрузки:\n" + error)
        self._set_busy(False)
        self._render_assets()
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
