from django.apps import AppConfig


class DatalayerConfig(AppConfig):
    """A Django AppConfig for the datalayer app. This is used to configure the datalayer app and set up any necessary configurations or signals when the app is ready."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "datalayer"
    verbose_name = "Datalayer"
