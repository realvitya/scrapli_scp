"""scrapli_scp.asyncscp.base"""
import asyncio
import hashlib
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import PurePath
from time import time
from typing import Callable, Literal, Optional, TypedDict, Union
from scrapli.driver.network import AsyncNetworkDriver

import aiofiles
import asyncssh
from asyncssh import SSHClientConnectionOptions, connect, scp
from scrapli_scp.logging import logger


@dataclass()
class FileCheckResult:
    """
    hash - hash value string (empty on error)

    size - size in bytes

    free - free space in bytes (0 on error)
    """

    hash: str
    size: int
    free: int


@dataclass()
class SCPConnectionParameterType(TypedDict):
    """
    Collection of authentication data needed to open a second SCP connection to the device.

    username: SSH username

    password: SSH password

    host: SSH host

    port: SSH port

    options: current SSH connection options
    """

    username: str
    password: str
    host: str
    port: int
    options: SSHClientConnectionOptions


@dataclass()
class FileTransferResult:
    """
    exists - True if destination existed or created

    transferred - True if file was transferred

    verified - True if files are identical (hashes match)
    """

    exists: bool
    transferred: bool
    verified: bool


class AsyncSCPFeature(ABC):
    """
    This class extends a driver with SCP capabilities

    You need to implement device specific methods. If your device does not support that method,
    just return a value described in the abstract methods.
    """

    def __init__(self, connection: AsyncNetworkDriver):
        # \x0C is CTRL-L which usually refresh the prompt and harmless to send as keepalive
        self.keepalive_pattern = "\x0C".encode("UTF-8")
        self.conn = connection

    @abstractmethod
    async def check_device_file(self, device_fs: Optional[str], file_name: str) -> FileCheckResult:
        """
        Check remote file and storage space
        Returning empty hash means error accessing the file

        Args:
            device_fs: filesystem on device (e.g. disk0:/)
            file_name: file to examine

        Returns:
            FileCheckResult
        """
        ...

    @abstractmethod
    async def _ensure_scp_capability(self, force: Optional[bool] = False) -> Union[bool, None]:
        """
        Ensure device is capable of using scp.

        Args:
            force: Try reconfigure device if it doesn't support scp. If set to `None`, don't check
                   anything.

        Returns:
            bool: `True` if device supports scp now and we changed configuration.
                  `False` if device does not support scp or we didn't force configuration which was
                          needed.
                  `None` if we are good to proceed or we didn't check at all.
        """
        ...

    @abstractmethod
    async def _cleanup_after_transfer(self) -> None:
        """
        Device specific cleanup procedure if needed. Useful to restore configuration in case
        _ensure_scp_capability reconfigured the device.

        Returns:
            None
        """
        ...

    @abstractmethod
    async def _get_device_fs(self) -> Optional[str]:
        """
        Device specific drive detection.

        Returns:
            Drive as a string. E.g. disk0:/ or flash0:/
            `None`, if drive not detected or detection is not supported
        """
        ...

    @staticmethod
    async def check_local_file(device_fs: Optional[str], file_name: str) -> FileCheckResult:
        """
        Check local file and storage space

        Args:
            device_fs: If specified, this path will be checked for free space. Else path will be
                       taken from `file_name`
            file_name: local file to examine. This should be the full path of local file

        Returns:
            FileCheckResult
        """
        try:
            async with aiofiles.open(file_name, "rb") as f:
                file_hash = hashlib.md5(await f.read()).hexdigest()
                logger.debug(f"'{file_name}' hash is '{file_hash}'")
            file_size = os.path.getsize(file_name)
        except FileNotFoundError:
            file_size = 0
            file_hash = ""
        try:
            path = device_fs if device_fs else os.path.dirname(file_name)
            # check free space of directory of the file or the local dir
            free_space = shutil.disk_usage(path if path else ".").free
        except FileNotFoundError:
            free_space = 0
        return FileCheckResult(hash=file_hash, size=file_size, free=free_space)

    async def _async_file_transfer(  # noqa: C901
        self,
        operation: Literal["get", "put"],
        src: str,
        dst: str,
        progress_handler: Optional[Callable] = None,
        prevent_timeout: Optional[float] = None,
    ) -> bool:
        """
        SCP a file from device to localhost

        Args:
            operation: 'get' or 'put' files from or to the device
            src: Source file name
            dst: Destination file name
            progress_handler: scp callback function to be able to follow the copy progress
            prevent_timeout: interval in seconds when we send an empty command to keep SSH channel
                             up, 0 to turn it off,
                             default is same as `timeout_ops`

        Returns:
            bool: True on success
        """

        start_time = 0.0
        if prevent_timeout is None:
            prevent_timeout = self.conn.timeout_ops

        async def _prevent_timeout():
            """Send enter to idle SSH channel to prevent timing out while transferring file"""
            logger.debug("Sending keepalive to device")
            self.conn.transport.write(self.keepalive_pattern)

        def timed_progress_handler(srcpath, dstpath, copied, total):
            """Progress handler wrapper which prevents timeouts while file transfer"""
            nonlocal start_time

            now = time()
            if 0 < prevent_timeout <= (now - start_time):
                logger.debug("Preventing timeout")
                asyncio.ensure_future(_prevent_timeout())
                start_time = now

            # call original handler if specified
            if progress_handler:
                progress_handler(srcpath, dstpath, copied, total)

        # noinspection PyProtectedMember
        scp_options = SCPConnectionParameterType(
            username=self.conn.auth_username,
            password=self.conn.auth_password,
            port=self.conn.port,
            host=self.conn.host,
            options=self.conn.transport.session._options,  # noqa: W0212
        )
        result = False
        try:
            async with connect(**scp_options) as scp_conn:
                start_time = time()
                if operation == "get":
                    await scp(
                        (scp_conn, src),
                        dst,
                        progress_handler=timed_progress_handler,
                        block_size=65536,
                    )
                elif operation == "put":
                    await scp(
                        src,
                        (scp_conn, dst),
                        progress_handler=timed_progress_handler,
                        block_size=65536,
                    )
                else:
                    raise ValueError(f"Invalid operation: {operation}")
        except (asyncssh.SFTPError,) as e:
            result = False
            logger.warning(f"SCP error: {e}")
        except Exception as e:
            result = False
            logger.warning(f"Other error: {e}")
            raise e
        else:
            result = True

        return result

    async def file_transfer(  # noqa: C901
        self,
        operation: Literal["get", "put"],
        src: str,
        dst: str = "",
        verify: bool = True,
        device_fs: Optional[str] = None,
        overwrite: bool = False,
        force_scp_config: bool = False,
        cleanup: bool = True,
        progress_handler: Optional[Callable] = None,
        prevent_timeout: Optional[float] = None,
    ) -> FileTransferResult:
        """SCP for network devices

        This transfer is idempotent and does the following checks before/after transfer:

        #. | checksum
        #. | existence of file at destination (also with hash)
        #. | available space at destination
        #. | scp enablement on device (and tries to turn it on if needed)
        #. | restore configuration after transfer if it was changed
        #. | check MD5 after transfer

        The file won't be transferred if the hash of the files on local/device are the same!

        Args:
            operation: put/get file to/from device
            src: source file name
            dst: destination file name (same as src if omitted)
            verify: `True` if verification is needed (checksum, file existence, disk space)
            device_fs: IOS device filesystem (autodetect if empty)
            overwrite: If set to `True`, destination will be overwritten in case hash verification
                       fails
                       If set to `False`, destination file won't be overwritten.
                       Beware: turning off `verify` will make this parameter ignored and destination
                        will be overwritten regardless! (Logic is that if user does not care about
                        checking, just copy it over)
            force_scp_config: If set to `True`, SCP function will be enabled in device configuration
                               before transfer.
                              If set to `False`, SCP functionality will be checked but won't
                              configure the device.
                              If set to `None`, capability won't even checked.
            cleanup: If set to True, call the cleanup procedure to restore configuration if it was
                     altered
            progress_handler: function to call by file copy (used by asyncssh.scp function)
            prevent_timeout: interval in seconds when we send an empty command to keep SSH channel
                             up, 0 to turn it off, default is same as `timeout_ops`

        Returns:
            FileTransferResult
        """

        result = FileTransferResult(False, False, False)
        src_file_data = FileCheckResult("", 0, 0)
        dst_file_data = FileCheckResult("", 0, 0)
        if prevent_timeout is None:
            prevent_timeout = self.conn.timeout_ops
        dst_device_fs: Optional[str] = None
        src_device_fs: Optional[str] = None

        # set destination filename to source if missing
        if dst in ("", "."):
            # set destination to filename and strip all path
            dst = PurePath(src).name

        # Detect default filesystem the device use
        if not device_fs:
            device_fs = await self._get_device_fs()

        if operation == "get":
            src_check = self.check_device_file
            src_device_fs = device_fs
            dst_check = self.check_local_file
            logger.info(f"Downloading '{src_device_fs+src}' as '{dst}'")
        elif operation == "put":
            src_check = self.check_local_file
            dst_check = self.check_device_file
            dst_device_fs = device_fs
            logger.info(f"Uploading '{src}' as '{dst_device_fs+dst}'")
        else:
            raise ValueError(f"Operation '{operation}' is not supported")
        if verify:
            # gather info on source side
            src_file_data = await src_check(src_device_fs, src)
            logger.debug(f"Source file '{src}': {src_file_data}")
            if not src_file_data.hash:
                # source file cannot be found, we are done here
                logger.warning(f"Source file '{src}' does NOT exists!")
                return result
            # gather info on destination file
            dst_file_data = await dst_check(dst_device_fs, dst)
            logger.debug(f"Destination file '{dst}': {dst_file_data}")
            # check if destination file exists
            if dst_file_data.hash:
                result.exists = True
            # check if destination file has the same hash as source
            if dst_file_data.hash and src_file_data.hash == dst_file_data.hash:
                result.verified = True
                # no need to transfer file
                logger.debug(f"'{dst}' file already exists at destination and verified OK")
                return result

            # if hash does not match and we want to overwrite
            if dst_file_data.hash and not overwrite:
                logger.warning(f"'{dst}' file will NOT be overwritten!")
                return result

            # check if we have enough free space to transfer the file
            if dst_file_data.free < src_file_data.size:
                logger.warning(
                    f"'{dst}' file is too big ({src_file_data.size}). Destination free space: "
                    f"{dst_file_data.free}"
                )
                return result

        # check if we are capable of transferring files
        scp_capability = await self._ensure_scp_capability(force=force_scp_config)
        if scp_capability is False:
            logger.error("SCP feature is not enabled on device!")
            return result

        _need_to_cleanup = scp_capability

        # transfer the file
        try:
            _transferred = await self._async_file_transfer(
                operation,
                src,
                dst,
                progress_handler=progress_handler,
                prevent_timeout=prevent_timeout,
            )
            result.transferred = _transferred
        except Exception as e:
            raise e

        # clean up if needed
        if cleanup and _need_to_cleanup:
            await self._cleanup_after_transfer()

        if verify:
            # check destination file after copy
            dst_file_data = await dst_check(dst_device_fs, dst)
            # check if file was created
            if dst_file_data.hash:
                result.exists = True
            # check if file has the same hash as source
            if dst_file_data.hash and dst_file_data.hash == src_file_data.hash:
                result.verified = True
            else:
                logger.warning(f"'{dst}' failed hash verification!")
        else:
            # assuming transfer created the file even if we did not check hash
            if result.transferred:
                result.exists = True
        return result
