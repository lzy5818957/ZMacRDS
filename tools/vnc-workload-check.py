#!/usr/bin/env python3
"""Bounded 100-round, two-app VNC test in pre-created synthetic VM editors.

Use a separate process/config per account. Verifies both document and motion
UID/nonce markers before input, keeps draining while idle, and records batch
latency separately from single-key latency. Never accesses a clipboard.
"""
import argparse
import asyncio
import json
import math
import struct
import time

from vnc_check import connect, marker, read_config


async def run(cfg, motion_nonce, prefix, rounds, period):
    async with await connect(cfg, decoder="zrle") as client:
        await asyncio.wait_for(client.screenshot(), 8)
        initial = marker(client.video.as_rgba(), cfg["marker"])
        motion = marker(client.video.as_rgba(), [48, 1098])
        if not initial or initial[:2] != (cfg["uid"],cfg["nonce"]) or not motion or motion[:2] != (cfg["uid"],motion_nonce):
            raise ValueError("both expected synthetic fixtures must be visible before input")
        state = dict(length=initial[2], tick=motion[2], fresh=time.monotonic(),
                     distinct_ticks=1, error=None, visible=time.monotonic())
        condition = asyncio.Condition()
        started = time.monotonic()
        print(json.dumps({"event":"vnc_workload_start","uid":cfg["uid"],
                          "started_monotonic":started}),flush=True)

        async def receive():
            try:
                while True:
                    client.video.refresh()
                    await client.drain()
                    await asyncio.wait_for(client.read(), 6)
                    frame=client.video.as_rgba()
                    document=marker(frame,cfg["marker"])
                    moving=marker(frame,[48,1098])
                    now=time.monotonic()
                    if moving:
                        if moving[:2] != (cfg["uid"],motion_nonce) or moving[2]<state["tick"]:
                            raise ValueError("wrong or reversed motion identity")
                        if moving[2]!=state["tick"]:
                            state.update(tick=moving[2],fresh=now,distinct_ticks=state["distinct_ticks"]+1)
                    if now-state["fresh"]>6:
                        raise ValueError("motion desktop stopped updating")
                    async with condition:
                        if document:
                            if document[:2] != initial[:2]:
                                raise ValueError("foreign document identity")
                            state.update(length=document[2],visible=now)
                        condition.notify_all()
            except Exception as error:
                async with condition:
                    state["error"]=error
                    condition.notify_all()

        async def healthy_pause(seconds):
            deadline=time.monotonic()+seconds
            async with condition:
                while time.monotonic()<deadline:
                    if state["error"]:
                        raise state["error"]
                    try:
                        await asyncio.wait_for(condition.wait(),min(1,deadline-time.monotonic()))
                    except TimeoutError:
                        pass

        async def wait_length(expected):
            async with asyncio.timeout(3):
                async with condition:
                    while True:
                        if state["error"]:
                            raise state["error"]
                        if state["length"]==expected:
                            return state["visible"]
                        await condition.wait()

        async def click(x,y):
            client.mouse.move(x,y)
            await client.drain()
            with client.mouse.hold():
                await client.drain()
                await asyncio.sleep(.08)
            await client.drain()

        def text_events(text):
            for character in text:
                key=ord(character) if ord(character)<256 else 0x01000000|ord(character)
                for down in (1,0):
                    client.writer.write(struct.pack(">BBHI",4,down,0,key))

        reader=asyncio.create_task(receive())
        batches=[]
        try:
            for index in range(rounds):
                await healthy_pause(max(0,started+1+index*period-time.monotonic()))
                # Distinct bundle at left, primary editor at right; no user apps.
                await click(300,850)
                await healthy_pause(.12)
                await click(1000,460)
                await healthy_pause(.12)
                client.keyboard.press("Super_L","a")
                client.keyboard.press("BackSpace")
                await client.drain()
                await wait_length(0)
                text=f"{prefix}-{index:03d}-中文"+("x"*(index%7))
                sent=time.monotonic()
                text_events(text)
                await client.drain()
                visible=await wait_length(len(text))
                batches.append((visible-sent)*1000)
                # Select within the primary text. Next round clears it explicitly.
                client.mouse.move(950,458)
                with client.mouse.hold():
                    await client.drain()
                    for x in (990,1030,1070,1110):
                        client.mouse.move(x,458)
                        await client.drain()
                        await asyncio.sleep(.04)
                await client.drain()
                print(json.dumps({"event":"vnc_round","uid":cfg["uid"],"round":index,
                    "expected_text":text,"batch_visible_ms":batches[-1],"motion_tick":state["tick"]}),flush=True)
            await healthy_pause(max(0,started+cfg["seconds"]-time.monotonic()))
            if state["error"]:
                raise state["error"]
            ordered=sorted(batches)
            return {"event":"vnc_workload_complete","result":"PASS","uid":cfg["uid"],
                "rounds":rounds,"cross_app_clicks":rounds*2,"selection_drags":rounds,
                "seconds":time.monotonic()-started,"distinct_motion_ticks":state["distinct_ticks"],
                "batch_p95_ms":ordered[math.ceil(rounds*.95)-1],
                "latency_scope":"whole variable-length text batch, not single-key latency"}
        finally:
            reader.cancel()
            await asyncio.gather(reader,return_exceptions=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--motion-nonce",type=int,required=True)
    parser.add_argument("--prefix",required=True)
    parser.add_argument("--rounds",type=int,default=100)
    parser.add_argument("--period",type=float,default=18)
    args=parser.parse_args()
    try:
        cfg=read_config(args.config)
        if not 0<args.motion_nonce<2**32 or not 1<=args.rounds<=100 or not .5<=args.period<=18 or not args.prefix.isalnum() or len(args.prefix)>12:
            raise ValueError("invalid bounded workload")
        result=asyncio.run(asyncio.wait_for(run(cfg,args.motion_nonce,args.prefix,args.rounds,args.period),cfg["seconds"]+20))
        print(json.dumps(result),flush=True)
    except Exception as error:
        print(json.dumps({"result":"FAIL","error_type":type(error).__name__,"error":str(error)}),flush=True)
        raise SystemExit(1)
