#!/usr/bin/env python3
"""Verify cross-process text editing, IME composition and bounded reconnect evidence."""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path

SPEC=importlib.util.spec_from_file_location("capture",Path(__file__).with_name("m1-verify.py"))
CAPTURE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)
require=CAPTURE.require


def read(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def verify_editor(rows, agent):
    starts=[r for r in rows if r["kind"]=="start"]
    require(len(starts)==1,"one independent editor process required")
    first=starts[0]
    require(first["pid"]!=agent.first["pid"],"editor must be a separate process")
    for r in rows:
        require(all(r[k]==first[k] for k in ("pid","nonce","uid","audit_session_id","boot_id","case_id")),"editor process/session changed")
        require(all(r[k]==agent.first[k] for k in ("uid","audit_session_id","boot_id")),"editor belongs to wrong session")
        require(r["gui_uid"]==r["uid"] and r["login_complete"],"editor GUI identity invalid")
    return first


def verify_cross(agents, editors, rounds=100):
    for i,(agent,editor) in enumerate(zip(agents,editors)):
        editor_first=verify_editor(editor,agent)
        own_events=[r for r in agent.rows if r["kind"]=="input_received"]+[r for r in editor if r["kind"]=="event"]
        other_events=[r for r in agents[1-i].rows if r["kind"]=="input_received"]+[r for r in editors[1-i] if r["kind"]=="event"]
        posts=[r for r in agent.rows if r["kind"]=="script_post"]
        require(posts,"no cross-app input posted")
        tags={r["tag"] for r in posts}
        require(len(tags)==len(posts),"script tags reused")
        require(all(r["route"]=="kCGSessionEventTap" for r in posts),"PID-directed input is not desktop input")
        require(not any(r["tag"] in tags for r in other_events),"cross-user input delivery")
        receipts=[r for r in own_events if r["tag"] in tags]
        counts=Counter(r["tag"] for r in receipts)
        require(all(n==1 for n in counts.values()),"duplicate script receipts")
        # NSView's default first-click handling activates an inactive window
        # without forwarding that click to mouseDown/mouseUp. The editor has
        # an app-local event monitor; the agent records view callbacks only.
        # Permit this one case only with a timely, successful focus checkpoint.
        activation_tags=set();action=None
        for row in agent.rows:
            if row["kind"]=="script_started":action=None
            elif row["kind"]=="script_action":action=row["action"]
            elif row["kind"]=="script_post" and action=={"type":"focus","target":"agent"}:
                if row["event_type"] in (1,2) and any(
                    c["kind"]=="checkpoint" and c["label"].startswith("agent-") and c["ok"]
                    and c["frontmost_pid"]==agent.first["pid"]
                    and 0<c["uptime_ms"]-row["uptime_ms"]<=2000 for c in agent.rows):
                    activation_tags.add(row["tag"])
        require(tags-counts.keys()<=activation_tags,"missing script receipts")
        posted={r["tag"]:r for r in posts}
        for r in receipts:
            p=posted[r["tag"]]
            require(r["event_type"]==p["event_type"],"input event type changed")
            require(0<=r["uptime_ms"]-p["uptime_ms"]<=2000,"input late or out of order")
        require(not any(r["kind"]=="script_aborted" for r in agent.rows),"script aborted")
        starts=[r for r in agent.rows if r["kind"]=="script_started"]
        finishes=[r for r in agent.rows if r["kind"]=="script_finished"]
        require(starts and len(starts)==len(finishes),"script did not finish")
        for start,end in zip(starts,finishes):
            require(start["uptime_ms"]<end["uptime_ms"],"invalid script interval")
            hbs=[r for r in agent.heartbeats if start["uptime_ms"]<=r["uptime_ms"]<=end["uptime_ms"]]
            require(hbs and all(r["console_uid"]==start["console_uid"] for r in hbs),"console changed during script")
            require(all(r["accessibility_trusted"] and r["connected"] for r in hbs),"input without permissions or connection")
            require(start["editor_pid"]==editor_first["pid"] and start["editor_nonce"]==editor_first["nonce"],"wrong editor instance")
        checkpoints=[r for r in agent.rows if r["kind"]=="checkpoint"]
        for r in checkpoints:
            require(r["ok"] is True,"checkpoint failed")
            require(r["editor"]["pid"]==editor_first["pid"] and r["editor"]["uid"]==agent.uid,"checkpoint wrong editor")
            # Independent editor log must corroborate the saved document.
            require(any(e["kind"]=="document" and e["text"]==r["editor"]["text"] and 0<=r["uptime_ms"]-e["uptime_ms"]<=3500 for e in editor),"checkpoint text not corroborated")
        for console in [a.uid for a in agents]:
            unicode_checks=[r for r in checkpoints if r["console_uid"]==console and r["label"].startswith("unicode-")]
            require({r["label"] for r in unicode_checks}>={f"unicode-{n}" for n in range(1,rounds+1)},"missing Unicode trials in one console direction")
            for r in unicode_checks:
                n=int(r["label"].split("-")[1]);require(r["editor"]["text"]==f"用户{agent.uid}：中文{n:03d}🌍","wrong Unicode document")
                require(r["editor"]["selection_length"]>0,"drag did not select text")
            ime_checks=[r for r in checkpoints if r["console_uid"]==console and r["label"].startswith("pinyin-")]
            require(ime_checks and all(r["editor"]["text"]=="你好世界" and not r["editor"]["marked"] for r in ime_checks),"IME text not committed")
            require(any(e["kind"]=="composition" and e["console_uid"]==console and e["input_source"]=="com.apple.inputmethod.SCIM.ITABC" for e in editor),"no real Pinyin composition")
            require(any(r["label"].startswith("agent-") and r["console_uid"]==console and r["frontmost_pid"]==agent.first["pid"] for r in checkpoints),"no cross-app focus transition")
        print(f"uid={agent.uid}: cross-app Unicode, selection, focus and Pinyin pass in both console directions; {len(posts)} events, {len(tags-counts.keys())} activation events checked by focus")


def verify_recovery(a,b,editor):
    disconnects=[r for r in a.rows if r["kind"]=="disconnected" and r["reason"]=="requested"]
    reconnects=[r for r in a.rows if r["kind"]=="reconnected"]
    require(len(disconnects)==len(reconnects)==1,"one explicit disconnect/reconnect required")
    off,on=disconnects[0],reconnects[0]
    require(on["uptime_ms"]-off["uptime_ms"]>=20000,"disconnect interval too short")
    require(on["epoch"]>off["epoch"],"reconnect did not advance epoch")
    require(not any(off["uptime_ms"]<r["uptime_ms"]<on["uptime_ms"] and r["kind"] in ("frame","script_post","input_post") for r in a.rows),"disconnected agent still delivered frames/input")
    b.verify_interval(off["uptime_ms"]+2000,on["uptime_ms"]-1000)
    require(any(r["kind"]=="script_started" and off["uptime_ms"]<r["uptime_ms"]<on["uptime_ms"] for r in b.rows)
            and any(r["kind"]=="script_finished" and off["uptime_ms"]<r["uptime_ms"]<on["uptime_ms"] for r in b.rows),
            "other user did not finish an input batch while disconnected")
    require(any(r["kind"]=="script_rejected" and r["reason"]=="stale identity or epoch" and r["uptime_ms"]>off["uptime_ms"] for r in a.rows),"old request was not rejected")
    saved=[r for r in editor if r["kind"]=="document" and r["uptime_ms"]<=off["uptime_ms"]][-1]
    retained=[r for r in a.rows if r["kind"]=="checkpoint" and r["label"]=="retained-document" and r["uptime_ms"]>on["uptime_ms"]]
    require(retained and retained[0]["ok"] and retained[0]["editor"]["text"]==saved["text"] and retained[0]["editor"]["pid"]==saved["pid"],"editor/document not retained")
    require(any(r["kind"]=="checkpoint" and r["label"]=="resume-edited" and r["ok"] and r["uptime_ms"]>on["uptime_ms"] for r in a.rows),"could not edit after reconnect")
    print("PASS LOCAL RECONNECT: same editor/document, new epoch, stale requests rejected; other user kept capturing and editing")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",type=Path);parser.add_argument("--recovery",action="store_true")
    args=parser.parse_args()
    try:
        agents=[CAPTURE.Log(args.directory/f"{side}.jsonl") for side in "AB"]
        editors=[read(args.directory/f"{side}-editor.jsonl") for side in "AB"]
        # Explicit disconnect creates a sampling gap; it is checked separately.
        for agent in agents:agent.heartbeats=[h for h in agent.heartbeats if h.get("connected",True)]
        CAPTURE.verify_capture(*agents);verify_cross(agents,editors)
        if args.recovery:verify_recovery(*agents,editors[0])
        print("PASS CROSS-APP / CHINESE; logout, permission revocation and RDP require separate evidence")
    except (CAPTURE.Problem,KeyError,TypeError,ValueError,OSError) as error:
        print(f"FAIL CROSS-APP: {error}");return 1
    return 0


if __name__=="__main__":raise SystemExit(main())
