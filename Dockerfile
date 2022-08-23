# syntax=docker/dockerfile:1.4
# vi: syntax=Dockerfile

# Test one from pcdshub/ioc-machine-core
FROM pcds-ioc:latest

# RUN python3 -m pip install whatrecord

COPY --chown=username ./support/whatrecord /cds/home/username/Repos/whatrecord

WORKDIR /cds/home/username/Repos/whatrecord
RUN python3 -m pip install --user .

WORKDIR ..

COPY --chown=username ads-ioc/ ./ads-ioc

WORKDIR pcds-ioc-builder

COPY --chown=username build.py ./
COPY --chown=username .ci/cue.py ./
COPY --chown=username git-template/ ./git-template

ENTRYPOINT ["/bin/bash", "--login", "-c"]
