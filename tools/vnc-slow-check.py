#!/usr/bin/env python3
"""Bounded stalled-receiver fault in one synthetic account; keep the other live."""
import argparse
import asyncio
import json
import socket
import struct
import time
from contextlib import AsyncExitStack

from vnc_check import connect, marker, read_config


async def run(a,b):
    if a["port"]!=b["port"] or a["uid"]==b["uid"]:
        raise ValueError("distinct accounts on one gateway required")
    async with AsyncExitStack() as stack:
        healthy=await stack.enter_async_context(await connect(b))
        await asyncio.wait_for(healthy.screenshot(),8)
        state=dict(tick=None,last=time.monotonic(),max_gap=0,error=None)

        async def receive():
            try:
                while True:
                    healthy.video.refresh()
                    await healthy.drain()
                    await healthy.read()
                    seen=marker(healthy.video.as_rgba(),b["marker"])
                    if seen:
                        if seen[:2]!=(b["uid"],b["nonce"]):
                            raise ValueError("wrong healthy desktop")
                        if seen[2]!=state["tick"]:
                            now=time.monotonic()
                            state.update(tick=seen[2],last=now,max_gap=max(state["max_gap"],now-state["last"]))
            except Exception as error:
                state["error"]=error
        task=asyncio.create_task(receive())
        try:
            slow=await stack.enter_async_context(await connect(a))
            slow.writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
            slow.writer.transport.pause_reading()
            request=b"\3\0"+struct.pack(">HHHH",0,0,slow.video.width,slow.video.height)
            slow.writer.write(request*8)
            await slow.drain()
            before=state["tick"]
            await asyncio.sleep(8)
            slow.writer.transport.resume_reading()
            drained=0
            async with asyncio.timeout(8):
                while block:=await slow.reader.read(32768):
                    drained+=len(block)
                    if drained>32*1024*1024:
                        raise ValueError("stalled receiver was not removed")
            if state["error"]:
                raise state["error"]
            if before is None or state["tick"]<=before or state["max_gap"]>2:
                raise ValueError("healthy account paused during fault")
            restored=await stack.enter_async_context(await connect(a))
            seen=marker(await asyncio.wait_for(restored.screenshot(),8),a["marker"])
            if not seen or seen[:2]!=(a["uid"],a["nonce"]):
                raise ValueError("same account could not recover")
            return {"event":"vnc_slow_receiver","result":"PASS","stalled_uid":a["uid"],
                    "healthy_uid":b["uid"],"old_transport_closed":True,"reconnect_identity_ok":True,
                    "buffered_bytes_drained":drained,"healthy_max_gap_ms":state["max_gap"]*1000}
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stalled")
    parser.add_argument("healthy")
    args=parser.parse_args()
    try:
        result=asyncio.run(asyncio.wait_for(run(read_config(args.stalled),read_config(args.healthy)),40))
        print(json.dumps(result),flush=True)
    except Exception as error:
        print(json.dumps({"result":"FAIL","error_type":type(error).__name__,"error":str(error)}),flush=True)
        raise SystemExit(1)
