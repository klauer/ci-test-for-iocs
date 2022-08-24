# syntax=docker/dockerfile:1.4
# vi: syntax=Dockerfile

# Test one from pcdshub/ioc-machine-core
FROM pcds-ioc:latest

RUN sudo yum -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-7.noarch.rpm \
    && \
    sudo yum update -y \
    && \
    sudo yum -y install \
      `# required for the sequencer:` \
      re2c \
      `# required for asyn:` \
      rpcgen libtirpc-devel \
    && \
    sudo yum -y clean all

COPY --chown=username ./support/whatrecord /cds/home/username/Repos/whatrecord

WORKDIR /cds/home/username/Repos/whatrecord
RUN python3 -m pip install --user .

WORKDIR ..

COPY --chown=username ads-ioc/ ./ads-ioc

WORKDIR pcds-ioc-builder

COPY --chown=username build.py ./
COPY --chown=username .ci/cue.py ./
COPY --chown=username git-template/ ./git-template

RUN echo 'whatrecord deps . -d EPICS_BASE=$EPICS_BASE EPICS_MODULES=$EPICS_MODULES EPICS_SITE_TOP=$EPICS_SITE_TOP' >> ~/.bash_history
RUN echo './build.py ../ads-ioc' >> ~/.bash_history

ENTRYPOINT ["/bin/bash", "--login", "-c"]
