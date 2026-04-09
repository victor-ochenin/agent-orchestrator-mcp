"""Entry point for the Agent Orchestrator MCP server."""

import asyncio
from mcp.server.stdio import stdio_server
from orchestrator.server import app


async def main():
    """Run the MCP server via stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
