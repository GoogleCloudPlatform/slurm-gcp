#!/usr/bin/env python3

# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import subprocess
import uuid
from typing import List, Optional, Dict


def order(pathes: List[List[str]], start_from: str) -> List[str]:
    # build a tree
    class Node:
        def __init__(self, name: str, parent: "Node"):
            self.name = name
            self.parent = parent
            self.children = {}

    root = Node("", None)
    start = None
    for path in pathes:
        assert path, "empty path"
        n = root
        for v in path:
            if v not in n.children:
                n.children[v] = Node(v, n)
            n = n.children[v]

        if path[-1] == start_from:
            start = n
    assert start, f"'{start_from}' not found"
    assert not start.children, f"'{start_from}' is not a node"

    result = []

    def up(v):  #
        if not v.children:  # it's a leaf, i.e. a node
            result.append(v.name)
        p = v.parent
        if not p:
            return
        # traverse all children of parent, except v, in lexicographical order
        for u_name in sorted(p.children.keys()):
            if u_name != v.name:
                down(p.children[u_name])
        up(p)

    def down(v):  # ordinary PostOrderTraversal
        for u_name in sorted(v.children.keys()):
            down(v.children[u_name])
        if not v.children:  # it's a leaf, i.e. a node
            result.append(v.name)

    up(start)
    return result


class Instance:
    def __init__(self, name: str, zone: str, physical_host: Optional[str]):
        self.name = name
        self.zone = zone
        self.physical_host = physical_host


def make_path(node_name: str, inst: Optional[Instance]) -> List[str]:
    if not inst:  # node with uknown instance (e.g. hybrid cluster)
        return ["unknown", node_name]

    # TODO: add region(?)
    zone = f"zone_{inst.zone}"

    if (
        not inst.physical_host
    ):  # node without physical host info (e.g. no placement policy)
        return [zone, "unknown", node_name]

    assert inst.physical_host.startswith(
        "/"
    ), f"Unexpected physicalHost: {inst.physical_host}"
    parts = inst.physical_host[1:].split("/")
    if len(parts) >= 4:
        return [*parts, node_name]
    elif len(parts) == 3:
        return [zone, *parts, node_name]  # add zone

    raise ValueError(f"Unexpected physicalHost: {inst.physical_host}")


def to_hostnames(nodelist: str) -> List[str]:
    cmd = ["scontrol", "show", "hostnames", nodelist]
    out = subprocess.run(cmd, check=True, stdout=subprocess.PIPE).stdout
    return [n.decode("utf-8") for n in out.splitlines()]


def get_instances(node_names: List[str]) -> Dict[str, object]:
    fmt = (
        "--format=csv[no-heading,separator=','](zone,resourceStatus.physicalHost,name)"
    )
    cmd = ["gcloud", "compute", "instances", "list", fmt]

    scp = os.path.commonprefix(node_names)
    if scp:
        cmd.append(f"--filter=name~'{scp}.*'")
    out = subprocess.run(cmd, check=True, stdout=subprocess.PIPE).stdout
    d = {}
    for line in out.splitlines():
        zone, physical_host, name = line.decode("utf-8").split(",")
        d[name] = Instance(name, zone, physical_host)
    return {n: d.get(n) for n in node_names}


def main(args) -> None:
    nodelist = args.nodelist or os.getenv("SLURM_NODELIST")
    if not nodelist:
        raise ValueError("nodelist is not provided and SLURM_NODELIST is not set")
    output = args.output or f"hosts.{uuid.uuid4()}"

    node_names = to_hostnames(nodelist)
    instannces = get_instances(node_names)
    pathes = [make_path(n, instannces[n]) for n in node_names]
    start_from = min(node_names)  # start from the smallest node name
    ordered = order(pathes, start_from=start_from)

    with open(output, "w") as f:
        f.write("\n".join(ordered))
    print(output)


if __name__ == "__main__":
    import argparse

    # !!! doc
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nodelist",
        type=str,
        help="Slurm 'hostlist expression' of nodes to sort, if not provided the value of SLURM_NODELIST environment variable will be used",
    )
    parser.add_argument(
        "--output", type=str, help="Output file to write, defaults to 'hosts.<uuid>'"
    )
    # !!! repeats n_task !!!
    args = parser.parse_args()
    main(args)
