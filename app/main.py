from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.core.window import Window


class UpdaterApp(App):
    def build(self):
        self.title = "NS2026 Updater"

        layout = BoxLayout(
            orientation="vertical",
            padding=25,
            spacing=20
        )

        layout.add_widget(Label(
            text="[b]S.T.A.L.K.E.R.[/b]\nНАРОДНАЯ СОЛЯНКА 2026",
            markup=True,
            font_size="25sp",
            halign="center"
        ))

        layout.add_widget(Label(
            text="Центр обновления игры",
            font_size="18sp"
        ))

        self.status = Label(
            text="Ожидание подключения",
            font_size="16sp"
        )
        layout.add_widget(self.status)

        button = Button(
            text="ПРОВЕРИТЬ ОБНОВЛЕНИЯ",
            size_hint=(1, 0.25),
            font_size="18sp"
        )
        button.bind(on_press=self.check_updates)
        layout.add_widget(button)

        return layout

    def check_updates(self, instance):
        self.status.text = "Тестовая сборка работает"

    def on_start(self):
        Window.clearcolor = (0.06, 0.07, 0.06, 1)


if __name__ == "__main__":
    UpdaterApp().run()
