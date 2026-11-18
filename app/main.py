# -*- coding: utf-8 -*-
import hashlib
import json
import os
import shutil
import threading
import urllib.error
import urllib.request

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
RELEASE_API = (
    "https://api.github.com/repos/"
    + OWNER + "/" + REPOSITORY + "/releases/tags/" + RELEASE_TAG
)
REQUIRED_ASSETS = (
    "gamedata.sq_base_configs",
    "gamedata.sq_patch",
    "gamedata.sq_patch_2",
)


def format_size(value):
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return "{:.2f} {}".format(value, unit)
        value /= 1024
    return "{:.2f} ТБ".format(value)


def http_open(url, accept="application/octet-stream"):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NS2026-Updater-Android",
            "Accept": accept,
        },
    )
    return urllib.request.urlopen(request, timeout=45)


class UpdaterApp(App):
    def build(self):
        self.title = "NS2026 Updater"
        self.release = None
        self.assets = {}
        self.busy = False
        self.download_dir = os.path.join(self.user_data_dir, "updates")
        os.makedirs(self.download_dir, exist_ok=True)

        Window.clearcolor = (0.035, 0.045, 0.04, 1)

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
            text="Файлы будут сохранены в хранилище приложения",
            color=(0.58, 0.62, 0.56, 1),
            font_size="11sp",
            size_hint_y=None,
            height=dp(30),
        )
        root.add_widget(self.footer)

        self._render_assets()
        return root

    def _fit_label(self, widget, size):
        widget.text_size = (size[0] - dp(12), None)

    def _set_status(self, message):
        self.status.text = message

    def _set_busy(self, value):
        self.busy = value
        self.check_button.disabled = value
        self.download_button.disabled = value or not self.assets

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
            with http_open(RELEASE_API, "application/vnd.github+json") as response:
                release = json.loads(response.read().decode("utf-8"))

            if release.get("tag_name") != RELEASE_TAG:
                raise RuntimeError("GitHub вернул неожиданный релиз.")

            found = {}
            for item in release.get("assets", []):
                if item.get("name") in REQUIRED_ASSETS:
                    found[item["name"]] = item

            missing = [name for name in REQUIRED_ASSETS if name not in found]
            if missing:
                raise RuntimeError(
                    "В релизе latest отсутствуют файлы: " + ", ".join(missing)
                )

            for name, asset in found.items():
                asset["downloaded"] = self._is_cached(asset)

            self.release = release
            self.assets = found
            total = sum(item["size"] for item in found.values())
            pending = sum(
                item["size"] for item in found.values()
                if not item["downloaded"]
            )
            release_name = release.get("name") or release.get("tag_name")
            message = (
                "Релиз: {}\nОпубликован: {}\nВсего: {} • К загрузке: {}"
            ).format(
                release_name,
                (release.get("published_at") or "дата не указана")[:10],
                format_size(total),
                format_size(pending),
            )
            Clock.schedule_once(lambda dt: self._check_done(message), 0)
        except Exception as exc:
            error = str(exc)
            Clock.schedule_once(lambda dt, e=error: self._check_failed(e), 0)

    def _is_cached(self, asset):
        path = os.path.join(self.download_dir, asset["name"])
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) != int(asset.get("size", -1)):
            return False

        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            return self._sha256(path) == digest.split(":", 1)[1].lower()

        # Without a published checksum, the GitHub asset ID and timestamp
        # are stored beside the cached file to identify the exact release asset.
        meta_path = path + ".json"
        try:
            with open(meta_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            return (
                metadata.get("id") == asset.get("id")
                and metadata.get("updated_at") == asset.get("updated_at")
                and metadata.get("size") == asset.get("size")
            )
        except Exception:
            return False

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
        pending = [
            (name, asset) for name, asset in self.assets.items()
            if not asset.get("downloaded")
        ]
        total_bytes = sum(asset["size"] for _, asset in pending)
        completed_bytes = 0

        try:
            free_bytes = shutil.disk_usage(self.download_dir).free
            safety_margin = 100 * 1024 * 1024
            if free_bytes < total_bytes + safety_margin:
                raise RuntimeError(
                    "Недостаточно свободного места. Нужно примерно {}, доступно {}.".format(
                        format_size(total_bytes + safety_margin),
                        format_size(free_bytes),
                    )
                )

            for name, asset in pending:
                target = os.path.join(self.download_dir, name)
                temporary = target + ".part"
                url = asset.get("browser_download_url")
                if not url:
                    raise RuntimeError("У файла {} нет ссылки загрузки.".format(name))

                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "NS2026-Updater-Android"},
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    response_length = response.headers.get("Content-Length")
                    if response_length and int(response_length) != int(asset["size"]):
                        raise RuntimeError(
                            "GitHub сообщил неожиданный размер файла {}.".format(name)
                        )

                    received = 0
                    digest = hashlib.sha256()
                    with open(temporary, "wb") as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            digest.update(chunk)
                            received += len(chunk)
                            overall = completed_bytes + received
                            percent = (
                                overall * 100.0 / total_bytes
                                if total_bytes else 100
                            )
                            Clock.schedule_once(
                                lambda dt, p=percent, n=name, r=received:
                                    self._download_progress(p, n, r),
                                0,
                            )

                if received != int(asset["size"]):
                    raise RuntimeError(
                        "Размер {} не совпал: получено {}, ожидалось {}.".format(
                            name, received, asset["size"]
                        )
                    )

                expected = asset.get("digest") or ""
                if expected.startswith("sha256:"):
                    if digest.hexdigest().lower() != expected.split(":", 1)[1].lower():
                        raise RuntimeError("SHA-256 не совпал для {}.".format(name))

                os.replace(temporary, target)
                with open(target + ".json", "w", encoding="utf-8") as meta:
                    json.dump({
                        "id": asset.get("id"),
                        "updated_at": asset.get("updated_at"),
                        "size": asset.get("size"),
                        "digest": asset.get("digest"),
                    }, meta, ensure_ascii=False)

                asset["downloaded"] = True
                completed_bytes += received

            Clock.schedule_once(lambda dt: self._download_done(), 0)
        except Exception as exc:
            error = str(exc)
            Clock.schedule_once(lambda dt, e=error: self._download_failed(e), 0)

    def _download_progress(self, percent, name, received):
        self.progress.value = min(100, percent)
        self._set_status(
            "Загрузка: {}\nПолучено: {}".format(name, format_size(received))
        )

    def _download_done(self):
        self.progress.value = 100
        self._render_assets()
        self._set_status("Загрузка завершена. Все три архива сохранены.")
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
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


if __name__ == "__main__":
    UpdaterApp().run()
