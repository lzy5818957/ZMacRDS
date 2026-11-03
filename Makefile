.PHONY: probe
probe: build/session-probe

build/session-probe: tools/session-probe.m
	@mkdir -p build
	xcrun clang -Wall -Wextra -Werror -fobjc-arc -framework Foundation -framework ApplicationServices -lbsm $< -o $@
