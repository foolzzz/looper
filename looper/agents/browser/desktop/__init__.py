"""Desktop RPC contract, client, and in-memory driver.

Importing this package does not listen or start a process. The socket exists
only when a caller passes ``enabled=True`` to the endpoint, which the service
does not do.
"""
