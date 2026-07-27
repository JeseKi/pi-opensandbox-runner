import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CallToolResultSchema, ListToolsResultSchema } from "@modelcontextprotocol/sdk/types.js";
import * as Type from "typebox";

type ExtensionAPI = {
  on(event: "session_start", handler: () => Promise<void>): void;
  registerTool(tool: unknown): void;
};

type ServerConfig = {
  transport: "streamable-http" | "sse";
  url: string;
  headers: Record<string, string>;
  requestTimeoutMs: number;
};

type Config = { mcpServers: Record<string, ServerConfig> };
type McpTool = {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
};

const ENV_REFERENCE = /\$\{(MCP_[A-Z0-9_]+)\}/g;
const MAX_OUTPUT_BYTES = 1_048_576;

function expand(template: string): string {
  return template.replace(ENV_REFERENCE, (_match, name: string) => process.env[name] ?? "");
}

function parameterSchema(schema: unknown, depth = 0): Type.TSchema {
  if (!schema || typeof schema !== "object" || Array.isArray(schema) || depth > 10) {
    return Type.Any();
  }
  const item = schema as Record<string, unknown>;
  const description = typeof item.description === "string" ? { description: item.description } : {};
  if (Array.isArray(item.enum) && item.enum.every((entry) => typeof entry === "string")) {
    return Type.Union(item.enum.map((entry) => Type.Literal(entry as string)), description);
  }
  switch (item.type) {
    case "string": return Type.String(description);
    case "number":
    case "integer": return Type.Number(description);
    case "boolean": return Type.Boolean(description);
    case "array": return Type.Array(parameterSchema(item.items, depth + 1), description);
    case "object": {
      const properties = item.properties;
      if (!properties || typeof properties !== "object" || Array.isArray(properties)) {
        return Type.Record(Type.String(), Type.Unknown(), description);
      }
      const required = new Set(Array.isArray(item.required) ? item.required : []);
      const converted: Record<string, Type.TSchema> = {};
      for (const [name, child] of Object.entries(properties)) {
        const childSchema = parameterSchema(child, depth + 1);
        converted[name] = required.has(name) ? childSchema : Type.Optional(childSchema);
      }
      return Type.Object(converted, description);
    }
    default: return Type.Any(description);
  }
}

function toolName(serverName: string, name: string): string {
  const safe = `mcp_${serverName}_${name}`.replace(/[^A-Za-z0-9_]/g, "_");
  if (safe.length <= 64) return safe;
  let hash = 0;
  for (const char of safe) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return `${safe.slice(0, 55)}_${Math.abs(hash).toString(36).slice(0, 8)}`;
}

function outputContent(result: unknown): Array<{ type: "text"; text: string }> {
  const raw = JSON.stringify(result);
  const truncated = raw.length > MAX_OUTPUT_BYTES ? `${raw.slice(0, MAX_OUTPUT_BYTES)}\n[truncated]` : raw;
  return [{ type: "text", text: truncated }];
}

async function listTools(client: Client, timeout: number): Promise<McpTool[]> {
  const tools: McpTool[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < 100; page++) {
    const result = await client.request(
      { method: "tools/list", params: cursor ? { cursor } : {} },
      ListToolsResultSchema,
      { timeout },
    );
    tools.push(...(result.tools as McpTool[]));
    cursor = result.nextCursor;
    if (!cursor) return tools;
  }
  throw new Error("MCP tools/list exceeded 100 pages");
}

export default function mcpExtension(pi: ExtensionAPI): void {
  const configPath = process.env.PI_RUNNER_MCP_CONFIG;
  if (!configPath) return;

  pi.on("session_start", async () => {
    let config: Config;
    try {
      config = JSON.parse(await (await import("node:fs/promises")).readFile(configPath, "utf8")) as Config;
    } catch (error) {
      console.error(`[pi-runner-mcp] invalid configuration: ${String(error)}`);
      return;
    }
    for (const [serverName, server] of Object.entries(config.mcpServers)) {
      try {
        const headers = Object.fromEntries(
          Object.entries(server.headers).map(([name, value]) => [name, expand(value)]),
        );
        const transport = server.transport === "sse"
          ? new SSEClientTransport(new URL(server.url), { requestInit: { headers } })
          : new StreamableHTTPClientTransport(new URL(server.url), { requestInit: { headers } });
        const client = new Client({ name: "pi-runner", version: "0.1.0" });
        await client.connect(transport);
        for (const tool of await listTools(client, server.requestTimeoutMs)) {
          pi.registerTool({
            name: toolName(serverName, tool.name),
            label: `MCP ${serverName}: ${tool.name}`,
            description: tool.description ?? `Tool ${tool.name} provided by MCP server ${serverName}.`,
            parameters: parameterSchema(tool.inputSchema),
            async execute(
              _id: string,
              params: Record<string, unknown>,
              signal: AbortSignal | undefined,
            ) {
              try {
                const result = await client.request(
                  { method: "tools/call", params: { name: tool.name, arguments: params } },
                  CallToolResultSchema,
                  { timeout: server.requestTimeoutMs, signal },
                );
                return { content: outputContent(result), details: { server: serverName, tool: tool.name } };
              } catch (error) {
                return {
                  content: [{ type: "text", text: `MCP ${serverName}/${tool.name} failed: ${String(error)}` }],
                  details: { server: serverName, tool: tool.name, error: String(error) },
                  isError: true,
                };
              }
            },
          });
        }
      } catch (error) {
        console.error(`[pi-runner-mcp] failed to start ${serverName}: ${String(error)}`);
      }
    }
  });
}
