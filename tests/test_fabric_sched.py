"""Tests for the D4 scheduler (pbatch/fabric_sched.py).

关键路径 / 就绪集 / 设备选择（可行集 → cordon → 反亲和 → 负载 → 局部性）。
纯函数测试，无网络。
"""

from __future__ import annotations

from pbatch import fabric
from pbatch.fabric_devices import TaskPlacement
from pbatch.fabric_sched import (TaskNode, critical_path, devices_from_status,
                                 pick_device, ready_nodes)


def _node(nid, deps=(), est=10.0):
    return TaskNode(id=nid, goal=nid, argv=["echo", nid],
                    depends_on=list(deps), estimate_seconds=est)


def test_critical_path_chain():
    nodes = [_node("a", est=10.0), _node("b", deps=("a",), est=20.0),
             _node("c", deps=("b",), est=5.0)]
    assert critical_path(nodes) == 35.0


def test_critical_path_parallel_branches():
    nodes = [_node("a", est=10.0), _node("b", deps=("a",), est=50.0),
             _node("c", deps=("a",), est=5.0)]
    assert critical_path(nodes) == 60.0  # 长分支决定总工期


def test_critical_path_empty():
    assert critical_path([]) == 0.0


def test_ready_nodes_topological():
    nodes = [_node("a"), _node("b", deps=("a",)), _node("c", deps=("b",)),
             _node("x")]
    assert {n.id for n in ready_nodes(nodes, set())} == {"a", "x"}
    assert {n.id for n in ready_nodes(nodes, {"a"})} == {"b", "x"}
    assert ready_nodes(nodes, {"a", "b", "c", "x"}) == []


def _local_device():
    device = fabric.local_probe()
    device["trust"] = {"max_effect_class": "irreversible"}
    return device


def test_pick_device_basic():
    devices = [_local_device()]
    placement = TaskPlacement(required_os=["Linux"])
    assert pick_device(placement, devices) == devices[0]["name"]


def test_pick_device_cordon_excluded():
    devices = [_local_device()]
    name = devices[0]["name"]
    assert pick_device(TaskPlacement(), devices, cordoned={name}) is None


def test_pick_device_load_preference():
    """负载最低者优先（调度效率层，AADM-D §12 第四层）。"""
    devices = [_local_device(), _local_device()]
    devices[1]["device_id"] = "other-" + devices[1]["device_id"]
    devices[1]["name"] = "busy-node"
    busy = {devices[0]["name"]: 0, devices[1]["name"]: 5}
    chosen = pick_device(TaskPlacement(), devices, busy=busy)
    assert chosen == devices[0]["name"]


def test_pick_device_prefers_locality():
    """数据局部性优先（模型/数据集已缓存的设备，AADM-D §14）。"""
    devices = [_local_device(), _local_device()]
    devices[1]["device_id"] = "other-" + devices[1]["device_id"]
    devices[1]["name"] = "cached-node"
    busy = {devices[0]["name"]: 0, devices[1]["name"]: 0}
    chosen = pick_device(TaskPlacement(), devices, busy=busy,
                         prefer={devices[1]["name"]})
    assert chosen == "cached-node"


def test_pick_device_avoid():
    devices = [_local_device()]
    assert pick_device(TaskPlacement(), devices,
                       avoid={devices[0]["name"]}) is None


def test_pick_device_no_candidates():
    placement = TaskPlacement(required_os=["Windows"])
    assert pick_device(placement, [_local_device()]) is None


def test_devices_from_status_shape():
    view = [{"device_id": "rn-1", "name": "node-a", "online": True,
             "cordoned": False,
             "static_capabilities": {"os": "Linux", "cpu_cores": 4},
             "runtimes": {"docker": True}}]
    devices = devices_from_status(view)
    assert devices[0]["name"] == "node-a"
    assert devices[0]["dynamic_state"]["status"] == "online"
    assert devices[0]["static_capabilities"]["cpu_cores"] == 4
    assert devices[0]["cordoned"] is False


def test_pick_device_excludes_non_schedulable_lifecycle():
    device = _local_device()
    device["lifecycle_state"] = "stale"
    assert pick_device(TaskPlacement(), [device]) is None


def test_devices_from_status_preserves_lifecycle_state():
    devices = devices_from_status([{
        "device_id": "rn-stale", "name": "stale-node", "online": True,
        "lifecycle_state": "stale", "static_capabilities": {},
    }])
    assert devices[0]["lifecycle_state"] == "stale"
    assert devices[0]["dynamic_state"]["status"] == "stale"
