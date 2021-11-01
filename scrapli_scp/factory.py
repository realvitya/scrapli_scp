"""scrapli_scp.factory"""
from scrapli.driver.network import AsyncNetworkDriver, NetworkDriver
from scrapli.driver.core import (
    AsyncIOSXEDriver,
)

from scrapli_scp.asyncscp.cisco_iosxe import AsyncSCPIOSXE
from scrapli_scp.asyncscp.base import AsyncSCPFeature
from scrapli_scp.exceptions import ScrapliSCPException

ASYNC_CORE_PLATFORM_MAP = {
    AsyncIOSXEDriver: AsyncSCPIOSXE,
}


def AsyncSrapliSCP(conn: AsyncNetworkDriver) -> "AsyncSCPFeature":
    if isinstance(conn, NetworkDriver):
        raise ScrapliSCPException(
            "provided scrapli connection is sync but using 'AsyncScrapliCfg' -- you must use an "
            "async connection with 'AsyncScrapliCfg'!"
        )
    platform_class = ASYNC_CORE_PLATFORM_MAP.get(type(conn))
    if not platform_class:
        raise ScrapliSCPException(
            f"scrapli connection object type '{type(conn)}' not a supported scrapli-scp type"
        )
    final_platform: "AsyncSCPFeature" = platform_class(conn)

    return final_platform
