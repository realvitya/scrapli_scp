from rich.logging import RichHandler
from rich.progress import Progress
from scrapli import AsyncScrapli
from scrapli_scp import AsyncSrapliSCP
import asyncio
import logging

logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])
scrapli_logger = logging.getLogger("scrapli")
scrapli_logger.setLevel(logging.WARNING)
asyncssh_logger = logging.getLogger("asyncssh")
asyncssh_logger.setLevel(logging.WARNING)
scrapli_scp_logger = logging.getLogger("scrapli_scp")
scrapli_scp_logger.setLevel(logging.DEBUG)

device = {
    "host": "rtr1",
    "auth_username": "test",
    "auth_password": "test123",
    "auth_strict_key": False,
    "transport": "asyncssh",
    "ssh_config_file": "sshconfig.txt",
    "platform": "cisco_iosxe",
}

filename = "cisco_test.file"


async def main():
    with Progress(refresh_per_second=100) as progress:
        task = progress.add_task("Copy file...")
        def progress_handler(srcpath, dstpath: bytes, copied, total):
            progress.update(task, completed=copied, total=total, description=dstpath.decode())
        async with AsyncScrapli(**device) as conn:
            scp = AsyncSrapliSCP(conn)
            result = await scp.file_transfer("get", src="csr1000v-rpboot.16.12.05.SPA.pkg", dst=filename,
                                             force_scp_config=True, overwrite=True, prevent_timeout=5,
                                             progress_handler=progress_handler)
            result = await scp.file_transfer("get", src="packages.conf", dst=filename,
                                             force_scp_config=True, overwrite=True,
                                             progress_handler=progress_handler)
        print(result)

if __name__ == "__main__":
    asyncio.run(main())
