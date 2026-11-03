MACOS_MIN = 13.0
ARCH := $(shell uname -m)
KIT = build/ZMacRDS-M0-$(ARCH).tar.gz

.PHONY: probe m0 m0-kit
probe: build/session-probe

m0: build/m0 build/session-probe build/m0-verify

build/session-probe: tools/session-probe.m
	@mkdir -p build
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -fobjc-arc -framework Foundation -framework ApplicationServices -framework SystemConfiguration -lbsm $< -o $@

build/m0-verify: tools/m0-verify.swift
	@mkdir -p build
	xcrun swiftc -target $(ARCH)-apple-macosx$(MACOS_MIN) -O $< -o $@

build/m0: tools/m0
	@mkdir -p build
	install -m 755 $< $@

m0-kit: $(KIT)

$(KIT): build/m0 build/session-probe build/m0-verify docs/M0_TEST.md
	COPYFILE_DISABLE=1 tar -czf $@.tmp -C build m0 session-probe m0-verify -C "$(CURDIR)/docs" M0_TEST.md
	mv $@.tmp $@
