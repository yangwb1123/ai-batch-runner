"""Real signed JWT + HTTP + SQLite task reads; local issuer/metadata are owned substitutes."""
from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from pbatch.hub_client import HubClient, HubError
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from pbatch.hub_task_center_store import source_key
from test_hub_auth import signed


@pytest.fixture
def signed_task_hub(tmp_path,signed):
    auth,token,*_rest=signed
    auth.grants=[replace(auth.grants[0],scopes=frozenset({"agent.instances:read","agent.sessions:read"}))]
    store=HubStore(tmp_path/"owned.sqlite",clock=lambda:10)
    for tenant,instance in (("team","alpha"),("team","beta"),("other","alpha")):
        store.register_instance(tenant,instance,{"boot_id":"owned"})
        session=store.upsert_session(tenant,instance,1,{"local_session_id":"same","name":"owned "+instance,
                                                     "controllable":True})
        turn=store.submit_turn(tenant,session["session_id"],"actor","PRIVATE-SYNTHETIC-PROMPT",tenant+instance)
        store.append_event(tenant,instance,1,{"session_id":session["session_id"],"turn_id":turn["turn_id"],
            "event_id":"failed-"+instance,"kind":"turn.failed","payload":{"text":"PRIVATE-SYNTHETIC-BODY"}})
    server=HubHTTPServer(("127.0.0.1",0),store,auth)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f"http://127.0.0.1:{server.server_port}"
    try:
        yield auth,token,store,origin
    finally:
        server.shutdown();server.server_close();thread.join(5);store.close()
        assert not thread.is_alive()


def test_signed_scope_intersection_tenant_instance_and_current_grant_are_enforced_per_read(signed_task_hub):
    auth,token,_store,origin=signed_task_hub
    client=HubClient(origin,token(scope="agent.instances:read agent.sessions:read agent.tasks:write"))
    value=client.task_snapshot()
    assert value["observed_task_count"]==2 and value["attention"]["observed_items"]==1
    assert {row["hub"]["instance_id"] for row in value["sources"]}=={"alpha"}
    assert "PRIVATE" not in json.dumps(value) and value["scope"]["tenant_id"]=="team"
    assert client.task_snapshot(source=source_key("team","beta"))["tasks"]==[]
    auth.grants=[replace(auth.grants[0],instances=frozenset({"beta"}))]
    second=client.task_snapshot()
    assert {row["hub"]["instance_id"] for row in second["sources"]}=={"beta"}
    assert not set(row["task_id"] for row in value["tasks"]) & set(row["task_id"] for row in second["tasks"])


@pytest.mark.parametrize("claims,status", [({"scope":"agent.sessions:read"},403),
    ({"scope":"agent.instances:read"},403),({"scope":"agent.tasks:read"},403),
    ({"tenant_id":"other"},403),({"aud":"other"},401),({"exp":1},401)])
def test_signed_bad_authority_cannot_read_any_task_fact(signed_task_hub,claims,status):
    _auth,token,_store,origin=signed_task_hub
    with pytest.raises(HubError) as error:
        HubClient(origin,token(**dict({"scope":"agent.instances:read agent.sessions:read"},**claims))).task_snapshot()
    assert error.value.status==status
