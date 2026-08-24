# Setup, filtering and Teleconsole

## Endpoints

Pick the URL that matches the user's network:

| Endpoint | Use |
| --- | --- |
| `https://telecontext.telekom.net/mcp` | Reachable from the internet, no DTAG network needed. Default choice, and the one for proxied clients and Light-managed macOS devices. |
| `https://telecontext.telekom.de/mcp` | Internal DTAG network, no proxy. For Nucleus / DevClient. |

Non-prod equivalents: `https://telecontext-np.telekom.net/mcp` and `https://telecontext-np.telekom.de/mcp`.

Access needs an EntraID account (EMEA1, EMEA2, or Collaboration Network), or an MCICD GitLab account as fallback.

## Client configuration

Claude Code:

```bash
claude mcp add -s project -t http "Telecontext" -- https://telecontext.telekom.net/mcp
```

VS Code / JetBrains Copilot, in `mcp.json`:

```json
{
  "servers": {
    "Telecontext": {
      "type": "http",
      "url": "https://telecontext.telekom.net/mcp"
    }
  }
}
```

Copilot CLI (`~/.copilot/mcp-config.json`), Cursor (`~/.cursor/mcp.json` or `.cursor/mcp.json`) and Windsurf use the same shape under an `mcpServers` key. A project-level file overrides the global one for that repository.

In ChatGPT Enterprise, Telecontext is the pre-approved `DT Telecontext` plugin - no URL, and therefore no URL-based filtering. It exposes `query_company_knowledge` plus the meta-tools, and everything else is reached through `call_tool`.

## Authentication

Most clients open the browser automatically on first connect: authorize Telecontext, allow access, done. Claude Code needs the flow triggered by hand - `/mcp`, select `Telecontext`, select `Authenticate`.

Custom integrations must use an OAuth redirect URI on an allowlisted domain: `*.telekom.de`, `*.telekom.net`, `vscode.dev`, `insiders.vscode.dev`, `chatgpt.com`, `claude.ai`, or `localhost` on any port.

## Tool filtering

Without query parameters, Telecontext loads only `get_time` and the meta-tools `search_tools`, `call_tool`, `call_readonly_tool`. That keeps the context window small while leaving every other tool reachable on demand, and it is the right default for most work.

Load tools up front when the user calls the same cluster constantly and wants the client's own tool picker to show them:

```text
?toolCategories=gitlab,jira
?tools=get_time,query_company_knowledge
?toolCategories=time&tools=query_company_knowledge
```

`toolCategories` takes whole clusters; `tools` takes individual names. Combined, naming an individual tool from a category loads that tool rather than the whole category.

Category ids: `time`, `meta`, `knowledge`, `terminus-devtools`, `terminus-onearch`, `tardis`, `psa`, `psa-verification`, `gitlab`, `jira`, `confluence`, `leanix`, `smnow`, `raccoon`, `chatbot-factory`, `feedback`, plus path-shaped ids for TARDIS-exposed servers such as `/bp/magentaqa-mcp/v1`.

Two limits worth respecting: past roughly 50 loaded tools, client performance and context usage suffer; and a configuration URL beyond 2000 characters breaks some clients' settings parsing. Beyond that, keep the meta-tool loop instead of loading more.

The [MCP Tools page](https://teleconsole.telecontext.telekom.net/mcp-tools) in Teleconsole builds this URL visually - point the user there rather than hand-assembling a long one.

## Teleconsole

The web dashboard at [teleconsole.telecontext.telekom.net](https://teleconsole.telecontext.telekom.net/):

- [Authorization Services](https://teleconsole.telecontext.telekom.net/authorization-services) - connect or revoke per-service access. Which cluster needs which connection is in [TOOLS.md](TOOLS.md).
- [MCP Tools](https://teleconsole.telecontext.telekom.net/mcp-tools) - select tools and copy the generated configuration URL.
- [Activity Log](https://teleconsole.telecontext.telekom.net/activity-log) - the user's own logins, service connections and tool executions, with timestamp, resource, client and status. Filterable by date range, 90-day retention, meta-tool calls excluded. Browser only, and each user sees only their own activity.

## Compliance

Telecontext is approved for work use across Deutsche Telekom: PSA `SYS202534757`, Workers Council `KBR-24180`. No per-team approval is needed. Access to every connected system runs through the user's existing credentials and permissions.
