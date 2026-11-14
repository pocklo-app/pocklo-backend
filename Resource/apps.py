from django.apps import AppConfig


class ResourceConfig(AppConfig):
    name = 'Resource'

    def ready(self):
        from Resource import song_signals  # noqa: F401
