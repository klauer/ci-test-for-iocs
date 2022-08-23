DOCKER_BUILDKIT ?= 1
IMAGE_NAME ?= ci-test-ioc
IMAGE_VERSION ?= latest
IMAGE ?= $(IMAGE_NAME):$(IMAGE_VERSION)
RUN_ARGS ?= ./build.py ../ads-ioc
INSPECT_ARGS ?= /bin/bash --login
CACHE_ARGS ?= -v $(PWD)/docker-build-cache:/cds/home/username/Repos/pcds-ioc-builder/cache

all: run-build

build-image: Dockerfile
	DOCKER_BUILDKIT=$(DOCKER_BUILDKIT) && \
		docker build --tag $(IMAGE) --file Dockerfile --progress=plain .

run-build: build-image
	mkdir -p docker-build-cache
	docker run -it $(CACHE_ARGS) $(IMAGE) "$(RUN_ARGS)"

inspect: build-image
	mkdir -p docker-build-cache
	docker run -it $(CACHE_ARGS) $(IMAGE) "$(INSPECT_ARGS)"

.PHONY: all build-image run-build inspect
