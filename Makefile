DOCKER_BUILDKIT=1
IMAGE_NAME:=ci-test-ioc
IMAGE_VERSION:=ci-test-ioc
IMAGE:=$(IMAGE_NAME):$(IMAGE_VERSION)
BASE:=pcds-epics-base:latest

all: run-ioc

build-ioc: Dockerfile
	DOCKER_BUILDKIT=$(DOCKER_BUILDKIT) && \
			docker build --tag $(IMAGE) --file Dockerfile --progress=plain .

run-ioc: build-ioc
	docker run -it $(RUN_ARGS) -v $(PWD)/:/cds/home/username/builder $(IMAGE)

.PHONY: build-ioc run-ioc all
