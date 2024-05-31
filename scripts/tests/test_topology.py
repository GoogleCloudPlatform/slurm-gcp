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
def test_gen_topology_conf(tpu_mock):
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
        output_dir=tempfile.mkdtemp(),
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

    conf.gen_topology_conf(lkp)

    want = "\n".join(
        [
            # Pre-amble
            "",
            "# Warning:",
            "# This file is managed by a script. Manual modifications will be overwritten.",
            "",
            # Root switch
            "SwitchName=slurm-root Switches=ns_blue,ns_green,ns_pink,ns_violet,region_us",
            # Block of "phony" topology: root->nodeset->node
            "SwitchName=ns_blue Nodes=m22-blue-[0-6]",
            "SwitchName=ns_green Nodes=m22-green-[0-4]",
            "SwitchName=ns_pink Nodes=m22-pink-[0-3]",
            "SwitchName=ns_violet Nodes=m22-violet-[1,3-4]",
            # Block of "real" topology:  root->region->...4 pieces of physicalHost...->node
            "SwitchName=region_us Switches=w,zone_nm",  # no zone_wy sincel only 4-part nodes are in wy
            # m22-crimson-fhtagn uses 4-pieces physicalHost w/x/y/z, no need for padding
            "SwitchName=w Switches=w_x",
            "SwitchName=w_x Switches=w_x_y",
            "SwitchName=w_x_y Switches=w_x_y_z",
            "SwitchName=w_x_y_z Nodes=m22-crimson-fhtagn",
            # m22-violet-0 doesn't have physicalHost so create 3-fake switches
            # m22-violet-2 has 3-pieces physicalHost /x/y/z, add padding zone to it
            "SwitchName=zone_nm Switches=m22-violet-0_pad2,x",
            "SwitchName=m22-violet-0_pad2 Switches=m22-violet-0_pad1",
            "SwitchName=m22-violet-0_pad1 Switches=m22-violet-0_pad0",
            "SwitchName=m22-violet-0_pad0 Nodes=m22-violet-0",
            "SwitchName=x Switches=x_y",
            "SwitchName=x_y Switches=x_y_z",
            "SwitchName=x_y_z Nodes=m22-violet-2",
            # Separate graph for TPUs: tpu_root->nodeset->sub_switch->node
            "SwitchName=nodeset_tpu-root Switches=ns_bold,ns_slim",
            "SwitchName=ns_bold Switches=ns_bold-[0-3]",
            "SwitchName=ns_bold-0 Nodes=m22-bold-[0-2]",
            "SwitchName=ns_bold-1 Nodes=m22-bold-3",
            "SwitchName=ns_bold-2 Nodes=m22-bold-[4-6]",
            "SwitchName=ns_bold-3 Nodes=m22-bold-[7-8]",
            "SwitchName=ns_slim Nodes=m22-slim-[0-2]",
            "",
            "",
        ]
    )
    assert open(cfg.output_dir + "/cloud_topology.conf").read() == want
