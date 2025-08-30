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
``scrapli``, ``scrapli-community``, ``asyncssh``, ``aiofiles``

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

Progress bar example
--------------------

.. code-block:: python

    from rich.progress import Progress

    with Progress(refresh_per_second=100) as progress:
        task = progress.add_task("Getting config...")

        def progress_handler(srcpath: bytes, dstpath: bytes, copied: int, total: int):  # arg signature is important!
            progress.update(task, completed=copied, total=total, description=dstpath.decode())

        async with AsyncScrapli(**device) as conn:
            scp: AsyncSCPFortiOS = AsyncSrapliSCP(conn)
            result = await scp.get_config(
                filename=filename,
                overwrite=True,
                force_scp_config=True,
                progress_handler=progress_handler
            )
    print(result)
