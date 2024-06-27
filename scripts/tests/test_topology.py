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
    real_topology: bool = False


@dataclass
class TstCfg:
    slurm_cluster_name: str = "m22"
    nodeset: dict[str, TstNodeset] = field(default_factory=dict)
    nodeset_dyn: dict[str, TstNodeset] = field(default_factory=dict)
    nodeset_tpu: dict[str, TstNodeset] = field(default_factory=dict)
    output_dir: Optional[str] = None


@dataclass
class TstTPU:  # to prevent client initialization durint "TPU.__init__"
    vmcount: int


@dataclass
class TstInstance:
    name: str
    region: str
    zone: str
    placementPolicyId: Optional[str] = None
    physicalHost: Optional[str] = None

    @property
    def resourceStatus(self):
        return {"physicalHost": self.physicalHost}


def make_to_hostnames_mock(tbl: Optional[dict[str, list[str]]]):
    tbl = tbl or {}

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
def test_gen_topology(tpu_mock):
    cfg = TstCfg(
        nodeset_tpu={
            "a": TstNodeset("bold", node_count_static=4, node_count_dynamic_max=5),
            "b": TstNodeset("slim", node_count_dynamic_max=3),
        },
        nodeset={
            "c": TstNodeset("green", node_count_static=2, node_count_dynamic_max=3),
            "d": TstNodeset("blue", node_count_static=7),
            "e": TstNodeset("pink", node_count_dynamic_max=4),
            "f": TstNodeset(
                "violet",
                node_count_static=2,
                node_count_dynamic_max=3,
                real_topology=True,
            ),
        },
        nodeset_dyn={
            "g": TstNodeset("crimson"),
        },
    )

    def tpu_se(ns: TstNodeset) -> TstTPU:
        if ns.nodeset_name == "bold":
            return TstTPU(vmcount=3)
        if ns.nodeset_name == "slim":
            return TstTPU(vmcount=1)
        raise AssertionError(f"unexpected TPU name: '{ns.nodeset_name}'")

    tpu_mock.side_effect = tpu_se
    lkp = util.Lookup(cfg)
    lkp.instances = lambda: [
        TstInstance("m22-violet-0", region="us", zone="nm"),  # no physicalHost
        TstInstance(
            "m22-violet-2", region="us", zone="nm", physicalHost="/x/y/z"
        ),  # 3-parts topo
        TstInstance(
            "m22-crimson-fhtagn", region="us", zone="wy", physicalHost="/w/x/y/z"
        ),  # 4-parts topo
        # !!! add 5-parts case
    ]

    uncompressed = conf.gen_topology(lkp)

    want_uncomressed = [
        # Root switch
        "SwitchName=slurm-root Switches=ns_blue,ns_green,ns_pink,ns_violet,w,zone_nm",
        # Block of "phony" topology: root->nodeset->node
        "SwitchName=ns_blue Nodes=m22-blue-[0-6]",
        "SwitchName=ns_green Nodes=m22-green-[0-4]",
        "SwitchName=ns_pink Nodes=m22-pink-[0-3]",
        "SwitchName=ns_violet Nodes=m22-violet-[1,3-4]",
        # Block of "real" topology:  root->...4 pieces of physicalHost...->node
        # m22-crimson-fhtagn uses 4-pieces physicalHost w/x/y/z, no need for padding
        "SwitchName=w Switches=x",
        "SwitchName=x Switches=y",
        "SwitchName=y Switches=z",
        "SwitchName=z Nodes=m22-crimson-fhtagn",
        # m22-violet-0 doesn't have physicalHost so create 3-fake switches
        # m22-violet-2 has 3-pieces physicalHost /x/y/z, add padding zone to it
        "SwitchName=zone_nm Switches=m22-violet-0_pad2,x",
        "SwitchName=m22-violet-0_pad2 Switches=m22-violet-0_pad1",
        "SwitchName=m22-violet-0_pad1 Switches=m22-violet-0_pad0",
        "SwitchName=m22-violet-0_pad0 Nodes=m22-violet-0",
        "SwitchName=x Switches=y",
        "SwitchName=y Switches=z",
        "SwitchName=z Nodes=m22-violet-2",
        # Separate graph for TPUs: tpu_root->nodeset->sub_switch->node
        "SwitchName=tpu-root Switches=ns_bold,ns_slim",
        "SwitchName=ns_bold Switches=ns_bold-[0-3]",
        "SwitchName=ns_bold-0 Nodes=m22-bold-[0-2]",
        "SwitchName=ns_bold-1 Nodes=m22-bold-3",
        "SwitchName=ns_bold-2 Nodes=m22-bold-[4-6]",
        "SwitchName=ns_bold-3 Nodes=m22-bold-[7-8]",
        "SwitchName=ns_slim Nodes=m22-slim-[0-2]",
    ]
    assert list(uncompressed.render_conf_lines()) == want_uncomressed

    want_compressed = [
        "SwitchName=s0 Switches=s0_[0-5]",
        "SwitchName=s0_0 Nodes=m22-blue-[0-6]",
        "SwitchName=s0_1 Nodes=m22-green-[0-4]",
        "SwitchName=s0_2 Nodes=m22-pink-[0-3]",
        "SwitchName=s0_3 Nodes=m22-violet-[1,3-4]",
        "SwitchName=s0_4 Switches=s0_4_0",
        "SwitchName=s0_4_0 Switches=s0_4_0_0",
        "SwitchName=s0_4_0_0 Switches=s0_4_0_0_0",
        "SwitchName=s0_4_0_0_0 Nodes=m22-crimson-fhtagn",
        "SwitchName=s0_5 Switches=s0_5_[0-1]",
        "SwitchName=s0_5_0 Switches=s0_5_0_0",
        "SwitchName=s0_5_0_0 Switches=s0_5_0_0_0",
        "SwitchName=s0_5_0_0_0 Nodes=m22-violet-0",
        "SwitchName=s0_5_1 Switches=s0_5_1_0",
        "SwitchName=s0_5_1_0 Switches=s0_5_1_0_0",
        "SwitchName=s0_5_1_0_0 Nodes=m22-violet-2",
        "SwitchName=s1 Switches=s1_[0-1]",
        "SwitchName=s1_0 Switches=s1_0_[0-3]",
        "SwitchName=s1_0_0 Nodes=m22-bold-[0-2]",
        "SwitchName=s1_0_1 Nodes=m22-bold-3",
        "SwitchName=s1_0_2 Nodes=m22-bold-[4-6]",
        "SwitchName=s1_0_3 Nodes=m22-bold-[7-8]",
        "SwitchName=s1_1 Nodes=m22-slim-[0-2]",
    ]
    compressed = uncompressed.compress()
    assert list(compressed.render_conf_lines()) == want_compressed
