import os
import pytest
import tempfile
from nuu.coding_agent.tools.ls import LsTool
from nuu.coding_agent.tools.read import ReadTool
from nuu.coding_agent.tools.write import WriteTool
from nuu.coding_agent.tools.bash import BashTool


@pytest.mark.asyncio
async def test_ls_tool():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "subdir"))
        with open(os.path.join(tmpdir, "file.txt"), "w") as f:
            f.write("hello")

        tool = LsTool(cwd=tmpdir)
        result = await tool.execute("call_1", {"path": "."})
        output = result.content[0].text
        assert "subdir/" in output
        assert "file.txt" in output


@pytest.mark.asyncio
async def test_read_write_tool():
    with tempfile.TemporaryDirectory() as tmpdir:
        write_tool = WriteTool(cwd=tmpdir)
        await write_tool.execute(
            "call_1", {"path": "test.txt", "content": "hello world"}
        )

        assert os.path.exists(os.path.join(tmpdir, "test.txt"))

        read_tool = ReadTool(cwd=tmpdir)
        result = await read_tool.execute("call_2", {"path": "test.txt"})
        assert result.content[0].text == "hello world"


@pytest.mark.asyncio
async def test_bash_tool():
    with tempfile.TemporaryDirectory() as tmpdir:
        tool = BashTool(cwd=tmpdir)
        result = await tool.execute("call_1", {"command": "echo 'hello bash'"})
        assert "hello bash" in result.content[0].text
