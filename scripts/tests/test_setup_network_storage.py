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

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from addict import Dict as NSDict

PARENT_DIR = str(Path(__file__).resolve().parent.parent)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from setup_network_storage import (  # noqa: E402
    _check_nfs_exports_showmount,
    _find_showmount,
    _is_controller_nfs_server,
    _is_path_exported,
    _probe_nfs_mount,
    _probe_tcp_port,
    separate_external_internal_mounts,
    setup_network_storage as run_setup_network_storage,
    wait_for_controller_nfs,
)
import util  # noqa: E402


def test_probe_tcp_port_success():
    with patch("socket.create_connection") as mock_conn:
        mock_conn.return_value.__enter__.return_value = MagicMock()
        assert _probe_tcp_port("10.0.0.1", port=2049, timeout=1.0) is True
        mock_conn.assert_called_once_with(("10.0.0.1", 2049), timeout=1.0)


def test_probe_tcp_port_failure():
    with patch(
        "socket.create_connection", side_effect=socket.error("Connection refused")
    ):
        assert _probe_tcp_port("10.0.0.1", port=2049, timeout=1.0) is False


def test_probe_tcp_port_empty_or_invalid_host():
    assert _probe_tcp_port("", port=2049) is False
    assert _probe_tcp_port("   ", port=2049) is False
    assert _probe_tcp_port(None, port=2049) is False  # type: ignore


def test_probe_tcp_port_ipv6():
    with patch("socket.create_connection") as mock_conn:
        mock_conn.return_value.__enter__.return_value = MagicMock()
        assert _probe_tcp_port("::1", port=2049, timeout=1.0) is True
        mock_conn.assert_called_once_with(("::1", 2049), timeout=1.0)


def test_probe_tcp_port_os_errors():
    for err in (
        OSError("Network unreachable"),
        TypeError("bad arg"),
        OverflowError("port too large"),
    ):
        with patch("socket.create_connection", side_effect=err):
            assert _probe_tcp_port("10.0.0.1", port=2049) is False


def test_find_showmount():
    with patch("shutil.which", return_value="/usr/sbin/showmount"):
        assert _find_showmount() == "/usr/sbin/showmount"

    with patch("shutil.which", return_value=None), patch(
        "pathlib.Path.is_file", return_value=True
    ), patch("os.access", return_value=True):
        assert _find_showmount() == "/usr/sbin/showmount"

    with patch("shutil.which", return_value=None), patch(
        "pathlib.Path.is_file", return_value=False
    ):
        assert _find_showmount() is None


def test_is_path_exported():
    active = {"/home", "/opt/apps"}
    assert _is_path_exported("/home", active) is True
    assert _is_path_exported("/home/", active) is True
    assert _is_path_exported("/home/shared", active) is True
    assert _is_path_exported("//home/shared", active) is True
    assert _is_path_exported("/opt/apps/cuda/12.0", active) is True
    # Prefix collision must NOT match
    assert _is_path_exported("/home2", active) is False
    assert _is_path_exported("/home_other", active) is False
    assert _is_path_exported("/opt/apps2", active) is False
    assert _is_path_exported("/scratch", active) is False
    # Empty or relative paths must NOT match
    assert _is_path_exported("", active) is False
    assert _is_path_exported("", {""}) is False
    assert _is_path_exported("relative/path", active) is False
    assert _is_path_exported("/home", {""}) is False
    # Root export matches any absolute path (including double-slash root)
    assert _is_path_exported("/any/nested/path", {"/"}) is True
    assert _is_path_exported("/home", {"//"}) is True


def test_check_nfs_exports_showmount_success():
    showmount_output = """Export list for 10.0.0.1:
/home                *
/opt/apps            10.0.0.0/16
/slurm/key_distribution (everyone)
"""
    mock_res = MagicMock(returncode=0, stdout=showmount_output)
    with patch(
        "setup_network_storage._find_showmount",
        return_value="/usr/sbin/showmount",
    ), patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage.run", return_value=mock_res
    ) as mock_run:
        result = _check_nfs_exports_showmount("10.0.0.1", {"/home", "/opt/apps"})
        assert result is True
        mock_run.assert_called_once()


def test_check_nfs_exports_showmount_subdirectories():
    showmount_output = """Export list for 10.0.0.1:
/home                *
/opt/apps            10.0.0.0/16
"""
    mock_res = MagicMock(returncode=0, stdout=showmount_output)
    with patch(
        "setup_network_storage._find_showmount",
        return_value="/usr/sbin/showmount",
    ), patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage.run", return_value=mock_res
    ):
        assert (
            _check_nfs_exports_showmount(
                "10.0.0.1", {"/home/shared", "/opt/apps/cuda/12.0"}
            )
            is True
        )
        assert _check_nfs_exports_showmount("10.0.0.1", {"/home_other"}) is False


def test_check_nfs_exports_showmount_missing_share():
    showmount_output = """Export list for 10.0.0.1:
/home *
"""
    mock_res = MagicMock(returncode=0, stdout=showmount_output)
    with patch(
        "setup_network_storage._find_showmount",
        return_value="/usr/sbin/showmount",
    ), patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage.run", return_value=mock_res
    ):
        result = _check_nfs_exports_showmount("10.0.0.1", {"/home", "/opt/apps"})
        assert result is False


def test_check_nfs_exports_showmount_port111_unreachable():
    with patch(
        "setup_network_storage._find_showmount",
        return_value="/usr/sbin/showmount",
    ), patch("setup_network_storage._probe_tcp_port", return_value=False), patch(
        "setup_network_storage.run"
    ) as mock_run:
        result = _check_nfs_exports_showmount("10.0.0.1", {"/home"})
        assert result is None
        # Must NOT call showmount if port 111 is unreachable
        mock_run.assert_not_called()


def test_check_nfs_exports_showmount_binary_missing():
    with patch("setup_network_storage._find_showmount", return_value=None):
        result = _check_nfs_exports_showmount("10.0.0.1", {"/home"})
        assert result is None


def test_check_nfs_exports_showmount_command_error():
    mock_res = MagicMock(returncode=1, stderr="RPC: Program not registered")
    with patch(
        "setup_network_storage._find_showmount",
        return_value="/usr/sbin/showmount",
    ), patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage.run", return_value=mock_res
    ):
        result = _check_nfs_exports_showmount("10.0.0.1", {"/home"})
        assert result is None


def test_check_nfs_exports_showmount_exception():
    with patch(
        "setup_network_storage._find_showmount",
        return_value="/usr/sbin/showmount",
    ), patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage.run", side_effect=RuntimeError("exec error")
    ):
        result = _check_nfs_exports_showmount("10.0.0.1", {"/home"})
        assert result is None


def test_probe_nfs_mount_success():
    mock_res = MagicMock(returncode=0)
    with patch("setup_network_storage.run", return_value=mock_res) as mock_run, patch(
        "pathlib.Path.is_mount", return_value=True
    ), patch("pathlib.Path.rmdir"):
        result = _probe_nfs_mount("10.0.0.1", "/home", timeout=5.0)
        assert result is True
        assert any("umount -l" in str(call) for call in mock_run.call_args_list)


def test_probe_nfs_mount_failure():
    mock_res = MagicMock(returncode=32, stderr="mount.nfs: access denied by server")
    with patch("setup_network_storage.run", return_value=mock_res), patch(
        "pathlib.Path.is_mount", return_value=False
    ), patch("pathlib.Path.rmdir"):
        result = _probe_nfs_mount("10.0.0.1", "/home", timeout=5.0)
        assert result is False


def test_wait_for_controller_nfs_invalid_timeout():
    mock_log = MagicMock()
    assert (
        wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=0, log=mock_log) is False
    )
    assert (
        wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=-5, log=mock_log)
        is False
    )
    assert mock_log.warning.call_count == 2
    assert "Invalid timeout 0s" in mock_log.warning.call_args_list[0][0][0]
    assert "Invalid timeout -5s" in mock_log.warning.call_args_list[1][0][0]


def test_wait_for_controller_nfs_happy_path_tier1():
    with patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage._check_nfs_exports_showmount", return_value=True
    ), patch("time.sleep") as mock_sleep:
        assert (
            wait_for_controller_nfs("10.0.0.1", ["/home", "/apps"], timeout=10) is True
        )
        mock_sleep.assert_called_once_with(0.5)


def test_wait_for_controller_nfs_tier2_fallback():
    with patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage._check_nfs_exports_showmount", return_value=None
    ), patch(
        "setup_network_storage._probe_nfs_mount", return_value=True
    ) as mock_probe, patch(
        "time.sleep"
    ) as mock_sleep:
        assert (
            wait_for_controller_nfs("10.0.0.1", ["/home", "/apps"], timeout=10) is True
        )
        assert mock_probe.call_count == 2
        mock_sleep.assert_called_once_with(0.5)


def test_wait_for_controller_nfs_retries_on_unready_exports_then_succeeds():
    attempts = 0

    def mock_check(server, expected_paths, timeout=3.0, log=None):
        nonlocal attempts
        attempts += 1
        return attempts >= 3

    with patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage._check_nfs_exports_showmount",
        side_effect=mock_check,
    ), patch("time.sleep"):
        assert wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=30) is True
        assert attempts == 3


def test_wait_for_controller_nfs_empty_paths():
    with patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "time.sleep"
    ) as mock_sleep:
        assert wait_for_controller_nfs("10.0.0.1", [], timeout=10) is True
        mock_sleep.assert_called_once_with(0.5)


def test_wait_for_controller_nfs_port_2049_timeout():
    fake_time = [100.0]
    mock_log = MagicMock()

    def mock_monotonic():
        fake_time[0] += 5.0
        return fake_time[0]

    with patch("setup_network_storage._probe_tcp_port", return_value=False), patch(
        "time.monotonic", side_effect=mock_monotonic
    ), patch("time.sleep"):
        assert (
            wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=5, log=mock_log)
            is False
        )
        mock_log.warning.assert_called_once()
        assert (
            "Timed out after 5s waiting for NFS port 2049 on 10.0.0.1"
            in mock_log.warning.call_args[0][0]
        )


def test_wait_for_controller_nfs_exports_timeout():
    fake_time = [100.0]
    mock_log = MagicMock()

    def mock_monotonic():
        fake_time[0] += 5.0
        return fake_time[0]

    with patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage._check_nfs_exports_showmount", return_value=False
    ), patch("time.monotonic", side_effect=mock_monotonic), patch("time.sleep"):
        assert (
            wait_for_controller_nfs("10.0.0.1", ["/home"], timeout=5, log=mock_log)
            is False
        )
        mock_log.warning.assert_called_once()
        assert (
            "Timed out after 5s waiting for controller '10.0.0.1' to export"
            in mock_log.warning.call_args[0][0]
        )


def test_separate_external_internal_mounts_dns_fast_path():
    with patch("setup_network_storage.lkp") as mock_lkp:
        mock_lkp.control_host = "test-controller"
        mock_lkp.control_host_addr = "10.0.0.2"
        mock_lkp.control_addr = "10.0.0.3"
        mock_lkp.instance_role = "login"

        mounts = [
            NSDict({"server_ip": "test-controller", "remote_mount": "/home"}),
            NSDict({"server_ip": "10.0.0.2", "remote_mount": "/apps"}),
            NSDict({"server_ip": "10.0.0.3", "remote_mount": "/slurm"}),
            NSDict({"server_ip": "10.99.99.99", "remote_mount": "/filestore"}),
        ]

        with patch("setup_network_storage.util.host_lookup") as mock_lookup:
            mock_lookup.return_value = "10.99.99.99"
            ext, int_m = separate_external_internal_mounts(mounts)

            assert len(int_m) == 3
            assert len(ext) == 1
            # host_lookup should only have been called for the external server, not the 3 fast-path matches
            mock_lookup.assert_called_once_with("10.99.99.99")


def test_is_controller_nfs_server_variants():
    cfg = NSDict(slurm_control_host="slurm-controller", slurm_control_addr="10.0.0.2")
    lookup_obj = util.Lookup(cfg)

    with patch("setup_network_storage.util.host_lookup") as mock_host_lookup, patch(
        "socket.gethostbyname"
    ) as mock_gethostbyname:
        assert _is_controller_nfs_server("", lookup_obj) is False
        assert _is_controller_nfs_server("slurm-controller", lookup_obj) is True
        assert _is_controller_nfs_server("10.0.0.2", lookup_obj) is True
        mock_host_lookup.assert_not_called()
        mock_gethostbyname.assert_not_called()

    # External unresolvable hostname: single gethostbyname call, no util.host_lookup retry
    with patch("setup_network_storage.util.host_lookup") as mock_host_lookup, patch(
        "socket.gethostbyname", side_effect=socket.gaierror("Name not known")
    ) as mock_gethostbyname:
        assert (
            _is_controller_nfs_server("external-nfs.example.internal", lookup_obj)
            is False
        )
        mock_gethostbyname.assert_called_once_with("external-nfs.example.internal")
        mock_host_lookup.assert_not_called()

    # External resolvable hostname resolving to non-controller IP
    with patch("setup_network_storage.util.host_lookup") as mock_host_lookup, patch(
        "socket.gethostbyname",
        side_effect=lambda h: "10.99.0.5"
        if h == "filestore.example.internal"
        else "10.0.0.2",
    ):
        assert (
            _is_controller_nfs_server("filestore.example.internal", lookup_obj) is False
        )
        mock_host_lookup.assert_not_called()

    # Alias hostname resolving to controller IP
    with patch("setup_network_storage.util.host_lookup") as mock_host_lookup, patch(
        "socket.gethostbyname", return_value="10.0.0.2"
    ) as mock_gethostbyname:
        assert (
            _is_controller_nfs_server("controller-alias.internal", lookup_obj) is True
        )
        mock_gethostbyname.assert_called_once_with("controller-alias.internal")
        mock_host_lookup.assert_not_called()

    # slurm_control_addr=None where server IP matches socket.gethostbyname(control_host)
    cfg_no_addr = NSDict(slurm_control_host="slurm-controller", slurm_control_addr=None)
    lookup_no_addr = util.Lookup(cfg_no_addr)
    with patch("setup_network_storage.util.host_lookup") as mock_host_lookup, patch(
        "socket.gethostbyname", return_value="10.0.0.2"
    ):
        assert _is_controller_nfs_server("10.0.0.2", lookup_no_addr) is True
        mock_host_lookup.assert_not_called()


def test_setup_network_storage_controller_skips_preflight():
    mock_log = MagicMock()
    with patch("setup_network_storage.lkp") as mock_lkp, patch(
        "setup_network_storage.resolve_network_storage", return_value=[]
    ), patch(
        "setup_network_storage.separate_external_internal_mounts",
        return_value=([], []),
    ), patch(
        "setup_network_storage.wait_for_controller_nfs"
    ) as mock_wait, patch(
        "pathlib.Path.is_file", return_value=True
    ), patch(
        "shutil.copy2"
    ), patch(
        "builtins.open", mock_open()
    ), patch(
        "setup_network_storage.mount_fstab"
    ), patch(
        "setup_network_storage.munge_mount_handler"
    ):
        mock_lkp.instance_role = "controller"
        run_setup_network_storage(mock_log)
        mock_wait.assert_not_called()


def test_setup_network_storage_client_invokes_preflight():
    mock_log = MagicMock()
    internal_mount = NSDict(
        {
            "server_ip": "10.0.0.1",
            "remote_mount": "/home",
            "local_mount": "/home",
            "fs_type": "nfs",
            "mount_options": "defaults",
        }
    )

    with patch("setup_network_storage.lkp") as mock_lkp, patch(
        "setup_network_storage.cfg"
    ) as mock_cfg, patch(
        "setup_network_storage.resolve_network_storage",
        return_value=[internal_mount],
    ), patch(
        "setup_network_storage.separate_external_internal_mounts",
        return_value=([], [internal_mount]),
    ), patch(
        "setup_network_storage.wait_for_controller_nfs", return_value=True
    ) as mock_wait, patch(
        "pathlib.Path.is_file", return_value=True
    ), patch(
        "shutil.copy2"
    ), patch(
        "builtins.open", mock_open()
    ), patch(
        "setup_network_storage.mount_fstab"
    ), patch(
        "setup_network_storage.munge_mount_handler"
    ), patch(
        "setup_network_storage.util.mkdirp"
    ):
        mock_lkp.instance_role = "login"
        mock_lkp.control_host = "test-controller"
        mock_lkp.control_host_addr = "10.0.0.1"
        mock_lkp.control_addr = "10.0.0.1"
        mock_cfg.munge_mount = NSDict(
            {
                "server_ip": "10.0.0.1",
                "remote_mount": "/etc/munge",
                "fs_type": "nfs",
            }
        )

        run_setup_network_storage(mock_log)
        mock_wait.assert_called_once_with(
            "10.0.0.1", ["/etc/munge", "/home"], timeout=600, log=mock_log
        )


def test_setup_network_storage_client_preflight_timeout_falls_back_to_mount_fstab():
    mock_log = MagicMock()
    internal_mount = NSDict(
        {
            "server_ip": "10.0.0.1",
            "remote_mount": "/home",
            "local_mount": "/home",
            "fs_type": "nfs",
            "mount_options": "defaults",
        }
    )

    fake_time = [100.0]

    def mock_monotonic():
        fake_time[0] += 700.0
        return fake_time[0]

    with patch("setup_network_storage.lkp") as mock_lkp, patch(
        "setup_network_storage.cfg"
    ) as mock_cfg, patch(
        "setup_network_storage.resolve_network_storage",
        return_value=[internal_mount],
    ), patch(
        "setup_network_storage.separate_external_internal_mounts"
    ) as mock_separate, patch(
        "setup_network_storage._probe_tcp_port", return_value=False
    ), patch(
        "time.monotonic", side_effect=mock_monotonic
    ), patch(
        "time.sleep"
    ), patch(
        "pathlib.Path.is_file", return_value=True
    ), patch(
        "shutil.copy2"
    ) as mock_copy, patch(
        "builtins.open", mock_open()
    ) as mock_file, patch(
        "setup_network_storage.mount_fstab"
    ) as mock_mount_fstab, patch(
        "setup_network_storage.munge_mount_handler"
    ) as mock_munge, patch(
        "setup_network_storage.util.mkdirp"
    ):
        mock_lkp.instance_role = "login"
        mock_lkp.control_addr = "10.0.0.1"
        mock_cfg.munge_mount = None

        run_setup_network_storage(mock_log)
        mock_separate.assert_not_called()
        mock_log.warning.assert_called_once()
        assert (
            "Proceeding to standard mount retries" in mock_log.warning.call_args[0][0]
        )
        # Ensure /etc/fstab is written and mount_fstab is called as fallback
        assert mock_copy.called
        assert mock_file.called
        mock_mount_fstab.assert_called_once()
        mock_munge.assert_called_once_with(mock_log)


def test_setup_network_storage_startup_timeout_calculation():
    mock_log = MagicMock()
    internal_mount = NSDict(
        {
            "server_ip": "10.0.0.1",
            "remote_mount": "/home",
            "local_mount": "/home",
            "fs_type": "nfs",
            "mount_options": "defaults",
        }
    )

    # Verify omitted key in NSDict does not auto-vivify controller_startup_scripts_timeout
    cfg_omitted = NSDict({"munge_mount": None})
    with patch("setup_network_storage.lkp") as mock_lkp, patch(
        "setup_network_storage.cfg", cfg_omitted
    ), patch(
        "setup_network_storage.resolve_network_storage",
        return_value=[internal_mount],
    ), patch(
        "setup_network_storage.wait_for_controller_nfs", return_value=True
    ) as mock_wait, patch(
        "pathlib.Path.is_file", return_value=True
    ), patch(
        "shutil.copy2"
    ), patch(
        "builtins.open", mock_open()
    ), patch(
        "setup_network_storage.mount_fstab"
    ), patch(
        "setup_network_storage.munge_mount_handler"
    ), patch(
        "setup_network_storage.util.mkdirp"
    ):
        mock_lkp.instance_role = "login"
        mock_lkp.control_addr = "10.0.0.1"
        run_setup_network_storage(mock_log)
        mock_wait.assert_called_once_with(
            "10.0.0.1", ["/home"], timeout=600, log=mock_log
        )
        assert "controller_startup_scripts_timeout" not in cfg_omitted

    for cfg_timeout, expected_wait in (
        (None, 600),
        (True, 600),
        (120, 600),
        (300, 600),
        (900, 1200),
    ):
        cfg_obj = NSDict(
            {
                "munge_mount": None,
                "controller_startup_scripts_timeout": cfg_timeout,
            }
        )
        with patch("setup_network_storage.lkp") as mock_lkp, patch(
            "setup_network_storage.cfg", cfg_obj
        ), patch(
            "setup_network_storage.resolve_network_storage",
            return_value=[internal_mount],
        ), patch(
            "setup_network_storage.wait_for_controller_nfs", return_value=True
        ) as mock_wait, patch(
            "pathlib.Path.is_file", return_value=True
        ), patch(
            "shutil.copy2"
        ), patch(
            "builtins.open", mock_open()
        ), patch(
            "setup_network_storage.mount_fstab"
        ), patch(
            "setup_network_storage.munge_mount_handler"
        ), patch(
            "setup_network_storage.util.mkdirp"
        ):
            mock_lkp.instance_role = "login"
            mock_lkp.control_addr = "10.0.0.1"
            run_setup_network_storage(mock_log)
            mock_wait.assert_called_once_with(
                "10.0.0.1", ["/home"], timeout=expected_wait, log=mock_log
            )


def test_setup_network_storage_external_nfs_server_avoids_host_lookup_retries():
    mock_log = MagicMock()
    cfg_obj = NSDict(
        {
            "slurm_control_host": "slurm-controller",
            "munge_mount": NSDict(
                {
                    "server_ip": "slurm-controller",
                    "remote_mount": "/etc/munge",
                    "fs_type": "nfs",
                }
            ),
        }
    )
    lookup_obj = util.Lookup(cfg_obj)

    external_mount = NSDict(
        {
            "server_ip": "unresolvable-external-filestore.local",
            "remote_mount": "/datasets",
            "local_mount": "/mnt/datasets",
            "fs_type": "nfs",
            "mount_options": "defaults",
        }
    )
    home_mount = NSDict(
        {
            "server_ip": "slurm-controller",
            "remote_mount": "/home",
            "local_mount": "/home",
            "fs_type": "nfs",
            "mount_options": "defaults",
        }
    )

    with patch.object(util.Lookup, "instance_role", new="login"), patch(
        "setup_network_storage.lkp", lookup_obj
    ), patch("setup_network_storage.cfg", cfg_obj), patch(
        "setup_network_storage.resolve_network_storage",
        return_value=[external_mount, home_mount],
    ), patch(
        "setup_network_storage.separate_external_internal_mounts"
    ) as mock_separate, patch(
        "setup_network_storage.util.host_lookup"
    ) as mock_host_lookup, patch(
        "socket.gethostbyname", side_effect=socket.gaierror("DNS failed")
    ) as mock_gethostbyname, patch(
        "setup_network_storage.wait_for_controller_nfs", return_value=True
    ) as mock_wait, patch(
        "pathlib.Path.is_file", return_value=True
    ), patch(
        "shutil.copy2"
    ), patch(
        "builtins.open", mock_open()
    ), patch(
        "setup_network_storage.mount_fstab"
    ), patch(
        "setup_network_storage.munge_mount_handler"
    ), patch(
        "setup_network_storage.util.mkdirp"
    ):
        run_setup_network_storage(mock_log)
        mock_separate.assert_not_called()
        mock_host_lookup.assert_not_called()
        mock_gethostbyname.assert_called_once_with(
            "unresolvable-external-filestore.local"
        )
        mock_wait.assert_called_once_with(
            "slurm-controller", ["/etc/munge", "/home"], timeout=600, log=mock_log
        )


def test_setup_network_storage_standalone_munge_mount_invokes_preflight():
    """Verify that when disable_default_mounts is true, munge_mount alone still triggers preflight."""
    mock_log = MagicMock()

    with patch("setup_network_storage.lkp") as mock_lkp, patch(
        "setup_network_storage.cfg"
    ) as mock_cfg, patch(
        "setup_network_storage.resolve_network_storage",
        return_value=[],
    ), patch(
        "setup_network_storage.separate_external_internal_mounts",
        return_value=([], []),
    ), patch(
        "setup_network_storage.wait_for_controller_nfs", return_value=True
    ) as mock_wait, patch(
        "pathlib.Path.is_file", return_value=True
    ), patch(
        "shutil.copy2"
    ), patch(
        "builtins.open", mock_open()
    ), patch(
        "setup_network_storage.mount_fstab"
    ), patch(
        "setup_network_storage.munge_mount_handler"
    ), patch(
        "setup_network_storage.util.mkdirp"
    ):
        mock_lkp.instance_role = "login"
        mock_lkp.control_host = "test-controller"
        mock_lkp.control_host_addr = "10.0.0.1"
        mock_lkp.control_addr = "10.0.0.1"
        mock_cfg.munge_mount = NSDict(
            {
                "server_ip": "10.0.0.1",
                "remote_mount": "/etc/munge",
                "fs_type": "nfs",
            }
        )

        run_setup_network_storage(mock_log)
        mock_wait.assert_called_once_with(
            "10.0.0.1", ["/etc/munge"], timeout=600, log=mock_log
        )


def test_wait_for_controller_nfs_tier2_fallback_partial_failure_times_out():
    """Verify that if one of multiple exports fails in Tier 2, wait_for_controller_nfs times out and returns False."""
    mock_log = MagicMock()

    def mock_probe(server, path, timeout=4.0, log=None):
        return path == "/home"  # /apps fails

    with patch("setup_network_storage._probe_tcp_port", return_value=True), patch(
        "setup_network_storage._check_nfs_exports_showmount", return_value=None
    ), patch("setup_network_storage._probe_nfs_mount", side_effect=mock_probe), patch(
        "time.sleep"
    ):
        assert (
            wait_for_controller_nfs(
                "10.0.0.1", ["/home", "/apps"], timeout=1, log=mock_log
            )
            is False
        )
        mock_log.warning.assert_called_once()
        assert "Timed out after 1s waiting" in mock_log.warning.call_args[0][0]


def test_setup_network_storage_case_insensitive_fs_type():
    """Verify that uppercase NFS and mixed-case Nfs are recognized and trigger preflight."""
    mock_log = MagicMock()
    mount_upper = NSDict(
        {
            "server_ip": "10.0.0.1",
            "remote_mount": "/home",
            "local_mount": "/home",
            "fs_type": "NFS",
            "mount_options": "defaults",
        }
    )
    mount_mixed = NSDict(
        {
            "server_ip": "10.0.0.1",
            "remote_mount": "/apps",
            "local_mount": "/apps",
            "fs_type": "Nfs",
            "mount_options": "defaults",
        }
    )

    with patch("setup_network_storage.lkp") as mock_lkp, patch(
        "setup_network_storage.cfg"
    ) as mock_cfg, patch(
        "setup_network_storage.resolve_network_storage",
        return_value=[mount_upper, mount_mixed],
    ), patch(
        "setup_network_storage.wait_for_controller_nfs", return_value=True
    ) as mock_wait, patch(
        "pathlib.Path.is_file", return_value=True
    ), patch(
        "shutil.copy2"
    ), patch(
        "builtins.open", mock_open()
    ), patch(
        "setup_network_storage.mount_fstab"
    ), patch(
        "setup_network_storage.munge_mount_handler"
    ), patch(
        "setup_network_storage.util.mkdirp"
    ):
        mock_lkp.instance_role = "login"
        mock_lkp.control_addr = "10.0.0.1"
        mock_cfg.munge_mount = None

        run_setup_network_storage(mock_log)
        mock_wait.assert_called_once_with(
            "10.0.0.1", ["/apps", "/home"], timeout=600, log=mock_log
        )
