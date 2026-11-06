#!/usr/bin/env python3
"""Run the two loopback RDP clients and five denial checks prepared by m3-case.py."""
import argparse
import asyncio
import json
import os
from pathlib import Path


async def run(binary, directory):
    if directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
        raise ValueError('case directory must be private and owned by the caller')
    processes = []

    async def start(case, output):
        # Exclusive log creation prevents mixing evidence from previous runs.
        with (directory / (output + '.jsonl')).open('xb') as log, (directory / (output + '.stderr')).open('xb') as err:
            process = await asyncio.create_subprocess_exec(str(binary), str(directory / (case + '.json')),
                                                           stdout=log, stderr=err)
        processes.append(process)
        return process

    async def successful(process, label):
        if await process.wait() != 0:
            raise ValueError(f'{label} failed; inspect its JSONL and stderr')

    async def first_marker(process, label):
        log = directory / (label + '.jsonl')
        for _ in range(200):
            if process.returncode is not None:
                raise ValueError(f'{label} exited before the overlap check')
            # Ignore the final line if the child is in the middle of writing it.
            lines = log.read_bytes().split(b'\n')[:-1]
            if any(json.loads(line).get('event') == 'marker' for line in lines):
                return
            await asyncio.sleep(0.1)
        raise ValueError(f'{label} did not show an authenticated desktop in time')

    async def denial(case, output):
        process = await start(case, output)
        await successful(process, output)
        rows = [json.loads(row) for row in (directory / (output + '.jsonl')).read_text().splitlines()]
        if not any(r.get('event') == 'check_complete' and r.get('result') == 'PASS' and
                   r.get('access_denied') is True and r.get('graphics') == 0 for r in rows):
            raise ValueError(f'{output} returned without an explicit no-graphics denial')

    try:
        a = await start('a', 'a')
        b = await start('b', 'b')
        await asyncio.gather(first_marker(a, 'a'), first_marker(b, 'b'))
        await asyncio.gather(*(denial(case, case) for case in ('unknown-a', 'wrong-a', 'cross-b')))
        if a.returncode is not None or b.returncode is not None:
            raise ValueError('a controller exited during the concurrent negative checks')
        print('Both desktops active; occupied-route denial checks passed.', flush=True)
        await asyncio.gather(successful(a, 'a'), successful(b, 'b'))
        # An occupied route already rejects every login. Retest the wrong
        # passwords with both slots free, so exclusivity cannot mask bad auth.
        await asyncio.gather(denial('wrong-a', 'free-wrong-a'), denial('cross-b', 'free-cross-b'))
        print('RDP clients finished. Collect GUI evidence and run m3-verify.py.')
    finally:
        for process in processes:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        for process in processes:
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('client_binary', type=Path)
    parser.add_argument('case_directory', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(run(args.client_binary.resolve(), args.case_directory.resolve()))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'FAIL: {error}\n')
