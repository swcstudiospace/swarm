#!/usr/bin/env bun
/** Pass-through to scripts/swarm_mcp.py — the stdio MCP protocol core stays in Python. */
import { passthrough } from "./passthrough.ts";
process.exit(passthrough("swarm_mcp"));
