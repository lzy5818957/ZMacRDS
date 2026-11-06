#!/usr/bin/env python3
"""Bounded real gateway takeover check with continuously draining viewers."""
import argparse
import asyncio
import json
from contextlib import AsyncExitStack

from vnc_check import connect, marker, read_config


async def viewer(stack, cfg, tasks):
    client=await stack.enter_async_context(await connect(cfg))
    await asyncio.wait_for(client.screenshot(),8)
    state=dict(tick=None,error=None,closed=asyncio.Event())

    def inspect():
        found=marker(client.video.as_rgba(),cfg["marker"])
        if found:
            if found[:2]!=(cfg["uid"],cfg["nonce"]):
                raise ValueError("wrong desktop identity")
            state["tick"]=found[2]
    inspect()

    async def receive():
        try:
            while True:
                client.video.refresh()
                await client.drain()
                await client.read()
                inspect()
        except (ConnectionError,asyncio.IncompleteReadError) as error:
            state["error"]=error
            state["closed"].set()
        except Exception as error:
            state["error"]=error
    tasks.append(asyncio.create_task(receive()))
    return state


async def fresh(state, previous=None):
    async with asyncio.timeout(8):
        while True:
            if state["error"]:
                raise state["error"]
            tick=state["tick"]
            if tick is not None and (previous is None or tick>previous):
                return tick
            await asyncio.sleep(.02)


async def run(a,b):
    if a["port"]!=b["port"] or a["uid"]==b["uid"] or a["username"]==b["username"]:
        raise ValueError("two distinct accounts on the same gateway required")
    tasks=[]
    async with AsyncExitStack() as stack:
        try:
            old=await viewer(stack,a,tasks)
            other=await viewer(stack,b,tasks)
            first=await fresh(old)
            second=await fresh(other)
            wrong=dict(a,password=a["password"]+"-invalid-test")
            try:
                async with await connect(wrong):
                    raise ValueError("wrong password accepted")
            except PermissionError:
                pass
            await fresh(old,first)
            new=await viewer(stack,a,tasks)
            await asyncio.wait_for(old["closed"].wait(),3)
            await fresh(new)
            await fresh(other,second)
            return {"event":"vnc_takeover_check","result":"PASS","uid":a["uid"],
                    "other_uid":b["uid"],"wrong_password_denied":True,
                    "old_survived_wrong_password":True,"old_closed":True,
                    "new_desktop_fresh":True,"other_desktop_fresh":True}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first")
    parser.add_argument("second")
    args=parser.parse_args()
    try:
        result=asyncio.run(asyncio.wait_for(run(read_config(args.first),read_config(args.second)),40))
        print(json.dumps(result),flush=True)
    except Exception as error:
        print(json.dumps({"result":"FAIL","error_type":type(error).__name__,"error":str(error)}),flush=True)
        raise SystemExit(1)
