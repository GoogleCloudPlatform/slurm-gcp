#!/usr/bin/env python3
# Copyright (C) SchedMD LLC.
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

import argparse
import logging
from suspend import batch_execute, truncate_iter, wait_for_operations
from util import lkp, compute, parse_self_link

log = logging.getLogger()


def delete_placement_groups(project, region, resourcePolicy):
    request = compute.resourcePolicies().delete(
        project=project, region=region, resourcePolicy=resourcePolicy
    )
    return request


def delete_policies(policy_list):
    log.info(
        "Deleting {0} resource policies:\n{1}".format(
            len(policy_list), "\n".join(policy_list)
        )
    )

    ops = {}
    for self_link in policy_list:
        link_info = parse_self_link(self_link)
        ops[self_link] = delete_placement_groups(
            project=link_info.project,
            region=link_info.region,
            resourcePolicy=link_info.resourcePolicie,
        )
    done, failed = batch_execute(ops)
    if failed:
        failed_items = [f"{n}: {e}" for n, (_, e) in failed.items()]
        items_str = "\n".join(str(el) for el in truncate_iter(failed_items, 5))
        log.error(f"some policies failed to delete: {items_str}")
    wait_for_operations(done.values())


def main(args):
    # NOTE: Resource policies cannot be labeled
    if args.partition_name:
        filter = f"name={args.slurm_cluster_name}-{args.partition_name}-*"
    else:
        filter = f"name={args.slurm_cluster_name}-*"
    log.debug(f'filter = "{filter}"')
    p_id = args.project_id if args.project_id else lkp.project
    if not p_id:
        print("Error: Project id cannot be determined")
        exit(1)
    result = (
        compute.resourcePolicies().aggregatedList(project=p_id, filter=filter).execute()
    )

    policy_list = []
    for item in result["items"].values():
        policies = item.get("resourcePolicies")
        if policies is not None:
            for policy in policies:
                policy_list.append(policy["selfLink"])

    delete_policies(policy_list)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("slurm_cluster_name", help="Slurm cluster name filter")
    parser.add_argument(
        "--partition", "-p", dest="partition_name", help="Slurm partition name filter"
    )
    parser.add_argument(
        "--project_id", help="Google cloud project ID", type=str, default=None
    )
    main(parser.parse_args())
