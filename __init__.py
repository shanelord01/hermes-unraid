"""Hermes Agent plugin: read-only Unraid server status via the official GraphQL API."""

from . import schemas, tools


def register(ctx):
    ctx.register_tool(
        name="unraid_overview",
        toolset="unraid",
        schema=schemas.OVERVIEW,
        handler=tools.unraid_overview,
    )
    ctx.register_tool(
        name="unraid_disks",
        toolset="unraid",
        schema=schemas.DISKS,
        handler=tools.unraid_disks,
    )
    ctx.register_tool(
        name="unraid_containers",
        toolset="unraid",
        schema=schemas.CONTAINERS,
        handler=tools.unraid_containers,
    )
    ctx.register_tool(
        name="unraid_notifications",
        toolset="unraid",
        schema=schemas.NOTIFICATIONS,
        handler=tools.unraid_notifications,
    )
    ctx.register_tool(
        name="unraid_graphql",
        toolset="unraid",
        schema=schemas.GRAPHQL,
        handler=tools.unraid_graphql,
    )

    def _handle_unraid(raw_args: str) -> str:
        return tools.unraid_overview({})

    ctx.register_command(
        "unraid",
        handler=_handle_unraid,
        description="Quick Unraid server status (array, containers, notifications)",
    )
