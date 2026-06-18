# SyncTalk render-optimization patch (backup)

The SyncTalk server changes live in the **separate** repo
`/home/admin-aifc/SyncTalk_2D` (`synctalk_server.py` + `bench_forward.py`),
whose `origin` is the upstream `ZiqiaoPeng/SyncTalk_2D` (no write access). To
keep the work recoverable, the commit is mirrored here as a patch.

Apply to a fresh SyncTalk_2D clone:

    git -C /path/to/SyncTalk_2D am < deploy/synctalk/0001-*.patch
    # or, to apply without commit metadata:
    git -C /path/to/SyncTalk_2D apply deploy/synctalk/0001-*.patch

Then restart SyncTalk (`bash scripts/start_synctalk.sh`). Flags + rationale:
`OPTIMIZATION_PLAN.md`. Defaults (pipeline + fast composite ON) come from `config.env`.
