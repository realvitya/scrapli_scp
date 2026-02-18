"""scrapli_scp.asyncssh.fortinet_fortios"""
import re
import textwrap
from pathlib import Path
from typing import Any, Optional, Union, Callable
from scrapli_scp.asyncscp.base import AsyncSCPFeature, FileTransferResult
from scrapli_scp.logging import logger


class AsyncSCPFortiOS(AsyncSCPFeature):
    def __init__(self, *args: Any, **kwargs: Any):
        self._need_scp_config: bool = False
        self._scp_to_apply = textwrap.dedent("""\
            config system global
                set admin-scp enable
            end
        """).splitlines()
        self._scp_to_clean = textwrap.dedent("""\
            config system global
                unset admin-scp
            end
        """).splitlines()
        super().__init__(*args, **kwargs)

    async def file_transfer(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def _get_file_check_result(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def check_file_exists(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def _get_file_list(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def check_file_checksum(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def _get_file_checksum(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def check_file_size(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def _get_file_size(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def check_device_file(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def _get_device_fs(self):
        raise NotImplementedError("Not valid for FortiOS. Please use get_config, put_config, put_image!")

    async def _ensure_scp_capability(self, force: Optional[bool] = False) -> Union[bool, None]:
        """
        Algorithm based on:
        https://community.fortinet.com/t5/FortiGate/Technical-Tip-How-to-download-a-FortiGate-configuration-file-and/ta-p/197125
        https://fortihelp.blogspot.com/2018/12/scp-config-backup-config-restore-and.html
        https://community.fortinet.com/t5/FortiGate/Technical-Tip-Backing-Up-the-FortiGate-configuration-file-via/ta-p/367088

        Args:
            force (Optional[bool]): If True, forces the SCP configuration even if it's already
                enabled. Defaults to False.

        Returns:
            Union[bool, None]: Returns True if the SCP capability was successfully ensured, False
                if the operation failed or wasn't allowed due to insufficient permissions and
                force=False. Returns None if no configuration changes are needed.
        """
        result = None
        if force is None:  # prevent checking
            return result

        # intended configuration:
        #
        # config system global
        #     set admin-scp enable
        # end

        output = await self.conn.send_command("get system global | grep admin-scp")
        if re.search(r"admin-scp\s*:\s*disable", output.result):  # not enabled
            self._need_scp_config = True

        # would need configuration, but do we want it?
        if not force and self._need_scp_config:
            result = False
            return result

        logger.debug("Checking SCP capability")
        # check admin rights after 7.4.4
        # https://community.fortinet.com/t5/FortiGate/Technical-Tip-Backing-Up-the-FortiGate-configuration-file-via/ta-p/367088
        output = await self.conn.send_command("get system status | grep ^Version:")

        output_check = await self.conn.send_command(f"get system admin list | grep {self.conn.auth_username}")
        # [user]    ssh     mgmt:1.1.1.1:22    root  [ADMINPROF]   2.2.2.2:49058     2026-01-30 07:53:51
        match = re.search(r"\S+\s+\S+\s+\S+\s+\S+\s+(?P<admin_prof>\S+)", output_check.result)
        admin_prof = match.group("admin_prof")
        logger.debug("Admin profile detected: %s", admin_prof)
        if admin_prof != "super_admin":  # no need to check anything more
            logger.warning(
                "Backup is not full backup, it may not contain all the information and may not be "
                "restorable! Please use user with super_admin profile to get full backup!"
            )
            match = re.search(r"Version: \S+ v(?P<version>\d+\.\d+\.\d+)", output.result)
            version = match.group("version")
            major, minor, patch = (int(x) for x in version.split("."))
            logger.debug("FortiOS version detected: %s", version)
            if (major, minor, patch) >= (7, 4, 4):
                logger.debug("FortiOS version >= 7.4.4 detected, checking admin SCP rights")
                if admin_prof in ("super_admin_readonly", "admin_no_access"):
                    logger.error("user '%s' does not have SCP rights!", self.conn.auth_username)
                    result = False
                    return result
                else:
                    output_prof = await self.conn.send_command(f"show sys accprofile {admin_prof} | grep 'set admin' -B5")
                    match = re.search(r"(?s)config sysgrp-permission.*\sset admin (?P<right>\S+)", output_prof.result)
                    if not match or (match and match.group("right") != "read-write"):
                        logger.error("user '%s' does not have SCP rights!", self.conn.auth_username)
                        result = False
                        return result

        # apply SCP enablement
        output_apply = await self.conn.send_commands(self._scp_to_apply)

        if output_apply.failed:
            # commands did not succeed
            result = False
            # try to revert
            await self.conn.send_commands(self._scp_to_clean)
        else:
            # device reconfigured for scp
            result = True

        return result

    async def _cleanup_after_transfer(self) -> None:
        # we assume that _scp_to_clean was populated by a previously called _ensure_scp_capability
        if not self._need_scp_config:
            return
        await self.conn.send_commands(self._scp_to_clean)

    async def get_config(self,
                         filename: str = "",
                         overwrite: bool = False,
                         force_scp_config: bool = False,
                         cleanup: bool = True,
                         progress_handler: Optional[Callable] = None,
                         prevent_timeout: Optional[float] = None,
                         sys_config: bool = True,
                         ) -> FileTransferResult:
        """Download configuration from firewall

        Args:
            filename: output filename with configuration
            overwrite: If set to `True`, will overwrite the existing file
            force_scp_config: If set to `True`, SCP function will be enabled in the device configuration before transfer.
                              If set to `False`, SCP functionality will be checked but won't
                              configure the device.
                              If set to `None`, capability won't be checked.
            cleanup: If set to True, call the cleanup procedure to restore configuration if it was
                     altered
            progress_handler: function to call by file copy (used by asyncssh.scp function)
            prevent_timeout: interval in seconds when we send an empty command to keep the SSH channel
                             up, 0 to turn it off, default is the same as `timeout_ops`
            sys_config (bool): Get system config which requires local admin user. Remote users can only get limited
                               backup

        Returns:
            (FileTransferResult): Result of transfer

        Notes:
            No checksum nor destination space check is performed!
        """
        result = FileTransferResult(False, False, False)
        # check if we are capable of transferring files
        scp_capability = await self._ensure_scp_capability(force=force_scp_config)
        if scp_capability is False:
            logger.error("SCP feature is not enabled on device!")
            return result

        _need_to_cleanup = scp_capability

        if not filename:  # default to the current directory with hostname as filename
            filename = self.conn.host + ".conf"

        if not overwrite and Path(filename).exists():
            logger.warning("'%s' file will NOT be overwritten!", filename)
            result.exists = True
            return result

        # transfer the file
        logger.debug("getting config to '%s'", filename)
        try:
            _transferred = await self._async_file_transfer(
                "get",
                "sys_config" if sys_config else "fgt-config",
                filename,
                progress_handler=progress_handler,
                prevent_timeout=prevent_timeout,
                block_size=128000,
            )
            if Path(filename).is_file():  # File created. It's not guaranteed.
                result.transferred = _transferred
                result.exists = True
                logger.debug("'%s' file written", filename)
            else:
                logger.warning("'%s' file not created! Check user rights or use sys_config = False!", filename)
        except Exception as e:
            raise e

        if cleanup and _need_to_cleanup:
            await self._cleanup_after_transfer()

        return result