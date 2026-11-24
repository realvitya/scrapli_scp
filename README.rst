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
``scrapli<2026.11.12``, ``scrapli-community``, ``asyncssh``, ``aiofiles``

Scrapli 2026.11.12 removed the ``scrapli.driver`` module used by this project.
Scrapli 2025.1.30 and 2026.2.20 have been checked with the regression tests.

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

FortiOS configuration in memory
------------------------------

Pass a writable ``StringIO`` or ``TextIOWrapper`` as ``filename`` to receive the
configuration as text. Downloads use ``asyncssh.scp()`` with an anonymous
memory-backed file on Linux when ``os.memfd_create`` is available. Windows and
systems without that function use a temporary file in a unique directory.
If creating or downloading to the memory file fails, the download is retried
once using a temporary file. Errors writing to the caller's stream still propagate.
UTF-8 decoded text is then copied into the stream. Both approaches isolate
concurrent downloads; use a separate output stream for each call. Internal
storage is released on success, failure, or cancellation. Progress callbacks
receive the internal download path as their destination.
Device setup, transfer, and restoration are serialized on each driver;
separate drivers can download concurrently. Cancellation waits for active file
operations and cleanup to finish before returning, so callers can safely close
their output streams afterward. Device restoration also runs after a failed
or cancelled transfer when ``cleanup=True``.
With temporary files, the configuration briefly exists on disk. Linux memory files can
be swapped to disk by the operating system.
The stream stays open at the end of the written text; ``overwrite`` only applies to paths.
On a failed transfer the stream may contain partial configuration; check
``result.transferred`` before using it.

.. code-block:: python

    from io import StringIO

    async with AsyncScrapli(**device) as conn:
        scp = AsyncSrapliSCP(conn)
        with StringIO() as output:
            result = await scp.get_config(filename=output)
            if result.transferred:
                config = output.getvalue()

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
