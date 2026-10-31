# Channel delivery during a planned restart

Telegram polling uses `drop_pending_updates=False`. The channel configuration
`backlog_max_age_seconds` defaults to 600 and must be a positive integer.
Older messages produce a log entry containing their update ID and age; their
contents are not logged or handed to the Manager.

In python-telegram-bot 22.8, `Updater._start_polling` first awaits
`update_queue.put(update)`, then advances `_last_update_id`. The next
`getUpdates` request acknowledges that offset. Its stop cleanup also issues a
`getUpdates` request at the latest offset; it does not wait for handlers first.
Looper therefore commits each received update before queue insertion to the
runtime directory's `.local/telegram/<channel_id>/<bot_id>.sqlite3`. The `updates` table
uses `update_id` as its primary key. Bot identity namespaces the file so replacing
a bot on an existing channel cannot suppress a different bot's updates; rotating
the same bot's token retains dedupe. Startup replays pending rows. A successful
handler retains the processed key and deletes the message payload. A planned
stop drains the Application handlers before closing this inbox, and a
redelivered processed update never reaches the Manager a second time.

This is a planned restart guarantee, not an atomic transaction with Manager
execution. An abrupt process failure after bus publication but before marking
the inbox row processed can still replay that update. No exactly-once claim is
made for that crash window.

The Lark stand-in platform delivers events only to currently connected clients;
it has no offline queue. `tests/test_lark_restart.py` disconnects the real
WebSocket consumer, emits an event in the gap, reconnects it, and observes only
the subsequent live event. Existing stream tests separately cover dedupe when
the platform explicitly redelivers an event. The `lark-cli` stand-in emits its
configured event list for each consumer invocation, which models explicit
redelivery but cannot establish the real platform's reconnect behavior.

Neither the bot long connection nor `lark-cli event consume` supplies a durable
resume cursor in Looper. The stand-ins do not establish a production guarantee
that Lark replays events missed while disconnected. v0 leaves both transports
unchanged; operators should treat Lark messages sent during the restart gap as
potentially lost until a platform replay guarantee is independently verified.
