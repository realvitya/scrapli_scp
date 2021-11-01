from scrapli import AsyncScrapli
from scrapli_scp import AsyncSrapliSCP
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
scrapli_logger = logging.getLogger("scrapli")
scrapli_logger.setLevel(logging.WARNING)
asyncssh_logger = logging.getLogger("asyncssh")
asyncssh_logger.setLevel(logging.WARNING)
scrapli_scp_logger = logging.getLogger("scrapli_scp")
scrapli_scp_logger.setLevel(logging.DEBUG)

device = {
    "host": "172.16.255.100",
    "auth_username": "admin",
    "auth_password": "test123",
    "auth_strict_key": False,
    "transport": "asyncssh",
    "ssh_config_file": "sshconfig.txt",
    "platform": "cisco_iosxe",
}

filename = "e:/download/mr.pdf"


async def main():
    async with AsyncScrapli(**device) as conn:
        scp = AsyncSrapliSCP(conn)
        result = await scp.file_transfer("put", src=filename, dst="test.pdf", force_scp_config=True)
    print(result)

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
