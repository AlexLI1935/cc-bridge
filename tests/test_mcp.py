import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import ROOT


def test_real_stdio_handshake_and_task_lifecycle(fake_claude, project):
    async def scenario():
        with anyio.fail_after(40):
            server = StdioServerParameters(command=sys.executable,
                                           args=[str(ROOT / "mcp_server.py")], env=dict(os.environ))
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "cc-bridge"
                    listed = await session.list_tools()
                    assert {t.name for t in listed.tools} == {
                        "submit_cc_task", "get_cc_task", "resume_cc_task", "cancel_cc_task", "list_cc_tasks",
                        "get_cc_usage", "refresh_cc_usage", "get_cc_pending", "claim_cc_pending", "done_cc_pending"}
                    async def call(name, args):
                        reply = await session.call_tool(name, args)
                        assert not reply.isError, reply
                        return reply.structuredContent or json.loads(reply.content[0].text)
                    submitted = await call("submit_cc_task", {
                        "goal": "Review", "workdir": str(project), "prompt": "Inspect the README"})
                    assert submitted["success"], submitted
                    task_id = submitted["task_id"]
                    while True:
                        final = await call("get_cc_task", {"task_id": task_id, "full": True})
                        if final["status"] not in ("QUEUED", "RUNNING"):
                            break
                        await anyio.sleep(0.1)
                    assert final["status"] == "COMPLETED", final
                    assert final["result"] == "Review completed"
                    assert (await call("list_cc_tasks", {}))["tasks"][0]["task_id"] == task_id
                    refused = await call("get_cc_task", {"task_id": "../invalid"})
                    assert refused["success"] is False
    anyio.run(scenario)
