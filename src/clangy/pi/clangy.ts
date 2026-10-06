/**
 * Clangy's pi extension, loaded inside the sandbox.
 *
 * - Replaces pi's assembled system prompt with Clangy's fixed one. pi appends the current date and
 *   any AGENTS.md it finds, which would break prompt caching between runs.
 * - Bridges 127.0.0.1:CLANGY_PORT to the host's unix socket, the sandbox's only way out, and
 *   127.0.0.1:CLANGY_PROXY_PORT to the host's relay to the filtering proxy when one is configured.
 * - Registers the host-side tools. The host assigns numbers and validates the format.
 */

import { readFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { Type } from "@mariozechner/pi-ai";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const socketPath = process.env.CLANGY_SOCKET!;
const systemPrompt = readFileSync(process.env.CLANGY_SYSTEM_PROMPT!, "utf-8");

interface HostReply {
	text: string;
}

function callHost(name: string, params: unknown): Promise<HostReply> {
	return new Promise((resolve, reject) => {
		const request = http.request(
			{ socketPath, path: `/tools/${name}`, method: "POST", headers: { "Content-Type": "application/json" } },
			(response) => {
				let data = "";
				response.on("data", (chunk) => (data += chunk));
				response.on("end", () => {
					const result = JSON.parse(data);
					if (result.error) reject(new Error(result.error));
					else resolve(result);
				});
			},
		);
		request.on("error", reject);
		request.end(JSON.stringify(params));
	});
}

const title = Type.String({ description: "Short, specific title" });
const body = Type.String({ description: "Markdown body, with ## headings" });
const files = Type.Optional(
	Type.Array(
		Type.Object({
			path: Type.String({ description: "An attachments/ path or a file you wrote" }),
			about: Type.String({ description: "What it shows" }),
		}),
		{ description: "Files a developer needs, kept with it" },
	),
);

const tools = [
	{
		name: "set_game",
		description: "Record which game the conversation is about. Loads the matching knowledge.",
		parameters: Type.Object({ game: Type.Union([Type.Literal("se1"), Type.Literal("se2")]) }),
	},
	{
		name: "attach_files",
		description:
			"Attach files from your work folder to your answer, max 5 MB each. Call once per group of related files; " +
			"a group of more than 3 files is sent as one ZIP archive. Each file can be sent only once.",
		parameters: Type.Object({
			paths: Type.Array(Type.String(), { description: "Paths relative to the work folder" }),
			archive: Type.Optional(Type.String({ description: "ZIP file name if the group gets archived" })),
		}),
	},
	{
		name: "ticket_create",
		description: "Create a ticket from a problem report. Search store/tickets for duplicates first.",
		parameters: Type.Object({ title, body, files }),
	},
	{
		name: "ticket_add_report",
		description: "Add a repeated report of the same problem to an existing ticket.",
		parameters: Type.Object({ id: Type.String({ description: "Ticket id, e.g. T-0012" }), body, files }),
	},
	{
		name: "note_create",
		description: "Create a note with shared findings or analysis.",
		parameters: Type.Object({ title, body, files }),
	},
	{
		name: "plan_create",
		description: "Create a plan from an idea or request, e.g. a plugin, mod or script request.",
		parameters: Type.Object({ title, body, files }),
	},
];

function bridge(port: number, path: string) {
	const server = net.createServer((client) => {
		const upstream = net.connect(path);
		client.pipe(upstream).pipe(client);
		client.on("error", () => upstream.destroy());
		upstream.on("error", () => client.destroy());
	});
	server.listen(port, "127.0.0.1");
	server.unref();
}

export default function (pi: ExtensionAPI) {
	bridge(Number(process.env.CLANGY_PORT), socketPath);
	// The filtering proxy, for downloads by commands the agent runs; the env has HTTP(S)_PROXY pointing here
	if (process.env.CLANGY_PROXY_PORT) bridge(Number(process.env.CLANGY_PROXY_PORT), process.env.CLANGY_PROXY_SOCKET!);

	pi.on("before_agent_start", () => ({ systemPrompt }));

	for (const tool of tools) {
		pi.registerTool({
			...tool,
			label: tool.name,
			async execute(_toolCallId, params) {
				const reply = await callHost(tool.name, params);
				return { content: [{ type: "text", text: reply.text }], details: {} };
			},
		});
	}
}
