DOCKER_BUILDKIT=1
IMAGE_NAME:=ci-test-ioc
IMAGE_VERSION:=latest
IMAGE:=$(IMAGE_NAME):$(IMAGE_VERSION)
BASE:=pcds-epics-base:latest
RUN_ARGS:=python3 build.py ../ads-ioc

all: run-ioc

build-ioc: Dockerfile
	DOCKER_BUILDKIT=$(DOCKER_BUILDKIT) && \
		docker build --tag $(IMAGE) --file Dockerfile --progress=plain .

run-ioc: build-ioc
	mkdir -p docker-build-cache
	docker run -it \
		-v $(PWD)/docker-build-cache:/cds/home/username/Repos/pcds-ioc-builder/cache \
		$(IMAGE) \
		"$(RUN_ARGS)"

.PHONY: build-ioc run-ioc all
