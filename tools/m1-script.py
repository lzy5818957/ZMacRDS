#!/usr/bin/env python3
"""Build bounded cross-application experiment requests from a current agent lease."""
import argparse
import json
from pathlib import Path

KEYS = dict(zip("abcdefghijklmnopqrstuvwxyz", [0,11,8,2,14,3,5,4,34,38,40,37,46,45,31,35,12,15,1,17,32,9,13,7,16,6]))


def request(state, case, mode, count):
    actions = []
    def add(kind, **values): actions.append(dict(type=kind, **values))
    add("focus", target="editor")
    add("delay", ms=1200)
    add("checkpoint", label="editor-focused", focus="editor")
    add("source", id="com.apple.inputmethod.SCIM.ITABC" if mode == "pinyin" else "com.apple.keylayout.ABC")
    add("delay", ms=1500)
    if mode == "resume":
        add("checkpoint", label="retained-document", text=f"用户{state['uid']}：中文{count:03d}🌍", focus="editor")
        add("key", code=124)
        add("text", text="，恢复成功")
        add("delay", ms=1200)
        add("checkpoint", label="resume-edited", text=f"用户{state['uid']}：中文{count:03d}🌍，恢复成功", focus="editor")
    else:
        for i in range(1, count + 1):
            if mode == "unicode" and i % 10 == 0:
                add("focus", target="agent")
                add("delay", ms=400)
                add("checkpoint", label=f"agent-{i}", focus="agent")
                add("focus", target="editor")
                add("delay", ms=400)
                add("checkpoint", label=f"editor-{i}", focus="editor")
            add("key", code=0, command=True)
            if mode == "pinyin":
                for word in ["nihao", "shijie"]:
                    for ch in word: add("key", code=KEYS[ch])
                    add("delay", ms=800)
                    add("key", code=49)
                    add("delay", ms=800)
                expected = "你好世界"
            else:
                expected = f"用户{state['uid']}：中文{i:03d}🌍"
                add("text", text=expected)
                add("drag")
            add("delay", ms=400)
            add("checkpoint", label=f"{mode}-{i}", text=expected, focus="editor", selection_min=1 if mode == "unicode" else 0)
    return {**{k: state[k] for k in ("uid", "audit_session_id", "boot_id", "nonce", "epoch")}, "case_id": case, "actions": actions}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("case")
    parser.add_argument("mode", choices=["unicode", "pinyin", "resume"])
    parser.add_argument("--count", type=int, default=100)
    args=parser.parse_args()
    if not 1 <= args.count <= 100: parser.error("count must be 1..100")
    print(json.dumps(request(json.loads(args.state.read_text()),args.case,args.mode,args.count),ensure_ascii=False))


if __name__ == "__main__": main()
