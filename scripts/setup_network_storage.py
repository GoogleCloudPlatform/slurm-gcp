#!/usr/bin/env python3

# Copyright (C) SchedMD LLC.
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
import time
import subprocess

from pathlib import Path
from concurrent.futures import as_completed
from addict import Dict as NSDict

from util import (
    lkp,
    dirs,
    separate,
    host_lookup,
    load_config_file,
    backoff_delay,
)


def call_update_file(path, data, log=None):
    """Calls the func_update_file Bash function to update a file using Ansible.

    Args:
        path: The path to the file to update.
        data: The line to add to the file.
        log: A logging object to record messages.
    """
    try:
        if os.geteuid() == 0:  # If run by root.
            subprocess.run(
                f"""/slurm/scripts/network_wrapper.sh update_file '{path}' '{data}'""",
                shell=True,
                check=True,  # Raise an exception if the command fails
                text=True,  # Capture stdout/stderr as text
            )
        else:  # If run by non-root user
            subprocess.run(
                f"""sudo /slurm/scripts/network_wrapper.sh update_file '{path}' '{data}'""",
                shell=True,
                check=True,  # Raise an exception if the command fails
                text=True,  # Capture stdout/stderr as text
            )
        log.info("File updated successfully via Ansible.")
    except Exception as e:
        log.error(f"An error occurred: {e}")


def call_run_cmd(command, *args, timeout=None, log=None):
    """Calls the func_run_cmd Bash function to execute a shell command.

    Args:
        command: The shell command to run.
        *args: Additional arguments for the command.
        log: A logging object to record messages.
    """
    try:
        if os.geteuid() == 0:  # If run by root
            subprocess.run(
                f"/slurm/scripts/network_wrapper.sh run_cmd {command} {' '.join(args)}",
                shell=True,
                check=True,  # Raise an exception if the command fails
                text=True,  # Capture stdout/stderr as text
            )
        else:  # If run by non-root
            subprocess.run(
                f"sudo /slurm/scripts/network_wrapper.sh run_cmd {command} {' '.join(args)}",
                shell=True,
                check=True,  # Raise an exception if the command fails
                timeout=timeout,
                text=True,  # Capture stdout/stderr as text
            )
        log.info(f"Command '{command}' executed successfully.")
    except Exception as e:
        log.error(f"An error occurred: {e}")


def mounts_by_local(mounts):
    """convert list of mounts to dict of mounts, local_mount as key"""
    return {str(Path(m.local_mount).resolve()): m for m in mounts}


def resolve_network_storage(nodeset=None):
    """Combine appropriate network_storage fields to a single list"""
    cfg = load_config_file(Path(__file__).with_name("config.yaml"))
    if lkp.instance_role == "compute":
        try:
            nodeset = lkp.node_nodeset()
        except Exception:
            # External nodename, skip lookup
            nodeset = None

    # seed mounts with the default controller mounts
    if cfg.disable_default_mounts:
        default_mounts = []
    else:
        default_mounts = [
            NSDict(
                {
                    "server_ip": lkp.control_addr or lkp.control_host,
                    "remote_mount": str(path),
                    "local_mount": str(path),
                    "fs_type": "nfs",
                    "mount_options": "defaults,hard,intr",
                }
            )
            for path in (
                dirs.home,
                dirs.apps,
            )
        ]

    # create dict of mounts, local_mount: mount_info
    mounts = mounts_by_local(default_mounts)

    # On non-controller instances, entries in network_storage could overwrite
    # default exports from the controller. Be careful, of course
    mounts.update(mounts_by_local(cfg.network_storage))
    if lkp.instance_role in ("login", "controller"):
        mounts.update(mounts_by_local(cfg.login_network_storage))

    if nodeset is not None:
        mounts.update(mounts_by_local(nodeset.network_storage))
    return list(mounts.values())


def separate_external_internal_mounts(mounts):
    """separate into cluster-external and internal mounts"""

    def internal_mount(mount):
        # NOTE: Valid Lustre server_ip can take the form of '<IP>@tcp'
        server_ip = mount.server_ip.split("@")[0]
        mount_addr = host_lookup(server_ip)
        return mount_addr == lkp.control_host_addr

    return separate(internal_mount, mounts)


def setup_network_storage(log):
    """prepare network fs mounts and add them to fstab"""
    log.info("Set up network storage")

    # filter mounts into two dicts, cluster-internal and external mounts

    all_mounts = resolve_network_storage()
    ext_mounts, int_mounts = separate_external_internal_mounts(all_mounts)

    if lkp.instance_role == "controller":
        mounts = ext_mounts
    else:
        mounts = ext_mounts + int_mounts

    # Determine fstab entries and write them out
    fstab_entries = []
    for mount in mounts:
        local_mount = Path(mount.local_mount)
        remote_mount = mount.remote_mount
        fs_type = mount.fs_type
        server_ip = mount.server_ip or ""
        call_run_cmd("mkdir -p", str(local_mount), log=log)

        log.info(
            "Setting up mount ({}) {}{} to {}".format(
                fs_type,
                server_ip + ":" if fs_type != "gcsfuse" else "",
                remote_mount,
                local_mount,
            )
        )

        mount_options = mount.mount_options.split(",") if mount.mount_options else []
        if not mount_options or "_netdev" not in mount_options:
            mount_options += ["_netdev"]

        if fs_type == "gcsfuse":
            fstab_entries.append(
                "{0}   {1}     {2}     {3}     0 0".format(
                    remote_mount, local_mount, fs_type, ",".join(mount_options)
                )
            )
        else:
            fstab_entries.append(
                "{0}:{1}    {2}     {3}      {4}  0 0".format(
                    server_ip,
                    remote_mount,
                    local_mount,
                    fs_type,
                    ",".join(mount_options),
                )
            )

    # Copy fstab to fstab.bak and use backup as clean copy to re-evaluate mounts.
    fstab = Path("/etc/fstab")
    if not Path(fstab.with_suffix(".bak")).is_file():
        call_run_cmd("cp -p", str(fstab), str(fstab.with_suffix(".bak")), log=log)
    call_run_cmd("cp -p", str(fstab.with_suffix(".bak")), str(fstab), log=log)

    # Update fstab.
    for entry in fstab_entries:
        call_update_file("/etc/fstab", entry, log)

    mount_fstab(mounts_by_local(mounts), log)
    munge_mount_handler(log)


def mount_fstab(mounts, log):
    """Wait on each mount, then make sure all fstab is mounted"""
    from more_executors import Executors, ExceptionRetryPolicy

    def mount_path(path):
        log.info(f"Waiting for '{path}' to be mounted...")
        try:
            call_run_cmd("mount", path, timeout=120, log=log)
        except Exception as e:
            log.error(f"mount of path '{path}' failed: {e}")
            return
        log.info(f"Mount point '{path}' was mounted.")

    MAX_MOUNT_TIMEOUT = 60 * 5
    future_list = []
    retry_policy = ExceptionRetryPolicy(
        max_attempts=40, exponent=1.6, sleep=1.0, max_sleep=16.0
    )
    with Executors.thread_pool().with_timeout(MAX_MOUNT_TIMEOUT).with_retry(
        retry_policy=retry_policy
    ) as exe:
        for path in mounts:
            future = exe.submit(mount_path, path)
            future_list.append(future)

        # Iterate over futures, checking for exceptions
        for future in as_completed(future_list):
            try:
                future.result()
            except Exception as e:
                raise e


def munge_mount_handler(log):
    cfg = load_config_file(Path(__file__).with_name("config.yaml"))
    if not cfg.munge_mount:
        log.error("Missing munge_mount in cfg")
    elif lkp.instance_role == "controller":
        return

    mount = cfg.munge_mount
    server_ip = (
        mount.server_ip
        if mount.server_ip
        else (cfg.slurm_control_addr or cfg.slurm_control_host)
    )
    remote_mount = mount.remote_mount
    local_mount = Path("/mnt/munge")
    fs_type = mount.fs_type if mount.fs_type is not None else "nfs"
    mount_options = (
        mount.mount_options
        if mount.mount_options is not None
        else "defaults,hard,intr,_netdev"
    )

    munge_key = Path(dirs.munge / "munge.key")

    log.info(f"Mounting munge share to: {local_mount}")
    call_run_cmd("mkdir -p", str(local_mount), log=log)
    if fs_type.lower() == "gcsfuse".lower():
        if remote_mount is None:
            remote_mount = ""
        cmd = [
            "gcsfuse",
            f"--only-dir={remote_mount}" if remote_mount != "" else None,
            server_ip,
            str(local_mount),
        ]
    else:
        if remote_mount is None:
            remote_mount = Path("/etc/munge")
        cmd = [
            "mount",
            f"--types={fs_type}",
            f"--options={mount_options}" if mount_options != "" else None,
            f"{server_ip}:{remote_mount}",
            str(local_mount),
        ]
    # wait max 120s for munge mount
    timeout = 120
    for retry, wait in enumerate(backoff_delay(0.5, timeout), 1):
        try:
            call_run_cmd(cmd, timeout=timeout, log=log)
            break
        except Exception as e:
            log.error(
                f"munge mount failed: '{cmd}' {e}, try {retry}, waiting {wait:0.2f}s"
            )
            time.sleep(wait)
            err = e
            continue
    else:
        raise err

    log.info(f"Copy munge.key from: {local_mount}")
    call_run_cmd(
        "cp", "-r", str(Path(local_mount) / "munge.key"), str(munge_key), log=log
    )

    log.info("Restrict permissions of munge.key")
    call_run_cmd("chown -r munge:munge", munge_key, log=log)
    call_run_cmd("chmod", "0400", munge_key, log=log)

    log.info(f"Unmount {local_mount}")
    if fs_type.lower() == "gcsfuse".lower():
        call_run_cmd("fusermount -u", local_mount, timeout=120, log=log)
    else:
        call_run_cmd("umount", local_mount, timeout=120, log=log)
    call_run_cmd("rm -rf", local_mount, log=log)


def setup_nfs_exports(log):
    """nfs export all needed directories"""
    # The controller only needs to set up exports for cluster-internal mounts
    # switch the key to remote mount path since that is what needs exporting
    cfg = load_config_file(Path(__file__).with_name("config.yaml"))
    mounts = resolve_network_storage()
    # manually add munge_mount
    mounts.append(
        NSDict(
            {
                "server_ip": cfg.munge_mount.server_ip,
                "remote_mount": cfg.munge_mount.remote_mount,
                "local_mount": Path(f"{dirs.munge}_tmp"),
                "fs_type": cfg.munge_mount.fs_type,
                "mount_options": cfg.munge_mount.mount_options,
            }
        )
    )
    # controller mounts
    _, con_mounts = separate_external_internal_mounts(mounts)
    con_mounts = {m.remote_mount: m for m in con_mounts}
    for nodeset in cfg.nodeset.values():
        # get internal mounts for each nodeset by calling
        # resolve_network_storage as from a node in each nodeset
        ns_mounts = resolve_network_storage(nodeset=nodeset)
        _, int_mounts = separate_external_internal_mounts(ns_mounts)
        con_mounts.update({m.remote_mount: m for m in int_mounts})

    # export path if corresponding selector boolean is True
    exports = []
    for path in con_mounts:
        call_run_cmd("mkdir -p", path, log=log)
        call_run_cmd("sed", "-i", rf"\#{path}#d", "/etc/exports", timeout=30, log=log)
        exports.append(f"{path}  *(rw,no_subtree_check,no_root_squash)")

    exportsd = Path("/etc/exports.d")
    call_run_cmd("mkdir -p", str(exportsd), log=log)

    for export in exports:
        call_update_file("/etc/exports.d/slurm.exports", export, log)

    call_run_cmd("exportfs -a", timeout=30, log=log)
