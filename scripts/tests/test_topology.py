from typing import Optional
import mock
import sys

if ".." not in sys.path:
    sys.path.append("..")  # TODO: make this more robust
import util
import conf

from dataclasses import dataclass, field
import tempfile


# TODO: use "real" classes once they are defined (instead of NSDict)
@dataclass
class TstNodeset:
    nodeset_name: str
    node_count_static: int = 0
    node_count_dynamic_max: int = 0


@dataclass
class TstNodesetTpu:
    nodeset_name: str
    node_count_static: int = 0
    node_count_dynamic_max: int = 0


@dataclass
class TstCfg:
    slurm_cluster_name: str = "m22"
    nodeset: dict[TstNodeset] = field(default_factory=dict)
    nodeset_tpu: dict[TstNodesetTpu] = field(default_factory=dict)
    output_dir: Optional[str] = None


@dataclass
class TstTPU:  # to prevent client initialization durint "TPU.__init__"
    vmcount: int


def make_to_hostlist_mock(tbl: Optional[dict[str, str]] = None, strict: bool = False):
    if tbl is None:
        tbl = {}

    def se(names: list[str]) -> str:
        k = ",".join(sorted(names))
        if k in tbl:
            return tbl[k]
        if strict:
            raise AssertionError(f"to_hostlist mock: unexpected nodenames: '{k}'")
        return k

    return se


def make_to_hostnames_mock(tbl: Optional[dict[str, list[str]]]):
    def se(k: str) -> list[str]:
        if k not in tbl:
            raise AssertionError(f"to_hostnames mock: unexpected nodelist: '{k}'")
        return tbl[k]

    return se


def test_gen_topology_conf_empty():
    cfg = TstCfg(output_dir=tempfile.mkdtemp())
    conf.gen_topology_conf(util.Lookup(cfg))
    assert (
        open(cfg.output_dir + "/cloud_topology.conf").read()
        == """
# Warning:
# This file is managed by a script. Manual modifications will be overwritten.


"""
    )


@mock.patch("util.TPU")
@mock.patch(
    "util.to_hostlist",
    side_effect=make_to_hostlist_mock(
        {
            "bold-0,bold-1,bold-2,bold-3": "bold-[0-3]",
            "m22-bold-0,m22-bold-1,m22-bold-2": "m22-bold-[0-2]",
            "m22-bold-4,m22-bold-5,m22-bold-6": "m22-bold-[4-6]",
            "m22-bold-7,m22-bold-8": "m22-bold-[7-8]",
        }
    ),
)
@mock.patch(
    "util.to_hostnames",
    side_effect=make_to_hostnames_mock(
        {
            "m22-bold-[0-3]": ["m22-bold-0", "m22-bold-1", "m22-bold-2", "m22-bold-3"],
            "m22-bold-[4-8]": [
                "m22-bold-4",
                "m22-bold-5",
                "m22-bold-6",
                "m22-bold-7",
                "m22-bold-8",
            ],
            "m22-slim-[0-2]": ["m22-slim-0", "m22-slim-1", "m22-slim-2"],
        }
    ),
)
def test_gen_topology_conf(to_hostnames_mock, to_hostlist_mock, tpu_mock):
    cfg = TstCfg(
        nodeset_tpu={
            "a": TstNodesetTpu("bold", node_count_static=4, node_count_dynamic_max=5),
            "b": TstNodesetTpu("slim", node_count_dynamic_max=3),
        },
        nodeset={
            "c": TstNodeset("green", node_count_static=2, node_count_dynamic_max=3),
            "d": TstNodeset("blue", node_count_static=7),
            "e": TstNodeset("pink", node_count_dynamic_max=4),
        },
        output_dir=tempfile.mkdtemp(),
    )

    def tpu_se(ns: TstNodesetTpu) -> TstTPU:
        if ns.nodeset_name == "bold":
            return TstTPU(vmcount=3)
        if ns.nodeset_name == "slim":
            return TstTPU(vmcount=1)
        raise AssertionError(f"unexpected TPU name: '{ns.nodeset_name}'")

    tpu_mock.side_effect = tpu_se

    conf.gen_topology_conf(util.Lookup(cfg))
    assert (
        open(cfg.output_dir + "/cloud_topology.conf").read()
        == """
# Warning:
# This file is managed by a script. Manual modifications will be overwritten.

SwitchName=nodeset_tpu-root Switches=bold,slim
SwitchName=bold Switches=bold-[0-3]
SwitchName=bold-0 Nodes=m22-bold-[0-2]
SwitchName=bold-1 Nodes=m22-bold-3
SwitchName=bold-2 Nodes=m22-bold-[4-6]
SwitchName=bold-3 Nodes=m22-bold-[7-8]
SwitchName=slim Nodes=m22-slim-[0-2]
SwitchName=nodeset-root Switches=blue,green,pink
SwitchName=blue Nodes=m22-blue-[0-6]
SwitchName=green Nodes=m22-green-[0-1],m22-green-[2-4]
SwitchName=pink Nodes=m22-pink-[0-3]

"""
    )
