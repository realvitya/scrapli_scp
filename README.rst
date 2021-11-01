===========
Scrapli SCP
===========
Welcome to Scrapli SCP project!

This project is about to add smart SCP capability to Scrapli based connections.
By smart, I mean various checks before and after the file copy to ensure the file copy is possible
and successful.

These are the checks done by default:

#. checksum
#. existence of file at destination (also with hash)
#. available space at destination
#. scp enablement on device (and tries to turn it on if needed)
#. restore configuration after transfer if it was changed
#. check MD5 after transfer

Requirements
------------
``scrapli``, ``asyncssh``, ``aiofiles``

Installation
------------
.. code-block:: console

    $ pip install scrapli-scp


Simple example
--------------
You can find it in ``test`` folder but the main part:

.. code-block:: python

    async with AsyncScrapli(**device) as conn:
        scp = AsyncSrapliSCP(conn)
        result = await scp.file_transfer("put", src=filename, dst=".", force_scp_config=True)
    print(result)
