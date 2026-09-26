"""rekuest-service: expose a service's periodic work and events to its hub's rekuest — the service side of a HookAgent.

The source of truth is the ``rekuest-service`` package (``Code/packages/rekuest-service``).
Until it is on PyPI, services carry a copy of this directory, written by that package's
``scripts/vendor.py`` — edit the package, re-vendor, never edit a copy.

A service declares itself once, like an arkitekt ``App``::

    from rekuest_service import Service

    service = Service("mikro", description="Microscopy data")

    @service.action(default_interval=300)
    def reembed_stale() -> dict:
        '''Re-embed stale rows.'''
        return {"reembedded": ...}

    dataset_created = service.signal("@mikro/arraydataset", kinds=["CREATED"], descriptors=[...])

and mounts ``*service.urls`` (``_rekuest/hook`` and its manifest). Rekuest's reaper provisions
the service as a HookAgent from ``rekuest.service_agents``, reads the manifest, schedules every
action that declares a default, and checks users' triggers against the declared signals. Each
run arrives as a signed Assign; the service reports Started at once, runs the function in a
thread, and reports its return value (or the error) back to rekuest's intake. ``emit`` on a
declared signal announces an object — with the provenance token of the task it was created in —
and rekuest runs whatever triggers attach to it. The service keeps no queue and no timer.

Settings (``settings.REKUEST_HOOK``, from the service's ``rekuest_service`` config block)::

    SECRET       the HMAC secret shared with rekuest (its ``service_agents[].secret``)
    REKUEST_URL  rekuest's base URL on the internal network, e.g. http://rekuest:80/rekuest
    SERVICE      overrides the Service's name (what rekuest knows it by); optional
    MAX_SKEW     how old (seconds) a signed request may be; default 300

The module-level ``action`` / ``declare_signal`` / ``emit`` / ``views.urlpatterns`` register on
a process-default service; they remain for existing callers.
"""

from rekuest_service.registry import action, declare_signal, declared_signals, registered
from rekuest_service.service import Service, Signal
from rekuest_service.signals import emit

__all__ = ["Service", "Signal", "action", "declare_signal", "declared_signals", "emit", "registered"]
