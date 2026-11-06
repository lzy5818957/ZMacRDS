#!/usr/bin/env python3
"""Verify synthetic VNC client results against both independent app logs."""
import argparse
import json
import os
import stat


def rows(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        meta=os.fstat(source.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_size>32*1024*1024:
            raise ValueError("owned, bounded regular evidence files required")
        return [json.loads(line) for line in source if line.strip()]


def verify(client,primary,secondary,uid,other_prefix,since):
    complete=[r for r in client if r.get("event")=="vnc_workload_complete"]
    rounds=[r for r in client if r.get("event")=="vnc_round"]
    if len(complete)!=1 or complete[0].get("result")!="PASS" or complete[0].get("seconds",0)<1800:
        raise ValueError("missing successful 30-minute result")
    if len(rounds)!=100 or [r["round"] for r in rounds]!=list(range(100)) or any(r["uid"]!=uid for r in rounds):
        raise ValueError("100 ordered own-account rounds required")
    primary=[r for r in primary if r.get("uptime_ms",0)>=since]
    secondary=[r for r in secondary if r.get("uptime_ms",0)>=since]
    for log in (primary,secondary):
        if not log or any(r.get("uid")!=uid or r.get("gui_uid")!=uid or not r.get("login_complete") for r in log):
            raise ValueError("application identity mismatch")
    primary_pids={r["pid"] for r in primary}
    secondary_pids={r["pid"] for r in secondary}
    if len(primary_pids)!=1 or len(secondary_pids)!=1 or primary_pids==secondary_pids:
        raise ValueError("two independent application processes required")
    documents=[r for r in primary if r["kind"]=="document"]
    if any(other_prefix in r["text"] for r in documents):
        raise ValueError("foreign account text found")
    selected={r["text"] for r in documents if r.get("selection_length",0)>0}
    if any(r["expected_text"] not in selected for r in rounds):
        raise ValueError("missing exact text plus selection evidence for a round")
    if any(r["kind"]=="document" and r.get("text") for r in secondary):
        raise ValueError("typing reached the secondary application")
    main_down=sum(r["kind"]=="event" and r.get("event_type")==1 for r in primary)
    secondary_down=sum(r["kind"]=="event" and r.get("event_type")==1 for r in secondary)
    drags=sum(r["kind"]=="event" and r.get("event_type")==6 for r in primary)
    if main_down<200 or secondary_down<100 or drags<100:
        raise ValueError("missing cross-application mouse/drag receipts")
    hz=complete[0]["distinct_motion_ticks"]/complete[0]["seconds"]
    if hz<15:
        raise ValueError("decoded moving-marker rate below 15 Hz")
    return dict(event="vnc_workload_verified",result="PASS",uid=uid,rounds=100,
                exact_texts_and_selections=100,primary_mouse_down=main_down,
                secondary_mouse_down=secondary_down,drag_events=drags,
                decoded_marker_hz=hz,foreign_texts=0)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client")
    parser.add_argument("primary")
    parser.add_argument("secondary")
    parser.add_argument("--uid",type=int,required=True)
    parser.add_argument("--other-prefix",required=True)
    parser.add_argument("--since-uptime-ms",type=float,default=0)
    args=parser.parse_args()
    try:
        print(json.dumps(verify(rows(args.client),rows(args.primary),rows(args.secondary),
                                args.uid,args.other_prefix,args.since_uptime_ms)))
    except Exception as error:
        print(json.dumps(dict(result="FAIL",error=str(error))))
        raise SystemExit(1)
