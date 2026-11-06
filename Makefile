MACOS_MIN = 13.0
SIGN_IDENTITY ?= -
ARCH := $(shell uname -m)
KIT = build/ZMacRDS-M0-$(ARCH).tar.gz
M3_KIT = build/ZMacRDS-M3-$(ARCH).tar.gz

.PHONY: probe m0 m0-kit m1 m1-kit m3 m3-kit vnc-kit audio-probe management

.PHONY: release
release: management build/ZMacRDS-Desktop.app/Contents/MacOS/M1Agent
	cargo build --release --locked -p zmacrds-rdp-probe -p zmacrds-rdp-check
	"$(VNC_RUNTIME)/bin/python3.13" tools/build-release.py --runtime "$(VNC_RUNTIME)" --sign-identity "$(SIGN_IDENTITY)"

management: build/zmacrds-control build/ZMacRDS-Manager.app/Contents/MacOS/ZMacRDSManager

build/zmacrds-control: tools/service-controller.m tools/service-protocol.h Makefile
	@mkdir -p build
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -fobjc-arc -O2 -framework Foundation -framework Security $< -o $@
	codesign --force --sign "$(SIGN_IDENTITY)" --timestamp=none --identifier org.zmacrds.control $@

build/ZMacRDS-Manager.app/Contents/MacOS/ZMacRDSManager: tools/service-manager.m tools/service-protocol.h tools/Manager-Info.plist Makefile
	@mkdir -p build/ZMacRDS-Manager.app/Contents/MacOS
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -fobjc-arc -O2 -framework AppKit -framework Security $< -o $@
	cp tools/Manager-Info.plist build/ZMacRDS-Manager.app/Contents/Info.plist
	codesign --force --sign "$(SIGN_IDENTITY)" --timestamp=none --identifier org.zmacrds.manager build/ZMacRDS-Manager.app

audio-probe: build/ZMacRDS-Audio-Probe.app/Contents/MacOS/AudioProbe

build/ZMacRDS-Audio-Probe.app/Contents/MacOS/AudioProbe: tools/audio-probe.m tools/AudioProbe-Info.plist
	@mkdir -p build/ZMacRDS-Audio-Probe.app/Contents/MacOS
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -fobjc-arc -O2 -framework AppKit -framework CoreAudio -framework AudioToolbox $< -o $@
	cp tools/AudioProbe-Info.plist build/ZMacRDS-Audio-Probe.app/Contents/Info.plist
	codesign --force --sign - --identifier org.zmacrds.audio-probe build/ZMacRDS-Audio-Probe.app

vnc-kit:
	tools/package-vnc "$(VNC_RUNTIME)"

build/m2-transport-test: tools/m2-transport-test.m tools/m2-bridge.h tools/m1-common.h tools/m1-capture.h
	@mkdir -p build
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -Wno-deprecated-declarations -fobjc-arc -O2 -framework AppKit -framework CoreVideo -framework ApplicationServices -framework SystemConfiguration -framework Carbon -lbsm $< -o $@
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

m1: build/ZMacRDS-M1.app/Contents/MacOS/M1Agent build/ZMacRDS-M1-Editor.app/Contents/MacOS/M1Editor build/m1 build/install-m1 build/display-probe

build/display-probe: tools/display-probe.m
	@mkdir -p build
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -fobjc-arc -framework Foundation -framework ApplicationServices $< -o $@

build/ZMacRDS-M1.app/Contents/MacOS/M1Agent: tools/m1-agent.m tools/m1-marker.h tools/m1-common.h tools/m1-capture.h tools/m1-script.inc tools/m2-bridge.h tools/m1-audio-tap.h tools/service-command.h tools/service-protocol.h tools/M1Agent-Info.plist
	@mkdir -p build/ZMacRDS-M1.app/Contents/MacOS
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -Wno-deprecated-declarations -fobjc-arc -O2 -framework AppKit -framework Carbon -framework ScreenCaptureKit -framework CoreMedia -framework AudioToolbox -framework CoreAudio -framework AVFoundation -framework CoreVideo -framework ApplicationServices -framework SystemConfiguration -lbsm tools/m1-agent.m -o $@
	cp tools/M1Agent-Info.plist build/ZMacRDS-M1.app/Contents/Info.plist
	codesign --force --sign - --identifier org.zmacrds.m1-agent build/ZMacRDS-M1.app

build/ZMacRDS-M1-Editor.app/Contents/MacOS/M1Editor: tools/m1-editor.m tools/m1-common.h tools/m1-marker.h tools/M1Editor-Info.plist
	@mkdir -p build/ZMacRDS-M1-Editor.app/Contents/MacOS
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -Wno-deprecated-declarations -fobjc-arc -O2 -framework AppKit -framework Carbon -framework ApplicationServices -framework SystemConfiguration -lbsm tools/m1-editor.m -o $@
	cp tools/M1Editor-Info.plist build/ZMacRDS-M1-Editor.app/Contents/Info.plist
	codesign --force --sign - --identifier org.zmacrds.m1-editor build/ZMacRDS-M1-Editor.app

build/ZMacRDS-M1-Secondary.app/Contents/MacOS/M1Editor: build/ZMacRDS-M1-Editor.app/Contents/MacOS/M1Editor
	@mkdir -p build/ZMacRDS-M1-Secondary.app/Contents/MacOS
	install -m 755 $< $@
	cp tools/M1Editor-Info.plist build/ZMacRDS-M1-Secondary.app/Contents/Info.plist
	plutil -replace CFBundleIdentifier -string org.zmacrds.m1-secondary build/ZMacRDS-M1-Secondary.app/Contents/Info.plist
	codesign --force --sign - --identifier org.zmacrds.m1-secondary build/ZMacRDS-M1-Secondary.app

build/m3-motion: tools/m3-motion.m tools/m1-marker.h
	@mkdir -p build
	xcrun clang -mmacosx-version-min=$(MACOS_MIN) -Wall -Wextra -Werror -fobjc-arc -O2 -framework AppKit $< -o $@

build/m1: tools/m1
	@mkdir -p build
	install -m 755 $< $@

build/install-m1: tools/install-m1
	@mkdir -p build
	install -m 755 $< $@

m1-kit: m1 docs/M1_TEST.md
	COPYFILE_DISABLE=1 tar -czf build/ZMacRDS-M1-$(ARCH).tar.gz.tmp -C build ZMacRDS-M1.app ZMacRDS-M1-Editor.app m1 install-m1 display-probe -C "$(CURDIR)/docs" M1_TEST.md
	mv build/ZMacRDS-M1-$(ARCH).tar.gz.tmp build/ZMacRDS-M1-$(ARCH).tar.gz

# Give the interactive desktop agent its own privacy entry; historical M1
# diagnostic builds use a different ad-hoc signing requirement.
build/ZMacRDS-Desktop.app/Contents/MacOS/M1Agent: build/ZMacRDS-M1.app/Contents/MacOS/M1Agent Makefile
	@mkdir -p build/ZMacRDS-Desktop.app/Contents/MacOS
	install -m 755 $< $@
	cp build/ZMacRDS-M1.app/Contents/Info.plist build/ZMacRDS-Desktop.app/Contents/Info.plist
	plutil -replace CFBundleIdentifier -string org.zmacrds.desktop build/ZMacRDS-Desktop.app/Contents/Info.plist
	plutil -replace CFBundleName -string "ZMacRDS Desktop" build/ZMacRDS-Desktop.app/Contents/Info.plist
	plutil -replace CFBundleDisplayName -string "ZMacRDS Desktop" build/ZMacRDS-Desktop.app/Contents/Info.plist
	plutil -replace CFBundleShortVersionString -string "0.3.0" build/ZMacRDS-Desktop.app/Contents/Info.plist
	plutil -replace NSScreenCaptureUsageDescription -string "将当前登录账号自己的桌面发送给已验证的远程连接。" build/ZMacRDS-Desktop.app/Contents/Info.plist
	codesign --force --sign "$(SIGN_IDENTITY)" --timestamp=none --identifier org.zmacrds.desktop build/ZMacRDS-Desktop.app

m3:
	cargo build --locked -p zmacrds-rdp-probe -p zmacrds-rdp-check

# Explicit inputs: no run directories, credentials, keys or desktop pixels.
m3-kit: m1 audio-probe management build/ZMacRDS-Desktop.app/Contents/MacOS/M1Agent build/ZMacRDS-M1-Secondary.app/Contents/MacOS/M1Editor build/m3-motion
	cargo build --release --locked -p zmacrds-rdp-probe -p zmacrds-rdp-check
	COPYFILE_DISABLE=1 tar -czf $(M3_KIT).tmp \
	  -C "$(CURDIR)/build" ZMacRDS-Desktop.app ZMacRDS-Manager.app zmacrds-control ZMacRDS-Audio-Probe.app ZMacRDS-M1.app ZMacRDS-M1-Editor.app ZMacRDS-M1-Secondary.app m3-motion m1 install-m1 display-probe \
	  -C "$(CURDIR)/docs" M1_TEST.md \
	  -C "$(CURDIR)/build/cargo/release" zmacrds-rdp-probe zmacrds-rdp-check \
	  -C "$(CURDIR)" LICENSE Cargo.lock docs tools/start-desktop tools/install-service.py tools/service-control.py tools/install-tunnel-guest.py tools/install-tunnel-host.py tools/m3-case.py tools/m3-run.py tools/m3-verify.py \
	    config/ZMacRDS-fixed.rdp vendor/ironrdp-server/LICENSE-MIT vendor/ironrdp-server/LICENSE-APACHE vendor/ironrdp-server/ZMACRDS_PATCH.md \
	    vendor/ironrdp-nscodec/LICENSE-MIT vendor/ironrdp-nscodec/LICENSE-APACHE vendor/ironrdp-nscodec/ZMACRDS_PATCH.md
	mv $(M3_KIT).tmp $(M3_KIT)
