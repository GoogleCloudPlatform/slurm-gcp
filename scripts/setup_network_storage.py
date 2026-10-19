#!/usr/bin/env python3

# Copyright (C) SchedMD LLC.
# Copyright 2026 Google LLC
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

from typing import Iterable, Optional, Set, Union

import os
import posixpath
import random
import socket
import sys
import stat
import time
import logging
import shutil
from pathlib import Path
from concurrent.futures import as_completed
from addict import Dict as NSDict

import util
from util import lkp, run, cfg, dirs, separate


def mounts_by_local(mounts):
    """convert list of mounts to dict of mounts, local_mount as key"""
    return {str(Path(m.local_mount).resolve()): m for m in mounts}


def resolve_network_storage(nodeset=None):
    """Combine appropriate network_storage fields to a single list"""

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
        mount_addr = util.host_lookup(server_ip)
        return mount_addr == lkp.control_host_addr

    return separate(internal_mount, mounts)


def _is_controller_nfs_server(server: str, lookup_obj=None) -> bool:
    """Check if an NFS server string refers to the controller without retrying DNS lookups."""
    if not server:
        return False
    t = lookup_obj if lookup_obj is not None else lkp
    srv_fn = getattr(t, "controller_mount_server_ip", None)
    hosts = (getattr(t, "control_host", None), getattr(cfg, "slurm_control_host", None))
    addrs = (getattr(t, "control_addr", None), getattr(cfg, "slurm_control_addr", None))
    for cand in (srv_fn() if callable(srv_fn) else None, *hosts, *addrs):
        if isinstance(cand, str) and cand and server == cand:
            return True
    try:
        server_ip = socket.gethostbyname(server)
    except OSError:
        return False
    if any(isinstance(a, str) and a and server_ip == a for a in addrs):
        return True
    host = next((h for h in hosts if isinstance(h, str) and h), None)
    try:
        return bool(host and server_ip == socket.gethostbyname(host))
    except OSError:
        return False


def _probe_tcp_port(host: str, port: int = 2049, timeout: float = 1.0) -> bool:
    """Check if TCP port is accepting connections (pure Python, IPv4/IPv6 compatible)."""
    if not host or not host.strip():
        return False
    try:
        with socket.create_connection((host.strip(), port), timeout=timeout):
            return True
    except (OSError, TypeError, OverflowError):
        return False


def _is_path_exported(expected_path: str, active_exports: Set[str]) -> bool:
    """Check if expected_path matches an active export or is a subdirectory of one."""
    if not expected_path or not expected_path.startswith("/"):
        return False
    norm_p = posixpath.normpath("/" + expected_path.lstrip("/"))
    for exp in active_exports:
        if exp and exp.startswith("/"):
            norm_e = posixpath.normpath("/" + exp.lstrip("/"))
            if norm_p == norm_e or norm_p.startswith(norm_e.rstrip("/") + "/"):
                return True
    return False


def _check_nfs_exports_showmount(
    server: str,
    expected_paths: Set[str],
    timeout: float = 3.0,
    log: Optional[logging.Logger] = None,
) -> Optional[bool]:
    """Verify exports via showmount -e if available and port 111 is reachable."""
    logger = log or logging.getLogger(__name__)
    path_env = f"/usr/sbin:/sbin:{os.environ.get('PATH', '')}"
    bin_path = shutil.which("showmount", path=path_env)
    if not bin_path or not _probe_tcp_port(server, port=111, timeout=0.5):
        return None
    env = {**os.environ, "LC_ALL": "C", "PATH": path_env}
    try:
        res = run(
            [bin_path, "--no-headers", "-e", server],
            timeout=timeout,
            check=False,
            env=env,
        )
        if res.returncode != 0:
            return None
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        exports = {
            posixpath.normpath("/" + ln.split()[0].lstrip("/"))
            for ln in lines
            if not ln.startswith("Export") and ln.split()[0].startswith("/")
        }
        missing = {p for p in expected_paths if not _is_path_exported(p, exports)}
        if not missing:
            logger.info(
                f"showmount confirmed exports {sorted(expected_paths)} on {server}."
            )
            return True
        logger.debug(f"showmount on {server} missing shares: {sorted(missing)}")
        return False
    except Exception as e:
        logger.debug(f"showmount query on {server} failed: {e}")
        return None


def wait_for_controller_nfs(
    server: str,
    expected_paths: Iterable[Union[str, Path]],
    timeout: int = 600,
    log: Optional[logging.Logger] = None,
) -> bool:
    """Wait for controller NFS server to accept TCP 2049 and export expected paths."""
    logger = log or logging.getLogger(__name__)
    if timeout <= 0:
        logger.warning(f"Invalid timeout {timeout}s waiting for '{server}'; skipping.")
        return False

    norm = {
        posixpath.normpath("/" + str(p).lstrip("/")) for p in expected_paths if str(p)
    }
    logger.info(f"Waiting up to {timeout}s for '{server}' to export {sorted(norm)}...")
    deadline = time.monotonic() + timeout
    port_open = False

    for wait in util.backoff_delay(min(1.5, float(timeout)), timeout=timeout):
        if not port_open:
            port_open = _probe_tcp_port(server, port=2049, timeout=1.0)
            if port_open:
                logger.info(f"NFS TCP port 2049 is open on {server}.")
        if port_open:
            if not norm or _check_nfs_exports_showmount(
                server, norm, timeout=3.0, log=logger
            ) in (True, None):
                logger.info(f"Controller NFS exports confirmed ready on {server}.")
                time.sleep(0.5)
                return True
        if time.monotonic() >= deadline:
            break
        rem = max(0.0, deadline - time.monotonic())
        time.sleep(min(wait * random.uniform(0.8, 1.2), rem))

    msg = (
        f"NFS port 2049 on {server}"
        if not port_open
        else f"'{server}' to export {sorted(norm)}"
    )
    logger.warning(
        f"Timed out after {timeout}s waiting for {msg}. Proceeding to mount retries."
    )
    return False


def setup_network_storage(log):
    """prepare network fs mounts and add them to fstab"""
    log.info("Set up network storage")
    # filter mounts into two dicts, cluster-internal and external mounts

    all_mounts = resolve_network_storage()
    if lkp.instance_role == "controller":
        mounts, _ = separate_external_internal_mounts(all_mounts)
    else:
        mounts = all_mounts

    if lkp.instance_role != "controller":
        controller_mounts: dict[str, set[str]] = {}
        for m in mounts:
            if (m.fs_type or "").lower() == "nfs" and m.server_ip and m.remote_mount:
                server = str(m.server_ip).split("@")[0]
                if _is_controller_nfs_server(server, lkp):
                    controller_mounts.setdefault(server, set()).add(str(m.remote_mount))

        if cfg.munge_mount and (cfg.munge_mount.fs_type or "nfs").lower() == "nfs":
            m_srv = (
                cfg.munge_mount.server_ip
                or cfg.slurm_control_addr
                or cfg.slurm_control_host
            )
            srv = str(m_srv).split("@")[0] if m_srv else ""
            if srv and _is_controller_nfs_server(srv, lkp):
                m_path = str(cfg.munge_mount.remote_mount or "/etc/munge")
                controller_mounts.setdefault(srv, set()).add(m_path)

        key = "controller_startup_scripts_timeout"
        raw_t = cfg.get(key) if isinstance(cfg, dict) else getattr(cfg, key, None)
        valid_t = (
            isinstance(raw_t, (int, float))
            and not isinstance(raw_t, bool)
            and raw_t > 0
        )
        nfs_timeout = max(600, (int(raw_t) if valid_t else 300) + 300)
        for server, paths in controller_mounts.items():
            wait_for_controller_nfs(server, sorted(paths), timeout=nfs_timeout, log=log)

    # Determine fstab entries and write them out
    fstab_entries = []
    for mount in mounts:
        local_mount = Path(mount.local_mount)
        remote_mount = mount.remote_mount
        fs_type = mount.fs_type
        server_ip = mount.server_ip or ""
        util.mkdirp(local_mount)

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

    fstab = Path("/etc/fstab")
    if not Path(fstab.with_suffix(".bak")).is_file():
        shutil.copy2(fstab, fstab.with_suffix(".bak"))
    shutil.copy2(fstab.with_suffix(".bak"), fstab)
    with open(fstab, "a") as f:
        f.write("\n")
        for entry in fstab_entries:
            f.write(entry)
            f.write("\n")

    mount_fstab(mounts_by_local(mounts), log)
    munge_mount_handler(log)


def mount_fstab(mounts, log):
    """Wait on each mount, then make sure all fstab is mounted"""
    from more_executors import Executors, ExceptionRetryPolicy

    def mount_path(path):
        log.info(f"Waiting for '{path}' to be mounted...")
        try:
            run(f"mount {path}", timeout=120)
        except Exception as e:
            exc_type, _, _ = sys.exc_info()
            log.error(f"mount of path '{path}' failed: {exc_type}: {e}")
            raise e
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
    local_mount.mkdir()
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
    for retry, wait in enumerate(util.backoff_delay(0.5, timeout), 1):
        try:
            run(cmd, timeout=timeout)
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
    shutil.copy2(Path(local_mount / "munge.key"), munge_key)

    log.info("Restrict permissions of munge.key")
    shutil.chown(munge_key, user="munge", group="munge")
    os.chmod(munge_key, stat.S_IRUSR)

    log.info(f"Unmount {local_mount}")
    if fs_type.lower() == "gcsfuse".lower():
        run(f"fusermount -u {local_mount}", timeout=120)
    else:
        run(f"umount {local_mount}", timeout=120)
    shutil.rmtree(local_mount)


def setup_nfs_exports():
    """nfs export all needed directories"""
    # The controller only needs to set up exports for cluster-internal mounts
    # switch the key to remote mount path since that is what needs exporting
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
        util.mkdirp(Path(path))
        run(rf"sed -i '\#{path}#d' /etc/exports", timeout=30)
        exports.append(f"{path}  *(rw,no_subtree_check,no_root_squash)")

    exportsd = Path("/etc/exports.d")
    util.mkdirp(exportsd)
    with (exportsd / "slurm.exports").open("w") as f:
        f.write("\n")
        f.write("\n".join(exports))
    run("exportfs -a", timeout=30)
