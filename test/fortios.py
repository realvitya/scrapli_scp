"""scrapli_scp.test.fortios"""

from scrapli import AsyncScrapli
from scrapli_scp import AsyncSrapliSCP
from scrapli_scp.asyncscp.fortinet_fortios import AsyncSCPFortiOS
import asyncio
import logging
from rich.progress import Progress

logging.basicConfig(level=logging.INFO)
scrapli_logger = logging.getLogger("scrapli")
scrapli_logger.setLevel(logging.WARNING)
asyncssh_logger = logging.getLogger("asyncssh")
asyncssh_logger.setLevel(logging.WARNING)
scrapli_scp_logger = logging.getLogger("scrapli_scp")
scrapli_scp_logger.setLevel(logging.INFO)

device = {
    "host": "fg1",
    "auth_username": "test_admin",
    "auth_password": "test123",
    "auth_strict_key": False,
    "transport": "asyncssh",
    "ssh_config_file": "sshconfig.txt",
    "platform": "fortinet_fortios",
}

filename = ""


async def main():
    with Progress(refresh_per_second=100) as progress:
        task = progress.add_task("Getting config...")
        def progress_handler(srcpath, dstpath: bytes, copied, total):
            progress.update(task, completed=copied, total=total, description=dstpath.decode())
        async with AsyncScrapli(**device) as conn:
            scp: AsyncSCPFortiOS = AsyncSrapliSCP(conn)
            result = await scp.get_config(filename=filename, overwrite=True, force_scp_config=True, progress_handler=progress_handler)
    print(result)

if __name__ == "__main__":
    asyncio.run(main())
