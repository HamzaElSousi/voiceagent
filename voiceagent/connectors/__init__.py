"""CRM connectors: where leads come from and where call outcomes go.

Vendor-neutral by design. Any CRM can plug in by exposing an MCP server (generic MCP client with a
field mapping), a pair of webhooks, or a CSV export. Nothing in here knows about a specific CRM.
"""
