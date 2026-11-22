import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bank_server.settings")

# Traces and metrics, when the config names a collector (`telemetry.endpoint`): set up before
# Django's application is built. Off, and costing nothing, otherwise.
from arkitekt_service import telemetry  # noqa: E402

telemetry.setup()

from django.core.asgi import get_asgi_application  # noqa: E402

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()


from kante.router import router  # noqa: E402

from .schema import schema  # noqa: E402

# Request/response only: nothing loops in here. Accounts sync when a client asks
# (``syncAccount`` / ``syncConnection``), or when the hub's rekuest runs the scheduled
# ``sync_all_accounts`` (``finance.scheduled``) — also just a request, to ``_rekuest/hook``.
application = telemetry.wrap(
    router(
        schema=schema,
        django_asgi_app=django_asgi_app,
        schema_path="schema",
    )
)
