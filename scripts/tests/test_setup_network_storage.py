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

from contextlib import contextmanager, ExitStack
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, mock_open, patch

from addict import Dict as NSDict

PARENT_DIR = str(Path(__file__).resolve().parent.parent)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

import setup_network_storage as sns  # noqa: E402
from setup_network_storage import (  # noqa: E402
    _check_nfs_exports_showmount,
    _is_controller_nfs_server,
    _is_path_exported,
    _probe_tcp_port,
    setup_network_storage as run_setup_network_storage,
    wait_for_controller_nfs,
)
import util  # noqa: E402

PO = patch.object


def _mnt(server="10.0.0.1", path="/home", fs_type="nfs"):
    return NSDict(
        server_ip=server, remote_mount=path, local_mount=path, fs_type=fs_type
    )


@contextmanager
def _client_env(mounts, cfg_obj=None, lookup_obj=None):
    log = MagicMock()
    cfg_obj = NSDict(munge_mount=None) if cfg_obj is None else cfg_obj
    lkp = lookup_obj or MagicMock(
        instance_role="login", control_host="test-controller", control_addr="10.0.0.1"
    )
    with ExitStack() as s:
        s.enter_context(PO(sns, "lkp", lkp))
        s.enter_context(PO(sns, "cfg", cfg_obj))
        s.enter_context(PO(sns, "resolve_network_storage", return_value=mounts))
        wait = s.enter_context(PO(sns, "wait_for_controller_nfs", return_value=True))
        s.enter_context(PO(Path, "is_file", return_value=True))
        copy = s.enter_context(PO(shutil, "copy2"))
        fstab = s.enter_context(patch("builtins.open", mock_open()))
        mount = s.enter_context(PO(sns, "mount_fstab"))
        munge = s.enter_context(PO(sns, "munge_mount_handler"))
        s.enter_context(PO(util, "mkdirp"))
        yield log, cfg_obj, wait, mount, copy, fstab, munge


def test_probe_tcp_port():
    with PO(socket, "create_connection") as mock_conn:
        mock_conn.return_value.__enter__.return_value = MagicMock()
        assert _probe_tcp_port("10.0.0.1", port=2049, timeout=1.0) is True
        assert _probe_tcp_port("::1", port=2049, timeout=1.0) is True
    for invalid in ("", "   ", None):
        assert _probe_tcp_port(invalid, port=2049) is False  # type: ignore
    for err in (OSError("refused"), TypeError("bad"), OverflowError("overflow")):
        with PO(socket, "create_connection", side_effect=err):
            assert _probe_tcp_port("10.0.0.1", port=2049) is False


def test_is_path_exported():
    active = {"/home", "/opt/apps"}
    for valid in ("/home", "/home/", "/home/shared", "//home/shared", "/opt/apps/cuda"):
        assert _is_path_exported(valid, active) is True
    for bad in ("/home2", "/home_other", "/opt/apps2", "/scratch", "", "rel/path"):
        assert _is_path_exported(bad, active) is False
    assert _is_path_exported("", {""}) is False
    assert _is_path_exported("/home", {""}) is False
    assert _is_path_exported("/any/nested/path", {"/"}) is True
    assert _is_path_exported("/home", {"//"}) is True


def test_check_nfs_exports_showmount():
    out = "Export list for 10.0.0.1:\n/home *\n/opt/apps 10.0.0.0/16\n"
    ok = MagicMock(returncode=0, stdout=out)
    with PO(shutil, "which", return_value="/sbin/showmount"), PO(
        sns, "_probe_tcp_port", return_value=True
    ), PO(sns, "run", return_value=ok):
        assert _check_nfs_exports_showmount("10.0.0.1", {"/home", "/opt/apps"}) is True
        assert (
            _check_nfs_exports_showmount("10.0.0.1", {"/home/s", "/opt/apps/c"}) is True
        )
        assert _check_nfs_exports_showmount("10.0.0.1", {"/home", "/scratch"}) is False
        assert _check_nfs_exports_showmount("10.0.0.1", {"/home_other"}) is False

    with PO(shutil, "which", return_value=None):
        assert _check_nfs_exports_showmount("10.0.0.1", {"/home"}) is None

    with PO(shutil, "which", return_value="/sbin/showmount"), PO(
        sns, "_probe_tcp_port", return_value=False
    ), PO(sns, "run") as mock_run:
        assert _check_nfs_exports_showmount("10.0.0.1", {"/home"}) is None
        mock_run.assert_not_called()

    for eff in ([MagicMock(returncode=1, stderr="err")], RuntimeError("boom")):
        with PO(shutil, "which", return_value="/sbin/showmount"), PO(
            sns, "_probe_tcp_port", return_value=True
        ), PO(sns, "run", side_effect=eff):
            assert _check_nfs_exports_showmount("10.0.0.1", {"/home"}) is None


def test_wait_for_controller_nfs():
    log = MagicMock()
    assert wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=0, log=log) is False
    assert wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=-5, log=log) is False
    assert log.warning.call_count == 2

    cases: list[tuple[Optional[bool], list[str]]] = [
        (True, ["/home", "/apps"]),
        (None, ["/home"]),
        (True, []),
    ]
    for ret, paths in cases:
        with PO(sns, "_probe_tcp_port", return_value=True), PO(
            sns, "_check_nfs_exports_showmount", return_value=ret
        ), PO(time, "sleep") as mock_sleep:
            assert wait_for_controller_nfs("10.0.0.1", paths, timeout=10) is True
            mock_sleep.assert_called_once_with(0.5)

    with PO(sns, "_probe_tcp_port", return_value=True), PO(
        sns, "_check_nfs_exports_showmount", side_effect=[False, False, True]
    ) as mock_chk, PO(time, "sleep"):
        assert wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=30) is True
        assert mock_chk.call_count == 3


def test_wait_for_controller_nfs_timeouts():
    t = [100.0]
    tick = lambda: t.__setitem__(0, t[0] + 5.0) or t[0]  # noqa: E731
    log = MagicMock()
    with PO(sns, "_probe_tcp_port", return_value=False), PO(
        time, "monotonic", side_effect=tick
    ), PO(time, "sleep"):
        assert (
            wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=5, log=log) is False
        )
        assert "NFS port 2049" in log.warning.call_args[0][0]

    log.reset_mock()
    with PO(sns, "_probe_tcp_port", return_value=True), PO(
        sns, "_check_nfs_exports_showmount", return_value=False
    ), PO(time, "monotonic", side_effect=tick), PO(time, "sleep"):
        assert (
            wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=5, log=log) is False
        )
        assert "to export ['/home']" in log.warning.call_args[0][0]


def test_is_controller_nfs_server():
    lkp = util.Lookup(
        NSDict(slurm_control_host="slurm-ctrl", slurm_control_addr="10.0.0.2")
    )
    with PO(util, "host_lookup") as hl, PO(socket, "gethostbyname") as ghbn:
        assert _is_controller_nfs_server("", lkp) is False
        assert _is_controller_nfs_server("slurm-ctrl", lkp) is True
        assert _is_controller_nfs_server("10.0.0.2", lkp) is True
        hl.assert_not_called()
        ghbn.assert_not_called()

    with PO(util, "host_lookup") as hl, PO(
        socket, "gethostbyname", side_effect=socket.gaierror("no host")
    ) as ghbn:
        assert _is_controller_nfs_server("ext.internal", lkp) is False
        ghbn.assert_called_once_with("ext.internal")
        hl.assert_not_called()

    with PO(util, "host_lookup") as hl, PO(
        socket,
        "gethostbyname",
        side_effect=lambda h: "10.9.0.5" if h == "fs" else "10.0.0.2",
    ):
        assert _is_controller_nfs_server("fs", lkp) is False
        assert _is_controller_nfs_server("ctrl-alias.internal", lkp) is True
        hl.assert_not_called()

    lkp2 = util.Lookup(NSDict(slurm_control_host="slurm-ctrl", slurm_control_addr=None))
    with PO(util, "host_lookup") as hl, PO(
        socket, "gethostbyname", return_value="10.0.0.2"
    ):
        assert _is_controller_nfs_server("10.0.0.2", lkp2) is True
        hl.assert_not_called()


def test_setup_network_storage_controller_and_client_preflight():
    ctrl = MagicMock(instance_role="controller")
    with _client_env([], lookup_obj=ctrl) as (log, _, wait, *_), PO(
        sns, "separate_external_internal_mounts", return_value=([], [])
    ):
        run_setup_network_storage(log)
        wait.assert_not_called()

    m_cfg = NSDict(
        munge_mount=NSDict(
            server_ip="10.0.0.1", remote_mount="/etc/munge", fs_type="nfs"
        )
    )
    mounts = [_mnt(path="/home", fs_type="NFS"), _mnt(path="/apps", fs_type="Nfs")]
    with _client_env(mounts, cfg_obj=m_cfg) as (log, _, wait, *_):
        run_setup_network_storage(log)
        wait.assert_called_once_with(
            "10.0.0.1", ["/apps", "/etc/munge", "/home"], timeout=600, log=log
        )

    with _client_env([], cfg_obj=m_cfg) as (log, _, wait, *_):
        run_setup_network_storage(log)
        wait.assert_called_once_with("10.0.0.1", ["/etc/munge"], timeout=600, log=log)


def test_setup_network_storage_timeout_scaling_and_fallback():
    with _client_env([_mnt()]) as (log, cfg_obj, wait, *_):
        run_setup_network_storage(log)
        wait.assert_called_once_with("10.0.0.1", ["/home"], timeout=600, log=log)
        assert "controller_startup_scripts_timeout" not in cfg_obj

    for val, exp in ((None, 600), (True, 600), (120, 600), (300, 600), (900, 1200)):
        c = NSDict(munge_mount=None, controller_startup_scripts_timeout=val)
        with _client_env([_mnt()], cfg_obj=c) as (log, _, wait, *_):
            run_setup_network_storage(log)
            wait.assert_called_once_with("10.0.0.1", ["/home"], timeout=exp, log=log)

    with _client_env([_mnt()]) as (log, _, wait, mount, copy, fstab, munge):
        wait.return_value = False
        run_setup_network_storage(log)
        assert copy.called and fstab.called
        mount.assert_called_once()
        munge.assert_called_once_with(log)


def test_setup_network_storage_external_nfs_server_avoids_host_lookup_retries():
    m_mnt = NSDict(server_ip="slurm-ctrl", remote_mount="/etc/munge", fs_type="nfs")
    cfg_obj = NSDict(slurm_control_host="slurm-ctrl", munge_mount=m_mnt)
    mounts = [
        _mnt(server="unresolvable.local", path="/data"),
        _mnt(server="slurm-ctrl", path="/home"),
    ]
    with PO(util.Lookup, "instance_role", new="login"), _client_env(
        mounts, cfg_obj=cfg_obj, lookup_obj=util.Lookup(cfg_obj)
    ) as (log, _, wait, *_), PO(sns, "separate_external_internal_mounts") as sep, PO(
        util, "host_lookup"
    ) as hl, PO(
        socket, "gethostbyname", side_effect=socket.gaierror("DNS failed")
    ) as ghbn:
        run_setup_network_storage(log)
        sep.assert_not_called()
        hl.assert_not_called()
        ghbn.assert_called_once_with("unresolvable.local")
        wait.assert_called_once_with(
            "slurm-ctrl", ["/etc/munge", "/home"], timeout=600, log=log
        )
