"""常量与正则：Sentry 证据抓取的默认值、附件名格式、失败标记。

这里只放"没有副作用、谁都能借"的字面量；改上限等于改行为，改前先看 README 的取舍。
"""

from __future__ import annotations

import re

EVIDENCE_SCHEMA = 1


EVIDENCE_KIND = "sentry-evidence"


DEFAULT_OUTDIR = "temp/evidence"


DEFAULT_PERIOD = "7d"


DEFAULT_LIMIT = 20


DEFAULT_LOG_LIMIT = 1000


DEFAULT_MAX_EVENTS = 10


MAX_LOG_LIMIT = 1000


LOG_SORTS = ("newest", "oldest")


HTTP_TIMEOUT_SECONDS = 60


SENTRY_HOST = "https://sentry.io"


ATTACHMENT_NAME_RE = re.compile(
    r"^mfa-task-failure-([0-9a-f]+)-(\d+)-screenshots\.zip$", re.IGNORECASE
)


MARKER_RES = (
    re.compile(r"Task timeout \[pretask\.name=([^\]]+)\]"),
    re.compile(r"invalid node id, handle error \[node\.name=([^\]]+)\]"),
    re.compile(r"node not completed, handle error \[node\.name=([^\]]+)\]"),
)
