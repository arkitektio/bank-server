"""The external auth flow contract (v1): one shape for a login at any provider.

``startLink`` hands out an ``AuthSession``; ``completeAuth``, ``resumeAuth``, ``cancelAuth`` and
``authSession`` take its ``state``. End to end against fakebank (REDIRECT) and fakescalable (POLL).
"""

import asyncio
import threading
import time
from datetime import timedelta

import pytest
from django.utils import timezone

from bank_server.schema import schema
from finance import models
from tests.conftest import COMPLETE, LINK, SCALABLE_COMPLETE, SCALABLE_START, SESSION, account, connection_of

pytestmark = pytest.mark.django_db(transaction=True)

READ = "query($s: String!) { authSession(state: $s) { %s } }" % SESSION
RESUME = "mutation($s: String!) { resumeAuth(state: $s) { %s } }" % SESSION
CANCEL = "mutation($s: String!) { cancelAuth(state: $s) { %s } }" % SESSION
PENDING_AUTH = "{ bankConnections(filters: {status: PENDING}) { id pendingAuth { state openUrl status } } }"


@pytest.fixture
def started(aexecute, fakebank, eb_provider):
    """A login tenant A's user started at this test's fake bank (its session)."""

    async def _start() -> dict:
        fakebank.scenario([account(iban="AT111")])
        return (await aexecute(LINK, {"provider": eb_provider, "aspsp": fakebank.aspsp})).data["startLink"]

    return _start


def _exchanges(fakebank, since: int) -> int:
    return len([e for e in fakebank.log()[since:] if e["method"] == "POST" and e["path"] == "/sessions"])


# --- the two ways to finish --------------------------------------------------------------------


async def test_a_redirect_login_from_start_to_done(aexecute, fakebank, started):
    session = await started()
    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]

    done = (await aexecute(COMPLETE, {"code": fakebank.approve(session["state"]), "state": session["state"]})).data["completeAuth"]

    assert (session["status"], session["finish"], session["redirectUrl"]) == ("PENDING", "REDIRECT", "https://bank.test/callback")
    assert f"state={session['state']}" in session["openUrl"]
    assert session["interval"] is None and session["userCode"] is None and session["step"] is None and session["result"] is None
    assert read == session
    assert done["status"] == "DONE" and done["errorCode"] is None
    assert done["result"] == {"identifier": "@bank/connection", "id": done["result"]["id"], "label": fakebank.aspsp}
    assert [a["iban"] for a in (await connection_of(aexecute, done))["accounts"]] == ["AT111"]


async def test_a_poll_login_steps_through_the_second_factor(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": {"cash": 1}}, mfa=True)
    fakescalable.config(interval=3)
    session = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(session["userCode"])

    waiting = (await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})).data["completeAuth"]  # the dialog closes here
    resumed = (await aexecute(RESUME, {"s": session["state"]})).data["resumeAuth"]
    fakescalable.mfa("SUCCESS")
    done = (await aexecute(SCALABLE_COMPLETE, {"state": resumed["state"]})).data["completeAuth"]

    assert (session["finish"], session["interval"], session["redirectUrl"]) == ("POLL", 3, None)
    assert session["openUrl"].endswith(f"user_code={session['userCode']}")
    assert (waiting["status"], waiting["step"]) == ("PENDING", "MFA")
    assert resumed == waiting
    assert done["status"] == "DONE" and done["result"]["identifier"] == "@bank/connection"


async def test_a_state_is_long_enough_to_be_unguessable(started):
    session = await started()

    assert len(session["state"]) >= 43  # 32 random bytes, url-safe


# --- settling once -----------------------------------------------------------------------------


async def test_completing_a_finished_login_again_answers_the_same_and_exchanges_nothing(aexecute, fakebank, started):
    session = await started()
    code = fakebank.approve(session["state"])
    seen = len(fakebank.log())

    first = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    again = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    without_code = (await aexecute(COMPLETE, {"state": session["state"]})).data["completeAuth"]

    assert first["status"] == "DONE" and again == first and without_code == first
    assert _exchanges(fakebank, seen) == 1


async def test_two_completions_at_once_exchange_the_code_once(aexecute, authenticated_context, fakebank, started):
    """The callback page and a pasted redirect race: a real second thread, parked mid-exchange at the bank."""
    session = await started()
    variables = {"code": fakebank.approve(session["state"]), "state": session["state"]}
    seen = len(fakebank.log())
    outcome: dict = {}

    def first() -> None:
        from django.db import connection

        try:
            outcome["result"] = asyncio.run(schema.execute(COMPLETE, variable_values=variables, context_value=authenticated_context))
        finally:
            connection.close()

    fakebank.hold()
    thread = threading.Thread(target=first)
    thread.start()
    deadline = time.monotonic() + 10
    while fakebank.held() != 1:
        assert time.monotonic() < deadline, "the first completion never reached the bank"
        await asyncio.sleep(0.02)

    meanwhile = (await aexecute(COMPLETE, variables)).data["completeAuth"]
    fakebank.release()
    await asyncio.to_thread(thread.join)
    winner = outcome["result"]
    after = (await aexecute(COMPLETE, variables)).data["completeAuth"]

    assert meanwhile["status"] == "PENDING" and meanwhile["errorCode"] is None
    assert not winner.errors and winner.data["completeAuth"]["status"] == "DONE"
    assert after == winner.data["completeAuth"]
    assert _exchanges(fakebank, seen) == 1
    assert await models.BankConnection.objects.filter(state=session["state"]).acount() == 1


async def test_a_code_the_bank_refuses_ends_the_login_as_failed(aexecute, started):
    session = await started()

    failed = (await aexecute(COMPLETE, {"code": "not-the-code", "state": session["state"]})).data["completeAuth"]
    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]

    assert (failed["status"], failed["errorCode"]) == ("FAILED", "BANK_ERROR") and failed["errorMessage"]
    assert failed["result"] is None and read == failed


async def test_the_providers_refusal_is_recorded_in_its_words(aexecute, fakebank, started):
    session = await started()
    seen = len(fakebank.log())

    refused = (await aexecute(COMPLETE, {"state": session["state"], "error": "access_denied", "description": "The user cancelled at the bank."})).data["completeAuth"]
    later = (await aexecute(COMPLETE, {"state": session["state"], "code": fakebank.approve(session["state"])})).data["completeAuth"]

    assert (refused["status"], refused["errorCode"], refused["errorMessage"]) == ("FAILED", "LOGIN_REFUSED", "The user cancelled at the bank.")
    assert later == refused and _exchanges(fakebank, seen) == 0


async def test_a_denied_second_factor_is_failed_on_every_later_call(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": {"cash": 1}}, mfa=True)
    session = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(session["userCode"])
    await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})
    fakescalable.mfa("DENY")

    first = (await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})).data["completeAuth"]
    again = (await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})).data["completeAuth"]

    assert (first["status"], first["errorCode"]) == ("FAILED", "MFA_REJECTED") and again == first
    assert (await models.BankConnection.objects.aget(state=session["state"])).last_error_code == "MFA_REJECTED"


async def test_a_throttled_provider_does_not_end_a_login(aexecute, fakescalable, sc_provider):
    """One bad answer while the login waits for the phone is an error of that call, not of the login."""
    fakescalable.seed({"p1": {"cash": 1}}, mfa=True)
    session = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(session["userCode"])
    await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})
    fakescalable.mfa("SUCCESS")
    fakescalable.config(graphql_rate_limited=1)

    throttled = await aexecute(SCALABLE_COMPLETE, {"state": session["state"]}, allow_errors=True)
    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]
    done = (await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})).data["completeAuth"]

    assert throttled.errors[0].extensions["code"] == "RATE_LIMITED"
    assert (read["status"], read["step"]) == ("PENDING", "MFA")
    assert done["status"] == "DONE"


# --- running out -------------------------------------------------------------------------------


async def test_a_login_nobody_approved_in_time_is_expired(aexecute, fakebank, started):
    session = await started()
    code = fakebank.approve(session["state"])
    seen = len(fakebank.log())
    await models.BankConnection.objects.filter(state=session["state"]).aupdate(pending_expires_at=timezone.now() - timedelta(minutes=1))

    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]
    resumed = (await aexecute(RESUME, {"s": session["state"]})).data["resumeAuth"]
    completed = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    listed = (await aexecute(PENDING_AUTH)).data["bankConnections"]

    assert (read["status"], read["errorCode"]) == ("EXPIRED", "CODE_EXPIRED") and resumed == read
    assert completed["status"] == "EXPIRED" and _exchanges(fakebank, seen) == 0
    assert [c["pendingAuth"] for c in listed] == []  # it is FAILED now, no longer a pending login


async def test_a_login_waiting_for_its_second_factor_outlives_the_code(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": {"cash": 1}}, mfa=True)
    session = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(session["userCode"])
    await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})
    await models.BankConnection.objects.filter(state=session["state"]).aupdate(pending_expires_at=timezone.now() - timedelta(minutes=1))

    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]
    fakescalable.mfa("SUCCESS")
    done = (await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})).data["completeAuth"]

    assert (read["status"], read["step"]) == ("PENDING", "MFA")
    assert done["status"] == "DONE"


# --- whose login it is -------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["colleague_context", "other_org_context"])
async def test_only_who_started_a_login_reaches_it(aexecute, fakebank, started, who, colleague_context, other_org_context):
    """Another member of the organization, and another organization, get what an unknown state gets."""
    stranger = {"colleague_context": colleague_context, "other_org_context": other_org_context}[who]
    session = await started()
    code = fakebank.approve(session["state"])
    seen = len(fakebank.log())

    answers = [
        await aexecute(READ, {"s": session["state"]}, context=stranger, allow_errors=True),
        await aexecute(RESUME, {"s": session["state"]}, context=stranger, allow_errors=True),
        await aexecute(CANCEL, {"s": session["state"]}, context=stranger, allow_errors=True),
        await aexecute(COMPLETE, {"code": code, "state": session["state"]}, context=stranger, allow_errors=True),
    ]
    unknown = await aexecute(READ, {"s": "not-a-state"}, allow_errors=True)
    still = (await aexecute(READ, {"s": session["state"]})).data["authSession"]

    assert [a.errors[0].extensions["code"] for a in answers] == ["INVALID_STATE"] * 4
    assert [a.errors[0].message for a in answers] == [unknown.errors[0].message] * 4
    assert still["status"] == "PENDING" and _exchanges(fakebank, seen) == 0


# --- resume and cancel -------------------------------------------------------------------------


async def test_a_pending_login_is_offered_on_its_connection_to_who_started_it(aexecute, colleague_context, started):
    session = await started()

    mine = (await aexecute(PENDING_AUTH)).data["bankConnections"]
    theirs = (await aexecute(PENDING_AUTH, context=colleague_context)).data["bankConnections"]
    resumed = (await aexecute(RESUME, {"s": mine[0]["pendingAuth"]["state"]})).data["resumeAuth"]

    assert [c["pendingAuth"] for c in mine] == [{"state": session["state"], "openUrl": session["openUrl"], "status": "PENDING"}]
    assert [c["pendingAuth"] for c in theirs] == [None]
    assert resumed == session


async def test_cancel_is_idempotent_and_the_login_can_still_be_read(aexecute, fakebank, started):
    session = await started()
    code = fakebank.approve(session["state"])
    seen = len(fakebank.log())

    cancelled = (await aexecute(CANCEL, {"s": session["state"]})).data["cancelAuth"]
    again = (await aexecute(CANCEL, {"s": session["state"]})).data["cancelAuth"]
    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]
    completed = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    listed = (await aexecute("{ bankConnections { id } }")).data["bankConnections"]

    assert cancelled["status"] == "CANCELLED" and again == cancelled and read == cancelled and completed == cancelled
    assert listed == [] and _exchanges(fakebank, seen) == 0


async def test_cancelling_a_finished_login_changes_nothing(aexecute, fakebank, started):
    session = await started()
    done = (await aexecute(COMPLETE, {"code": fakebank.approve(session["state"]), "state": session["state"]})).data["completeAuth"]

    cancelled = (await aexecute(CANCEL, {"s": session["state"]})).data["cancelAuth"]

    assert cancelled == done
    assert (await connection_of(aexecute, cancelled))["status"] == "ACTIVE"


async def test_cancelling_a_login_waiting_for_its_second_factor_logs_it_out(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": {"cash": 1}}, mfa=True)
    session = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(session["userCode"])
    await aexecute(SCALABLE_COMPLETE, {"state": session["state"]})

    cancelled = (await aexecute(CANCEL, {"s": session["state"]})).data["cancelAuth"]

    assert cancelled["status"] == "CANCELLED"
    assert fakescalable.families() == [{"revoked": True, "refreshes": 0}]
    assert (await models.BankConnection.objects.aget(state=session["state"])).secret is None
