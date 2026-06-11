# signal-cli-rest-api 0.99 with signal-cli upgraded to 0.14.5.
#
# Temporary hotfix: Signal's servers changed sealed-sender envelope behavior on
# 2026-06-11, making every bundled signal-cli <= 0.14.4.1 throw
# "getServerGuid(...) must not be null" on ALL incoming messages
# (AsamK/signal-cli#2059). 0.14.5 carries the fix; no upstream rest-api image
# bundles it yet. Drop this (revert compose to the upstream pinned image) once
# an official release bundles signal-cli >= 0.14.5 AND the M1 protocol checks
# have been re-run against it.
FROM bbernhard/signal-cli-rest-api:0.99

ADD https://github.com/AsamK/signal-cli/releases/download/v0.14.5/signal-cli-0.14.5.tar.gz /tmp/

RUN tar xzf /tmp/signal-cli-0.14.5.tar.gz -C /opt \
    && rm /tmp/signal-cli-0.14.5.tar.gz \
    && ln -sf /opt/signal-cli-0.14.5/bin/signal-cli /usr/bin/signal-cli \
    && /usr/bin/signal-cli --version
