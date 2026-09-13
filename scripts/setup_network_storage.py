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
import tempfile
from pathlib import Path
from concurrent.futures import as_completed
from addict import Dict as NSDict

import util
from util import lkp, run, cfg, dirs, separate

log = logging.getLogger(__name__)


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
        server_ip = (mount.server_ip or "").split("@")[0]
        if not server_ip:
            return lkp.instance_role == "controller"
        if lkp.control_host is not None and server_ip == lkp.control_host:
            return True
        if lkp.control_host_addr is not None and server_ip == lkp.control_host_addr:
            return True
        if lkp.control_addr is not None and server_ip == lkp.control_addr:
            return True
        try:
            mount_addr = util.host_lookup(server_ip)
        except Exception:
            mount_addr = None
        return mount_addr == lkp.control_host_addr

    return separate(internal_mount, mounts)


def _probe_tcp_port(host: str, port: int = 2049, timeout: float = 1.0) -> bool:
    """Check if TCP port is accepting connections (pure Python, IPv4/IPv6 compatible)."""
    if not host or not host.strip():
        return False
    try:
        with socket.create_connection((host.strip(), port), timeout=timeout):
            return True
    except (OSError, TypeError, OverflowError):
        return False


def _find_showmount() -> Optional[str]:
    """Find showmount binary in PATH or standard system binary paths."""
    bin_path = shutil.which("showmount")
    if bin_path:
        return bin_path
    for candidate in ("/usr/sbin/showmount", "/sbin/showmount", "/usr/bin/showmount"):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _check_nfs_exports_showmount(
    server: str,
    expected_paths: Set[str],
    timeout: float = 3.0,
    log: Optional[logging.Logger] = None,
) -> Optional[bool]:
    """Verify exports via showmount -e if available and port 111 is reachable.

    Returns:
        True: All expected paths are confirmed exported by the server.
        False: Port 111 is reachable, but expected paths are not yet exported.
        None: showmount unavailable, port 111 blocked/closed, or RPC query failed (trigger fallback).
    """
    logger = log or logging.getLogger(__name__)
    showmount_bin = _find_showmount()
    if not showmount_bin:
        logger.debug("showmount binary not found; bypassing Tier 1 probe.")
        return None

    # Guard: probe port 111 with a strict 0.5s timeout. If firewalled or closed, NEVER call showmount.
    if not _probe_tcp_port(server, port=111, timeout=0.5):
        logger.debug(
            f"Port 111 unreachable on {server}; firewall blocks RPC or rpcbind disabled."
        )
        return None

    env = os.environ.copy()
    env["LC_ALL"] = "C"
    if "/usr/sbin" not in env.get("PATH", ""):
        env["PATH"] = f"/usr/sbin:/sbin:{env.get('PATH', '')}"

    try:
        res = run(
            [showmount_bin, "--no-headers", "-e", server],
            timeout=timeout,
            check=False,
            env=env,
        )
        if res.returncode != 0:
            logger.debug(
                f"showmount -e {server} returned exit code {res.returncode}: {res.stderr}"
            )
            return None

        active_exports: Set[str] = set()
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("Export list"):
                continue
            parts = line.split()
            if parts:
                active_exports.add(posixpath.normpath(parts[0]))

        if expected_paths.issubset(active_exports):
            logger.info(
                f"showmount confirmed exports {sorted(expected_paths)} on {server}."
            )
            return True
        else:
            missing = expected_paths - active_exports
            logger.debug(
                f"showmount exports on {server} missing expected shares: {sorted(missing)}"
            )
            return False

    except Exception as e:
        logger.debug(f"showmount query on {server} failed with exception: {e}")
        return None


def _probe_nfs_mount(
    server: str,
    remote_path: str,
    timeout: float = 5.0,
    log: Optional[logging.Logger] = None,
) -> bool:
    """Probe kernel NFS export readiness via a transient non-destructive mount.

    Fallback for environments where port 111 is firewalled or showmount is absent.
    """
    logger = log or logging.getLogger(__name__)
    try:
        probe_base = Path("/run/slurm")
        probe_base.mkdir(parents=True, exist_ok=True)
    except Exception:
        probe_base = Path(tempfile.gettempdir())

    probe_dir = Path(tempfile.mkdtemp(prefix=".nfs_probe_", dir=str(probe_base)))

    cmd = [
        "mount",
        "-t",
        "nfs",
        "-o",
        "ro,soft,timeo=20,retrans=1,retry=0",
        f"{server}:{remote_path}",
        str(probe_dir),
    ]
    try:
        res = run(cmd, timeout=timeout, check=False)
        if res.returncode == 0:
            logger.info(f"Transient probe mount succeeded for {server}:{remote_path}.")
            return True
        else:
            logger.debug(
                f"Probe mount {server}:{remote_path} returned {res.returncode}: {res.stderr}"
            )
            return False
    except Exception as e:
        logger.debug(f"Probe mount {server}:{remote_path} exception: {e}")
        return False
    finally:
        try:
            if probe_dir.is_mount():
                run(f"umount -l {probe_dir}", timeout=10, check=False)
            if probe_dir.is_dir():
                probe_dir.rmdir()
        except Exception:
            pass


def wait_for_controller_nfs(
    server: str,
    expected_paths: Iterable[Union[str, Path]],
    timeout: int = 360,
    log: Optional[logging.Logger] = None,
) -> None:
    """Wait for controller NFS server to export all expected paths.

    Employs a multi-tier probe strategy:
      - Tier 0: Pure-Python TCP 2049 probe with backoff.
      - Tier 1: Port 111 pre-checked showmount -e structured query.
      - Tier 2: Transient probe mount fallback (handles firewalled port 111 and NFSv4-only).
      - Anti-thundering-herd jitter on all polling intervals.
    """
    logger = log or logging.getLogger(__name__)
    if timeout <= 0:
        raise TimeoutError(
            f"Invalid timeout {timeout}s waiting for controller NFS server '{server}'."
        )

    normalized_expected = {posixpath.normpath(str(p)) for p in expected_paths}
    logger.info(
        f"Waiting up to {timeout}s for controller NFS server '{server}' "
        f"to export {sorted(normalized_expected)}..."
    )

    deadline = time.monotonic() + timeout
    start_delay = min(1.5, float(timeout))
    sample_path = sorted(normalized_expected)[0] if normalized_expected else None

    # Step 1: Wait for TCP 2049 readiness (Tier 0)
    port_2049_open = False
    for wait in util.backoff_delay(start_delay, timeout=timeout):
        if _probe_tcp_port(server, port=2049, timeout=1.0):
            port_2049_open = True
            logger.info(f"NFS TCP port 2049 is open on {server}.")
            break
        if time.monotonic() >= deadline:
            break
        sleep_sec = min(
            wait * random.uniform(0.8, 1.2), max(0.0, deadline - time.monotonic())
        )
        time.sleep(sleep_sec)

    if not port_2049_open:
        raise TimeoutError(
            f"Timed out after {timeout}s waiting for NFS port 2049 on {server}. "
            "Controller setup failed or NFS service is down."
        )

    if not normalized_expected:
        time.sleep(0.5)
        return

    # Step 2: Actively verify exports (Tier 1 showmount -> Tier 2 transient probe)
    exports_ready = False
    remaining_timeout = deadline - time.monotonic()
    if remaining_timeout > 0.05:
        step2_start = min(start_delay, remaining_timeout)
        for wait in util.backoff_delay(step2_start, timeout=remaining_timeout):
            # Tier 1: showmount probe
            showmount_res = _check_nfs_exports_showmount(
                server, normalized_expected, timeout=3.0, log=logger
            )
            if showmount_res is True:
                exports_ready = True
                break
            elif showmount_res is False:
                # Server is running and reachable on RPC, but exportfs -ra hasn't exported these shares yet
                pass
            else:
                # Tier 2 Fallback: Port 111 firewalled, showmount missing, or NFSv4-only
                if sample_path and _probe_nfs_mount(
                    server, sample_path, timeout=4.0, log=logger
                ):
                    exports_ready = True
                    break

            if time.monotonic() >= deadline:
                break

            sleep_sec = min(
                wait * random.uniform(0.8, 1.2), max(0.0, deadline - time.monotonic())
            )
            time.sleep(sleep_sec)

    if not exports_ready:
        raise TimeoutError(
            f"Timed out after {timeout}s waiting for controller '{server}' to export "
            f"{sorted(normalized_expected)}. Controller setup.py likely failed."
        )

    logger.info(f"Controller NFS exports confirmed ready on {server}.")
    # Brief 0.5s settle window for kernel filehandle propagation
    time.sleep(0.5)


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

    # Pre-flight check on client nodes: wait for controller NFS exports to be ready
    if lkp.instance_role != "controller":
        controller_mounts_by_server: dict[str, set[str]] = {}
        for m in int_mounts:
            if m.fs_type == "nfs" and m.server_ip and m.remote_mount:
                server = m.server_ip.split("@")[0]
                controller_mounts_by_server.setdefault(server, set()).add(
                    str(m.remote_mount)
                )

        if cfg.munge_mount and (cfg.munge_mount.fs_type or "nfs").lower() == "nfs":
            munge_server = (
                cfg.munge_mount.server_ip
                or cfg.slurm_control_addr
                or cfg.slurm_control_host
            )
            if munge_server:
                munge_server = munge_server.split("@")[0]
                if (
                    munge_server
                    in (
                        lkp.control_host,
                        lkp.control_host_addr,
                        lkp.control_addr,
                    )
                    or not controller_mounts_by_server
                ):
                    munge_remote = str(cfg.munge_mount.remote_mount or "/etc/munge")
                    controller_mounts_by_server.setdefault(munge_server, set()).add(
                        munge_remote
                    )

        for server, paths in controller_mounts_by_server.items():
            wait_for_controller_nfs(server, sorted(paths), log=log)

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
