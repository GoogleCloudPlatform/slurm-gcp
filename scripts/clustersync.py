#!/usr/bin/env python3
# Copyright 2021 SchedMD LLC
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

from collections import namedtuple
import logging
import time
import numpy as np
from pathlib import Path
import re
from shutil import copy2, chown
import subprocess
import yaml

import resume
import slurmsync
import setup
import util
from util import dirs, load_config_file, run, save_config


filename = Path(__file__).name
logfile = (Path(util.dirs.log/filename)).with_suffix('.log')
util.config_root_logger(filename, level='DEBUG', util_level='DEBUG',
                        logfile=logfile)
log = logging.getLogger(filename)
setup.log.disabled = False
slurmsync.log.disabled = False
resume.log.disabled = False
util.log.disabled = False

SCONTROL = Path(util.cfg.slurm_cmd_path or '')/'scontrol'
StateTuple = namedtuple('StateTuple', 'base,flags')


def natural_keys(text):
    """ String sorting heuristic function for numbers """
    def atoi(text):
        return int(text) if text.isdigit() else text

    return [atoi(c) for c in re.split(r'(\d+)', text)]


def make_tuple(line):
    """ Turn 'part,states' to (part, StateTuple(state)) """
    # Node State: CLOUD, DOWN, DRAIN, FAIL, FAILING, FUTURE, UNKNOWN
    # Partition States: UP, DOWN, DRAIN, INACTIVE
    item, states = line.split(',')
    state = states.split('+')
    state_tuple = StateTuple(state[0], set(state[1:]))
    return (item, state_tuple)


def get_partitions():
    """ Get partitions and their states """
    cmd = (f"{SCONTROL} show partitions | "
           r"grep -oP '^PartitionName=\K(\S+)|State=\K(\S+)' | "
           r"paste -sd',\n'")
    part_lines = run(cmd, shell=True).stdout.rstrip().splitlines()
    slurm_parts = dict(make_tuple(line) for line in part_lines)
    return slurm_parts


def get_nodes():
    """ Get compute nodes, their states and flags """
    cmd = (f"{SCONTROL} show nodes | grep -oP '^NodeName=\K(\S+)|State=\K(\S+)'"
           r" | paste -sd',\n'")
    node_lines = run(cmd, shell=True).stdout.rstrip().splitlines()
    slurm_nodes = dict(
        node_tuple for line in node_lines
        if 'CLOUD' in (node_tuple := make_tuple(line))[1].flags
    )
    return slurm_nodes


def update_partitions(partitions, state):
    log.info(f"Updating partitions states to 'State={state}'.")
    for part, part_state in partitions.items():
        log.debug(f"Current: PartitionName={part} State={part_state}")
        run(f"{SCONTROL} update PartitionName={part} State={state}")


def update_nodes(hostlist, state, reason=None):
    log.debug(f"Updating 'NodeName={hostlist}' to 'State={state}'.")
    cmd = f"{SCONTROL} update NodeName={hostlist} State={state}"
    if reason is not None:
        cmd += f" Reason='{reason}'"
    run(cmd)


def diff_plan(lkp0, lkp1):
    """ Generate a diff plan for cluster """
    plan = {
        'add': [],
        'notify': [],
        'remove': [],
        'suspend': [],
    }

    template_map_file = dirs.scripts/'.template_map.yaml'
    template_map_file_bak = dirs.scripts/'.template_map.yaml.bak'
    if template_map_file_bak.is_file() and not template_map_file.is_file():
        log.info(f"Restoring '{template_map_file}' from backup")
        copy2(template_map_file_bak, template_map_file)
    elif (dirs.scripts/'.template_map').is_file():
        copy2(template_map_file, template_map_file_bak)
    template_map0 = load_config_file(template_map_file) or lkp0.template_map

    nodelist0 = []
    for template in lkp0.template_nodes:
        for node in lkp0.template_nodes[template]:
            static, dynamic = setup.nodenames(node)
            nodelist0.extend(resume.expand_nodelist(static))
            nodelist0.extend(resume.expand_nodelist(dynamic))

    nodelist1 = []
    nodelist2 = []
    for template in lkp1.template_nodes:
        for node in lkp1.template_nodes[template]:
            static, dynamic = setup.nodenames(node)
            static_nodelist = resume.expand_nodelist(static)
            dynamic_nodelist = resume.expand_nodelist(dynamic)

            if node['template_details']['url'] != template_map0[template]:
                nodelist2.extend(static_nodelist)
                nodelist2.extend(dynamic_nodelist)

            nodelist1.extend(static_nodelist)
            nodelist1.extend(dynamic_nodelist)

    plan['add'].extend(np.setdiff1d(nodelist1, nodelist0))
    plan['notify'].extend(np.intersect1d(nodelist0, nodelist1))
    plan['remove'].extend(np.setdiff1d(nodelist0, nodelist1))
    plan['suspend'].extend(np.intersect1d(nodelist0, nodelist2))

    return plan


def diff_apply(plan):
    """ Apply diff plan to cluster """
    delta = False

    for action, nodelist in plan.items():
        nodelist.sort(key=natural_keys)

        if action == 'add':
            # These nodes will be added by regenerating the slurm.conf.
            # Nothing to do here but notify.
            if len(nodelist):
                delta = True
                hostlist = slurmsync.to_hostlist(nodelist)
                log.info(f"NodeName={hostlist} will be added to cluster.")
        elif action == 'notify':
            # These nodes have not changed in slurm.conf.
            # Nothing to do here but notify.
            if len(nodelist):
                delta = True
                hostlist = slurmsync.to_hostlist(nodelist)
                log.info(
                    f"NodeName={hostlist} will remain present in cluster.")
        elif action == 'remove':
            # These nodes will be removed by regenerating the slurm.conf.
            # They should be downed and suspended otherwise slurmsync should
            #   detect them as orphans.
            if len(nodelist):
                delta = True
                hostlist = slurmsync.to_hostlist(nodelist)
                log.info(
                    f"NodeName={hostlist} will be excised from cluster and destroyed.")
                update_nodes(hostlist, 'POWER_DOWN_FORCE',
                             "clustersync: node excised from cluster")
                slurmsync.delete_instances(hostlist)
        elif action == 'suspend':
            # These nodes should be destroyed because the immutable template
            #   resource has been replaced, hence hardware specs have changed.
            if len(nodelist):
                delta = True
                hostlist = slurmsync.to_hostlist(nodelist)
                log.info(
                    f"NodeName={hostlist} template resource has changed. Nodes using it must be destroyed.")
                update_nodes(hostlist, 'POWER_DOWN_FORCE',
                             "clustersync: node template resource changed")
                slurmsync.delete_instances(hostlist)
        else:
            log.error(f"Unhandled action '{action}'.")

    return delta


def update_conf():
    # Generate new config files
    setup.gen_cloud_nodes_conf(util.lkp)
    setup.install_slurm_conf(util.lkp)
    setup.install_slurmdbd_conf(util.lkp)
    setup.install_gres_conf(util.lkp)

    # Save conf changes
    save_config(util.cfg, dirs.scripts/'config.yaml')
    chown(dirs.scripts/'config.yaml', user='slurm', group='slurm')
    save_config(util.lkp.template_map, dirs.scripts/'.template_map.yaml')
    chown(dirs.scripts/'.template_map.yaml', user='slurm', group='slurm')


def restart_cluster():
    """ Restart daemons to use new slurm.conf """
    log.info("Sleeping for 30 seconds to allow slurm to process RPCs")
    time.sleep(30)
    run("systemctl restart slurmdbd")
    time.sleep(5)  # Allow time for slurmdbd to start
    run("systemctl restart slurmctld")
    time.sleep(5)  # Allow time for slurmctld to start
    run("serf event -coalesce=false 'restart-slurmd'")


def main():
    if util.lkp.instance_role != 'controller':
        log.error("Only the controller can run this script. Aborting...")
        return

    log.info("Updating cluster development scripts")
    run("serf event -coalesce=false 'update-scripts'")

    lkp0 = util.lkp

    config_yaml = yaml.safe_load(util.instance_metadata('attributes/config'))
    util.cfg = util.new_config(config_yaml)
    util.lkp = util.Lookup(util.cfg)

    plan = diff_plan(lkp0, util.lkp)
    delta = diff_apply(plan)

    if delta:
        log.info("Cluster configuration has changed. Applying updates.")
        # Temporarily stop scheduling
        partitions = get_partitions()
        update_partitions(partitions, 'INACTIVE')

        update_conf()
        restart_cluster()
    else:
        log.info("Cluster configuration has not changed. Nothing to update.")


if __name__ == '__main__':
    try:
        main()
    except subprocess.TimeoutExpired as e:
        log.error(f"""TimeoutExpired:
        command={e.cmd}
        timeout={e.timeout}
        stdout:
    {e.stdout.strip()}
        stderr:
    {e.stderr.strip()}
    """)
        log.exception(e)
    except subprocess.CalledProcessError as e:
        log.error(f"""CalledProcessError:
    command={e.cmd}
    returncode={e.returncode}
    stdout:
{e.stdout.strip()}
    stderr:
{e.stderr.strip()}
""")
        log.exception(e)
    except Exception as e:
        log.exception(e)
