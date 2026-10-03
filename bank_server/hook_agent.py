"""bank's hook agent: the work the hub's rekuest can ask of this process (vendored ``rekuest_hook``).

An agent of its own, not a part of the service declared in ``bank_server.service``: the service
says what exists, the agent says what can be done. Each has its own entry in the hub's
configuration (``rekuest.services`` / ``rekuest.hook_agents``) and its own endpoints (``urls.py``).

Every organization has the agent, so an action is handed the slug of the organization a run is
for and does that organization's share of the work, nothing else. Nothing is wired: whether and
when an action runs (a schedule, a trigger, by hand) is the organization's own automation.
Nothing here loops or waits: each run is one pass rekuest started.
"""

from rekuest_hook import HookAgent

#: Its actions are declared in ``finance/scheduled.py`` (imported when the app is ready).
agent = HookAgent("bank", description="bank's housekeeping: work on its own data.")
